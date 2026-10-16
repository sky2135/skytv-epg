from __future__ import annotations

import copy
import gzip
import json
import random
import string
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as app
import build_vod_catalogs as vod
from test_skytvplayer_catalog import FakeTmdbClient, NOW, LATER, app_source


class EpisodeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "public" / "skytvplayer"
        self.cache = self.root / "private"

    def build(self, source=None, *, now=NOW, epg_dir=None):
        return app.build(self.output, self.cache, [("server_1", source or app_source(), None)], FakeTmdbClient(), now, epg_dir)

    def series(self, index, series_id="202"):
        manifest = app.read_artifact(self.output, index["servers"][0]["series"])
        for reference in manifest["shards"]:
            for item in app.read_artifact(self.output, reference)["items"]:
                if item["id"] == series_id:
                    return item
        self.fail("Expected series item missing from publication.")

    def source(self, count, *, rich=False):
        source = app_source()
        generator = random.Random(42)
        episodes = []
        for number in range(count):
            row = {"id": str(900000 + number), "episode_num": number + 1,
                   "title": f"Episode {number + 1}", "container_extension": "mp4"}
            if rich:
                row["info"] = {"plot": "".join(generator.choices(string.ascii_letters + string.digits, k=2000))}
            episodes.append(row)
        source["series_details"]["202"] = {"episodes": {"1": episodes}}
        return source

    def test_ordinary_series_keep_inline_episodes_with_explicit_count(self):
        item = self.series(self.build())
        self.assertEqual(item["availableEpisodeCount"], 1)
        self.assertEqual(item["episodeFiles"], [])
        self.assertEqual(len(item["episodes"]), 1)

    def test_more_than_five_hundred_episodes_are_externalized_without_losing_titles(self):
        index = self.build(self.source(501))
        item = self.series(index)
        self.assertEqual(index["servers"][0]["seriesCount"], 4)
        self.assertEqual(item["episodes"], [])
        self.assertEqual(item["availableEpisodeCount"], 501)
        self.assertEqual([ref["recordCount"] for ref in item["episodeFiles"]], [500, 1])
        all_ids = []
        for reference in item["episodeFiles"]:
            shard = app.read_artifact(self.output, reference)
            self.assertEqual(shard["dataset"], "vod-episodes")
            self.assertEqual(shard["serverId"], "server_1")
            self.assertEqual(shard["providerSeriesId"], "202")
            self.assertEqual(shard["generatedAt"], NOW)
            self.assertLessEqual(reference["compressedBytes"], app.MAX_SHARD_BYTES)
            all_ids.extend(episode["id"] for episode in shard["items"])
        self.assertEqual(len(set(all_ids)), 501)
        self.assertEqual(app.validate(self.output, expected_servers=["server_1"]), {"serverCount": 1})

    def test_large_compressed_episode_metadata_splits_before_it_can_block_the_title_catalog(self):
        source = self.source(400, rich=True)
        self.assertGreater(len(gzip.compress(app.json_bytes(source["series_details"]["202"]))), app.MAX_SHARD_BYTES)
        index = self.build(source)
        item = self.series(index)
        self.assertEqual(item["availableEpisodeCount"], 400)
        self.assertEqual(item["episodes"], [])
        self.assertGreater(len(item["episodeFiles"]), 1)
        self.assertEqual(sum(ref["recordCount"] for ref in item["episodeFiles"]), 400)
        self.assertTrue(all(ref["compressedBytes"] <= app.MAX_SHARD_BYTES for ref in item["episodeFiles"]))
        self.assertLess(len(gzip.compress(app.json_bytes(item))), app.MAX_INLINE_EPISODE_BYTES)
        app.validate(self.output, expected_servers=["server_1"])

    def test_episode_hash_tampering_and_duplicate_ids_are_rejected(self):
        item = self.series(self.build(self.source(501)))
        duplicated = copy.deepcopy(item)
        duplicated["episodeFiles"][1] = duplicated["episodeFiles"][0]
        with self.assertRaisesRegex(vod.CatalogBuildError, "duplicate"):
            app.validate_episode_inventory(self.output, "server_1", duplicated, NOW)
        path = self.output / item["episodeFiles"][0]["file"]
        contents = path.read_bytes()
        path.write_bytes(contents[:-1] + bytes([contents[-1] ^ 1]))
        with self.assertRaisesRegex(vod.CatalogBuildError, "SHA-256"):
            app.validate_episode_inventory(self.output, "server_1", item, NOW)

    def test_episode_identity_and_total_count_must_match_the_series(self):
        item = self.series(self.build(self.source(501)))
        wrong_count = dict(item, availableEpisodeCount=502)
        with self.assertRaisesRegex(vod.CatalogBuildError, "episode count"):
            app.validate_episode_inventory(self.output, "server_1", wrong_count, NOW)
        wrong_series = dict(item, id="another-series")
        with self.assertRaisesRegex(vod.CatalogBuildError, "identity"):
            app.validate_episode_inventory(self.output, "server_1", wrong_series, NOW)
        wrong_inline = dict(item, episodes=[app.read_artifact(self.output, item["episodeFiles"][0])["items"][0]])
        with self.assertRaisesRegex(vod.CatalogBuildError, "inventory"):
            app.validate_episode_inventory(self.output, "server_1", wrong_inline, NOW)

    def test_first_seen_episode_tracking_survives_externalization(self):
        source = self.source(501)
        self.build(source)
        source["series_details"]["202"]["episodes"]["1"].append({"id": "999999", "episode_num": 502, "title": "Latest episode"})
        index = self.build(source, now=LATER)
        item = self.series(index)
        self.assertEqual(item["availableEpisodeCount"], 502)
        self.assertEqual(item["latestEpisodeAddedAt"], LATER)
        home = app.read_artifact(self.output, index["servers"][0]["home"])
        self.assertIn("server_1:series:202", home["seriesRows"][1]["itemIds"])
        self.assertNotIn("episodeFiles", home["items"]["server_1:series:202"])

    def test_hashed_filename_supports_provider_ids_with_colons_and_uppercase(self):
        source = self.source(501)
        source["get_series"][1]["series_id"] = "SERIES:202"
        source["series_details"]["SERIES:202"] = source["series_details"].pop("202")
        item = self.series(self.build(source), "SERIES:202")
        self.assertEqual(item["availableEpisodeCount"], 501)
        for reference in item["episodeFiles"]:
            self.assertNotIn(":", reference["file"])
            self.assertEqual(reference["file"], reference["file"].lower())
        app.validate(self.output, expected_servers=["server_1"])

    def test_existing_epg_json_is_used_when_v2_is_not_published(self):
        epg_dir = self.root / "public" / "EPG"
        epg_dir.mkdir(parents=True)
        for suffix in ("epg", "metadata"):
            (epg_dir / f"server_1_{suffix}.json.gz").write_bytes(gzip.compress(json.dumps({"serverId": "server_1"}).encode(), mtime=0))
        index = self.build(epg_dir=epg_dir)
        references = index["servers"][0]["epg"]
        self.assertEqual(references["programme"]["file"], "../EPG/server_1_epg.json.gz")
        self.assertEqual(app.validate(self.output, epg_dir=epg_dir, expected_servers=["server_1"]), {"serverCount": 1})


if __name__ == "__main__":
    unittest.main()
