from __future__ import annotations

import gzip
import io
import json
import socket
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_provider_transport as diagnostics


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
PUBLIC_URL = "https://guide.example/public.xml.gz"
XMLTV = (
    b'<?xml version="1.0"?><tv><channel id="fixture"><display-name>Private title</display-name>'
    b'</channel><programme channel="fixture" start="20261018120000 +0000" '
    b'stop="20261018130000 +0000"><title>Do not log this</title></programme></tv>'
)


def config(server_id: str = "server_2"):
    return diagnostics.vod.ServerConfig(
        server_id=server_id,
        base_url="https://panel.example",
        username="bob",
        password="Sensitive-Panel-Password",
    )


def response(body: bytes = b"", *, status: int = 200, headers: dict | None = None):
    value = mock.MagicMock()
    value.__enter__.return_value = value
    value.status_code = status
    value.headers = headers or {}
    value.iter_content.return_value = iter([body])
    return value


def session(*responses):
    value = mock.MagicMock()
    value.__enter__.return_value = value
    value.get.side_effect = responses
    return value


class ProviderTransportDiagnosticsTests(unittest.TestCase):
    def test_rejects_invalid_or_credential_bearing_destinations_before_dns(self):
        for target in (
            "", "   ", "http://guide.example/guide.xml", "ftp://guide.example/guide.xml",
            "https://username:password@guide.example/guide.xml",
            "https://guide.example:444/guide.xml", "https://gu\u0131de.example/guide.xml",
            "https://guide.example:bad/guide.xml",
        ):
            with self.subTest(target=target), mock.patch.object(diagnostics.socket, "getaddrinfo") as dns:
                self.assertIsNone(diagnostics.public_target(target, config().base_url, [config()]))
                dns.assert_not_called()

    def test_rejects_private_mixed_and_unresolved_dns(self):
        for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"):
            addresses = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (private, 443))]
            with self.subTest(address=private), mock.patch.object(
                diagnostics.socket, "getaddrinfo", return_value=addresses
            ):
                self.assertIsNone(diagnostics.public_target(PUBLIC_URL, config().base_url, [config()]))
        with mock.patch.object(diagnostics.socket, "getaddrinfo", side_effect=socket.gaierror("private diagnostic")):
            self.assertIsNone(diagnostics.public_target(PUBLIC_URL, config().base_url, [config()]))

    def test_public_https_target_discards_query_and_fragment(self):
        credential_query = "?username=bob&password=Sensitive-Panel-Password#private-fragment"
        with mock.patch.object(diagnostics.socket, "getaddrinfo", return_value=PUBLIC_DNS):
            self.assertEqual(
                diagnostics.public_target(PUBLIC_URL + credential_query, config().base_url, [config()]),
                PUBLIC_URL,
            )

    def test_reflected_short_case_and_encoded_credentials_never_reach_dns(self):
        for target in (
            "https://BOB.guide.example/guide.xml",
            "https://guide.example/BOB/guide.xml",
            "https://guide.example/SENSITIVE-panel-PASSWORD/guide.xml",
            "https://guide.example/%53ensitive%2DPanel%2DPassword/guide.xml",
            "https://guide.example/%2553ensitive%252DPanel%252DPassword/guide.xml",
            "https://guide.example/panel.example/guide.xml",
        ):
            with self.subTest(target=target), mock.patch.object(diagnostics.socket, "getaddrinfo") as dns:
                self.assertIsNone(diagnostics.public_target(target, config().base_url, [config()]))
                dns.assert_not_called()

    def test_clean_session_excludes_ambient_authentication_and_cookies(self):
        with diagnostics.clean_session() as clean:
            self.assertFalse(clean.trust_env)
            self.assertEqual(list(clean.cookies), [])
            prepared = clean.prepare_request(requests.Request(
                "GET", PUBLIC_URL, headers={"Authorization": "Bearer do-not-send"}
            ))
            self.assertNotIn("Authorization", prepared.headers)
            self.assertNotIn("Cookie", prepared.headers)

    def test_anonymous_raw_and_gzip_xmltv_sample_is_summarized_without_content(self):
        for body in (XMLTV, gzip.compress(XMLTV)):
            clean = session(response(body))
            with self.subTest(gzip=body.startswith(b"\x1f\x8b")), mock.patch.object(
                diagnostics, "clean_session", return_value=clean
            ):
                report = diagnostics.anonymous_xmltv(PUBLIC_URL)
            self.assertEqual(report["xmlRoot"], "tv")
            self.assertTrue(report["hasChannel"])
            self.assertTrue(report["hasProgramme"])
            self.assertFalse(report["credentialsSent"])
            self.assertNotIn("Private title", json.dumps(report))
            self.assertNotIn("Do not log this", json.dumps(report))
            args, kwargs = clean.get.call_args
            self.assertEqual(args, (PUBLIC_URL,))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertNotIn("params", kwargs)
            self.assertNotIn("auth", kwargs)
            self.assertNotIn("cookies", kwargs)

    def test_html_is_not_reported_as_an_xmltv_guide(self):
        clean = session(response(b"<html><body>Authentication failed: secret</body></html>"))
        with mock.patch.object(diagnostics, "clean_session", return_value=clean):
            report = diagnostics.anonymous_xmltv(PUBLIC_URL)
        self.assertEqual(report["xmlRoot"], "other")
        self.assertNotIn("hasChannel", report)
        self.assertNotIn("hasProgramme", report)
        self.assertNotIn("secret", json.dumps(report))

    def test_redirect_switches_sessions_and_never_forwards_query_or_cookies(self):
        panel = session(response(status=302, headers={
            "Location": PUBLIC_URL + "?username=bob&password=Sensitive-Panel-Password"
        }))
        anonymous = session(response(XMLTV))
        with mock.patch.object(diagnostics, "clean_session", side_effect=[panel, anonymous]), mock.patch.object(
            diagnostics.socket, "getaddrinfo", return_value=PUBLIC_DNS
        ):
            report = diagnostics.redirect_probe(config(), [config()])
        self.assertTrue(report["safeAnonymousTarget"])
        self.assertTrue(report["anonymousTarget"]["hasProgramme"])
        self.assertEqual(panel.get.call_args.kwargs["params"]["username"], "bob")
        self.assertFalse(panel.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(anonymous.get.call_args.args, (PUBLIC_URL,))
        self.assertNotIn("params", anonymous.get.call_args.kwargs)
        self.assertNotIn("Sensitive-Panel-Password", json.dumps(report))

    def test_inventory_failures_and_private_payload_are_only_reported_as_counts(self):
        leaked_url = "https://panel.example/player_api.php?username=bob&password=Sensitive-Panel-Password"
        clean = session(
            requests.ConnectionError(leaked_url),
            response(json.dumps([{"name": "Private title", "password": "Sensitive-Panel-Password"}]).encode()),
        )
        with mock.patch.object(diagnostics, "clean_session", return_value=clean):
            report = diagnostics.inventory_probe(config("server_1"), "fixture-agent")
        self.assertEqual(report["checks"][0]["issue"], "request_or_payload_failed")
        self.assertEqual(report["checks"][1]["rowCount"], 1)
        for private in (leaked_url, "Sensitive-Panel-Password", "Private title"):
            self.assertNotIn(private, json.dumps(report))

    def test_actual_inventory_reports_only_source_and_counts(self):
        clean = session()
        value = SimpleNamespace(
            source="panel_api",
            categories={"private-category": "Private title"},
            channels=[{"private": "Sensitive-Panel-Password"}] * 2,
            quarantined_stream_ids=("private-stream-id",),
        )
        with mock.patch.object(diagnostics, "clean_session", return_value=clean), mock.patch.object(
            diagnostics.inventory, "fetch_panel_inventory", return_value=value
        ) as fetch:
            report = diagnostics.actual_inventory_probe(config("server_1"), True)
        self.assertEqual(report, {
            "serverId": "server_1", "ok": True, "source": "panel_api",
            "categoryCount": 1, "channelCount": 2,
            "quarantinedRowCount": 1,
        })
        self.assertTrue(fetch.call_args.kwargs["allow_insecure_http"])
        clean.headers.update.assert_called_once_with({
            "User-Agent": f"SKYTV-Channel-Inventory/{diagnostics.inventory.SYNC_VERSION}",
            "Accept": diagnostics.vod.PROVIDER_HEADERS["Accept"],
        })
        self.assertNotIn("Private title", json.dumps(report))
        self.assertNotIn("Sensitive-Panel-Password", json.dumps(report))
        self.assertNotIn("private-stream-id", json.dumps(report))

    def test_actual_server_two_inventory_uses_its_own_identity_and_label(self):
        server_two = config("server_2")
        value = SimpleNamespace(source="player_api", categories=[], channels=[], quarantined_stream_ids=())
        with mock.patch.object(diagnostics, "clean_session", return_value=session()), mock.patch.object(
            diagnostics.inventory, "fetch_panel_inventory", return_value=value
        ) as fetch:
            report = diagnostics.actual_inventory_probe(server_two, False)
        sync_config = fetch.call_args.args[1]
        self.assertEqual(sync_config.server_id, "server_2")
        self.assertEqual(sync_config.server_label, "Server 2")
        self.assertEqual((sync_config.base_url, sync_config.username, sync_config.password),
                         (server_two.base_url, server_two.username, server_two.password))
        self.assertEqual(report["serverId"], "server_2")
        self.assertTrue(report["ok"])

    def test_actual_inventory_failure_classification_never_returns_exception_text(self):
        private_url = "https://panel.example/?username=bob&password=Sensitive-Panel-Password"
        for message, code in (
            ("Persistence was blocked", "reflection_guard"),
            ("Password is too short", "short_password_guard"),
            ("Malformed percent", "malformed_transport_text"),
            ("Account is not active", "inactive_account"),
            ("Rejected its configured credentials", "auth_rejected"),
            ("Unsafe credential redirect", "redirect_guard"),
            ("Network request failed", "network_error"),
            ("Unexpected failure", "sync_error"),
        ):
            error = diagnostics.inventory.SyncError(message + ": " + private_url)
            with self.subTest(code=code), mock.patch.object(
                diagnostics, "clean_session", return_value=session()
            ), mock.patch.object(diagnostics.inventory, "fetch_panel_inventory", side_effect=error):
                report = diagnostics.actual_inventory_probe(config("server_1"), False)
            self.assertFalse(report["ok"])
            self.assertEqual(report["errorCode"], code)
            self.assertNotIn(private_url, json.dumps(report))
            self.assertNotIn("Sensitive-Panel-Password", json.dumps(report))

    def test_server_two_request_failure_preserves_server_one_results_without_exception_text(self):
        output = io.StringIO()
        configs = [config("server_1"), config("server_2")]
        inventory_results = [
            {"serverId": server, "checks": [{"httpStatus": 200, "rowCount": 3}]}
            for server in ("server_1", "server_1", "server_2", "server_2")
        ]
        actual_result = {"serverId": "server_1", "ok": True, "channelCount": 3}
        additional_result = {"serverId": "server_2", "ok": False, "errorCode": "auth_rejected"}
        private_error = "https://panel.example/xmltv.php?username=bob&password=Sensitive-Panel-Password"
        with mock.patch.object(diagnostics.vod, "load_server_config", side_effect=configs), mock.patch.object(
            diagnostics, "inventory_probe", side_effect=inventory_results
        ) as probe, mock.patch.object(
            diagnostics, "actual_inventory_probe", side_effect=[actual_result, additional_result]
        ) as actual, mock.patch.object(
            diagnostics, "redirect_probe", side_effect=requests.ConnectionError(private_error)
        ), redirect_stdout(output):
            self.assertEqual(diagnostics.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["inventory"], inventory_results)
        self.assertEqual(report["actualInventory"], actual_result)
        self.assertEqual(report["additionalInventory"], [additional_result])
        self.assertEqual([call.args[0].server_id for call in actual.call_args_list], ["server_1", "server_2"])
        self.assertEqual(report["redirect"], {"serverId": "server_2", "issue": "request_failed"})
        self.assertEqual(probe.call_args_list[0].args[1], f"SKYTV-Channel-Inventory/{diagnostics.inventory.SYNC_VERSION}")
        self.assertEqual(probe.call_args_list[1].args[1], diagnostics.vod.PROVIDER_HEADERS["User-Agent"])
        self.assertEqual([call.args[0].server_id for call in probe.call_args_list],
                         ["server_1", "server_1", "server_2", "server_2"])
        self.assertEqual(probe.call_args_list[2].args[1], f"SKYTV-Channel-Inventory/{diagnostics.inventory.SYNC_VERSION}")
        self.assertEqual(probe.call_args_list[3].args[1], diagnostics.vod.PROVIDER_HEADERS["User-Agent"])
        self.assertNotIn("Sensitive-Panel-Password", output.getvalue())
        self.assertNotIn(private_error, output.getvalue())


if __name__ == "__main__":
    unittest.main()
