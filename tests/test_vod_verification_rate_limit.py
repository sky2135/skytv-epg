from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vod_verification_rate_limit import (  # noqa: E402
    PacedSession,
    SharedRateLimiter,
    VerificationDeferred,
    retry_after_seconds,
)


class Clock:
    def __init__(self, wall: float = 1_000, mono: float = 100):
        self.wall, self.mono = wall, mono
        self.sleeps: list[float] = []
        self.on_sleep = None
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.wall

    def monotonic(self):
        with self.lock:
            return self.mono

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.wall += seconds
            self.mono += seconds
        if self.on_sleep is not None:
            callback, self.on_sleep = self.on_sleep, None
            callback()

    def limiter(self, **kwargs):
        return SharedRateLimiter(now=self.now, monotonic=self.monotonic, sleep=self.sleep, **kwargs)


class Response:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False
        self.body = body if body is not None else {"id": 123}

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        yield json.dumps(self.body).encode("utf-8")


class RecordingSession:
    def __init__(self, clock, replies):
        self.clock = clock
        self.replies = iter(replies)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((self.clock.monotonic(), url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


class VerificationRateLimitTests(unittest.TestCase):
    url = "https://api.themoviedb.org/3/search/movie"

    def test_workers_share_one_request_rate_not_one_rate_per_session(self):
        clock = Clock()
        limiter = clock.limiter(rate=20)
        sessions = [RecordingSession(clock, [Response(), Response()]) for _ in range(3)]
        workers = [PacedSession(limiter, session) for session in sessions]
        for _ in range(2):
            for worker in workers:
                worker.get(self.url)
        times = sorted(call[0] for session in sessions for call in session.calls)
        self.assertEqual(len(times), 6)
        for previous, following in zip(times, times[1:]):
            self.assertGreaterEqual(following - previous + 1e-10, .05)
        self.assertEqual(limiter.snapshot()["wireAttempts"], 6)
        self.assertEqual(limiter.snapshot()["routeCounts"], {"search_movie": 6})

    def test_real_concurrent_workers_cannot_reserve_the_same_permit(self):
        limiter = SharedRateLimiter(rate=50, deadline=time.time() + 10)
        starts, lock = [], threading.Lock()

        def work():
            limiter.acquire()
            with lock:
                starts.append(time.monotonic())

        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: work(), range(9)))
        starts.sort()
        self.assertGreaterEqual(starts[-1] - starts[0], .15)
        # Small scheduling tolerance: the limiter checks before this timestamp.
        for before, after in zip(starts, starts[1:]):
            self.assertGreaterEqual(after - before, .015)

    def test_long_seconds_header_is_fully_respected_and_429_closed_first(self):
        clock = Clock()
        limited, good = Response(429, {"Retry-After": "125"}), Response()
        session = RecordingSession(clock, [limited, good])
        limiter = clock.limiter(deadline=2_000)
        worker = PacedSession(limiter, session)
        clock.on_sleep = lambda: self.assertTrue(limited.closed)
        self.assertIs(worker.get(self.url), good)
        self.assertEqual(session.calls[1][0] - session.calls[0][0], 125)
        self.assertTrue(all(delay <= 60 for delay in clock.sleeps))
        stats = limiter.snapshot()
        self.assertEqual(stats["wireAttempts"], 2)
        self.assertEqual(stats["statusCounts"], {"429": 1, "200": 1})
        self.assertEqual(stats["rateLimitRetries"], 1)
        self.assertEqual(stats["rateLimitResponses"], 1)
        self.assertEqual(stats["cooldownSeconds"], 125)
        self.assertEqual(stats["waitSeconds"], 125)
        self.assertIsNone(stats["cooldownUntil"])

    def test_http_date_header_uses_wall_clock_and_is_not_clipped(self):
        clock = Clock()
        date = formatdate(clock.now() + 120, usegmt=True)
        session = RecordingSession(clock, [Response(429, {"Retry-After": date}), Response()])
        worker = PacedSession(clock.limiter(deadline=2_000), session)
        worker.get(self.url)
        self.assertEqual(session.calls[1][0] - session.calls[0][0], 120)

    def test_long_cooldown_defers_without_sleep_and_survives_resume(self):
        clock = Clock()
        limited = Response(429, {"Retry-After": "600"})
        session = RecordingSession(clock, [limited])
        limiter = clock.limiter(deadline=clock.now() + 60)
        worker = PacedSession(limiter, session)
        with self.assertRaises(VerificationDeferred) as caught:
            worker.get(self.url, headers={"Authorization": "Bearer private-token"}, params={"api_key": "private-api-key"})
        self.assertEqual(clock.sleeps, [])
        self.assertTrue(limited.closed)
        error = caught.exception
        self.assertEqual(error.reason, "rate_limit_cooldown")
        self.assertEqual(error.retry_at, 1_600)
        self.assertIs(worker.last_deferred, error)
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["cooldownUntil"], 1_600)
        self.assertEqual(snapshot["wireAttempts"], 1)
        serialized = json.dumps(snapshot) + str(error)
        for secret in ("private-token", "private-api-key", self.url):
            self.assertNotIn(secret, serialized)
        resumed_clock = Clock(wall=1_100, mono=5)
        resumed = resumed_clock.limiter(deadline=2_000, resume_state=snapshot)
        fresh = RecordingSession(resumed_clock, [Response()])
        PacedSession(resumed, fresh).get(self.url)
        self.assertEqual(resumed_clock.now(), 1_600)
        self.assertEqual(resumed.snapshot()["wireAttempts"], 2)
        self.assertEqual(resumed.snapshot()["deferredCalls"], 1)

    def test_persistence_has_full_cooldown_before_defer_or_sleep(self):
        clock = Clock()
        saved = []
        limited = Response(429, {"Retry-After": "600"})
        limiter = clock.limiter(deadline=1_060, persist=lambda state: saved.append(json.loads(json.dumps(state))))
        session = RecordingSession(clock, [limited])
        with self.assertRaises(VerificationDeferred):
            PacedSession(limiter, session).get(self.url)
        self.assertEqual(clock.sleeps, [])
        cooldown_saves = [state for state in saved if state["cooldownUntil"] == 1_600]
        self.assertTrue(cooldown_saves)
        # The cooldown is saved before the following acquire defers. An abrupt
        # stop here can resume the server pause even without the runner finally.
        self.assertEqual(cooldown_saves[0]["deferredCalls"], 0)
        self.assertEqual(cooldown_saves[0]["wireAttempts"], 1)
        self.assertEqual(cooldown_saves[0]["statusCounts"], {"429": 1})
        self.assertEqual(cooldown_saves[0]["firstRequestAt"], 1_000)
        self.assertEqual(saved[-1]["deferredCalls"], 1)
        self.assertTrue(all(state["cooldownUntil"] == 1_600 for state in saved if state["statusCounts"].get("429")))
        resumed = Clock(wall=1_010).limiter(deadline=1_100, resume_state=cooldown_saves[0])
        with self.assertRaises(VerificationDeferred) as caught:
            resumed.acquire()
        self.assertEqual(caught.exception.retry_at, 1_600)

    def test_attempt_and_network_failure_are_durable_immediately(self):
        clock = Clock()
        saved = []
        limiter = clock.limiter(persist=lambda state: saved.append(state))
        session = RecordingSession(clock, [requests.Timeout("private URL")])
        with self.assertRaises(requests.RequestException):
            PacedSession(limiter, session).get(self.url)
        self.assertEqual(saved[0]["wireAttempts"], 1)
        self.assertEqual(saved[0]["firstRequestAt"], 1_000)
        self.assertEqual(saved[0]["lastRequestAt"], 1_000)
        self.assertEqual(saved[0]["networkErrors"], 0)
        self.assertEqual(saved[-1]["networkErrors"], 1)

    def test_persistence_failure_stops_before_another_wire_attempt(self):
        clock = Clock()
        saved = []

        def persist(state):
            saved.append(state)
            if state["cooldownUntil"] is not None:
                raise OSError("Checkpoint unavailable")

        limiter = clock.limiter(deadline=2_000, persist=persist)
        limited = Response(429, {"Retry-After": "60"})
        session = RecordingSession(clock, [limited, Response()])
        with self.assertRaisesRegex(OSError, "Checkpoint unavailable"):
            PacedSession(limiter, session).get(self.url)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(limited.closed)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(limiter.snapshot()["cooldownUntil"], 1_060)

    def test_persistence_callback_runs_outside_scheduling_lock(self):
        clock = Clock()
        limiter = None

        def persist(_state):
            # A callback may use a storage lock that another thread holds while
            # reporting a limiter snapshot. That thread must not deadlock here.
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(limiter.snapshot).result(timeout=2)["wireAttempts"], 1)

        limiter = clock.limiter(persist=persist)
        limiter.record_attempt("search_movie")

    def test_response_is_closed_when_response_checkpoint_fails(self):
        clock = Clock()

        def persist(state):
            if state["statusCounts"]:
                raise OSError("Checkpoint unavailable")

        response = Response()
        session = RecordingSession(clock, [response])
        with self.assertRaises(OSError):
            PacedSession(clock.limiter(persist=persist), session).get(self.url)
        self.assertTrue(response.closed)

    def test_concurrent_persistence_never_overwrites_newer_counters(self):
        clock = Clock()
        saved = []
        limiter = clock.limiter(persist=lambda state: saved.append(state))
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: limiter.record_attempt("search_movie"), range(24)))
        attempts = [state["wireAttempts"] for state in saved]
        self.assertEqual(attempts, sorted(attempts))
        self.assertEqual(attempts[-1], 24)
        resumed_clock = Clock(wall=2_000)
        resumed = resumed_clock.limiter(resume_state=saved[-1])
        resumed_clock.wall += 1
        resumed.record_attempt("search_movie")
        self.assertEqual(resumed.snapshot()["firstRequestAt"], 1_000)
        self.assertEqual(resumed.snapshot()["lastRequestAt"], 2_001)

    def test_another_worker_obeys_shared_429_cooldown(self):
        clock = Clock()
        limiter = clock.limiter(deadline=1_060)
        first = PacedSession(limiter, RecordingSession(clock, [Response(429, {"Retry-After": "90"})]))
        second_session = RecordingSession(clock, [Response()])
        second = PacedSession(limiter, second_session)
        with self.assertRaises(VerificationDeferred):
            first.get(self.url)
        with self.assertRaises(VerificationDeferred) as caught:
            second.get(self.url)
        self.assertEqual(caught.exception.retry_at, 1_090)
        self.assertEqual(second_session.calls, [])
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(limiter.snapshot()["wireAttempts"], 1)

    def test_new_429_during_another_workers_wait_extends_shared_pause(self):
        clock = Clock()
        limiter = clock.limiter(deadline=2_000)
        limiter.cooldown(10)
        clock.on_sleep = lambda: limiter.cooldown(100)
        session = RecordingSession(clock, [Response()])
        PacedSession(limiter, session).get(self.url)
        self.assertEqual(session.calls[0][0], 210)
        self.assertEqual(limiter.snapshot()["cooldownSeconds"], 110)

    def test_repeated_429_cannot_run_past_the_batch_deadline(self):
        clock = Clock()
        replies = [Response(429, {"Retry-After": "1"}) for _ in range(4)]
        session = RecordingSession(clock, replies)
        limiter = clock.limiter(deadline=clock.now() + 2)
        with self.assertRaises(VerificationDeferred):
            PacedSession(limiter, session).get(self.url)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(clock.now(), 1_001)
        self.assertEqual(limiter.snapshot()["cooldownUntil"], 1_002)
        self.assertEqual(limiter.snapshot()["rateLimitRetries"], 1)
        self.assertTrue(all(response.closed for response in replies[:2]))

    def test_rate_gate_itself_defers_without_oversleeping_the_deadline(self):
        clock = Clock()
        limiter = clock.limiter(rate=1, deadline=clock.now() + .5)
        session = RecordingSession(clock, [Response()])
        worker = PacedSession(limiter, session)
        worker.get(self.url)
        with self.assertRaises(VerificationDeferred) as caught:
            worker.get(self.url)
        self.assertEqual(caught.exception.reason, "batch_deadline")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(len(session.calls), 1)

    def test_batch_deadline_already_expired_makes_no_request(self):
        clock = Clock()
        limiter = clock.limiter(deadline=clock.now() - 1)
        session = RecordingSession(clock, [])
        with self.assertRaises(VerificationDeferred):
            PacedSession(limiter, session).get(self.url)
        self.assertEqual(session.calls, [])
        self.assertEqual(limiter.remaining_seconds, 0)

    def test_wall_clock_changes_do_not_shorten_active_cooldown_or_deadline(self):
        clock = Clock()
        limiter = clock.limiter(deadline=clock.now() + 100)
        limiter.cooldown(50)
        clock.wall += 3_600
        session = RecordingSession(clock, [Response()])
        PacedSession(limiter, session).get(self.url)
        self.assertEqual(session.calls[0][0], 150)
        self.assertEqual(limiter.remaining_seconds, 50)

    def test_shorter_cooldown_cannot_replace_a_longer_shared_cooldown(self):
        clock = Clock()
        limiter = clock.limiter()
        limiter.cooldown(120)
        limiter.cooldown(1)
        self.assertEqual(limiter.snapshot()["cooldownUntil"], 1_120)
        self.assertEqual(limiter.snapshot()["cooldownSeconds"], 120)

    def test_retry_after_accepts_past_dates_zero_and_fallback_but_not_nan(self):
        for value in ("0", "-3", formatdate(900, usegmt=True)):
            self.assertEqual(retry_after_seconds(value, 1_000), 0)
        for value in (None, "", "invalid", "NaN", "Infinity"):
            self.assertEqual(retry_after_seconds(value, 1_000), 1)
        self.assertEqual(retry_after_seconds(None, 1_000, attempt=100), 60)
        self.assertEqual(retry_after_seconds("3600", 1_000), 3_600)

    def test_outer_network_and_5xx_retries_are_each_paced_and_counted(self):
        clock = Clock()
        limiter = clock.limiter(rate=10)
        session = RecordingSession(clock, [requests.Timeout("secret credential URL"), Response(503), Response()])
        worker = PacedSession(limiter, session)
        with self.assertRaises(requests.RequestException) as caught:
            worker.get(self.url)
        self.assertEqual(str(caught.exception), "TMDB verification request failed.")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(worker.get(self.url).status_code, 503)
        self.assertEqual(worker.get(self.url).status_code, 200)
        self.assertEqual(len(session.calls), 3)
        for before, after in zip(session.calls, session.calls[1:]):
            self.assertGreaterEqual(after[0] - before[0] + 1e-10, .1)
        stats = limiter.snapshot()
        self.assertEqual(stats["wireAttempts"], 3)
        self.assertEqual(stats["networkErrors"], 1)
        self.assertEqual(stats["retryableServerResponses"], 1)
        self.assertEqual(stats["statusCounts"], {"503": 1, "200": 1})

    def test_requests_args_are_forwarded_but_redirects_are_forced_off(self):
        clock = Clock()
        session = RecordingSession(clock, [Response(302, {"Location": "https://other.invalid"})])
        worker = PacedSession(clock.limiter(), session)
        headers = {"Authorization": "Bearer private"}
        params = {"query": "Some movie", "year": 2026}
        reply = worker.get(self.url, headers=headers, params=params, timeout=(5, 15), stream=True, allow_redirects=True)
        self.assertEqual(reply.status_code, 302)
        forwarded = session.calls[0][2]
        self.assertEqual(forwarded, {"headers": headers, "params": params, "timeout": (5, 15), "stream": True, "allow_redirects": False})
        self.assertEqual(len(session.calls), 1)

    def test_only_exact_https_tmdb_api_host_is_allowed_before_any_request(self):
        for url in ("http://api.themoviedb.org/3/movie/1", "https://api.themoviedb.org.evil.invalid/3/movie/1",
                    "https://private@api.themoviedb.org/3/movie/1", "https://api.themoviedb.org:443/3/movie/1",
                    "https://image.tmdb.org/3/movie/1", "https://api.themoviedb.org/3/movie/1#fragment",
                    "https://api.themoviedb.org/not-api/movie/1", "https://[invalid"):
            with self.subTest(url=url):
                clock = Clock()
                limiter = clock.limiter()
                session = RecordingSession(clock, [])
                with self.assertRaises(requests.RequestException) as caught:
                    PacedSession(limiter, session).get(url)
                self.assertNotIn(url, str(caught.exception))
                self.assertEqual(session.calls, [])
                self.assertEqual(limiter.snapshot()["wireAttempts"], 0)

    def test_safe_route_classes_do_not_include_ids_titles_or_api_keys(self):
        clock = Clock()
        limiter = clock.limiter()
        paths = ["/search/movie", "/search/tv", "/movie/999/images", "/tv/998", "/find/tt1234567", "/unknown/private-title"]
        session = RecordingSession(clock, [Response() for _ in paths])
        worker = PacedSession(limiter, session)
        for path in paths:
            worker.get("https://api.themoviedb.org/3" + path, params={"api_key": "private"})
        stats = limiter.snapshot()
        self.assertEqual(stats["routeCounts"], {"search_movie": 1, "search_tv": 1, "movie_images": 1, "tv_details": 1, "find_external_id": 1, "other": 1})
        for value in ('"999"', '"998"', "tt1234567", "private"):
            self.assertNotIn(value, json.dumps(stats))

    def test_corrupt_or_secret_bearing_resume_state_is_rejected(self):
        for state in ({"cooldownUntil": "NaN"}, {"wireAttempts": -1}, {"wireAttempts": 1.5},
                      {"schemaVersion": 2}, {"routeCounts": {"private-token": 1}},
                      {"statusCounts": {"secret": 1}}, {"waitSeconds": float("inf")}):
            with self.subTest(state=state):
                with self.assertRaises(ValueError) as caught:
                    Clock().limiter(resume_state=state)
                self.assertNotIn("private-token", str(caught.exception))

    def test_invalid_rate_or_deadline_is_rejected(self):
        for rate in (0, -1, True, "NaN", float("inf")):
            with self.assertRaises(ValueError):
                Clock().limiter(rate=rate)
        with self.assertRaises(ValueError):
            Clock().limiter(deadline=float("nan"))

    def test_session_ownership_is_preserved(self):
        clock = Clock()
        session = RecordingSession(clock, [])
        PacedSession(clock.limiter(), session).close()
        self.assertFalse(session.closed)
        with mock.patch("vod_verification_rate_limit.requests.Session") as create:
            with PacedSession(clock.limiter()):
                self.assertFalse(create.return_value.trust_env)
            create.return_value.close.assert_called_once()

    def test_existing_tmdb_client_uses_full_retry_after_and_true_wire_metrics(self):
        import vod_tmdb

        clock = Clock()
        limited, unavailable, good = Response(429, {"Retry-After": "60"}), Response(503), Response()
        session = RecordingSession(clock, [limited, unavailable, good])
        limiter = clock.limiter(deadline=1_500)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(vod_tmdb.time, "monotonic", clock.monotonic), mock.patch.object(vod_tmdb.time, "sleep", clock.sleep):
            client = vod_tmdb.TmdbClient(Path(directory), token="secret", session=PacedSession(limiter, session), max_elapsed_seconds=500)
            state, data = client._request("/movie/123")
        self.assertEqual((state, data), ("ok", {"id": 123}))
        self.assertEqual(session.calls[1][0] - session.calls[0][0], 60)
        self.assertEqual(client.stats["requests"], 2)
        self.assertEqual(limiter.snapshot()["wireAttempts"], 3)
        self.assertEqual(client.stats["retries"] + limiter.snapshot()["rateLimitRetries"], 2)
        self.assertTrue(all(response.closed for response in (limited, unavailable, good)))


if __name__ == "__main__":
    unittest.main()
