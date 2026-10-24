from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_skytvplayer_catalog as app
import build_vod_catalogs as vod
import trace_vod_source_provenance as provenance


def sample():
    return {"get_vod_streams": [
        {"stream_id": 1194029, "name": "EN - Kraven the Hunter (2024)", "category_id": "1",
         "tmdb": "539972", "stream_icon": "https://images.example/kraven.jpg?token=very-private-token",
         "direct_source": "https://private-provider.example/movie/private-user/private-password/1194029.mp4"},
        {"stream_id": 1194031, "name": "EN - Smile 2 (2024)", "category_id": "1", "tmdb": None, "stream_icon": ""},
        {"stream_id": 954024, "name": "EN - Indiana Jones (2023) 4K", "category_id": "1", "tmdb": "335977",
         "stream_icon": "https://private-provider.example/image/954024.jpg"}],
        "get_vod_categories": [{"category_id": "1", "category_name": "English"}],
        "get_series": [], "get_series_categories": [], "vod_details": {}, "series_details": {}}


class SourceProvenanceTests(unittest.TestCase):
    def test_trace_preserves_valid_hints_and_reports_absent_source_without_network(self):
        source = sample()
        config = vod.ServerConfig("server_3", "https://private-provider.example", "private-user", "private-password")
        with mock.patch("requests.Session.get", side_effect=AssertionError("network forbidden")):
            report = provenance.trace(source, "server_3", provenance.DEFAULT_IDS, config=config)
        first, absent, control = report["records"]
        self.assertEqual(first["stages"]["normalized"]["tmdbId"], "539972")
        self.assertEqual(first["stages"]["matchingInput"]["tmdbId"], "539972")
        self.assertTrue(first["stages"]["normalized"]["posterPresent"])
        self.assertFalse(absent["tmdb"]["usableCandidatePresent"])
        self.assertIsNone(absent["stages"]["normalized"]["tmdbId"])
        self.assertFalse(absent["poster"]["usableCandidatePresent"])
        self.assertFalse(control["stages"]["normalized"]["posterPresent"])
        self.assertEqual(next(e for e in control["poster"]["candidates"] if e["selected"])["reason"], "provider_hostname_blocked")
        self.assertFalse(report["productionWritten"])
        self.assertEqual(report["tmdbRequests"], 0)
        # Neither raw nor normalized URLs, titles or credential-bearing fields
        # should enter a retained sanitized artifact.
        rendered = json.dumps(report)
        for secret in ("https://", "images.example", "private-provider", "private-user", "private-password",
                       "very-private-token", "Kraven", "direct_source"):
            self.assertNotIn(secret, rendered)

    def test_reports_invalid_first_precedence_masking_a_usable_later_field(self):
        source = sample()
        source["vod_details"] = {"1194029": {"info": {"tmdb_id": "bad-id", "poster_path": "not-a-url"}}}
        report = provenance.trace(source, "server_3", ["1194029"])
        row = report["records"][0]
        self.assertEqual(row["tmdb"]["selectedPath"], "detail.info.tmdb_id")
        self.assertTrue(row["tmdb"]["invalidSelectionMasksValidCandidate"])
        self.assertTrue(row["poster"]["invalidSelectionMasksValidCandidate"])
        self.assertIsNone(row["stages"]["normalized"]["tmdbId"])
        self.assertFalse(row["stages"]["normalized"]["posterPresent"])

    def test_row_hashes_and_input_are_stable_without_mutation(self):
        source = sample()
        before = copy.deepcopy(source)
        first = provenance.trace(source, "server_3", provenance.DEFAULT_IDS)
        second = provenance.trace(source, "server_3", provenance.DEFAULT_IDS)
        self.assertEqual(source, before)
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(first["rawBundleSha256"], second["rawBundleSha256"])

    def test_missing_selected_row_is_explicit_and_conflicting_duplicates_fail(self):
        source = sample()
        row = provenance.trace(source, "server_3", ["123"])["records"][0]
        self.assertFalse(row["inventoryPresent"])
        self.assertEqual(row["stages"]["normalized"], {"present": False})
        duplicate = dict(source["get_vod_streams"][0], tmdb="9999")
        source["get_vod_streams"].append(duplicate)
        with self.assertRaises(vod.CatalogBuildError):
            provenance.trace(source, "server_3", ["1194029"])

    def test_cli_writes_sanitized_report_and_never_dumps_exceptions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = app.json_bytes(sample())
            (root / "server_3_vod_bundle.json").write_bytes(raw)
            target = root / "report.json"
            result = provenance.main(["--input-dir", str(root), "--output", str(target)])
            self.assertEqual(result, 0)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["rawBundleSha256"], __import__("hashlib").sha256(raw).hexdigest())
            self.assertEqual(len(report["records"]), 3)

    def test_invalid_ids_do_not_echo_untrusted_text(self):
        with self.assertRaisesRegex(vod.CatalogBuildError, "one to ten numeric"):
            provenance.trace(sample(), "server_3", ["private-password"])
        with self.assertRaises(vod.CatalogBuildError):
            provenance.trace(sample(), "server_3", range(11))

    def test_matching_input_missing_raw_link_is_visible_without_hiding_inventory(self):
        source = sample()
        source["get_vod_streams"][0]["vod_id"] = source["get_vod_streams"][0].pop("stream_id")
        row = provenance.trace(source, "server_3", ["1194029"])["records"][0]
        self.assertTrue(row["inventoryPresent"])
        self.assertFalse(row["rawMatchingRowPresent"])
        self.assertEqual(row["stages"]["normalized"]["tmdbId"], "539972")

    def test_published_artifacts_are_digest_and_generation_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = "2026-10-24T13:00:21Z"
            source = sample()
            prepared = app._prepare_catalog("server_3", "movie", source)
            rows = [dict(item, tmdbId=None, metadataMatch="unavailable") for item in prepared["normalized"]]
            reference = app.write_dataset(root, "server_3", "movie", rows, now)
            index = {"generatedAt": now, "servers": [{"serverId": "server_3", "movies": reference}]}
            app.atomic_json(root / "index.json", index)
            report = provenance.trace(source, "server_3", ["1194029"], app_catalog_dir=root)
            self.assertEqual(report["publishedGeneration"]["generatedAt"], now)
            facts = report["records"][0]["stages"]
            self.assertEqual(facts["matchingInput"]["tmdbId"], "539972")
            self.assertIsNone(facts["published"]["tmdbId"])
            self.assertEqual(facts["published"]["metadataMatch"], "unavailable")
            index["generatedAt"] = "2026-10-25T13:00:21Z"
            app.atomic_json(root / "index.json", index)
            with self.assertRaisesRegex(vod.CatalogBuildError, "generation"):
                provenance.trace(source, "server_3", ["1194029"], app_catalog_dir=root)
            index["generatedAt"] = now
            app.atomic_json(root / "index.json", index)
            target = root / reference["file"]
            target.write_bytes(target.read_bytes().replace(b"vod-movies", b"bad-movies"))
            with self.assertRaisesRegex(vod.CatalogBuildError, "SHA-256"):
                provenance.trace(source, "server_3", ["1194029"], app_catalog_dir=root)


if __name__ == "__main__":
    unittest.main()
