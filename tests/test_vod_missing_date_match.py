from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


NOW = 1_800_000_000
TMDB_ID = 1756427
TITLE = "The Bloodline of Bathory"
POSTER = "/eLUBCr6e4iSxzhhIhXqjam9ECXp.jpg"


def movie(**changes):
    item = {"id": "826646", "contentId": "server_1:movie:826646",
            "title": TITLE + " (2026) (English)", "languages": ["en"],
            "releaseDate": None, "tmdbId": None, "providerLastModified": None,
            "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w600_and_h900_bestv2" + POSTER,
                        "backdropUrl": None}}
    item.update(changes)
    return item


def search(*results, **changes):
    payload = {"page": 1, "total_pages": 1, "total_results": len(results), "results": list(results)}
    payload.update(changes)
    return FakeResponse(payload)


def candidate(**changes):
    result = {"id": TMDB_ID, "title": TITLE, "original_title": TITLE, "release_date": ""}
    result.update(changes)
    return result


def details(**changes):
    result = {"id": TMDB_ID, "title": TITLE, "original_title": TITLE,
              "original_language": "en", "release_date": "", "poster_path": POSTER,
              "overview": "A supernatural thriller.", "adult": False,
              "images": {"posters": [{"file_path": POSTER}]},
              "release_dates": {"results": []}, "alternative_titles": {"titles": []},
              "credits": {"cast": [], "crew": []}, "videos": {"results": []}}
    result.update(changes)
    return result


class MissingMovieDateMatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def client(self, *responses, now=NOW, **changes):
        options = {"token": "offline-fixture-token", "session": FakeSession(*responses), "now": now}
        options.update(changes)
        return tmdb.TmdbClient(self.cache, **options)

    def matched(self):
        client = self.client(search(), search(candidate()), FakeResponse(details()))
        result = client.enrich(movie(), "movie")
        self.assertEqual("matched_poster", result["matchStatus"])
        self.assertEqual(str(TMDB_ID), result["tmdbId"])
        self.assertIsNone(result["releaseDate"])
        return client, result

    def test_unique_exact_title_and_official_poster_match_without_inventing_date(self):
        item = movie()
        original = copy.deepcopy(item)
        client, result = self.matched()
        self.assertEqual(item, original)
        self.assertEqual(result["posterUrl"], "https://image.tmdb.org/t/p/w500" + POSTER)
        self.assertEqual(len(client.session.calls), 3)
        first, second, third = client.session.calls
        self.assertEqual(first[0], tmdb.API_ROOT + "/search/movie")
        self.assertEqual(first[1]["params"]["year"], "2026")
        self.assertEqual(second[0], tmdb.API_ROOT + "/search/movie")
        self.assertNotIn("year", second[1]["params"])
        self.assertEqual(second[1]["params"]["include_adult"], "true")
        self.assertEqual(third[0], tmdb.API_ROOT + "/movie/1756427")
        appended = set(third[1]["params"]["append_to_response"].split(","))
        self.assertTrue({"credits", "videos", "alternative_titles", "images", "release_dates"} <= appended)
        self.assertNotIn("external_ids", appended)
        key = tmdb._enrichment_identity(item, "movie")[0]
        envelope = json.loads(client._cache_path(key).read_text(encoding="utf-8"))["data"]
        self.assertIsNone(envelope["identity"]["year"])
        self.assertIsNone(envelope["metadata"]["releaseDate"])

    def test_verified_undated_cache_reuses_offline_with_no_http(self):
        _client, result = self.matched()
        replay = self.client(token="", max_requests=0)
        self.assertEqual(result, replay.enrich(movie(), "movie"))
        self.assertEqual([], replay.session.calls)
        self.assertEqual(replay.stats["requests"], 0)

    def test_source_year_or_poster_change_cannot_reuse_undated_proof(self):
        self.matched()
        variants = [movie(title=TITLE + " (2027) (English)"),
                    movie(artwork={"posterUrl": "https://image.tmdb.org/t/p/w500/different.jpg", "backdropUrl": None}),
                    movie(artwork={"posterUrl": None, "backdropUrl": None})]
        for item in variants:
            with self.subTest(item=item):
                replay = self.client(token="", max_requests=0)
                self.assertFalse(replay.enrich(item, "movie")["matchStatus"].startswith("matched_"))
                self.assertEqual(replay.stats["requests"], 0)

    def test_malformed_cached_date_or_unbound_old_poster_proof_cannot_be_reused(self):
        client, _result = self.matched()
        key = tmdb._enrichment_identity(movie(), "movie")[0]
        path = client._cache_path(key)
        original = json.loads(path.read_text(encoding="utf-8"))
        malformed = copy.deepcopy(original)
        malformed["data"]["metadata"]["releaseDate"] = "not-a-date"
        old_proof = copy.deepcopy(original)
        standard_keys = {"names", "year", "originalTitle", "tmdbId", "mediaKind",
                         "posterPaths", "posterPath", "posterPolicy"}
        old_proof["data"]["identity"] = {key: value for key, value in old_proof["data"]["identity"].items()
                                           if key in standard_keys}
        for envelope in (malformed, old_proof):
            with self.subTest(envelope=envelope):
                path.write_text(json.dumps(envelope), encoding="utf-8")
                replay = self.client(token="", max_requests=0)
                self.assertFalse(replay.enrich(movie(), "movie")["matchStatus"].startswith("matched_"))
                self.assertEqual(replay.stats["requests"], 0)

    def test_multiple_global_results_and_inexact_title_are_not_approved(self):
        responses = [search(candidate(), candidate(id=TMDB_ID + 1)),
                     search(candidate(), candidate(id=TMDB_ID + 1, title="Another Film", original_title="Another Film")),
                     search(candidate(title=TITLE + " II", original_title=TITLE + " II"))]
        for discovery in responses:
            with self.subTest(payload=discovery.chunks), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="fixture", now=NOW,
                    session=FakeSession(search(), discovery, FakeResponse(details())))
                result = client.enrich(movie(), "movie")
                self.assertFalse(result["matchStatus"].startswith("matched_"))
                self.assertIsNone(result["tmdbId"])

    def test_details_need_genuine_absent_dates_and_the_exact_official_poster(self):
        invalid = [
            {"poster_path": "/other.jpg", "images": {"posters": [{"file_path": "/other.jpg"}]}},
            {"title": TITLE + " II", "original_title": TITLE + " II"},
            {"release_date": "2026-10-16"},
            {"release_date": "not-a-date"},
            {"release_date": "2026-99-99"},
            {"release_dates": {"results": [{"iso_3166_1": "IN", "release_dates": [
                {"release_date": "2026-10-16T00:00:00.000Z", "type": 3}]}]}},
            {"release_dates": {"results": "invalid"}},
            {"release_dates": None},
            {"images": {"posters": "invalid"}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="fixture", now=NOW,
                    session=FakeSession(search(), search(candidate()), FakeResponse(details(**changes))))
                result = client.enrich(movie(), "movie")
                self.assertFalse(result["matchStatus"].startswith("matched_"))
                self.assertIsNone(result["tmdbId"])

    def test_provider_id_and_reviewed_rejected_id_constraints_still_apply(self):
        supplied = self.client(FakeResponse(details()))
        self.assertFalse(supplied.enrich(movie(tmdbId="999"), "movie")["matchStatus"].startswith("matched_"))
        correction = {"rejectIdentity": False, "rejectedTmdbIds": [str(TMDB_ID)], "metadata": {},
                      "evidenceURLs": ["https://www.themoviedb.org/movie/1756427"]}
        with mock.patch.object(tmdb, "_reviewed", return_value=correction):
            rejected = self.client(search(), search(candidate()), FakeResponse(details()))
            result = rejected.enrich(movie(), "movie")
        self.assertFalse(result["matchStatus"].startswith("matched_"))
        self.assertIsNone(result["tmdbId"])

    def test_known_official_poster_collision_cannot_be_overridden_by_unique_search(self):
        client = self.client(search(), search(candidate()), FakeResponse(details()))
        for identifier in (str(TMDB_ID), str(TMDB_ID + 1)):
            client.poster_index.add("movie", identifier, {POSTER}, {"names": [tmdb._normalized_title(TITLE)], "year": None})
        self.assertTrue(client.poster_index.collided(POSTER))
        self.assertFalse(client.enrich(movie(), "movie")["matchStatus"].startswith("matched_"))

    def test_partial_discovery_and_exhausted_detail_budget_remain_unavailable(self):
        partial = self.client(search(), search(candidate(), total_pages=2, total_results=1),
                              FakeResponse(status=403))
        self.assertEqual("unavailable", partial.enrich(movie(), "movie")["matchStatus"])
        with tempfile.TemporaryDirectory() as directory:
            exhausted = tmdb.TmdbClient(Path(directory), token="fixture", now=NOW, max_requests=2,
                session=FakeSession(search(), search(candidate())))
            self.assertEqual("unavailable", exhausted.enrich(movie(), "movie")["matchStatus"])
            self.assertEqual(2, exhausted.stats["requests"])

    def test_stale_verified_undated_positive_survives_temporary_refresh_failure(self):
        _client, result = self.matched()
        replay = self.client(now=NOW + tmdb.DETAIL_TTL + 1, max_requests=0)
        self.assertEqual(result, replay.enrich(movie(), "movie"))
        self.assertEqual(replay.stats["requests"], 0)
        self.assertGreater(replay.stats["staleFallbacks"], 0)

    def test_existing_dated_match_keeps_its_verified_date_without_fallback(self):
        existing = movie_details()
        client = self.client(search({"id": 101, "title": "Example Film", "release_date": "2025-04-03"}),
                             FakeResponse(existing))
        result = client.enrich({"title": "Example Film (2025)"}, "movie")
        self.assertEqual("matched_search", result["matchStatus"])
        self.assertEqual("2025-04-03", result["releaseDate"])
        self.assertEqual(2, len(client.session.calls))


if __name__ == "__main__":
    unittest.main()
