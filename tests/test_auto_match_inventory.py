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
            self.assertTrue(spool.is_file())

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
            self.assertTrue(spool.is_file())

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

    def test_xml_and_text_catalog_mismatch_blocks_before_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "all.xml.gz"
            text_catalog = base / "all.txt"
            spool = base / "selected.sqlite3"
            write_gzip(source, xml_bytes(strong=True))
            text_catalog.write_bytes(text_catalog_bytes("Different.Channel.us2"))
            with self.assertRaisesRegex(AutoMatchError, "do not declare the same exact IDs"):
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
