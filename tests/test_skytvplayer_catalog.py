from __future__ import annotations

import copy
import gzip
import hashlib
import json
import random
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as app  # noqa: E402
import build_vod_catalogs as vod  # noqa: E402
from test_vod_catalogs import sample_source  # noqa: E402


NOW = "2026-10-16T16:30:00Z"
LATER = "2026-10-17T16:30:00Z"


class FakeTmdbClient:
    def __init__(self):
        self.stats = {"requests": 0, "matches": 0}
        self.max_requests = 1500
        self.closed = False

    def enrich(self, item, kind):
        identifier = item.get("tmdbId")
        known = identifier in {"98765", "45678", "104", "202", "204"}
        return {
            "tmdbId": identifier,
            "matchStatus": "matched_id" if known else "unavailable",
            "adult": False if known else None,
            "originalLanguage": "hi" if known else None,
            "genres": ["Drama"] if known else [],
            "popularity": 100 if identifier == "104" else 25 if known else None,
            "posterUrl": "https://image.tmdb.org/t/p/w500/fake.jpg" if known else None,
            "backdropUrl": "https://image.tmdb.org/t/p/w1280/fake.jpg" if known else None,
            "nextEpisode": {"season": 9, "episode": 99, "airDate": LATER[:10]} if kind == "series" and known else None,
        }

    def trending(self, kind):
        return {"104": 1, "98765": 2} if kind == "movie" else {"202": 1, "204": 2, "45678": 3}

    def close(self):
        self.closed = True


def app_source():
    source = sample_source()
    source["get_vod_categories"].append({"category_id": "40", "category_name": "Hindi Movies"})
    source["get_vod_streams"].extend([
        {"stream_id": 103, "name": "Explicit Adult Film", "category_id": "10", "adult": True},
        {"stream_id": 104, "name": "Hindi Film", "category_id": "40", "tmdb_id": 104, "adult": False},
    ])
    source["get_series_categories"].extend([
        {"category_id": "50", "category_name": "English Series"},
        {"category_id": "60", "category_name": "Hindi Series"},
    ])
    source["get_series"].extend([
        {"series_id": 202, "name": "Safe Series", "category_id": "50", "tmdb_id": 202, "adult": False},
        {"series_id": 203, "name": "Unknown Series", "category_id": "50"},
        {"series_id": 204, "name": "Hindi Series", "category_id": "60", "tmdb_id": 204, "adult": False},
    ])
    source["series_details"]["202"] = {
        "episodes": {"1": [{"id": "9001", "episode_num": 1, "title": "Episode One", "container_extension": "mp4"}]}
    }
    return source


def walk_rows(rows):
    for row in rows:
        yield row
        yield from walk_rows(row.get("rows", []))
        yield from walk_rows(row.get("byLanguage", {}).values())


class SkytvPlayerCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "skytvplayer"
        self.cache = self.root / "private-cache"

    def build(self, source=None, *, now=NOW, sources=None, epg_dir=None):
        if sources is None:
            sources = [("server_1", source if source is not None else app_source(), None)]
        return app.build(self.output, self.cache, sources, FakeTmdbClient(), now, epg_dir)

    def catalog(self, entry, plural):
        manifest = app.read_artifact(self.output, entry[plural])
        items = []
        for ref in manifest["shards"]:
            items.extend(app.read_artifact(self.output, ref)["items"])
        return items, manifest

    def home(self, index, server=0):
        return app.read_artifact(self.output, index["servers"][server]["home"])

    def test_three_servers_keep_identity_and_references_separate_with_epg(self):
        epg_dir = self.root / "EPG"
        epg_dir.mkdir()
        sources = []
        for number in range(1, 4):
            server_id = f"server_{number}"
            source = app_source()
            source["get_vod_streams"][0]["name"] = f"Server {number} Film"
            sources.append((server_id, source, None))
            for suffix in ("epg", "epg_v2", "metadata"):
                (epg_dir / f"{server_id}_{suffix}.json.gz").write_bytes(gzip.compress(json.dumps({"serverId": server_id}).encode(), mtime=0))
        index = self.build(sources=sources, epg_dir=epg_dir)
        self.assertEqual(index["serverCount"], 3)
        self.assertEqual(app.validate(self.output, epg_dir=epg_dir), {"serverCount": 3})
        self.assertEqual(index["appName"], "SkyTVPlayer")
        all_ids = set()
        for number, entry in enumerate(index["servers"], 1):
            self.assertEqual(entry["serverId"], f"server_{number}")
            self.assertEqual(entry["epg"]["programme"]["file"], f"../EPG/server_{number}_epg.json.gz")
            self.assertEqual(entry["epg"]["programmeRich"]["file"], f"../EPG/server_{number}_epg_v2.json.gz")
            movies, manifest = self.catalog(entry, "movies")
            series, _ = self.catalog(entry, "series")
            self.assertEqual(movies[0]["title"], f"Server {number} Film")
            self.assertEqual(entry["movieCount"], 4)
            self.assertEqual(entry["seriesCount"], 4)
            for item in movies + series:
                self.assertTrue(item["contentId"].startswith(f"server_{number}:"))
                self.assertNotIn(item["contentId"], all_ids)
                all_ids.add(item["contentId"])
            for ref in [entry["movies"], entry["series"], entry["home"], *manifest["shards"]]:
                self.assertEqual(Path(ref["file"]).name, ref["file"])
                self.assertTrue((self.output / ref["file"]).is_file())
                self.assertEqual(hashlib.sha256((self.output / ref["file"]).read_bytes()).hexdigest(), ref["sha256"])
            for ref in entry["epg"].values():
                path = self.output / ref["file"]
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), ref["sha256"])

    def test_home_has_requested_row_order_and_local_personalization_placeholders(self):
        home = self.home(self.build())
        self.assertEqual([row["id"] for row in home["movieRows"]], [
            "featured", "continueWatching", "trendingInYourLanguages", "newlyAdded", "languages", "browseAll",
        ])
        self.assertEqual([row["id"] for row in home["seriesRows"]], [
            "upNext", "newEpisodes", "newSeries", "trendingTv", "languages", "browseAll",
        ])
        self.assertEqual(home["movieRows"][1]["source"], "local")
        self.assertEqual(home["movieRows"][1]["itemIds"], [])
        self.assertEqual(home["seriesRows"][0]["source"], "local")
        self.assertEqual(home["seriesRows"][0]["itemIds"], [])
        self.assertEqual(home["movieRows"][-1]["dataset"], "vod-movies")
        self.assertEqual(home["seriesRows"][-1]["dataset"], "vod-series")

    def test_discovery_excludes_unknown_and_adult_but_browse_catalogs_keep_them(self):
        index = self.build()
        home = self.home(index)
        movies, _ = self.catalog(index["servers"][0], "movies")
        series, _ = self.catalog(index["servers"][0], "series")
        by_id = {item["contentId"]: item for item in movies + series}
        self.assertIs(by_id["server_1:movie:102"]["adult"], None)
        self.assertIs(by_id["server_1:movie:103"]["adult"], True)
        # A TMDB non-adult classification cannot override the provider's adult category.
        self.assertIs(by_id["server_1:series:201"]["adult"], True)
        self.assertIs(by_id["server_1:series:203"]["adult"], None)
        discovery_ids = {identifier for row in walk_rows(home["movieRows"] + home["seriesRows"]) for identifier in row["itemIds"]}
        self.assertEqual(discovery_ids, {
            "server_1:movie:101", "server_1:movie:104", "server_1:series:202", "server_1:series:204",
        })
        for identifier in discovery_ids:
            self.assertIs(by_id[identifier]["adult"], False)
        self.assertEqual(len(by_id), 8)
        self.assertEqual(home["movieRows"][0]["itemIds"], ["server_1:movie:104", "server_1:movie:101"])
        self.assertEqual(home["movieRows"][2]["itemIds"], ["server_1:movie:104", "server_1:movie:101"])

    def test_language_rows_use_provider_audio_evidence_and_home_previews_omit_episodes(self):
        index = self.build()
        home = self.home(index)
        movie_languages = {row["language"]: row for row in home["movieRows"][4]["rows"]}
        self.assertEqual(movie_languages["en"]["itemIds"], ["server_1:movie:101"])
        self.assertEqual(movie_languages["hi"]["itemIds"], ["server_1:movie:104"])
        movies, _ = self.catalog(index["servers"][0], "movies")
        self.assertEqual(next(item for item in movies if item["id"] == "101")["originalLanguage"], "hi")
        self.assertEqual(home["items"]["server_1:movie:101"]["languages"], ["en"])
        series, _ = self.catalog(index["servers"][0], "series")
        item = next(item for item in series if item["id"] == "202")
        self.assertEqual(item["episodesStatus"], "available")
        self.assertEqual(item["episodes"][0]["id"], "9001")
        self.assertNotIn("episodes", home["items"][item["contentId"]])
        preview = home["items"][item["contentId"]]
        self.assertNotIn("cast", preview)
        for key, value in preview.items():
            self.assertEqual(value, item[key] if key != "overview" else (item[key] or "")[:500] or None)

    def test_initial_inventory_is_baseline_then_new_movies_series_and_episodes_are_detected(self):
        source = app_source()
        first = self.build(source)
        first_home = self.home(first)
        self.assertEqual(first_home["movieRows"][3]["itemIds"], [])
        self.assertEqual(first_home["seriesRows"][1]["itemIds"], [])
        self.assertEqual(first_home["seriesRows"][2]["itemIds"], [])
        # TMDB's future nextEpisode is metadata, never evidence of a provider addition.
        initial_series, _ = self.catalog(first["servers"][0], "series")
        self.assertIsNotNone(next(item for item in initial_series if item["id"] == "202")["nextEpisode"])
        source["get_vod_streams"].append({"stream_id": 105, "name": "New Film", "category_id": "10", "adult": False})
        source["get_series"].append({"series_id": 205, "name": "New Series", "category_id": "50", "adult": False})
        source["series_details"]["202"]["episodes"]["1"].append({"id": "9002", "episode_num": 2, "title": "Episode Two"})
        second = self.build(source, now=LATER)
        second_home = self.home(second)
        self.assertEqual(second_home["movieRows"][3]["itemIds"], ["server_1:movie:105"])
        self.assertEqual(second_home["seriesRows"][1]["itemIds"], ["server_1:series:202"])
        self.assertEqual(second_home["seriesRows"][2]["itemIds"], ["server_1:series:205"])
        movies, _ = self.catalog(second["servers"][0], "movies")
        existing = next(item for item in movies if item["id"] == "101")
        added = next(item for item in movies if item["id"] == "105")
        self.assertEqual(existing["firstSeenAt"], NOW)
        self.assertIsNone(existing["addedAt"])
        self.assertEqual(added["addedAt"], LATER)
        self.assertEqual(added["firstSeenAt"], LATER)
        repeated = self.home(self.build(source, now="2026-10-18T16:30:00Z"))
        self.assertEqual(repeated["items"]["server_1:series:202"]["latestEpisodeAddedAt"], LATER)

    def test_provider_added_dates_are_used_without_claiming_an_unknown_initial_addition(self):
        source = app_source()
        source["get_vod_streams"][0]["added"] = "2026-10-15T12:00:00Z"
        source["series_details"]["202"]["episodes"]["1"][0]["added"] = "2026-10-15T13:00:00Z"
        home = self.home(self.build(source))
        self.assertEqual(home["movieRows"][3]["itemIds"], ["server_1:movie:101"])
        self.assertEqual(home["seriesRows"][1]["itemIds"], ["server_1:series:202"])

    def test_public_artifacts_exclude_playback_urls_provider_secrets_and_query_credentials(self):
        source = app_source()
        source["get_vod_streams"][0]["direct_source"] = "https://provider.invalid/movie/private-account/private-password/101.mp4"
        source["series_details"]["202"]["episodes"]["1"][0]["direct_source"] = "https://provider.invalid/series/private-account/private-password/9001.mp4"
        config = vod.ServerConfig("server_1", "https://provider.invalid", "private-account", "private-password")
        self.build(sources=[("server_1", source, config)])
        raw = b"\n".join(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes() for path in self.output.glob("*.json*"))
        for forbidden in (b"direct_source", b"provider.invalid", b"private-account", b"private-password", b"discard-me", b"account=private"):
            self.assertNotIn(forbidden, raw)
        app.validate(self.output, expected_servers=["server_1"])

    def test_movie_catalog_shards_have_at_most_five_hundred_records_and_home_stays_small(self):
        source = app_source()
        source["get_vod_streams"] = [{"stream_id": number, "name": f"Film {number}", "category_id": "10", "adult": False} for number in range(1, 504)]
        source["vod_details"] = {}
        index = self.build(source)
        items, manifest = self.catalog(index["servers"][0], "movies")
        self.assertEqual(len(items), 503)
        self.assertEqual([ref["recordCount"] for ref in manifest["shards"]], [500, 3])
        self.assertTrue(all(ref["compressedBytes"] <= app.MAX_SHARD_BYTES for ref in manifest["shards"]))
        home = self.home(index)
        self.assertLess(len(home["items"]), 503)
        for row in walk_rows(home["movieRows"] + home["seriesRows"]):
            self.assertLessEqual(len(row["itemIds"]), 40)

    def test_shards_split_by_compressed_bytes_as_well_as_record_count(self):
        index = self.build()
        items, _ = self.catalog(index["servers"][0], "movies")
        items = [copy.deepcopy(items[0]) for _ in range(8)]
        generator = random.Random(42)
        for number, item in enumerate(items):
            item["id"] = str(number)
            item["contentId"] = f"server_1:movie:{number}"
            item["overview"] = "".join(generator.choices(string.ascii_letters + string.digits, k=1500))
        with mock.patch.object(app, "MAX_SHARD_BYTES", 2500):
            ref = app.write_dataset(self.output, "server_1", "movie", items, NOW)
        manifest = app.read_artifact(self.output, ref)
        self.assertGreater(len(manifest["shards"]), 1)
        self.assertEqual(sum(shard["recordCount"] for shard in manifest["shards"]), 8)
        self.assertTrue(all(shard["compressedBytes"] <= 2500 for shard in manifest["shards"]))

    def test_checksum_tampering_is_rejected(self):
        index = self.build()
        _, manifest = self.catalog(index["servers"][0], "movies")
        path = self.output / manifest["shards"][0]["file"]
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        with self.assertRaisesRegex(vod.CatalogBuildError, "SHA-256"):
            app.validate(self.output, expected_servers=["server_1"])

    def test_late_secret_failure_retains_existing_release_index(self):
        self.build()
        previous = (self.output / "index.json").read_bytes()
        source = app_source()
        source["get_vod_streams"][0]["name"] = "Leaked top_secret_metadata_key"
        with mock.patch.dict("os.environ", {"TMDB_API_KEY": "top_secret_metadata_key"}):
            with self.assertRaisesRegex(vod.CatalogBuildError, "metadata secret"):
                self.build(source, now=LATER)
        self.assertEqual((self.output / "index.json").read_bytes(), previous)
        self.assertEqual(app.validate(self.output, expected_servers=["server_1"]), {"serverCount": 1})

    def test_invalid_or_incomplete_server_sets_and_private_cache_in_publication_are_rejected(self):
        self.build()
        with self.assertRaisesRegex(vod.CatalogBuildError, "server set"):
            app.validate(self.output)
        with self.assertRaisesRegex(vod.CatalogBuildError, "Private app caches"):
            app.build(self.output, self.output / "private-cache", [("server_1", app_source(), None)], FakeTmdbClient(), NOW)

    def test_catalog_artifact_path_traversal_is_rejected(self):
        index = self.build()
        index["servers"][0]["movies"]["file"] = "../outside.json"
        with self.assertRaisesRegex(vod.CatalogBuildError, "file name"):
            app.validate(self.output, expected_servers=["server_1"], index=index)


if __name__ == "__main__":
    unittest.main()
