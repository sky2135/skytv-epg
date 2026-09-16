from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
SRC_DIR = REPO_ROOT / "src"
for import_path in (SCRIPT_DIR, SRC_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import auto_match_inventory as integration  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
from skytv_epg_auto_match_v1 import (  # noqa: E402
    MatcherIdentity,
    MatcherPreflight,
    STRICT_ENGINE_SOURCE_SHA256,
    STRICT_MATCHER_BUILD_ID,
    STRICT_MATCHER_SOURCE_SHA256,
    STRICT_MATCHER_VERSION,
)


GENERATED_AT = "2026-09-15T12:00:00Z"


def _mapping_row(*, stream_id: str, name: str) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": name,
            "canonical_name": name,
            "category_id": "us",
            "category_name": "US | General",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "region_code": "north_america",
            "country_codes": "US",
        }
    )
    return row


def _inventory(*, stream_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        server_id="server_1",
        categories=[{"category_id": "us", "category_name": "US | General"}],
        channels=[
            {
                "stream_id": stream_id,
                "category_id": "us",
                "category_name": "US | General",
                "name": name,
                "epg_channel_id": "ignored.panel.id",
            }
        ],
    )


class _FakeEngine:
    def build_inventory_profiles_v8(self, channels, category_names):
        return ({"us": {}}, {str(channels[0]["stream_id"]): {}})


class _ReviewOnlyResolver:
    def __init__(self) -> None:
        self.engine = _FakeEngine()

    def resolve(self, row, **_kwargs):
        return (
            SimpleNamespace(
                route_explicit=True,
                explicit_market="US",
                route_plan=("US",),
            ),
            {
                "action": "REVIEW",
                "source": "epgshare",
                "epg_feed": "US2",
                "epg_id": "Good.Channel.us2",
                "match_method": "contextual_fuzzy",
                "reason": "suggestion only",
                "second_epg_id": "Good.Channel.Plus.us2",
            },
        )


def _runtime_factory(_candidates, _dummies):
    return integration.MatcherRuntime(
        resolver=_ReviewOnlyResolver(),
        identity=MatcherIdentity(
            version=STRICT_MATCHER_VERSION,
            build_id=STRICT_MATCHER_BUILD_ID,
            source_sha256=STRICT_MATCHER_SOURCE_SHA256,
            engine_source_sha256=STRICT_ENGINE_SOURCE_SHA256,
        ),
        preflight=MatcherPreflight(True, "fixture"),
        approved_aliases_sha256="a" * 64,
        schedule_equivalences_sha256="b" * 64,
    )


def _xml_bytes() -> bytes:
    channel_ids = (
        "Good.Channel.us2",
        "Good.Channel.Plus.us2",
        "Good.Channel.Extra.us2",
        "Good.Channel.ca2",
        "Adult.Programming.Dummy.us",
    )
    channels = "".join(
        f'<channel id="{epg_id}"><display-name>{epg_id}</display-name></channel>'
        for epg_id in channel_ids
    )
    programmes: list[str] = []
    for epg_id in ("Good.Channel.us2", "Good.Channel.Plus.us2"):
        programmes.extend(
            (
                f'<programme channel="{epg_id}" start="20260915130000 +0000" '
                'stop="20260915170000 +0000"><title>Programme One</title></programme>',
                f'<programme channel="{epg_id}" start="20260915170000 +0000" '
                'stop="20260915200000 +0000"><title>Programme Two</title></programme>',
            )
        )
    return (
        '<?xml version="1.0"?><tv>' + channels + "".join(programmes) + "</tv>"
    ).encode("utf-8")


def _text_catalog_bytes() -> bytes:
    return (
        "20260915120000\n"
        "-- epg_ripper_US2 --\n"
        "Good.Channel.us2\n"
        "Good.Channel.Plus.us2\n"
        "Good.Channel.Extra.us2\n"
        "-- epg_ripper_CA2 --\n"
        "Good.Channel.ca2\n"
        "-- epg_ripper_DUMMY_CHANNELS --\n"
        "Adult.Programming.Dummy.us\n"
    ).encode("utf-8")


class AutoMatchShortlistTests(unittest.TestCase):
    def test_existing_review_shortlist_is_same_snapshot_programme_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            spool = root / "selection.sqlite"
            with gzip.open(source, "wb") as handle:
                handle.write(_xml_bytes())
            catalog.write_bytes(_text_catalog_bytes())

            row = _mapping_row(stream_id="17", name="US: Good Channell HD")
            outcome = integration.auto_match_and_spool(
                mapping_rows=[row],
                inventories=[_inventory(stream_id="17", name=row["channel_name"])],
                new_rows=[],
                review_rows=[row],
                all_source_file=source,
                all_source_catalog_file=catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                enable_ai_review=True,
                minimum_unique_channels=5,
                runtime_factory=_runtime_factory,
            )

            self.assertEqual(outcome.recheck_considered_rows, 1)
            self.assertEqual(outcome.recheck_approved_rows, 0)
            self.assertEqual(len(outcome.ai_review_shortlists), 1)
            shortlist = outcome.ai_review_shortlists[0]
            self.assertEqual(shortlist.key, ("server_1", "17"))
            self.assertEqual(shortlist.market, "US")
            self.assertEqual(
                [candidate.candidate_key for candidate in shortlist.candidates],
                ["c01", "c02"],
            )
            # The similar Extra ID was selected during the same source pass but
            # dropped because it had no strong programme gate.  CA and dummy
            # IDs never entered this US real-candidate shortlist.
            self.assertEqual(
                {candidate.epg_id for candidate in shortlist.candidates},
                {"Good.Channel.us2", "Good.Channel.Plus.us2"},
            )
            self.assertTrue(
                all(
                    candidate.region == "US" and candidate.feed == "US2"
                    for candidate in shortlist.candidates
                )
            )
            self.assertEqual(
                outcome.summary_fields()["ai_review_shortlisted_candidates"], 2
            )
            self.assertEqual(outcome.ai_review_attempted_rows, 1)
            self.assertGreater(outcome.ai_review_comparisons, 0)
            self.assertTrue(spool.is_file())

    def test_disabled_ai_skips_shortlist_staging_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            spool = root / "selection.sqlite"
            with gzip.open(source, "wb") as handle:
                handle.write(_xml_bytes())
            catalog.write_bytes(_text_catalog_bytes())
            row = _mapping_row(stream_id="17", name="US: Good Channell HD")

            with mock.patch.object(
                integration,
                "_stage_ai_review_shortlists_with_metrics",
                side_effect=AssertionError("AI staging must remain disabled"),
            ) as stage:
                outcome = integration.auto_match_and_spool(
                    mapping_rows=[row],
                    inventories=[
                        _inventory(stream_id="17", name=row["channel_name"])
                    ],
                    new_rows=[],
                    review_rows=[row],
                    all_source_file=source,
                    all_source_catalog_file=catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    enable_ai_review=False,
                    minimum_unique_channels=5,
                    runtime_factory=_runtime_factory,
                )

            stage.assert_not_called()
            self.assertEqual(outcome.ai_review_shortlists, ())
            self.assertEqual(outcome.ai_review_attempted_rows, 0)
            self.assertEqual(outcome.ai_review_comparisons, 0)

    def test_new_rows_and_unsafe_contexts_never_receive_shortlists(self) -> None:
        candidate = {
            "epg_id": "Good.Channel.us2",
            "display_name": "Good Channel",
            "feed": "US2",
            "region": "US",
            "normalized": "good channel",
        }
        other = {
            **candidate,
            "epg_id": "Good.Channel.Plus.us2",
            "display_name": "Good Channel Plus",
            "normalized": "good channel plus",
        }
        proposal = SimpleNamespace(
            server_id="server_1",
            stream_id="1",
            channel_name="XXX Good Channel",
            category_name="US | Adults",
            eligible_for_finalization=False,
            route_explicit=True,
            explicit_market="US",
            route_plan=("US",),
        )
        self.assertEqual(
            integration._stage_ai_review_shortlists(
                proposals={("server_1", "1"): proposal},
                review_keys=(),
                corroborated_real_candidates=(candidate, other),
            ),
            (),
        )
        self.assertEqual(
            integration._stage_ai_review_shortlists(
                proposals={("server_1", "1"): proposal},
                review_keys=(("server_1", "1"),),
                corroborated_real_candidates=(candidate, other),
            ),
            (),
        )

    def test_duplicate_normalized_candidate_family_is_excluded(self) -> None:
        proposal = SimpleNamespace(
            server_id="server_2",
            stream_id="2",
            channel_name="US Good Channel HD",
            category_name="US | General",
            eligible_for_finalization=False,
            route_explicit=True,
            explicit_market="US",
            route_plan=("US",),
        )
        candidates = (
            {
                "epg_id": "Good.Channel.us",
                "display_name": "Good Channel",
                "feed": "US1",
                "region": "US",
                "normalized": "good channel",
            },
            {
                "epg_id": "Good.Channel.us2",
                "display_name": "Good Channel",
                "feed": "US2",
                "region": "US",
                "normalized": "good channel",
            },
            {
                "epg_id": "Good.Channel.Plus.us2",
                "display_name": "Good Channel Plus",
                "feed": "US2",
                "region": "US",
                "normalized": "good channel plus",
            },
        )
        self.assertEqual(
            integration._stage_ai_review_shortlists(
                proposals={("server_2", "2"): proposal},
                review_keys=(("server_2", "2"),),
                corroborated_real_candidates=candidates,
            ),
            (),
        )

    def test_shortlists_are_deterministically_bounded_to_fifty_by_eight(self) -> None:
        proposals = {
            ("server_3", str(index)): SimpleNamespace(
                server_id="server_3",
                stream_id=str(index),
                channel_name="US Good Channel HD",
                category_name="US | General",
                eligible_for_finalization=False,
                route_explicit=True,
                explicit_market="US",
                route_plan=("US",),
            )
            for index in range(60, 0, -1)
        }
        candidate_labels = (
            "Alpha",
            "Bravo",
            "Charlie",
            "Delta",
            "Echo",
            "Foxtrot",
            "Golf",
            "Hotel",
            "India",
        )
        candidates = tuple(
            {
                "epg_id": f"Good.Channel.{label}.us2",
                "display_name": f"Good Channel {label}",
                "feed": "US2",
                "region": "US",
                "normalized": f"good channel {label.casefold()}",
            }
            for label in candidate_labels
        )
        shortlists = integration._stage_ai_review_shortlists(
            proposals=proposals,
            review_keys=reversed(tuple(proposals)),
            corroborated_real_candidates=candidates,
        )
        self.assertEqual(len(shortlists), 50)
        self.assertEqual([item.stream_id for item in shortlists[:3]], ["1", "2", "3"])
        self.assertEqual(shortlists[-1].stream_id, "50")
        self.assertTrue(all(len(item.candidates) == 8 for item in shortlists))

    def test_queue_round_robins_servers_and_supports_bounded_rotation(self) -> None:
        proposals = {
            (server_id, str(index)): SimpleNamespace(
                server_id=server_id,
                stream_id=str(index),
                channel_name="US Good Channel HD",
                category_name="US | General",
                eligible_for_finalization=False,
                route_explicit=True,
                explicit_market="US",
                route_plan=("US",),
            )
            for server_id in ("server_1", "server_2", "server_3")
            for index in (1, 2)
        }
        candidates = (
            {
                "epg_id": "Good.Channel.us2",
                "display_name": "Good Channel",
                "feed": "US2",
                "region": "US",
                "normalized": "good channel",
            },
            {
                "epg_id": "Good.Channel.Plus.us2",
                "display_name": "Good Channel Plus",
                "feed": "US2",
                "region": "US",
                "normalized": "good channel plus",
            },
        )

        normal = integration._stage_ai_review_shortlists(
            proposals=proposals,
            review_keys=tuple(reversed(tuple(proposals))),
            corroborated_real_candidates=candidates,
        )
        rotated = integration._stage_ai_review_shortlists(
            proposals=proposals,
            review_keys=tuple(proposals),
            corroborated_real_candidates=candidates,
            rotation=1,
        )

        self.assertEqual(
            [item.key for item in normal],
            [
                ("server_1", "1"),
                ("server_2", "1"),
                ("server_3", "1"),
                ("server_1", "2"),
                ("server_2", "2"),
                ("server_3", "2"),
            ],
        )
        self.assertEqual(
            [item.key for item in rotated[:3]],
            [("server_2", "1"), ("server_3", "1"), ("server_1", "1")],
        )
        with self.assertRaises(integration.AutoMatchError):
            integration._stage_ai_review_shortlists(
                proposals=proposals,
                review_keys=tuple(proposals),
                corroborated_real_candidates=candidates,
                rotation=integration.MAX_AI_REVIEW_ROTATION + 1,
            )

    def test_attempted_rows_and_fuzzy_comparisons_are_independently_bounded(self) -> None:
        proposals = {
            ("server_1", str(index)): SimpleNamespace(
                server_id="server_1",
                stream_id=str(index),
                channel_name="US Good Channel HD",
                category_name="US | General",
                eligible_for_finalization=False,
                route_explicit=True,
                explicit_market="US",
                route_plan=("US",),
            )
            for index in range(1, 11)
        }
        candidates = tuple(
            {
                "epg_id": f"Good.Channel.{index}.us2",
                "display_name": f"Good Channel {index}",
                "feed": "US2",
                "region": "US",
                "normalized": f"good channel {index}",
            }
            for index in range(3)
        )

        with mock.patch.object(
            integration, "MAX_AI_REVIEW_ATTEMPTED_ROWS", 2
        ):
            row_bounded = integration._stage_ai_review_shortlists_with_metrics(
                proposals=proposals,
                review_keys=tuple(proposals),
                corroborated_real_candidates=candidates,
            )
        self.assertEqual(row_bounded.attempted_rows, 2)
        self.assertEqual(row_bounded.comparisons, 6)

        with mock.patch.object(
            integration, "MAX_AI_REVIEW_COMPARISONS", 4
        ):
            comparison_bounded = (
                integration._stage_ai_review_shortlists_with_metrics(
                    proposals=proposals,
                    review_keys=tuple(proposals),
                    corroborated_real_candidates=candidates,
                )
            )
        self.assertEqual(comparison_bounded.attempted_rows, 2)
        self.assertEqual(comparison_bounded.comparisons, 3)
        self.assertLessEqual(
            comparison_bounded.comparisons,
            4,
        )


if __name__ == "__main__":
    unittest.main()
