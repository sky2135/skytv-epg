from __future__ import annotations

import copy
import gzip
import hashlib
import json
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as app
import build_vod_catalogs as vod
import vod_tmdb as tmdb
from vod_provider_evidence import DeadlineSession, MovieEvidenceCollector, normalize_facts
from vod_resolution_queue import ResolutionQueue
from test_vod_tmdb import FakeResponse, FakeSession, movie_details
from test_skytvplayer_release_backfill import source, movie, NOW, RecordingClient
from test_skytvplayer_catalog import FakeTmdbClient, app_source


class ResolutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "private-cache"
        self.config = vod.ServerConfig("server_1", "https://provider.invalid", "private-user", "private-password")

    def queue(self):
        return ResolutionQueue(self.cache / "resolution-queue.json", policy_version=tmdb.MATCH_STRATEGY, now=NOW)

    def test_provider_evidence_resolves_one_asset_and_publisher_reuses_it_without_requests(self):
        data = source(movie(1))
        original = copy.deepcopy(data)
        candidates = [{"id": i, "title": "Example Film", "release_date": "2026-10-10"} for i in (101, 102)]
        details = {**movie_details(101), "release_date": "2026-10-10",
                   "external_ids": {"imdb_id": "tt1234567"}}
        search = {"total_pages": 1, "results": candidates}
        session = FakeSession(FakeResponse(search), FakeResponse({"movie_results": [{"id": 101}]}),
                              FakeResponse(details))
        client = tmdb.TmdbClient(self.cache / "tmdb", token="private-token", session=session)
        collector = MovieEvidenceCollector(self.cache / "provider-evidence", configs={"server_1": self.config})
        self.addCleanup(collector.close)
        with mock.patch.object(vod, "_request_panel_json_once", return_value={
                "info": {"imdb_id": "tt1234567", "cast": "Actor One, Actor Two", "director": "Director One"},
                "direct_source": "https://provider.invalid/private-user/private-password/1.mp4"}) as request:
            result = app.backfill_release_dates([("server_1", data)], client, NOW,
                queue=self.queue(), evidence=collector, report_dir=self.root / "reports")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["matchedDates"], 1)
        self.assertEqual(result["providerEvidence"]["requests"], 1)
        self.assertEqual(result["stats"]["requests"], 3)
        self.assertEqual([url for url, _options in session.calls], [
            tmdb.API_ROOT + "/search/movie", tmdb.API_ROOT + "/find/tt1234567", tmdb.API_ROOT + "/movie/101"])
        self.assertEqual(data, original)
        replay = tmdb.TmdbClient(self.cache / "tmdb", session=FakeSession())
        replay.trending = lambda _kind: {}
        output = self.root / "public-output"
        index = app.build(output, self.cache, [("server_1", data, None)], replay, NOW)
        manifest = app.read_artifact(output, index["servers"][0]["movies"])
        rows = [row for ref in manifest["shards"] for row in app.read_artifact(output, ref)["items"]]
        self.assertEqual(rows[0]["tmdbId"], "101")
        self.assertEqual(rows[0]["releaseDate"], "2026-10-10")
        self.assertEqual(replay.stats["requests"], 0)
        raw = "".join(p.read_text(encoding="utf-8") for p in self.cache.rglob("*.json"))
        for secret in ("private-password", "private-user", "private-token", "provider.invalid"):
            self.assertNotIn(secret, raw)
        for row in rows:
            self.assertNotIn("_identityEvidence", row)
            self.assertNotIn("providerEvidenceFingerprint", row)

    def test_stable_failure_is_not_retried_on_the_next_immediate_build(self):
        queue = self.queue()
        client = RecordingClient(results={"1": {"matchStatus": "no_match", "releaseDate": None}})
        client.last_match_diagnostic = {"reason": "ambiguous_title_year", "candidateIds": ["101", "102"]}
        app.backfill_release_dates([("server_1", source(movie(1)))], client, NOW, queue=queue)
        second = RecordingClient()
        result = app.backfill_release_dates([("server_1", source(movie(1)))], second, NOW, queue=self.queue())
        self.assertEqual(second.items, [])
        self.assertEqual(result["servers"][0]["queueDeferred"], 1)

    def test_fresh_publisher_match_reconciles_only_an_existing_queue_identity(self):
        queue = self.queue()
        initial = RecordingClient(results={"1": {"matchStatus": "no_match", "releaseDate": None}})
        initial.last_match_diagnostic = {"reason": "ambiguous_title_year", "candidateIds": ["101", "102"]}
        app.backfill_release_dates([("server_1", source(movie(1)))], initial, NOW, queue=queue)
        key, history = next(iter(queue.entries.items()))
        history = copy.deepcopy(history)
        data = source(movie(1), movie(2, "Another Film (2026)"))
        responses = []
        for identifier, title in ((101, "Example Film"), (102, "Another Film")):
            responses.extend((FakeResponse({"results": [{"id": identifier, "title": title, "release_date": "2026-10-10"}]}),
                              FakeResponse({**movie_details(identifier), "title": title, "release_date": "2026-10-10"})))
        publisher = tmdb.TmdbClient(self.cache / "tmdb", token="private-token", session=FakeSession(*responses))
        rows = app.make_items("server_1", "movie", data, app.load_state(self.cache / "inventory.json"), publisher, NOW)
        self.assertEqual([row["releaseDate"] for row in rows], ["2026-10-10", "2026-10-10"])
        replay = tmdb.TmdbClient(self.cache / "tmdb", session=FakeSession())
        result = app.backfill_release_dates([("server_1", data)], replay, NOW,
            queue=queue, report_dir=self.root / "reports")
        self.assertEqual(result["processedCount"], 0)
        self.assertEqual(result["servers"][0]["cachedDatesReused"], 2)
        self.assertEqual(replay.stats["requests"], 0)
        self.assertEqual(set(queue.entries), {key})
        reconciled = queue.entries[key]
        self.assertEqual(reconciled["reason"], "matched_search")
        self.assertEqual(reconciled["candidateIds"], ["101"])
        for field in ("attempts", "lastAttemptAt", "firstSeenAt", "sourceFingerprint", "selectionOrder"):
            self.assertEqual(reconciled[field], history[field], field)
        self.assertEqual(reconciled["failureStreak"], 0)
        report = json.loads((self.root / "reports/resolution_queue.json").read_text(encoding="utf-8"))
        self.assertEqual(report["reasonCounts"], {"matched_search": 1})

    def test_fresh_cache_reconciliation_does_not_cross_existing_evidence_binding(self):
        data = source(movie(1))
        prepared, _stats = vod.normalize_catalog(data["get_vod_streams"], [], {}, kind="movie", label="test")
        item = dict(prepared[0], contentId="server_1:movie:1", providerLastModified=None,
                    _identityEvidence={"contentId": "server_1:movie:1", "fingerprint": "f" * 64, "imdbId": "tt1234567"})
        queue = self.queue()
        queue.record_result("server_1", "movie", item, {"matchStatus": "no_match"},
            {"reason": "ambiguous_title_year", "candidateIds": ["101", "102"]})
        key, history = next(iter(queue.entries.items()))
        history = copy.deepcopy(history)
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Example Film", "release_date": "2026-10-10"}]}),
                              FakeResponse({**movie_details(), "release_date": "2026-10-10"}))
        publisher = tmdb.TmdbClient(self.cache / "tmdb", token="private-token", session=session)
        app.make_items("server_1", "movie", data, app.load_state(self.cache / "inventory.json"), publisher, NOW)
        replay = tmdb.TmdbClient(self.cache / "tmdb", session=FakeSession())
        app.backfill_release_dates([("server_1", data)], replay, NOW, queue=queue)
        self.assertEqual(set(queue.entries), {key})
        self.assertEqual(queue.entries[key], history)

    def test_provider_failure_keeps_a_reportable_unresolved_record_and_does_not_raise(self):
        client = RecordingClient(results={"1": {"matchStatus": "no_match", "releaseDate": None}})
        client.last_match_diagnostic = {"reason": "ambiguous_title_year", "candidateIds": ["101", "102"]}
        collector = MovieEvidenceCollector(self.cache / "provider-evidence", configs={"server_1": self.config})
        self.addCleanup(collector.close)
        with mock.patch.object(vod, "_request_panel_json_once", side_effect=vod.CatalogBuildError("safe failure")):
            result = app.backfill_release_dates([("server_1", source(movie(1)))], client, NOW,
                queue=self.queue(), evidence=collector, report_dir=self.root / "reports")
        self.assertEqual(result["noMatch"] if "noMatch" in result else result["servers"][0]["noMatch"], 1)
        self.assertEqual(result["providerEvidence"]["errors"], 1)
        report = json.loads((self.root / "reports/resolution_queue.json").read_text(encoding="utf-8"))
        self.assertEqual(report["records"][0]["reason"], "ambiguous_title_year")

    def test_collector_rejects_secret_reflection_and_limits_optional_requests(self):
        collector = MovieEvidenceCollector(self.cache / "provider-evidence", configs={"server_1": self.config}, max_requests=1)
        self.addCleanup(collector.close)
        first = {"id": "1", "contentId": "server_1:movie:1", "title": "Film (2026)"}
        second = dict(first, id="2", contentId="server_1:movie:2")
        with mock.patch.object(vod, "_request_panel_json_once", return_value={"info": {"director": "private-password"}}) as request:
            self.assertIsNone(collector.collect("server_1", first))
            self.assertIsNone(collector.collect("server_1", second))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(collector.stats["errors"], 1)
        self.assertEqual(collector.stats["budgetDeferred"], 1)
        self.assertEqual(list((self.cache / "provider-evidence").glob("*.json")), [])

    def test_evidence_is_invalidated_by_recycled_title_or_changed_provider_metadata(self):
        collector = MovieEvidenceCollector(self.cache / "provider-evidence", configs={"server_1": self.config})
        self.addCleanup(collector.close)
        item = {"id": "1", "contentId": "server_1:movie:1", "title": "Film (2026)", "providerLastModified": "2026-10-20T00:00:00Z"}
        with mock.patch.object(vod, "_request_panel_json_once", return_value={"info": {"imdb_id": "tt1234567"}}):
            self.assertIsNotNone(collector.collect("server_1", item))
        self.assertIsNone(collector.load(dict(item, title="Another Film (2026)")))
        self.assertIsNone(collector.load(dict(item, providerLastModified="2026-10-21T00:00:00Z")))

    def test_provider_fact_whitelist_discards_urls_and_keeps_distinct_people(self):
        facts = normalize_facts({"info": {"director": "Director One", "cast": [
            {"name": "Actor One", "playback": "private"}, "Actor Two", "https://private.invalid"],
            "duration_secs": "5400", "imdb_id": "tt1234567", "password": "not retained"}})
        self.assertEqual(facts, {"imdbId": "tt1234567", "directors": ["Director One"],
                                 "cast": ["Actor One", "Actor Two"], "runtimeMinutes": 90})

    def test_inconsistent_provider_id_is_rejected_before_a_request(self):
        collector = MovieEvidenceCollector(self.cache / "provider-evidence", configs={"server_1": self.config})
        with mock.patch.object(vod, "_request_panel_json_once") as request:
            with self.assertRaises(ValueError):
                collector.collect("server_1", {"id": "2", "contentId": "server_1:movie:1", "title": "Film"})
        request.assert_not_called()

    def test_redirect_hops_share_the_provider_http_attempt_cap(self):
        stats = {"requests": 1}
        session = DeadlineSession(time.monotonic() + 60, stats, 2)
        self.addCleanup(session.close)
        session.initial_reserved = True
        response = SimpleNamespace(iter_content=lambda **_kwargs: [], close=lambda: None)
        with mock.patch("requests.Session.get", return_value=response) as request:
            session.get("https://provider.invalid/player_api.php")
            session.get("https://provider.invalid/player_api.php")
            with self.assertRaises(Exception) as failed:
                session.get("https://provider.invalid/player_api.php")
        self.assertIsInstance(failed.exception, app.requests.Timeout)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(stats["requests"], 2)

    def test_normal_publisher_honors_queue_backoff_after_negative_cache_ttl(self):
        data = source(movie(1, stream_icon="https://image.tmdb.org/t/p/w500/provider.jpg"))
        warm = tmdb.TmdbClient(self.cache / "tmdb", token="token", now=1_800_000_000,
                              session=FakeSession(FakeResponse({"total_pages": 0, "results": []})))
        app.backfill_release_dates([("server_1", data)], warm, NOW, queue=self.queue())
        replay = tmdb.TmdbClient(self.cache / "tmdb", token="token", now=1_800_000_000 + 2 * 86400,
                                session=FakeSession())
        original = replay.enrich
        replay.enrich = lambda item, kind: original(item, kind) if kind == "movie" else tmdb._empty(None)
        replay.trending = lambda _kind: {}
        app.build(self.root / "output", self.cache, [("server_1", data, None)], replay, "2026-10-23T16:30:00Z")
        self.assertEqual(replay.stats["requests"], 0)
        self.assertEqual(replay.stats["queueDeferred"], 1)

    def test_unused_episode_quota_is_redistributed_with_prior_inventory(self):
        seen = []
        lock = threading.Lock()

        def refresh(_source, server_id, cache, config, session, *, max_requests, now, deadline_seconds, previous=None):
            with lock:
                seen.append((server_id, max_requests, previous))
            count = max_requests if server_id == "server_3" else 0
            return {}, {"requests": count, "cacheHits": 0, "statusBySeries": {},
                        "budgetDeferred": 1 if server_id == "server_3" and previous is None else 0,
                        "deadlineReached": False, "attemptedSeriesIds": ["1"]}

        sources = [(f"server_{i}", app_source(), vod.ServerConfig(f"server_{i}", "https://provider.invalid", "user", "password"))
                   for i in range(1, 4)]
        with mock.patch.object(app, "enrich_series", side_effect=refresh):
            index = app.build(self.root / "output", self.cache, sources, FakeTmdbClient(), NOW,
                              fetch_episodes=True, max_episode_requests=8)
        supplemental = [entry for entry in seen if entry[2] is not None]
        self.assertEqual(len(supplemental), 1)
        self.assertEqual(supplemental[0][0:2], ("server_3", 6))
        self.assertEqual(sum(server["episodeRefresh"]["requests"] for server in index["servers"]), 8)
        self.assertTrue(all("attemptedSeriesIds" not in server["episodeRefresh"] for server in index["servers"]))

    def test_independent_episode_sources_overlap_and_keep_isolated_sessions_and_quotas(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        observed = []

        def refresh(_source, server_id, cache, config, session, *, max_requests, now, deadline_seconds, previous=None):
            with lock:
                observed.append((server_id, id(session), max_requests, session))
            barrier.wait(timeout=3)
            return {}, {"requests": 0, "cacheHits": 0, "statusBySeries": {}, "budgetDeferred": 0, "deadlineReached": False}

        sources = [(f"server_{i}", app_source(), vod.ServerConfig(f"server_{i}", "https://provider.invalid", "user", "password"))
                   for i in range(1, 4)]
        with mock.patch.object(app, "enrich_series", side_effect=refresh):
            index = app.build(self.root / "output", self.cache, sources, FakeTmdbClient(), NOW,
                              fetch_episodes=True, max_episode_requests=8)
        self.assertEqual(index["serverCount"], 3)
        self.assertEqual(len({entry[1] for entry in observed}), 3)
        self.assertEqual(sum(entry[2] for entry in observed), 8)
        self.assertEqual(sorted(entry[2] for entry in observed), [2, 3, 3])
        self.assertTrue(all(not entry[3].trust_env for entry in observed))

    def test_reused_shard_bytes_are_identical_to_the_original_encoding(self):
        output = self.root / "output"
        output.mkdir()
        items = [{"id": "1", "contentId": "server_1:movie:1", "title": "Film",
                  "category": {"id": "10", "name": "English"}, "languages": ["en"]}]
        ref = app.write_dataset(output, "server_1", "movie", items, NOW)
        manifest = app.read_artifact(output, ref)
        shard = manifest["shards"][0]
        expected = gzip.compress(app.json_bytes({"schemaVersion": 1, "serverId": "server_1", "dataset": "vod-movies",
            "generatedAt": NOW, "recordCount": 1, "items": items}), mtime=0)
        self.assertEqual((output / shard["file"]).read_bytes(), expected)
        self.assertEqual(shard["sha256"], hashlib.sha256(expected).hexdigest())


if __name__ == "__main__":
    unittest.main()
