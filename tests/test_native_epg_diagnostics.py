from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_provider_transport as diagnostic

NOW = int(datetime(2026, 11, 5, 14, tzinfo=timezone.utc).timestamp())


def channel(channel_id="private-channel"):
    return f'<channel id="{channel_id}"><display-name>Private channel</display-name></channel>'


def programme(start, stop, channel_id="private-channel"):
    stop_attribute = f' stop="{diagnostic.epg.xmltv_timestamp(stop)}"' if stop is not None else ""
    return (f'<programme channel="{channel_id}" start="{diagnostic.epg.xmltv_timestamp(start)}"'
            f'{stop_attribute}><title>Private programme title</title></programme>')


def source(programmes=None, *, declared=True, doctype=""):
    if programmes is None:
        programmes = [programme(NOW - 1800, NOW + 1800), programme(NOW + 1800, NOW + 5400)]
    return (f'<?xml version="1.0"?>{doctype}<tv>' + (channel() if declared else "")
            + "".join(programmes) + '</tv>').encode()


def config(server_id):
    return diagnostic.vod.ServerConfig(server_id, "https://private-panel.example", "private-user", "Secret-password")


class NativeEpgDiagnosticTests(unittest.TestCase):
    def probe(self, body, **kwargs):
        downloaded = []

        def download(server_id, destination, *, maximum_attempts):
            self.assertEqual(server_id, "server_3")
            self.assertEqual(maximum_attempts, 1)
            destination.write_bytes(body)
            downloaded.append(destination)
            return destination, {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

        with mock.patch.dict(os.environ, {"SKYTV_PANEL_CACHE_DIR": ""}), mock.patch.object(
            diagnostic.epg, "download_panel_xmltv", side_effect=download
        ):
            report = diagnostic.native_epg_probe("server_3", now=NOW, **kwargs)
        self.assertTrue(downloaded)
        self.assertFalse(downloaded[0].exists(), "Raw source must be removed after probing")
        return report

    def test_complete_raw_and_gzip_guides_have_actual_current_and_future_rows(self):
        for body in (source(), gzip.compress(source())):
            with self.subTest(gzip=body.startswith(b"\x1f\x8b")):
                report = self.probe(body)
            self.assertTrue(report["usable"])
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["httpStatus"], 200)
            self.assertEqual(report["channels"], 1)
            self.assertEqual(report["programmeRows"], 2)
            self.assertEqual(report["currentChannels"], 1)
            self.assertEqual(report["futureChannels"], 1)
            self.assertEqual(report["currentAndFutureChannels"], 1)
            self.assertEqual(report["checkedAt"], NOW)
            self.assertEqual(report["sha256"], hashlib.sha256(body).hexdigest())
            for private in ("private-channel", "Private channel", "Private programme title"):
                self.assertNotIn(private, json.dumps(report))

    def test_expired_future_only_current_only_and_distant_guides_are_not_recovery(self):
        cases = (
            [programme(NOW - 7200, NOW - 3600)],
            [programme(NOW + 600, NOW + 3600)],
            [programme(NOW - 600, NOW + 3600)],
            [programme(NOW + 90 * 86400, NOW + 90 * 86400 + 3600)],
            [programme(NOW - 3600, NOW + 90 * 86400), programme(NOW + 100, NOW + 90 * 86400)],
            [],
        )
        for programmes in cases:
            with self.subTest(programmes=programmes):
                report = self.probe(source(programmes))
            self.assertFalse(report["usable"])
            self.assertEqual(report["status"], "stale_or_empty")

    def test_inferred_stops_and_undeclared_id_cannot_prove_recovery(self):
        for body in (
            source([programme(NOW - 600, None), programme(NOW + 600, None)]),
            source(declared=False),
            source([programme(NOW - 600, NOW + 600, "orphan"), programme(NOW + 600, NOW + 3600, "orphan")]),
        ):
            with self.subTest(body=body):
                report = self.probe(body)
            self.assertFalse(report["usable"])
            self.assertEqual(report["programmeRows"], 0)

    def test_disjoint_current_and_future_channel_evidence_is_not_recovery(self):
        body = ('<tv>' + channel("current-only") + channel("future-only")
                + programme(NOW - 600, NOW + 600, "current-only")
                + programme(NOW + 600, NOW + 3600, "future-only") + '</tv>').encode()
        report = self.probe(body)
        self.assertFalse(report["usable"])
        self.assertEqual(report["currentChannels"], 1)
        self.assertEqual(report["futureChannels"], 1)
        self.assertEqual(report["currentAndFutureChannels"], 0)

    def test_unsafe_dtd_invalid_root_and_truncated_tail_never_report_success(self):
        for body in (
            source(doctype='<!DOCTYPE tv SYSTEM "https://private-panel.example/secret.dtd">'),
            source(doctype='<!DOCTYPE tv [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'),
            b'<html><body>Secret-password</body></html>',
            source()[:-5],
            source() + b'<private-tail>',
        ):
            with self.subTest(body=body):
                report = self.probe(body)
            self.assertFalse(report["usable"])
            self.assertEqual(report["status"], "invalid_or_rejected")
            self.assertNotIn("Secret-password", json.dumps(report))
            self.assertNotIn("private-panel.example", json.dumps(report))

    def test_standard_inert_xmltv_doctype_is_supported(self):
        self.assertTrue(self.probe(source(doctype='<!DOCTYPE tv SYSTEM "xmltv.dtd">'))["usable"])

    def test_native_server_one_is_rejected_before_any_download(self):
        with mock.patch.object(diagnostic.epg, "download_panel_xmltv") as download:
            with self.assertRaises(ValueError):
                diagnostic.native_epg_probe("server_1", now=NOW)
        download.assert_not_called()

    def test_unavailable_result_retains_safe_http_and_cooldown_only(self):
        error = diagnostic.epg.PanelSourceUnavailable("server_3", "HTTP 503")
        error.next_attempt_at = NOW + 3600
        with mock.patch.object(diagnostic.epg, "download_panel_xmltv", side_effect=error):
            report = diagnostic.native_epg_probe("server_3", now=NOW)
        self.assertFalse(report["usable"])
        self.assertEqual(report["httpStatus"], 503)
        self.assertEqual(report["nextAttemptAt"], NOW + 3600)
        self.assertEqual(report["issue"], "source_unavailable")

    def test_rejected_http_only_reports_status_not_exception_content(self):
        error = diagnostic.epg.BuildError(
            'server_3 panel failed HTTP 401 https://private-panel.example/?password=Secret-password'
        )
        with mock.patch.object(diagnostic.epg, "download_panel_xmltv", side_effect=error):
            report = diagnostic.native_epg_probe("server_3", now=NOW)
        self.assertFalse(report["usable"])
        self.assertEqual(report["httpStatus"], 401)
        self.assertNotIn("Secret-password", json.dumps(report))
        self.assertNotIn("private-panel.example", json.dumps(report))

    def test_configured_cache_saves_only_a_fully_usable_source(self):
        save = mock.Mock(return_value=True)
        body = source()

        def download(server_id, destination, *, maximum_attempts):
            destination.write_bytes(body)
            return destination, {"sha256": "a" * 64, "bytes": destination.stat().st_size}

        with tempfile.TemporaryDirectory() as cache, mock.patch.dict(
            os.environ, {"SKYTV_PANEL_CACHE_DIR": cache}
        ), mock.patch.dict(sys.modules, {"private_panel_cache": SimpleNamespace(save_source=save)}), \
            mock.patch.object(diagnostic.epg, "download_panel_xmltv", side_effect=download), \
            mock.patch.object(diagnostic.epg, "panel_credentials", return_value=("origin", "user", "secret")):
            report = diagnostic.native_epg_probe("server_3", now=NOW)
            body = source([programme(NOW - 3600, NOW - 1800)])
            expired_report = diagnostic.native_epg_probe("server_3", now=NOW)
        self.assertTrue(report["cacheSaved"])
        self.assertFalse(expired_report["usable"])
        self.assertNotIn("cacheSaved", expired_report)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "server_3")
        self.assertEqual(save.call_args.kwargs["captured_at"], NOW)
        self.assertEqual(save.call_args.kwargs["credentials"], ("origin", "user", "secret"))

    def test_optional_cache_error_does_not_relabel_a_valid_source(self):
        def download(server_id, destination, *, maximum_attempts):
            destination.write_bytes(source())
            return destination, {"sha256": "a" * 64, "bytes": destination.stat().st_size}

        save = mock.Mock(side_effect=OSError("Secret-password"))
        with tempfile.TemporaryDirectory() as cache, mock.patch.dict(
            os.environ, {"SKYTV_PANEL_CACHE_DIR": cache}
        ), mock.patch.dict(sys.modules, {"private_panel_cache": SimpleNamespace(save_source=save)}), \
            mock.patch.object(diagnostic.epg, "download_panel_xmltv", side_effect=download), \
            mock.patch.object(diagnostic.epg, "panel_credentials", return_value=("origin", "user", "secret")):
            report = diagnostic.native_epg_probe("server_3", now=NOW)
        self.assertTrue(report["usable"])
        self.assertFalse(report["cacheSaved"])
        self.assertNotIn("Secret-password", json.dumps(report))

    def test_native_mode_selects_only_requested_server_and_writes_safe_report(self):
        result = {"serverId": "server_3", "usable": False, "status": "unavailable", "httpStatus": 502}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            diagnostic.vod, "load_server_config", side_effect=lambda server, **_: config(server)
        ) as load, mock.patch.object(diagnostic, "native_epg_probe", return_value=result) as probe, \
            mock.patch.object(diagnostic, "inventory_probe") as inventory, redirect_stdout(output):
            target = Path(folder) / "nested" / "report.json"
            self.assertEqual(diagnostic.main([
                "--mode", "native_epg", "--servers", "server_3", "--output", str(target)
            ]), 0)
            report = json.loads(target.read_text())
        self.assertFalse(report["recovered"])
        self.assertEqual(report["nativeEpg"], [result])
        self.assertEqual(load.call_args.args, ("server_3",))
        probe.assert_called_once_with("server_3")
        inventory.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_one_unavailable_source_does_not_hide_other_source_recovery(self):
        results = [
            {"serverId": "server_2", "usable": True, "status": "ok"},
            {"serverId": "server_3", "usable": False, "status": "unavailable"},
        ]
        with mock.patch.object(diagnostic.vod, "load_server_config", side_effect=lambda server, **_: config(server)), \
            mock.patch.object(diagnostic, "native_epg_probe", side_effect=results) as probe, \
            redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostic.main(["--mode", "native_epg"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["nativeEpg"], results)
        self.assertFalse(report["recovered"])
        self.assertEqual([call.args[0] for call in probe.call_args_list], ["server_2", "server_3"])

    def test_one_rejected_configuration_does_not_prevent_other_server_probe(self):
        result = {"serverId": "server_3", "usable": True, "status": "ok"}
        with mock.patch.object(diagnostic.vod, "load_server_config", side_effect=[
            diagnostic.vod.CatalogBuildError("Secret-password private-panel.example"), config("server_3")
        ]), mock.patch.object(diagnostic, "native_epg_probe", return_value=result) as probe, \
            redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostic.main(["--mode", "native_epg"]), 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["recovered"])
        self.assertEqual(report["nativeEpg"][0], {
            "serverId": "server_2", "status": "configuration_rejected",
            "usable": False, "issue": "invalid_configuration",
        })
        self.assertEqual(report["nativeEpg"][1], result)
        probe.assert_called_once_with("server_3")
        self.assertNotIn("Secret-password", output.getvalue())
        self.assertNotIn("private-panel.example", output.getvalue())

    def test_live_default_now_probes_all_three_and_redirects_both_native_sources(self):
        with mock.patch.object(diagnostic.vod, "load_server_config", side_effect=lambda server, **_: config(server)) as load, \
            mock.patch.object(diagnostic.inventory, "load_server_configs", return_value=[]) as inventory_configs, \
            mock.patch.object(diagnostic, "production_session_inventory_probe", return_value=[]), \
            mock.patch.object(diagnostic, "inventory_probe", side_effect=lambda conf, _identity: {"serverId": conf.server_id}), \
            mock.patch.object(diagnostic, "actual_inventory_probe", side_effect=lambda conf, _allow: {"serverId": conf.server_id}), \
            mock.patch.object(diagnostic, "redirect_probe", side_effect=lambda conf, _configs: {"serverId": conf.server_id}) as redirect, \
            redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diagnostic.main([]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual([call.args[0] for call in load.call_args_list], list(diagnostic.ALL_SERVERS))
        inventory_configs.assert_called_once_with(list(diagnostic.ALL_SERVERS))
        self.assertEqual([call.args[0].server_id for call in redirect.call_args_list], ["server_2", "server_3"])
        self.assertEqual(report["redirects"], [{"serverId": "server_2"}, {"serverId": "server_3"}])
        self.assertEqual(report["redirect"], {"serverId": "server_2"})
        self.assertEqual(report["additionalInventory"], [{"serverId": "server_2"}, {"serverId": "server_3"}])


if __name__ == "__main__":
    unittest.main()
