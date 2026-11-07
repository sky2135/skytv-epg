from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_tivimate_playlist_links as diagnostic
import sync_channel_inventory as inventory


def playlist(*rows):
    text = "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 tvg-id="{epg_id}" group-title="SPORTS",{name}\n'
        f'https://panel.example/live/user123/pass123/{stream_id}.ts\n'
        for stream_id, name, epg_id in rows
    )
    categories, channels = inventory.parse_xtream_m3u(text, username="user123", password="pass123")
    return inventory.PanelInventory("server_1", "Server 1", categories, channels, "authenticated_m3u")


class DiagnoseTiviMatePlaylistLinksTests(unittest.TestCase):
    config = inventory.ServerConfig("server_1", "Server 1", "https://panel.example", "user123", "pass123")

    def test_exact_ids_and_all_shared_members_include_non_targets(self):
        source = playlist(
            ("122410", "UK || SKY SPORTS PREMIER LEAGUE", "Sky PL.uk"),
            ("122412", "UK || SKY SPORTS CRICKET", "Sky  Cricket.uk"),
            ("122414", "UK || SKY SPORTS PLUS", "Sky+HD.uk"),
            ("500", "Sky Sports Cricket FHD", "Sky  Cricket.uk"),
            ("501", "Cricket Other Feed", "Sky Cricket.uk"),
        )
        report = diagnostic.build_report(source)
        targets = {row["streamId"]: row for row in report["targets"]}
        self.assertEqual(set(targets), {"122410", "122412", "122414"})
        self.assertEqual(targets["122412"]["providerEpgId"], "Sky  Cricket.uk")
        self.assertEqual(targets["122412"]["memberCount"], 2)
        self.assertEqual({row["streamId"] for row in targets["122412"]["members"]}, {"122412", "500"})
        self.assertEqual(targets["122410"]["memberCount"], 1)
        rendered = json.dumps(report)
        self.assertNotIn("panel.example", rendered)
        self.assertNotIn("user123", rendered)
        self.assertNotIn("pass123", rendered)

    def test_shared_member_cap_retains_total_and_omitted_count(self):
        source = playlist(("122410", "Sky Sports Premier League", "shared"), *((str(index), f"Channel {index}", "shared") for index in range(1, 70)))
        target = diagnostic.build_report(source)["targets"][0]
        self.assertEqual(target["memberCount"], 70)
        self.assertEqual(len(target["members"]), 50)
        self.assertEqual(target["membersOmitted"], 20)

    def test_absent_unsafe_and_missing_ids_are_distinguished(self):
        source = playlist(("122410", "Sky Sports Premier League", ""), ("122412", "Sky Sports Cricket", " bad.id "))
        rows = diagnostic.build_report(source)["targets"]
        self.assertEqual([row["idStatus"] for row in rows], ["absent", "unsafe", "not_found"])
        self.assertTrue(all(row["providerEpgId"] == "" for row in rows))

    def test_unsafe_name_is_omitted_but_member_count_remains_exact(self):
        source = playlist(("122410", "Sky Sports Premier League", "shared"), ("500", "https://unrelated.invalid/channel", "shared"))
        row = diagnostic.build_report(source)["targets"][0]
        self.assertEqual(row["memberCount"], 2)
        self.assertIn({"streamId": "500", "name": ""}, row["members"])
        self.assertNotIn("https://", json.dumps(row))

    def test_wrong_server_or_non_playlist_source_is_rejected(self):
        source = playlist(("122410", "Sky Sports Premier League", "shared"))
        source.server_id = "server_3"
        self.assertEqual(diagnostic.build_report(source)["status"], "invalid_source")
        source.server_id, source.source = "server_1", "player_api"
        self.assertEqual(diagnostic.build_report(source)["status"], "invalid_source")

    def test_one_bounded_m3u_fetch_and_no_api_or_native_calls(self):
        source = playlist(("122410", "Sky Sports Premier League", "shared"))
        with mock.patch.object(inventory, "load_server_configs", return_value=[self.config]) as configs, mock.patch.object(inventory, "fetch_m3u_inventory", return_value=source) as fetch, mock.patch.object(inventory, "fetch_panel_inventory", side_effect=AssertionError("API not permitted")):
            report = diagnostic.diagnose(allow_insecure_http=False)
        configs.assert_called_once_with(["server_1"])
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[2], ["https://panel.example"])
        self.assertEqual(fetch.call_args.kwargs["maximum_attempts"], 1)
        self.assertEqual(fetch.call_args.kwargs["request_timeout"], (10, 60))
        self.assertEqual(report["candidateLookups"], 1)
        self.assertEqual(report["status"], "available")

    def test_unavailable_fetch_reports_facts_without_retry(self):
        with mock.patch.object(inventory, "load_server_configs", return_value=[self.config]), mock.patch.object(inventory, "fetch_m3u_inventory", return_value=None) as fetch:
            report = diagnostic.diagnose(allow_insecure_http=False)
        fetch.assert_called_once()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["candidateLookups"], 1)
        self.assertTrue(all(row["idStatus"] == "unavailable" for row in report["targets"]))

    def test_configuration_failure_does_not_make_a_request(self):
        with mock.patch.object(inventory, "load_server_configs", side_effect=inventory.SyncError("No credentials")), mock.patch.object(inventory, "fetch_m3u_inventory") as fetch:
            report = diagnostic.diagnose(allow_insecure_http=False)
        fetch.assert_not_called()
        self.assertEqual(report["status"], "configuration_unavailable")
        self.assertEqual(report["candidateLookups"], 0)

    def test_reflected_credentials_never_reach_report(self):
        source = playlist(("122410", "Sky Sports pass123", "shared"))
        with mock.patch.object(inventory, "load_server_configs", return_value=[self.config]), mock.patch.object(inventory, "fetch_m3u_inventory", return_value=source):
            report = diagnostic.diagnose(allow_insecure_http=False)
        self.assertEqual(report["status"], "source_rejected")
        self.assertNotIn("pass123", json.dumps(report))

    def test_transport_exception_text_is_never_copied(self):
        with mock.patch.object(inventory, "load_server_configs", return_value=[self.config]), mock.patch.object(inventory, "fetch_m3u_inventory", side_effect=inventory.SyncError("https://secret.invalid?password=private")) as fetch:
            report = diagnostic.diagnose(allow_insecure_http=False)
        fetch.assert_called_once()
        self.assertEqual(report["status"], "source_rejected")
        self.assertNotIn("secret", json.dumps(report))

    def test_cli_writes_checked_report_and_prints_only_counts(self):
        source = playlist(("122410", "Sky Sports Premier League", "shared"))
        report = diagnostic.build_report(source)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(diagnostic, "diagnose", return_value=report), contextlib.redirect_stdout(io.StringIO()) as stdout:
            output = Path(folder) / "report.json"
            self.assertEqual(diagnostic.main(["--output", str(output)]), 0)
            stored = json.loads(output.read_bytes())
        self.assertEqual(stored["targets"][0]["providerEpgId"], "shared")
        counts = json.loads(stdout.getvalue())
        self.assertTrue(all(type(value) is int for value in counts.values()))
        self.assertNotIn("shared", stdout.getvalue().replace('"sharedTargets"', '"count"'))


if __name__ == "__main__":
    unittest.main()
