from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from vod_verification_checkpoint import Checkpoint, CheckpointMismatch  # noqa: E402

SNAPSHOT = {"generation": "2026-10-22T17:21:42Z", "codePolicy": "sha256:policy-v1"}


def entry(number=1, *, server="server_1", kind="movie", language="en", identity="constraint-complete-key", **extra):
    return {"contentId": f"{server}:{kind}:{number}", "serverId": server, "kind": kind,
            "item": {"title": "Example Film (2026)", "id": str(number)},
            "languages": [language], "identityKey": identity,
            "baseline": {"tmdbId": None, "metadataMatch": "no_match"}, **extra}


def matched(identifier="123"):
    return {"matchStatus": "matched_search", "tmdbId": identifier,
            "originalTitle": "Example Film", "releaseDate": "2026-10-01"}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "checkpoint.sqlite3"
        self.store = Checkpoint(self.path)
        self.addCleanup(self.store.close)

    def initialize(self, records=None, snapshot=None):
        return self.store.initialize(records if records is not None else [entry()], snapshot or SNAPSHOT)

    def result(self, content_id="server_1:movie:1", metadata=None, diagnostic=None, **kwargs):
        return self.store.record_result(content_id, metadata or matched(),
                                        diagnostic or {"reason": "matched_search"}, **kwargs)

    def test_frozen_input_resume_is_order_independent_and_preserves_completed_rows(self):
        first = self.initialize([entry(1), entry(2)])
        self.result()
        self.store.close()
        self.store = Checkpoint(self.path)
        self.addCleanup(self.store.close)
        resumed = self.initialize([entry(2), entry(1)])
        self.assertFalse(first["resumed"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(first["inputFingerprint"], resumed["inputFingerprint"])
        self.assertEqual([row["contentId"] for row in self.store.pending()], ["server_1:movie:2"])

    def test_changed_input_rejects_resume_without_mutating_prior_results(self):
        self.initialize()
        self.result(stats={"requests": 2})
        before = self.store.report()
        with self.assertRaises(CheckpointMismatch):
            self.initialize([entry(item={"title": "Different Film"})])
        self.assertEqual(self.store.get("server_1:movie:1")["result"]["metadata"], matched())
        self.assertEqual(self.store.report()["apiUsage"], before["apiUsage"])

    def test_changed_source_code_policy_or_attempt_policy_rejects_resume(self):
        self.initialize()
        with self.assertRaises(CheckpointMismatch):
            self.initialize(snapshot={**SNAPSHOT, "codePolicy": "different"})
        self.store.close()
        self.store = Checkpoint(self.path, max_attempts=4)
        self.addCleanup(self.store.close)
        with self.assertRaises(CheckpointMismatch):
            self.initialize()

    def test_incomplete_initialization_rolls_back_all_inputs_and_identity(self):
        with mock.patch.object(self.store, "_set_meta", side_effect=RuntimeError("interruption")):
            with self.assertRaises(RuntimeError):
                self.initialize([entry(1), entry(2)])
        self.assertIsNone(self.store.get_meta("snapshotIdentity"))
        self.assertEqual(self.store.report()["totals"]["total"], 0)
        self.assertFalse(self.initialize()["resumed"])

    def test_duplicate_logical_ids_and_conflicting_ids_reject_atomic_initialization(self):
        for records in ([entry(), entry()], [entry(serverId="server_2")], [entry(contentId="bad")]):
            with self.assertRaises(ValueError):
                self.initialize(records)
            self.assertEqual(self.store.report()["totals"]["total"], 0)

    def test_published_match_flag_alone_does_not_skip_cache_validation(self):
        baseline = {"tmdbId": "123", "metadataMatch": {"status": "matched_id"},
                    "title": "Example Film", "releaseDate": "2026-10-01"}
        self.initialize([entry(baseline=baseline)])
        self.assertEqual(len(self.store.pending()), 1)
        self.result()
        totals = self.store.report()["totals"]
        self.assertEqual(totals["previouslyVerified"], 1)
        self.assertEqual(totals["newMatches"], 0)
        self.assertEqual(totals["retainedVerified"], 1)

    def test_validated_cache_can_be_seeded_without_requests(self):
        self.initialize([entry(baselineStatus="matched_search", baselineMetadata=matched(), baselineVerified=True)])
        report = self.store.report()
        self.assertTrue(report["complete"])
        self.assertEqual(report["totals"]["reusedVerifiedCaches"], 1)
        self.assertEqual(report["apiUsage"], {})
        self.assertEqual(self.store.pending(), [])

    def test_invalid_verified_seed_is_rejected(self):
        with self.assertRaises(ValueError):
            self.initialize([entry(baselineStatus="matched_id", baselineVerified=True)])

    def test_new_matches_are_compared_with_actual_verified_baseline_ids(self):
        self.initialize([entry(baseline={"tmdbId": None, "metadataMatch": "matched_id"})])
        self.result()
        self.assertEqual(self.store.report()["totals"]["newMatches"], 1)

    def test_definitive_no_match_finishes_only_this_frozen_pass(self):
        self.initialize()
        self.result(metadata={"matchStatus": "no_match", "tmdbId": None},
                    diagnostic={"reason": "ambiguous_title_year"}, stats={"requests": 3})
        report = self.store.report()
        self.assertTrue(report["complete"])
        self.assertEqual(report["totals"]["definitiveUnresolved"], 1)
        self.assertEqual(report["unresolvedReasons"], {"ambiguous_title_year": 1})
        self.assertEqual(self.store.pending(now=1e10), [])
        other = Checkpoint(self.path.parent / "next-pass.sqlite3")
        self.addCleanup(other.close)
        other.initialize([entry()], SNAPSHOT)
        self.assertEqual(len(other.pending()), 1)

    def test_transient_error_retries_are_durable_due_and_bounded_per_pass(self):
        self.initialize()
        unavailable = {"matchStatus": "unavailable", "tmdbId": None}
        for attempt, timestamp in enumerate((100, 105, 115), start=1):
            self.result(metadata=unavailable, diagnostic={"reason": "unavailable"},
                        stats={"requests": 1, "errors": 1}, now=timestamp)
            row = self.store.get("server_1:movie:1")["checkpoint"]
            self.assertEqual(row["attempts"], attempt)
            self.assertEqual(row["state"], "retry" if attempt < 3 else "done")
            if attempt < 3:
                self.assertEqual(self.store.pending(now=timestamp), [])
                self.assertEqual(len(self.store.pending(now=row["nextAttemptAt"])), 1)
        report = self.store.report()
        self.assertTrue(report["complete"])
        self.assertEqual(report["totals"]["transientUnresolved"], 1)
        self.assertEqual(report["byStatus"], {"unavailable": 1})
        self.assertEqual(report["apiUsage"]["requests"], 3)

    def test_unavailable_cannot_become_definitive_by_override(self):
        self.initialize()
        self.result(metadata={"matchStatus": "unavailable"}, diagnostic={"reason": "unavailable"},
                    transient=False)
        self.assertFalse(self.store.report()["complete"])

    def test_incomplete_search_remains_transient_not_definitive_no_match(self):
        self.initialize()
        self.result(metadata={"matchStatus": "no_match"}, diagnostic={"reason": "incomplete_search"})
        self.assertFalse(self.store.report()["complete"])
        self.assertEqual(self.store.get("server_1:movie:1")["checkpoint"]["state"], "retry")
        self.assertEqual(self.store.report()["byStatus"], {"unavailable": 1})
        self.assertEqual(self.store.get("server_1:movie:1")["result"]["metadata"]["matchStatus"], "unavailable")

    def test_defer_persists_cooldown_without_spending_an_attempt(self):
        self.initialize()
        self.store.defer("server_1:movie:1", "rate_limited", 150, now=100,
                         stats={"requests": 1, "rateLimited": 1}, elapsed=2)
        self.assertEqual(self.store.pending(now=149), [])
        row = self.store.pending(now=150)[0]
        self.assertEqual(row["checkpoint"]["attempts"], 0)
        self.store.close()
        self.store = Checkpoint(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.report(now=100)["nextAttemptAt"], 150)
        self.assertEqual(self.store.report()["apiUsage"]["rateLimited"], 1)

    def test_batch_deadlines_cannot_exhaust_real_attempts(self):
        self.initialize()
        for _ in range(5):
            self.result(metadata={"matchStatus": "unavailable"},
                        diagnostic={"reason": "batch_deadline"}, retry_after=30, now=100)
        row = self.store.get("server_1:movie:1")["checkpoint"]
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["state"], "retry")

    def test_pending_prefers_never_attempted_rows_before_due_retries(self):
        self.initialize([entry(1), entry(2)])
        self.result(metadata={"matchStatus": "unavailable"}, diagnostic={"reason": "unavailable"}, now=1)
        rows = self.store.pending(now=100)
        self.assertEqual([row["contentId"] for row in rows], ["server_1:movie:2", "server_1:movie:1"])
        self.assertEqual(len(self.store.pending(now=100, include_transient_retry=False)), 1)

    def test_group_result_charges_requests_once_but_updates_all_content_ids(self):
        records = [entry(), entry(server="server_2"), entry(server="server_3")]
        self.initialize(records)
        ids = [row["contentId"] for row in records]
        self.result(ids, stats={"requests": 3, "cacheHits": 1}, elapsed=4.5)
        report = self.store.report()
        self.assertEqual(report["totals"]["newMatches"], 3)
        self.assertEqual(report["apiUsage"], {"requests": 3, "cacheHits": 1})
        self.assertEqual(report["activeElapsedSeconds"], 4.5)
        self.assertEqual(report["attemptEvents"], 1)

    def test_pending_interleaves_all_six_server_and_kind_groups(self):
        records = [entry(number, server=f"server_{server}", kind=kind)
                   for server in range(1, 4) for kind in ("movie", "series") for number in (1, 2)]
        self.initialize(records)
        rows = self.store.pending(limit=6)
        self.assertEqual({(row["serverId"], row["kind"]) for row in rows},
                         {(f"server_{server}", kind) for server in range(1, 4) for kind in ("movie", "series")})
        self.assertTrue(all(row["contentId"].endswith(":1") for row in rows))

    def test_pending_group_expands_beyond_batch_selection_but_excludes_done_and_future_retries(self):
        records = [entry(number) for number in range(1, 6)] + [entry(6, identity="different-key")]
        self.initialize(records)
        self.result("server_1:movie:1")
        self.store.defer("server_1:movie:2", "rate_limited", 200, now=100)
        self.store.defer("server_1:movie:3", "rate_limited", 150, now=100)
        selected = self.store.pending(limit=1, now=160)
        self.assertEqual(len(selected), 1)
        group = self.store.pending_group(selected[0]["identityKey"], now=160)
        self.assertEqual({row["contentId"] for row in group},
                         {"server_1:movie:3", "server_1:movie:4", "server_1:movie:5"})
        self.result([row["contentId"] for row in group], metadata={"matchStatus": "no_match"},
                    diagnostic={"reason": "no_search_results"}, stats={"requests": 1})
        self.assertEqual(self.store.pending_group("constraint-complete-key", now=160), [])
        self.assertEqual([row["contentId"] for row in self.store.pending_group("constraint-complete-key", now=200)],
                         ["server_1:movie:2"])
        self.assertEqual(len(self.store.pending_group("different-key", now=160)), 1)

    def test_completed_records_exposes_saved_results_without_skipping_revalidation(self):
        self.initialize([entry(1), entry(2), entry(3)])
        self.result()
        self.result("server_1:movie:2", metadata={"matchStatus": "no_match"}, diagnostic={"reason": "missing_identity"})
        completed = self.store.completed_records()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["result"]["metadata"]["tmdbId"], "123")
        self.assertEqual(completed[0]["item"], entry()["item"])
        self.assertEqual(len(self.store.completed_records(verified_only=False)), 2)
        self.assertEqual(self.store.count(), 3)
        self.assertEqual(len(self.store.records()), 3)

    def test_group_cannot_share_results_on_title_only_or_different_constraint_keys(self):
        self.initialize([entry(identity=None), entry(2, identity="different")])
        with self.assertRaises(ValueError):
            self.result(["server_1:movie:1", "server_1:movie:2"], stats={"requests": 4})
        self.assertEqual(self.store.report()["totals"]["remaining"], 2)
        self.assertEqual(self.store.report()["apiUsage"], {})

    def test_invalid_group_rolls_back_every_row_and_usage(self):
        self.initialize()
        with self.assertRaises(KeyError):
            self.result(["server_1:movie:1", "server_1:movie:999"], stats={"requests": 4})
        self.assertEqual(self.store.get("server_1:movie:1")["checkpoint"]["attempts"], 0)
        self.assertEqual(self.store.report()["apiUsage"], {})

    def test_exact_completed_replay_is_idempotent_but_different_result_cannot_replace_it(self):
        self.initialize()
        self.assertTrue(self.result(stats={"requests": 2}))
        self.assertFalse(self.result(stats={"requests": 2}))
        with self.assertRaises(ValueError):
            self.result(metadata=matched("456"))
        self.assertEqual(self.store.report()["apiUsage"]["requests"], 2)

    def test_new_poster_collision_revokes_only_poster_matches_and_keeps_usage(self):
        self.initialize([entry(1), entry(2), entry(3)])
        self.result(metadata={**matched(), "matchStatus": "matched_poster"},
                    diagnostic={"reason": "matched_poster"}, stats={"requests": 3}, elapsed=2)
        self.result("server_1:movie:2", stats={"requests": 1})
        self.result("server_1:movie:3", metadata={"matchStatus": "no_match"},
                    diagnostic={"reason": "ambiguous_title_year"}, stats={"requests": 2})
        self.assertTrue(self.store.report()["complete"])
        reopened = self.store.reopen(["server_1:movie:1", "server_1:movie:2", "server_1:movie:3"])
        self.assertEqual(reopened, 1)
        report = self.store.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["matched"], 1)
        self.assertEqual(report["totals"]["unresolved"], 1)
        self.assertEqual(report["apiUsage"]["requests"], 6)
        self.assertEqual(report["activeElapsedSeconds"], 2)
        row = self.store.pending()[0]
        self.assertEqual(row["contentId"], "server_1:movie:1")
        self.assertEqual(row["checkpoint"]["attempts"], 0)
        self.assertIsNone(self.store.get(row["contentId"])["result"]["metadata"])
        self.result(stats={"requests": 1})
        self.assertTrue(self.store.report()["complete"])
        self.assertEqual(self.store.report()["apiUsage"]["requests"], 7)

    def test_reopen_is_idempotent_and_unknown_group_rolls_back_revocation(self):
        self.initialize()
        self.result(metadata={**matched(), "matchStatus": "matched_poster"},
                    diagnostic={"reason": "matched_poster"})
        with self.assertRaises(KeyError):
            self.store.reopen(["server_1:movie:1", "server_1:movie:999"])
        self.assertTrue(self.store.report()["complete"])
        self.assertEqual(self.store.reopen("server_1:movie:1"), 1)
        self.assertEqual(self.store.reopen("server_1:movie:1"), 0)

    def test_report_breaks_down_server_kind_and_overlapping_language_tags(self):
        rows = [entry(languages=["en", "hi"]), entry(2, server="server_2", kind="series", language="pa"),
                entry(3, server="server_3", languages=[])]
        self.initialize(rows)
        self.result()
        self.result("server_2:series:2", metadata={"matchStatus": "no_match"}, diagnostic={"reason": "missing_identity"})
        report = self.store.report()
        self.assertEqual(report["byLanguage"]["en"]["newMatches"], 1)
        self.assertEqual(report["byLanguage"]["hi"]["newMatches"], 1)
        self.assertEqual(report["byLanguage"]["pa"]["unresolved"], 1)
        self.assertEqual(report["byLanguage"]["unknown"]["remaining"], 1)
        self.assertEqual(report["byKind"]["series"]["total"], 1)
        self.assertEqual(report["byServer"]["server_3"]["remaining"], 1)
        self.assertTrue(report["hasUnfinished"])
        json.dumps(report, allow_nan=False)

    def test_shared_rate_limiter_cooldown_and_stats_survive_restart(self):
        self.initialize()
        self.store.set_meta("limiterCooldownUntil", 200)
        self.store.set_meta("sessionStats", {"requests": 4, "http429": 1})
        self.store.close()
        self.store = Checkpoint(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.get_meta("limiterCooldownUntil"), 200)
        self.assertEqual(self.store.get_meta("sessionStats")["http429"], 1)
        with self.assertRaises(ValueError):
            self.store.set_meta("inputFingerprint", "replace")

    def test_json_bounds_nonfinite_values_invalid_statuses_and_labels_are_rejected(self):
        for bad in (entry(item={"title": "x" * (512 * 1024)}), entry(item={"value": math.nan}),
                    entry(item=None), entry(languages=["invalid!"])):
            with self.assertRaises(ValueError):
                self.initialize([bad])
        self.initialize()
        for kwargs in ({"stats": {"requests": math.inf}}, {"stats": {"request?token=secret": 1}},
                       {"elapsed": -1}, {"metadata": {"matchStatus": "matched_id", "tmdbId": True}},
                       {"diagnostic": {"reason": "https://private.invalid/?key=secret"}}):
            with self.assertRaises(ValueError):
                self.result(**kwargs)
        self.assertEqual(self.store.report()["totals"]["remaining"], 1)

    def test_threaded_independent_results_are_committed_without_lost_usage(self):
        records = [entry(number) for number in range(1, 21)]
        self.initialize(records)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.result, row["contentId"], stats={"requests": 1}) for row in records]
            for future in futures:
                self.assertTrue(future.result())
        self.assertEqual(self.store.report()["apiUsage"]["requests"], 20)
        self.assertTrue(self.store.report()["complete"])

    def test_closed_database_is_one_portable_json_free_sqlite_file(self):
        original = entry(item={"title": "ਪੰਜਾਬੀ हिन्दी", "year": "2026"})
        untouched = copy.deepcopy(original)
        self.initialize([original])
        self.result()
        self.assertEqual(original, untouched)
        self.store.close()
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ["checkpoint.sqlite3"])
        copy_path = self.path.parent / "copied.sqlite3"
        copy_path.write_bytes(self.path.read_bytes())
        with Checkpoint(copy_path) as restored:
            self.assertEqual(restored.get("server_1:movie:1")["item"]["title"], "ਪੰਜਾਬੀ हिन्दी")
            self.assertTrue(restored.report()["complete"])


if __name__ == "__main__":
    unittest.main()
