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

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import diagnose_vod_provider_evidence as diagnostic
from vod_provider_evidence import MovieEvidenceCollector


class ProviderEvidenceDiagnosticTests(unittest.TestCase):
    def invoke(self, collector, *, allow_http="false", config_error=None, argv=()):
        stream = io.StringIO()
        config = diagnostic.vod.ServerConfig("server_1", "https://private.invalid", "private-user", "private-password")
        with mock.patch.dict(os.environ, {"ALLOW_INSECURE_PANEL_HTTP": allow_http}), \
                mock.patch.object(diagnostic.vod, "load_server_config", return_value=config,
                                  side_effect=config_error) as loader, \
                mock.patch.object(diagnostic, "MovieEvidenceCollector", return_value=collector) as factory, \
                contextlib.redirect_stdout(stream):
            result = diagnostic.main(list(argv))
        output = stream.getvalue()
        for secret in [config.base_url, config.username, config.password, "SECRET", "817224", "& Sons"]:
            self.assertNotIn(secret, output)
        return result, json.loads(output), loader, factory

    def test_one_real_approved_movie_and_only_server_one_are_used(self):
        collector = mock.Mock(stats={"requests": 1, "lookups": 1, "errors": 0, "errorCounts": {}})
        collector.collect.return_value = {"imdbId": "tt1234567", "cast": ["SECRET"], "directors": ["SECRET"]}
        result, report, loader, factory = self.invoke(collector, allow_http="TRUE")
        self.assertEqual(result, 0)
        loader.assert_called_once_with("server_1", allow_insecure_http=True)
        self.assertEqual(factory.call_args.kwargs["max_requests"], 1)
        self.assertEqual(factory.call_args.kwargs["max_seconds"], 15)
        self.assertEqual(set(factory.call_args.kwargs["configs"]), {"server_1"})
        server, item = collector.collect.call_args.args
        self.assertEqual(server, "server_1")
        self.assertEqual(item["contentId"], "server_1:movie:817224")
        self.assertEqual(item["id"], "817224")
        self.assertEqual(item["title"], "& Sons (2026)")
        self.assertEqual((report["success"], report["hasIMDb"], report["castCount"], report["directorCount"]),
                         (True, True, 1, 1))
        collector.close.assert_called_once()

    def test_optional_failure_and_fixed_error_codes_are_sanitized(self):
        collector = mock.Mock(stats={"requests": 1, "errors": 1, "SECRET": 7,
            "errorCounts": {"http_461": 1, "auth_rejected": 2, "SECRET": 99, "http_999": 3,
                            "transport": "SECRET", "invalid_json": -1, "redirect_refused": 1},
            "errorStages": {"request": 1, "cache": 0, "SECRET": 4}})
        collector.collect.return_value = None
        result, report, _, _ = self.invoke(collector)
        self.assertEqual(result, 0)
        self.assertFalse(report["success"])
        self.assertEqual(report["errorCounts"], {"http_461": 1, "auth_rejected": 2, "transport": 0,
                                               "invalid_json": 0, "redirect_refused": 1})
        self.assertEqual(report["errorStages"], {"request": 1, "cache": 0})
        self.assertNotIn("SECRET", report["stats"])

    def test_config_errors_invalid_http_and_options_never_create_collector(self):
        for options in [{"config_error": diagnostic.vod.CatalogBuildError("SECRET")},
                        {"allow_http": "SECRET"}, {"argv": ["SECRET"]}]:
            with self.subTest(options=list(options)):
                result, report, _, factory = self.invoke(mock.Mock(), **options)
                self.assertEqual(result, 2)
                self.assertFalse(report["success"])
                factory.assert_not_called()

    def test_unexpected_lookup_exception_is_optional_and_never_printed(self):
        collector = mock.Mock(stats={})
        collector.collect.side_effect = RuntimeError("SECRET")
        result, report, _, _ = self.invoke(collector)
        self.assertEqual(result, 0)
        self.assertEqual(report["stats"]["errors"], 1)
        collector.close.assert_called_once()

    def test_refused_same_host_rewrite_reports_constraints_without_following(self):
        config = diagnostic.vod.ServerConfig("server_1", "https://private.invalid", "private-user", "private-password")
        response = mock.Mock(status_code=302, is_redirect=True, is_permanent_redirect=False,
                             headers={"Location": "/private-rewrite.php?username=private-user&password=private-password&action=get_vod_info&vod_id=817224"})
        original = diagnostic.vod._safe_panel_redirect
        with tempfile.TemporaryDirectory() as cache:
            collector = MovieEvidenceCollector(cache, configs={"server_1": config}, max_requests=1)
            with mock.patch("requests.Session.get", return_value=response) as request:
                result, report, _, _ = self.invoke(collector)
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(report["stats"]["requests"], 1)
        self.assertEqual(report["errorCounts"], {"redirect_refused": 1})
        self.assertEqual(report["redirect"]["decision"], "refused")
        self.assertEqual(report["redirect"]["reason"], "path_changed")
        self.assertTrue(report["redirect"]["sameHost"])
        self.assertTrue(report["redirect"]["samePort"])
        self.assertTrue(report["redirect"]["queryEquivalent"])
        self.assertFalse(report["redirect"]["samePath"])
        self.assertNotIn("private-rewrite", json.dumps(report))
        self.assertIs(diagnostic.vod._safe_panel_redirect, original)

    def test_accepted_redirect_is_observed_but_one_http_attempt_is_still_enforced(self):
        config = diagnostic.vod.ServerConfig("server_1", "https://private.invalid", "private-user", "private-password")
        response = mock.Mock(status_code=301, is_redirect=True, is_permanent_redirect=True,
                             headers={"Location": "/player_api.php"})
        original = diagnostic.vod._safe_panel_redirect
        with tempfile.TemporaryDirectory() as cache:
            collector = MovieEvidenceCollector(cache, configs={"server_1": config}, max_requests=1)
            with mock.patch("requests.Session.get", return_value=response) as request:
                result, report, _, _ = self.invoke(collector)
        self.assertEqual(result, 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(report["stats"]["requests"], 1)
        self.assertEqual(report["errorCounts"], {"transport": 1})
        self.assertEqual(report["redirect"]["decision"], "accepted")
        self.assertEqual(report["redirect"]["reason"], "accepted")
        self.assertTrue(report["redirect"]["samePath"])
        self.assertIs(diagnostic.vod._safe_panel_redirect, original)

    def test_redirect_flags_distinguish_origin_scheme_query_and_user_info(self):
        endpoint = "https://private.invalid/player_api.php"
        params = {"username": "private-user", "password": "private-password", "action": "get_vod_info", "vod_id": "817224"}
        cases = (("https://another.invalid/player_api.php", "sameHost", "host_changed"),
                 ("https://private.invalid:8443/player_api.php", "portAllowed", "port_changed"),
                 ("http://private.invalid/player_api.php", "upgradeOrSameScheme", "scheme_changed"),
                 ("/player_api.php?action=get_series", "queryEquivalent", "query_changed"),
                 ("/player_api.php#private-fragment", "hasFragment", "fragment"),
                 ("https://private-user:private-password@private.invalid/player_api.php", "hasUserInfo", "user_info"))
        for location, flag, reason in cases:
            with self.subTest(reason=reason):
                observations = []
                observe = diagnostic._observe_redirect(diagnostic.vod._safe_panel_redirect, observations)
                with self.assertRaises(diagnostic.vod.CatalogBuildError):
                    observe(endpoint, location, params)
                report = diagnostic._report(observations=observations)
                self.assertEqual(report["redirect"]["reason"], reason)
                self.assertEqual(report["redirect"][flag], flag in {"hasFragment", "hasUserInfo"})
                for private in ("private.invalid", "private-user", "private-password", "817224", "player_api.php", "another.invalid"):
                    self.assertNotIn(private, json.dumps(report))
        flags = diagnostic._redirect_constraints("http://private.invalid/player_api.php", "https://private.invalid/player_api.php", params)
        self.assertFalse(flags["samePort"])
        self.assertTrue(flags["portAllowed"])
        self.assertTrue(flags["upgradeOrSameScheme"])

    def test_redirect_observer_is_restored_when_collector_raises(self):
        original = diagnostic.vod._safe_panel_redirect
        collector = mock.Mock(stats={})

        def refuse_then_raise(*_args):
            with self.assertRaises(diagnostic.vod.CatalogBuildError):
                diagnostic.vod._safe_panel_redirect("https://private.invalid/player_api.php", "https://another.invalid/player_api.php", {})
            raise RuntimeError("SECRET")

        collector.collect.side_effect = refuse_then_raise
        result, report, _, _ = self.invoke(collector)
        self.assertEqual(result, 0)
        self.assertEqual(report["redirect"]["reason"], "host_changed")
        self.assertIs(diagnostic.vod._safe_panel_redirect, original)
        collector.close.assert_called_once()

    def test_redirect_report_rejects_extra_values_and_malformed_decision_codes(self):
        report = diagnostic._report(observations=[{"decision": ["SECRET"], "reason": "SECRET",
            "endpoint": "https://private.invalid", "location": "SECRET", "sameHost": "SECRET"}])
        self.assertEqual(report["redirect"]["decision"], "refused")
        self.assertEqual(report["redirect"]["reason"], "unclassified_refusal")
        self.assertFalse(report["redirect"]["sameHost"])
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("private.invalid", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
