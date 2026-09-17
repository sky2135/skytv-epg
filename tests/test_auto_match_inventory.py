from __future__ import annotations

import gzip
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
SRC_DIR = REPO_ROOT / "src"
for import_path in (SCRIPT_DIR, SRC_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import build_epg_streaming as streaming  # noqa: E402
import auto_match_inventory as integration  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from auto_match_inventory import (  # noqa: E402
    AutoMatchError,
    MatcherRuntime,
    auto_match_and_spool,
    prepare_matcher_runtime,
)
from skytv_epg_auto_match_v1 import (  # noqa: E402
    MatcherIdentity,
    MatcherPreflight,
    STRICT_ENGINE_SOURCE_SHA256,
    STRICT_MATCHER_BUILD_ID,
    STRICT_MATCHER_SOURCE_SHA256,
    STRICT_MATCHER_VERSION,
)


GENERATED_AT = "2026-09-15T12:00:00Z"


def mapping_row(
    *,
    server_id: str,
    stream_id: str,
    channel_name: str,
    action: str = "REVIEW",
    source: str = "epgshare01",
    epg_id: str = "",
    enabled: str = "FALSE",
    category_id: str = "cat",
    category_name: str = "US | General",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": server_id,
            "server_label": server_id.replace("_", " ").title(),
            "stream_id": stream_id,
            "enabled": enabled,
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": category_id,
            "category_name": category_name,
            "action": action,
            "source": source,
            "epg_feed": "ALL_SOURCES1" if source == "epgshare01" else source,
            "epg_id": epg_id,
            "region_code": "north_america",
            "genre": "general",
            "primary_language": "en",
            "country_codes": "US",
            "language_codes": "en",
            "audience_codes": "general",
            "content_rating": "general",
            "channel_role": "linear",
            "metadata_status": "review",
            "metadata_source": "provider_category",
            "metadata_confidence": "0.8",
            "metadata_locked": "FALSE",
            "notes": "discovery note",
        }
    )
    return row


def inventory(
    *,
    server_id: str = "server_1",
    stream_id: str = "new-1",
    name: str = "US: Good Channel",
    panel_id: str = "secret.panel.id",
) -> SimpleNamespace:
    return SimpleNamespace(
        server_id=server_id,
        server_label=server_id.replace("_", " ").title(),
        source="player_api",
        categories=[{"category_id": "cat", "category_name": "US | General"}],
        channels=[
            {
                "stream_id": stream_id,
                "category_id": "cat",
                "category_name": "US | General",
                "name": name,
                "epg_channel_id": panel_id,
                "stream_icon": "https://panel.invalid/user/password/logo.png",
            }
        ],
    )


def xml_bytes(*, strong: bool = True, include_old: bool = False) -> bytes:
    channels = [
        '<channel id="Good.Channel.us2"><display-name>Untrusted display</display-name></channel>'
    ]
    if include_old:
        channels.append(
            '<channel id="Old.Channel.us2"><display-name>Old Channel</display-name></channel>'
        )
    programmes = [
        '<programme channel="Good.Channel.us2" start="20260915130000 +0000" '
        'stop="20260915170000 +0000"><title>Afternoon News</title></programme>'
    ]
    if strong:
        programmes.append(
            '<programme channel="Good.Channel.us2" start="20260915170000 +0000" '
            'stop="20260915200000 +0000"><title>Evening Report</title></programme>'
        )
    if include_old:
        programmes.extend(
            [
                '<programme channel="Old.Channel.us2" start="20260915130000 +0000" '
                'stop="20260915170000 +0000"><title>Old One</title></programme>',
                '<programme channel="Old.Channel.us2" start="20260915170000 +0000" '
                'stop="20260915200000 +0000"><title>Old Two</title></programme>',
            ]
        )
    return ("<?xml version=\"1.0\"?><tv>" + "".join(channels + programmes) + "</tv>").encode(
        "utf-8"
    )


def custom_xml_bytes(
    channel_ids: tuple[str, ...], *, programme_id: str
) -> bytes:
    channels = [
        f'<channel id="{epg_id}"><display-name>{epg_id}</display-name></channel>'
        for epg_id in channel_ids
    ]
    programmes = [
        f'<programme channel="{programme_id}" start="20260915130000 +0000" '
        'stop="20260915170000 +0000"><title>Programme One</title></programme>',
        f'<programme channel="{programme_id}" start="20260915170000 +0000" '
        'stop="20260915200000 +0000"><title>Programme Two</title></programme>',
    ]
    return (
        "<?xml version=\"1.0\"?><tv>"
        + "".join(channels + programmes)
        + "</tv>"
    ).encode("utf-8")


def write_gzip(path: Path, content: bytes) -> None:
    with gzip.open(path, "wb") as handle:
        handle.write(content)


def text_catalog_bytes(
    *real_ids: str, dummy_ids: tuple[str, ...] = ()
) -> bytes:
    lines = ["20260915120000"]
    if real_ids:
        lines.extend(("-- epg_ripper_US2 --", *real_ids))
    if dummy_ids:
        lines.extend(("-- epg_ripper_DUMMY_CHANNELS --", *dummy_ids))
    return ("\n".join(lines) + "\n").encode("utf-8")


class FakeEngine:
    def build_inventory_profiles_v8(self, channels, category_names):
        return (
            {str(item["category_id"]): {"count": len(channels)} for item in channels},
            {str(item["stream_id"]): {"role": "linear"} for item in channels},
        )


class FakeResolver:
    def __init__(self, target_id: str = "Good.Channel.us2") -> None:
        self.engine = FakeEngine()
        self.calls: list[dict[str, object]] = []
        self.target_id = target_id

    def resolve(self, row, **kwargs):
        self.calls.append({"row": dict(row), **kwargs})
        query = SimpleNamespace(
            route_explicit=True,
            explicit_market="US",
            route_plan=("US",),
        )
        return query, {
            "action": "AUTO_EPGSHARE",
            "source": "epgshare",
            "epg_feed": "US2",
            "epg_id": self.target_id,
            "match_method": "strict",
            "reason": "exact synthetic fixture",
            "second_epg_id": "",
        }


class RuntimeFactory:
    def __init__(self, target_id: str = "Good.Channel.us2") -> None:
        self.resolver = FakeResolver(target_id)
        self.candidates: list[dict[str, str]] = []
        self.dummies: dict[str, str] = {}

    def __call__(self, candidates, dummies):
        self.candidates = list(candidates)
        self.dummies = dict(dummies)
        return MatcherRuntime(
            resolver=self.resolver,
            identity=MatcherIdentity(
                version=STRICT_MATCHER_VERSION,
                build_id=STRICT_MATCHER_BUILD_ID,
                source_sha256=STRICT_MATCHER_SOURCE_SHA256,
                engine_source_sha256=STRICT_ENGINE_SOURCE_SHA256,
            ),
            preflight=MatcherPreflight(True, "fixture preflight"),
            approved_aliases_sha256="a" * 64,
            schedule_equivalences_sha256="b" * 64,
        )


class AutoMatchInventoryTests(unittest.TestCase):
    def run_fixture(
        self,
        temporary: str,
        *,
        strong: bool,
        old_rows=(),
        new_server: str = "server_1",
        include_old: bool = False,
    ):
        base = Path(temporary)
        source = base / "all.xml.gz"
        text_catalog = base / "all.txt"
        spool = base / "selected.sqlite3"
        write_gzip(source, xml_bytes(strong=strong, include_old=include_old))
        text_catalog.write_bytes(
            text_catalog_bytes(
                "Good.Channel.us2",
                *(() if not include_old else ("Old.Channel.us2",)),
            )
        )
        new = mapping_row(
            server_id=new_server,
            stream_id="new-1",
            channel_name="US: Good Channel",
        )
        factory = RuntimeFactory()
        outcome = auto_match_and_spool(
            mapping_rows=list(old_rows),
            inventories=[inventory(server_id=new_server)],
            new_rows=[new],
            all_source_file=source,
            all_source_catalog_file=text_catalog,
            spool_out=spool,
            generated_at=GENERATED_AT,
            minimum_unique_channels=1,
            runtime_factory=factory,
        )
        return outcome, factory, spool, new

    def test_exact_match_with_strong_guide_is_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outcome, _factory, spool, original = self.run_fixture(
                temporary, strong=True
            )
            row = outcome.rows[0]
            self.assertEqual(row["action"], "AUTO_EPGSHARE")
            self.assertEqual(row["source"], "epgshare01")
            self.assertEqual(row["epg_feed"], "ALL_SOURCES1")
            self.assertEqual(row["epg_id"], "Good.Channel.us2")
            self.assertEqual(row["enabled"], "TRUE")
            self.assertEqual(row["metadata_status"], original["metadata_status"])
            self.assertEqual(outcome.approved_rows, 1)
            self.assertEqual(outcome.review_rows, 0)
            self.assertEqual(outcome.catalog_channels, 1)
            self.assertEqual(outcome.text_catalog_channels, 1)
            self.assertEqual(outcome.corroborated_catalog_channels, 1)
            self.assertEqual(outcome.xml_only_catalog_channels, 0)
            self.assertEqual(outcome.text_only_catalog_channels, 0)
            self.assertEqual(outcome.catalog_drift_channels, 0)
            self.assertEqual(outcome.catalog_corroboration_mode, "exact")
            summary = outcome.summary_fields()
            self.assertEqual(summary["epgshare_corroborated_catalog_channels"], 1)
            self.assertEqual(summary["epgshare_shared_catalog_channels"], 1)
            self.assertEqual(summary["epgshare_xml_only_catalog_channels"], 0)
            self.assertEqual(summary["epgshare_text_only_catalog_channels"], 0)
            self.assertRegex(summary["epgshare_catalog_drift_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("verification_evidence", summary)
            evidence = outcome.verification_evidence
            self.assertEqual(evidence.source_sha256, outcome.source_sha256)
            self.assertEqual(
                evidence.text_catalog_file_sha256,
                outcome.text_catalog_file_sha256,
            )
            self.assertEqual(
                evidence.text_catalog_fingerprint_sha256,
                outcome.text_catalog_fingerprint_sha256,
            )
            self.assertEqual(
                evidence.text_catalog_generated_token,
                outcome.text_catalog_generated_token,
            )
            self.assertEqual(
                evidence.checked_at_epoch,
                integration._timestamp_epoch(GENERATED_AT),
            )
            self.assertEqual(
                evidence.xml_catalog_ids,
                frozenset({"Good.Channel.us2"}),
            )
            self.assertEqual(
                evidence.text_catalog_ids,
                frozenset({"Good.Channel.us2"}),
            )
            self.assertEqual(len(evidence.catalog_candidates), 1)
            candidate = evidence.catalog_candidates[0]
            self.assertEqual(candidate.epg_id, "Good.Channel.us2")
            self.assertEqual(candidate.market, "US")
            self.assertEqual(candidate.feed, "US2")
            self.assertTrue(candidate.is_real)
            self.assertEqual(
                candidate.semantics,
                integration.VerificationSemanticsEvidence(),
            )
            self.assertEqual(outcome.ai_review_shortlists, ())
            self.assertTrue(spool.is_file())

    def test_text_catalog_generation_must_be_current(self) -> None:
        now_epoch = integration._timestamp_epoch(GENERATED_AT)
        self.assertEqual(
            integration._validate_text_catalog_generation(
                "20260915120000", now_epoch=now_epoch
            ),
            now_epoch,
        )
        for token in ("20260912115959", "20260915180001", "2026091512000"):
            with self.subTest(token=token):
                with self.assertRaises(AutoMatchError):
                    integration._validate_text_catalog_generation(
                        token,
                        now_epoch=now_epoch,
                    )

    def test_new_and_existing_review_rows_are_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=True))
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))

            existing_review = mapping_row(
                server_id="server_1",
                stream_id="review-1",
                channel_name="US: Good Channel Existing",
            )
            new = mapping_row(
                server_id="server_1",
                stream_id="new-1",
                channel_name="US: Good Channel New",
            )
            current_inventory = inventory(
                server_id="server_1",
                stream_id="review-1",
                name="US: Good Channel Existing",
            )
            current_inventory.channels.append(
                {
                    **current_inventory.channels[0],
                    "stream_id": "new-1",
                    "name": "US: Good Channel New",
                }
            )

            outcome = auto_match_and_spool(
                mapping_rows=[existing_review],
                inventories=[current_inventory],
                new_rows=[new],
                review_rows=[existing_review],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=RuntimeFactory(),
            )

            self.assertEqual(outcome.considered_rows, 2)
            self.assertEqual(outcome.approved_rows, 2)
            self.assertEqual(outcome.new_considered_rows, 1)
            self.assertEqual(outcome.new_provisional_rows, 1)
            self.assertEqual(outcome.new_approved_rows, 1)
            self.assertEqual(outcome.new_review_rows, 0)
            self.assertEqual(outcome.recheck_considered_rows, 1)
            self.assertEqual(outcome.recheck_provisional_rows, 1)
            self.assertEqual(outcome.recheck_approved_rows, 1)
            self.assertEqual(outcome.recheck_review_rows, 0)
            summary = outcome.summary_fields()
            self.assertEqual(summary["auto_match_considered_rows"], 1)
            self.assertEqual(summary["auto_match_provisional_rows"], 1)
            self.assertEqual(summary["auto_matched_rows"], 1)
            self.assertEqual(summary["review_recheck_considered_rows"], 1)
            self.assertEqual(summary["review_recheck_provisional_rows"], 1)
            self.assertEqual(summary["review_recheck_safe_matches"], 1)
            self.assertTrue(spool.is_file())

    def test_mixed_gate_summary_keeps_new_and_recheck_failures_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=False))
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))
            existing_review = mapping_row(
                server_id="server_1",
                stream_id="review-1",
                channel_name="US: Good Channel Existing",
            )
            new = mapping_row(
                server_id="server_1",
                stream_id="new-1",
                channel_name="US: Good Channel New",
            )
            current_inventory = inventory(
                server_id="server_1",
                stream_id="review-1",
                name="US: Good Channel Existing",
            )
            current_inventory.channels.append(
                {
                    **current_inventory.channels[0],
                    "stream_id": "new-1",
                    "name": "US: Good Channel New",
                }
            )

            outcome = auto_match_and_spool(
                mapping_rows=[existing_review],
                inventories=[current_inventory],
                new_rows=[new],
                review_rows=[existing_review],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=RuntimeFactory(),
            )

            self.assertEqual(outcome.provisional_rows, 2)
            self.assertEqual(outcome.rejected_programme_gates, 2)
            self.assertEqual(outcome.new_rejected_programme_gates, 1)
            self.assertEqual(outcome.recheck_rejected_programme_gates, 1)
            summary = outcome.summary_fields()
            self.assertEqual(summary["auto_match_provisional_rows"], 1)
            self.assertEqual(summary["auto_match_rejected_programme_gates"], 1)
            self.assertEqual(summary["review_recheck_provisional_rows"], 1)
            self.assertEqual(
                summary["review_recheck_rejected_programme_gates"], 1
            )

    def test_weak_schedule_stays_disabled_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outcome, _factory, spool, _original = self.run_fixture(
                temporary, strong=False
            )
            row = outcome.rows[0]
            self.assertEqual(row["action"], "REVIEW")
            self.assertEqual(row["enabled"], "FALSE")
            self.assertEqual(row["epg_id"], "Good.Channel.us2")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertEqual(outcome.rejected_programme_gates, 1)
            gates = outcome.verification_evidence.programme_gates
            self.assertEqual(len(gates), 1)
            self.assertEqual(gates[0].channel_key, "Good.Channel.us2")
            self.assertEqual(gates[0].distinct_informative_programmes, 1)
            self.assertFalse(gates[0].passed)
            self.assertEqual(
                gates[0].checked_at_epoch,
                outcome.verification_evidence.checked_at_epoch,
            )
            self.assertEqual(gates[0].source_sha256, outcome.source_sha256)
            self.assertTrue(gates[0].reason)
            self.assertTrue(spool.is_file())

    def test_provisional_and_rejected_gate_summaries_count_rows_not_unique_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=False))
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))
            two_channel_inventory = inventory()
            two_channel_inventory.channels.append(
                {
                    **two_channel_inventory.channels[0],
                    "stream_id": "new-2",
                    "name": "US: Good Channel Backup",
                }
            )
            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[two_channel_inventory],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="US: Good Channel",
                    ),
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-2",
                        channel_name="US: Good Channel Backup",
                    ),
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=RuntimeFactory(),
            )
            self.assertEqual(outcome.considered_rows, 2)
            self.assertEqual(outcome.provisional_rows, 2)
            self.assertEqual(outcome.approved_rows, 0)
            self.assertEqual(outcome.review_rows, 2)
            self.assertEqual(outcome.rejected_programme_gates, 2)

    def test_official_country_section_conflict_can_never_auto_approve(self) -> None:
        target_id = "PTC.CHAK.DE.in"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes((target_id,), programme_id=target_id),
            )
            text_catalog.write_bytes(
                ("20260915120000\n-- epg_ripper_US2 --\n" + target_id + "\n").encode(
                    "utf-8"
                )
            )
            with self.assertRaisesRegex(
                AutoMatchError, "conflicting country evidence"
            ):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="IN: PTC Chak De")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="IN: PTC Chak De",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=RuntimeFactory(target_id),
                )
            self.assertFalse(spool.exists())

    def test_server_one_panel_identity_and_url_never_reach_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outcome, factory, _spool, _original = self.run_fixture(
                temporary, strong=True
            )
            call_row = factory.resolver.calls[0]["row"]
            self.assertEqual(call_row["panel_epg_id"], "")
            self.assertNotIn("secret.panel.id", repr(factory.resolver.calls))
            self.assertNotIn("panel.invalid", repr(factory.resolver.calls))
            self.assertEqual(outcome.rows[0]["source"], "epgshare01")

    def test_existing_rows_are_untouched_and_only_new_identity_is_matched(self) -> None:
        existing = mapping_row(
            server_id="server_1",
            stream_id="old-1",
            channel_name="Old Channel",
            action="APPROVED",
            epg_id="Old.Channel.us2",
            enabled="TRUE",
        )
        before = deepcopy(existing)
        with tempfile.TemporaryDirectory() as temporary:
            outcome, factory, _spool, _original = self.run_fixture(
                temporary,
                strong=True,
                old_rows=[existing],
                include_old=True,
            )
            self.assertEqual(existing, before)
            self.assertEqual(len(factory.resolver.calls), 1)
            self.assertEqual(factory.resolver.calls[0]["row"]["channel_name"], "US: Good Channel")
            self.assertEqual(outcome.fixed_requested_ids, 1)

    def test_no_new_rows_still_seals_existing_active_epg_id(self) -> None:
        existing = mapping_row(
            server_id="server_1",
            stream_id="old-1",
            channel_name="Old Channel",
            action="APPROVED",
            epg_id="Old.Channel.us2",
            enabled="TRUE",
        )
        inv = inventory(stream_id="old-1", name="Old Channel", panel_id="native")
        factory = RuntimeFactory()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=True, include_old=True))
            text_catalog.write_bytes(
                text_catalog_bytes("Good.Channel.us2", "Old.Channel.us2")
            )
            outcome = auto_match_and_spool(
                mapping_rows=[existing],
                inventories=[inv],
                new_rows=[],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=factory,
            )
            self.assertEqual(outcome.considered_rows, 0)
            self.assertEqual(outcome.rows, ())
            self.assertTrue(spool.is_file())
            with sqlite3.connect(spool) as connection:
                requests = connection.execute(
                    "SELECT channel_key, request_kind, source_channel_id "
                    "FROM epg_spool_requests"
                ).fetchall()
            self.assertEqual(requests, [("Old.Channel.us2", "fixed", "Old.Channel.us2")])

    def test_malformed_source_removes_stale_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            source.write_bytes(b"not-a-gzip")
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))
            spool.write_bytes(b"stale")
            with self.assertRaises(AutoMatchError):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory()],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: Good Channel",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=RuntimeFactory(),
                )
            self.assertFalse(spool.exists())

    def test_mutable_knowledge_file_is_rejected_before_loading_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            altered = Path(temporary) / "approved.csv"
            altered.write_text("alias,regions,epg_ids\nchanged,US,Changed.us2\n", encoding="utf-8")
            with mock.patch.object(integration, "APPROVED_ALIASES_PATH", altered), mock.patch.object(
                integration, "_load_frozen_engine"
            ) as load_engine:
                with self.assertRaisesRegex(AutoMatchError, "knowledge integrity"):
                    prepare_matcher_runtime([], {})
            load_engine.assert_not_called()

    def test_large_automatic_approval_batch_fails_before_spool_seal(self) -> None:
        many_channels = [
            {
                "stream_id": f"new-{index}",
                "category_id": "cat",
                "category_name": "US | General",
                "name": "US: Good Channel",
                "epg_channel_id": "native.panel.id",
                "stream_icon": "https://panel.invalid/private.png",
            }
            for index in range(100)
        ]
        inv = SimpleNamespace(
            server_id="server_1",
            categories=[{"category_id": "cat", "category_name": "US | General"}],
            channels=many_channels,
        )
        new_rows = [
            mapping_row(
                server_id="server_1",
                stream_id=f"new-{index}",
                channel_name="US: Good Channel",
            )
            for index in range(100)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=True))
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))
            with self.assertRaisesRegex(AutoMatchError, "approval batch exceeded"):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inv],
                    new_rows=new_rows,
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=RuntimeFactory(),
                )
            self.assertFalse(spool.exists())

    def test_catalogs_with_no_complete_exact_intersection_block_before_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=True))
            text_catalog.write_bytes(text_catalog_bytes("Different.Channel.us2"))
            with self.assertRaisesRegex(AutoMatchError, "intersection is below"):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory()],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: Good Channel",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=RuntimeFactory(),
                )
            self.assertFalse(spool.exists())

    def test_small_catalog_drift_approves_only_exactly_corroborated_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    ("Good.Channel.us2", "XML.Only.us2"),
                    programme_id="Good.Channel.us2",
                ),
            )
            text_catalog.write_bytes(
                text_catalog_bytes(
                    "Good.Channel.us2",
                    "Text.Only.us2",
                    dummy_ids=("Text.Only.DUMMY.us2",),
                )
            )
            factory = RuntimeFactory()
            # Production catalogs have >25k IDs, so a two-ID publication skew
            # is far below 1%. This tiny fixture raises only the fractional
            # ceiling while retaining the production absolute ceiling.
            with mock.patch.object(
                integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
            ):
                outcome = auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory()],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: Good Channel",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=factory,
                )

            candidate_ids = {item["epg_id"] for item in factory.candidates}
            self.assertEqual(
                candidate_ids,
                {"Good.Channel.us2", "Text.Only.us2", "XML.Only.us2"},
            )
            self.assertEqual(
                factory.dummies,
                {"text.only.dummy.us2": "Text.Only.DUMMY.us2"},
            )
            self.assertEqual(outcome.rows[0]["action"], "AUTO_EPGSHARE")
            self.assertEqual(outcome.rows[0]["enabled"], "TRUE")
            self.assertEqual(outcome.corroborated_catalog_channels, 1)
            self.assertEqual(outcome.xml_only_catalog_channels, 1)
            self.assertEqual(outcome.text_only_catalog_channels, 2)
            self.assertEqual(outcome.catalog_drift_channels, 3)
            self.assertEqual(outcome.catalog_corroboration_mode, "bounded-drift")
            self.assertTrue(spool.is_file())

    def test_xml_only_target_is_an_ambiguity_blocker_but_never_approved(self) -> None:
        target_id = "XML.Only.us2"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    ("Good.Channel.us2", target_id), programme_id=target_id
                ),
            )
            text_catalog.write_bytes(
                text_catalog_bytes("Good.Channel.us2", "Text.Only.us2")
            )
            factory = RuntimeFactory(target_id)
            with mock.patch.object(
                integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
            ):
                outcome = auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: XML Only")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: XML Only",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=factory,
                )

            self.assertIn(target_id, {item["epg_id"] for item in factory.candidates})
            self.assertEqual(outcome.rows[0]["epg_id"], target_id)
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertIn("lacks unambiguous real-catalog", outcome.rows[0]["reason"])
            self.assertEqual(outcome.approved_rows, 0)

    def test_case_only_catalog_drift_is_never_casefold_reconciled(self) -> None:
        xml_target = "Alkass_5_En.bein"
        text_target = "Alkass_5_EN.bein"
        result = None
        with mock.patch.object(
            integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
        ):
            result = integration._corroborate_catalog_ids(
                {"Good.Channel.us2", xml_target},
                {"Good.Channel.us2", text_target},
                minimum_unique_channels=1,
            )
        self.assertEqual(result.exact_ids, frozenset({"Good.Channel.us2"}))
        self.assertEqual(result.xml_only_ids, frozenset({xml_target}))
        self.assertEqual(result.text_only_ids, frozenset({text_target}))
        self.assertEqual(
            result.union_casefold_collision_keys,
            frozenset({xml_target.casefold()}),
        )

    def test_union_casefold_collision_cannot_auto_approve_either_spelling(self) -> None:
        xml_target = "Alkass_5_En.bein"
        text_target = "Alkass_5_EN.bein"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    ("Good.Channel.us2", xml_target), programme_id=xml_target
                ),
            )
            text_catalog.write_bytes(
                (
                    "20260915120000\n"
                    "-- epg_ripper_US2 --\n"
                    "Good.Channel.us2\n"
                    "-- epg_ripper_BEIN1 --\n"
                    f"{text_target}\n"
                ).encode("utf-8")
            )
            factory = RuntimeFactory(xml_target)
            with mock.patch.object(
                integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
            ):
                outcome = auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: Alkass")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: Alkass",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                    runtime_factory=factory,
                )

            self.assertIn(text_target, {item["epg_id"] for item in factory.candidates})
            self.assertNotIn(xml_target, {item["epg_id"] for item in factory.candidates})
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            evidence = outcome.verification_evidence
            self.assertIn(xml_target, evidence.xml_catalog_ids)
            self.assertNotIn(xml_target, evidence.text_catalog_ids)
            self.assertIn(text_target, evidence.text_catalog_ids)
            self.assertNotIn(text_target, evidence.xml_catalog_ids)
            self.assertEqual(xml_target.casefold(), text_target.casefold())
            self.assertNotIn(
                xml_target.casefold(),
                {candidate.epg_id.casefold() for candidate in evidence.catalog_candidates},
            )

    def test_xml_only_case_collision_family_cannot_create_false_uniqueness(
        self,
    ) -> None:
        common_id = "KABC-DT.us_locals1"
        xml_only_variants = ("KABC-TV.us_locals1", "kabc-TV.us_locals1")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (common_id, *xml_only_variants), programme_id=common_id
                ),
            )
            text_catalog.write_bytes(
                (
                    "20260915120000\n"
                    "-- epg_ripper_US_LOCALS1 --\n"
                    f"{common_id}\n"
                ).encode("utf-8")
            )
            with mock.patch.object(
                integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
            ):
                outcome = auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: KABC")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: KABC",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)

    def test_shared_non_ascii_whitespace_competitor_cannot_create_false_uniqueness(
        self,
    ) -> None:
        """The actual pinned matcher must never approve after dropping a twin.

        Its legacy text cleanup converts NBSP to ordinary whitespace.  The
        integration must retain a deterministic, non-approvable blocker in
        that exact engine-safe form and must not auto-approve the similar KABC
        candidate.
        """

        valid_id = "KABC-DT.us_locals1"
        unsafe_competitor = "KABC\u00a0TV.us_locals1"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (valid_id, unsafe_competitor), programme_id=valid_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                f"{valid_id}\n"
                f"{unsafe_competitor}\n",
                encoding="utf-8",
            )

            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory(name="US: KABC")],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="US: KABC",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertTrue(spool.is_file())

    def test_real_and_dummy_text_membership_fails_before_spool_actual_pinned(
        self,
    ) -> None:
        ambiguous_id = "KABC-TV.us_locals1"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes((ambiguous_id,), programme_id=ambiguous_id),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                f"{ambiguous_id}\n"
                "-- epg_ripper_DUMMY_CHANNELS --\n"
                f"{ambiguous_id}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AutoMatchError, "assigns an ID to both real and dummy sections"
            ):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: KABC")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: KABC",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                )
            self.assertFalse(spool.exists())

    def test_country_conflict_competitor_fails_globally_before_actual_matcher(
        self,
    ) -> None:
        valid_id = "KABC-DT.us_locals1"
        contradictory_competitor = "KABC-TV.in"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (valid_id, contradictory_competitor), programme_id=valid_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                f"{valid_id}\n"
                f"{contradictory_competitor}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AutoMatchError, "conflicting country evidence for an ID"
            ):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: KABC")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: KABC",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                )
            self.assertFalse(spool.exists())

    def test_cross_market_unicode_shadow_collision_fails_before_matcher(self) -> None:
        valid_id = "KABC-DT.us_locals1"
        india_competitor = "KABC\u00a0TV"
        usa_competitor = "KABC\u3000TV"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (valid_id, india_competitor, usa_competitor),
                    programme_id=valid_id,
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                f"{valid_id}\n"
                f"{usa_competitor}\n"
                "-- epg_ripper_IN1 --\n"
                f"{india_competitor}\n",
                encoding="utf-8",
            )
            spool.write_bytes(b"stale")

            with self.assertRaisesRegex(
                AutoMatchError,
                "normalization-confusable IDs in multiple feed/market routes",
            ):
                auto_match_and_spool(
                    mapping_rows=[],
                    inventories=[inventory(name="US: KABC")],
                    new_rows=[
                        mapping_row(
                            server_id="server_1",
                            stream_id="new-1",
                            channel_name="US: KABC",
                        )
                    ],
                    all_source_file=source,
                    all_source_catalog_file=text_catalog,
                    spool_out=spool,
                    generated_at=GENERATED_AT,
                    minimum_unique_channels=1,
                )
            self.assertFalse(spool.exists())

    def test_shared_all_route_station_competitor_blocks_false_uniqueness(self) -> None:
        target_id = "KABC-DT.us_locals1"
        all_route_competitor = "plex.tv.KABC-TV.plex"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (target_id, all_route_competitor), programme_id=target_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                f"{target_id}\n"
                "-- epg_ripper_PLEX1 --\n"
                f"{all_route_competitor}\n",
                encoding="utf-8",
            )

            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory(name="US: KABC")],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="US: KABC",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertIn("unscoped ALL-market", outcome.rows[0]["reason"])
            self.assertTrue(spool.is_file())

    def test_shared_all_route_competitor_also_vetoes_ai_shortlist(self) -> None:
        blocked_id = "KABC-DT.us_locals1"
        safe_id = "Other.Channel.us2"

        def shortlist(epg_id: str) -> integration.AiReviewShortlist:
            return integration.AiReviewShortlist(
                server_id="server_1",
                stream_id=epg_id,
                channel_name="US: KABC",
                category_name="US | General",
                market="US",
                candidates=(),
                smart_epg_id=epg_id,
            )

        staged = integration._AiReviewStaging(
            shortlists=(shortlist(blocked_id), shortlist(safe_id)),
            attempted_rows=2,
            comparisons=4,
        )
        filtered = integration._apply_ai_shortlist_catalog_ambiguity_veto(
            staged,
            all_route_labels={blocked_id: frozenset({"strict"})},
            same_market_ids=frozenset(),
        )

        self.assertEqual(
            tuple(item.smart_epg_id for item in filtered.shortlists),
            (safe_id,),
        )
        self.assertEqual(filtered.attempted_rows, staged.attempted_rows)
        self.assertEqual(filtered.comparisons, staged.comparisons)

    def test_shared_all_route_approved_identity_competitor_blocks_approval(self) -> None:
        target_id = "PTC.CHAK.DE.in"
        all_route_competitor = "PTC Chak De"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (target_id, all_route_competitor), programme_id=target_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_IN1 --\n"
                f"{target_id}\n"
                "-- epg_ripper_PLEX1 --\n"
                f"{all_route_competitor}\n",
                encoding="utf-8",
            )

            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory(name="IN: PTC Chak De")],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="IN: PTC Chak De",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertIn("unscoped ALL-market", outcome.rows[0]["reason"])

    def test_shared_all_route_strict_competitor_blocks_approval(self) -> None:
        target_id = "A.us2"
        all_route_competitor = "A"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (target_id, all_route_competitor), programme_id=target_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US2 --\n"
                f"{target_id}\n"
                "-- epg_ripper_PLEX1 --\n"
                f"{all_route_competitor}\n",
                encoding="utf-8",
            )

            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory(name="US: A")],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="US: A",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertIn("unscoped ALL-market", outcome.rows[0]["reason"])

    def test_same_market_coarse_station_family_blocks_structural_match(self) -> None:
        target_id = "History.ca2"
        same_market_competitor = "History.Television.HD.(Canada).ca2"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(
                source,
                custom_xml_bytes(
                    (target_id, same_market_competitor), programme_id=target_id
                ),
            )
            text_catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_CA2 --\n"
                f"{target_id}\n"
                f"{same_market_competitor}\n",
                encoding="utf-8",
            )

            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory(name="CA History Channel")],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="CA History Channel",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                enable_ai_review=True,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)
            self.assertIn("same-market EPG identity", outcome.rows[0]["reason"])
            self.assertEqual(outcome.ai_review_shortlists, ())

    def test_catalog_drift_fraction_and_absolute_boundaries_are_exact(self) -> None:
        ratio_boundary_common = {
            f"Ratio.Common.{index}.us2" for index in range(399)
        }
        accepted_ratio_boundary = integration._corroborate_catalog_ids(
            ratio_boundary_common.union({"Ratio.XML.Only.us2"}),
            ratio_boundary_common,
            minimum_unique_channels=1,
        )
        self.assertEqual(accepted_ratio_boundary.mode, "bounded-drift")
        ratio_over_common = set(list(ratio_boundary_common)[:398])
        with self.assertRaisesRegex(
            integration.catalog_stream.CatalogStreamError, "drift exceeds"
        ):
            integration._corroborate_catalog_ids(
                ratio_over_common.union({"Ratio.XML.Only.us2"}),
                ratio_over_common,
                minimum_unique_channels=1,
            )

        absolute_common = {
            f"Absolute.Common.{index}.us2" for index in range(26_000)
        }
        accepted_absolute_boundary = integration._corroborate_catalog_ids(
            absolute_common.union(
                {f"Absolute.XML.Only.{index}.us2" for index in range(64)}
            ),
            absolute_common,
            minimum_unique_channels=1,
        )
        self.assertEqual(accepted_absolute_boundary.mode, "bounded-drift")
        with self.assertRaisesRegex(
            integration.catalog_stream.CatalogStreamError, "drift exceeds"
        ):
            integration._corroborate_catalog_ids(
                absolute_common.union(
                    {f"Absolute.XML.Only.{index}.us2" for index in range(65)}
                ),
                absolute_common,
                minimum_unique_channels=1,
            )

    def test_bounded_drift_rejects_non_ascii_whitespace_or_control_ids(self) -> None:
        unsafe_ids = ("Café.us2", "Has Space.us2", "Has\tTab.us2")
        for unsafe_id in unsafe_ids:
            with self.subTest(unsafe_id=repr(unsafe_id)), mock.patch.object(
                integration, "MAX_CATALOG_ID_DRIFT_RATIO_DENOMINATOR", 1
            ), self.assertRaisesRegex(
                integration.catalog_stream.CatalogStreamError,
                "non-ASCII, whitespace, or control-character",
            ):
                integration._corroborate_catalog_ids(
                    {"Common.us2", unsafe_id},
                    {"Common.us2"},
                    minimum_unique_channels=1,
                )

    def test_catalog_inputs_and_intersection_each_keep_completeness_floor(self) -> None:
        with self.assertRaisesRegex(
            integration.catalog_stream.CatalogStreamError,
            "below its corroboration completeness floor",
        ):
            integration._corroborate_catalog_ids(
                {"Only.One.us2"},
                {"Only.One.us2", "Text.Two.us2"},
                minimum_unique_channels=2,
            )

        with self.assertRaisesRegex(
            integration.catalog_stream.CatalogStreamError,
            "intersection is below its completeness floor",
        ):
            integration._corroborate_catalog_ids(
                {"Common.us2", "XML.Only.us2"},
                {"Common.us2", "Text.Only.us2"},
                minimum_unique_channels=2,
            )

    def test_non_dummy_named_id_in_dummy_section_never_auto_enables(self) -> None:
        dummy_id = "Synthetic.Placeholder.us2"
        xml = (
            "<?xml version=\"1.0\"?><tv>"
            f'<channel id="{dummy_id}"><display-name>Placeholder</display-name></channel>'
            f'<programme channel="{dummy_id}" start="20260915130000 +0000" '
            'stop="20260915170000 +0000"><title>Event One</title></programme>'
            f'<programme channel="{dummy_id}" start="20260915170000 +0000" '
            'stop="20260915200000 +0000"><title>Event Two</title></programme>'
            "</tv>"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml)
            text_catalog.write_bytes(text_catalog_bytes(dummy_ids=(dummy_id,)))
            outcome = auto_match_and_spool(
                mapping_rows=[],
                inventories=[inventory()],
                new_rows=[
                    mapping_row(
                        server_id="server_1",
                        stream_id="new-1",
                        channel_name="US: Good Channel",
                    )
                ],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=RuntimeFactory(dummy_id),
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(outcome.approved_rows, 0)

    def test_unverified_dummy_review_proposal_is_not_exposed_as_safe(self) -> None:
        dummy_id = "Synthetic.Placeholder.us2"
        xml = (
            "<?xml version=\"1.0\"?><tv>"
            f'<channel id="{dummy_id}"><display-name>Placeholder</display-name></channel>'
            "</tv>"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml)
            text_catalog.write_bytes(text_catalog_bytes(dummy_ids=(dummy_id,)))
            factory = RuntimeFactory(dummy_id)

            def resolve(_row, **_kwargs):
                return SimpleNamespace(
                    route_explicit=True,
                    explicit_market="US",
                    route_plan=("US",),
                ), {
                    "action": "AUTO_DUMMY",
                    "source": "dummy",
                    "epg_feed": "DUMMY_CHANNELS",
                    "epg_id": dummy_id,
                    "match_method": "safety_rule",
                    "reason": "exact current dummy fixture",
                    "second_epg_id": "",
                }

            factory.resolver.resolve = resolve
            row = mapping_row(
                server_id="server_1",
                stream_id="review-1",
                channel_name="US: Placeholder",
            )
            outcome = auto_match_and_spool(
                mapping_rows=[row],
                inventories=[inventory(stream_id="review-1", name="US: Placeholder")],
                new_rows=(),
                review_rows=[row],
                all_source_file=source,
                all_source_catalog_file=text_catalog,
                spool_out=spool,
                generated_at=GENERATED_AT,
                minimum_unique_channels=1,
                runtime_factory=factory,
            )
            self.assertEqual(outcome.rows[0]["action"], "REVIEW")
            self.assertEqual(outcome.rows[0]["enabled"], "FALSE")
            self.assertEqual(
                outcome.verified_placeholder_keys,
                frozenset(),
            )
            self.assertNotIn("verified_placeholder", outcome.summary_fields())

    def test_source_failure_happens_before_any_google_write_or_snapshot(self) -> None:
        headers = list(streaming.SHEET_COLUMNS)
        empty_table = sync.MappingTable(headers, headers, [])
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            snapshot = base / "effective.csv"
            source.write_bytes(b"not-a-gzip")
            text_catalog.write_bytes(text_catalog_bytes("Good.Channel.us2"))
            spool.write_bytes(b"stale")
            with mock.patch.object(
                sync, "google_sheet_values", return_value=[headers]
            ), mock.patch.object(
                sync, "google_sync_alert_values", return_value=[list(sync.ALERT_COLUMNS)]
            ), mock.patch.object(
                sync, "append_sync_alert_rows"
            ) as write_alerts, mock.patch.object(
                sync, "append_google_sheet_rows"
            ) as write_mappings:
                with self.assertRaises(sync.SyncError):
                    sync.run_sync(
                        table=empty_table,
                        inventories=[inventory()],
                        output_dir=base / "reports",
                        generated_at=GENERATED_AT,
                        snapshot_out=snapshot,
                        write_to_sheet=True,
                        google_session=object(),
                        sheet_id="a" * 30,
                        all_source_file=source,
                        all_source_catalog_file=text_catalog,
                        epgshare_spool_out=spool,
                    )
            write_alerts.assert_not_called()
            write_mappings.assert_not_called()
            self.assertFalse(snapshot.exists())
            self.assertFalse(spool.exists())


if __name__ == "__main__":
    unittest.main()
