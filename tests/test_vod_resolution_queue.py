from __future__ import annotations

import copy
import base64
import csv
import html
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import quote

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vod_resolution_queue as resolution  # noqa: E402


NOW = "2026-10-21T12:00:00Z"
EPOCH = int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp())


def movie(identifier=1, title="Example Film (2026)", **extra):
    return {"id": str(identifier), "title": title, "languages": ["en"], **extra}


class ResolutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.cache = self.base / "private" / "queue.json"

    def queue(self, now=NOW, **kwargs):
        return resolution.ResolutionQueue(self.cache, policy_version=2, now=now, **kwargs)

    def row(self, queue, identifier=1, server="server_1", kind="movie"):
        return next(value for value in queue.entries.values()
                    if value["contentId"] == f"{server}:{kind}:{identifier}")

    def record(self, queue, reason, *, identifier=1, attempted=True, item=None, **diagnostic):
        return queue.record_result("server_1", "movie", item or movie(identifier),
                                   {"matchStatus": "unmatched"},
                                   {"reason": reason, **diagnostic}, attempted=attempted)

    def test_corrected_display_identity_and_provider_scope_do_not_mutate_source(self):
        queue = self.queue()
        item = movie(42, "Example Film (2026)", tmdbId="987")
        original = copy.deepcopy(item)
        triples = [("server_1", "movie", item), ("server_2", "movie", item),
                   ("server_1", "series", item)]
        selected = queue.select_due(triples)
        self.assertEqual(3, len(selected))
        self.assertIs(selected[0][2], item)
        self.assertEqual(original, item)
        self.assertEqual({"server_1:movie:42", "server_2:movie:42", "server_1:series:42"},
                         {row["contentId"] for row in queue.entries.values()})
        self.assertEqual("Example Film", self.row(queue, 42)["queryTitle"])
        self.assertEqual("2026", self.row(queue, 42)["queryYear"])

    def test_due_selection_rotates_even_when_budget_prevents_attempt_and_survives_restart(self):
        items = [("server_1", "movie", movie(i)) for i in range(1, 5)]
        queue = self.queue()
        self.assertEqual(["1", "2"], [item[2]["id"] for item in queue.select_due(items, limit=2)])
        self.assertTrue(queue.save())
        queue = self.queue()
        self.assertEqual(["3", "4"], [item[2]["id"] for item in queue.select_due(items, limit=2)])
        self.assertEqual(["1", "2"], [item[2]["id"] for item in queue.select_due(items, limit=2)])
        self.assertTrue(all(row["attempts"] == 0 and row["lastAttemptAt"] is None
                            for row in queue.entries.values()))

    def test_new_due_items_keep_caller_priority_then_persisted_rotation_wins(self):
        items = [("server_1", "movie", movie(i)) for i in (9, 2, 11, 1)]
        queue = self.queue()
        self.assertEqual(["9", "2"], [item[2]["id"] for item in queue.select_due(items, limit=2)])
        queue.save()
        queue = self.queue()
        self.assertEqual(["11", "1"], [item[2]["id"] for item in queue.select_due(items, limit=2)])
        # Once selected, persistent fairness overrides a later input reordering.
        self.assertEqual(["9", "2"], [item[2]["id"] for item in queue.select_due(list(reversed(items)), limit=2)])

    def test_retain_scope_prunes_only_removed_selected_scope_and_keeps_positive_history(self):
        queue = self.queue()
        for identifier in (1, 2, 3):
            self.record(queue, "title_mismatch", identifier=identifier)
        self.record(queue, "matched_search", identifier=4)
        queue.record_result("server_2", "movie", movie(2), {}, {"reason": "no_search_results"})
        queue.record_result("server_1", "series", movie(2), {}, {"reason": "no_search_results"})
        retained_positive = copy.deepcopy(self.row(queue, 4))
        self.assertEqual(2, queue.retain_scope("server_1", "movie", {"server_1:movie:1", "server_1:movie:4"}))
        expected = {"server_1:movie:1", "server_1:movie:4", "server_2:movie:2", "server_1:series:2"}
        self.assertEqual(expected, set(queue._by_content))
        self.assertEqual(expected, {row["contentId"] for row in queue.entries.values()})
        self.assertEqual(retained_positive, self.row(queue, 4))
        self.assertTrue(queue.save())
        queue = self.queue()
        self.assertEqual(expected, set(queue._by_content))
        self.assertEqual(2, queue.retain_scope("server_1", "movie", []))
        self.assertEqual({"server_2:movie:2", "server_1:series:2"}, set(queue._by_content))
        self.assertEqual(0, queue.retain_scope("server_1", "movie", []))

    def test_retain_scope_validates_all_ids_before_mutating(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        before = copy.deepcopy((queue.entries, queue._by_content, queue.stats, queue._selection_counter))
        invalid = [("server_1", "movie", {"server_1:movie:1", "server_2:movie:2"}),
                   ("server_1", "movie", {"server_1:series:1"}),
                   ("server_1", "movie", "server_1:movie:1"),
                   ("server_1", "movie", [None]), ("server_1", "movie", None),
                   ("server_1", "episode", []), ("bad-server", "movie", [])]
        for server_id, kind, ids in invalid:
            with self.subTest(server=server_id, kind=kind, ids=ids), self.assertRaises(ValueError):
                queue.retain_scope(server_id, kind, ids)
            self.assertEqual(before, (queue.entries, queue._by_content, queue.stats, queue._selection_counter))

    def test_only_current_source_scope_is_due_and_duplicate_input_deduplicates(self):
        queue = self.queue()
        first, second = movie(1), movie(2)
        queue.select_due([("server_2", "movie", second)])
        selected = queue.select_due([("server_1", "movie", first)] * 2)
        self.assertEqual([("server_1", "movie", first)], selected)
        self.assertEqual(2, len(queue.entries))

    def test_stable_failures_backoff_seven_fourteen_twenty_eight_days(self):
        queue = self.queue()
        for index, days in enumerate((7, 14, 28, 28, 28), start=1):
            now = EPOCH + index * 31 * resolution.DAY
            queue.now = now
            self.record(queue, "ambiguous_title_year", candidateIds=[10, 11])
            row = self.row(queue)
            self.assertEqual(index, row["attempts"])
            self.assertEqual(index, row["failureStreak"])
            self.assertEqual(now + days * resolution.DAY, row["nextAttemptAt"])
        queue.now = row["nextAttemptAt"] - 1
        self.assertEqual([], queue.select_due([("server_1", "movie", movie())]))
        queue.now += 1
        self.assertEqual(1, len(queue.select_due([("server_1", "movie", movie())])))

    def test_temporary_backoff_is_shorter_and_reason_group_change_resets_streak(self):
        queue = self.queue()
        for index, days in enumerate((1, 2, 4, 7, 7), start=1):
            queue.now = EPOCH + index * 31 * resolution.DAY
            self.record(queue, "unavailable")
            self.assertEqual(queue.now + days * resolution.DAY, self.row(queue)["nextAttemptAt"])
        self.record(queue, "title_mismatch")
        self.assertEqual(1, self.row(queue)["failureStreak"])
        self.assertEqual(queue.now + 7 * resolution.DAY, self.row(queue)["nextAttemptAt"])
        self.record(queue, "year_mismatch")
        self.assertEqual(2, self.row(queue)["failureStreak"])
        self.assertEqual(queue.now + 14 * resolution.DAY, self.row(queue)["nextAttemptAt"])

    def test_missing_identity_is_thirty_days_and_verified_success_resets_failures(self):
        queue = self.queue()
        self.record(queue, "missing_identity")
        self.assertEqual(EPOCH + 30 * resolution.DAY, self.row(queue)["nextAttemptAt"])
        self.record(queue, "matched_alias")
        self.assertEqual(0, self.row(queue)["failureStreak"])
        self.assertEqual(EPOCH + 14 * resolution.DAY, self.row(queue)["nextAttemptAt"])
        self.assertEqual(2, self.row(queue)["attempts"])

    def test_positive_cache_hit_schedules_refresh_without_counting_an_attempt(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        queue.now += resolution.DAY
        self.assertFalse(self.record(queue, "matched_search", attempted=False, cacheHit=True))
        row = self.row(queue)
        self.assertEqual(1, row["attempts"])
        self.assertEqual(EPOCH, row["lastAttemptAt"])
        self.assertEqual(0, row["failureStreak"])
        self.assertEqual(queue.now + 14 * resolution.DAY, row["nextAttemptAt"])
        self.assertTrue(queue.save())
        self.assertEqual([], self.queue(now=queue.now).select_due([("server_1", "movie", movie())]))

    def test_negative_cache_hit_does_not_extend_backoff_or_increment_attempts(self):
        queue = self.queue()
        self.record(queue, "no_search_results")
        original = dict(self.row(queue))
        queue.now += resolution.DAY
        self.assertFalse(self.record(queue, "no_search_results", attempted=False, cacheHit=True))
        row = self.row(queue)
        for key in ("attempts", "failureStreak", "lastAttemptAt", "nextAttemptAt"):
            self.assertEqual(original[key], row[key])
        new_item = movie(2)
        self.record(queue, "unavailable", item=new_item, attempted=False)
        self.assertEqual(0, self.row(queue, 2)["attempts"])
        self.assertEqual(queue.now, self.row(queue, 2)["nextAttemptAt"])

    def test_defer_cached_miss_only_withholds_future_unresolved_exact_identities(self):
        for reason in resolution.STABLE | {"missing_identity", "unavailable"}:
            with self.subTest(reason=reason):
                queue = self.queue()
                self.record(queue, reason)
                self.assertTrue(queue.defer_cached_miss("server_1", "movie", movie()))
                queue.now = self.row(queue)["nextAttemptAt"]
                self.assertFalse(queue.defer_cached_miss("server_1", "movie", movie()))
        for reason in resolution.MATCHED:
            queue = self.queue()
            self.record(queue, reason)
            self.assertFalse(queue.defer_cached_miss("server_1", "movie", movie()))
        queue = self.queue()
        queue.select_due([("server_1", "movie", movie())])
        self.assertFalse(queue.defer_cached_miss("server_1", "movie", movie()))

    def test_defer_unknown_content_returns_before_computing_matcher_identity(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        with mock.patch.object(queue, "_identity", side_effect=AssertionError("unknown identity was computed")):
            self.assertFalse(queue.defer_cached_miss("server_1", "movie", movie(2)))
            self.assertFalse(queue.defer_cached_miss("server_2", "movie", movie()))
            self.assertFalse(queue.defer_cached_miss("server_1", "series", movie()))
            self.assertFalse(queue.defer_cached_miss("server_1", "movie", None))

    def test_defer_is_read_only_and_source_evidence_changes_bypass_backoff(self):
        item = movie(_identityEvidence={"fingerprint": "first"}, providerEvidenceFingerprint="provider-first")
        queue = self.queue()
        self.record(queue, "ambiguous_title_year", item=item)
        queue.save()
        before = copy.deepcopy((queue.entries, queue._by_content, queue._selection_counter, queue.stats, item))
        cache_before = self.cache.read_bytes()
        self.assertTrue(queue.defer_cached_miss("server_1", "movie", item))
        for changes in ({"title": "Corrected Title (2026)"}, {"languages": ["fr"]},
                        {"tmdbId": "987"}, {"_identityEvidence": {"fingerprint": "second"}},
                        {"providerEvidenceFingerprint": "provider-second"},
                        {"contentId": "server_1:movie:2"}, {"title": "https://private.example"}):
            self.assertFalse(queue.defer_cached_miss("server_1", "movie", {**item, **changes}))
        self.assertEqual(before, (queue.entries, queue._by_content, queue._selection_counter, queue.stats, item))
        self.assertEqual(cache_before, self.cache.read_bytes())
        newer = resolution.ResolutionQueue(self.cache, policy_version=3, now=NOW)
        self.assertFalse(newer.defer_cached_miss("server_1", "movie", item))

    def test_temporary_diagnostic_wins_over_a_stale_positive_metadata_fallback(self):
        queue = self.queue()
        queue.record_result("server_1", "movie", movie(), {"matchStatus": "matched_search"},
                            {"reason": "unavailable"}, attempted=True)
        self.assertEqual("unavailable", self.row(queue)["reason"])
        self.assertEqual(EPOCH + resolution.DAY, self.row(queue)["nextAttemptAt"])

    def test_display_language_matcher_id_and_evidence_fingerprint_changes_reset_state(self):
        variants = [movie(title="Corrected Film (2026)"), movie(languages=["fr"]),
                    movie(tmdbId="123"), movie(_identityEvidence={"fingerprint": "new"}),
                    movie(providerEvidenceFingerprint="new")]
        for item in variants:
            with self.subTest(item=item):
                queue = self.queue()
                self.record(queue, "ambiguous_title_year")
                selected = queue.select_due([("server_1", "movie", item)])
                self.assertEqual(1, len(selected))
                self.assertEqual(1, len(queue.entries))
                self.assertEqual(0, self.row(queue)["attempts"])
                self.assertEqual(1, queue.stats["fingerprintResets"])

    def test_raw_provider_evidence_changes_without_fingerprint_change_are_not_persisted(self):
        private_url = "https://private.example/provider?password=secret"
        item = movie(_identityEvidence={"fingerprint": "stable", "raw": private_url},
                     providerBlob={"username": "private-user"})
        queue = self.queue()
        self.record(queue, "ambiguous_title_year", item=item)
        changed = copy.deepcopy(item)
        changed["_identityEvidence"]["raw"] = "different untrusted extra"
        self.assertEqual([], queue.select_due([("server_1", "movie", changed)]))
        self.assertEqual(0, queue.stats["fingerprintResets"])
        self.assertTrue(queue.save())
        queue.write_reports(self.base / "reports")
        for path in (self.cache, self.base / "reports" / "resolution_queue.json",
                     self.base / "reports" / "resolution_queue.csv"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("private.example", text)
            self.assertNotIn("password", text)
            self.assertNotIn("private-user", text)
            self.assertNotIn("providerBlob", text)

    def test_policy_bump_invalidates_prior_scheduling_state(self):
        queue = self.queue()
        self.record(queue, "no_search_results")
        self.assertTrue(queue.save())
        newer = resolution.ResolutionQueue(self.cache, policy_version=3, now=NOW)
        self.assertEqual({}, newer.entries)
        self.assertEqual(1, newer.stats["discardedEntries"])
        self.assertEqual(1, len(newer.select_due([("server_1", "movie", movie())])))

    def test_candidate_ids_are_bounded_deduplicated_and_not_raw_response_data(self):
        queue = self.queue()
        self.record(queue, "ambiguous_title_year",
                    candidateIds=[123, "123", True, 0, -1, {}, "https://bad.example", "9999999999", "002", "456"])
        self.assertEqual(["123", "456"], self.row(queue)["candidateIds"])
        self.record(queue, "title_mismatch", candidateIds=list(range(1, 121)))
        self.assertEqual(100, len(self.row(queue)["candidateIds"]))
        self.assertEqual("100", self.row(queue)["candidateIds"][-1])

    def test_reports_are_whitelisted_and_csv_formula_values_are_escaped(self):
        queue = self.queue()
        item = movie(title="=Example Film (2026)", providerURL="https://private.example/secret")
        self.record(queue, "ambiguous_title_year", item=item, candidateIds=[123, 456],
                    apiURL="https://api.example?key=private", response={"password": "private"})
        queue.record_result("server_2", "series", movie(2), {"matchStatus": "matched_id"},
                            {"reason": "matched_id", "candidateIds": [789]}, attempted=False)
        output = self.base / "reports"
        summary = queue.write_reports(output)
        self.assertEqual(2, summary["recordCount"])
        self.assertEqual({"ambiguous_title_year": 1, "matched_id": 1}, summary["reasonCounts"])
        document = json.loads((output / summary["jsonFile"]).read_text(encoding="utf-8"))
        self.assertEqual(2, document["recordCount"])
        self.assertEqual("=Example Film (2026)", document["records"][0]["title"])
        self.assertEqual(["https://www.themoviedb.org/movie/123", "https://www.themoviedb.org/movie/456"],
                         document["records"][0]["sourceURLs"])
        self.assertEqual(["https://www.themoviedb.org/tv/789"], document["records"][1]["sourceURLs"])
        self.assertNotIn("sourceFingerprint", json.dumps(document))
        self.assertNotIn("private", json.dumps(document))
        with (output / summary["csvFile"]).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("'=Example Film (2026)", rows[0]["title"])
        self.assertEqual("'=Example Film", rows[0]["queryTitle"])
        self.assertEqual("123 | 456", rows[0]["candidateIds"])

    def test_unsafe_display_identity_is_rejected_before_registration(self):
        queue = self.queue(secret_values=("private-token",))
        invalid = [movie(title="https://private.example/file"), movie(title="bad\ntitle"),
                   movie(title="private-token film"), movie(title="x" * 301),
                   movie(contentId="server_2:movie:1"), movie(identifier="not-numeric")]
        self.assertEqual([], queue.select_due([("server_1", "movie", item) for item in invalid]))
        self.assertEqual(len(invalid), queue.stats["invalidInputs"])
        self.assertEqual({}, queue.entries)

    def test_secret_encodings_and_provider_hostname_cannot_enter_reports_or_cache(self):
        secret = "private-token&42"
        queue = self.queue(secret_values=(secret, "https://account.example:443/private"))
        variants = (secret, quote(secret, safe=""), quote(quote(secret, safe=""), safe=""),
                    base64.b64encode(secret.encode()).decode(), html.escape(secret),
                    "account.example")
        for index, value in enumerate(variants, start=1):
            self.assertEqual([], queue.select_due([("server_1", "movie", movie(index, value + " Film (2026)"))]))
        self.assertEqual(len(variants), queue.stats["invalidInputs"])
        self.assertEqual({}, queue.entries)
        self.assertTrue(queue.save())
        queue.write_reports(self.base / "reports")
        for path in (self.cache, self.base / "reports" / "resolution_queue.json",
                     self.base / "reports" / "resolution_queue.csv"):
            text = path.read_text(encoding="utf-8")
            self.assertFalse(any(value in text for value in variants))

    def test_report_checks_both_files_and_all_ancestors_before_any_write(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        directory = self.base / "reports"
        original = Path.is_symlink
        for linked in (self.base, directory / "resolution_queue.csv"):
            with self.subTest(linked=linked):
                def is_link(path):
                    return path == linked or original(path)
                with mock.patch.object(Path, "is_symlink", is_link), self.assertRaises(ValueError):
                    queue.write_reports(directory)
                self.assertFalse(directory.exists())

    def test_cache_symlink_ancestor_and_junction_destinations_are_refused(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        original = Path.is_symlink
        with mock.patch.object(Path, "is_symlink", lambda path: path == self.cache.parent or original(path)):
            self.assertFalse(queue.save())
        self.assertFalse(self.cache.exists())
        with mock.patch.object(Path, "is_junction", lambda path: path == self.base, create=True):
            with self.assertRaises(ValueError):
                queue.write_reports(self.base / "reports")

    def test_invalid_limits_and_timestamps_are_rejected(self):
        queue = self.queue()
        self.assertEqual([], queue.select_due([("server_1", "movie", movie())], limit=0))
        for limit in (-1, True, 1.5, 10_001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                queue.select_due([], limit=limit)
        for now in (True, -1, float("nan"), datetime(2026, 1, 1), "not-a-date"):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.queue(now=now)
        for version in (True, 0, -1, "2"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                resolution.ResolutionQueue(self.cache, policy_version=version, now=NOW)

    def test_corrupt_cache_cannot_inject_raw_fields_or_unbounded_schedule(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        self.assertTrue(queue.save())
        original = json.loads(self.cache.read_text(encoding="utf-8"))
        mutations = [lambda row: row.update(providerURL="https://private.example"),
                     lambda row: row.update(title=None), lambda row: row.update(queryYear=2026),
                     lambda row: row.update(candidateIds=[{}]), lambda row: row.update(reason=[]),
                     lambda row: row.update(nextAttemptAt=EPOCH + 365 * resolution.DAY)]
        for mutation in mutations:
            document = copy.deepcopy(original)
            mutation(next(iter(document["entries"].values())))
            self.cache.write_text(json.dumps(document), encoding="utf-8")
            restored = self.queue()
            self.assertEqual({}, restored.entries)
            self.assertEqual(1, restored.stats["discardedEntries"])
        self.cache.write_text("not-json", encoding="utf-8")
        restored = self.queue()
        self.assertEqual({}, restored.entries)
        self.assertEqual(1, restored.stats["loadErrors"])

    def test_invalid_top_level_schema_is_rejected(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        queue.save()
        original = json.loads(self.cache.read_text(encoding="utf-8"))
        for field, value in (("schemaVersion", True), ("policyVersion", True), ("selectionCounter", -1)):
            document = copy.deepcopy(original)
            document[field] = value
            self.cache.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(1, self.queue().stats["loadErrors"])

    def test_queue_size_is_bounded_and_evicts_oldest_seen_source(self):
        queue = self.queue()
        self.record(queue, "title_mismatch", identifier=1)
        queue.now += 10
        self.record(queue, "title_mismatch", identifier=2)
        with mock.patch.object(resolution, "MAX_ENTRIES", 2):
            self.record(queue, "title_mismatch", identifier=3)
        self.assertEqual({"server_1:movie:2", "server_1:movie:3"},
                         {row["contentId"] for row in queue.entries.values()})
        self.assertEqual(1, queue.stats["evictedEntries"])

    def test_failed_atomic_save_preserves_existing_state_and_reports_error(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        self.assertTrue(queue.save())
        before = self.cache.read_bytes()
        self.record(queue, "matched_search")
        with mock.patch.object(Path, "replace", side_effect=OSError("denied")):
            self.assertFalse(queue.save())
        self.assertEqual(before, self.cache.read_bytes())
        self.assertEqual([], list(self.cache.parent.glob("*.tmp")))
        self.assertEqual(1, queue.stats["writeErrors"])

    def test_oversized_cache_is_not_read_and_oversized_save_preserves_prior_file(self):
        queue = self.queue()
        self.record(queue, "title_mismatch")
        self.assertTrue(queue.save())
        before = self.cache.read_bytes()
        with mock.patch.object(resolution, "MAX_CACHE_BYTES", 10):
            self.assertFalse(queue.save())
            restored = self.queue()
        self.assertEqual(before, self.cache.read_bytes())
        self.assertEqual({}, restored.entries)
        self.assertEqual(1, restored.stats["loadErrors"])


if __name__ == "__main__":
    unittest.main()
