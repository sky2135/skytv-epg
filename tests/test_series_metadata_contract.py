"""Publisher integration checks for series edition and inventory facts."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_skytvplayer_catalog as app  # noqa: E402
import build_vod_catalogs as vod  # noqa: E402
from vod_series import normalize_episodes  # noqa: E402


NOW = "2026-11-08T16:28:39Z"
CHECKED = "2026-11-08T15:00:00Z"
NEW_FIELDS = {
    "isMultilingual", "languageListComplete", "languageSource", "originalPlatforms",
    "airedEpisodeCount", "serverEpisodeCount", "episodeInventoryComplete",
    "episodeInventoryCheckedAt",
}
PRIME = {"name": "Prime Video", "type": "streaming_service", "tmdbNetworkId": "1024"}
NETFLIX = {"name": "Netflix", "type": "streaming_service", "tmdbNetworkId": "213"}
STAR_PLUS = {"name": "Star Plus", "type": "tv_channel", "tmdbNetworkId": "159"}


class SeriesClient:
    def __init__(self, metadata):
        self.metadata = metadata

    def enrich(self, item, kind):
        assert kind == "series"
        return copy.deepcopy(self.metadata.get(item["id"], {"matchStatus": "unavailable"}))


def verified(original="hi", platform=PRIME, **extra):
    return {"matchStatus": "matched_search", "tmdbId": "9991001",
            "originalLanguage": original, "originalPlatforms": [copy.deepcopy(platform)],
            "seasonCount": 2, "episodeCount": 10, "airedEpisodeCount": 7, **extra}


class SeriesMetadataContractTests(unittest.TestCase):
    def items(self, rows=None, metadata=None, *, episodes=None, statuses=None, inventory_facts=None):
        rows = rows if rows is not None else [{"series_id": "91001", "name": "Dupahiya", "category_id": "10"}]
        source = {"get_series": copy.deepcopy(rows), "get_series_categories": [
            {"category_id": "10", "category_name": "ENGLISH | AMAZON PRIME"}], "series_details": {}}
        result = app.make_items("server_1", "series", source, {"initialized": False, "items": {}},
                                SeriesClient(metadata if metadata is not None else {"91001": verified()}), NOW,
                                episodes=episodes, statuses=statuses, inventory_facts=inventory_facts)
        # The production writer supplies legacy count/reference fields afterward.
        with tempfile.TemporaryDirectory() as directory:
            app.write_episode_files(Path(directory), "server_1", result, NOW)
        return {item["id"]: item for item in result}

    def test_requested_examples_keep_edition_language_and_original_platform_separate(self):
        titles = {"91001": "Dupahiya", "91002": "Stranger Things",
                  "91003": "Stranger Things (Hindi)", "91004": "Anupamaa"}
        metadata = {"91001": verified(), "91002": verified("en", NETFLIX),
                    "91003": verified("en", NETFLIX), "91004": verified("hi", STAR_PLUS)}
        rows = [{"series_id": identity, "name": title, "category_id": "10"} for identity, title in titles.items()]
        items = self.items(rows, metadata)
        for identity, expected in (("91001", "hi"), ("91002", "en"), ("91003", "hi"), ("91004", "hi")):
            with self.subTest(identity=identity):
                item = items[identity]
                self.assertEqual(item["title"], titles[identity])
                self.assertEqual(item["language"], expected)
                self.assertEqual(item["languages"], [expected])
                self.assertEqual(item["category"]["name"], "ENGLISH | AMAZON PRIME")
                self.assertEqual(item["originalPlatforms"], metadata[identity]["originalPlatforms"])
                self.assertEqual(item["originalLanguage"], metadata[identity]["originalLanguage"])
                self.assertEqual(item["contentId"], f"server_1:series:{identity}")
                self.assertEqual(item["providerSeriesId"], identity)
                self.assertTrue(NEW_FIELDS.issubset(item))
        self.assertEqual(items["91003"]["languageSource"], "title_suffix")
        self.assertEqual(items["91004"]["originalPlatforms"][0]["type"], "tv_channel")
        self.assertNotIn("streamingAvailability", items["91004"])

    def test_multi_edition_keeps_original_without_invented_additional_languages(self):
        item = self.items([{"series_id": "91001", "name": "Dupahiya (Multi Audio)", "category_id": "10"}])["91001"]
        self.assertEqual(item["originalLanguage"], "hi")
        self.assertEqual(item["language"], "hi")
        self.assertEqual(item["languages"], ["hi"])
        self.assertTrue(item["isMultilingual"])
        self.assertFalse(item["languageListComplete"])
        self.assertEqual(item["languageSource"], "provider_multilingual")

    def test_unverified_metadata_does_not_supply_original_platform_or_bare_language(self):
        item = self.items(metadata={"91001": {"matchStatus": "unavailable", "originalLanguage": "hi",
            "originalPlatforms": [PRIME], "airedEpisodeCount": 7}})["91001"]
        self.assertEqual(item["language"], "und")
        self.assertEqual(item["languages"], ["und"])
        self.assertEqual(item["languageSource"], "unknown")
        self.assertEqual(item["originalPlatforms"], [])
        self.assertIsNone(item["airedEpisodeCount"])

    def test_explicit_audio_inventory_overrides_generic_labels_and_suffix(self):
        item = self.items([{"series_id": "91001", "name": "Dupahiya (Hindi)", "category_id": "10",
                            "audio_languages": ["ta"], "language": "en"}])["91001"]
        self.assertEqual(item["languages"], ["ta"])
        self.assertEqual(item["languageSource"], "provider_audio")

    def test_pending_inventory_is_unknown_not_confirmed_zero(self):
        item = self.items()["91001"]
        self.assertEqual(item["episodeCount"], 10)
        self.assertEqual(item["airedEpisodeCount"], 7)
        self.assertEqual(item["availableEpisodeCount"], 0)
        self.assertEqual(item["episodesStatus"], "pending")
        self.assertIsNone(item["serverEpisodeCount"])
        self.assertFalse(item["episodeInventoryComplete"])
        self.assertIsNone(item["episodeInventoryCheckedAt"])
        app.validate_series_metadata(item)

    def test_quality_copies_do_not_inflate_server_logical_episode_count(self):
        episodes = normalize_episodes({"episodes": {"1": [
            {"id": "ep1hd", "episode_num": 1, "title": "Episode 1 HD", "container_extension": "mkv"},
            {"id": "ep1sd", "episode_num": 1, "title": "Episode 1 SD", "container_extension": "mp4"},
        ]}})
        facts = {"serverEpisodeCount": 1, "episodeInventoryComplete": True, "episodeInventoryCheckedAt": CHECKED}
        item = self.items(episodes={"91001": episodes}, statuses={"91001": "available"},
                          inventory_facts={"91001": facts})["91001"]
        self.assertEqual(item["serverEpisodeCount"], 1)
        self.assertEqual(item["availableEpisodeCount"], 2)
        self.assertEqual(len(item["episodes"]), 2)
        self.assertTrue(item["episodeInventoryComplete"])
        self.assertEqual(item["episodeInventoryCheckedAt"], CHECKED)
        self.assertEqual(item["episodeCount"], 10)
        self.assertEqual(item["airedEpisodeCount"], 7)
        app.validate_series_metadata(item)
        with tempfile.TemporaryDirectory() as directory:
            app.validate_episode_inventory(Path(directory), "server_1", item, NOW)

    def test_explicit_confirmed_empty_inventory_can_publish_zero(self):
        facts = {"serverEpisodeCount": 0, "episodeInventoryComplete": True, "episodeInventoryCheckedAt": CHECKED}
        item = self.items(episodes={"91001": []}, statuses={"91001": "available"},
                          inventory_facts={"91001": facts})["91001"]
        self.assertEqual(item["serverEpisodeCount"], 0)
        self.assertTrue(item["episodeInventoryComplete"])
        app.validate_series_metadata(item)

    def test_stale_cached_count_is_preserved_as_incomplete(self):
        episodes = normalize_episodes({"episodes": {"1": [{"id": "ep1", "episode_num": 1}]}})
        facts = {"serverEpisodeCount": 1, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": CHECKED}
        item = self.items(episodes={"91001": episodes}, statuses={"91001": "stale"},
                          inventory_facts={"91001": facts})["91001"]
        self.assertEqual(item["serverEpisodeCount"], 1)
        self.assertFalse(item["episodeInventoryComplete"])
        self.assertEqual(item["episodeInventoryCheckedAt"], CHECKED)
        app.validate_series_metadata(item)

    def test_legacy_cached_episodes_do_not_invent_complete_inventory_facts(self):
        episodes = normalize_episodes({"episodes": {"1": [{"id": "ep1", "episode_num": 1}]}})
        item = self.items(episodes={"91001": episodes}, statuses={"91001": "stale"})["91001"]
        self.assertEqual(item["availableEpisodeCount"], 1)
        self.assertIsNone(item["serverEpisodeCount"])
        self.assertFalse(item["episodeInventoryComplete"])
        self.assertIsNone(item["episodeInventoryCheckedAt"])

    def test_expired_next_episode_is_cleared_but_future_detail_is_preserved(self):
        stale = {"season": 1, "episode": 8, "airDate": "2026-11-05"}
        future = {"season": 1, "episode": 8, "airDate": "2026-11-09"}
        self.assertIsNone(self.items(metadata={"91001": verified(nextEpisode=stale)})["91001"]["nextEpisode"])
        item = self.items(metadata={"91001": verified(nextEpisode=future)})["91001"]
        self.assertEqual(item["nextEpisode"], future)
        self.assertIsNone(item["serverEpisodeCount"])

    def test_home_preview_retains_new_facts_without_full_inventory_fields(self):
        item = self.items()["91001"]
        preview = app.home_preview(item)
        for key in NEW_FIELDS | {"originalLanguage", "seasonCount", "episodeCount"}:
            with self.subTest(key=key):
                self.assertIn(key, preview)
                self.assertEqual(preview[key], item[key])
        for key in ("availableEpisodeCount", "episodes", "episodeFiles"):
            self.assertNotIn(key, preview)

    def test_legacy_records_without_all_new_fields_still_validate(self):
        item = self.items()["91001"]
        for key in NEW_FIELDS:
            item.pop(key)
        app.validate_series_metadata(item)
        item["isMultilingual"] = False
        with self.assertRaises(vod.CatalogBuildError):
            app.validate_series_metadata(item)

    def test_new_metadata_types_and_unknown_count_constraints_are_checked(self):
        item = self.items()["91001"]
        invalid = (
            {"isMultilingual": "false"}, {"languageListComplete": 1}, {"languageSource": "category"},
            {"originalPlatforms": "Prime Video"}, {"airedEpisodeCount": True}, {"airedEpisodeCount": -1},
            {"serverEpisodeCount": True}, {"serverEpisodeCount": -1}, {"episodeInventoryComplete": "true"},
            {"episodeInventoryCheckedAt": "yesterday"}, {"episodeInventoryComplete": True},
        )
        for change in invalid:
            with self.subTest(change=change):
                candidate = copy.deepcopy(item)
                candidate.update(change)
                with self.assertRaises(vod.CatalogBuildError):
                    app.validate_series_metadata(candidate)

    def test_facts_build_freezes_followup_tmdb_budget_and_publishes_only_public_inventory_summary(self):
        rows = [
            {"series_id": "91001", "name": "Dupahiya", "category_id": "10", "adult": False},
            {"series_id": "91002", "name": "Stranger Things", "category_id": "10", "adult": False},
            {"series_id": "91004", "name": "Anupamaa", "category_id": "10", "adult": False},
            {"series_id": "91099", "name": "Background Show", "category_id": "10", "adult": False},
        ]
        source = {"get_series": rows, "get_series_categories": [
            {"category_id": "10", "category_name": "ENGLISH | AMAZON PRIME"}],
            "get_vod_streams": [], "get_vod_categories": [], "vod_details": {}, "series_details": {}}
        observations = []
        test = self

        class BuildClient(SeriesClient):
            def __init__(self):
                super().__init__({row["series_id"]: verified(tmdbId=str(9991000 + index))
                                  for index, row in enumerate(rows, 1)})
                self.max_requests = 50
                self.stats = {"requests": 2}

            def trending(self, kind):
                observations.append(("trending", kind, self.max_requests))
                test.assertEqual(self.max_requests, self.stats["requests"])
                return {}

            def enrichment_priority(self, item, kind):
                observations.append(("priority", kind, self.max_requests))
                test.assertEqual(self.max_requests, 5)
                return 0

            def enrich(self, item, kind):
                observations.append(("enrich", kind, self.max_requests))
                test.assertEqual(self.max_requests, self.stats["requests"])
                test.assertEqual(self.max_requests, 5)
                return super().enrich(item, kind)

        client = BuildClient()
        priority_ids = ["91001", "91002", "91004"]
        episodes = {identity: normalize_episodes({"episodes": {"1": [
            {"id": f"{identity}-ep1", "episode_num": 1, "container_extension": "mp4"}]}})
            for identity in priority_ids}
        facts = {identity: {"serverEpisodeCount": 1, "episodeInventoryComplete": True,
                            "episodeInventoryCheckedAt": CHECKED} for identity in priority_ids}
        statuses = {identity: "available" if identity in priority_ids else "pending"
                    for identity in (row["series_id"] for row in rows)}
        provider_stats = {"requests": 3, "cacheHits": 0, "statusBySeries": statuses,
                          "attemptedSeriesIds": priority_ids, "inventoryBySeries": facts}
        summary = {"processedCount": 3, "wireAttempts": 3}

        def refresh_facts(candidates, actual_client, cache, now, *, limit):
            self.assertIs(actual_client, client)
            self.assertEqual(actual_client.max_requests, 50)
            self.assertEqual(now, NOW)
            self.assertEqual(limit, 3)
            self.assertEqual({item["id"] for item in candidates}, {row["series_id"] for row in rows})
            actual_client.stats["requests"] += 3
            observations.append(("facts", "series", actual_client.max_requests))
            return summary

        with tempfile.TemporaryDirectory() as directory:
            output, cache = Path(directory) / "public", Path(directory) / "private-cache"
            with mock.patch("vod_series_facts.backfill_series_facts", side_effect=refresh_facts) as facts_call, \
                    mock.patch.object(app, "enrich_series", return_value=(episodes, provider_stats)) as detail_call:
                index = app.build(output, cache, [("server_1", source, None)], client, NOW,
                                  fetch_episodes=True, max_episode_requests=3, series_facts_limit=3)
            facts_call.assert_called_once()
            detail_call.assert_called_once()
            self.assertEqual(detail_call.call_args.kwargs["priority_series_ids"], priority_ids)
            self.assertEqual(detail_call.call_args.kwargs["max_requests"], 3)
            self.assertEqual(client.max_requests, 5)
            self.assertEqual(client.stats["requests"], 5)
            self.assertEqual([entry[0] for entry in observations[:3]], ["trending", "trending", "facts"])
            self.assertTrue(any(entry[0] == "enrich" for entry in observations))
            self.assertEqual(index["schemaVersion"], 1)
            self.assertEqual(index["seriesMetadataVersion"], 2)
            self.assertEqual(index["seriesFactRefresh"], summary)
            server = index["servers"][0]
            manifest = app.read_artifact(output, server["series"])
            self.assertEqual(manifest["schemaVersion"], 1)
            self.assertEqual(manifest["seriesMetadataVersion"], 2)
            self.assertEqual(manifest["totalRecords"], 4)
            self.assertEqual(server["episodeRefresh"], {"requests": 3, "cacheHits": 0,
                "statusCounts": {"available": 3, "stale": 0, "pending": 1, "unavailable": 0}})
            self.assertNotIn("inventoryBySeries", server["episodeRefresh"])
            self.assertEqual(app.validate(output, expected_servers=["server_1"]), {"serverCount": 1})


if __name__ == "__main__":
    unittest.main()
