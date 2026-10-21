from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(path.read_text())["checkedAt"], NOW)

    def test_first_empty_lookup_is_cached_without_a_refresh_failure(self):
        collector = self.collector()
        with mock.patch.object(vod, "_request_panel_json_once", return_value={}) as request:
            self.assertIsNone(collector.collect("server_1", ITEM))
            self.assertIsNone(collector.collect("server_1", ITEM))
        request.assert_called_once()
        self.assertEqual(collector.stats["errors"], 0)
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

if __name__ == "__main__":
    unittest.main()
