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


def numbered_detail(*episodes: tuple[str, int, int]) -> dict:
    return {"episodes": [{"id": identifier, "season": season_number, "episode_num": episode_number}
                         for identifier, season_number, episode_number in episodes]}


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

    def test_supplement_preserves_oversized_success_and_processes_untouched_ids(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(series, "MAX_CACHE_BYTES", 32):
            cache = Path(folder)
            value = source("1", "2", "3")
            with mock.patch.object(vod, "request_panel_json", return_value=detail("900")) as request:
                first = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=1, now=NOW)
            self.assertEqual(first[1]["attemptedSeriesIds"], ["1"])
            self.assertEqual(list(cache.rglob("*.json")), [])

            def supplement(_session, _config, _action, *, item_id, **_kwargs):
                if item_id == "1":
                    raise AssertionError("A successful first-pass series must not be fetched again")
                if item_id == "2":
                    raise vod.CatalogBuildError("safe supplemental provider failure")
                return detail("902")

            with mock.patch.object(vod, "request_panel_json", side_effect=supplement) as request:
                episodes, stats = series.enrich_series(value, "server_1", cache, CONFIG, object(),
                    max_requests=2, now=NOW, previous=first)
            self.assertEqual([call.kwargs["item_id"] for call in request.call_args_list], ["2", "3"])
            self.assertEqual(episodes["1"], first[0]["1"])
            self.assertEqual(episodes["3"][0]["id"], "902")
            self.assertEqual(stats["statusBySeries"], {"1": "available", "2": "unavailable", "3": "available"})
            self.assertEqual(stats["attemptedSeriesIds"], ["2", "3"])
            self.assertEqual(stats["requests"], 2)
            self.assertEqual(stats["budgetDeferred"], 0)

    def test_supplement_does_not_retry_an_uncached_first_pass_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(series, "MAX_CACHE_BYTES", 32):
            cache = Path(folder)
            value = source("1", "2")
            with mock.patch.object(vod, "request_panel_json", side_effect=vod.CatalogBuildError("safe failure")):
                first = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=1, now=NOW)
            with mock.patch.object(vod, "request_panel_json", return_value=detail("901")) as request:
                episodes, stats = series.enrich_series(value, "server_1", cache, CONFIG, object(),
                    max_requests=1, now=NOW, previous=first)
            self.assertEqual(request.call_args.kwargs["item_id"], "2")
            self.assertEqual(stats["attemptedSeriesIds"], ["2"])
            self.assertEqual(stats["statusBySeries"], {"1": "unavailable", "2": "available"})
            self.assertEqual(episodes["1"], [])

    def test_supplement_keeps_unattempted_stale_and_pending_series_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            normalized = series.normalize_episodes(detail("900"), CONFIG)
            previous = ({"1": normalized, "2": normalized, "3": []},
                        {"statusBySeries": {"1": "stale", "2": "available", "3": "pending"},
                         "attemptedSeriesIds": []})
            with mock.patch.object(vod, "request_panel_json", return_value=detail("901")) as request:
                episodes, stats = series.enrich_series(source("1", "2", "3"), "server_1", cache, CONFIG,
                    object(), max_requests=2, now=NOW, previous=previous)
            self.assertEqual([call.kwargs["item_id"] for call in request.call_args_list], ["1", "3"])
            self.assertEqual(episodes["2"], normalized)
            self.assertEqual(stats["attemptedSeriesIds"], ["1", "3"])
            self.assertEqual(stats["statusBySeries"], {"1": "available", "2": "available", "3": "available"})

    def test_previous_inventories_are_revalidated_and_foreign_ids_cannot_enter_output(self) -> None:
        bad = series.normalize_episodes(detail("900"), CONFIG)
        bad[0]["title"] = CONFIG.password
        foreign = series.normalize_episodes(detail("999"), CONFIG)
        previous = ({"1": bad, "999": foreign},
                    {"statusBySeries": {"1": "available", "999": "available"},
                     "attemptedSeriesIds": ["999", "1", "2"]})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json") as request:
            episodes, stats = series.enrich_series(source("1"), "server_1", Path(folder), CONFIG,
                now=NOW, previous=previous)
        request.assert_not_called()
        self.assertEqual(episodes, {"1": []})
        self.assertEqual(stats["statusBySeries"], {"1": "pending"})
        self.assertEqual(stats["attemptedSeriesIds"], [])
        self.assertNotIn(CONFIG.password, json.dumps((episodes, stats)))


class EpisodeInventoryFactsTests(unittest.TestCase):
    def test_complete_inventory_counts_episode_identity_not_quality_files(self) -> None:
        value = source("1", "2")
        value["series_details"]["1"] = numbered_detail(("900", 0, 0), ("901", 1, 1), ("902", 1, 1))
        with tempfile.TemporaryDirectory() as folder:
            episodes, stats = series.enrich_series(value, "server_1", Path(folder), CONFIG, now=NOW)
        self.assertEqual(len(episodes["1"]), 3)
        self.assertEqual(stats["inventoryBySeries"], {
            "1": {"serverEpisodeCount": 2, "episodeInventoryComplete": True, "episodeInventoryCheckedAt": NOW},
            "2": {"serverEpisodeCount": None, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": None},
        })

    def test_explicit_valid_empty_mapping_list_and_seasons_are_complete_zero(self) -> None:
        for payload in ({}, [], {"1": [], "0": []}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(vod, "request_panel_json", return_value={"episodes": payload}) as request:
                cache = Path(folder)
                episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
                self.assertEqual(episodes, {"1": []})
                self.assertEqual(stats["statusBySeries"], {"1": "unavailable"})
                self.assertEqual(stats["inventoryBySeries"]["1"], {
                    "serverEpisodeCount": 0, "episodeInventoryComplete": True, "episodeInventoryCheckedAt": NOW})
                _, reused = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now="2026-10-16T13:00:00Z")
                self.assertEqual(request.call_count, 1)
                self.assertEqual(reused["inventoryBySeries"], stats["inventoryBySeries"])

    def test_malformed_authentication_and_error_envelopes_are_unknown_not_zero(self) -> None:
        invalid = (
            {}, [], None, {"episodes": None}, {"episodes": "private malformed"},
            {"episodes": {"1": {}}}, {"episodes": {}, "error": "private error"},
            {"episodes": [], "errors": ["private error"]},
            {"episodes": [], "user_info": {"auth": 0}},
            {"episodes": [], "user_info": {"auth": 1, "status": "Expired"}},
            {"episodes": {}, "success": "FALSE"}, {"episodes": {}, "status": False},
            {"episodes": {}, "message": "private authentication failure"},
            {"episodes": {}, "info": []}, {"episodes": {}, "seasons": "private-invalid"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(vod, "request_panel_json", return_value=payload):
                _, stats = series.enrich_series(source("1"), "server_1", Path(folder), CONFIG, object(), now=NOW)
            self.assertEqual(stats["statusBySeries"]["1"], "unavailable")
            self.assertEqual(stats["inventoryBySeries"]["1"], {
                "serverEpisodeCount": None, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": None})
            self.assertNotIn("private", json.dumps(stats))

    def test_partial_response_keeps_valid_rows_but_never_certifies_partial_count(self) -> None:
        value = numbered_detail(("900", 1, 1))
        value["episodes"].extend([{"id": "901", "season": 1}, "invalid row"])
        value["episodes"] = {"1": value["episodes"], "2": {"invalid": "season rows"}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=value):
            episodes, stats = series.enrich_series(source("1"), "server_1", Path(folder), CONFIG, object(), now=NOW)
        self.assertEqual([row["id"] for row in episodes["1"]], ["900"])
        self.assertEqual(stats["statusBySeries"]["1"], "available")
        self.assertIsNone(stats["inventoryBySeries"]["1"]["serverEpisodeCount"])
        self.assertFalse(stats["inventoryBySeries"]["1"]["episodeInventoryComplete"])
        self.assertIsNone(stats["inventoryBySeries"]["1"]["episodeInventoryCheckedAt"])

    def test_stale_failure_and_partial_refresh_keep_episodes_and_last_verified_facts(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json") as request:
            cache = Path(folder)
            request.return_value = numbered_detail(("900", 1, 1), ("901", 1, 2))
            first, initial = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            request.side_effect = vod.CatalogBuildError("safe provider failure")
            stale, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now="2026-10-18T12:00:00Z")
            self.assertEqual(stale, first)
            historical = {"serverEpisodeCount": 2, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": NOW}
            self.assertEqual(stats["inventoryBySeries"]["1"], historical)
            self.assertEqual(stats["statusBySeries"]["1"], "stale")
            request.side_effect = None
            partial = numbered_detail(("902", 1, 3))
            partial["episodes"].append({"id": "903", "episode_num": 4})
            request.return_value = partial
            combined, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(),
                now="2026-10-19T12:00:00Z", priority_series_ids=("1",))
            self.assertEqual([item["id"] for item in combined["1"]], ["900", "901", "902"])
            self.assertEqual(stats["inventoryBySeries"]["1"], historical)
            cached = json.loads(next(cache.rglob("*.json")).read_bytes())
            self.assertEqual(cached["schemaVersion"], 2)
            self.assertEqual(cached["episodeInventoryCheckedAt"], NOW)
            self.assertEqual(cached["fetchedAt"], "2026-10-19T12:00:00Z")
            self.assertEqual(initial["inventoryBySeries"]["1"]["serverEpisodeCount"], 2)

    def test_expired_inventory_without_network_keeps_historical_count_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()):
            cache = Path(folder)
            series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            _, stats = series.enrich_series(source("1", "2"), "server_1", cache, CONFIG, now="2026-10-18T12:00:00Z")
        self.assertEqual(stats["inventoryBySeries"]["1"], {
            "serverEpisodeCount": 1, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": NOW})
        self.assertIsNone(stats["inventoryBySeries"]["2"]["serverEpisodeCount"])

    def test_v1_cache_migrates_conservatively_and_priority_rechecks_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            first, _ = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            path = next(cache.rglob("*.json"))
            value = json.loads(path.read_bytes())
            value = {key: item for key, item in value.items() if key in series.LEGACY_CACHE_KEYS}
            value["schemaVersion"] = 1
            path.write_text(json.dumps(value), encoding="utf-8")
            migrated, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now="2026-10-16T13:00:00Z")
            self.assertEqual(migrated, first)
            self.assertEqual(request.call_count, 1)
            self.assertIsNone(stats["inventoryBySeries"]["1"]["serverEpisodeCount"])
            self.assertFalse(stats["inventoryBySeries"]["1"]["episodeInventoryComplete"])
            _, verified = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(),
                now="2026-10-16T14:00:00Z", priority_series_ids=["1"])
            self.assertEqual(request.call_count, 2)
            self.assertTrue(verified["inventoryBySeries"]["1"]["episodeInventoryComplete"])
            self.assertEqual(json.loads(path.read_bytes())["schemaVersion"], 2)

    def test_changed_and_explicit_priorities_precede_normal_missing_titles(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()) as request:
            cache = Path(folder)
            series.enrich_series(source("2", "3"), "server_1", cache, CONFIG, object(), now=NOW)
            request.reset_mock()
            value = source("1", "2", "3")
            value["get_series"][1]["last_modified"] = "101"
            _, stats = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=2,
                now="2026-10-16T13:00:00Z", priority_series_ids=("3",))
            self.assertEqual([call.kwargs["item_id"] for call in request.call_args_list], ["3", "2"])
            self.assertEqual(stats["statusBySeries"]["1"], "pending")
            self.assertEqual(stats["budgetDeferred"], 1)
        for invalid in ("private-identifier", tuple(str(index) for index in range(1001))):
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(vod.CatalogBuildError):
                series.enrich_series(source("1"), "server_1", Path(folder), now=NOW, priority_series_ids=invalid)

    def test_supplement_preserves_uncached_inventory_facts_and_stale_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(series, "MAX_CACHE_BYTES", 32):
            cache = Path(folder)
            value = source("1", "2")
            with mock.patch.object(vod, "request_panel_json", return_value=detail()):
                first = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=1, now=NOW)
            with mock.patch.object(vod, "request_panel_json", side_effect=vod.CatalogBuildError("safe failure")):
                second = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=1, now=NOW, previous=first)
            self.assertEqual(second[1]["inventoryBySeries"]["1"], first[1]["inventoryBySeries"]["1"])
            stale_previous = (first[0], {**first[1], "statusBySeries": {"1": "stale", "2": "pending"}, "attemptedSeriesIds": []})
            with mock.patch.object(vod, "request_panel_json", side_effect=vod.CatalogBuildError("safe failure")):
                episodes, stats = series.enrich_series(value, "server_1", cache, CONFIG, object(), max_requests=1,
                    now="2026-10-18T12:00:00Z", previous=stale_previous)
            self.assertEqual(episodes["1"], first[0]["1"])
            self.assertEqual(stats["inventoryBySeries"]["1"], {
                "serverEpisodeCount": 1, "episodeInventoryComplete": False, "episodeInventoryCheckedAt": NOW})

    def test_partial_refresh_does_not_expand_the_episode_bound(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(series, "MAX_EPISODES_PER_SERIES", 2), \
                mock.patch.object(vod, "request_panel_json") as request:
            cache = Path(folder)
            request.return_value = numbered_detail(("900", 1, 1), ("901", 1, 2))
            first, _ = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            request.return_value = {"episodes": {"1": [{"id": "902", "episode_num": 3}], "2": "invalid"}}
            episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(),
                now="2026-10-18T12:00:00Z")
            self.assertEqual(episodes, first)
            self.assertEqual(stats["inventoryBySeries"]["1"]["serverEpisodeCount"], 2)
            self.assertFalse(stats["inventoryBySeries"]["1"]["episodeInventoryComplete"])

    def test_cache_and_supplement_cannot_supply_unvalidated_complete_counts(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json", return_value=detail()):
            cache = Path(folder)
            first = series.enrich_series(source("1"), "server_1", cache, CONFIG, object(), now=NOW)
            path = next(cache.rglob("*.json"))
            original = json.loads(path.read_bytes())
            for changes in ({"serverEpisodeCount": 99}, {"serverEpisodeCount": True},
                            {"episodeInventoryCheckedAt": "private-invalid-time"},
                            {"fetchedAt": "2026-10-16T11:00:00Z"}):
                path.write_text(json.dumps({**original, **changes}), encoding="utf-8")
                episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, now=NOW)
                self.assertEqual(episodes["1"], [])
                self.assertIsNone(stats["inventoryBySeries"]["1"]["serverEpisodeCount"])
                self.assertFalse(stats["inventoryBySeries"]["1"]["episodeInventoryComplete"])
            forged = (first[0], {**first[1], "inventoryBySeries": {"1": {
                "serverEpisodeCount": 99, "episodeInventoryComplete": True, "episodeInventoryCheckedAt": NOW}}})
            episodes, stats = series.enrich_series(source("1"), "server_1", cache, CONFIG, now=NOW, previous=forged)
            self.assertEqual(episodes["1"], first[0]["1"])
            self.assertIsNone(stats["inventoryBySeries"]["1"]["serverEpisodeCount"])


if __name__ == "__main__":
    unittest.main()
