from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


NOW = 1_800_000_000


def movie(**changes):
    result = {"id": "77", "contentId": "server_1:movie:77", "title": "Example Film (2025)",
              "languages": ["en"], "releaseDate": None,
              "artwork": {"posterUrl": None, "backdropUrl": None}, "providerLastModified": None}
    result.update(changes)
    return result


def empty_search():
    return FakeResponse({"page": 1, "total_pages": 1, "total_results": 0, "results": []})


class UnmatchedPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, *responses, now=NOW, **kwargs):
        return tmdb.TmdbClient(self.root, token="offline-fixture-token", session=FakeSession(*responses),
                               now=now, **kwargs)

    def test_completed_miss_remains_provider_only_after_normal_ttl(self):
        target = movie()
        first = self.client(empty_search())
        original = copy.deepcopy(target)
        result = first.enrich(target, "movie")
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertEqual(set(result), set(tmdb._empty(None)))
        self.assertEqual(target, original)
        self.assertEqual(len(first.session.calls), 1)
        self.assertTrue(first.last_match_diagnostic["ignoredWithoutArtwork"])
        second = self.client(now=NOW + 10 * tmdb.MISS_TTL)
        before = copy.deepcopy(second.stats)
        self.assertTrue(second.ignored_without_artwork(target, "movie"))
        self.assertEqual(second.stats, before)
        self.assertEqual(second.enrich(target, "movie"), result)
        self.assertEqual(second.session.calls, [])
        self.assertEqual(second.last_match_diagnostic["reason"], "no_search_results")
        self.assertTrue(second.last_match_diagnostic["ignoredWithoutArtwork"])

    def test_missing_year_can_complete_without_http_but_unknown_record_cannot_stop(self):
        target = movie(title="The Trap")
        client = self.client()
        self.assertEqual(client.enrich(target, "movie")["matchStatus"], "no_match")
        self.assertTrue(client.ignored_without_artwork(target, "movie"))
        self.assertEqual(client.session.calls, [])
        for changed in ({"contentId": None}, {"id": "78"}, {"contentId": "bad"},
                        {"_serverId": "server_2"}, {"providerStreamId": "78"}):
            with self.subTest(changed=changed):
                self.assertIsNone(tmdb.unmatched_passthrough_fingerprint(dict(target, **changed), "movie"))
        synthetic = {"title": "Synthetic Film"}
        client.enrich(synthetic, "movie")
        self.assertFalse(client.ignored_without_artwork(synthetic, "movie"))

    def test_changes_reopen_even_while_original_miss_is_fresh(self):
        target = movie()
        changes = ({"providerLastModified": "2026-10-22T12:00:00Z"}, {"languages": ["hi"]},
                   {"releaseDate": "2025-01-01"}, {"title": "EXAMPLE Film (2025)"},
                   {"id": "78", "contentId": "server_1:movie:78"},
                   {"contentId": "server_2:movie:77"})
        for changed in changes:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(empty_search(), empty_search()), now=NOW)
                client.enrich(target, "movie")
                altered = dict(target, **changed)
                self.assertFalse(client.ignored_without_artwork(altered, "movie"))
                client.enrich(altered, "movie")
                self.assertEqual(len(client.session.calls), 2)

    def test_two_sources_keep_independent_stops_when_shared_key_is_overwritten(self):
        first = movie()
        second = movie(id="88", contentId="server_2:movie:88")
        self.assertEqual(tmdb._enrichment_identity(first, "movie")[0], tmdb._enrichment_identity(second, "movie")[0])
        client = self.client(empty_search(), empty_search())
        client.enrich(first, "movie")
        client.enrich(second, "movie")
        self.assertEqual(len(client.session.calls), 2)
        later = self.client(now=NOW + 100 * tmdb.MISS_TTL)
        for target in (first, second):
            self.assertTrue(later.ignored_without_artwork(target, "movie"))
            self.assertEqual(later.enrich(target, "movie")["matchStatus"], "no_match")
        self.assertEqual(later.session.calls, [])

    def test_any_poster_or_backdrop_excludes_policy_including_non_tmdb(self):
        for artwork in ({"posterUrl": "https://cdn.example.com/poster.jpg", "backdropUrl": None},
                        {"posterUrl": None, "backdropUrl": "https://cdn.example.com/backdrop.jpg"},
                        {"posterUrl": "https://image.tmdb.org/t/p/w500/abc123.jpg"}):
            with self.subTest(artwork=artwork), tempfile.TemporaryDirectory() as directory:
                target = movie(artwork=artwork)
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(*[empty_search() for _ in range(5)]), now=NOW)
                client.enrich(movie(), "movie")
                self.assertTrue(client.ignored_without_artwork(movie(), "movie"))
                self.assertIsNone(tmdb.unmatched_passthrough_fingerprint(target, "movie"))
                self.assertFalse(client.ignored_without_artwork(target, "movie"))
                client.enrich(target, "movie")
                first_attempts = len(client.session.calls)
                self.assertGreater(first_attempts, 1)
                client._now = NOW + 2 * tmdb.MISS_TTL
                client.enrich(target, "movie")
                self.assertGreater(len(client.session.calls), first_attempts)

    def test_disabled_budget_and_incomplete_failures_never_stop(self):
        for mode in ("disabled", "budget", "incomplete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="" if mode == "disabled" else "fixture",
                    max_requests=0 if mode == "budget" else 10, now=NOW,
                    session=FakeSession(FakeResponse({"page": 1, "total_pages": 9, "results": []})))
                result = client.enrich(movie(), "movie")
                self.assertEqual(result["matchStatus"], "unavailable")
                self.assertFalse(client.ignored_without_artwork(movie(), "movie"))
                self.assertFalse(client.last_match_diagnostic["ignoredWithoutArtwork"])

    def test_successful_id_match_and_stale_positive_win_over_stopped_proof(self):
        target = movie()
        client = self.client(empty_search())
        client.enrich(target, "movie")
        key = tmdb._enrichment_identity(target, "movie")[0]
        details = movie_details()
        metadata = tmdb._metadata(details, "movie", "matched_search")
        proof = tmdb._detail_identity(details, "movie")
        client.last_match_diagnostic = tmdb._match_diagnostic({"reason": "matched_search"})
        client._write_identity_cache(key, metadata, tmdb.DETAIL_TTL, proof)
        self.assertFalse(client.ignored_without_artwork(target, "movie"))
        self.assertEqual(client.enrich(target, "movie")["matchStatus"], "matched_search")
        later = self.client(now=NOW + tmdb.DETAIL_TTL + 1, max_requests=0)
        self.assertFalse(later.ignored_without_artwork(target, "movie"))
        self.assertEqual(later.enrich(target, "movie")["matchStatus"], "matched_search")
        direct = movie(tmdbId="101")
        matched = self.client(FakeResponse(details))
        self.assertEqual(matched.enrich(direct, "movie")["matchStatus"], "matched_id")
        self.assertFalse(matched.ignored_without_artwork(direct, "movie"))

    def test_series_never_gets_durable_policy(self):
        target = movie(contentId="server_1:series:77")
        client = self.client(empty_search())
        client.enrich(target, "series")
        self.assertFalse(client.ignored_without_artwork(target, "series"))
        self.assertIsNone(tmdb.unmatched_passthrough_fingerprint(target, "series"))

    def test_supplied_id_validated_evidence_and_reviewed_rule_reset_proof(self):
        target = movie()
        client = self.client(empty_search())
        client.enrich(target, "movie")
        self.assertFalse(client.ignored_without_artwork(dict(target, tmdbId="101"), "movie"))
        changed = dict(target, _identityEvidence={"contentId": target["contentId"], "fingerprint": "f" * 64,
            "imdbId": "tt1234567", "directors": [], "cast": []})
        self.assertFalse(client.ignored_without_artwork(changed, "movie"))
        correction = {"rejectIdentity": False, "metadata": {}, "rejectedTmdbIds": [],
                      "evidenceURLs": ["https://www.themoviedb.org/movie/101"]}
        with mock.patch.object(tmdb, "_reviewed", return_value=correction):
            self.assertFalse(client.ignored_without_artwork(target, "movie"))
            _, fresh, _ = client._identity_cache(*self.identity_args(target))
            self.assertFalse(fresh)

    def identity_args(self, target):
        key, identifier, title, year = tmdb._enrichment_identity(target, "movie")
        return key, target, "movie", identifier, title, year

    def test_policy_and_strategy_changes_reset_even_inside_ttl(self):
        for constant in ("UNMATCHED_PASSTHROUGH_POLICY", "MATCH_STRATEGY"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(empty_search(), empty_search()), now=NOW)
                target = movie()
                client.enrich(target, "movie")
                with mock.patch.object(tmdb, constant, getattr(tmdb, constant) + 1):
                    self.assertFalse(client.ignored_without_artwork(target, "movie"))
                    client.enrich(target, "movie")
                    self.assertEqual(len(client.session.calls), 2)

    def test_malformed_stop_envelopes_are_not_trusted_or_diagnostics_coerced(self):
        target = movie()
        client = self.client(empty_search())
        client.enrich(target, "movie")
        for path in client.cache_dir.glob("*.json"):
            envelope = json.loads(path.read_text())
            envelope["data"]["identity"]["ignoredWithoutArtwork"]["policyVersion"] = True
            path.write_text(json.dumps(envelope))
        self.assertFalse(client.ignored_without_artwork(target, "movie"))
        self.assertFalse(tmdb._match_diagnostic({"ignoredWithoutArtwork": "true"})["ignoredWithoutArtwork"])

    def test_optional_failure_reopens_bounded_miss_without_erasing_other_source(self):
        first = movie()
        second = movie(id="88", contentId="server_2:movie:88")
        client = self.client(empty_search(), empty_search())
        client.enrich(first, "movie")
        client.enrich(second, "movie")
        shared_key = tmdb._enrichment_identity(first, "movie")[0]
        shared_before = client._cache_path(shared_key).read_bytes()
        self.assertTrue(client.reopen_unmatched_without_artwork(first, "movie"))
        self.assertFalse(client.ignored_without_artwork(first, "movie"))
        self.assertTrue(client.ignored_without_artwork(second, "movie"))
        self.assertEqual(client._cache_path(shared_key).read_bytes(), shared_before)
        stopped_key = client._passthrough_key(shared_key, tmdb.unmatched_passthrough_fingerprint(first, "movie"))
        value, fresh = client._read_cache(stopped_key)
        self.assertTrue(fresh)
        self.assertEqual(value["metadata"]["matchStatus"], "no_match")
        self.assertEqual(value["matchDiagnostic"]["reason"], "no_search_results")
        self.assertNotIn("ignoredWithoutArtwork", value["identity"])
        self.assertFalse(client.last_match_diagnostic["ignoredWithoutArtwork"])
        self.assertFalse(client.reopen_unmatched_without_artwork(first, "movie"))
        later = self.client(empty_search(), now=NOW + 2 * tmdb.MISS_TTL)
        later.enrich(first, "movie")
        self.assertEqual(len(later.session.calls), 1)
        self.assertTrue(later.ignored_without_artwork(second, "movie"))

    def test_reopen_preserves_current_positive_and_original_rejection_state(self):
        target = movie(tmdbId="999")
        client = self.client(FakeResponse(status=404))
        result = client.enrich(target, "movie")
        self.assertTrue(result["rejectedIdentity"])
        self.assertTrue(client.reopen_unmatched_without_artwork(target, "movie"))
        key = tmdb._enrichment_identity(target, "movie")[0]
        value, _fresh = client._read_cache(key)
        self.assertTrue(value["metadata"]["rejectedIdentity"])
        self.assertEqual(value["matchDiagnostic"]["reason"], "title_mismatch")
        # A newer validated main match must survive cleanup of an older stop.
        plain = movie()
        client.session = FakeSession(empty_search())
        client.enrich(plain, "movie")
        key = tmdb._enrichment_identity(plain, "movie")[0]
        details = movie_details()
        client.last_match_diagnostic = tmdb._match_diagnostic({"reason": "matched_search"})
        client._write_identity_cache(key, tmdb._metadata(details, "movie", "matched_search"),
                                     tmdb.DETAIL_TTL, tmdb._detail_identity(details, "movie"))
        before = client._cache_path(key).read_bytes()
        self.assertTrue(client.reopen_unmatched_without_artwork(plain, "movie"))
        self.assertEqual(client._cache_path(key).read_bytes(), before)
        self.assertFalse(client.ignored_without_artwork(plain, "movie"))


if __name__ == "__main__":
    unittest.main()
