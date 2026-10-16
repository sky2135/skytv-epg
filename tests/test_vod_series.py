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
import vod_series as series


NOW = "2026-10-16T12:00:00Z"
CONFIG = vod.ServerConfig("server_1", "https://provider.invalid", "exampleuser", "private-password-427")


def detail(episode_id: str = "900", *, added: str = "1760000000") -> dict:
    return {
        "episodes": {"0": [{
            "id": episode_id, "episode_num": 0, "title": "Special episode",
            "container_extension": "MKV", "added": added,
            "direct_source": "https://provider.invalid/series/exampleuser/private-password-427/900.mkv",
            "info": {"plot": "An episode overview.", "air_date": "2025-01-02", "duration": "01:02:03", "movie_image": "/still.jpg"},
        }]},
    }


def source(*ids: str, modified: str = "100") -> dict:
    return {"get_series": [{"series_id": item_id, "last_modified": modified} for item_id in ids], "series_details": {}}


class EpisodeNormalizationTests(unittest.TestCase):
    def test_specials_metadata_and_no_playback_fields(self) -> None:
        rows = series.normalize_episodes(detail(), CONFIG)
        self.assertEqual(len(rows), 1)
        item = rows[0]
        self.assertEqual(set(item), series.EPISODE_KEYS)
        self.assertEqual((item["seasonNumber"], item["episodeNumber"]), (0, 0))
        self.assertEqual(item["containerExtension"], "mkv")
        self.assertEqual(item["durationSeconds"], 3723)
        self.assertEqual(item["airDate"], "2025-01-02")
        self.assertEqual(item["stillUrl"], "https://image.tmdb.org/t/p/w500/still.jpg")
        self.assertEqual(item["addedAt"], "2025-10-09T08:53:20Z")
        encoded = json.dumps(rows)
        self.assertNotIn("provider.invalid", encoded)
        self.assertNotIn("private-password", encoded)
        self.assertNotIn("direct_source", encoded)

    def test_bad_identity_and_missing_numbers_are_skipped(self) -> None:
        value = {"episodes": [{"id": "../bad", "season": 1, "episode_num": 1}, {"id": "1", "season": True, "episode_num": 1}, {"id": "2", "season": 1}]}
        self.assertEqual(series.normalize_episodes(value), [])

    def test_private_artwork_and_unsafe_extension_are_removed(self) -> None:
        value = detail()
        row = value["episodes"]["0"][0]
        row["container_extension"] = "../mp4"
        row["info"]["movie_image"] = "https://provider.invalid/img.jpg?token=private"
        item = series.normalize_episodes(value, CONFIG)[0]
        self.assertIsNone(item["containerExtension"])
        self.assertIsNone(item["stillUrl"])

    def test_reflected_secret_is_rejected(self) -> None:
        value = detail()
        value["episodes"]["0"][0]["title"] = CONFIG.password
        with self.assertRaises(vod.CatalogBuildError):
            series.normalize_episodes(value, CONFIG)

    def test_conflicting_provider_ids_are_rejected(self) -> None:
        value = detail()
        duplicate = dict(value["episodes"]["0"][0], title="A different title")
        value["episodes"]["0"].append(duplicate)
        with self.assertRaises(vod.CatalogBuildError):
            series.normalize_episodes(value)

    def test_naive_added_time_stays_unknown_and_iso_offset_is_utc(self) -> None:
        self.assertIsNone(series._timestamp("2026-10-16T12:00:00"))
        self.assertEqual(series._timestamp("2026-10-16T08:00:00-04:00"), NOW)


class EpisodeEnrichmentTests(unittest.TestCase):
    def test_offline_details_are_normalized_without_any_request(self) -> None:
        value = source("1", "2")
        value["series_details"]["1"] = detail()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json") as request:
            episodes, stats = series.enrich_series(value, "server_1", Path(folder), config=CONFIG, now=NOW)
        request.assert_not_called()
        self.assertEqual(stats["requests"], 0)
        self.assertEqual(stats["statusBySeries"], {"1": "available", "2": "pending"})
        self.assertEqual(episodes["1"][0]["id"], "900")

    def test_fresh_cache_reused_and_modified_fingerprint_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            self.assertEqual(stats["requests"], 1)
            cached, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now="2026-10-16T13:00:00Z")
            self.assertEqual(cached, episodes)
            self.assertEqual(stats["requests"], 0)
            self.assertEqual(stats["cacheHits"], 1)
            _, stats = series.enrich_series(source("1", modified="101"), "server_1", cache, CONFIG, object(), now="2026-10-16T13:00:00Z")
            self.assertEqual(stats["requests"], 1)
            self.assertEqual(request.call_count, 2)

    def test_failure_keeps_stale_episodes_and_does_not_reset_added_at(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            first, _ = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            request.side_effect = vod.CatalogBuildError("provider request failed")
            stale, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now="2026-10-18T12:00:00Z")
            self.assertEqual(stats["statusBySeries"]["1"], "stale")
            self.assertEqual(first, stale)
            contents = next(cache.rglob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn(CONFIG.password, contents)
            self.assertNotIn(CONFIG.base_url, contents)

    def test_budget_is_bounded_and_next_run_prioritizes_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            _, stats = series.enrich_series(source("1", "2", "3"), "server_1", cache, CONFIG, object(), max_requests=1, now=NOW)
            self.assertEqual(stats["statusBySeries"], {"1": "available", "2": "pending", "3": "pending"})
            self.assertEqual(stats["requests"], 1)
            series.enrich_series(source("1", "2", "3"), "server_1", cache, CONFIG, object(), max_requests=1, now="2026-10-18T12:00:00Z")
            self.assertEqual(request.call_args.kwargs["item_id"], "2")

    def test_negative_attempt_does_not_starve_other_missing_titles(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value={}) as request:
            cache = Path(folder)
            episodes, stats = series.enrich_series(source("1", "2"), "server_1", cache, CONFIG, object(), max_requests=1, now=NOW)
            self.assertEqual(episodes["1"], [])
            self.assertEqual(stats["statusBySeries"]["1"], "unavailable")
            _, stats = series.enrich_series(source("1", "2"), "server_1", cache, CONFIG, object(), max_requests=1, now="2026-10-16T13:00:00Z")
            self.assertEqual(request.call_args.kwargs["item_id"], "2")
            self.assertEqual(stats["requests"], 1)

    def test_expired_cache_is_marked_stale_without_network_session(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            _, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, now="2026-10-18T12:00:00Z")
            self.assertEqual(stats["statusBySeries"]["1"], "stale")
            self.assertEqual(stats["requests"], 0)
            self.assertEqual(request.call_count, 1)

    def test_elapsed_budget_stops_new_requests_and_keeps_all_series_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request, mock.patch.object(series.time, "monotonic", side_effect=[0, 0, 91]):
            episodes, stats = series.enrich_series(source("1", "2"), "server_1", Path(folder), CONFIG, object(), now=NOW)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(stats["requests"], 1)
            self.assertEqual(stats["budgetDeferred"], 1)
            self.assertTrue(stats["deadlineReached"])
            self.assertEqual(stats["statusBySeries"], {"1": "available", "2": "pending"})
            self.assertEqual(set(episodes), {"1", "2"})

    def test_oversized_optional_cache_does_not_discard_fetched_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()), mock.patch.object(series, "MAX_CACHE_BYTES", 32):
            cache = Path(folder)
            episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            self.assertEqual(episodes["1"][0]["id"], "900")
            self.assertEqual(stats["statusBySeries"]["1"], "available")
            self.assertEqual(list(cache.rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
