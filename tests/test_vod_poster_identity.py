from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_tmdb as tmdb
import vod_poster_index as posters
from test_vod_tmdb import FakeResponse, FakeSession, movie_details, evidence_item, evidence_details

POSTER = "/jrhXbIOFingzdLjkccjg9vZnqIp.jpg"


def item(title="Example Film (2025)", path="/abc123.jpg", **changes):
    result = {"title": title, "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500" + path}}
    result.update(changes)
    return result


def search(*identifiers, title="Example Film", pages=1):
    return FakeResponse({"page": 1, "total_pages": pages, "total_results": len(identifiers),
        "results": [{"id": identifier, "title": title, "release_date": "2025-04-03"} for identifier in identifiers]})


def details(identifier=101, path="/abc123.jpg", *, alternates=(), **changes):
    result = movie_details(identifier)
    result.update(poster_path=path, images={"posters": [{"file_path": value} for value in alternates]})
    result.update(changes)
    return FakeResponse(result)


class PosterIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, session=None, **kwargs):
        return tmdb.TmdbClient(self.root, token="offline-fixture-token", session=session or FakeSession(), now=1_800_000_000, **kwargs)

    def seed(self, client, identifier=101, path="/abc123.jpg", kind="movie", title="example film", year="2025"):
        client.poster_index.add(kind, str(identifier), [path], {"names": [title], "year": year})

    def test_strict_public_tmdb_paths_only_and_resize_variants_share_identity(self):
        for url in ("https://image.tmdb.org/t/p/original" + POSTER,
                    "https://media.themoviedb.org/t/p/w300_and_h450_bestv2" + POSTER):
            self.assertEqual(tmdb.poster_identity({"artwork": {"posterUrl": url}}), POSTER)
        for url in ("http://image.tmdb.org/t/p/w500/a.jpg", "https://image.tmdb.org.evil.invalid/t/p/w500/a.jpg",
                    "https://user:password@image.tmdb.org/t/p/w500/a.jpg", "https://image.tmdb.org:8443/t/p/w500/a.jpg",
                    "https://image.tmdb.org/t/p/w500/a.jpg?token=x", "https://image.tmdb.org/t/p/w500/a.jpg#x",
                    "https://image.tmdb.org/t/p/w500/../a.jpg", "https://image.tmdb.org/t/p/w500/%61.jpg",
                    "/a.jpg", "//image.tmdb.org/t/p/w500/a.jpg", "https://example.invalid/a.jpg"):
            with self.subTest(url=url):
                self.assertIsNone(tmdb.poster_identity({"artwork": {"posterUrl": url}}))

    def test_actual_oxford_record_uses_verified_index_and_current_official_id(self):
        oxford = {**movie_details(1307078), "title": "My Oxford Year", "original_title": "My Oxford Year",
                  "poster_path": POSTER, "release_date": "2025-07-31"}
        session = FakeSession(FakeResponse(oxford), FakeResponse(oxford))
        client = self.client(session)
        client.enrich({"tmdbId": "1307078", "title": "My Oxford Year (2025)"}, "movie")
        target = item("My Oxford Year (2025) (English)", POSTER, id="826840", contentId="server_1:movie:826840")
        result = client.enrich(target, "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("1307078", "matched_poster"))
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(call[0].endswith("/movie/1307078") for call in session.calls))
        self.assertIn("images", session.calls[-1][1]["params"]["append_to_response"])

    def test_alternate_official_poster_is_valid_but_unrelated_artwork_is_not(self):
        client = self.client(FakeSession(details(path="/new-default.jpg", alternates=["/abc123.jpg"])))
        self.seed(client)
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "matched_poster")
        changed = item(path="/unrelated.jpg")
        self.assertIsNone(client.poster_candidate(changed, "movie"))
        self.assertNotEqual(tmdb._enrichment_identity(item(), "movie")[0], tmdb._enrichment_identity(changed, "movie")[0])

    def test_complete_same_title_year_tie_requires_unique_official_poster(self):
        session = FakeSession(search(101, 102), details(101, "/different.jpg"),
                              details(102, "/other-default.jpg", alternates=["/abc123.jpg"]))
        result = self.client(session).enrich(item(), "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("102", "matched_poster"))
        self.assertEqual(len(session.calls), 3)
        self.assertTrue(all("images" in call[1]["params"]["append_to_response"] for call in session.calls[1:]))

    def test_shared_or_absent_posters_do_not_resolve_ambiguous_titles(self):
        for second in ("/abc123.jpg", "/different.jpg"):
            with self.subTest(second=second):
                path = "/different.jpg" if second == "/different.jpg" else "/abc123.jpg"
                client = self.client(FakeSession(search(101, 102), details(101, path), details(102, second)))
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "no_match")

    def test_incomplete_search_or_alternate_images_never_establish_uniqueness(self):
        client = self.client(FakeSession(search(101, 102, pages=9)))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
        self.assertEqual(client.stats["requests"], 1)
        client = self.client(FakeSession(search(101, 102), details(101), details(102, "/other.jpg", images=None)))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
        self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")

    def test_candidate_limit_and_kind_collisions_fail_closed(self):
        client = self.client(FakeSession(search(101, 102, 103, 104)))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "no_match")
        self.assertEqual(client.stats["requests"], 1)
        client = self.client(FakeSession(search(101, 102)))
        target = item(path="/cross-kind.jpg")
        self.seed(client, path="/cross-kind.jpg")
        self.seed(client, identifier=202, path="/cross-kind.jpg", kind="series")
        self.assertIsNone(client.poster_candidate(target, "movie"))
        self.assertEqual(client.enrich(target, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.stats["requests"], 1)

    def test_supplied_id_and_reviewed_rejected_id_are_hard_constraints(self):
        client = self.client(FakeSession(details(102)))
        self.seed(client)
        result = client.enrich(item(tmdbId="102"), "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("102", "matched_id"))
        client = self.client(FakeSession(search(101)))
        correction = {"rejectIdentity": True, "metadata": {}, "rejectedTmdbIds": ["101"]}
        with mock.patch.object(tmdb, "_reviewed", return_value=correction):
            self.assertIsNone(client.poster_candidate(item(), "movie"))
            self.assertIsNone(client.enrich(item(), "movie")["tmdbId"])

    def test_poster_never_overrides_current_title_year_or_provider_cast_evidence(self):
        client = self.client(FakeSession(FakeResponse(evidence_details(matches=False))))
        self.seed(client)
        target = {**evidence_item(), "artwork": item()["artwork"]}
        self.assertEqual(client.enrich(target, "movie")["matchStatus"], "no_match")
        self.assertIsNone(client.poster_candidate(item("Different Work (2025)"), "movie"))
        self.assertIsNone(client.poster_candidate(item("Example Film (1995)"), "movie"))
        self.assertIsNone(client.poster_candidate(item("Example Film"), "movie"))

    def test_old_positive_cache_reused_and_old_miss_does_not_mask_poster_rule(self):
        session = FakeSession(details())
        client = self.client(session)
        expected = client.enrich({"tmdbId": "101", "title": "Example Film (2025)"}, "movie")
        self.assertEqual(client.enrich(item(tmdbId="101"), "movie"), expected)
        self.assertEqual(len(session.calls), 1)
        old_key = tmdb._enrichment_identity({"title": "Example Film (2025)"}, "movie")[0]
        client._write_identity_cache(old_key, tmdb._empty(None, "no_match"), tmdb.MISS_TTL)
        client.session = FakeSession(details())
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "matched_poster")

    def test_new_index_candidate_can_retry_fresh_miss_once_and_preserve_backoff(self):
        client = self.client(FakeSession(search(), search()))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "no_match")
        self.seed(client)
        self.assertEqual(client.poster_retry_candidate(item(), "movie"), "101")
        client.session = FakeSession(details(release_date="1995-04-03"))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "no_match")
        client.enrichment_priority(item(), "movie")
        self.assertIsNone(client.poster_retry_candidate(item(), "movie"))
        client.session = FakeSession()
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "no_match")
        self.assertEqual(client.session.calls, [])

    def test_priority_pass_warms_verified_cache_without_promoting_existing_matches(self):
        source = self.client(FakeSession(details()))
        source.enrich({"tmdbId": "101", "title": "Example Film (2025)"}, "movie")
        reader = self.client()
        reader.poster_index = posters.VerifiedPosterIndex()
        self.assertEqual(reader.enrichment_priority(item(tmdbId="101"), "movie"), 3)
        self.assertEqual(reader.poster_candidate(item(), "movie"), "101")
        with mock.patch.object(reader, "_read_cache", side_effect=AssertionError("unexpected cache IO")):
            self.assertIsNone(reader.poster_retry_candidate(item(tmdbId="101"), "movie"))
            self.assertEqual(reader.poster_retry_candidate(item(), "movie"), "101")

    def test_verified_alternate_paths_persist_for_explicit_known_kind_warmup(self):
        client = self.client(FakeSession(details(path="/new.jpg", alternates=["/abc123.jpg"])))
        self.seed(client)
        client.enrich(item(), "movie")
        reader = self.client()
        reader.poster_index = posters.VerifiedPosterIndex()
        self.assertEqual(reader.warm_poster_index([item()], "movie"), 1)
        self.assertEqual(reader.poster_candidate(item(), "movie"), "101")
        self.assertIsNone(reader.poster_candidate(item(), "series"))
        self.assertEqual(reader.stats["requests"], 0)

    def test_cached_poster_proof_requires_exact_kind_path_and_no_known_collision(self):
        client = self.client(FakeSession(details()))
        self.seed(client)
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "matched_poster")
        key, identifier, title, year = tmdb._enrichment_identity(item(), "movie")
        original, _fresh = client._read_cache(key)
        for field, value in (("mediaKind", "series"), ("posterPath", "/wrong.jpg"), ("posterPolicy", 999)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["identity"][field] = value
                client._write_cache(key, changed, tmdb.DETAIL_TTL)
                self.assertIsNone(client._identity_cache(key, item(), "movie", identifier, title, year)[0])
        client._write_cache(key, original, tmdb.DETAIL_TTL)
        self.seed(client, identifier=202, kind="series")
        self.assertIsNone(client._identity_cache(key, item(), "movie", identifier, title, year)[0])

    def test_missing_poster_keeps_original_request_behavior_and_budgets_bound_assistance(self):
        client = self.client(FakeSession(search(101), details()))
        self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "matched_search")
        self.assertNotIn("images", client.session.calls[-1][1]["params"]["append_to_response"])
        client = self.client(FakeSession(), max_requests=0)
        target = item("Another Film (2025)", "/budget.jpg")
        self.seed(client, identifier=102, path="/budget.jpg", title="another film")
        self.assertEqual(client.enrich(target, "movie")["matchStatus"], "unavailable")
        self.assertEqual(client.session.calls, [])

    def test_punjabi_marker_is_decoration_without_country_to_language_inference(self):
        self.assertEqual(tmdb._title_and_year({"title": "Example Film (2025) (Punjabi)"}), ("Example Film", "2025"))
        target = {"title": "IN - Dangal (2016)", "languages": ["und"]}
        self.assertEqual(tmdb._title_and_year(target), ("Dangal", "2016"))
        self.assertEqual(target["languages"], ["und"])

    def test_shared_worker_index_is_thread_safe_and_keeps_collision_evidence_bounded(self):
        clients = [self.client() for _ in range(3)]
        self.assertIs(clients[0].poster_index, clients[1].poster_index)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda number: self.seed(clients[number % 3], identifier=100 + number), range(20)))
        self.assertIsNone(clients[0].poster_candidate(item(), "movie"))
        self.assertLessEqual(len(clients[0].poster_index._paths["/abc123.jpg"]), 5)


if __name__ == "__main__":
    unittest.main()
