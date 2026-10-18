from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as streaming  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402


def valid_rows(count: int = 99) -> list[dict[str, object]]:
    return [
        {"stream_id": str(number), "name": f"Channel {number}", "category_id": "cat"}
        for number in range(1, count + 1)
    ]


def mapping_row(stream_id: str, name: str) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        server_id="server_1", server_label="Server 1", stream_id=stream_id,
        enabled="TRUE", channel_name=name, canonical_name=name, category_id="cat",
        category_name="General", action="APPROVED", source="epgshare01",
        epg_feed="ALL_SOURCES1", epg_id="Fixture.test", sort_priority="1000",
        metadata_status="approved", metadata_locked="TRUE",
    )
    return row


class Response:
    def __init__(self, payload: object):
        self.status_code = 200
        self.content = json.dumps(payload).encode("utf-8")
        self.headers: dict[str, str] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class InventoryQuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = sync.ServerConfig(
            "server_1", "Server 1", "https://panel.private.test", "reader", "safe-password"
        )
        self.policy = sync.provider_reflection_needles(self.config, [self.config.base_url])

    def recover(self, payload):
        return sync.normalize_blank_name_quarantine(payload, reflection_policy=self.policy)

    def inventory(self, *, bad_id: str = "1000", clean: bool = False):
        raw = valid_rows() + [{"stream_id": bad_id, "name": "Old Label" if clean else ""}]
        if clean:
            channels = sync.normalize_panel_action_rows(raw, "get_live_streams")
            ids = ()
        else:
            channels, ids = self.recover(raw)
        for channel in channels:
            channel["category_name"] = "General"
        return sync.PanelInventory(
            "server_1", "Server 1", [], channels, "player_api", quarantined_stream_ids=ids
        )

    def test_strict_normalizer_stays_strict_and_adapter_retains_only_presence(self):
        payload = valid_rows() + [{"stream_id": 1000, "name": "", "title": None}]
        original = copy.deepcopy(payload)
        with self.assertRaisesRegex(sync.SyncError, "ID or name but not both"):
            sync.normalize_panel_action_rows(payload, "get_live_streams")
        channels, ids = self.recover(payload)
        self.assertEqual(len(channels), 99)
        self.assertEqual(ids, ("1000",))
        self.assertEqual(payload, original)
        self.assertNotIn("1000", {channel["stream_id"] for channel in channels})

    def test_absent_or_noncanonical_ids_and_conflicting_aliases_fail_closed(self):
        bad_rows = [
            {"name": ""}, {"streamId": "1000", "name": ""},
            *({"stream_id": value, "name": ""} for value in (
                None, True, 1000.0, "001000", "1000 ", "1e3", "-1000", "0", "",
            )),
            {"stream_id": "1000", "id": "1001", "name": ""},
            {"stream_id": "1000", "streamId": " 1000", "name": ""},
        ]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(sync.SyncError):
                self.recover(valid_rows() + [row])

    def test_every_other_malformed_item_still_rejects_entire_inventory(self):
        for row in (None, "not-an-object", {}, {"unexpected": "metadata"}, {"name": "No ID"}):
            with self.subTest(row=row), self.assertRaises(sync.SyncError):
                self.recover(valid_rows(199) + [{"stream_id": "1000", "name": ""}, row])
        with self.assertRaises(sync.SyncError):
            self.recover({"streams": valid_rows() + [{"stream_id": "1000", "name": ""}]})

    def test_all_name_fields_must_be_blank_and_normal_aliases_remain_real_names(self):
        for field in sync._STREAM_NAME_FIELDS:
            payload = valid_rows() + [{"stream_id": "1000", "name": "", field: "Real Name"}]
            channels = sync.normalize_panel_action_rows(payload, "get_live_streams")
            self.assertEqual(channels[-1]["name"], "Real Name")
            with self.assertRaises(sync.SyncError):
                self.recover(payload)

    def test_duplicate_or_semantically_colliding_bad_ids_fail_closed(self):
        cases = [
            valid_rows(198) + [{"stream_id": "1000", "name": ""}] * 2,
            valid_rows() + [{"stream_id": "1", "name": ""}],
            valid_rows() + [{"stream_id": "1000", "name": ""}, {"stream_id": "01000", "name": "Collision"}],
            valid_rows() + [{"stream_id": "1000", "name": ""}, {"stream_id": "2000", "liveId": "1000", "name": "Collision"}],
        ]
        for payload in cases:
            with self.subTest(size=len(payload)), self.assertRaises(sync.SyncError):
                self.recover(payload)

    def test_quarantine_count_and_one_percent_limits_are_both_enforced(self):
        with self.assertRaises(sync.SyncError):
            self.recover(valid_rows(98) + [{"stream_id": "1000", "name": ""}])
        payload = valid_rows(990) + [
            {"stream_id": str(number), "name": ""} for number in range(1000, 1010)
        ]
        channels, ids = self.recover(payload)
        self.assertEqual((len(channels), len(ids)), (990, 10))
        with self.assertRaises(sync.SyncError):
            self.recover(valid_rows(1089) + [
                {"stream_id": str(number), "name": ""} for number in range(2000, 2011)
            ])

    def test_reflected_numeric_secret_in_quarantined_id_is_rejected(self):
        config = sync.ServerConfig("server_1", "Server 1", self.config.base_url, "reader", "1234")
        policy = sync.provider_reflection_needles(config, [config.base_url])
        with self.assertRaisesRegex(sync.SyncError, "persistence was blocked") as caught:
            sync.normalize_blank_name_quarantine(
                valid_rows() + [{"stream_id": "912345", "name": ""}], reflection_policy=policy
            )
        self.assertNotIn("912345", str(caught.exception))
        self.assertNotIn("1234", str(caught.exception))

    def test_fetch_inventory_recovers_without_m3u_or_fabricated_names(self):
        payload = valid_rows() + [{"stream_id": "1000", "name": ""}]
        responses: list[Response] = []

        def get(_url, **kwargs):
            action = kwargs["params"]["action"]
            response = Response(
                [{"category_id": "cat", "category_name": "General"}]
                if action == "get_live_categories" else payload
            )
            responses.append(response)
            return response

        with mock.patch.object(sync, "fetch_m3u_inventory") as fallback:
            result = sync.fetch_panel_inventory(
                SimpleNamespace(get=get), self.config, allow_insecure_http=False
            )
        fallback.assert_not_called()
        self.assertEqual(result.source, "player_api")
        self.assertEqual(result.quarantined_stream_ids, ("1000",))
        self.assertEqual(len(result.channels), 99)
        self.assertTrue(all(response.closed for response in responses))
        self.assertNotIn("1000", json.dumps(result.diagnostics))
        self.assertIn("1 identified blank-name rows", result.diagnostics[0])

    def test_quarantine_presence_prevents_false_missing_and_cannot_create_new_row(self):
        inventory = self.inventory()
        rows = [mapping_row(str(number), f"Channel {number}") for number in range(1, 100)]
        rows += [mapping_row("1000", "Old Label"), mapping_row("2000", "Truly Missing")]
        table = sync.MappingTable(list(streaming.SHEET_COLUMNS), list(streaming.SHEET_COLUMNS), rows)
        new, changed, missing = sync.compare_inventory(table, [inventory], discovered_at="2026-10-18T12:00:00Z")
        self.assertEqual((new, changed), ([], []))
        self.assertEqual([row["stream_id"] for row in missing], ["2000"])
        table.rows = rows[:-2]
        new, changed, missing = sync.compare_inventory(table, [inventory], discovered_at="2026-10-18T12:00:00Z")
        self.assertEqual((new, changed, missing), ([], [], []))
        self.assertEqual(sync.inventory_floor_failures([inventory], {"server_1": 100}, require_all=False), ["server_1=99 (minimum 100)"])

    def test_copy_and_revalidation_do_not_turn_presence_evidence_into_channels(self):
        inventory = self.inventory()
        enriched, _ = sync.enrich_review_inventories_from_m3u(
            object(), [inventory], [self.config], selected_servers=(), allow_insecure_http=False
        )
        self.assertEqual(enriched[0].quarantined_stream_ids, ("1000",))
        update = mapping_row("1000", "Old Label")
        # A row omitted from the proposal's valid channel list cannot be approved.
        verified, stats = sync.revalidate_provider_identity_updates(
            [update], [inventory], [self.config], allow_insecure_http=False
        )
        self.assertEqual(verified, [])
        self.assertEqual(stats["provider_batch_revalidation_rejected"], 1)

    def test_runtime_quarantine_is_temporary_and_sheet_snapshot_is_unchanged(self):
        for mode in ("off", "dry-run", "apply"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = [mapping_row(str(number), f"Channel {number}") for number in range(1, 100)]
                rows.append(mapping_row("1000", "Old Label"))
                original = copy.deepcopy(rows)
                table = sync.MappingTable(list(streaming.SHEET_COLUMNS), list(streaming.SHEET_COLUMNS), rows)
                expected = root / "expected.csv"
                sync.write_csv_report(expected, rows, streaming.SHEET_COLUMNS)
                outcome = SimpleNamespace(rows=(), learned_alias_registered=0,
                    learned_alias_support_rows=(), summary_fields=lambda: {})
                for clean in (False, True):
                    output = root / ("clean" if clean else "quarantined")
                    kwargs = {} if mode == "off" else dict(
                        review_recheck_mode=mode, all_source_file=root / "guide.xml",
                        all_source_catalog_file=root / "catalog.txt", epgshare_spool_out=root / "spool.db",
                    )
                    if mode == "apply":
                        kwargs.update(google_session=object(), sheet_id="a" * 30)
                    with mock.patch.object(sync.automatch, "auto_match_and_spool", return_value=outcome), \
                            mock.patch.object(sync, "google_sheet_values", return_value=[list(streaming.SHEET_COLUMNS)] + [[row[column] for column in streaming.SHEET_COLUMNS] for row in rows]), \
                            mock.patch.object(sync, "google_sync_alert_values", return_value=[list(sync.ALERT_COLUMNS)]), \
                            mock.patch.object(sync, "append_sync_alert_rows", return_value=0), \
                            mock.patch.object(sync, "append_google_sheet_rows") as append:
                        summary = sync.run_sync(
                            table=table, inventories=[self.inventory(clean=clean)], output_dir=output / "reports",
                            generated_at="2026-10-18T12:00:00Z", snapshot_out=output / "effective.csv",
                            authoritative_snapshot_out=output / "authoritative.csv", **kwargs,
                        )
                    append.assert_not_called()
                    self.assertEqual(rows, original)
                    self.assertEqual((output / "authoritative.csv").read_bytes(), expected.read_bytes())
                    effective = streaming.parse_mapping_csv((output / "effective.csv").read_bytes(), {"server_1"})
                    target = next(row for row in effective if row.stream_id == "1000")
                    self.assertEqual(target.runtime_eligible, clean)
                    self.assertEqual(summary["quarantined_provider_rows"], 0 if clean else 1)
                    self.assertEqual(summary["servers"]["server_1"]["quarantined_provider_rows"], 0 if clean else 1)
                    self.assertEqual(summary["missing_rows"], 0)
                    self.assertEqual(summary["open_sync_alerts"], 0)

    def test_unknown_bad_id_does_not_cause_snapshot_orphan_or_fabricated_new_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [mapping_row(str(number), f"Channel {number}") for number in range(1, 100)]
            table = sync.MappingTable(list(streaming.SHEET_COLUMNS), list(streaming.SHEET_COLUMNS), rows)
            summary = sync.run_sync(
                table=table, inventories=[self.inventory()], output_dir=root / "reports",
                generated_at="2026-10-18T12:00:00Z", snapshot_out=root / "effective.csv",
            )
            self.assertEqual((summary["new_rows"], summary["missing_rows"]), (0, 0))
            self.assertEqual(summary["quarantined_provider_rows"], 1)
            self.assertNotIn("1000", (root / "effective.csv").read_text(encoding="utf-8").splitlines()[0])
            effective = streaming.parse_mapping_csv((root / "effective.csv").read_bytes(), {"server_1"})
            self.assertNotIn("1000", {row.stream_id for row in effective})

    def test_late_human_added_bad_id_is_preserved_but_quarantined_on_every_final_read(self):
        for recover_after_error in (False, True):
            with self.subTest(recover_after_error=recover_after_error), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                initial_rows = [mapping_row(str(number), f"Channel {number}") for number in range(1, 100)]
                late_row = mapping_row("1000", "Human Added Label")
                final_rows = initial_rows + [late_row]
                table = sync.MappingTable(list(streaming.SHEET_COLUMNS), list(streaming.SHEET_COLUMNS), initial_rows)

                def sheet_values(rows):
                    return [list(streaming.SHEET_COLUMNS)] + [
                        [row[column] for column in streaming.SHEET_COLUMNS] for row in rows
                    ]

                with mock.patch.object(sync, "google_sheet_values", side_effect=[sheet_values(initial_rows), sheet_values(final_rows)]), \
                        mock.patch.object(sync, "google_sync_alert_values", return_value=[list(sync.ALERT_COLUMNS)]), \
                        mock.patch.object(sync, "append_sync_alert_rows", return_value=0), \
                        mock.patch.object(sync, "append_google_sheet_rows", side_effect=sync.SheetWriteError("fixed write failure") if recover_after_error else None, return_value=0):
                    kwargs = dict(
                        table=table, inventories=[self.inventory()], output_dir=root / "reports",
                        generated_at="2026-10-18T12:00:00Z", snapshot_out=root / "effective.csv",
                        write_to_sheet=True, google_session=object(), sheet_id="a" * 30,
                    )
                    if recover_after_error:
                        with self.assertRaisesRegex(sync.SheetWriteError, "fixed write failure"):
                            sync.run_sync(**kwargs)
                    else:
                        sync.run_sync(**kwargs)
                expected = root / "expected.csv"
                sync.write_csv_report(expected, final_rows, streaming.SHEET_COLUMNS)
                self.assertEqual((root / "authoritative_mapping.csv").read_bytes(), expected.read_bytes())
                effective = streaming.parse_mapping_csv((root / "effective.csv").read_bytes(), {"server_1"})
                target = next(row for row in effective if row.stream_id == "1000")
                self.assertFalse(target.runtime_eligible)
                self.assertEqual(late_row, final_rows[-1])
                summary = json.loads((root / "reports" / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["snapshot_integrity_servers"]["server_1"]["quarantined_rows"], 1)

    def test_legacy_reserved_reason_cannot_be_copied_into_authoritative_mapping(self):
        row = mapping_row("1", "Channel 1")
        row["reason"] = streaming.LEGACY_EFFECTIVE_QUARANTINE_REASON
        table = sync.MappingTable(list(streaming.SHEET_COLUMNS), list(streaming.SHEET_COLUMNS), [row])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "reserved effective-snapshot"):
                sync.write_mapping_snapshot(root / "effective.csv", table)
            with self.assertRaisesRegex(sync.SyncError, "reserved effective-snapshot"):
                sync.write_private_mapping_snapshot_bundle(
                    effective_path=root / "effective.csv", authoritative_path=root / "authoritative.csv",
                    manifest_path=root / "manifest.json", table=table,
                )


if __name__ == "__main__":
    unittest.main()
