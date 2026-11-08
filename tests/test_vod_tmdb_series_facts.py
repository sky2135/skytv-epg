from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vod_tmdb as tmdb
from tests.test_vod_tmdb import FakeResponse, FakeSession


NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc).timestamp()
ITEM = {"tmdbId": "301", "title": "Example Series", "year": 2024}


def details(**changes):
    result = {
        "id": 301, "name": "Example Series", "original_name": "Example Series",
        "first_air_date": "2024-01-01", "last_air_date": "2026-11-08",
        "number_of_seasons": 2, "number_of_episodes": 4,
        "networks": [{"id": 213, "name": "Netflix"}],
        "seasons": [{"season_number": 0, "episode_count": 10},
                    {"season_number": 1, "episode_count": 2},
                    {"season_number": 2, "episode_count": 2}],
    }
    result.update(changes)
    return result


def season(number, *dates, **changes):
    result = {"season_number": number, "episodes": [
        {"season_number": number, "episode_number": index, "show_id": 301, "air_date": date}
        for index, date in enumerate(dates, 1)
    ]}
    result.update(changes)
    return result


class SeriesFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def client(self, *responses, **options):
        return tmdb.TmdbClient(self.root, token="test-private-token", session=FakeSession(*responses), now=options.pop("now", NOW), **options)

    def seed(self, *, legacy_fields=True, item=None):
        item = item or ITEM
        client = self.client(FakeResponse(details()))
        metadata = client.enrich(item, "series")
        self.assertEqual(metadata["matchStatus"], "matched_id")
        key, _identifier, title, year = tmdb._enrichment_identity(item, "series")
        path = client.identity_cache_path(item, "series", key=key, title=title, year=year)
        if legacy_fields:
            entry = json.loads(path.read_bytes())
            entry["data"]["metadata"].pop("originalPlatforms", None)
            entry["data"]["metadata"].pop("airedEpisodeCount", None)
            path.write_text(json.dumps(entry), encoding="utf8")
        return metadata, path

    def test_original_networks_are_canonical_typed_and_not_watch_providers(self):
        networks = [{"id": 213, "name": "NETFLIX"}, {"id": 1024, "name": "Amazon"},
                    {"id": 391, "name": "STARPlus"}, {"id": 77, "name": "Unknown Network"},
                    {"id": 88, "name": "Star+"}, {"id": 2, "name": "https://unsafe.invalid"}]
        result = tmdb._metadata(details(networks=networks), "series", "matched_id")
        self.assertEqual(result["originalPlatforms"], [
            {"name": "Netflix", "type": "streaming_service", "tmdbNetworkId": "213"},
            {"name": "Prime Video", "type": "streaming_service", "tmdbNetworkId": "1024"},
            {"name": "Star Plus", "type": "tv_channel", "tmdbNetworkId": "391"},
            {"name": "Unknown Network", "type": "network", "tmdbNetworkId": "77"},
            {"name": "Star+", "type": "streaming_service", "tmdbNetworkId": "88"},
        ])
        self.assertIsNone(result["airedEpisodeCount"])

    def test_legacy_metadata_migrates_without_invalidating_identity(self):
        self.seed()
        client = self.client()
        result = client.enrich(ITEM, "series")
        self.assertEqual(result["matchStatus"], "matched_id")
        self.assertEqual(result["originalPlatforms"], [])
        self.assertIsNone(result["airedEpisodeCount"])
        self.assertFalse(client.session.calls)

    def test_verified_legacy_cache_backfills_details_and_complete_seasons_no_search(self):
        self.seed()
        client = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", "2026-11-15")))
        result = client.refresh_series_facts(ITEM)
        self.assertEqual(result["airedEpisodeCount"], 3)
        self.assertEqual(result["originalPlatforms"][0]["name"], "Netflix")
        self.assertEqual(result["matchStatus"], "matched_id")
        self.assertEqual(client.last_series_facts_status, "available")
        self.assertEqual([call[0] for call in client.session.calls], [tmdb.API_ROOT + "/tv/301", tmdb.API_ROOT + "/tv/301/season/1", tmdb.API_ROOT + "/tv/301/season/2"])
        self.assertEqual(client.enrich(ITEM, "series"), result)
        self.assertEqual(len(client.session.calls), 3)
        other = {**ITEM, "contentId": "server_3:series:999"}
        self.assertEqual(client.refresh_series_facts(other), result)
        self.assertEqual(client.last_series_facts_status, "cache_hit")
        self.assertEqual(len(client.session.calls), 3)
        offline = self.client()
        self.assertEqual(offline.refresh_series_facts(ITEM), result)
        self.assertEqual(offline.last_series_facts_status, "cache_hit")
        self.assertFalse(offline.session.calls)

    def test_absent_or_unverified_cache_never_makes_a_request(self):
        client = self.client()
        self.assertIsNone(client.refresh_series_facts(ITEM))
        self.assertEqual(client.last_series_facts_status, "unverified")
        self.assertFalse(client.session.calls)
        self.seed()
        self.assertIsNone(client.refresh_series_facts({**ITEM, "title": "Different Series"}))
        self.assertFalse(client.session.calls)

    def test_refreshed_detail_identity_conflicts_keep_verified_baseline(self):
        for changed in [{"id": 999}, {"name": "Wrong Show", "original_name": "Wrong Show"}, {"first_air_date": "2025-01-01"}]:
            with self.subTest(changed=changed):
                self.seed()
                client = self.client(FakeResponse(details(**changed)))
                result = client.refresh_series_facts(ITEM)
                self.assertEqual(result["matchStatus"], "matched_id")
                self.assertEqual(result["originalPlatforms"], [])
                self.assertIsNone(result["airedEpisodeCount"])
                self.assertEqual(client.last_series_facts_status, "failed")
                self.assertEqual(len(client.session.calls), 1)

    def test_budget_or_deadline_defer_season_count_without_losing_match(self):
        self.seed()
        client = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), max_requests=2)
        result = client.refresh_series_facts(ITEM)
        self.assertEqual(result["matchStatus"], "matched_id")
        self.assertIsNone(result["airedEpisodeCount"])
        self.assertEqual(result["originalPlatforms"][0]["name"], "Netflix")
        self.assertEqual(client.last_series_facts_status, "deferred")
        self.assertEqual(client.stats["requests"], 2)
        self.assertEqual(client.refresh_series_facts(ITEM), result)
        self.assertEqual(len(client.session.calls), 2)
        expired = self.client(max_elapsed_seconds=0)
        # The partial cached facts retain the deferred status without wire use.
        self.assertEqual(expired.refresh_series_facts(ITEM)["matchStatus"], "matched_id")
        self.assertFalse(expired.session.calls)
        self.assertEqual(expired.last_series_facts_status, "deferred")

    def test_missing_future_air_dates_are_unknown_not_total_episode_count(self):
        self.seed()
        client = self.client(FakeResponse(details(number_of_episodes=9999)), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", None)))
        result = client.refresh_series_facts(ITEM)
        self.assertIsNone(result["airedEpisodeCount"])
        self.assertEqual(result["episodeCount"], 9999)
        self.assertEqual(client.last_series_facts_status, "available")

    def test_partial_or_wrong_show_season_never_counts_as_complete(self):
        cases = [season(1, "2024-01-01"), season(1, "2024-01-01", "2024-01-02", season_number=2)]
        wrong_show = season(1, "2024-01-01", "2024-01-02")
        wrong_show["episodes"][0]["show_id"] = 999
        cases.append(wrong_show)
        for payload in cases:
            with self.subTest(payload=payload):
                self.seed()
                client = self.client(FakeResponse(details()), FakeResponse(payload))
                self.assertIsNone(client.refresh_series_facts(ITEM)["airedEpisodeCount"])
                self.assertEqual(client.last_series_facts_status, "failed")
                # Isolate each attempt from the independently persisted facts.
                client._cache_path("series:facts-v1:301").unlink(missing_ok=True)

    def test_utc_today_and_new_day_recompute_from_cached_season_dates(self):
        self.seed()
        first = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", "2026-11-09")))
        self.assertEqual(first.refresh_series_facts(ITEM)["airedEpisodeCount"], 3)
        next_day = self.client(FakeResponse(details()), FakeResponse(season(2, "2026-11-08", "2026-11-09")), now=NOW + 86400)
        self.assertEqual(next_day.refresh_series_facts(ITEM)["airedEpisodeCount"], 4)
        self.assertEqual([call[0] for call in next_day.session.calls], [tmdb.API_ROOT + "/tv/301", tmdb.API_ROOT + "/tv/301/season/2"])

    def test_yesterdays_aired_count_is_unknown_without_rematching_or_cache_loss(self):
        self.seed()
        first = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", "2026-11-09")))
        self.assertEqual(first.refresh_series_facts(ITEM)["airedEpisodeCount"], 3)
        tomorrow = self.client(now=NOW + 86400)
        result = tomorrow.enrich(ITEM, "series")
        self.assertEqual(result["matchStatus"], "matched_id")
        self.assertEqual(result["originalPlatforms"][0]["name"], "Netflix")
        self.assertIsNone(result["airedEpisodeCount"])
        self.assertFalse(tomorrow.session.calls)
        # The publication guard doesn't erase yesterday's verified cache fact.
        key, _, title, year = tmdb._enrichment_identity(ITEM, "series")
        stored, _ = tomorrow._read_identity_cache(key, ITEM, "series", title, year)
        self.assertEqual(stored["metadata"]["airedEpisodeCount"], 3)

    def test_current_facts_memo_fills_other_verified_cache_variant(self):
        self.seed()
        client = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", "2026-11-15")))
        client.refresh_series_facts(ITEM)
        # Simulate another corroborated source variant with legacy empty fields.
        legacy_metadata = tmdb._metadata(details(), "series", "matched_id")
        legacy_metadata["originalPlatforms"] = []
        projected = client._series_metadata_with_current_facts(legacy_metadata, ("Example Series", "2024"))
        self.assertEqual(projected["airedEpisodeCount"], 3)
        self.assertEqual(projected["originalPlatforms"][0]["name"], "Netflix")
        self.assertEqual(len(client.session.calls), 3)

    def test_fresh_worker_reads_current_facts_for_other_legacy_identity_key(self):
        first = {**ITEM, "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/first.jpg"}}
        other = {**ITEM, "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/other.jpg"}}
        _metadata, first_path = self.seed(item=first)
        _metadata, other_path = self.seed(item=other)
        self.assertNotEqual(first_path, other_path)
        parent = self.client(FakeResponse(details()), FakeResponse(season(1, "2024-01-01", "2024-01-02")), FakeResponse(season(2, "2026-11-08", "2026-11-15")))
        expected = parent.refresh_series_facts(first)
        # Only the first exact identity key was rewritten by the parent.
        self.assertNotIn("airedEpisodeCount", json.loads(other_path.read_bytes())["data"]["metadata"])
        worker = self.client(max_requests=0)
        with mock.patch.object(worker, "_read_cache", wraps=worker._read_cache) as reader:
            result = worker.enrich(other, "series")
            again = worker.enrich(first, "series")
        self.assertEqual(result["airedEpisodeCount"], expected["airedEpisodeCount"])
        self.assertEqual(result["originalPlatforms"], expected["originalPlatforms"])
        self.assertEqual(again["airedEpisodeCount"], expected["airedEpisodeCount"])
        facts_reads = [call for call in reader.call_args_list if call.args == ("series:facts-v1:301",)]
        self.assertEqual(len(facts_reads), 1)
        self.assertFalse(worker.session.calls)
        unverified = worker.enrich({**other, "title": "Different Series"}, "series")
        self.assertFalse(unverified["matchStatus"].startswith("matched_"))
        self.assertEqual(unverified["originalPlatforms"], [])
        self.assertIsNone(unverified["airedEpisodeCount"])
        self.assertFalse(worker.session.calls)

    def test_restored_new_fields_are_sanitized_not_raw_cache_objects(self):
        metadata = tmdb._metadata(details(), "series", "matched_id")
        metadata["originalPlatforms"] = [{"name": "https://unsafe.invalid", "type": "network", "tmdbNetworkId": "8"}]
        metadata["airedEpisodeCount"] = True
        sanitized = tmdb._cached_metadata(metadata)
        self.assertEqual(sanitized["originalPlatforms"], [])
        self.assertIsNone(sanitized["airedEpisodeCount"])
        metadata["unknownField"] = "not accepted"
        self.assertIsNone(tmdb._cached_metadata(metadata))


if __name__ == "__main__":
    unittest.main()
