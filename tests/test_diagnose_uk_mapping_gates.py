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

import diagnose_uk_mapping_gates as diagnostic
import sync_channel_inventory as inventory


def row(name, **changes):
    result = dict.fromkeys(inventory.streaming.SHEET_COLUMNS, "")
    result.update(server_id="server_1", stream_id="private-stream-id", channel_name=name,
                  category_name="UK | GENERAL", action="APPROVED", enabled="TRUE",
                  source="epgshare01", epg_feed="UK1", epg_id="private-epg-id",
                  country_codes="GB", primary_language="en", language_codes="en")
    result.update(changes)
    return result


def values_for(*records):
    headers = list(inventory.streaming.SHEET_COLUMNS)
    return [headers] + [[record[key] for key in headers] for record in records]


def table(*records):
    # Exercise the real bounded Version 1 parser, not synthetic MappingTable
    # instances that could bypass production schema and identity validation.
    prepared = []
    for index, record in enumerate(records):
        prepared.append(dict(record, stream_id=f"private-stream-{index}"))
    return inventory.parse_table_values(values_for(*prepared), maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS)


def grouped(report):
    return {item["group"]: item for item in report["groups"]}


class DiagnoseUKMappingGatesTests(unittest.TestCase):
    def test_exact_labels_preserve_timeshift_and_scotland(self):
        examples = {
            "UK: BBC1": "BBC1", "UK: BBC 1": "BBC1",
            " ＵＫ： ＢＢＣ １ ＦＨＤ ": "BBC1",
            "UK: BBC TWO (SCOTLAND)": "BBCTwoScotland",
            "UK : BBC TWO ( SCOTLAND ) HD": "BBCTwoScotland",
            "UK: ITV +1": "ITVPlus1", "UK: ITV+1 ᴴᴰ": "ITVPlus1",
            "UK: BBC  3 HD": "BBC3", "Talk TV": "TalkTV",
        }
        for name, group in examples.items():
            with self.subTest(name=name):
                report = diagnostic.summarize(table(row(name)))
                self.assertEqual(report["selectedRows"], 1)
                self.assertEqual(grouped(report)[group]["rowCount"], 1)
        for name in ("UK: ITV1", "UK: ITV 1", "UK: ITV+2", "UK: ITV2+1",
                     "UK: BBC TWO", "UK: BBC TWO (ENGLAND)", "UK: BBC TWO (WALES)",
                     "UK: BBC 11", "UK: BBC 1 +1", "BBC1", "UK: TalkSport"):
            with self.subTest(name=name):
                self.assertEqual(diagnostic.summarize(table(row(name)))["selectedRows"], 0)

    def test_server_scope_and_other_market_metadata_are_vetoed(self):
        report = diagnostic.summarize(table(
            row("UK: BBC 1"), row("UK: BBC 1", server_id="server_2"),
            row("UK: BBC 1", server_id="server_3"),
            row("US: BBC 1", country_codes="US"),
            row("UK: BBC 1", country_codes="GB|IE"),
            row("Talk TV", category_name="USA | NEWS"),
            row("UK: BBC TWO (SCOTLAND)", category_name="UK | ENGLAND"),
            row("UK: ITV +1", country_codes="private-country"),
        ))
        self.assertEqual(report["selectedRows"], 1)
        self.assertEqual(report["vetoedLookalikeRows"], 5)
        groups = grouped(report)
        self.assertEqual(groups["BBC1"]["vetoedLookalikeRows"], 2)
        self.assertEqual(groups["BBCTwoScotland"]["vetoReasonCounts"], {"different_uk_region_metadata": 1})
        self.assertEqual(groups["ITVPlus1"]["vetoReasonCounts"], {"unrecognized_country_metadata": 1})
        self.assertNotIn("private-country", json.dumps(report))

    def test_english_language_category_does_not_mean_england_region(self):
        report = diagnostic.summarize(table(row("UK: BBC TWO (SCOTLAND)", category_name="ENGLISH | TV")))
        self.assertEqual(report["selectedRows"], 1)

    def test_gates_and_private_note_categories_are_aggregates(self):
        report = diagnostic.summarize(table(
            row("UK: BBC 1", source="panel", epg_feed="PANEL", metadata_locked="TRUE",
                notes="manual review private-note-8675309 https://private.invalid/provider"),
            row("UK: BBC TWO (SCOTLAND)", enabled="FALSE", action="REVIEW"),
            row("UK: ITV +1", enabled="FALSE", action="IGNORE"),
            row("UK: BBC 3 HD", enabled="FALSE", action="MANUAL"),
            row("Talk TV", notes="do not remap; guide is locked", language_codes="en|cy|private-language"),
        ))
        groups = grouped(report)
        self.assertEqual(groups["BBC1"]["sourcePolicyBlockedRows"], 1)
        self.assertEqual(groups["BBC1"]["sourceCounts"], {"panel": 1})
        self.assertEqual(groups["BBC1"]["protectedNoteRows"], 1)
        self.assertEqual(groups["BBC1"]["protectedTermCounts"]["manual_review"], 1)
        self.assertEqual(groups["BBC1"]["metadataLockedRows"], 1)
        self.assertEqual(groups["BBC1"]["enabledRows"], 1)
        self.assertEqual(groups["BBCTwoScotland"]["reviewRows"], 1)
        self.assertEqual(groups["BBCTwoScotland"]["manualDisabledRows"], 0)
        self.assertEqual(groups["ITVPlus1"]["explicitIgnoreRows"], 1)
        self.assertEqual(groups["BBC3"]["manualDecisionRows"], 1)
        self.assertEqual(groups["BBC3"]["manualDisabledRows"], 1)
        self.assertEqual(groups["TalkTV"]["eligibleRows"], 1)
        self.assertEqual(groups["TalkTV"]["protectedTermCounts"]["do_not_remap"], 1)
        self.assertEqual(groups["TalkTV"]["languageCodes"], ["cy", "en"])
        self.assertEqual(groups["TalkTV"]["unrecognizedLanguageRows"], 1)
        self.assertEqual(groups["BBC1"]["countryCodes"], ["GB"])
        for private in ("private-note-8675309", "private.invalid", "private-language", "private-stream", "private-epg-id", "UK: BBC 1"):
            self.assertNotIn(private, json.dumps(report))

    def test_exact_public_id_enums_do_not_approve_a_match(self):
        report = diagnostic.summarize(table(
            row("UK: BBC 1", epg_id="BBC.One.Lon.HD.uk"),
            row("UK: BBC TWO (SCOTLAND)", epg_id="BBC.Two.HD.uk"),
            row("UK: ITV +1", epg_id="ITV1.HD.uk"),
            row("UK: ITV +1", epg_id="ITV1+1.uk"),
            row("UK: BBC 3 HD", epg_id="BBC.Three.HD.uk"),
            row("Talk TV", epg_id="TalkRadio.(TalkTV).uk"),
            row("Talk TV", epg_id=""),
            row("UK: BBC 1", epg_id="bbc.one.lon.hd.uk"),
        ))
        groups = grouped(report)
        self.assertTrue(report["idProvenanceIsNotMatchApproval"])
        self.assertEqual(groups["BBC1"]["epgIdProvenanceCounts"], {"bbc_one_london": 1, "other_nonempty": 1})
        self.assertEqual(groups["BBCTwoScotland"]["epgIdProvenanceCounts"], {"bbc_two_generic": 1})
        self.assertEqual(groups["ITVPlus1"]["epgIdProvenanceCounts"], {"itv1_main": 1, "itv1_plus_one": 1})
        self.assertEqual(groups["TalkTV"]["epgIdProvenanceCounts"], {"blank": 1, "talk_radio_talk_tv": 1})
        for identifier in diagnostic.KNOWN_EPG_IDS:
            self.assertNotIn(identifier, json.dumps(report))

    def test_invalid_flags_sources_and_actions_never_echo_raw_values(self):
        report = diagnostic.summarize(table(
            row("UK: BBC 1", source="private-source", epg_id="https://private.invalid/id"),
            row("UK: BBC 1", enabled="private-enabled"),
            row("UK: BBC 3 HD", metadata_locked="private-locked"),
            row("Talk TV", action="PRIVATE-ACTION"),
        ))
        groups = grouped(report)
        self.assertEqual(groups["BBC1"]["invalidGateRows"], 2)
        self.assertEqual(groups["BBC3"]["invalidGateRows"], 1)
        self.assertEqual(groups["TalkTV"]["actionCounts"], {"other": 1})
        self.assertEqual(groups["TalkTV"]["eligibleRows"], 0)
        self.assertNotIn("private", json.dumps(report).casefold())

    def test_one_sheet_read_uses_strict_parser_and_closes_without_provider_or_writes(self):
        session = mock.Mock()
        settings = {"GOOGLE_SHEET_ID": "x" * 25, "GOOGLE_SHEET_TAB": "Mappings", "GOOGLE_SERVICE_ACCOUNT_JSON": "private-google-key"}
        with mock.patch.dict(os.environ, settings), \
                mock.patch.object(inventory, "authorized_google_session", return_value=session) as auth, \
                mock.patch.object(inventory, "google_sheet_values", return_value=values_for(row("UK: BBC 1"))) as read, \
                mock.patch.object(inventory, "parse_table_values", wraps=inventory.parse_table_values) as parse, \
                mock.patch.object(inventory, "fetch_panel_inventory", side_effect=AssertionError("No provider calls")):
            report = diagnostic.diagnose()
        auth.assert_called_once_with("private-google-key")
        read.assert_called_once_with(session, "x" * 25, "Mappings")
        parse.assert_called_once_with(read.return_value, maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS)
        session.close.assert_called_once()
        session.post.assert_not_called()
        session.put.assert_not_called()
        session.patch.assert_not_called()
        self.assertEqual(report["selectedRows"], 1)
        self.assertNotIn("private-google-key", json.dumps(report))

    def test_bad_table_or_sheet_failure_is_sanitized_and_session_closed(self):
        bad_values = values_for(row("UK: BBC 1"))
        bad_values[0] = list(reversed(bad_values[0]))
        for failure in (bad_values, inventory.SyncError("private credential https://private.invalid")):
            session = mock.Mock()
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.dict(os.environ, {"GOOGLE_SHEET_ID": "x" * 25}), \
                    mock.patch.object(inventory, "authorized_google_session", return_value=session), \
                    mock.patch.object(inventory, "google_sheet_values", **({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})):
                report = diagnostic.diagnose()
            self.assertEqual(report["status"], "source_unavailable")
            self.assertEqual(report["selectedRows"], 0)
            self.assertNotIn("private", json.dumps(report))
            session.close.assert_called_once()

    def test_console_counts_only_and_atomic_report_contains_no_private_data(self):
        report = diagnostic.summarize(table(row("UK: BBC 1")))
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(diagnostic, "diagnose", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            path = Path(folder) / "report.json"
            self.assertEqual(diagnostic.main(["--output", str(path)]), 0)
            saved = json.loads(path.read_bytes())
        self.assertEqual(saved["selectedRows"], 1)
        self.assertTrue(all(type(value) is int for value in json.loads(stdout.getvalue()).values()))
        self.assertNotIn("BBC", stdout.getvalue())
        self.assertNotIn("private", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
