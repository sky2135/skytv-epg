from __future__ import annotations

import gzip
import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as streaming  # noqa: E402
from epg_catalog_stream import (  # noqa: E402
    CatalogStreamError,
    KNOWN_AMBIGUOUS_PROGRAMME_IDS,
    KNOWN_UNINFORMATIVE_PROGRAMME_IDS,
    infer_catalog_route,
    informative_programme_title,
    parse_all_sources_text,
    stream_catalog_and_programmes_once as production_stream_catalog_once,
)


NOW = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())


def stream_catalog_and_programmes_once(**kwargs):
    """Run tiny XML fixtures without weakening the production default floor."""
    kwargs.setdefault("minimum_unique_channels", 1)
    return production_stream_catalog_once(**kwargs)


def stamp(offset_seconds: int) -> str:
    value = datetime.fromtimestamp(NOW + offset_seconds, tz=timezone.utc)
    return value.strftime("%Y%m%d%H%M%S +0000")


def xml_document(channels: str, programmes: str = "") -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<tv>\n"
        f"{channels}\n"
        f"{programmes}\n"
        "</tv>\n"
    ).encode("utf-8")


class OnePassCatalogTests(unittest.TestCase):
    def write_source(self, root: Path, content: bytes, *, compressed: bool = True) -> Path:
        path = root / ("all.xml.gz" if compressed else "all.xml")
        if compressed:
            with path.open("wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
                    handle.write(content)
        else:
            path.write_bytes(content)
        return path

    def test_catalog_boundary_selection_and_programme_gate(self) -> None:
        channels = """
          <channel id="Alpha.HD.in"><display-name>Alpha TV</display-name></channel>
          <channel id="Alpha.HD.in"><display-name>Alpha HD</display-name></channel>
          <channel id="Beta.ca2"><display-name>Beta</display-name></channel>
        """
        programmes = "\n".join(
            (
                f'<programme channel="Alpha.HD.in" start="{stamp(-600)}" stop="{stamp(3600)}"><title>Morning</title></programme>',
                f'<programme channel="Alpha.HD.in" start="{stamp(3600)}" stop="{stamp(8 * 3600)}"><title>Afternoon</title><category>News</category></programme>',
                f'<programme channel="Beta.ca2" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>Ignored</title></programme>',
            )
        )
        selected_records = []
        selected_channels = []
        selector_observations = []
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )

            def selector(catalog):
                selector_observations.append(tuple(row.epg_id for row in catalog.channels))
                return ["Alpha.HD.in"]

            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=selector,
                window_start=NOW - 86400,
                now_epoch=NOW,
                channel_sink=lambda key, row: selected_channels.append((key, row.epg_id)),
                programme_sink=selected_records.append,
            )
            expected_source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

        self.assertEqual(
            selector_observations,
            [("Alpha.HD.in", "Beta.ca2")],
        )
        self.assertEqual(selected_channels, [("Alpha.HD.in", "Alpha.HD.in")])
        self.assertEqual(len(selected_records), 2)
        self.assertTrue(result.programme_gates["Alpha.HD.in"].passed)
        self.assertEqual(
            result.programme_gates["Alpha.HD.in"].distinct_informative_programmes,
            2,
        )
        self.assertEqual(result.stats.channel_elements, 3)
        self.assertEqual(result.stats.unique_channel_ids, 2)
        self.assertEqual(result.stats.duplicate_channel_ids, 1)
        self.assertEqual(result.stats.programme_elements, 3)
        self.assertEqual(result.stats.selected_programmes, 2)
        self.assertEqual(result.source_sha256, expected_source_sha256)
        self.assertEqual(result.catalog.source_sha256, expected_source_sha256)

    def test_atomic_path_swap_after_hash_is_rejected_before_parse(self) -> None:
        """A replacement cannot pair A's digest with B's XML catalog.

        The former path-based sequence hashed A, scanned A, then reopened and
        parsed B.  Restoring A before the final pathname check could make that
        substitution invisible.  The parser now retains A's descriptor and
        rejects the changed pathname at the next trust boundary.
        """
        document_a = xml_document(
            '<channel id="Snapshot.A.in"><display-name>A</display-name></channel>'
        )
        document_b = xml_document(
            '<channel id="Snapshot.B.in"><display-name>B</display-name></channel>'
        )
        selector_calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.write_source(root, document_a)
            expected_a_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            replacement = root / "replacement.xml.gz"
            held_original = root / "held-original.xml.gz"
            with replacement.open("wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
                    handle.write(document_b)

            real_prefix_check = streaming.reject_unsafe_xml_prefix_handle

            def replace_after_prefix(handle):
                real_prefix_check(handle)
                os.replace(path, held_original)
                os.replace(replacement, path)

            try:
                with mock.patch.object(
                    streaming,
                    "reject_unsafe_xml_prefix_handle",
                    side_effect=replace_after_prefix,
                ):
                    with self.assertRaisesRegex(
                        CatalogStreamError, "changed during verification"
                    ):
                        stream_catalog_and_programmes_once(
                            path=path,
                            fixed_wanted_ids=(),
                            select_provisional_ids=lambda catalog: selector_calls.append(
                                tuple(row.epg_id for row in catalog.channels)
                            )
                            or (),
                            window_start=NOW,
                            now_epoch=NOW,
                        )
            finally:
                if held_original.exists():
                    if path.exists():
                        os.replace(path, replacement)
                    os.replace(held_original, path)

            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), expected_a_sha256
            )
        self.assertEqual(selector_calls, [])

    def test_overlong_channel_xmltv_id_is_rejected_without_truncation(self) -> None:
        overlong = "X" * 301
        channels = f'<channel id="{overlong}"><display-name>Long</display-name></channel>'
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.assertRaisesRegex(CatalogStreamError, "Channel XMLTV ID exceeds"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: (),
                    window_start=NOW,
                    now_epoch=NOW,
                )

    def test_control_character_id_variants_cannot_collapse(self) -> None:
        channels = """
          <channel id="Foo&#x9;Bar.in"><display-name>Tab</display-name></channel>
          <channel id="Foo Bar.in"><display-name>Space</display-name></channel>
        """
        programmes = "\n".join(
            (
                f'<programme channel="Foo&#x9;Bar.in" start="{stamp(0)}" stop="{stamp(4 * 3600)}"><title>One</title></programme>',
                f'<programme channel="Foo Bar.in" start="{stamp(4 * 3600)}" stop="{stamp(8 * 3600)}"><title>Two</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            with self.assertRaisesRegex(CatalogStreamError, "control characters"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: ("Foo Bar.in",),
                    window_start=NOW,
                    now_epoch=NOW,
                )

    def test_overlong_programme_xmltv_id_is_rejected_without_truncation(self) -> None:
        channels = '<channel id="Normal.in"><display-name>Normal</display-name></channel>'
        overlong = "P" * 301
        programmes = (
            f'<programme channel="{overlong}" start="{stamp(0)}">'
            "<title>Long</title></programme>"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            with self.assertRaisesRegex(CatalogStreamError, "Programme XMLTV ID exceeds"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: (),
                    window_start=NOW,
                    now_epoch=NOW,
                )

    def test_overlong_requested_xmltv_ids_are_rejected_without_truncation(self) -> None:
        overlong = "R" * 301
        channels = '<channel id="Normal.in"><display-name>Normal</display-name></channel>'
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.subTest("fixed"):
                with self.assertRaisesRegex(CatalogStreamError, "fixed_wanted_ids exceeds"):
                    stream_catalog_and_programmes_once(
                        path=path,
                        fixed_wanted_ids=(overlong,),
                        select_provisional_ids=lambda _catalog: (),
                        window_start=NOW,
                        now_epoch=NOW,
                    )
            with self.subTest("provisional"):
                with self.assertRaisesRegex(
                    CatalogStreamError, "select_provisional_ids result exceeds"
                ):
                    stream_catalog_and_programmes_once(
                        path=path,
                        fixed_wanted_ids=(),
                        select_provisional_ids=lambda _catalog: (overlong,),
                        window_start=NOW,
                        now_epoch=NOW,
                    )

    def test_fixed_case_variant_resolves_only_when_unique(self) -> None:
        channels = '<channel id="Exact.Case.uk"><display-name>Exact</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Exact.Case.uk" start="{stamp(0)}" stop="{stamp(4 * 3600)}"><title>One</title></programme>',
                f'<programme channel="Exact.Case.uk" start="{stamp(4 * 3600)}" stop="{stamp(8 * 3600)}"><title>Two</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=["exact.case.UK"],
                select_provisional_ids=lambda _catalog: (),
                window_start=NOW,
                now_epoch=NOW,
            )
        self.assertEqual(
            dict(result.resolved_source_ids),
            {"Exact.Case.uk": "exact.case.UK"},
        )
        self.assertFalse(result.unresolved_requested_ids)
        self.assertTrue(result.programme_gates["exact.case.UK"].passed)

    def test_casefold_collision_is_excluded_from_matcher_candidates(self) -> None:
        channels = """
          <channel id="ABC.in"><display-name>ABC upper</display-name></channel>
          <channel id="abc.in"><display-name>ABC lower</display-name></channel>
        """
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: (),
                window_start=NOW,
                now_epoch=NOW,
            )
        self.assertTrue(result.catalog.is_case_ambiguous("ABC.in"))
        self.assertIsNone(result.catalog.unique_casefold("AbC.In"))
        self.assertEqual(result.catalog.matcher_candidates(), [])

    def test_dummy_and_explicit_dummy_ids_are_not_real_match_candidates(self) -> None:
        channels = """
          <channel id="Movie.Dummy.us"><display-name>Movie</display-name></channel>
          <channel id="Special.Placeholder.us"><display-name>Special</display-name></channel>
          <channel id="Real.in"><display-name>Real</display-name></channel>
        """
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: (),
                window_start=NOW,
                now_epoch=NOW,
            )
        candidates = result.catalog.matcher_candidates(
            explicit_dummy_ids=("Special.Placeholder.us",)
        )
        self.assertEqual([row["epg_id"] for row in candidates], ["Real.in"])

    def test_catalog_completeness_floor_fails_before_selection(self) -> None:
        channels = '<channel id="Only.in"><display-name>Only</display-name></channel>'
        selector_calls = []
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.assertRaisesRegex(CatalogStreamError, "completeness floor"):
                production_stream_catalog_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda catalog: selector_calls.append(catalog),
                    window_start=NOW,
                    now_epoch=NOW,
                )
        self.assertEqual(selector_calls, [])

    def test_text_catalog_has_same_floor_and_dummy_partition(self) -> None:
        content = "\n".join(
            (
                "20260915120000",
                "-- epg_ripper_IN1 --",
                "Real.in",
                "SBS World Watch",
                "Opaque\u00a0ID.za",
                "Opaque ID.za",
                "-- epg_ripper_DUMMY_CHANNELS --",
                "Movie.Dummy.us",
            )
        )
        with self.assertRaisesRegex(CatalogStreamError, "completeness floor"):
            parse_all_sources_text(content)
        snapshot = parse_all_sources_text(content, minimum_ids=1)
        real, dummy = snapshot.matcher_inputs()
        self.assertEqual(
            [row["epg_id"] for row in real], ["Real.in", "SBS World Watch"]
        )
        real_by_id = {row["epg_id"]: row for row in real}
        self.assertEqual(real_by_id["Real.in"]["region"], "IN")
        self.assertEqual(real_by_id["SBS World Watch"]["region"], "IN")
        self.assertEqual(real_by_id["SBS World Watch"]["feed"], "IN1")
        self.assertEqual(dummy, {"movie.dummy.us": "Movie.Dummy.us"})
        self.assertIn(
            "Opaque\u00a0ID.za", {entry.epg_id for entry in snapshot.entries}
        )
        self.assertIn("Opaque ID.za", {entry.epg_id for entry in snapshot.entries})

    def test_text_catalog_country_section_must_not_contradict_id_market(self) -> None:
        for section in ("US2", "US_LOCALS1", "US_SPORTS1"):
            with self.subTest(section=section):
                content = "\n".join(
                    (
                        "20260915120000",
                        f"-- epg_ripper_{section} --",
                        "PTC.CHAK.DE.in",
                    )
                )
                snapshot = parse_all_sources_text(content, minimum_ids=1)
                real, dummy = snapshot.matcher_inputs()
                self.assertEqual(real, [])
                self.assertEqual(dummy, {})
                self.assertEqual(snapshot.entries[0].sections, (section,))
                self.assertEqual(snapshot.entries[0].route.region, "IN")
                with self.assertRaisesRegex(
                    CatalogStreamError, "conflicting country evidence"
                ):
                    snapshot.validate_for_unattended_matching()

    def test_text_catalog_real_dummy_ambiguity_fails_matching_preflight(self) -> None:
        content = "\n".join(
            (
                "20260915120000",
                "-- epg_ripper_US_LOCALS1 --",
                "KABC-TV.us_locals1",
                "-- epg_ripper_DUMMY_CHANNELS --",
                "KABC-TV.us_locals1",
            )
        )
        snapshot = parse_all_sources_text(content, minimum_ids=1)
        with self.assertRaisesRegex(CatalogStreamError, "real and dummy"):
            snapshot.validate_for_unattended_matching()

    def test_text_catalog_confusable_shadow_cannot_span_country_markets(self) -> None:
        content = "\n".join(
            (
                "20260915120000",
                "-- epg_ripper_IN1 --",
                "KABC\u00a0TV",
                "-- epg_ripper_US_LOCALS1 --",
                "KABC\u3000TV",
            )
        )
        snapshot = parse_all_sources_text(content, minimum_ids=1)
        with self.assertRaisesRegex(
            CatalogStreamError, "normalization-confusable IDs in multiple feed/market routes"
        ):
            snapshot.validate_for_unattended_matching()

    def test_text_catalog_confusable_shadow_cannot_span_feeds_in_one_market(self) -> None:
        content = "\n".join(
            (
                "20260915120000",
                "-- epg_ripper_US_LOCALS1 --",
                "KABC-TV",
                "-- epg_ripper_US_SPORTS1 --",
                "kabc-tv",
            )
        )
        snapshot = parse_all_sources_text(content, minimum_ids=1)
        with self.assertRaisesRegex(CatalogStreamError, "multiple feed/market routes"):
            snapshot.validate_for_unattended_matching()

    def test_text_catalog_confusable_shadow_cannot_mix_real_and_dummy(self) -> None:
        content = "\n".join(
            (
                "20260915120000",
                "-- epg_ripper_US_LOCALS1 --",
                "kabc-tv.us_locals1",
                "-- epg_ripper_DUMMY_CHANNELS --",
                "KABC-TV.us_locals1",
            )
        )
        snapshot = parse_all_sources_text(content, minimum_ids=1)
        with self.assertRaisesRegex(
            CatalogStreamError, "split between real and dummy sections"
        ):
            snapshot.validate_for_unattended_matching()

    def test_text_catalog_rejects_whitespace_normalization_of_opaque_id(self) -> None:
        for unsafe_id in ("  Foo.in  ", "\tFoo.in\t"):
            with self.subTest(unsafe_id=unsafe_id):
                content = "\n".join(
                    (
                        "20260915120000",
                        "-- epg_ripper_IN1 --",
                        unsafe_id,
                    )
                )
                with self.assertRaisesRegex(
                    CatalogStreamError, "leading/trailing whitespace"
                ):
                    parse_all_sources_text(content, minimum_ids=1)

    def test_selector_must_return_current_exact_catalog_id(self) -> None:
        channels = '<channel id="Alpha.in"><display-name>Alpha</display-name></channel>'
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.assertRaisesRegex(CatalogStreamError, "not present exactly"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: ["alpha.in"],
                    window_start=NOW,
                    now_epoch=NOW,
                )

    def test_placeholder_rows_do_not_pass_gate(self) -> None:
        channels = '<channel id="Placeholder.us2"><display-name>Placeholder</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Placeholder.us2" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>No EPG Available</title></programme>',
                f'<programme channel="Placeholder.us2" start="{stamp(8 * 3600)}" stop="{stamp(16 * 3600)}"><title>TV guide is not available</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ["Placeholder.us2"],
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates["Placeholder.us2"]
        self.assertFalse(gate.passed)
        self.assertEqual(gate.distinct_informative_programmes, 0)
        self.assertFalse(informative_programme_title("Off Air"))
        for title in (
            "No EPG",
            "Unknown",
            "N/A",
            "Not available",
            "Schedule unavailable",
            "Schedule Not Available",
            "Programme unavailable",
            "Program Information Unavailable",
            "TV guide is not available",
            "TV guide unavailable",
            "EPG not available",
            "No EPG Available",
            "No Event Available",
            "No Information 1",
            "N/A - N/A",
            "Unknown show",
            "Pas De Diffusion",
            "Liiga: No Broadcasting",
            "NOVASPORTSEXTRA2HD: Ξανά κοντά σας",
            "Vivez en direct les évènements de CANAL+",
        ):
            with self.subTest(title=title):
                self.assertFalse(informative_programme_title(title))
        self.assertTrue(informative_programme_title("Canada vs USA"))
        for title in (
            "Unknown Showdown",
            "Liiga: Broadcasting Live",
            "Vivez en direct: Paris",
        ):
            with self.subTest(real_title=title):
                self.assertTrue(informative_programme_title(title))

    def test_known_holding_block_id_fails_even_with_informative_titles(self) -> None:
        expected_ids = {
            "Prime.TV.al",
            "Tring.Originals.al",
            "First.Channel.al",
            "TV.Syri.Vision.al",
            *(f"CANAL+LIVE.{number}.fr" for number in range(11, 20)),
            "M+.Liga.de.Campeones.8.es",
            "M+.Liga.de.Campeones.13.es",
            "Novasportsextra3HD.gr",
            "Novasportsextra4HD.gr",
        }
        self.assertEqual(KNOWN_UNINFORMATIVE_PROGRAMME_IDS, expected_ids)
        epg_id = "Prime.TV.al"
        channels = (
            f'<channel id="{epg_id}"><display-name>Prime TV</display-name></channel>'
        )
        programmes = "\n".join(
            (
                f'<programme channel="{epg_id}" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>Morning News</title></programme>',
                f'<programme channel="{epg_id}" start="{stamp(8 * 3600)}" stop="{stamp(16 * 3600)}"><title>Evening Film</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: [epg_id],
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates[epg_id]
        self.assertFalse(gate.passed)
        self.assertIn("holding blocks", gate.reason)

    def test_crosswired_epg_id_fails_even_with_informative_titles(self) -> None:
        epg_id = "KSA.sports.1.ae"
        channels = (
            f'<channel id="{epg_id}"><display-name>KSA Sports 1</display-name></channel>'
        )
        programmes = "\n".join(
            (
                f'<programme channel="{epg_id}" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>Live Match</title></programme>',
                f'<programme channel="{epg_id}" start="{stamp(8 * 3600)}" stop="{stamp(16 * 3600)}"><title>Sports Roundup</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: [epg_id],
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates[epg_id]
        self.assertFalse(gate.passed)
        self.assertIn("cross-wired", gate.reason)

    def test_duplicate_schedule_id_fails_even_with_informative_titles(self) -> None:
        self.assertEqual(len(KNOWN_AMBIGUOUS_PROGRAMME_IDS), 6)
        epg_id = "Al.Anwar.TV.2.ae"
        channels = (
            f'<channel id="{epg_id}"><display-name>Al Anwar 2</display-name></channel>'
        )
        programmes = "\n".join(
            (
                f'<programme channel="{epg_id}" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>Morning News</title></programme>',
                f'<programme channel="{epg_id}" start="{stamp(8 * 3600)}" stop="{stamp(16 * 3600)}"><title>Evening Film</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: [epg_id],
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates[epg_id]
        self.assertFalse(gate.passed)
        self.assertIn("same schedule", gate.reason)

    def test_gate_requires_guide_to_extend_six_hours(self) -> None:
        channels = '<channel id="Short.us2"><display-name>Short</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Short.us2" start="{stamp(0)}" stop="{stamp(1800)}"><title>One</title></programme>',
                f'<programme channel="Short.us2" start="{stamp(1800)}" stop="{stamp(3600)}"><title>Two</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ["Short.us2"],
                window_start=NOW,
                now_epoch=NOW,
            )
        self.assertFalse(result.programme_gates["Short.us2"].passed)
        self.assertIn("safety horizon", result.programme_gates["Short.us2"].reason)

    def test_gate_rejects_only_far_future_programmes(self) -> None:
        channels = '<channel id="Future.us2"><display-name>Future</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Future.us2" start="{stamp(48 * 3600)}" stop="{stamp(49 * 3600)}"><title>Far One</title></programme>',
                f'<programme channel="Future.us2" start="{stamp(49 * 3600)}" stop="{stamp(50 * 3600)}"><title>Far Two</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ("Future.us2",),
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates["Future.us2"]
        self.assertEqual(gate.distinct_informative_programmes, 2)
        self.assertEqual(gate.first_start_epoch, NOW + 48 * 3600)
        self.assertFalse(gate.passed)
        self.assertIn("near-term safety window", gate.reason)

    def test_gate_accepts_programmes_beginning_within_six_hours(self) -> None:
        channels = '<channel id="Near.us2"><display-name>Near</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Near.us2" start="{stamp(5 * 3600)}" stop="{stamp(6 * 3600)}"><title>Near One</title></programme>',
                f'<programme channel="Near.us2" start="{stamp(6 * 3600)}" stop="{stamp(8 * 3600)}"><title>Near Two</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ("Near.us2",),
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates["Near.us2"]
        self.assertEqual(gate.first_start_epoch, NOW + 5 * 3600)
        self.assertTrue(gate.passed)

    def test_gate_rejects_nonpositive_initial_gap_configuration(self) -> None:
        channels = '<channel id="Alpha.in"><display-name>Alpha</display-name></channel>'
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.assertRaisesRegex(CatalogStreamError, "initial-gap window"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: (),
                    window_start=NOW,
                    now_epoch=NOW,
                    gate_maximum_initial_gap_seconds=0,
                )

    def test_title_variants_for_one_interval_count_as_one_programme(self) -> None:
        channels = '<channel id="Variant.us2"><display-name>Variant</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="Variant.us2" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>English title</title></programme>',
                f'<programme channel="Variant.us2" start="{stamp(0)}" stop="{stamp(8 * 3600)}"><title>Titre francais</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(
                Path(temporary), xml_document(channels, programmes)
            )
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ["Variant.us2"],
                window_start=NOW,
                now_epoch=NOW,
                minimum_unique_channels=1,
            )

        gate = result.programme_gates["Variant.us2"]
        self.assertEqual(gate.distinct_informative_programmes, 1)
        self.assertFalse(gate.passed)

    def test_absurd_duration_cannot_make_a_short_guide_pass(self) -> None:
        channels = '<channel id="LongStop.us2"><display-name>Long Stop</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="LongStop.us2" start="{stamp(0)}" stop="{stamp(1800)}"><title>One</title></programme>',
                f'<programme channel="LongStop.us2" start="{stamp(1800)}" stop="{stamp(3600)}"><title>Two</title></programme>',
                f'<programme channel="LongStop.us2" start="{stamp(3600)}" stop="{stamp(7 * 86400)}"><title>Bad Stop</title></programme>',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ("LongStop.us2",),
                window_start=NOW,
                now_epoch=NOW,
            )
        gate = result.programme_gates["LongStop.us2"]
        self.assertFalse(gate.passed)
        self.assertEqual(gate.distinct_informative_programmes, 2)
        self.assertEqual(gate.latest_stop_epoch, NOW + 3600)
        self.assertEqual(result.stats.implausible_gate_programmes, 1)

    def test_synthesized_stops_never_count_as_gate_evidence(self) -> None:
        channels = '<channel id="NoStop.in"><display-name>No Stop</display-name></channel>'
        programmes = "\n".join(
            (
                f'<programme channel="NoStop.in" start="{stamp(0)}"><title>One</title></programme>',
                f'<programme channel="NoStop.in" start="{stamp(5 * 3600)}"><title>Two</title></programme>',
            )
        )
        selected_records = []
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels, programmes))
            result = stream_catalog_and_programmes_once(
                path=path,
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: ("NoStop.in",),
                window_start=NOW,
                now_epoch=NOW,
                programme_sink=selected_records.append,
            )
        gate = result.programme_gates["NoStop.in"]
        self.assertEqual(len(selected_records), 2)
        self.assertEqual(result.stats.synthesized_stop, 2)
        self.assertEqual(gate.distinct_informative_programmes, 0)
        self.assertFalse(gate.passed)

    def test_channel_after_programme_is_rejected(self) -> None:
        content = xml_document(
            '<channel id="First.in"><display-name>First</display-name></channel>',
            f'<programme channel="First.in" start="{stamp(0)}"><title>One</title></programme>\n'
            '<channel id="Late.in"><display-name>Late</display-name></channel>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), content)
            with self.assertRaisesRegex(CatalogStreamError, "channel after programmes"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: (),
                    window_start=NOW,
                    now_epoch=NOW,
                )

    def test_catalog_fingerprint_is_independent_of_channel_order(self) -> None:
        one = """
          <channel id="B.ca2"><display-name>Zulu</display-name></channel>
          <channel id="A.in"><display-name>Alpha</display-name></channel>
        """
        two = """
          <channel id="A.in"><display-name>Alpha</display-name></channel>
          <channel id="B.ca2"><display-name>Zulu</display-name></channel>
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.write_source(root, xml_document(one), compressed=False)
            first.rename(root / "first.xml")
            second = self.write_source(root, xml_document(two), compressed=False)
            second.rename(root / "second.xml")
            result_one = stream_catalog_and_programmes_once(
                path=root / "first.xml",
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: (),
                window_start=NOW,
                now_epoch=NOW,
            )
            result_two = stream_catalog_and_programmes_once(
                path=root / "second.xml",
                fixed_wanted_ids=(),
                select_provisional_ids=lambda _catalog: (),
                window_start=NOW,
                now_epoch=NOW,
            )
        self.assertEqual(
            result_one.catalog.fingerprint_sha256,
            result_two.catalog.fingerprint_sha256,
        )

    def test_channel_element_limit_is_enforced(self) -> None:
        channels = "\n".join(
            f'<channel id="C{index}.in"><display-name>C{index}</display-name></channel>'
            for index in range(3)
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_source(Path(temporary), xml_document(channels))
            with self.assertRaisesRegex(CatalogStreamError, "channel-element limit"):
                stream_catalog_and_programmes_once(
                    path=path,
                    fixed_wanted_ids=(),
                    select_provisional_ids=lambda _catalog: (),
                    window_start=NOW,
                    now_epoch=NOW,
                    maximum_channel_elements=2,
                )

    def test_route_inference_covers_existing_priority_feeds(self) -> None:
        self.assertEqual(infer_catalog_route("Station.us_locals1").feed, "US_LOCALS1")
        self.assertEqual(infer_catalog_route("Station.us2").region, "US")
        self.assertEqual(infer_catalog_route("Station.ca2").feed, "CA2")
        self.assertEqual(infer_catalog_route("Station.uk").region, "UK")
        self.assertEqual(infer_catalog_route("Station.in2").region, "IN")
        self.assertEqual(infer_catalog_route("Station.bein").region, "BEIN")
        self.assertEqual(infer_catalog_route("Station.za").region, "ZA")


if __name__ == "__main__":
    unittest.main()
