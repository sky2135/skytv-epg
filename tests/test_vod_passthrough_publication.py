from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_skytvplayer_catalog as app
import vod_tmdb as tmdb
from vod_resolution_queue import ResolutionQueue
from test_vod_tmdb import FakeResponse, FakeSession

NOW = "2026-10-22T16:30:00Z"
LATER = "2026-11-22T16:30:00Z"


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def source(identifier=1):
    return {"get_vod_streams": [{"stream_id": identifier, "name": "Example Film (2026)",
             "language": "en", "container_extension": "mkv", "rating": "6.5",
             "plot": "Provider synopsis", "added": "1656668940", "category_id": "10"}],
            "get_vod_categories": [{"category_id": "10", "category_name": "English Movies"}],
            "vod_details": {}, "get_series": [], "get_series_categories": [], "series_details": {}}


def miss():
    return FakeResponse({"page": 1, "total_pages": 1, "total_results": 0, "results": []})


class PassthroughPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"

    def client(self, session, now=NOW):
        return tmdb.TmdbClient(self.cache / "tmdb", token="private-token", session=session,
                               now=epoch(now), max_requests=30)

    def test_unmatched_movie_retains_provider_fields_and_playback_without_later_requests(self):
        data = source()
        original = copy.deepcopy(data)
        state = app.load_state(self.cache / "inventory.json")
        first = self.client(FakeSession(miss()))
        before = app.make_items("server_2", "movie", data, state, first, NOW)
        second = self.client(FakeSession(), LATER)
        after = app.make_items("server_2", "movie", data, state, second, LATER)
        self.assertEqual(data, original)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        for field in ("id", "contentId", "providerStreamId", "title", "category", "languages",
                      "containerExtension", "overview", "rating", "artwork", "metadataMatch"):
            self.assertEqual(after[0][field], before[0][field], field)
        self.assertEqual(after[0]["providerStreamId"], "1")
        self.assertEqual(after[0]["containerExtension"], "mkv")
        self.assertEqual(after[0]["overview"], "Provider synopsis")
        self.assertEqual(after[0]["metadataMatch"], "no_match")
        self.assertIsNone(after[0]["tmdbId"])
        self.assertEqual(second.stats["requests"], 0)
        self.assertNotIn("ignoredWithoutArtwork", after[0])

    def test_backfill_tries_provider_evidence_once_then_uses_no_more_retry_slots(self):
        class Evidence:
            def __init__(self):
                self.stats = {"requests": 0}

            def load(self, _item):
                return None

            def collect(self, _server, _item):
                self.stats["requests"] += 1
                return None

        data = source()
        queue_path = self.cache / "resolution-queue.json"
        queue = ResolutionQueue(queue_path, policy_version=tmdb.MATCH_STRATEGY, now=NOW)
        collector = Evidence()
        initial = app.backfill_release_dates([("server_1", data)], self.client(FakeSession(miss())), NOW,
                                             queue=queue, evidence=collector)
        self.assertEqual(initial["processedCount"], 1)
        self.assertEqual(collector.stats["requests"], 1)
        history = copy.deepcopy(next(iter(queue.entries.values())))
        self.assertEqual(history["reason"], "ignored_no_artwork")
        next_day = "2026-10-23T16:30:00Z"
        retry_queue = ResolutionQueue(queue_path, policy_version=tmdb.MATCH_STRATEGY, now=next_day)
        replay = self.client(FakeSession(), next_day)
        result = app.backfill_release_dates([("server_1", data)], replay, next_day,
                                            queue=retry_queue, evidence=collector, max_due=1)
        self.assertEqual(result["processedCount"], 0)
        self.assertEqual(result["servers"][0]["ignoredNoArtwork"], 1)
        self.assertEqual(replay.stats["requests"], 0)
        self.assertEqual(collector.stats["requests"], 1)
        row = next(iter(retry_queue.entries.values()))
        for field in ("attempts", "lastAttemptAt", "nextAttemptAt", "selectionOrder"):
            self.assertEqual(row[field], history[field], field)

    def test_parallel_sources_keep_separate_skips_and_all_rows(self):
        groups = [{"serverId": server, "kind": "movie",
                   "prepared": app._prepare_catalog(server, "movie", source(), None)}
                  for server in ("server_1", "server_2", "server_3")]
        sessions = []

        def session_factory(_pacer, _deadline):
            session = FakeSession(miss(), miss(), miss())
            sessions.append(session)
            return session

        first = self.client(FakeSession())
        results = app._prefetch_metadata(groups, first, 3, None, None, session_factory=session_factory)
        self.assertEqual(len(results.results), 3)
        self.assertEqual(first.stats["requests"], 3)
        second = self.client(FakeSession(), LATER)
        replay = app._prefetch_metadata(groups, second, 3, None, None,
                    session_factory=lambda _pacer, _deadline: FakeSession())
        self.assertEqual(len(replay.results), 3)
        self.assertEqual(second.stats["requests"], 0)
        for group in groups:
            server = group["serverId"]
            rows = app.make_items(server, "movie", source(), app.load_state(self.cache / (server + ".json")),
                                 replay, LATER, prepared=group["prepared"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["contentId"], server + ":movie:1")
            self.assertEqual(rows[0]["metadataMatch"], "no_match")

    def test_queue_hint_alone_cannot_stop_matching_after_private_proof_is_lost(self):
        data = source()
        queue_path = self.cache / "resolution-queue.json"
        queue = ResolutionQueue(queue_path, policy_version=tmdb.MATCH_STRATEGY, now=NOW)
        first = self.client(FakeSession(miss()))
        app.backfill_release_dates([("server_1", data)], first, NOW, queue=queue)
        self.assertEqual(next(iter(queue.entries.values()))["reason"], "ignored_no_artwork")
        for path in first.cache_dir.glob("*.json"):
            path.unlink()
        retry_queue = ResolutionQueue(queue_path, policy_version=tmdb.MATCH_STRATEGY, now=LATER)
        second = self.client(FakeSession(miss()), LATER)
        result = app.backfill_release_dates([("server_1", data)], second, LATER,
                                            queue=retry_queue, max_due=1)
        self.assertEqual(result["processedCount"], 1)
        self.assertEqual(second.stats["requests"], 1)
        self.assertEqual(next(iter(retry_queue.entries.values()))["attempts"], 2)

    def test_failed_or_budget_deferred_provider_probe_remains_retryable(self):
        class Evidence:
            def __init__(self, failure):
                self.failure = failure
                self.calls = 0
                self.stats = {"requests": 0, "errors": 0, "budgetDeferred": 0}

            def load(self, _item):
                return None

            def collect(self, _server, _item):
                self.calls += 1
                if self.calls == 1:
                    self.stats[self.failure] += 1
                return None

        for failure in ("errors", "budgetDeferred"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory)
                client = tmdb.TmdbClient(cache / "tmdb", token="fixture", session=FakeSession(miss()), now=epoch(NOW))
                queue = ResolutionQueue(cache / "queue.json", policy_version=tmdb.MATCH_STRATEGY, now=NOW)
                evidence = Evidence(failure)
                app.backfill_release_dates([("server_1", source())], client, NOW, queue=queue, evidence=evidence)
                self.assertEqual(next(iter(queue.entries.values()))["reason"], "unavailable")
                target = app._prepare_catalog("server_1", "movie", source())["normalized"][0]
                target["providerLastModified"] = None
                self.assertFalse(client.ignored_without_artwork(target, "movie"))
                later = "2026-10-23T16:30:01Z"
                retry = tmdb.TmdbClient(cache / "tmdb", token="fixture", session=FakeSession(miss()), now=epoch(later))
                retry_queue = ResolutionQueue(cache / "queue.json", policy_version=tmdb.MATCH_STRATEGY, now=later)
                result = app.backfill_release_dates([("server_1", source())], retry, later,
                                                     queue=retry_queue, evidence=evidence)
                self.assertEqual(result["processedCount"], 1)
                self.assertEqual(evidence.calls, 2)


if __name__ == "__main__":
    unittest.main()
