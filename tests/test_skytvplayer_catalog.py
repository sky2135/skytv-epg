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

    def test_discovery_excludes_unknown_adult_and_pending_series_but_browse_keeps_them(self):
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
        self.assertEqual(by_id["server_1:series:204"]["episodesStatus"], "pending")
        self.assertEqual(by_id["server_1:series:204"]["availableEpisodeCount"], 0)
        discovery_ids = {identifier for row in walk_rows(home["movieRows"] + home["seriesRows"]) for identifier in row["itemIds"]}
        self.assertEqual(discovery_ids, {
            "server_1:movie:101", "server_1:movie:104", "server_1:series:202",
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
        self.assertEqual(preview["discoveryAvailability"], {
            "evidence": "provider_episode_inventory", "episodeCount": 1,
        })
        # Current Android readers validate these inventory fields together.
        # A preview must not supply a positive full-inventory count without
        # the omitted episodes or episode-file references.
        for field in ("availableEpisodeCount", "episodes", "episodeFiles"):
            self.assertNotIn(field, preview)
        for key, value in preview.items():
            if key == "discoveryAvailability":
                continue
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
        source["get_series"].append({"series_id": 205, "name": "New Series", "category_id": "50", "adult": False,
                                     "releaseDate": LATER[:10]})
        source["series_details"]["205"] = {
            "episodes": {"1": [{"id": "9501", "episode_num": 1, "title": "Premiere"}]},
        }
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

    def test_new_series_uses_premiere_date_instead_of_provider_additions(self):
        source = app_source()
        premiere = next(row for row in source["get_series"] if row["series_id"] == 202)
        premiere["releaseDate"] = "2026-10-15"
        first = self.build(source)
        first_home = self.home(first)
        row = first_home["seriesRows"][2]
        self.assertEqual(row["itemIds"], ["server_1:series:202"])
        self.assertEqual(row["dateField"], "releaseDate")
        self.assertEqual(row["windowDays"], 30)
        self.assertEqual(row["availabilityEvidence"], "provider_episode_inventory")
        preview = first_home["items"]["server_1:series:202"]
        self.assertIsNone(preview["addedAt"])
        self.assertEqual(preview["firstSeenAt"], NOW)

        # An old provider addition date cannot suppress an actual premiere.
        premiere["added"] = "2017-02-19T12:00:00Z"
        # A post-baseline observation of an old show is not a new series,
        # even when its provider episode inventory is available.
        source["get_series"].append({
            "series_id": 205, "name": "Big Little Lies (2017)", "category_id": "50",
            "adult": False, "releaseDate": "2017-02-19",
        })
        source["series_details"]["205"] = {
            "episodes": {"1": [{"id": "9501", "episode_num": 1, "title": "Episode One"}]},
        }
        second = self.build(source, now=LATER)
        second_home = self.home(second)
        self.assertEqual(second_home["seriesRows"][2]["itemIds"], ["server_1:series:202"])
        records, _ = self.catalog(second["servers"][0], "series")
        old_show = next(item for item in records if item["id"] == "205")
        self.assertEqual(old_show["firstSeenAt"], LATER)
        self.assertEqual(old_show["addedAt"], LATER)
        self.assertEqual(old_show["availableEpisodeCount"], 1)
        self.assertIn(old_show["contentId"], second_home["items"])
        self.assertEqual(second_home["items"]["server_1:series:202"]["addedAt"], "2017-02-19T12:00:00Z")

    def test_new_series_requires_complete_valid_recent_nonfuture_release_dates(self):
        source = app_source()
        candidates = [
            ("301", None), ("302", "2026"), ("303", "2026-10"),
            ("304", "2026-10-17"), ("305", "2026-02-30"),
            ("306", "2026-09-15"), ("307", "2026-09-16"),
            ("308", "2026-10-16"), ("309", "2026-10-01"),
        ]
        for identifier, released in candidates:
            source["get_series"].append({
                "series_id": identifier, "name": f"Series {identifier}",
                "category_id": "50", "adult": False, "releaseDate": released, "added": NOW,
            })
            source["series_details"][identifier] = {
                "episodes": {"1": [{"id": f"9{identifier}", "episode_num": 1, "title": "Premiere"}]},
            }
        home = self.home(self.build(source))
        self.assertEqual(home["seriesRows"][2]["itemIds"], [
            "server_1:series:308", "server_1:series:309", "server_1:series:307",
        ])
        # Invalid or imprecise release dates stay browsable and may appear on
        # other shelves when their provider inventory and safety are known.
        for identifier, _released in candidates:
            self.assertIn(f"server_1:series:{identifier}", home["items"])

    def test_automatic_series_shelves_require_available_or_stale_positive_inventory(self):
        index = self.build()
        records, _ = self.catalog(index["servers"][0], "series")
        template = next(item for item in records if item["id"] == "202")
        cases = [
            ("301", "available", 1), ("302", "stale", 1),
            ("303", "pending", 1), ("304", "unavailable", 1),
            ("305", "available", 0), ("306", "stale", 0),
            ("307", "available", True), ("308", "available", "1"),
            ("309", "available", -1), ("310", "pending", 0),
        ]
        candidates = []
        for identifier, status, count in cases:
            item = copy.deepcopy(template)
            item.update(id=identifier, contentId=f"server_1:series:{identifier}",
                        providerSeriesId=identifier, episodesStatus=status,
                        availableEpisodeCount=count, releaseDate=NOW[:10],
                        addedAt=NOW, latestEpisodeAddedAt=NOW)
            candidates.append(item)
        home = app.make_home("server_1", [], candidates, {"series": {"202": 1}}, NOW)
        eligible = {"server_1:series:301", "server_1:series:302"}
        automatic = [row for row in walk_rows(home["seriesRows"])
                     if row["source"] == "catalog" and row["id"] != "browseAll"]
        self.assertEqual({identifier for row in automatic for identifier in row["itemIds"]}, eligible)
        for row in automatic:
            if row["id"] != "languages":
                self.assertEqual(set(row["itemIds"]), eligible)
        self.assertEqual(set(home["items"]), eligible)
        self.assertEqual(home["seriesRows"][-1]["dataset"], "vod-series")

    def test_sharded_inventory_remains_discoverable_without_adding_inventory_to_home(self):
        source = app_source()
        source["get_series"][1]["releaseDate"] = NOW[:10]
        source["series_details"]["202"]["episodes"]["1"] = [
            {"id": str(9000 + number), "episode_num": number, "title": f"Episode {number}"}
            for number in range(1, 502)
        ]
        index = self.build(source)
        home = self.home(index)
        records, _ = self.catalog(index["servers"][0], "series")
        item = next(item for item in records if item["id"] == "202")
        self.assertEqual(item["episodes"], [])
        self.assertTrue(item["episodeFiles"])
        self.assertEqual(item["availableEpisodeCount"], 501)
        self.assertIn(item["contentId"], home["seriesRows"][2]["itemIds"])
        preview = home["items"][item["contentId"]]
        self.assertEqual(preview["discoveryAvailability"], {
            "evidence": "provider_episode_inventory", "episodeCount": 501,
        })
        for field in ("episodes", "episodeFiles", "availableEpisodeCount"):
            self.assertNotIn(field, preview)
        self.assertEqual(app.validate(self.output, expected_servers=["server_1"]), {"serverCount": 1})

    def test_checksum_correct_home_cannot_reintroduce_pending_or_old_new_series(self):
        index = self.build()
        records, _ = self.catalog(index["servers"][0], "series")
        by_id = {item["id"]: item for item in records}
        for identifier, row_id, message in (
            ("204", "trendingTv", "lacks provider episode inventory"),
            ("202", "newSeries", "lacks a recent premiere date"),
        ):
            with self.subTest(identifier=identifier):
                candidate = copy.deepcopy(index)
                home = self.home(index)
                item = by_id[identifier]
                home["items"][item["contentId"]] = app.home_preview(item)
                row = next(row for row in home["seriesRows"] if row["id"] == row_id)
                row["itemIds"].append(item["contentId"])
                candidate["servers"][0]["home"] = app.write_artifact(
                    self.output, "server_1_home", home, count=len(home["items"]))
                with self.assertRaisesRegex(vod.CatalogBuildError, message):
                    app.validate(self.output, expected_servers=["server_1"], index=candidate)

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

    def test_reviewed_wrong_identity_never_restores_provider_details_when_api_unavailable(self):
        source = {
            "get_vod_categories": [], "get_series_categories": [], "get_series": [],
            "get_vod_streams": [{
                "stream_id": 622578, "name": "AR - Doctor (2021)", "language": "ar",
                "tmdb_id": 284052, "genre": "Fantasy", "releaseDate": "2016-10-25",
                "plot": "Wrong Doctor Strange synopsis", "rating": "8.5", "adult": False,
                "stream_icon": "https://cdn.example/doctor-strange.jpg",
            }],
        }
        index = self.build(sources=[("server_3", source, None)])
        rows, _ = self.catalog(index["servers"][0], "movies")
        item = rows[0]
        self.assertEqual(item["id"], "622578")
        self.assertEqual(item["originalLanguage"], "ta")
        self.assertEqual(item["directors"], ["Nelson Dilipkumar"])
        self.assertEqual(item["languages"], ["ar"])
        self.assertIsNone(item["tmdbId"])
        self.assertIsNone(item["releaseDate"])
        self.assertIsNone(item["overview"])
        self.assertIsNone(item["rating"])
        self.assertIsNone(item["adult"])
        self.assertEqual(item["artwork"], {"posterUrl": None, "backdropUrl": None})
        self.assertEqual(item["genre"], "unknown")
        self.assertEqual(item["metadataMatch"], "reviewed_correction")
        self.assertTrue(item["metadataEvidence"]["sourceURLs"])
        self.assertEqual(app.validate(self.output, expected_servers=["server_3"]), {"serverCount": 1})

    def test_rejected_provider_identity_keeps_only_a_validated_clean_replacement(self):
        source = app_source()
        class Replacement(FakeTmdbClient):
            def enrich(self, item, kind):
                return {
                    "matchStatus": "matched_search", "tmdbId": "777", "rejectedIdentity": True,
                    "originalTitle": "Correct Film", "originalLanguage": "fr", "genres": ["Comedy"],
                    "overview": "Verified replacement synopsis", "releaseDate": "2025-01-02",
                    "posterUrl": "https://image.tmdb.org/t/p/w500/verified.jpg",
                    "adult": False,
                }
        index = app.build(self.output, self.cache, [("server_1", source, None)], Replacement(), NOW)
        rows, _ = self.catalog(index["servers"][0], "movies")
        self.assertEqual(rows[0]["tmdbId"], "777")
        self.assertEqual(rows[0]["overview"], "Verified replacement synopsis")
        self.assertEqual(rows[0]["originalLanguage"], "fr")
        self.assertEqual(rows[0]["languages"], ["en"])
        self.assertEqual(rows[0]["genre"], "comedy")
        self.assertEqual(rows[0]["artwork"]["posterUrl"], "https://image.tmdb.org/t/p/w500/verified.jpg")

    def test_unvalidated_provider_tmdb_id_is_not_used_as_a_recommendation_join(self):
        source = app_source()
        source["get_vod_streams"].append({"stream_id": "919", "name": "Unverified Film", "tmdb_id": "919", "adult": False})
        rows, _ = self.catalog(self.build(source)["servers"][0], "movies")
        item = next(item for item in rows if item["id"] == "919")
        self.assertIsNone(item["tmdbId"])
        item["tmdbId"] = "919"
        with self.assertRaisesRegex(vod.CatalogBuildError, "unverified TMDB"):
            app.validate_metadata_identity("server_1", "movie", item)

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
