from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_vod_catalogs as vod
from vod_provider_evidence import EVIDENCE_TTL, MovieEvidenceCollector, source_fingerprint


NOW = 1_800_000_000
CONFIG = vod.ServerConfig("server_1", "https://provider.invalid", "sample-user", "sample-password")
ITEM = {"id": "1", "contentId": "server_1:movie:1", "title": "Example Film (2026)",
        "providerLastModified": "2026-10-20T00:00:00Z"}


class ProviderEvidenceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def collector(self, *, now=NOW, max_requests=40):
        result = MovieEvidenceCollector(self.cache, configs={"server_1": CONFIG}, now=now,
                                        max_requests=max_requests)
        self.addCleanup(result.close)
        return result

    def seed(self):
        with mock.patch.object(vod, "_request_panel_json_once", return_value={"info": {"imdb_id": "tt1234567"}}):
            facts = self.collector().collect("server_1", ITEM)
        path = next(self.cache.glob("*.json"))
        return facts, path, path.read_bytes()

    def test_empty_expired_refresh_retains_facts_without_renewing_checked_at(self):
        original, path, before = self.seed()
        collector = self.collector(now=NOW + EVIDENCE_TTL + 1)
        with mock.patch.object(vod, "_request_panel_json_once", return_value={}) as request:
            self.assertEqual(collector.collect("server_1", ITEM), original)
        request.assert_called_once()
        self.assertEqual(collector.stats["requests"], 1)
        self.assertEqual(collector.stats["lookups"], 1)
        self.assertEqual(collector.stats["errors"], 1)
        self.assertEqual(collector.stats["errorCounts"], {"empty_refresh": 1})
        self.assertEqual(collector.stats["errorStages"], {"validate": 1})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(path.read_text())["checkedAt"], NOW)

    def test_first_empty_lookup_is_cached_without_a_refresh_failure(self):
        collector = self.collector()
        with mock.patch.object(vod, "_request_panel_json_once", return_value={}) as request:
            self.assertIsNone(collector.collect("server_1", ITEM))
            self.assertIsNone(collector.collect("server_1", ITEM))
        request.assert_called_once()
        self.assertEqual(collector.stats["errors"], 0)
        self.assertEqual(collector.stats["errorCounts"], {})
        self.assertEqual(collector.stats["cacheHits"], 1)
        value = json.loads(next(self.cache.glob("*.json")).read_text())
        self.assertEqual(value["checkedAt"], NOW)
        self.assertEqual(value["facts"], {"imdbId": None, "directors": [], "cast": [], "runtimeMinutes": None})

    def test_changed_source_cannot_reuse_previous_facts_after_empty_response(self):
        original, path, before = self.seed()
        changed = dict(ITEM, providerLastModified="2026-10-21T00:00:00Z")
        collector = self.collector(now=NOW + EVIDENCE_TTL + 1)
        with mock.patch.object(vod, "_request_panel_json_once", return_value={}):
            self.assertIsNone(collector.collect("server_1", changed))
        self.assertEqual(collector.stats["errors"], 0)
        self.assertIsNone(collector.load(ITEM))
        self.assertNotEqual(path.read_bytes(), before)
        value = json.loads(path.read_text())
        self.assertEqual(value["sourceFingerprint"], source_fingerprint(changed))
        self.assertEqual(value["checkedAt"], NOW + EVIDENCE_TTL + 1)
        self.assertTrue(original)


    def test_lookup_failures_record_only_fixed_error_categories(self):
        cases = ((vod.TransientPanelRequestError("private response", http_status=502), "http_502"),
                 (vod.TransientPanelRequestError("private response"), "transport"),
                 (requests.ConnectionError("https://private.invalid/sample-password"), "transport"),
                 (vod.CatalogBuildError("server_1: get_vod_info provider returned HTTP 404."), "http_404"),
                 (vod.CatalogBuildError("server_1: provider credentials were rejected."), "auth_rejected"),
                 (vod.CatalogBuildError("server_1 provider response is not valid UTF-8 JSON."), "invalid_json"),
                 (vod.CatalogBuildError("server_1: too many provider redirects."), "redirect_refused"),
                 (vod.CatalogBuildError("server_1: unsafe provider redirect refused."), "redirect_refused"),
                 (vod.CatalogBuildError("server_1: provider response is too large."), "response_limit"),
                 (vod.CatalogBuildError("server_1: provider downloads exceed the total size limit."), "response_limit"),
                 (vod.CatalogBuildError("server_1: provider request failed."), "transport"))
        for error, category in cases:
            with self.subTest(category=category):
                collector = self.collector()
                with mock.patch.object(vod, "_request_panel_json_once", side_effect=error):
                    self.assertIsNone(collector.collect("server_1", ITEM))
                self.assertEqual(collector.stats["errors"], 1)
                self.assertEqual(collector.stats["errorCounts"], {category: 1})
                self.assertEqual(collector.stats["errorStages"], {"request": 1})
                raw = json.dumps(collector.stats)
                for private in ("private response", "private.invalid", "sample-password", "HTTP 404"):
                    self.assertNotIn(private, raw)

    def test_untrusted_error_details_or_status_values_cannot_form_category_names(self):
        errors = (ValueError("sample-password https://private.invalid"),
                  vod.TransientPanelRequestError("sample-password", http_status="502 https://private.invalid"),
                  vod.TransientPanelRequestError("sample-password", http_status=True),
                  vod.CatalogBuildError("server_1: get_vod_info provider returned HTTP 404. sample-password"))
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                collector = self.collector()
                with mock.patch.object(vod, "_request_panel_json_once", side_effect=error):
                    self.assertIsNone(collector.collect("server_1", ITEM))
                self.assertEqual(collector.stats["errorCounts"], {"invalid_facts_or_internal": 1})
                self.assertNotIn("sample-password", json.dumps(collector.stats))
                self.assertNotIn("private.invalid", json.dumps(collector.stats))

    def test_cache_io_failure_is_counted_without_exposing_path_or_losing_prior_facts(self):
        original, path, before = self.seed()
        collector = self.collector(now=NOW + EVIDENCE_TTL + 1)
        with mock.patch.object(vod, "_request_panel_json_once", return_value={"info": {"imdb_id": "tt7654321"}}), \
                mock.patch.object(Path, "write_bytes", side_effect=OSError("sample-password/private/cache/path")):
            self.assertEqual(collector.collect("server_1", ITEM), original)
        self.assertEqual(collector.stats["errorCounts"], {"cache_io": 1})
        self.assertEqual(collector.stats["errorStages"], {"cache": 1})
        self.assertEqual(collector.stats["errors"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("sample-password", json.dumps(collector.stats))

    def test_normalization_failure_has_a_fixed_stage_without_response_details(self):
        collector = self.collector()
        with mock.patch.object(vod, "_request_panel_json_once", return_value={}), \
                mock.patch("vod_provider_evidence.normalize_facts", side_effect=TypeError("sample-password/private-response")):
            self.assertIsNone(collector.collect("server_1", ITEM))
        self.assertEqual(collector.stats["errorCounts"], {"invalid_facts_or_internal": 1})
        self.assertEqual(collector.stats["errorStages"], {"normalize": 1})
        self.assertNotIn("sample-password", json.dumps(collector.stats))


if __name__ == "__main__":
    unittest.main()
