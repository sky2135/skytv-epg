from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vod_series_facts as facts
from vod_verification_rate_limit import SharedRateLimiter, VerificationDeferred


NOW = "2026-11-08T12:00:00Z"


class Clock:
    def __init__(self):
        self.wall, self.mono = 1000.0, 100.0
        self.sleeps = []

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.wall += seconds
        self.mono += seconds

    def limiter(self, *args, **kwargs):
        return SharedRateLimiter(*args, **kwargs, now=self.time, monotonic=self.monotonic, sleep=self.sleep)


class Response:
    def __init__(self, status=200, retry_after=None):
        self.status_code, self.closed = status, False
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls, self.closed = [], False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        self.closed = True


class Client:
    def __init__(self, *, statuses=None, session=None, wire=False):
        self.session = session if session is not None else Session()
        self.statuses = statuses or {}
        self.calls, self.budgets = [], []
        self.wire = wire

    def reset_elapsed_budget(self, seconds):
        self.budgets.append(seconds)

    def refresh_series_facts(self, item):
        self.calls.append(item["title"])
        if self.wire:
            response = self.session.get("https://api.themoviedb.org/3/tv/123", headers={"Authorization": "Bearer private-secret"})
            code = response.status_code
            response.close()
            self.last_series_facts_status = "available" if code == 200 else "failed"
        else:
            self.last_series_facts_status = self.statuses.get(item["title"], "available")
        return None if self.last_series_facts_status == "unverified" else {"tmdbId": "123"}

    def enrich(self, *_args, **_kwargs):
        raise AssertionError("Backfill must never match or search")


def item(title, **changes):
    return {"title": title, "name": title, "year": 2025, **changes}


class SeriesFactsBackfillTests(unittest.TestCase):
    def run_backfill(self, items, client, folder, clock=None, **kwargs):
        clock = clock or Clock()
        with mock.patch.object(facts.time, "time", side_effect=clock.time), \
                mock.patch.object(facts, "SharedRateLimiter", side_effect=clock.limiter):
            return facts.backfill_series_facts(items, client, Path(folder), NOW, **kwargs)

    def test_rotates_background_past_unverified_and_failed_rows_and_deduplicates(self):
        items = [item(f"Example {number}") for number in range(5)]
        ordered = sorted(items, key=lambda value: facts._identity(value)[0])
        statuses = {ordered[0]["title"]: "unverified", ordered[1]["title"]: "failed"}
        client = Client(statuses=statuses)
        with tempfile.TemporaryDirectory() as folder:
            first = self.run_backfill(items + [dict(items[0], serverId="server_3")], client, folder, limit=2)
            second = self.run_backfill(list(reversed(items)), client, folder, limit=2)
            third = self.run_backfill(items, client, folder, limit=2)
        self.assertEqual(client.calls, [value["title"] for value in ordered + ordered[:1]])
        self.assertEqual((first["unverified"], first["errors"], first["totalIdentities"]), (1, 1, 5))
        self.assertEqual(second["attempted"], 2)
        self.assertEqual(third["attempted"], 2)

    def test_named_priorities_and_versions_precede_background_and_fresh_hits_are_free(self):
        priorities = [item("Dupahiya"), item("Stranger Things"), item("Anupamaa Season 2", canonicalTitle="Anupamaa")]
        ordinary = [item("Ordinary A"), item("Ordinary B")]
        client = Client(statuses={value["title"]: "cache_hit" for value in priorities})
        with tempfile.TemporaryDirectory() as folder:
            summary = self.run_backfill(ordinary + list(reversed(priorities)), client, folder, limit=2)
        self.assertEqual(client.calls[:3], [value["title"] for value in priorities])
        self.assertEqual((summary["attempted"], summary["cacheHits"], summary["refreshed"]), (5, 3, 2))
        self.assertEqual(summary["remaining"], 0)

    def test_spin_off_titles_are_not_pinned_and_verified_id_duplicates_share_one_call(self):
        for title in ("Beyond Stranger Things", "Anupamaa Namaste America", "Dupahiya Behind the Scenes"):
            self.assertEqual(facts._identity(item(title))[1], 3)
        duplicate = item("Stranger Things", tmdbId="66732")
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            summary = self.run_backfill([dict(duplicate, serverId="server_1"),
                                         dict(duplicate, serverId="server_2"),
                                         dict(duplicate, serverId="server_3")], client, folder)
            saved = (Path(folder) / "series-facts-progress.json").read_text()
        self.assertEqual(client.calls, ["Stranger Things"])
        self.assertEqual(summary["totalIdentities"], 1)
        self.assertNotIn("66732", saved)

    def test_explicit_edition_scheduling_keeps_canonical_titles_and_identity_keys(self):
        examples = {
            "Stranger Things - Hindi": "Stranger Things",
            "Stranger Things [Hindi Dubbed]": "Stranger Things",
            "Stranger Things (2025) (Hindi) [FHD]": "Stranger Things",
            "Dupahiya (Multi Audio)": "Dupahiya",
            "Dupahiya - Multi Audio": "Dupahiya",
            "Ｄｕｐａｈｉｙａ （Ｍｕｌｔｉ Ａｕｄｉｏ）": "Dupahiya",
            "Anupamaa {Hindi + English} (1080p)": "Anupamaa",
            "Anupamaa - Hindi Dubbed": "Anupamaa",
            "Anupamaa (Multi Audio) S02": "Anupamaa",
        }
        for title, canonical in examples.items():
            with self.subTest(title=title):
                candidate = item(title)
                before = dict(candidate)
                identity = facts._enrichment_identity(candidate, "series")[0]
                self.assertEqual(facts.priority_series_title(title), canonical)
                self.assertLess(facts.priority_series_rank(candidate), 3)
                self.assertEqual(facts._identity(candidate)[0], hashlib.sha256(identity.encode()).hexdigest())
                self.assertEqual(candidate, before)
        for title in ("Beyond Stranger Things (Hindi)", "Anupamaa Namaste America - Hindi",
                      "Dupahiya Behind the Scenes (Multi Audio)", "Anupamaa (Namaste America)",
                      "Stranger Things 2 (Hindi)", "Stranger Things Hindi", "Stranger Things (Netflix)"):
            with self.subTest(excluded=title):
                self.assertIsNone(facts.priority_series_title(title))
                self.assertEqual(facts.priority_series_rank(item(title)), 3)
        self.assertIsNone(facts.priority_series_title("Anupamaa" + " (Hindi)" * 1000))

    def test_client_request_budget_stops_before_invoking_another_identity(self):
        client = Client()
        client.max_requests, client.stats = 0, {"requests": 0}
        with tempfile.TemporaryDirectory() as folder:
            summary = self.run_backfill([item("A"), item("B")], client, folder)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(summary["attempted"], 0)
        self.assertIsNone(summary["cursor"])
        client.max_requests = 1
        original = client.refresh_series_facts

        def consume(item):
            client.stats["requests"] += 1
            return original(item)

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(client, "refresh_series_facts", side_effect=consume):
            summary = self.run_backfill([item("A"), item("B")], client, folder)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["deferred"], 1)

    def test_failed_or_unverified_priority_does_not_monopolize_the_next_tiny_batch(self):
        for status in ("failed", "unverified"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                client = Client(statuses={"Anupamaa": status})
                items = [item("Anupamaa"), item("Background")]
                clock = Clock()
                self.run_backfill(items, client, folder, clock, limit=1)
                second = self.run_backfill(items, client, folder, clock, limit=1)
                self.assertEqual(client.calls, ["Anupamaa", "Background"])
                self.assertEqual(second["refreshed"], 1)
                clock.wall += 86401
                self.run_backfill(items, client, folder, clock, limit=1)
                self.assertEqual(client.calls[-1], "Anupamaa")

    def test_shared_pacing_counts_wire_attempts_and_restores_without_closing_original(self):
        session = Session([Response(), Response()])
        client, clock = Client(session=session, wire=True), Clock()
        with tempfile.TemporaryDirectory() as folder:
            summary = self.run_backfill([item("A"), item("B")], client, folder, clock)
            saved = (Path(folder) / "series-facts-progress.json").read_text()
        self.assertIs(client.session, session)
        self.assertFalse(session.closed)
        self.assertEqual(client.budgets, [180])
        self.assertEqual(summary["wireRequests"], 2)
        self.assertAlmostEqual(sum(clock.sleeps), .05)
        self.assertNotIn("private-secret", saved)
        self.assertNotIn("https://", saved)
        self.assertNotIn('"A"', saved)

    def test_long_retry_after_survives_resume_and_keeps_cursor_until_processed(self):
        limited = Response(429, "600")
        session = Session([limited])
        client, clock = Client(session=session, wire=True), Clock()
        with tempfile.TemporaryDirectory() as folder:
            first = self.run_backfill([item("A")], client, folder, clock)
            saved = json.loads((Path(folder) / "series-facts-progress.json").read_bytes())
            self.assertEqual(saved["rateLimiter"]["cooldownUntil"], 1600)
            second_session = Session([])
            second = self.run_backfill([item("A")], Client(session=second_session, wire=True), folder, clock)
            self.assertEqual(second_session.calls, [])
            self.assertEqual(second["attempted"], 0)
            self.assertEqual(second["deferred"], 1)
            clock.wall, clock.mono = 1601, 701
            final_session = Session([Response()])
            final = self.run_backfill([item("A")], Client(session=final_session, wire=True), folder, clock)
        self.assertTrue(limited.closed)
        self.assertEqual(first["wireRequests"], 1)
        self.assertEqual(first["deferred"], 1)
        self.assertIsNone(first["cursor"])
        self.assertEqual(final["wireRequests"], 1)
        self.assertIsNotNone(final["cursor"])
        self.assertEqual(clock.sleeps, [])

    def test_auth_response_stops_before_advancing_or_touching_other_identities(self):
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                client = Client(session=Session([Response(status)]), wire=True)
                summary = self.run_backfill([item("A"), item("B")], client, folder)
            self.assertTrue(summary["authStopped"])
            self.assertEqual(summary["attempted"], 1)
            self.assertIsNone(summary["cursor"])
            self.assertEqual(summary["remaining"], 2)

    def test_persistence_errors_propagate_and_restore_the_session(self):
        client = Client(session=Session([Response()]), wire=True)
        original = client.session
        real_save, saves = facts._save, []

        def failing_save(path, state):
            saves.append(1)
            if len(saves) > 1:
                raise OSError("safe persistence failure")
            real_save(path, state)

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(facts, "_save", side_effect=failing_save):
            with self.assertRaises(OSError):
                self.run_backfill([item("A")], client, folder)
        self.assertIs(client.session, original)
        self.assertFalse(original.closed)

    def test_invalid_or_oversized_checkpoint_fails_closed_without_reflecting_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "series-facts-progress.json"
            path.write_text('{"private-secret":"https://private.invalid"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "^Invalid series facts progress.$"):
                self.run_backfill([item("A")], Client(), folder)
        with tempfile.TemporaryDirectory() as folder:
            summary = self.run_backfill([item("A")], Client(), folder, limit=0)
            self.assertEqual(summary["attempted"], 0)


if __name__ == "__main__":
    unittest.main()
