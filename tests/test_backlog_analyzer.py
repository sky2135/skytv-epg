from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import analyze_review_backlog as analyzer  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402


def inventory(
    server_id: str,
    channels: list[dict[str, str]],
    *,
    category_name: str = "US News",
) -> sync.PanelInventory:
    normalized_channels: list[dict[str, str]] = []
    for channel in channels:
        normalized = {
            "stream_id": channel["stream_id"],
            "name": channel["name"],
            "category_id": channel.get("category_id", "news"),
            "epg_channel_id": channel.get("epg_channel_id", ""),
        }
        normalized_channels.append(normalized)
    return sync.PanelInventory(
        server_id=server_id,
        server_label=server_id.replace("_", " ").title(),
        categories=[
            {"category_id": "news", "category_name": category_name},
        ],
        channels=normalized_channels,
        source="offline-fixture",
    )


def channel(stream_id: str, name: str, *, epg_id: str = "") -> dict[str, str]:
    return {
        "stream_id": stream_id,
        "name": name,
        "category_id": "news",
        "epg_channel_id": epg_id,
    }


def review_item(
    server_id: str,
    stream_id: str,
    cluster: str,
    *,
    blocked: bool = False,
) -> analyzer.ReviewItem:
    private_name = f"PRIVATE-{server_id}-{stream_id}"
    return analyzer.ReviewItem(
        server_id=server_id,
        stream_id=stream_id,
        row=None,
        channel=channel(stream_id, private_name),
        cluster_key=(cluster,),
        safety_blocked=blocked,
    )


def xmltv_document(
    *,
    channel_ids: list[str],
    programmes: list[tuple[str, str, str, str]],
    doctype: str = "",
) -> bytes:
    # Production native feeds must contain at least 100 distinct channels.
    # Keep unit fixtures above that fixed truncation floor without weakening
    # the production validator for small requested candidate sets.
    padded_ids = list(channel_ids)
    for index in range(len(padded_ids), analyzer.MIN_NATIVE_CATALOG_IDS):
        padded_ids.append(f"Fixture.Padding.{index:03d}")
    channels = "".join(
        f'<channel id="{channel_id}"><display-name>{channel_id}</display-name></channel>'
        for channel_id in padded_ids
    )
    programme_xml = "".join(
        (
            f'<programme channel="{channel_id}" start="{start}" stop="{stop}">'
            f"<title>{title}</title></programme>"
        )
        for channel_id, start, stop, title in programmes
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{doctype}\n<tv>{channels}{programme_xml}</tv>"
    ).encode("utf-8")


class NativeProgrammeGateTests(unittest.TestCase):
    NOW = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    def validate(
        self,
        document: bytes,
        requested_ids: set[str],
    ) -> analyzer.NativeValidation:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "native.xml"
            source.write_bytes(document)
            return analyzer.validate_native_xmltv(
                source,
                server_id="server_2",
                requested_ids=requested_ids,
                now_epoch=self.NOW,
            )

    def test_native_id_requires_exact_case_and_rejects_casefold_collisions(self) -> None:
        programmes = [
            ("Exact.ID", "20260101000000 +0000", "20260101030000 +0000", "Morning News"),
            ("Exact.ID", "20260101030000 +0000", "20260101060000 +0000", "Afternoon News"),
            ("Case.ID", "20260101000000 +0000", "20260101030000 +0000", "Morning Show"),
            ("Case.ID", "20260101030000 +0000", "20260101060000 +0000", "Evening Show"),
        ]
        result = self.validate(
            xmltv_document(
                channel_ids=["Exact.ID", "Case.ID", "case.id"],
                programmes=programmes,
            ),
            {"Exact.ID", "exact.id", "Case.ID"},
        )
        self.assertEqual(result.requested_ids, 3)
        self.assertEqual(result.verified_ids, frozenset({"Exact.ID"}))

    def test_native_id_needs_two_informative_programmes_covering_six_hours(self) -> None:
        document = xmltv_document(
            channel_ids=["Good.ID", "One.ID", "Short.ID", "Placeholder.ID"],
            programmes=[
                ("Good.ID", "20260101000000 +0000", "20260101030000 +0000", "Morning News"),
                ("Good.ID", "20260101030000 +0000", "20260101060000 +0000", "Evening News"),
                ("One.ID", "20260101000000 +0000", "20260101070000 +0000", "One Long Show"),
                ("Short.ID", "20260101000000 +0000", "20260101030000 +0000", "First Show"),
                ("Short.ID", "20260101030000 +0000", "20260101055900 +0000", "Second Show"),
                ("Placeholder.ID", "20260101000000 +0000", "20260101030000 +0000", "No Information"),
                ("Placeholder.ID", "20260101030000 +0000", "20260101070000 +0000", "No Information"),
            ],
        )
        result = self.validate(
            document,
            {"Good.ID", "One.ID", "Short.ID", "Placeholder.ID"},
        )
        self.assertEqual(result.verified_ids, frozenset({"Good.ID"}))

    def test_inert_xmltv_doctype_is_allowed_but_entities_are_forbidden(self) -> None:
        valid = xmltv_document(
            channel_ids=["Safe.ID"],
            programmes=[
                ("Safe.ID", "20260101000000 +0000", "20260101030000 +0000", "First Show"),
                ("Safe.ID", "20260101030000 +0000", "20260101060000 +0000", "Second Show"),
            ],
            doctype='<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        )
        self.assertEqual(
            self.validate(valid, {"Safe.ID"}).verified_ids,
            frozenset({"Safe.ID"}),
        )

        unsafe = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE tv [<!ENTITY leak SYSTEM "file:///etc/passwd">]>\n'
            '<tv><channel id="Unsafe.ID"><display-name>&leak;</display-name>'
            "</channel></tv>"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            analyzer.BacklogAnalysisError,
            "security preflight",
        ):
            self.validate(unsafe, {"Unsafe.ID"})

    def test_server_one_native_validation_is_always_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "native.xml"
            source.write_bytes(xmltv_document(channel_ids=[], programmes=[]))
            with self.assertRaisesRegex(
                analyzer.BacklogAnalysisError,
                "Server 1 native EPG validation is forbidden",
            ):
                analyzer.validate_native_xmltv(
                    source,
                    server_id="server_1",
                    requested_ids={"Never.Native"},
                    now_epoch=self.NOW,
                )

    def test_native_completeness_floor_does_not_shrink_for_one_candidate(self) -> None:
        document = (
            '<?xml version="1.0"?><tv><channel id="Only.ID">'
            "<display-name>Only</display-name></channel>"
            '<programme channel="Only.ID" start="20260101000000 +0000" '
            'stop="20260101030000 +0000"><title>First</title></programme>'
            '<programme channel="Only.ID" start="20260101030000 +0000" '
            'stop="20260101060000 +0000"><title>Second</title></programme></tv>'
        ).encode("utf-8")
        with self.assertRaisesRegex(
            analyzer.BacklogAnalysisError,
            "completeness floor",
        ):
            self.validate(document, {"Only.ID"})


class ConservativeClusterTests(unittest.TestCase):
    def test_hd_sd_variants_group_only_within_one_server(self) -> None:
        inventories = [
            inventory(
                "server_1",
                [
                    channel("1", "US: CNN HD"),
                    channel("2", "US: CNN SD"),
                    channel("3", "US: CNN UHD"),
                    channel("4", "US: CNN +1"),
                    channel("5", "US: CNN East"),
                    channel("6", "US: CNN West"),
                    channel("7", "ES: CNN HD"),
                    channel("8", "US: BBC News HD"),
                ],
            ),
            inventory(
                "server_2",
                [
                    channel("11", "US | CNN FHD"),
                    channel("12", "US | BBC News FHD"),
                ],
            ),
        ]
        keys = analyzer.build_cluster_keys(inventories)

        base = keys[("server_1", "1")]
        self.assertEqual(base, keys[("server_1", "2")])
        self.assertNotEqual(base, keys[("server_2", "11")])
        self.assertNotEqual(keys[("server_1", "8")], keys[("server_2", "12")])
        for distinct in ("3", "4", "5", "6", "7"):
            with self.subTest(stream_id=distinct):
                self.assertNotEqual(base, keys[("server_1", distinct)])
        self.assertNotEqual(keys[("server_1", "5")], keys[("server_1", "6")])

    def test_numbered_single_token_and_non_latin_identities_are_server_scoped(self) -> None:
        names = [
            "NEWS 1",
            "MOVIES 1",
            "KIDS 1",
            "MUSIC 1",
            "CRICKET 1",
            "FOOTBALL 1",
            "PPV 1",
            "ENTERTAINMENT 1",
            "DOCUMENTARY 1",
            "SERIES 1",
            "RADIO 1",
            "CINEMA 1",
            "HOCKEY 1",
            "NBA 1",
            "NHL 1",
            "NFL 1",
            "MOVIES",
            "SPORTS",
            "ENTERTAINMENT",
            "DOCUMENTARY",
            "SERIES",
            "RADIO",
            "CINEMA",
            "CRICKET",
            "FOOTBALL",
            "SOCCER",
            "EVENT",
            "EVENTS",
            "ESPN",
            "凤凰卫视高清",
        ]
        inventories = [
            inventory(
                "server_1",
                [channel(str(index), name) for index, name in enumerate(names, 1)],
            ),
            inventory(
                "server_2",
                [channel(str(index + 100), name) for index, name in enumerate(names, 1)],
            ),
        ]
        keys = analyzer.build_cluster_keys(inventories)
        for index, name in enumerate(names, 1):
            with self.subTest(name=name):
                self.assertNotEqual(
                    keys[("server_1", str(index))],
                    keys[("server_2", str(index + 100))],
                )

        branded = analyzer.build_cluster_keys(
            [
                inventory("server_1", [channel("901", "US: BBC NEWS 1 HD")]),
                inventory("server_2", [channel("902", "US: BBC NEWS 1 SD")]),
            ]
        )
        self.assertNotEqual(
            branded[("server_1", "901")], branded[("server_2", "902")]
        )


class EligibilityAndNativeAdvisoryTests(unittest.TestCase):
    @staticmethod
    def mapping_table(rows: list[dict[str, str]]) -> sync.MappingTable:
        headers = sorted({key for row in rows for key in row})
        return sync.MappingTable(headers, headers, rows)

    def test_quarantined_row_still_requires_a_valid_mapping_action(self) -> None:
        row = {
            "server_id": "server_1",
            "stream_id": "1",
            "action": "BOGUS",
        }
        table = self.mapping_table([row])
        inventories = [inventory("server_1", [channel("1", "US: Example")])]
        with self.assertRaisesRegex(analyzer.BacklogAnalysisError, "invalid action"):
            analyzer.collect_review_items(
                table=table,
                inventories=inventories,
                cluster_keys={("server_1", "1"): ("example",)},
                quarantined_keys={("server_1", "1")},
            )

    def test_native_advisory_requires_exact_auto_discovered_disabled_review(self) -> None:
        provider = inventory(
            "server_2", [channel("2", "US: Example", epg_id="Exact.Native.ID")]
        )
        provider_channel = provider.channels[0]
        row = sync.new_mapping_row(
            provider,
            provider_channel,
            discovered_at="2026-09-16T12:34:56Z",
        )

        def candidate_for(
            candidate_row: dict[str, str],
            *,
            blocked: bool = False,
            changed: bool = False,
        ) -> frozenset[tuple[str, str]]:
            item = analyzer.ReviewItem(
                server_id="server_2",
                stream_id="2",
                row=candidate_row,
                channel=provider_channel,
                cluster_key=("server_2", "example"),
                safety_blocked=blocked,
            )
            selected = analyzer.select_native_advisory_candidates(
                review_items=[item],
                inventory_channels={item.key: provider_channel},
                changed_keys={item.key} if changed else (),
            )
            return selected["server_2"]

        self.assertEqual(candidate_for(row), frozenset({("server_2", "2")}))

        mutations = (
            ({"enabled": "TRUE"}, False, False),
            ({"action": "APPROVED"}, False, False),
            ({"source": "epgshare01", "epg_feed": "ALL_SOURCES1"}, False, False),
            ({"notes": "Manually entered"}, False, False),
            ({"epg_id": "exact.native.id"}, False, False),
            ({"epg_id": "Different.Native.ID"}, False, False),
            ({}, True, False),
            ({}, False, True),
        )
        for changes, blocked, changed in mutations:
            with self.subTest(changes=changes, blocked=blocked, changed=changed):
                mutated = dict(row)
                mutated.update(changes)
                self.assertFalse(
                    candidate_for(mutated, blocked=blocked, changed=changed)
                )

    def test_server_one_is_never_a_native_advisory_candidate(self) -> None:
        provider = inventory(
            "server_1", [channel("1", "US: Example", epg_id="Native.Forbidden")]
        )
        provider_channel = provider.channels[0]
        row = {
            "server_id": "server_1",
            "stream_id": "1",
            "action": "REVIEW",
            "enabled": "FALSE",
            "source": "panel",
            "epg_feed": "panel",
            "epg_id": "Native.Forbidden",
            "notes": (
                "Automatically discovered 2026-09-16T12:34:56Z; "
                "existing Sheet rows were not changed."
            ),
        }
        item = analyzer.ReviewItem(
            "server_1", "1", row, provider_channel, ("server_1", "example"), False
        )
        selected = analyzer.select_native_advisory_candidates(
            review_items=[item],
            inventory_channels={item.key: provider_channel},
            changed_keys=(),
        )
        self.assertFalse(selected["server_1"])

    def test_run_analysis_unions_native_advisory_and_matcher_eligibility(self) -> None:
        inventories = [
            inventory("server_1", [channel("1", "US: First")]),
            inventory(
                "server_2",
                [channel("2", "US: Second", epg_id="Exact.Native.ID")],
            ),
            inventory("server_3", [channel("3", "US: Third")]),
        ]
        rows = [
            sync.new_mapping_row(
                inventories[0],
                inventories[0].channels[0],
                discovered_at="2026-09-16T12:34:56Z",
            ),
            sync.new_mapping_row(
                inventories[1],
                inventories[1].channels[0],
                discovered_at="2026-09-16T12:34:56Z",
            ),
            {
                "server_id": "server_3",
                "stream_id": "3",
                "action": "APPROVED",
                "enabled": "TRUE",
            },
        ]
        table = self.mapping_table(rows)
        cluster_keys = {
            ("server_1", "1"): ("server_1", "first"),
            ("server_2", "2"): ("server_2", "second"),
            ("server_3", "3"): ("server_3", "third"),
        }

        def fake_download(server_id: str, destination: Path):
            destination.write_text("<tv/>", encoding="utf-8")
            return destination, {"server_id": server_id}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                output_dir=root / "public",
                private_work_dir=root / "private",
                now_utc="2026-09-16T12:34:56Z",
                allow_insecure_http=False,
                minimum_server_channels=[],
                all_source_file=root / "all.xml.gz",
                all_source_catalog_file=root / "all.txt",
            )
            with mock.patch.object(
                analyzer,
                "_load_live_inputs",
                return_value=(table, [], [], inventories),
            ), mock.patch.object(
                analyzer.sync,
                "compare_inventory",
                return_value=([], [], []),
            ), mock.patch.object(
                analyzer.sync,
                "inventory_overlap_issues",
                return_value=[],
            ), mock.patch.object(
                analyzer.sync,
                "open_alert_quarantine_keys",
                return_value=set(),
            ), mock.patch.object(
                analyzer.sync,
                "snapshot_quarantine_keys",
                return_value=set(),
            ), mock.patch.object(
                analyzer,
                "build_cluster_keys",
                return_value=cluster_keys,
            ), mock.patch.object(
                analyzer,
                "run_epgshare_analysis",
                return_value=(
                    frozenset({("server_1", "1")}),
                    frozenset(),
                    frozenset(),
                    {
                        "alignment_mode": "exact",
                        "confirmed_ids": 27_128,
                        "xml_only_ids": 0,
                        "text_only_ids": 0,
                    },
                ),
            ), mock.patch.object(
                analyzer.streaming,
                "download_panel_xmltv",
                side_effect=fake_download,
            ), mock.patch.object(
                analyzer,
                "validate_native_xmltv",
                return_value=analyzer.NativeValidation(
                    frozenset({"Exact.Native.ID"}), 1
                ),
            ), mock.patch.object(
                analyzer,
                "_assert_sheet_snapshot_unchanged",
            ):
                payload = analyzer.run_analysis(args)

        self.assertEqual(payload["totals"]["review_rows"], 2)
        self.assertEqual(payload["totals"]["analysis_eligible"], 2)
        self.assertEqual(payload["totals"]["matcher_eligible"], 1)
        self.assertEqual(payload["servers"]["server_2"]["native_candidates"], 1)
        self.assertEqual(payload["servers"]["server_2"]["native_verified"], 1)
        self.assertEqual(payload["servers"]["server_1"]["native_verified"], 0)

    def test_epgshare_placeholder_count_uses_only_current_corroborated_keys(self) -> None:
        stale = {
            "server_id": "server_1",
            "stream_id": "1",
            "action": "REVIEW",
            "enabled": "FALSE",
            "source": "dummy",
            "epg_feed": "DUMMY_CHANNELS",
            "epg_id": "Stale.Dummy.us",
            "notes": "old auto-map-v1 method=safety_rule",
        }
        table = self.mapping_table([stale])
        inv = [inventory("server_1", [channel("1", "US: Placeholder")])]
        outcome = SimpleNamespace(
            rows=(stale,),
            verified_placeholder_keys=frozenset(),
            catalog_corroboration_mode="exact",
            corroborated_catalog_channels=27_000,
            xml_only_catalog_channels=0,
            text_only_catalog_channels=0,
        )
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            analyzer.sync,
            "select_review_recheck_rows",
            side_effect=[([stale], {}), ([], {}), ([], {})],
        ), mock.patch.object(
            analyzer.automatch,
            "auto_match_and_spool",
            return_value=outcome,
        ):
            eligible, _strict, placeholders, _catalog = analyzer.run_epgshare_analysis(
                table=table,
                inventories=inv,
                changed_rows=(),
                quarantined_keys=frozenset(),
                all_source_file=Path(temporary) / "all.xml.gz",
                all_source_catalog_file=Path(temporary) / "all.txt",
                spool_out=Path(temporary) / "private.sqlite3",
                generated_at="2026-09-16T12:34:56Z",
            )
        self.assertEqual(eligible, frozenset({("server_1", "1")}))
        self.assertFalse(placeholders)

    def test_terminal_sheet_fingerprint_rejects_mapping_or_alert_changes(self) -> None:
        args = SimpleNamespace(sheet_id="x" * 20, sheet_tab="Mappings", alerts_tab="Sync Alerts")
        table = self.mapping_table(
            [{"server_id": "server_1", "stream_id": "1", "action": "REVIEW"}]
        )
        alerts = [{"server_id": "server_1", "stream_id": "1", "status": "OPEN"}]
        with mock.patch.object(
            analyzer,
            "_load_sheet_snapshot",
            return_value=(table, copy.deepcopy(alerts)),
        ):
            analyzer._assert_sheet_snapshot_unchanged(
                args, initial_table=table, initial_alerts=alerts
            )

        changed_table = self.mapping_table(
            [{"server_id": "server_1", "stream_id": "1", "action": "APPROVED"}]
        )
        with mock.patch.object(
            analyzer,
            "_load_sheet_snapshot",
            return_value=(changed_table, copy.deepcopy(alerts)),
        ), self.assertRaisesRegex(analyzer.BacklogAnalysisError, "changed during"):
            analyzer._assert_sheet_snapshot_unchanged(
                args, initial_table=table, initial_alerts=alerts
            )

        changed_alerts = copy.deepcopy(alerts)
        changed_alerts[0]["status"] = "RESOLVED"
        with mock.patch.object(
            analyzer,
            "_load_sheet_snapshot",
            return_value=(table, changed_alerts),
        ), self.assertRaisesRegex(analyzer.BacklogAnalysisError, "changed during"):
            analyzer._assert_sheet_snapshot_unchanged(
                args, initial_table=table, initial_alerts=alerts
            )

    def test_uncontrolled_exception_text_is_never_public(self) -> None:
        secret = "server_3 stream=PRIVATE-991 password=PRIVATE-PASSWORD"
        message = analyzer._public_error_message(sync.SyncError(secret))
        self.assertNotIn("PRIVATE", message)
        self.assertEqual(
            message,
            "Backlog analysis stopped safely before publishing a report.",
        )
        controlled = analyzer.BacklogAnalysisError("Fixed safe message.")
        self.assertEqual(analyzer._public_error_message(controlled), "Fixed safe message.")


class PublicSummaryTests(unittest.TestCase):
    GENERATED_AT = "2026-09-16T12:34:56Z"
    CATALOG = {
        "alignment_mode": "exact",
        "confirmed_ids": 27_128,
        "xml_only_ids": 0,
        "text_only_ids": 0,
    }

    def build_summary(self) -> dict[str, object]:
        inventories = [
            inventory(
                "server_1",
                [
                    channel("1", "PRIVATE-S1-A"),
                    channel("6", "PRIVATE-S1-F"),
                    channel("7", "PRIVATE-S1-G"),
                ],
            ),
            inventory(
                "server_2",
                [channel("2", "PRIVATE-S2-B"), channel("3", "PRIVATE-S2-C")],
            ),
            inventory(
                "server_3",
                [channel("4", "PRIVATE-S3-D"), channel("5", "PRIVATE-S3-E")],
            ),
        ]
        items = [
            review_item("server_1", "1", "cluster-a", blocked=True),
            review_item("server_2", "2", "cluster-b"),
            review_item("server_2", "3", "cluster-c"),
            review_item("server_3", "4", "cluster-d"),
            review_item("server_3", "5", "cluster-e"),
            review_item("server_1", "6", "cluster-f"),
            review_item("server_1", "7", "cluster-g"),
        ]
        matcher_eligible = frozenset(
            {items[2].key, items[3].key, items[4].key, items[6].key}
        )
        analysis_eligible = matcher_eligible | frozenset({items[1].key})
        return analyzer.build_public_summary(
            generated_at=self.GENERATED_AT,
            inventories=inventories,
            review_items=items,
            analysis_eligible=analysis_eligible,
            matcher_eligible=matcher_eligible,
            native_candidates={
                "server_1": frozenset(),
                "server_2": frozenset({("server_2", "2")}),
                "server_3": frozenset(),
            },
            native_verified={
                "server_1": frozenset(),
                "server_2": frozenset({("server_2", "2")}),
                "server_3": frozenset(),
            },
            native_status={"server_2": "available", "server_3": "not-checked"},
            strict_epgshare=frozenset({("server_2", "3")}),
            verified_placeholders=frozenset({("server_3", "4")}),
            catalog=self.CATALOG,
        )

    def test_lane_precedence_is_safety_native_epgshare_placeholder_unresolved(self) -> None:
        summary = self.build_summary()
        servers = summary["servers"]
        self.assertEqual(servers["server_1"]["safety_blocked"], 1)
        self.assertEqual(servers["server_2"]["native_verified"], 1)
        self.assertEqual(servers["server_2"]["strict_epgshare"], 1)
        self.assertEqual(servers["server_3"]["verified_placeholder"], 1)
        self.assertEqual(servers["server_1"]["not_eligible"], 1)
        self.assertEqual(servers["server_1"]["no_verified_candidate"], 1)
        self.assertEqual(servers["server_3"]["no_verified_candidate"], 1)
        self.assertEqual(summary["totals"]["analysis_eligible"], 5)
        self.assertEqual(summary["totals"]["matcher_eligible"], 4)
        self.assertEqual(summary["totals"]["review_rows"], 7)

    def test_public_summary_is_exactly_allowlisted_and_contains_no_private_values(self) -> None:
        summary = self.build_summary()
        self.assertEqual(set(summary), analyzer.TOP_LEVEL_PUBLIC_FIELDS)
        self.assertEqual(set(summary["catalog"]), analyzer.CATALOG_PUBLIC_FIELDS)
        self.assertEqual(set(summary["totals"]), analyzer.TOTAL_PUBLIC_FIELDS)
        for stats in summary["servers"].values():
            self.assertEqual(set(stats), analyzer.SERVER_PUBLIC_FIELDS)

        encoded = json.dumps(summary, sort_keys=True)
        for forbidden in (
            "PRIVATE-S1-A",
            "PRIVATE-S2-B",
            "stream_id",
            "channel_name",
            "epg_id",
            "username",
            "password",
            "base_url",
            "client_email",
            "private_key",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded.casefold())

    def test_validator_rejects_extra_fields_and_count_reconciliation_failures(self) -> None:
        summary = self.build_summary()

        leaked = copy.deepcopy(summary)
        leaked["servers"]["server_1"]["channel_name"] = "PRIVATE-S1-A"
        with self.assertRaisesRegex(
            analyzer.BacklogAnalysisError,
            "invalid schema",
        ):
            analyzer.validate_public_summary(leaked)

        wrong_total = copy.deepcopy(summary)
        wrong_total["totals"]["no_verified_candidate"] += 1
        with self.assertRaisesRegex(
            analyzer.BacklogAnalysisError,
            "do not reconcile",
        ):
            analyzer.validate_public_summary(wrong_total)

        wrong_lane = copy.deepcopy(summary)
        wrong_lane["servers"]["server_3"]["no_verified_candidate"] += 1
        with self.assertRaisesRegex(
            analyzer.BacklogAnalysisError,
            "lanes do not reconcile",
        ):
            analyzer.validate_public_summary(wrong_lane)

    def test_validator_rejects_impossible_native_catalog_and_cluster_states(self) -> None:
        base = self.build_summary()
        mutations: list[dict[str, object]] = []

        def mutated(path: tuple[str, ...], value: object) -> dict[str, object]:
            payload = copy.deepcopy(base)
            target = payload
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            return payload

        mutations.extend(
            [
                mutated(("servers", "server_2", "native_candidates"), 0),
                mutated(("servers", "server_2", "native_source_status"), "unavailable"),
                mutated(("catalog", "xml_only_ids"), 1),
                mutated(("servers", "server_1", "matcher_eligible"), 2),
                mutated(("servers", "server_3", "analysis_eligible"), 1),
                mutated(("totals", "review_clusters"), 6),
                mutated(("servers", "server_3", "provider_available"), False),
                mutated(("servers", "server_3", "provider_channels"), 0),
            ]
        )

        invented_analysis_eligibility = copy.deepcopy(base)
        invented_analysis_eligibility["servers"]["server_1"].update(
            {
                "analysis_eligible": 2,
                "not_eligible": 0,
                "no_verified_candidate": 2,
            }
        )
        invented_analysis_eligibility["totals"].update(
            {
                "analysis_eligible": 6,
                "not_eligible": 0,
                "no_verified_candidate": 3,
            }
        )
        mutations.append(invented_analysis_eligibility)

        missing_cluster_coverage = copy.deepcopy(base)
        missing_cluster_coverage["servers"]["server_1"]["review_clusters"] = 0
        missing_cluster_coverage["totals"]["review_clusters"] = 4
        mutations.append(missing_cluster_coverage)

        excessive_absolute_drift = copy.deepcopy(base)
        excessive_absolute_drift["catalog"].update(
            {"alignment_mode": "bounded-drift", "xml_only_ids": 65}
        )
        mutations.append(excessive_absolute_drift)

        excessive_fractional_drift = copy.deepcopy(base)
        excessive_fractional_drift["catalog"].update(
            {
                "alignment_mode": "bounded-drift",
                "confirmed_ids": 25_000,
                "xml_only_ids": 64,
            }
        )
        mutations.append(excessive_fractional_drift)

        oversized_source_catalog = copy.deepcopy(base)
        oversized_source_catalog["catalog"].update(
            {
                "alignment_mode": "bounded-drift",
                "confirmed_ids": analyzer.catalog_stream.MAX_CATALOG_CHANNEL_ELEMENTS,
                "xml_only_ids": 1,
            }
        )
        mutations.append(oversized_source_catalog)

        excessive_runtime_backlog = copy.deepcopy(base)
        excessive_runtime_backlog["servers"]["server_1"].update(
            {
                "provider_channels": analyzer.MAX_PUBLIC_SERVER_COUNT,
                "review_rows": analyzer.MAX_ANALYSIS_REVIEW_ROWS + 1,
                "review_clusters": analyzer.MAX_ANALYSIS_REVIEW_ROWS + 1,
                "safety_blocked": 0,
                "not_eligible": analyzer.MAX_ANALYSIS_REVIEW_ROWS + 1,
                "analysis_eligible": 0,
                "matcher_eligible": 0,
                "native_candidates": 0,
                "native_verified": 0,
                "strict_epgshare": 0,
                "verified_placeholder": 0,
                "no_verified_candidate": 0,
            }
        )
        mutations.append(excessive_runtime_backlog)

        for index, payload in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(analyzer.BacklogAnalysisError):
                    analyzer.validate_public_summary(payload)

    def test_summary_file_rejects_extra_symlink_and_oversize_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            summary = output / "summary.json"
            summary.write_text(json.dumps(self.build_summary()), encoding="utf-8")
            analyzer.validate_public_summary_file(summary)

            extra = output / "private.txt"
            extra.write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(analyzer.BacklogAnalysisError, "private files"):
                analyzer.validate_public_summary_file(summary)
            extra.unlink()

            target = output / "target.json"
            target.write_text("{}", encoding="utf-8")
            summary.unlink()
            summary.symlink_to(target)
            with self.assertRaisesRegex(analyzer.BacklogAnalysisError, "invalid"):
                analyzer.validate_public_summary_file(summary)
            summary.unlink()
            target.unlink()

            summary.write_bytes(b"x" * (analyzer.MAX_PUBLIC_SUMMARY_BYTES + 1))
            with self.assertRaisesRegex(analyzer.BacklogAnalysisError, "invalid"):
                analyzer.validate_public_summary_file(summary)


class BacklogAnalyzerWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = REPO_ROOT / ".github" / "workflows" / "backlog_analyzer.yml"
        self.workflow = self.path.read_text(encoding="utf-8")

    def test_workflow_is_manual_only_and_has_read_only_repository_permissions(self) -> None:
        trigger_section = self.workflow.split("on:", 1)[1].split("concurrency:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger_section)
        for forbidden in ("schedule:", "push:", "pull_request:", "workflow_run:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, trigger_section)
        permission_section = self.workflow.split("permissions:", 1)[1].split("env:", 1)[0]
        self.assertIn("contents: read", permission_section)
        self.assertNotIn("write", permission_section.casefold())

    def test_workflow_has_no_remote_write_or_ai_path(self) -> None:
        self.assertEqual(
            analyzer.GOOGLE_SHEETS_READONLY_SCOPE,
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        )
        for forbidden in (
            "--write-to-sheet",
            "batchUpdate",
            "contents: write",
            "pages: write",
            "actions: write",
            "id-token: write",
            "git push",
            "GEMINI_API_KEY",
            "ai_review_gemini.py",
            "--enable-ai",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.casefold(), self.workflow.casefold())

        analyze_step = self.workflow.split(
            "- name: Analyze the private REVIEW backlog without writing", 1
        )[1].split("- name: Validate the aggregate-only public summary", 1)[0]
        self.assertIn("GOOGLE_SHEETS_READONLY_TOKEN", analyze_step)
        self.assertNotIn("GOOGLE_SERVICE_ACCOUNT_JSON", analyze_step)
        self.assertIn("spreadsheets.readonly", self.workflow)
        self.assertRegex(
            self.workflow,
            r"scopes=\[\s*\"https://www\.googleapis\.com/auth/"
            r"spreadsheets\.readonly\"\s*\]",
        )

    def test_workflow_invokes_the_analyzer_with_private_and_public_paths(self) -> None:
        analyze_step = self.workflow.split(
            "- name: Analyze the private REVIEW backlog without writing", 1
        )[1].split("- name: Validate the aggregate-only public summary", 1)[0]
        self.assertIn("python -u scripts/analyze_review_backlog.py", analyze_step)
        for flag in (
            "--sheet-id",
            "--sheet-tab",
            "--output-dir",
            "--private-work-dir",
            "--all-source-file",
            "--all-source-catalog-file",
            "--minimum-server-channels",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, analyze_step)
        source = (REPO_ROOT / "scripts" / "analyze_review_backlog.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_assert_sheet_snapshot_unchanged(", source)

    def test_workflow_uploads_only_the_validated_aggregate_summary(self) -> None:
        upload_marker = "uses: actions/upload-artifact@"
        self.assertEqual(self.workflow.count(upload_marker), 1)
        upload_section = self.workflow.split(upload_marker, 1)[1]
        self.assertIn("path: .build/backlog-analysis/summary.json", upload_section)
        self.assertNotIn("path: .build/backlog-analysis\n", upload_section)
        self.assertNotIn("path: .build/backlog-analysis/\n", upload_section)
        self.assertIn("retention-days: 7", upload_section)
        self.assertIn(
            "files != [\"summary.json\"]",
            self.workflow,
        )

    def test_workflow_reports_only_current_server_local_analysis_fields(self) -> None:
        for required in (
            "analysis_eligible",
            "matcher_eligible",
            "native_candidates",
            "native_verified",
            "strict_epgshare",
            "verified_placeholder",
            "no_verified_candidate",
            "server-local",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.workflow)
        for removed in (
            "existing_mapping_reuse",
            "cross_server_clusters",
            "conflicting_clusters",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, self.workflow)


if __name__ == "__main__":
    unittest.main()
