from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_cricket_mapping_gates as diagnostic
import sync_channel_inventory as inventory


def row(name, **changes):
    result = {field: "" for field in inventory.streaming.SHEET_COLUMNS}
    result.update(server_id="server_1", stream_id="private-stream-id", channel_name=name, category_name="SPORTS | CRICKET", action="APPROVED", enabled="TRUE", source="epgshare01", epg_feed="IN1", epg_id="Private.Mapping.in", country_codes="IN", primary_language="en", language_codes="en")
    result.update(changes)
    return result


def table(*rows):
    return inventory.MappingTable([], [], list(rows))


class DiagnoseCricketMappingGatesTests(unittest.TestCase):
    def test_predefined_groups_cover_only_the_requested_families(self):
        examples = {
            "CRIC | Sony Sports 1 HD": "Sony1", "CRI | Sony Sports 2 FHD": "Sony2",
            "CRIC: Sony Sports 3 Hindi": "Sony3", "CRIC | Sony Sports 4 (Tamil) HD": "Sony4Tamil",
            "CRIC | Sony Sports 4 Telugu": "Sony4Telugu", "Sony Ten 5 HD": "Sony5",
            "CRIC: Sony ESPN HD": "SonyESPN", "Star Sports First": "StarFirst",
            "SG: Hub Sports 4 HD": "Hub4", "CRIC: Super Sports HD": "SuperSportsGeneric",
            "CRI | SuperSportsHD": "SuperSportsGeneric", "Sky Sport 1 FHD": "SkySport1Generic",
        }
        for name, group in examples.items():
            with self.subTest(name=name):
                self.assertEqual(diagnostic.group_for_row(row(name)), group)
        for name in ["Sony Sports 11 HD", "Sony Sports 4 HD", "Super Sports 1 HD", "Sky Sport 11 FHD", "Sony Sports 1 Spanish"]:
            self.assertIsNone(diagnostic.group_for_row(row(name)))

    def test_scope_is_server1_cricket_or_prefix_with_exact_hub_exception(self):
        report = diagnostic.summarize(table(
            row("Sony Sports 1", server_id="server_3"),
            row("Sony Sports 1", category_name="INDIA | TV"),
            row("CRIC | Sony Sports 1", category_name="OTHER"),
            row("SG: Hub Sports 4 HD", category_name="SINGAPORE"),
        ))
        self.assertEqual(report["selectedRows"], 2)
        groups = {item["group"]: item for item in report["groups"]}
        self.assertEqual(groups["Sony1"]["rowCount"], 1)
        self.assertEqual(groups["Hub4"]["rowCount"], 1)

    def test_gate_counts_and_note_enums_do_not_expose_private_values(self):
        report = diagnostic.summarize(table(
            row("CRIC | Sony Sports 1", source="panel", epg_feed="PANEL", metadata_locked="TRUE", notes="manual review private-note-8675309 https://private.invalid/provider"),
            row("Sony Sports 2", enabled="FALSE", action="REVIEW"),
            row("Sony Sports 3", enabled="FALSE", action="IGNORE"),
            row("Sony Sports 4 Tamil", enabled="FALSE", action="MANUAL"),
            row("Sony Sports 4 Telugu"),
            row("Sony Sports 5", notes="do not remap"),
            row("Super Sports HD"), row("CRIC | SuperSports HD"),
        ))
        groups = {item["group"]: item for item in report["groups"]}
        self.assertEqual(groups["Sony1"]["sourcePolicyBlockedRows"], 1)
        self.assertEqual(groups["Sony1"]["protectedNoteRows"], 1)
        self.assertEqual(groups["Sony1"]["protectedTermCounts"]["manual_review"], 1)
        self.assertEqual(groups["Sony1"]["metadataLockedRows"], 1)
        self.assertEqual(groups["Sony1"]["reviewedRuleIdentityRows"], 1)
        self.assertEqual(groups["Sony1"]["reviewedRuleAllowedRows"], 0)
        self.assertEqual(groups["Sony2"]["reviewRows"], 1)
        self.assertEqual(groups["Sony3"]["explicitIgnoreRows"], 1)
        self.assertEqual(groups["Sony4Tamil"]["manualDisabledRows"], 1)
        self.assertEqual(groups["Sony4Tamil"]["manualDecisionRows"], 1)
        self.assertEqual(groups["Sony4Telugu"]["eligibleRows"], 1)
        self.assertEqual(groups["Sony5"]["protectedTermCounts"]["do_not_remap"], 1)
        self.assertEqual(groups["SuperSportsGeneric"]["rowCount"], 2)
        self.assertEqual(groups["Sony1"]["countryCodes"], ["IN"])
        self.assertEqual(groups["Sony1"]["languageCodes"], ["en"])
        rendered = json.dumps(report)
        for private in ["private-stream-id", "private-note-8675309", "private.invalid", "Private.Mapping.in", "CRIC | Sony Sports 1"]:
            self.assertNotIn(private, rendered)

    def test_unknown_source_or_flags_are_counts_not_raw_error_output(self):
        report = diagnostic.summarize(table(row("Sony Sports 1", source="private-unsupported-source"), row("Sony Sports 2", enabled="private-invalid-flag")))
        groups = {item["group"]: item for item in report["groups"]}
        self.assertEqual(groups["Sony1"]["invalidGateRows"], 1)
        self.assertEqual(groups["Sony2"]["invalidGateRows"], 1)
        self.assertNotIn("private-", json.dumps(report))

    def test_mocked_sheet_is_read_once_parsed_strictly_and_session_closed(self):
        record = row("CRIC | Sony Sports 1")
        headers = list(inventory.streaming.SHEET_COLUMNS)
        values = [headers, [record[key] for key in headers]]
        session = mock.Mock()
        settings = {"GOOGLE_SHEET_ID": "x" * 25, "GOOGLE_SHEET_TAB": "Mappings", "GOOGLE_SERVICE_ACCOUNT_JSON": "private-google-key"}
        with mock.patch.dict(os.environ, settings), mock.patch.object(inventory, "authorized_google_session", return_value=session) as auth, mock.patch.object(inventory, "google_sheet_values", return_value=values) as read, mock.patch.object(inventory, "fetch_panel_inventory", side_effect=AssertionError("No provider calls")):
            report = diagnostic.diagnose()
        auth.assert_called_once_with("private-google-key")
        read.assert_called_once_with(session, "x" * 25, "Mappings")
        session.close.assert_called_once()
        self.assertEqual(report["selectedRows"], 1)
        self.assertNotIn("private-google-key", json.dumps(report))

    def test_sheet_error_returns_no_private_error_text(self):
        with mock.patch.dict(os.environ, {"GOOGLE_SHEET_ID": "x" * 25}), mock.patch.object(inventory, "authorized_google_session", side_effect=inventory.SyncError("private credential https://private.invalid")):
            report = diagnostic.diagnose()
        self.assertEqual(report["status"], "source_unavailable")
        self.assertEqual(report["selectedRows"], 0)
        self.assertNotIn("private", json.dumps(report))

    def test_console_contains_counts_only_and_report_fixed_labels(self):
        report = diagnostic.summarize(table(row("Sony Sports 1")))
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(diagnostic, "diagnose", return_value=report), contextlib.redirect_stdout(io.StringIO()) as stdout:
            path = Path(folder) / "report.json"
            self.assertEqual(diagnostic.main(["--output", str(path)]), 0)
            self.assertEqual(json.loads(path.read_bytes())["selectedRows"], 1)
        self.assertTrue(all(type(value) is int for value in json.loads(stdout.getvalue()).values()))
        self.assertNotIn("Sony", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
