from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_vod_catalogs as vod
import publication_snapshots as snapshots
import vod_verification_input as frozen


SOURCE_TIME = "2026-10-22T17:40:00Z"
APP_TIME = "2026-10-22T17:41:42Z"


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class FrozenInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public = self.root / "public"
        self.catalog = self.public / "skytvplayer"
        self.catalog.mkdir(parents=True)

    def artifact(self, stem, payload, *, compressed=False):
        decoded = json_bytes(payload)
        raw = gzip.compress(decoded, mtime=0) if compressed else decoded
        sha = hashlib.sha256(raw).hexdigest()
        name = f"{stem}.{sha[:16]}.json" + (".gz" if compressed else "")
        (self.catalog / name).write_bytes(raw)
        return {"file": name, "sha256": sha, "compressedBytes": len(raw),
                "uncompressedBytes": len(decoded)}

    def fixture(self, *, mutate_app=None, mutate_shard=None, mutate_manifest=None, servers=None):
        sources = []
        for server_id in frozen.SERVERS:
            source = {"get_vod_categories": [{"category_id": "1", "category_name": "English"}],
                "get_series_categories": [{"category_id": "1", "category_name": "Punjabi"}],
                "get_vod_streams": [{"stream_id": 10, "name": "Provider Film (2020)", "tmdb": "321",
                    "stream_icon": "https://image.tmdb.org/t/p/w500/provider.jpg", "category_id": "1"}],
                "get_series": [{"series_id": 20, "name": "Provider Series (2021)", "category_id": "1"}]}
            sources.append((server_id, source, None))
        vod.publish_catalogs(self.public / "VOD", sources, generated_at=SOURCE_TIME)
        app_servers = []
        for server_id in frozen.SERVERS:
            server = {"serverId": server_id, "movieCount": 1, "seriesCount": 1}
            for plural, kind, record_id in (("movies", "movie", "10"), ("series", "series", "20")):
                row = {"contentId": f"{server_id}:{kind}:{record_id}", "id": record_id,
                    "providerStreamId" if kind == "movie" else "providerSeriesId": record_id,
                    "title": "Enriched title", "tmdbId": "999", "metadataMatch": "matched_search",
                    "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/enriched.jpg"},
                    "releaseDate": "2020-01-01", "languages": ["en" if kind == "movie" else "pa"],
                    "providerLastModified": "2026-10-20T00:00:00Z",
                    "episodes": [{"id": "episode-1"}] if kind == "series" else []}
                if mutate_app:
                    mutate_app(server_id, kind, row)
                shard = {"schemaVersion": 1, "serverId": server_id, "dataset": "vod-" + plural,
                         "generatedAt": APP_TIME, "recordCount": 1, "items": [row]}
                if mutate_shard:
                    mutate_shard(server_id, kind, shard)
                shard_ref = self.artifact(server_id + "_" + plural + "_000000", shard, compressed=True)
                shard_ref["recordCount"] = len(shard["items"])
                manifest = {"schemaVersion": 1, "serverId": server_id, "dataset": "vod-" + plural,
                    "generatedAt": APP_TIME, "totalRecords": 1, "shards": [shard_ref]}
                if mutate_manifest:
                    mutate_manifest(server_id, kind, manifest)
                manifest_ref = self.artifact(server_id + "_" + plural + "_manifest", manifest)
                manifest_ref["recordCount"] = len(manifest["shards"])
                server[plural] = manifest_ref
            app_servers.append(server)
        index = {"schemaVersion": 1, "generatedAt": APP_TIME, "serverCount": 3,
                 "servers": app_servers if servers is None else app_servers[:servers]}
        (self.catalog / "index.json").write_bytes(json_bytes(index))
        component = self.root / "component"
        snapshots.package("vod", self.public, component)
        return component

    def test_all_six_catalogs_use_provider_inputs_and_app_baseline_only(self):
        component = self.fixture()
        result = frozen.load_component_input(component)
        self.assertEqual((result["recordCount"], result["movieCount"], result["seriesCount"]), (6, 3, 3))
        movie = next(row for row in result["records"] if row["kind"] == "movie")
        self.assertEqual(movie["item"]["tmdbId"], "321")
        self.assertEqual(movie["baseline"]["tmdbId"], "999")
        self.assertEqual(movie["item"]["title"], "Provider Film (2020)")
        self.assertEqual(movie["item"]["artwork"]["posterUrl"], "https://image.tmdb.org/t/p/w500/provider.jpg")
        self.assertIsNone(movie["item"]["releaseDate"])
        self.assertEqual(movie["item"]["providerLastModified"], "2026-10-20T00:00:00Z")
        self.assertEqual(movie["languages"], ["en"])
        series = next(row for row in result["records"] if row["kind"] == "series")
        self.assertNotIn("episodes", series["item"])
        self.assertNotIn("episodes", series["baseline"])
        self.assertEqual(result["sourceGeneratedAt"], SOURCE_TIME)
        self.assertEqual(result["generatedAt"], APP_TIME)
        self.assertEqual(result["matchingPolicies"]["matchStrategy"], frozen.MATCH_STRATEGY)
        self.assertEqual(len(result["componentSha256"]), 64)

    def test_fingerprint_stable_across_resume_and_changes_for_matching_policy(self):
        component = self.fixture()
        first = frozen.load_component_input(component)["inputFingerprint"]
        self.assertEqual(first, frozen.load_component_input(component)["inputFingerprint"])
        with mock.patch.object(frozen, "MATCH_STRATEGY", frozen.MATCH_STRATEGY + 1):
            self.assertNotEqual(first, frozen.load_component_input(component)["inputFingerprint"])

    def test_reviewed_corrections_applied_to_matching_copy(self):
        component = self.fixture()
        def correction(server_id, kind, item):
            return dict(item, title=item["title"] + " reviewed")
        with mock.patch.object(frozen, "apply_reviewed_correction", side_effect=correction) as apply:
            result = frozen.load_component_input(component)
        self.assertEqual(apply.call_count, 6)
        self.assertTrue(all(row["item"]["title"].endswith(" reviewed") for row in result["records"]))
        self.assertTrue(all(row["baseline"]["title"] == "Enriched title" for row in result["records"]))

    def test_component_rejects_changed_bytes(self):
        component = self.fixture()
        (component / "files/skytvplayer/index.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "checksum or size"):
            frozen.load_component_input(component)

    def test_missing_server_rejected(self):
        component = self.fixture(servers=2)
        with self.assertRaisesRegex(ValueError, "all three servers"):
            frozen.load_component_input(component)

    def test_wrong_content_id_rejected(self):
        component = self.fixture(mutate_app=lambda _s, _k, row: row.update(contentId="server_1:movie:999"))
        with self.assertRaisesRegex(ValueError, "provider identity"):
            frozen.load_component_input(component)

    def test_app_source_id_mismatch_rejected_even_with_equal_counts(self):
        component = self.fixture(mutate_app=lambda _s, _k, row: row.update(id="1234"))
        with self.assertRaisesRegex(ValueError, "differ from provider"):
            frozen.load_component_input(component)

    def test_duplicate_shards_rejected(self):
        component = self.fixture(mutate_manifest=lambda _s, _k, row: row["shards"].append(row["shards"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate shards"):
            frozen.load_component_input(component)

    def test_manifest_count_mismatch_rejected(self):
        component = self.fixture(mutate_manifest=lambda _s, _k, row: row.update(totalRecords=2))
        with self.assertRaisesRegex(ValueError, "manifest counts"):
            frozen.load_component_input(component)

    def test_mixed_generation_rejected(self):
        component = self.fixture(mutate_shard=lambda _s, _k, row: row.update(generatedAt=SOURCE_TIME))
        with self.assertRaisesRegex(ValueError, "generation"):
            frozen.load_component_input(component)

    def test_duplicate_app_ids_rejected(self):
        def duplicate(_s, _k, shard):
            shard["items"].append(dict(shard["items"][0]))
            shard["recordCount"] = 2
        component = self.fixture(mutate_shard=duplicate)
        with self.assertRaisesRegex(ValueError, "duplicated"):
            frozen.load_component_input(component)

    def test_reference_traversal_rejected(self):
        component = self.fixture(mutate_manifest=lambda _s, _k, row: row["shards"][0].update(file="../outside.json"))
        with self.assertRaisesRegex(ValueError, "unsafe"):
            frozen.load_component_input(component)

    def test_reference_decoded_length_enforced(self):
        component = self.fixture(mutate_manifest=lambda _s, _k, row: row["shards"][0].update(uncompressedBytes=1))
        with self.assertRaisesRegex(ValueError, "Decoded.*length"):
            frozen.load_component_input(component)

    def test_source_schema_validation_not_skipped(self):
        component = self.fixture()
        source = component / "files/VOD"
        index = json.loads((source / "index.json").read_bytes())
        index["servers"][0]["movieCount"] = 2
        (source / "index.json").write_bytes(json_bytes(index))
        with self.assertRaisesRegex(ValueError, "counts do not match"):
            frozen.load_frozen_input(component / "files/skytvplayer", source)


if __name__ == "__main__":
    unittest.main()
