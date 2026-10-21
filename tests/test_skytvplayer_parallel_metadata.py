from __future__ import annotations

import inspect
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_skytvplayer_catalog as app
import vod_tmdb as tmdb

NOW = "2026-10-21T16:00:00Z"


def source(movie_id=101, series_id=202):
    return {
        "get_vod_categories": [{"category_id": "10", "category_name": "English Movies"}],
        "get_vod_streams": [{"stream_id": movie_id, "name": f"Film{movie_id} (2026)",
                             "tmdb_id": movie_id, "category_id": "10", "adult": False}],
        "get_series_categories": [{"category_id": "20", "category_name": "English Series"}],
        "get_series": [{"series_id": series_id, "name": f"Show{series_id} (2026)",
                        "tmdb_id": series_id, "category_id": "20", "adult": False}],
        "vod_details": {}, "series_details": {},
    }


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode()

    def close(self):
        pass


class Sessions:
    def __init__(self, *, failing=False, retry=False):
        self.lock = threading.Lock()
        self.sessions, self.calls = [], []
        self.active = self.peak = 0
        self.failing, self.retry = failing, retry

    def factory(self, *_args):
        parent = self

        class Session:
            closed = False

            def get(self, url, **kwargs):
                with parent.lock:
                    parent.active += 1
                    parent.peak = max(parent.peak, parent.active)
                    parent.calls.append((self, url))
                try:
                    time.sleep(0.005)
                    if parent.failing:
                        raise RuntimeError("mock worker failed")
                    if parent.retry:
                        return Response(status=503)
                    identifier = int(url.rsplit("/", 1)[-1])
                    movie = "/movie/" in url
                    title = f"Film{identifier}" if movie else f"Show{identifier}"
                    return Response({"id": identifier,
                        "title" if movie else "name": title,
                        "original_title" if movie else "original_name": title,
                        "release_date" if movie else "first_air_date": "2026-01-02",
                        "original_language": "en", "adult": False, "genres": []})
                finally:
                    with parent.lock:
                        parent.active -= 1

            def close(self):
                self.closed = True

        session = Session()
        with self.lock:
            self.sessions.append(session)
        return session


class ParallelMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, name="cache", limit=12, session=None):
        result = tmdb.TmdbClient(self.root / name, token="test-token", max_requests=limit,
                                 session=session or Sessions().factory(), now=1_800_000_000)
        self.addCleanup(result.close)
        return result

    def groups(self, count=6, *, duplicate=False, two_rows=False):
        result = []
        for position in range(count):
            server = f"server_{position + 1}"
            identifier = 101 if duplicate else 101 + position
            inputs = source(identifier)
            if two_rows:
                inputs["get_vod_streams"].append({"stream_id": identifier + 100,
                    "name": f"Film{identifier + 100} (2026)", "tmdb_id": identifier + 100})
            result.append({"serverId": server, "kind": "movie",
                           "prepared": app._prepare_catalog(server, "movie", inputs)})
        return result

    def test_fair_quotas_include_previous_requests_and_never_overshoot(self):
        client = self.client(limit=8)
        client.stats["requests"] = 2
        sessions = Sessions()
        quotas = []

        def factory(*args, **kwargs):
            quotas.append(kwargs["max_requests"])
            return tmdb.TmdbClient(*args, **kwargs)

        app._prefetch_metadata(self.groups(two_rows=True), client, 3, None, None,
                               client_factory=factory, session_factory=sessions.factory)
        self.assertEqual(sorted(quotas), [1] * 6)
        self.assertEqual(client.stats["requests"], 8)
        self.assertEqual(len(sessions.calls), 6)
        self.assertLessEqual(sessions.peak, 3)
        self.assertGreaterEqual(sessions.peak, 2)
        self.assertEqual(len({id(session) for session in sessions.sessions}), 6)
        self.assertTrue(all(session.closed for session in sessions.sessions))
        self.assertEqual(client.stats["parallelUnusedRequests"], 0)

    def test_shared_identity_has_one_deterministic_owner_and_one_request(self):
        groups = self.groups(3, duplicate=True)
        client, sessions = self.client(), Sessions()
        results = app._prefetch_metadata(groups, client, 3, None, None,
                                        session_factory=sessions.factory)
        self.assertEqual(len(sessions.calls), 1)
        self.assertEqual(client.stats["parallelUniqueIdentities"], 1)
        self.assertEqual(client.stats["parallelResultReuse"], 2)
        self.assertEqual(client.stats["parallelUnusedRequests"], 11)
        for group in groups:
            item = group["prepared"]["normalized"][0]
            self.assertEqual(results.enrich(item, "movie")["tmdbId"], "101")

    def test_reviewed_constraints_do_not_share_results_or_cache_writers(self):
        groups = self.groups(2, duplicate=True)
        client, sessions = self.client(), Sessions()
        calls = []

        class Worker:
            def __init__(self, *args, **kwargs):
                self.stats = {"requests": 0}
                self.session = kwargs["session"]

            def enrichment_priority(self, *_args):
                return 0

            def enrich(self, item, kind):
                calls.append((item["contentId"], self.session))
                return {"matchStatus": "no_match" if item["contentId"].startswith("server_2:") else "matched_id"}

            def close(self):
                pass

        def correction(server, *_args):
            return {"rejectIdentity": True, "rejectedTmdbIds": ["101"]} if server == "server_2" else None

        with mock.patch.object(app, "correction_for", side_effect=correction):
            results = app._prefetch_metadata(groups, client, 3, None, None,
                    client_factory=Worker, session_factory=sessions.factory)
            self.assertEqual(results.enrich(groups[0]["prepared"]["normalized"][0], "movie")["matchStatus"], "matched_id")
            self.assertEqual(results.enrich(groups[1]["prepared"]["normalized"][0], "movie")["matchStatus"], "no_match")
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][1], calls[1][1])

    def test_record_bound_secondary_evidence_never_deduplicates_other_assets(self):
        groups = self.groups(2, duplicate=True)

        class Evidence:
            def load(self, item):
                return {"contentId": item["contentId"], "fingerprint": "f" * 64,
                        "imdbId": "tt1234567", "directors": [], "cast": []}

        keys = [app._metadata_result_key(app._matching_item(group["prepared"]["normalized"][0], {},
                    "movie", Evidence()), "movie", None) for group in groups]
        self.assertNotEqual(keys[0][0], keys[1][0])

    def test_retry_attempts_stay_within_each_fixed_group_quota(self):
        client, sessions = self.client(limit=6), Sessions(retry=True)
        with mock.patch.object(tmdb.time, "sleep"):
            app._prefetch_metadata(self.groups(3), client, 3, None, None,
                                   session_factory=sessions.factory)
        self.assertEqual(client.stats["requests"], 6)
        self.assertEqual(len(sessions.calls), 6)
        self.assertTrue(all(session.closed for session in sessions.sessions))

    def test_parallel_artifacts_equal_serial_and_normalize_only_once(self):
        inputs = source()
        serial_sessions, parallel_sessions = Sessions(), Sessions()
        serial = self.client("serial-cache", session=serial_sessions.factory())
        serial.trending = mock.Mock(return_value={})
        expected = app.build(self.root / "serial", self.root / "serial-state",
                             [("server_1", inputs, None)], serial, NOW)
        parallel = self.client("parallel-cache")
        parallel.trending = mock.Mock(return_value={})
        original = app._prefetch_metadata

        def prefetch(*args):
            return original(*args, session_factory=parallel_sessions.factory)

        with mock.patch.object(app, "_prefetch_metadata", side_effect=prefetch), \
                mock.patch.object(app.vod, "normalize_catalog", wraps=app.vod.normalize_catalog) as normalize:
            actual = app.build(self.root / "parallel", self.root / "parallel-state",
                               [("server_1", inputs, None)], parallel, NOW, tmdb_workers=3)
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(actual["servers"], expected["servers"])
        for reference in [actual["servers"][0][key] for key in ("home", "movies", "series")]:
            self.assertEqual((self.root / "serial" / reference["file"]).read_bytes(),
                             (self.root / "parallel" / reference["file"]).read_bytes())
        self.assertEqual(json.loads((self.root / "serial-state/server_1_inventory.json").read_text()),
                         json.loads((self.root / "parallel-state/server_1_inventory.json").read_text()))

    def test_worker_failure_closes_sessions_and_leaves_index_unchanged(self):
        output = self.root / "output"
        output.mkdir()
        previous = b'{"previous":true}'
        (output / "index.json").write_bytes(previous)
        client, sessions = self.client(), Sessions(failing=True)
        client.trending = mock.Mock(return_value={})
        original = app._prefetch_metadata
        with mock.patch.object(app, "_prefetch_metadata", side_effect=lambda *args:
                               original(*args, session_factory=sessions.factory)):
            with self.assertRaises(RuntimeError):
                app.build(output, self.root / "state", [("server_1", source(), None)],
                          client, NOW, tmdb_workers=3)
        self.assertEqual((output / "index.json").read_bytes(), previous)
        self.assertTrue(all(session.closed for session in sessions.sessions))

    def test_pacing_and_shared_429_cooldown_are_bounded(self):
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        pacer = app._TmdbPacer()
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(app.time, "sleep", side_effect=sleep):
            pacer.wait(110)
            pacer.wait(110)
            self.assertAlmostEqual(clock[0], 100.05)
            pacer.observe(Response(status=429, headers={"Retry-After": "2"}))
            pacer.wait(110)
            self.assertAlmostEqual(clock[0], 102.05)
            pacer.observe(Response(status=429, headers={"Retry-After": "90000"}))
            with self.assertRaises(app.requests.Timeout):
                pacer.wait(103)
            self.assertLess(clock[0], 103)

    def test_worker_option_defaults_to_serial_and_rejects_invalid_before_io(self):
        self.assertEqual(inspect.signature(app.build).parameters["tmdb_workers"].default, 1)
        for workers in (0, 4):
            with mock.patch.object(app.requests, "Session") as session:
                with self.assertRaises(app.vod.CatalogBuildError):
                    app.main(["--tmdb-workers", str(workers)])
                session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
