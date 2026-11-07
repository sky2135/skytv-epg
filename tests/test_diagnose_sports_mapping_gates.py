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

import diagnose_sports_mapping_gates as diagnostic
import diagnose_uk_mapping_gates as uk
import sync_channel_inventory as inventory


PHOTO_LABELS = (
    "SUPERSPORTS PSL (FHD)", "Sky Sports Premier League (FHD)", "Premier 2 (FHD)",
    "SUPERSPORTS FOOTBALL (FHD)", "SUPERSPORTS LALIGA (FHD)",
    "SUPERSPORTS PREMIER LEAGUE (FHD)", "TSN 1",
)


def values_for(*records):
    headers = list(inventory.streaming.SHEET_COLUMNS)
    rows = []
    for index, changes in enumerate(records):
        record = dict.fromkeys(headers, "")
        record.update(server_id="server_1", stream_id=f"private-stream-{index}",
                      channel_name=changes["channel_name"], category_name="private-category",
                      action="APPROVED", enabled="TRUE", source="epgshare01", epg_feed="private-feed",
                      epg_id="private-epg-id", country_codes="GB", language_codes="en", primary_language="en")
        record.update(changes)
        rows.append([record[key] for key in headers])
    return [headers, *rows]


def table(*records):
    return inventory.parse_table_values(values_for(*records), maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS)


def row(name, **changes):
    return {"channel_name": name, **changes}


def grouped(report):
    return {item["group"]: item for item in report["groups"]}


class DiagnoseSportsMappingGatesTests(unittest.TestCase):
    def test_seven_exact_photo_labels_and_compatibility_typography(self):
        report = diagnostic.summarize(table(*(row(name) for name in PHOTO_LABELS)))
        self.assertEqual(report["selectedRows"], 7)
        self.assertEqual([item["rowCount"] for item in report["groups"]], [1] * 7)
        fullwidth = " ＳＫＹ ＳＰＯＲＴＳ　ＰＲＥＭＩＥＲ ＬＥＡＧＵＥ （ＦＨＤ） "
        report = diagnostic.summarize(table(row(fullwidth), row(" tsn\t1 ")))
        self.assertEqual(report["selectedRows"], 2)
        self.assertEqual(grouped(report)["SkySportsPremierLeagueFHD"]["rowCount"], 1)

    def test_timeshifts_markets_brands_quality_and_channel_numbers_stay_distinct(self):
        rejected = (
            "SUPERSPORTS FOOTBALL PLUS (FHD)", "SuperSport Football (FHD)",
            "Sky Sports Premier League HD", "Sky Sports Premier League FHD",
            "Sky Sports Premier League", "SuperSports Premier League (HD)",
            "UK: Sky Sports Premier League (FHD)", "IE: Premier 2 (FHD)",
            "Premier Sports 2 (FHD)", "Premier 2", "Premier 1 (FHD)",
            "TSN 1 HD", "TSN 2", "TSN 3", "TSN 4", "TSN 5", "TSN 11",
        )
        self.assertEqual(diagnostic.summarize(table(*(row(name) for name in rejected)))["selectedRows"], 0)
        report = diagnostic.summarize(table(row(PHOTO_LABELS[1]), row(PHOTO_LABELS[5])))
        self.assertEqual(grouped(report)["SkySportsPremierLeagueFHD"]["rowCount"], 1)
        self.assertEqual(grouped(report)["SuperSportsPremierLeagueFHD"]["rowCount"], 1)

    def test_country_is_observed_not_approval_and_server_scope_is_fixed(self):
        report = diagnostic.summarize(table(
            row(PHOTO_LABELS[0], country_codes="ZA|NG"),
            row(PHOTO_LABELS[0], country_codes="IE", category_name="IRELAND"),
            row(PHOTO_LABELS[0], server_id="server_2"), row(PHOTO_LABELS[0], server_id="server_3"),
        ))
        item = grouped(report)["SuperSportsPSLFHD"]
        self.assertEqual(report["selectedRows"], 2)
        self.assertTrue(report["countryMetadataIsNotMatchApproval"])
        self.assertEqual(item["countryCodes"], ["IE", "NG", "ZA"])
        self.assertEqual(item["vetoedLookalikeRows"], 0)

    def test_safe_id_provenance_keeps_all_requested_market_and_tsn_variants(self):
        records = [row("TSN 1", epg_id=identifier) for identifier in diagnostic.KNOWN_EPG_IDS]
        records += [row("TSN 1", epg_id=""), row("TSN 1", epg_id="https://private.invalid/id")]
        report = diagnostic.summarize(table(*records))
        counts = grouped(report)["TSN1"]["epgIdProvenanceCounts"]
        self.assertEqual(counts, {**dict.fromkeys(diagnostic.KNOWN_EPG_IDS.values(), 1), "blank": 1, "other_nonempty": 1})
        self.assertNotEqual(counts.get("tsn_1_ca2"), None)
        self.assertIn("tsn_2_ca2", counts)
        for identifier in diagnostic.KNOWN_EPG_IDS:
            self.assertNotIn(identifier, json.dumps(report))

    def test_aggregate_gates_and_privacy_reuse_without_changing_uk_defaults(self):
        uk_table = table(row("UK: BBC 1", epg_id="BBC.One.Lon.HD.uk"))
        before = uk.summarize(uk_table)
        report = diagnostic.summarize(table(
            row(PHOTO_LABELS[0], action="REVIEW", enabled="FALSE", source="panel", epg_feed="PANEL",
                metadata_locked="TRUE", notes="manual review; private-note https://private.invalid"),
            row(PHOTO_LABELS[1], action="IGNORE", enabled="FALSE"),
            row(PHOTO_LABELS[2], action="MANUAL", enabled="FALSE"),
            row(PHOTO_LABELS[3], source="private-invalid-source"),
            row(PHOTO_LABELS[4], enabled="private-invalid-flag"),
            row(PHOTO_LABELS[5], language_codes="en|private-language"),
        ))
        groups = grouped(report)
        psl = groups["SuperSportsPSLFHD"]
        self.assertEqual((psl["reviewRows"], psl["sourcePolicyBlockedRows"], psl["metadataLockedRows"], psl["protectedNoteRows"]), (1, 1, 1, 1))
        self.assertEqual(groups["SkySportsPremierLeagueFHD"]["explicitIgnoreRows"], 1)
        self.assertEqual(groups["Premier2FHD"]["manualDecisionRows"], 1)
        self.assertEqual(groups["SuperSportsFootballFHD"]["invalidGateRows"], 1)
        self.assertEqual(groups["SuperSportsLaLigaFHD"]["invalidGateRows"], 1)
        self.assertEqual(uk.summarize(uk_table), before)
        self.assertEqual(before["groups"][0]["epgIdProvenanceCounts"], {"bbc_one_london": 1})
        rendered = json.dumps(report)
        for private in ("private", *PHOTO_LABELS, *diagnostic.KNOWN_EPG_IDS):
            self.assertNotIn(private, rendered)

    def test_single_strict_sheet_read_closes_and_never_calls_provider_or_writes(self):
        session = mock.Mock()
        values = values_for(row("TSN 1"))
        settings = {"GOOGLE_SHEET_ID": "x" * 25, "GOOGLE_SHEET_TAB": "Mappings", "GOOGLE_SERVICE_ACCOUNT_JSON": "private-key"}
        with mock.patch.dict(os.environ, settings), \
                mock.patch.object(inventory, "authorized_google_session", return_value=session) as auth, \
                mock.patch.object(inventory, "google_sheet_values", return_value=values) as read, \
                mock.patch.object(inventory, "parse_table_values", wraps=inventory.parse_table_values) as parse, \
                mock.patch.object(inventory, "fetch_panel_inventory", side_effect=AssertionError("No provider calls")):
            report = diagnostic.diagnose()
        auth.assert_called_once_with("private-key")
        read.assert_called_once_with(session, "x" * 25, "Mappings")
        parse.assert_called_once_with(values, maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS)
        session.close.assert_called_once()
        session.post.assert_not_called()
        session.put.assert_not_called()
        session.patch.assert_not_called()
        self.assertEqual(report["selectedRows"], 1)

    def test_unavailable_and_invalid_schema_are_sanitized_for_the_seven_groups(self):
        for failure in (inventory.SyncError("private https://private.invalid"), [["private-bad-header"]]):
            session = mock.Mock()
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.dict(os.environ, {"GOOGLE_SHEET_ID": "x" * 25}), \
                    mock.patch.object(inventory, "authorized_google_session", return_value=session), \
                    mock.patch.object(inventory, "google_sheet_values", **({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})):
                report = diagnostic.diagnose()
            self.assertEqual(report["status"], "source_unavailable")
            self.assertEqual(report["selectedRows"], 0)
            self.assertEqual(tuple(item["group"] for item in report["groups"]), diagnostic.GROUPS)
            self.assertNotIn("private", json.dumps(report))
            session.close.assert_called_once()

    def test_main_outputs_counts_only_and_writes_the_sanitized_report(self):
        report = diagnostic.summarize(table(row("TSN 1")))
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(diagnostic, "diagnose", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            path = Path(folder) / "report.json"
            self.assertEqual(diagnostic.main(["--output", str(path)]), 0)
            self.assertEqual(json.loads(path.read_bytes()), report)
        self.assertTrue(all(type(value) is int for value in json.loads(stdout.getvalue()).values()))
        self.assertNotIn("TSN", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
