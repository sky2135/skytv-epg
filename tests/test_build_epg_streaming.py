from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as runner  # noqa: E402


FIXED_NOW = 1_800_000_000
MAPPING_COLUMNS = (
    "server_id",
    "server_label",
    "stream_id",
    "enabled",
    "channel_name",
    "category_id",
    "category_name",
    "action",
    "source",
    "epg_feed",
    "epg_id",
)


def _mapping_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=MAPPING_COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _snapshot_mapping_bytes(rows: list[dict[str, str]]) -> bytes:
    """Serialize the exact private-Sheet schema used by snapshot attestation."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=runner.SHEET_COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _quarantined_snapshot_row(row: dict[str, str]) -> dict[str, str]:
    quarantined = dict(row)
    quarantined.update(
        {
            "enabled": "FALSE",
            "action": "REVIEW",
            "metadata_status": "review",
            "reason": runner.EFFECTIVE_QUARANTINE_REASON,
        }
    )
    return quarantined


def _mapping_row(
    *,
    server_id: str,
    stream_id: str,
    channel_name: str,
    category_name: str,
    epg_id: str,
    source: str = "epgshare01",
    epg_feed: str = "ALL_SOURCES1",
) -> dict[str, str]:
    return {
        "server_id": server_id,
        "server_label": server_id.replace("_", " ").title(),
        "stream_id": stream_id,
        "enabled": "TRUE",
        "channel_name": channel_name,
        "category_id": "fixture-category",
        "category_name": category_name,
        "action": "APPROVED",
        "source": source,
        "epg_feed": epg_feed,
        "epg_id": epg_id,
    }


def _xmltv_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000"
    )


def _write_deterministic_gzip(path: Path, content: str) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
        ) as compressed:
            compressed.write(content.encode("utf-8"))


def _read_gzip_json(path: Path) -> dict[str, object]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class PrivateMappingSnapshotGuardTests(unittest.TestCase):
    def test_valid_open_alert_quarantine_counts_authoritative_rows_for_floor(self) -> None:
        safe = _mapping_row(
            server_id="server_3",
            stream_id="1",
            channel_name="Safe Channel",
            category_name="General",
            epg_id="Safe.test",
        )
        suspect = _mapping_row(
            server_id="server_3",
            stream_id="2",
            channel_name="Suspect Channel",
            category_name="General",
            epg_id="Suspect.test",
        )
        authoritative = _snapshot_mapping_bytes([safe, suspect])
        effective = _snapshot_mapping_bytes(
            [safe, _quarantined_snapshot_row(suspect)]
        )
        validation = runner.validate_private_mapping_snapshots(
            authoritative, effective, {"server_3"}
        )
        stats = validation["servers"]["server_3"]
        self.assertEqual(stats["authoritative_runnable_rows"], 2)
        self.assertEqual(stats["effective_runnable_rows"], 1)
        self.assertEqual(stats["quarantined_authoritative_runnable_rows"], 1)

        # Exercise the production floor path. Reaching database creation proves
        # the two-row floor used the attested authoritative count, while the
        # effective snapshot still exposes only one row to publication.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authoritative_path = root / "authoritative.csv"
            effective_path = root / "effective.csv"
            manifest_path = root / "manifest.json"
            authoritative_path.write_bytes(authoritative)
            effective_path.write_bytes(effective)
            manifest_path.write_text(
                json.dumps(
                    runner.expected_mapping_snapshot_manifest(validation),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                runner,
                "create_database",
                side_effect=RuntimeError("row floor passed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "row floor passed"):
                    runner.main(
                        [
                            "--mapping-file",
                            str(effective_path),
                            "--mapping-authoritative-file",
                            str(authoritative_path),
                            "--mapping-snapshot-manifest",
                            str(manifest_path),
                            "--public-dir",
                            str(root / "public"),
                            "--work-dir",
                            str(root / "work"),
                            "--servers",
                            "server_3",
                            "--minimum-server-rows",
                            "server_3=2",
                        ]
                    )

    def test_snapshot_pair_rejects_a_dropped_row(self) -> None:
        first = _mapping_row(
            server_id="server_3",
            stream_id="1",
            channel_name="One",
            category_name="General",
            epg_id="One.test",
        )
        second = _mapping_row(
            server_id="server_3",
            stream_id="2",
            channel_name="Two",
            category_name="General",
            epg_id="Two.test",
        )
        with self.assertRaisesRegex(
            runner.BuildError, "row identity or order mismatch"
        ):
            runner.validate_private_mapping_snapshots(
                _snapshot_mapping_bytes([first, second]),
                _snapshot_mapping_bytes([first]),
                {"server_3"},
            )

    def test_snapshot_pair_rejects_identity_or_epg_edits_hidden_as_quarantine(self) -> None:
        authoritative_row = _mapping_row(
            server_id="server_3",
            stream_id="7",
            channel_name="Original Channel",
            category_name="General",
            epg_id="Original.test",
        )
        for field, value in (
            ("channel_name", "Replacement Channel"),
            ("epg_id", "Replacement.test"),
        ):
            with self.subTest(field=field):
                effective_row = _quarantined_snapshot_row(authoritative_row)
                effective_row[field] = value
                with self.assertRaisesRegex(
                    runner.BuildError, "exact OPEN-alert quarantine transform"
                ):
                    runner.validate_private_mapping_snapshots(
                        _snapshot_mapping_bytes([authoritative_row]),
                        _snapshot_mapping_bytes([effective_row]),
                        {"server_3"},
                    )

    def test_quarantining_an_already_disabled_row_adds_no_floor_credit(self) -> None:
        disabled = _mapping_row(
            server_id="server_3",
            stream_id="8",
            channel_name="Already Disabled",
            category_name="General",
            epg_id="Disabled.test",
        )
        disabled["enabled"] = "FALSE"
        validation = runner.validate_private_mapping_snapshots(
            _snapshot_mapping_bytes([disabled]),
            _snapshot_mapping_bytes([_quarantined_snapshot_row(disabled)]),
            {"server_3"},
        )
        stats = validation["servers"]["server_3"]
        self.assertEqual(stats["authoritative_runnable_rows"], 0)
        self.assertEqual(stats["effective_runnable_rows"], 0)
        self.assertEqual(stats["quarantined_authoritative_runnable_rows"], 0)
        self.assertEqual(stats["quarantined_already_ineligible_rows"], 1)

    def test_production_shaped_quarantine_keeps_integrity_floor_above_9400(self) -> None:
        authoritative_rows = [
            _mapping_row(
                server_id="server_3",
                stream_id=str(index),
                channel_name=f"Channel {index}",
                category_name="General",
                epg_id="Shared.fixture",
            )
            for index in range(9_943)
        ]
        effective_rows = [
            (
                _quarantined_snapshot_row(row)
                if index < 1_180
                else row
            )
            for index, row in enumerate(authoritative_rows)
        ]
        validation = runner.validate_private_mapping_snapshots(
            _snapshot_mapping_bytes(authoritative_rows),
            _snapshot_mapping_bytes(effective_rows),
            {"server_3"},
        )
        stats = validation["servers"]["server_3"]
        self.assertEqual(stats["authoritative_runnable_rows"], 9_943)
        self.assertEqual(stats["effective_runnable_rows"], 8_763)
        self.assertEqual(stats["quarantined_authoritative_runnable_rows"], 1_180)
        self.assertGreaterEqual(stats["authoritative_runnable_rows"], 9_400)
        self.assertLess(stats["effective_runnable_rows"], 9_400)

    def test_true_authoritative_truncation_remains_below_9400(self) -> None:
        authoritative_rows = [
            _mapping_row(
                server_id="server_3",
                stream_id=str(index),
                channel_name=f"Channel {index}",
                category_name="General",
                epg_id="Shared.fixture",
            )
            for index in range(9_399)
        ]
        content = _snapshot_mapping_bytes(authoritative_rows)
        validation = runner.validate_private_mapping_snapshots(
            content, content, {"server_3"}
        )
        stats = validation["servers"]["server_3"]
        self.assertEqual(stats["authoritative_runnable_rows"], 9_399)
        self.assertLess(stats["authoritative_runnable_rows"], 9_400)


class MappingContractTests(unittest.TestCase):
    def test_published_http_urls_reject_private_components(self) -> None:
        clean = "https://logos.example/channel.png"
        self.assertEqual(runner.valid_http_url(clean), clean)
        self.assertEqual(
            runner.valid_http_url(
                "logos/channel.png",
                base_url="https://source.example/feeds/catalog.xml.gz",
            ),
            "https://source.example/feeds/logos/channel.png",
        )
        for unsafe in (
            "https://user:password@logos.example/channel.png",
            "https://logos.example/channel.png?token=private",
            "https://logos.example/channel.png#private",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(runner.valid_http_url(unsafe), "")
        self.assertEqual(
            runner.valid_http_url(
                "logos/channel.png?token=private",
                base_url="https://source.example/feeds/catalog.xml.gz",
            ),
            "",
        )

    def test_panel_http_requires_an_explicit_opt_in(self) -> None:
        credentials = {
            "SERVER_2_BASE_URL": "http://panel.example:8080/provider",
            "SERVER_2_USERNAME": "fixture-user",
            "SERVER_2_PASSWORD": "fixture-password",
        }
        with mock.patch.dict(runner.os.environ, credentials, clear=True):
            with self.assertRaises(runner.BuildError):
                runner.panel_credentials("server_2")

        credentials["ALLOW_INSECURE_PANEL_HTTP"] = "TRUE"
        with mock.patch.dict(runner.os.environ, credentials, clear=True):
            with mock.patch("builtins.print"):
                self.assertEqual(
                    runner.panel_credentials("server_2"),
                    (
                        "http://panel.example:8080/provider",
                        "fixture-user",
                        "fixture-password",
                    ),
                )

        credentials["SERVER_2_BASE_URL"] = (
            "https://panel.example/provider?username=should-not-be-here"
        )
        with mock.patch.dict(runner.os.environ, credentials, clear=True):
            with self.assertRaises(runner.BuildError):
                runner.panel_credentials("server_2")

    def test_short_panel_password_fails_before_any_network_fetch(self) -> None:
        credentials = {
            "SERVER_2_BASE_URL": "https://panel.example/provider",
            "SERVER_2_USERNAME": "fixture-user",
            "SERVER_2_PASSWORD": "abc",
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "panel.xmltv"
            with mock.patch.dict(runner.os.environ, credentials, clear=True):
                with mock.patch.object(runner.requests, "Session") as session:
                    with self.assertRaisesRegex(
                        runner.BuildError, "password is too short"
                    ):
                        runner.download_panel_xmltv("server_2", destination)
                    session.assert_not_called()
            self.assertFalse(destination.exists())

    def test_xmltv_timezone_rejects_invalid_hour_or_minute_fields(self) -> None:
        self.assertIsNotNone(runner.parse_xmltv_time("20270115093000 +0530"))
        self.assertIsNone(runner.parse_xmltv_time("20270115093000 +0060"))
        self.assertIsNone(runner.parse_xmltv_time("20270115093000 -1260"))
        self.assertIsNone(runner.parse_xmltv_time("20270115093000 +2400"))

    def test_repository_source_paths_cannot_be_used_as_destructive_outputs(self) -> None:
        with self.assertRaisesRegex(runner.BuildError, "work directory"):
            runner.validate_build_paths(
                work_dir=REPO_ROOT / "scripts",
                public_dir=REPO_ROOT / "public",
                repository_root=REPO_ROOT,
            )
        with self.assertRaisesRegex(runner.BuildError, "public directory"):
            runner.validate_build_paths(
                work_dir=REPO_ROOT / ".build" / "work",
                public_dir=REPO_ROOT / "src",
                repository_root=REPO_ROOT,
            )
        with self.assertRaisesRegex(runner.BuildError, "repository"):
            runner.validate_build_paths(
                work_dir=REPO_ROOT / ".build" / "work",
                public_dir=REPO_ROOT,
                repository_root=REPO_ROOT,
            )

    def test_staging_symlink_cannot_redirect_cleanup_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            external = root / "external-sentinel"
            (repository / ".build").mkdir(parents=True)
            external.mkdir()
            sentinel = external / "must-survive.txt"
            sentinel.write_text("keep", encoding="utf-8")
            (repository / "public.staging").symlink_to(
                external, target_is_directory=True
            )

            with self.assertRaisesRegex(runner.BuildError, "symbolic link"):
                runner.validate_build_paths(
                    work_dir=repository / ".build" / "work",
                    public_dir=repository / "public",
                    repository_root=repository,
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_spreadsheet_literal_escape_is_removed_before_mapping_use(self) -> None:
        row = _mapping_row(
            server_id="server_1",
            stream_id="1",
            channel_name="'=Formula-looking channel label",
            category_name="Entertainment",
            epg_id="Safe.test",
        )
        parsed = runner.parse_mapping_csv(_mapping_bytes([row]), {"server_1"})
        self.assertEqual(parsed[0].channel_name, "=Formula-looking channel label")

    def test_csv_reports_escape_formula_like_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.csv"
            runner.write_csv(
                path,
                ("label", "count"),
                (("=DANGEROUS()", 3), ("  -provider label", 4)),
            )
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[1], ["'=DANGEROUS()", "3"])
            self.assertEqual(rows[2], ["'  -provider label", "4"])

    def test_server_1_quarantines_panel_rows_but_server_2_keeps_panel(self) -> None:
        content = _mapping_bytes(
            [
                _mapping_row(
                    server_id="Server 1",
                    stream_id="101",
                    channel_name="PTC Punjabi Gurbani",
                    category_name="Punjabi Religious",
                    epg_id="Gurbani.Punjabi.test",
                    source="panel",
                    epg_feed="server xmltv.php",
                ),
                _mapping_row(
                    server_id="server_2",
                    stream_id="202",
                    channel_name="Willow Punjabi Cricket",
                    category_name="Punjabi Sports",
                    epg_id="Willow.Punjabi.test",
                    source="",
                    epg_feed="server xmltv.php",
                ),
            ]
        )

        rows = runner.parse_mapping_csv(content, {"server_1", "server_2"})
        by_server = {row.server_id: row for row in rows}

        server_1 = by_server["server_1"]
        self.assertEqual(server_1.requested_source, "panel")
        self.assertEqual(server_1.effective_source, "epgshare01")
        self.assertEqual(server_1.source_key, "epgshare01")
        self.assertTrue(server_1.source_policy_blocked)
        self.assertFalse(server_1.runtime_eligible)
        self.assertEqual(
            server_1.schedule_key, "PANEL::Gurbani.Punjabi.test"
        )

        server_2 = by_server["server_2"]
        self.assertEqual(server_2.requested_source, "panel")
        self.assertEqual(server_2.effective_source, "panel")
        self.assertEqual(server_2.source_key, "panel:server_2")
        self.assertFalse(server_2.source_policy_blocked)
        self.assertTrue(server_2.runtime_eligible)
        self.assertEqual(server_2.schedule_key, "PANEL::Willow.Punjabi.test")

    def test_punjabi_language_religion_and_cricket_are_independent_dimensions(self) -> None:
        rows = runner.parse_mapping_csv(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="1",
                        channel_name="PTC Punjabi Gurbani",
                        category_name="Punjabi Religious",
                        epg_id="Gurbani.Punjabi.test",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="2",
                        channel_name="Willow Punjabi Cricket",
                        category_name="Punjabi Sports",
                        epg_id="Willow.Punjabi.test",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="3",
                        channel_name="Punjabi General Entertainment",
                        category_name="Punjabi Entertainment",
                        epg_id="General.Punjabi.test",
                    ),
                ]
            ),
            {"server_1"},
        )
        by_stream = {row.stream_id: row.metadata for row in rows}

        religious = by_stream["1"]
        self.assertEqual(religious.primary_language, "pa")
        self.assertEqual(religious.languages, ["pa"])
        self.assertEqual(religious.genre, "religion")
        self.assertEqual(religious.religions, ["sikhism"])
        self.assertEqual(religious.sports, [])

        cricket = by_stream["2"]
        self.assertEqual(cricket.primary_language, "pa")
        self.assertEqual(cricket.genre, "sports")
        self.assertEqual(cricket.sports, ["cricket"])
        self.assertEqual(cricket.religions, [])

        general = by_stream["3"]
        self.assertEqual(general.primary_language, "pa")
        self.assertEqual(general.genre, "entertainment")
        self.assertEqual(general.religions, [])
        self.assertEqual(general.sports, [])

        taxonomy = runner.taxonomy_payload()
        self.assertEqual(taxonomy["filterSemantics"]["dimensions"], "AND")
        self.assertTrue(
            taxonomy["filterSemantics"]["parentalExclusionRunsFirst"]
        )
        self.assertEqual(
            taxonomy["exampleRecipes"]["punjabi_sikh"],
            {
                "languages": ["pa"],
                "genre": ["religion"],
                "religions": ["sikhism"],
            },
        )
        self.assertEqual(
            taxonomy["exampleRecipes"]["cricket_channels"],
            {"genre": ["sports"], "sports": ["cricket"]},
        )

    def test_known_language_does_not_require_a_known_genre(self) -> None:
        metadata = runner.build_metadata(
            {
                "channel_name": "PTC Punjabi",
                "canonical_name": "PTC Punjabi",
                "category_name": "Punjabi",
                "epg_id": "PTCPunjabi.test",
            },
            2,
        )
        self.assertEqual(metadata.languages, ["pa"])
        self.assertEqual(metadata.genre, "unknown")
        self.assertGreaterEqual(metadata.confidence, 0.70)
        dimensions = runner.personalization_dimensions(metadata)
        self.assertIn("languages", dimensions)
        self.assertNotIn("genre", dimensions)

    def test_language_prefixes_work_at_each_evidence_field_boundary(self) -> None:
        cases = (("PB: Example", "pa"), ("HIN: Example", "hi"),
                 ("ENG: Example", "en"), ("GJR: Example", "gu"))
        for channel_name, expected in cases:
            with self.subTest(channel_name=channel_name):
                metadata = runner.build_metadata(
                    {
                        "category_name": "General",
                        "channel_name": channel_name,
                        "canonical_name": channel_name,
                        "epg_id": "Fixture.test",
                    },
                    2,
                )
                self.assertEqual(metadata.primary_language, expected)
                self.assertEqual(metadata.languages, [expected])

    def test_identity_specific_sport_wins_over_conflicting_provider_category(self) -> None:
        cricket = runner.build_metadata(
            {
                "channel_name": "SuperSports PSL",
                "canonical_name": "SuperSports PSL",
                "category_name": "Football",
                "epg_id": "SuperSports.PSL.test",
            },
            2,
        )
        hockey = runner.build_metadata(
            {
                "channel_name": "NHL Network",
                "canonical_name": "NHL Network",
                "category_name": "NFL",
                "epg_id": "NHL.Network.test",
            },
            3,
        )
        self.assertEqual(cricket.genre, "sports")
        self.assertEqual(cricket.sports, ["cricket"])
        self.assertEqual(hockey.genre, "sports")
        self.assertEqual(hockey.sports, ["ice_hockey"])

    def test_structured_india_prefix_groups_dummy_star_sports_logically(self) -> None:
        metadata = runner.build_metadata(
            {
                "channel_name": "IN | Star Sports 1",
                "canonical_name": "Star Sports 1",
                "category_name": "Sports",
                "source": "dummy",
                "epg_feed": "DUMMY_CHANNELS",
                "epg_id": "Star.Sports.Dummy.us",
            },
            2,
        )
        self.assertEqual(metadata.region, "south_asia")
        self.assertEqual(metadata.countries, ["IN"])
        self.assertEqual(metadata.genre, "sports")
        self.assertEqual(metadata.sports, ["multi_sport"])

        us_station = runner.build_metadata(
            {
                "channel_name": "US | Locals US PBS (WNIN) IN | Evansville",
                "canonical_name": "WNIN Evansville",
                "category_name": "US Locals",
                "epg_id": "WNIN-DT.us_locals1",
                "epg_feed": "US_LOCALS1",
            },
            3,
        )
        self.assertEqual(us_station.region, "north_america")
        self.assertEqual(us_station.countries, ["US"])

        for channel_name in ("In-Depth News", "IN-Touch TV"):
            with self.subTest(channel_name=channel_name):
                us_brand = runner.build_metadata(
                    {
                        "channel_name": channel_name,
                        "canonical_name": channel_name,
                        "category_name": "US News",
                        "epg_id": "Brand.us",
                        "epg_feed": "US2",
                    },
                    4,
                )
                self.assertEqual(us_brand.region, "north_america")
                self.assertEqual(us_brand.countries, ["US"])

    def test_faith_brand_evidence_is_specific_without_language_assumptions(self) -> None:
        cases = (
            ("Jinvani TV", "jainism"),
            ("Iqra TV", "islam"),
            ("TBN Inspire", "christianity"),
            ("Amritwani", "hinduism"),
            ("Gurkirpa", "sikhism"),
        )
        for channel_name, expected_faith in cases:
            with self.subTest(channel_name=channel_name):
                metadata = runner.build_metadata(
                    {
                        "channel_name": channel_name,
                        "canonical_name": channel_name,
                        "category_name": "Faith",
                        "epg_id": channel_name.replace(" ", ".") + ".test",
                    },
                    2,
                )
                self.assertEqual(metadata.genre, "religion")
                self.assertEqual(metadata.religions, [expected_faith])
                self.assertEqual(metadata.primary_language, "und")

    def test_explicit_family_audience_is_an_eligible_dimension(self) -> None:
        metadata = runner.build_metadata(
            {
                "channel_name": "Punjabi Family",
                "canonical_name": "Punjabi Family",
                "language_codes": "pa",
                "primary_language": "pa",
                "genre": "entertainment",
                "audience_codes": "family",
                "metadata_status": "approved",
                "metadata_confidence": "0.95",
            },
            2,
        )
        dimensions = runner.personalization_dimensions(metadata)
        self.assertIn("languages", dimensions)
        self.assertIn("genre", dimensions)
        self.assertIn("audiences", dimensions)

    def test_adult_content_preempts_movie_terms_but_not_adult_swim(self) -> None:
        adult = runner.build_metadata(
            {
                "channel_name": "XX: PORNO MOVIES TV",
                "canonical_name": "Porno Movies TV",
                "category_name": "FOR ADULTS",
                "epg_id": "AdultMovies.test",
            },
            2,
        )
        self.assertEqual(adult.genre, "adult")
        self.assertEqual(adult.content_rating, "adult")
        self.assertGreaterEqual(adult.confidence, 0.95)

        for disguised_adult in (
            "IN | ＡＤＵＬＴ",
            "ＰＯＲＮ Movies",
            "P\u200born Movies",
            "Pörn Movies",
            "Pоrn Movies",
            "PОrn Movies",
            "Pοrn Movies",
            "PΟrn Movies",
            "Аdult Movies",
            "аdult Movies",
            "Aԁult Movies",
            "AԀult Movies",
            "ΧΧΧ Movies",
            "χχχ Movies",
            "рorn Movies",
            "РORN Movies",
            "aduӏt Movies",
            "ADUӀT Movies",
            "ххх Movies",
            "ХХХ Movies",
            "××× Movies",
            "еrotic Movies",
            "ЕROTIC Movies",
            "erotіc Movies",
            "EROTІC Movies",
            "erotiс Movies",
            "EROTIС Movies",
            "аdult swim рorn",
            "αdυӏτ Movies",
            "ΑDΥӀΤ Movies",
            "ρorn Movies",
            "ΡORN Movies",
            "εroτιϲ Movies",
            "ΕROΤΙϹ Movies",
            "ѕeχτreme Movies",
            "ЅEΧΤREME Movies",
            "plaуboy Movies",
            "PLAУBOY Movies",
            "Ροrn Movies",
            "αdult Movies",
            "adυlt Movies",
            "adulτ Movies",
            "εrotic Movies",
            "erotιc Movies",
            "erotiϲ Movies",
            "ѕextreme Movies",
        ):
            with self.subTest(disguised_adult=disguised_adult):
                genre, _subgenres, confidence = runner.infer_genre(disguised_adult)
                self.assertEqual(genre, "adult")
                self.assertGreaterEqual(confidence, 0.95)

        adult_swim = runner.build_metadata(
            {
                "channel_name": "CA - ADULT SWIM HD",
                "canonical_name": "Adult Swim",
                "category_name": "US Kids",
                "epg_id": "Adult.Swim.ca2",
            },
            3,
        )
        self.assertEqual(adult_swim.genre, "kids")
        self.assertEqual(adult_swim.content_rating, "general")
        self.assertNotEqual(runner.infer_genre("Аdult Swim")[0], "adult")
        self.assertNotEqual(runner.infer_genre("αdυӏτ Swim")[0], "adult")
        self.assertNotEqual(runner.infer_genre("Spоrts Channel")[0], "sports")

        for channel_name in (
            "Sex and the City 24/7",
            "Sex Education 24/7",
            "Stingray Pop Adult",
            "Music Choice Adult Alternative",
        ):
            safe = runner.build_metadata(
                {
                    "channel_name": channel_name,
                    "canonical_name": channel_name,
                    "category_name": "Music and Entertainment",
                    "epg_id": channel_name.replace(" ", ".") + ".test",
                },
                4,
            )
            self.assertNotEqual(safe.genre, "adult", channel_name)
            self.assertEqual(safe.content_rating, "general", channel_name)

        dummy_adult = runner.build_metadata(
            {
                "channel_name": "Carib Penthouse",
                "canonical_name": "Carib Penthouse",
                "category_name": "Caribbean",
                "source": "dummy",
                "epg_feed": "DUMMY_CHANNELS",
                "epg_id": "Adult.Programming.Dummy.us",
            },
            5,
        )
        self.assertEqual(dummy_adult.genre, "adult")
        self.assertEqual(dummy_adult.content_rating, "adult")
        self.assertEqual(dummy_adult.region, "caribbean")

        contradictory_explicit = runner.build_metadata(
            {
                "channel_name": "Explicit Adult Fixture",
                "genre": "adult",
                "audience_codes": "general",
                "content_rating": "general",
                "metadata_status": "approved",
                "metadata_confidence": "1.0",
            },
            6,
        )
        self.assertEqual(contradictory_explicit.content_rating, "adult")
        self.assertEqual(contradictory_explicit.audiences, ["adults"])

        concealed_by_explicit_movie_genre = runner.build_metadata(
            {
                "channel_name": "FOR ADULTS PORNO MOVIES",
                "genre": "movies",
                "audience_codes": "general",
                "content_rating": "general",
                "metadata_status": "approved",
                "metadata_confidence": "1.0",
            },
            7,
        )
        self.assertEqual(concealed_by_explicit_movie_genre.genre, "adult")
        self.assertEqual(concealed_by_explicit_movie_genre.content_rating, "adult")
        self.assertEqual(concealed_by_explicit_movie_genre.audiences, ["adults"])

    def test_duplicate_server_and_stream_mapping_is_rejected(self) -> None:
        content = _mapping_bytes(
            [
                _mapping_row(
                    server_id="server_1",
                    stream_id="7",
                    channel_name="First Name",
                    category_name="General",
                    epg_id="First.test",
                ),
                _mapping_row(
                    server_id="Server 1",
                    stream_id="7",
                    channel_name="Second Name",
                    category_name="General",
                    epg_id="Second.test",
                ),
            ]
        )

        with self.assertRaisesRegex(
            runner.BuildError,
            r"Rows 2 and 3 duplicate \(server_1, 7\)",
        ):
            runner.parse_mapping_csv(content, {"server_1"})

    def test_unknown_source_action_and_contradictory_feed_fail_closed(self) -> None:
        cases = (
            ({"source": "panle"}, "invalid source"),
            ({"action": "AUTO_MATCH"}, "invalid action"),
            (
                {"source": "epgshare01", "epg_feed": "server xmltv.php"},
                "different source types",
            ),
            (
                {"action": "KEEP_PANEL", "source": "epgshare01"},
                "KEEP_PANEL requires source=panel",
            ),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                row = _mapping_row(
                    server_id="server_2",
                    stream_id="9",
                    channel_name="Safety Fixture",
                    category_name="General",
                    epg_id="Safety.test",
                )
                row.update(changes)
                with self.assertRaisesRegex(runner.BuildError, message):
                    runner.parse_mapping_csv(_mapping_bytes([row]), {"server_2"})

    def test_canonical_xmltv_id_cannot_be_stolen_by_a_better_alias(self) -> None:
        rows = runner.parse_mapping_csv(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="1",
                        channel_name="Canonical.test",
                        category_name="General",
                        epg_id="Canonical.test",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="2",
                        channel_name="Canonical.test",
                        category_name="General",
                        epg_id="Other.test",
                    ),
                ]
            ),
            {"server_1"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            connection = runner.create_database(Path(temporary) / "spool.sqlite3")
            try:
                for epg_id, title, quality in (
                    ("Canonical.test", "Canonical schedule", 1),
                    ("Other.test", "Richer alias schedule", 999),
                ):
                    connection.execute(
                        "INSERT INTO channels VALUES (?, ?, ?, ?, '')",
                        ("epgshare01", epg_id, epg_id, title),
                    )
                    connection.execute(
                        "INSERT INTO programmes VALUES (?, ?, ?, ?, ?, '', '', '[]', ?)",
                        (
                            "epgshare01",
                            epg_id,
                            FIXED_NOW,
                            FIXED_NOW + 3600,
                            title,
                            quality,
                        ),
                    )
                connection.commit()
                stats = runner.load_schedule_stats(connection, FIXED_NOW)
                entries, _streams, _conflicts = runner.build_xml_entries(
                    rows=rows,
                    connection=connection,
                    schedule_stats=stats,
                    icon_overrides=[],
                )
                chosen = entries["Canonical.test"]
                self.assertEqual(chosen.entry_type, "canonical_epg_id")
                self.assertEqual(chosen.channel_key, "Canonical.test")
                self.assertEqual(chosen.entry_priority, 2)
            finally:
                connection.close()

    def test_equal_quality_alias_resolution_ignores_sheet_row_order(self) -> None:
        rows = runner.parse_mapping_csv(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="20",
                        channel_name="Shared Alias",
                        category_name="Entertainment",
                        epg_id="B.test",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="10",
                        channel_name="Shared Alias",
                        category_name="Entertainment",
                        epg_id="A.test",
                    ),
                ]
            ),
            {"server_1"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            connection = runner.create_database(Path(temporary) / "spool.sqlite3")
            try:
                stats: dict[tuple[str, str], runner.ScheduleStats] = {}
                for row in rows:
                    connection.execute(
                        "INSERT INTO channels VALUES (?, ?, ?, ?, '')",
                        (row.source_key, row.epg_id, row.epg_id, row.epg_id),
                    )
                    connection.execute(
                        "INSERT INTO programmes VALUES (?, ?, ?, ?, ?, '', '', '[]', ?)",
                        (
                            row.source_key,
                            row.epg_id,
                            FIXED_NOW,
                            FIXED_NOW + 3600,
                            "Same quality",
                            10,
                        ),
                    )
                connection.commit()
                stats = runner.load_schedule_stats(connection, FIXED_NOW)
                forward, _, _ = runner.build_xml_entries(
                    rows=rows,
                    connection=connection,
                    schedule_stats=stats,
                    icon_overrides=[],
                )
                reverse, _, _ = runner.build_xml_entries(
                    rows=list(reversed(rows)),
                    connection=connection,
                    schedule_stats=stats,
                    icon_overrides=[],
                )
                self.assertEqual(
                    forward["Shared Alias"].channel_key,
                    reverse["Shared Alias"].channel_key,
                )
                self.assertEqual(forward["Shared Alias"].channel_key, "A.test")
            finally:
                connection.close()

    def test_mapping_hash_has_total_stream_id_order(self) -> None:
        input_rows = [
            _mapping_row(
                server_id="server_1",
                stream_id=stream_id,
                channel_name=f"Channel {stream_id}",
                category_name="Entertainment",
                epg_id=f"{index}.test",
            )
            for index, stream_id in enumerate(("1", "01", "a", "A"), start=1)
        ]
        forward = runner.parse_mapping_csv(_mapping_bytes(input_rows), {"server_1"})
        reverse = runner.parse_mapping_csv(
            _mapping_bytes(list(reversed(input_rows))), {"server_1"}
        )
        self.assertEqual(
            runner.canonical_mapping_sha256(forward),
            runner.canonical_mapping_sha256(reverse),
        )

    def test_replacement_schedule_cannot_keep_discarded_schedule_logo(self) -> None:
        rows = runner.parse_mapping_csv(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="1",
                        channel_name="B.test",
                        category_name="Entertainment",
                        epg_id="A.test",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="2",
                        channel_name="B.test",
                        category_name="Entertainment",
                        epg_id="B.test",
                    ),
                ]
            ),
            {"server_1"},
        )
        rows[0].canonical_name = "Discarded Schedule A"
        rows[1].canonical_name = "Kept Schedule B"
        with tempfile.TemporaryDirectory() as temporary:
            connection = runner.create_database(Path(temporary) / "spool.sqlite3")
            try:
                for epg_id, icon in (
                    ("A.test", "https://logos.example/wrong.png"),
                    ("B.test", ""),
                ):
                    connection.execute(
                        "INSERT INTO channels VALUES (?, ?, ?, ?, ?)",
                        ("epgshare01", epg_id, epg_id, epg_id, icon),
                    )
                    connection.execute(
                        "INSERT INTO programmes VALUES (?, ?, ?, ?, ?, '', '', '[]', ?)",
                        (
                            "epgshare01",
                            epg_id,
                            FIXED_NOW,
                            FIXED_NOW + 3600,
                            "Same quality",
                            10,
                        ),
                    )
                connection.commit()
                stats = runner.load_schedule_stats(connection, FIXED_NOW)
                entries, _, _ = runner.build_xml_entries(
                    rows=rows,
                    connection=connection,
                    schedule_stats=stats,
                    icon_overrides=[],
                )
                self.assertEqual(entries["B.test"].channel_key, "B.test")
                self.assertEqual(entries["B.test"].icon_url, "")
                self.assertIn("Kept Schedule B", entries["B.test"].display_names)
                self.assertNotIn(
                    "Discarded Schedule A", entries["B.test"].display_names
                )
            finally:
                connection.close()

    def test_exact_xmltv_ids_and_channel_aliases_preserve_double_spaces(self) -> None:
        exact_id = "US-MoreMax  Eastern"
        exact_alias = "USA: HBO  COMEDY"
        rows = runner.parse_mapping_csv(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="1",
                        channel_name=exact_alias,
                        category_name="Movies",
                        epg_id=exact_id,
                    )
                ]
            ),
            {"server_1"},
        )
        self.assertEqual(rows[0].epg_id, exact_id)
        self.assertEqual(rows[0].channel_name, exact_alias)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "source.xml.gz"
            destination = root / "guide.xml.gz"
            start = _xmltv_time(FIXED_NOW)
            stop = _xmltv_time(FIXED_NOW + 3600)
            _write_deterministic_gzip(
                source_path,
                (
                    f'<tv><channel id="{exact_id}"><display-name>MoreMax</display-name>'
                    f'</channel><programme channel="{exact_id}" start="{start}" '
                    f'stop="{stop}"><title>Exact ID Fixture</title></programme></tv>'
                ),
            )
            connection = runner.create_database(root / "spool.sqlite3")
            try:
                runner.ingest_xmltv_source(
                    connection=connection,
                    path=source_path,
                    source_key="epgshare01",
                    wanted_ids={exact_id},
                    window_start=FIXED_NOW - 1,
                    allow_source_icons=False,
                )
                stats = runner.load_schedule_stats(connection, FIXED_NOW - 1)
                entries, _, _ = runner.build_xml_entries(
                    rows=rows,
                    connection=connection,
                    schedule_stats=stats,
                    icon_overrides=[],
                )
                runner.write_tivimate_xmltv(
                    destination=destination,
                    entries=entries,
                    connection=connection,
                    window_start=FIXED_NOW - 1,
                )
            finally:
                connection.close()

            with gzip.open(destination, "rt", encoding="utf-8") as handle:
                output = handle.read()
            self.assertIn(f'<channel id="{exact_id}">', output)
            self.assertIn(f'<channel id="{exact_alias}">', output)
            self.assertIn(f'channel="{exact_id}"', output)
            runner.validate_xmltv(destination)

    def test_unsupported_top_level_xml_is_rejected_before_its_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "unsupported.xml"
            source_path.write_text(
                "<tv><extension><channel id='Wanted.test'/></extension></tv>",
                encoding="utf-8",
            )
            connection = runner.create_database(root / "spool.sqlite3")
            try:
                with self.assertRaisesRegex(
                    runner.BuildError, "unsupported top-level XMLTV element"
                ):
                    runner.ingest_xmltv_source(
                        connection=connection,
                        path=source_path,
                        source_key="epgshare01",
                        wanted_ids={"Wanted.test"},
                        window_start=FIXED_NOW,
                    )
            finally:
                connection.close()

    def test_long_prolog_dtd_and_corrupt_gzip_fail_as_build_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dtd_source = root / "long-prolog.xml"
            dtd_source.write_text(
                "<!--" + ("x" * 70_000) + "-->"
                "<!DOCTYPE tv [<!ENTITY marker 'EXPANDED'>]>"
                "<tv><channel id='&marker;'/></tv>",
                encoding="utf-8",
            )
            corrupt_gzip = root / "corrupt.xml.gz"
            corrupt_gzip.write_bytes(b"\x1f\x8bnot-a-valid-gzip-stream")
            connection = runner.create_database(root / "spool.sqlite3")
            try:
                with self.assertRaisesRegex(runner.BuildError, "forbidden DTD"):
                    runner.ingest_xmltv_source(
                        connection=connection,
                        path=dtd_source,
                        source_key="epgshare01",
                        wanted_ids={"EXPANDED"},
                        window_start=FIXED_NOW,
                    )
                with self.assertRaisesRegex(runner.BuildError, "malformed or truncated"):
                    runner.ingest_xmltv_source(
                        connection=connection,
                        path=corrupt_gzip,
                        source_key="epgshare01",
                        wanted_ids={"Fixture.test"},
                        window_start=FIXED_NOW,
                    )
            finally:
                connection.close()

    def test_duplicate_programme_keeps_richer_details_independent_of_order(self) -> None:
        start = _xmltv_time(FIXED_NOW)
        stop = _xmltv_time(FIXED_NOW + 3600)
        shallow = (
            f'<programme channel="Fixture.test" start="{start}" stop="{stop}">'
            "<title>Shared Title</title></programme>"
        )
        rich = (
            f'<programme channel="Fixture.test" start="{start}" stop="{stop}">'
            "<title>Shared Title</title><sub-title>Episode</sub-title>"
            "<desc>Richer description</desc><category>Cricket</category></programme>"
        )

        for order in ((shallow, rich), (rich, shallow)):
            with self.subTest(order="rich-last" if order[1] == rich else "rich-first"):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "source.xml"
                    source.write_text(
                        "<tv><channel id='Fixture.test'/>"
                        + "".join(order)
                        + "</tv>",
                        encoding="utf-8",
                    )
                    connection = runner.create_database(root / "spool.sqlite3")
                    try:
                        stats = runner.ingest_xmltv_source(
                            connection=connection,
                            path=source,
                            source_key="epgshare01",
                            wanted_ids={"Fixture.test"},
                            window_start=FIXED_NOW - 1,
                        )
                        retained = list(
                            runner.iter_schedule_rows(
                                connection,
                                "epgshare01",
                                "Fixture.test",
                                window_start=FIXED_NOW - 1,
                                window_end=None,
                            )
                        )
                    finally:
                        connection.close()
                    self.assertEqual(stats.duplicate_programmes, 1)
                    self.assertEqual(len(retained), 1)
                    self.assertEqual(retained[0][3], "Episode")
                    self.assertEqual(retained[0][4], "Richer description")
                    self.assertEqual(json.loads(retained[0][5]), ["Cricket"])

    def test_runnable_row_floor_fails_before_source_and_preserves_public(self) -> None:
        approved = _mapping_row(
            server_id="server_1",
            stream_id="1",
            channel_name="Approved Channel",
            category_name="Entertainment",
            epg_id="Approved.test",
        )
        review = _mapping_row(
            server_id="server_1",
            stream_id="2",
            channel_name="Review Channel",
            category_name="Entertainment",
            epg_id="Review.test",
        )
        review["action"] = "REVIEW"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "mapping.csv"
            authoritative = root / "authoritative.csv"
            manifest = root / "manifest.json"
            public = root / "public"
            public.mkdir()
            sentinel = public / "last-good.txt"
            sentinel.write_text("keep", encoding="utf-8")
            mapping_content = _mapping_bytes([approved, review])
            mapping.write_bytes(mapping_content)
            authoritative.write_bytes(mapping_content)
            validation = runner.validate_private_mapping_snapshots(
                mapping_content, mapping_content, {"server_1"}
            )
            manifest.write_text(
                json.dumps(runner.expected_mapping_snapshot_manifest(validation)),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                runner.BuildError, "runnable-row truncation guard"
            ):
                runner.main(
                    [
                        "--mapping-file",
                        str(mapping),
                        "--mapping-authoritative-file",
                        str(authoritative),
                        "--mapping-snapshot-manifest",
                        str(manifest),
                        "--all-source-file",
                        str(root / "must-not-be-read.xml.gz"),
                        "--public-dir",
                        str(public),
                        "--work-dir",
                        str(root / "work"),
                        "--servers",
                        "server_1",
                        "--minimum-server-rows",
                        "server_1=2",
                    ]
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_review_action_never_enters_public_metadata_when_enabled(self) -> None:
        approved = _mapping_row(
            server_id="server_1",
            stream_id="1",
            channel_name="Approved Channel",
            category_name="Entertainment",
            epg_id="Approved.test",
        )
        review = _mapping_row(
            server_id="server_1",
            stream_id="2",
            channel_name="Private Review Channel",
            category_name="Punjabi Religious",
            epg_id="Review.test",
        )
        review["enabled"] = "TRUE"
        review["action"] = "REVIEW"
        disabled_review = _mapping_row(
            server_id="server_1",
            stream_id="3",
            channel_name="Disabled Private Review Channel",
            category_name="Punjabi Religious",
            epg_id="Disabled.Review.test",
        )
        disabled_review["enabled"] = "FALSE"
        disabled_review["action"] = "REVIEW"
        rows = runner.parse_mapping_csv(
            _mapping_bytes([approved, review, disabled_review]), {"server_1"}
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = runner.create_database(root / "stage.db")
            try:
                result = runner.write_metadata_json(
                    destination=root / "metadata.json.gz",
                    rows=rows,
                    connection=connection,
                    generated_at=FIXED_NOW,
                    mapping_sha256="fixture",
                    icon_overrides=[],
                )
            finally:
                connection.close()
            payload = _read_gzip_json(root / "metadata.json.gz")

        self.assertEqual(result["metadataStreams"], 1)
        self.assertIn("1", payload["streamMetadata"])
        self.assertNotIn("2", payload["streamMetadata"])
        self.assertNotIn("3", payload["streamMetadata"])


class StreamingBuildIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.mapping_path = cls.root / "mapping.csv"
        cls.all_source_path = cls.root / "all-sources.xml.gz"
        cls.panel_source_path = cls.root / "server-2-panel.xml.gz"

        cls.mapping_path.write_bytes(
            _mapping_bytes(
                [
                    _mapping_row(
                        server_id="server_1",
                        stream_id="101",
                        channel_name="PTC Punjabi Gurbani",
                        category_name="Punjabi Religious",
                        epg_id="Gurbani.Punjabi.test",
                        source="epgshare01",
                        epg_feed="ALL_SOURCES1",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="102",
                        channel_name="Legacy Native Collision",
                        category_name="Sports",
                        epg_id="Collision.test",
                        source="panel",
                        epg_feed="server xmltv.php",
                    ),
                    _mapping_row(
                        server_id="server_1",
                        stream_id="103",
                        channel_name="Punjabi Movie Loop",
                        category_name="Punjabi 24/7 Movies",
                        epg_id="Movie.Dummy.us",
                        source="dummy",
                        epg_feed="DUMMY_CHANNELS",
                    ),
                    _mapping_row(
                        server_id="server_2",
                        stream_id="202",
                        channel_name="Willow Punjabi Cricket",
                        category_name="Punjabi Sports",
                        epg_id="Willow.Punjabi.test",
                        source="panel",
                        epg_feed="server xmltv.php",
                    ),
                    {
                        **_mapping_row(
                            server_id="server_1",
                            stream_id="review-enabled-private-stream-v1",
                            channel_name="Enabled Private Review Sentinel V1",
                            category_name="Private Review Category",
                            epg_id="Enabled.Private.Review.Sentinel.v1",
                        ),
                        "enabled": "TRUE",
                        "action": "REVIEW",
                    },
                    {
                        **_mapping_row(
                            server_id="server_1",
                            stream_id="review-disabled-private-stream-v1",
                            channel_name="Disabled Private Review Sentinel V1",
                            category_name="Private Review Category",
                            epg_id="Disabled.Private.Review.Sentinel.v1",
                            source="panel",
                            epg_feed="server xmltv.php",
                        ),
                        "enabled": "FALSE",
                        "action": "REVIEW",
                    },
                ]
            )
        )

        start = _xmltv_time(FIXED_NOW - 1_800)
        stop = _xmltv_time(FIXED_NOW + 3_600)
        all_source = f'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Gurbani.Punjabi.test">
    <display-name>EPGShare Gurbani</display-name>
    <icon src="https://logos.example/gurbani.png"/>
  </channel>
  <channel id="Willow.Punjabi.test">
    <display-name>EPGShare Cricket Decoy</display-name>
  </channel>
  <channel id="Collision.test">
    <display-name>Unrelated EPGShare Collision</display-name>
  </channel>
  <channel id="Movie.Dummy.us">
    <display-name>Movie Dummy</display-name>
  </channel>
  <programme channel="Gurbani.Punjabi.test" start="{start}" stop="{stop}">
    <title>EPGShare Sikh Programme</title>
    <category>Religion</category>
  </programme>
  <programme channel="Willow.Punjabi.test" start="{start}" stop="{stop}">
    <title>EPGShare Cricket Decoy Programme</title>
  </programme>
  <programme channel="Collision.test" start="{start}" stop="{stop}">
    <title>Unrelated EPGShare Collision Programme</title>
  </programme>
  <programme channel="Movie.Dummy.us" start="{start}" stop="{stop}">
    <title>Movie programming</title>
  </programme>
</tv>
'''
        panel_source = f'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Gurbani.Punjabi.test">
    <display-name>Native Server One Decoy</display-name>
  </channel>
  <channel id="Willow.Punjabi.test">
    <display-name>Native Willow</display-name>
    <icon src="https://logos.example/willow.png"/>
  </channel>
  <channel id="Collision.test">
    <display-name>Native Server One Collision</display-name>
  </channel>
  <programme channel="Gurbani.Punjabi.test" start="{start}" stop="{stop}">
    <title>Native Server One Decoy Programme</title>
  </programme>
  <programme channel="Willow.Punjabi.test" start="{start}" stop="{stop}">
    <title>Native Panel Cricket Programme</title>
    <category>Cricket</category>
  </programme>
  <programme channel="Collision.test" start="{start}" stop="{stop}">
    <title>Native Server One Collision Programme</title>
  </programme>
</tv>
'''
        _write_deterministic_gzip(cls.all_source_path, all_source)
        _write_deterministic_gzip(cls.panel_source_path, panel_source)

        cls.public_dirs: list[Path] = []
        for build_number in (1, 2):
            public_dir = cls.root / f"public-{build_number}"
            work_dir = cls.root / f"work-{build_number}"
            arguments = [
                "--mapping-file",
                str(cls.mapping_path),
                "--all-source-file",
                str(cls.all_source_path),
                "--panel-file",
                f"server_2={cls.panel_source_path}",
                "--public-dir",
                str(public_dir),
                "--work-dir",
                str(work_dir),
                "--icon-config",
                str(cls.root / "no-icon-overrides.csv"),
                "--servers",
                "server_1",
                "server_2",
                "--now-epoch",
                str(FIXED_NOW),
            ]
            # The legacy Server 1 row requests panel data. Supplying only
            # Server 2's fixture must never cause a Server 1 download attempt.
            with mock.patch.object(
                runner,
                "download_panel_xmltv",
                side_effect=AssertionError("unexpected native panel download"),
            ):
                result = runner.main(arguments)
            if result != 0:
                raise AssertionError(f"fixture build returned {result}")
            cls.public_dirs.append(public_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_server_source_selection_is_enforced_end_to_end(self) -> None:
        public = self.public_dirs[0]
        server_1_manifest = json.loads(
            (
                public
                / "reports/server_1/server_1_tivimate_manifest.json"
            ).read_text(encoding="utf-8")
        )
        server_2_manifest = json.loads(
            (
                public
                / "reports/server_2/server_2_tivimate_manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(server_1_manifest["sourcePolicy"], "EPGSHARE_ALL_ONLY")
        self.assertFalse(server_1_manifest["nativePanelXmltvUsed"])
        self.assertEqual(server_1_manifest["server1LegacyPanelRowsQuarantined"], 2)
        self.assertEqual(server_1_manifest["combinedSourceDummyGuideStreams"], 1)
        server_1_validation = json.loads(
            (
                public / "reports/server_1/server_1_validation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(server_1_validation["server1PolicyEnforced"])

        self.assertEqual(server_2_manifest["sourcePolicy"], "MAPPING_SELECTED")
        self.assertTrue(server_2_manifest["nativePanelXmltvUsed"])

        with gzip.open(
            public / "epg/server_1_tivimate.xml.gz", "rt", encoding="utf-8"
        ) as handle:
            server_1_xml = handle.read()
        with gzip.open(
            public / "epg/server_2_tivimate.xml.gz", "rt", encoding="utf-8"
        ) as handle:
            server_2_xml = handle.read()

        self.assertIn("EPGShare Sikh Programme", server_1_xml)
        self.assertNotIn("Native Server One Decoy Programme", server_1_xml)
        self.assertNotIn("Unrelated EPGShare Collision Programme", server_1_xml)
        self.assertNotIn("Native Server One Collision Programme", server_1_xml)
        self.assertIn("Movie programming", server_1_xml)
        self.assertIn("Native Panel Cricket Programme", server_2_xml)
        self.assertNotIn("EPGShare Cricket Decoy Programme", server_2_xml)
        self.assertNotIn("https://logos.example/willow.png", server_2_xml)

    def test_app_v1_programme_tuples_remain_compatible(self) -> None:
        public = self.public_dirs[0]
        server_1 = _read_gzip_json(public / "EPG/server_1_epg.json.gz")
        server_2 = _read_gzip_json(public / "EPG/server_2_epg.json.gz")

        required_top_level = {
            "schemaVersion",
            "serverId",
            "generatedAt",
            "windowStart",
            "windowEnd",
            "streamToEpg",
            "programmes",
        }
        for payload in (server_1, server_2):
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertTrue(required_top_level.issubset(payload))
            self.assertNotIn("streamMetadata", payload)
            for schedule in payload["programmes"].values():
                self.assertTrue(schedule)
                for programme in schedule:
                    self.assertIsInstance(programme, list)
                    self.assertEqual(len(programme), 3)
                    self.assertIs(type(programme[0]), int)
                    self.assertIs(type(programme[1]), int)
                    self.assertIs(type(programme[2]), str)
                    self.assertLess(programme[0], programme[1])

        self.assertEqual(
            server_1["streamToEpg"],
            {
                "101": "ALL_SOURCES1::Gurbani.Punjabi.test",
                "103": "DUMMY_CHANNELS::Movie.Dummy.us",
            },
        )
        self.assertEqual(
            server_2["streamToEpg"],
            {"202": "PANEL::Willow.Punjabi.test"},
        )
        self.assertEqual(
            server_1["programmes"]["ALL_SOURCES1::Gurbani.Punjabi.test"],
            [[FIXED_NOW - 1_800, FIXED_NOW + 3_600, "EPGShare Sikh Programme"]],
        )
        self.assertIn("DUMMY_CHANNELS::Movie.Dummy.us", server_1["programmes"])
        self.assertEqual(
            server_1["programmes"]["DUMMY_CHANNELS::Movie.Dummy.us"][0][2],
            "Movie programming",
        )
        self.assertEqual(
            server_2["programmes"]["PANEL::Willow.Punjabi.test"],
            [[FIXED_NOW - 1_800, FIXED_NOW + 3_600, "Native Panel Cricket Programme"]],
        )

    def test_personalization_metadata_preserves_punjabi_sikh_and_cricket_semantics(self) -> None:
        public = self.public_dirs[0]
        server_1 = _read_gzip_json(public / "EPG/server_1_metadata.json.gz")
        server_2 = _read_gzip_json(public / "EPG/server_2_metadata.json.gz")

        sikh = server_1["streamMetadata"]["101"]
        self.assertEqual(sikh["primaryLanguage"], "pa")
        self.assertEqual(sikh["languages"], ["pa"])
        self.assertEqual(sikh["genre"], "religion")
        self.assertEqual(sikh["religions"], ["sikhism"])
        self.assertEqual(sikh["sports"], [])
        self.assertTrue(sikh["personalizationEligible"])
        self.assertIn("languages", sikh["personalizationEligibleDimensions"])
        self.assertIn("genre", sikh["personalizationEligibleDimensions"])
        self.assertIn("religions", sikh["personalizationEligibleDimensions"])

        cricket = server_2["streamMetadata"]["202"]
        self.assertEqual(cricket["primaryLanguage"], "pa")
        self.assertEqual(cricket["languages"], ["pa"])
        self.assertEqual(cricket["genre"], "sports")
        self.assertEqual(cricket["sports"], ["cricket"])
        self.assertEqual(cricket["religions"], [])
        self.assertTrue(cricket["personalizationEligible"])
        self.assertIn("sports", cricket["personalizationEligibleDimensions"])
        self.assertEqual(cricket["logoUrl"], "")

        self.assertEqual(server_1["filterSemantics"]["dimensions"], "AND")
        self.assertEqual(server_2["filterSemantics"]["dimensions"], "AND")
        self.assertTrue(
            server_1["filterSemantics"]["requiredDimensionsMustBeEligible"]
        )

    def test_generated_gzip_artifacts_are_valid_and_byte_deterministic(self) -> None:
        first, second = self.public_dirs
        artifacts = (
            "epg/server_1_tivimate.xml.gz",
            "epg/server_2_tivimate.xml.gz",
            "EPG/server_1_epg.json.gz",
            "EPG/server_2_epg.json.gz",
            "EPG/server_1_metadata.json.gz",
            "EPG/server_2_metadata.json.gz",
        )
        for relative in artifacts:
            with self.subTest(artifact=relative):
                first_path = first / relative
                second_path = second / relative
                first_bytes = first_path.read_bytes()
                self.assertEqual(first_bytes[:2], b"\x1f\x8b")
                self.assertEqual(first_bytes[4:8], b"\x00\x00\x00\x00")
                self.assertEqual(first_bytes, second_path.read_bytes())
                with gzip.open(first_path, "rb") as handle:
                    expanded = handle.read()
                self.assertTrue(expanded)
                if relative.endswith(".xml.gz"):
                    root = ET.fromstring(expanded)
                    self.assertEqual(root.tag, "tv")
                    self.assertTrue(root.findall("channel"))
                    self.assertTrue(root.findall("programme"))
                else:
                    self.assertIsInstance(json.loads(expanded), dict)

        server_1_manifest = json.loads(
            (
                first / "reports/server_1/server_1_tivimate_manifest.json"
            ).read_text(encoding="utf-8")
        )
        server_1_xml = first / "epg/server_1_tivimate.xml.gz"
        self.assertEqual(
            server_1_manifest["dataSha256"],
            hashlib.sha256(server_1_xml.read_bytes()).hexdigest(),
        )

    def test_public_indexes_retain_schema_v1_compatibility_fields(self) -> None:
        public = self.public_dirs[0]
        tivimate = json.loads(
            (public / "epg/index.json").read_text(encoding="utf-8")
        )
        app = json.loads((public / "EPG/index.json").read_text(encoding="utf-8"))
        health = json.loads((public / "health.json").read_text(encoding="utf-8"))

        self.assertEqual(tivimate["schemaVersion"], 1)
        self.assertIn("generatedAtUtc", tivimate)
        self.assertEqual(tivimate["targetApp"], "TVMeta / TiviMate")
        self.assertEqual(
            tivimate["mappingSource"],
            "PRIVATE_GOOGLE_SHEET_EPHEMERAL_SNAPSHOT",
        )
        for build in tivimate["builds"]:
            for key in ("file", "sha256", "compressedBytes", "builderVersion"):
                self.assertIn(key, build)
            self.assertTrue(build["file"].startswith("epg/"))

        self.assertEqual(app["schemaVersion"], 1)
        self.assertEqual(
            app["mappingPolicy"],
            "APPROVED_MAPPING_ROWS_ONLY_NO_AUTOMATIC_REMATCH",
        )
        self.assertEqual(
            app["mappingSource"],
            "PRIVATE_GOOGLE_SHEET_EPHEMERAL_SNAPSHOT",
        )
        for build in app["builds"]:
            self.assertTrue(build["dataFile"].startswith("EPG/"))
            self.assertTrue(build["manifestFile"].startswith("EPG/"))
            self.assertTrue(build["metadataFile"].startswith("EPG/"))

        self.assertEqual(health["servers"], 2)
        self.assertEqual(health["serverCount"], 2)
        self.assertEqual(health["generatedAtUtc"], health["generatedAtIso"])

    def test_public_metadata_review_reports_never_publish_private_notes(self) -> None:
        public = self.public_dirs[0]
        for server_id in ("server_1", "server_2"):
            report = (
                public
                / "reports"
                / server_id
                / f"{server_id}_metadata_review.csv"
            )
            with report.open("r", encoding="utf-8", newline="") as handle:
                headers = next(csv.reader(handle))
            self.assertNotIn("notes", headers)

    def test_public_reports_exclude_enabled_and_disabled_review_rows(self) -> None:
        forbidden_values = (
            "review-enabled-private-stream-v1",
            "Enabled Private Review Sentinel V1",
            "Enabled.Private.Review.Sentinel.v1",
            "review-disabled-private-stream-v1",
            "Disabled Private Review Sentinel V1",
            "Disabled.Private.Review.Sentinel.v1",
        )
        for public in self.public_dirs:
            report_files = sorted(
                path
                for path in public.rglob("*")
                if path.is_file() and path.suffix in {".csv", ".json"}
            )
            self.assertTrue(report_files)
            for report in report_files:
                content = report.read_text(encoding="utf-8")
                for forbidden in forbidden_values:
                    with self.subTest(
                        public=public.name,
                        report=report.name,
                        forbidden=forbidden,
                    ):
                        self.assertNotIn(forbidden, content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
