from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import vod_tmdb as tmdb  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def movie_details(identifier=101):
    return {
        "id": identifier, "title": "Example Film", "original_title": "Original Film",
        "original_language": "hi", "overview": "A useful\nplot.",
        "release_date": "2025-04-03", "genres": [{"id": 18, "name": "Drama"}],
        "poster_path": "/abc123.jpg", "backdrop_path": "/def456.png",
        "vote_average": 8.2, "vote_count": 920, "popularity": 33.1, "runtime": 122,
        "status": "Released", "direct_source": "https://private.invalid/password",
        "adult": False,
        "credits": {
            "cast": [{"id": 9, "name": "Actor", "character": "A Character", "private": "secret"}],
            "crew": [{"name": "Director", "job": "Director"}, {"name": "Writer", "job": "Writer"}],
        },
        "videos": {"results": [
            {"site": "YouTube", "key": "abcdefghijk", "type": "Trailer", "official": True},
            {"site": "Other", "key": "https://unsafe.invalid", "type": "Trailer"},
        ]},
    }


class TmdbEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, session=None, **kwargs):
        return tmdb.TmdbClient(self.root, token="read-token-secret", session=session or FakeSession(), now=1_800_000_000, **kwargs)

    def test_direct_id_sanitizes_metadata_and_keeps_audio_availability_distinct(self):
        response = FakeResponse(movie_details())
        session = FakeSession(response)
        client = self.client(session)
        item = client.enrich({"tmdbId": "101", "title": "Provider title", "languages": ["en"]}, "movie")
        self.assertEqual(item["tmdbId"], "101")
        self.assertEqual(item["matchStatus"], "matched_id")
        self.assertEqual(item["originalLanguage"], "hi")
        self.assertIs(item["adult"], False)
        self.assertNotIn("languages", item)
        self.assertEqual(item["overview"], "A useful plot.")
        self.assertEqual(item["runtimeMinutes"], 122)
        self.assertEqual(item["directors"], ["Director"])
        self.assertEqual(item["cast"], [{"id": 9, "name": "Actor", "character": "A Character"}])
        self.assertEqual(item["posterUrl"], "https://image.tmdb.org/t/p/w500/abc123.jpg")
        self.assertEqual(item["trailerUrl"], "https://www.youtube.com/watch?v=abcdefghijk")
        url, options = session.calls[0]
        self.assertEqual(url, "https://api.themoviedb.org/3/movie/101")
        self.assertEqual(options["params"], {"append_to_response": "credits,videos"})
        self.assertEqual(options["headers"]["Authorization"], "Bearer read-token-secret")
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertTrue(response.closed)
        cached = "".join(path.read_text() for path in self.root.rglob("*.json"))
        self.assertNotIn("secret", cached)
        self.assertNotIn("private.invalid", cached)
        self.assertNotIn("api_key", cached)

    def test_exact_title_and_year_match_with_accent_normalization(self):
        session = FakeSession(
            FakeResponse({"total_pages": 1, "results": [
                {"id": 101, "title": "Café: Film", "release_date": "2025-04-03"},
                {"id": 102, "title": "Cafe Film", "release_date": "1995-04-03"},
                {"id": 103, "title": "Cafe Film Sequel", "release_date": "2025-04-03"},
            ]}),
            FakeResponse(movie_details()),
        )
        result = self.client(session).enrich({"title": "Cafe Film (2025)"}, "movie")
        self.assertEqual(result["tmdbId"], "101")
        self.assertEqual(result["matchStatus"], "matched_search")
        self.assertEqual(session.calls[0][1]["params"]["year"], "2025")

    def test_ambiguous_titles_mismatched_year_and_missing_year_fail_closed(self):
        ambiguous = [
            {"id": 101, "title": "Example Film", "release_date": "2025-04-03"},
            {"id": 102, "title": "Example Film", "release_date": "2025-06-03"},
        ]
        for payload in (
            {"total_pages": 1, "results": ambiguous},
            {"total_pages": 2, "results": ambiguous[:1]},
            {"total_pages": 1, "results": [{**ambiguous[0], "release_date": "2024-04-03"}]},
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(payload))
                client = tmdb.TmdbClient(Path(directory), token="key", session=session)
                self.assertEqual(client.enrich({"title": "Example Film", "year": 2025}, "movie")["matchStatus"], "no_match")
                self.assertEqual(len(session.calls), 1)
        session = FakeSession()
        self.assertEqual(self.client(session).enrich({"title": "Film"}, "movie")["matchStatus"], "no_match")
        self.assertFalse(session.calls)

    def test_series_details_and_calendar_never_imply_provider_episodes_exist(self):
        details = {
            "id": 301, "original_name": "Series", "original_language": "es",
            "first_air_date": "2024-01-01", "last_air_date": "2026-10-01",
            "episode_run_time": [0, 43], "number_of_seasons": 3, "number_of_episodes": 24,
            "next_episode_to_air": {"season_number": 3, "episode_number": 5, "air_date": "2026-10-22"},
        }
        session = FakeSession(FakeResponse(details))
        result = self.client(session).enrich({"tmdbId": 301}, "series")
        self.assertEqual(session.calls[0][0], tmdb.API_ROOT + "/tv/301")
        self.assertEqual(result["nextEpisode"], {"season": 3, "episode": 5, "airDate": "2026-10-22"})
        self.assertEqual(result["runtimeMinutes"], 43)
        self.assertEqual(result["seasonCount"], 3)
        self.assertEqual(result["episodeCount"], 24)
        self.assertNotIn("availableEpisodes", result)

    def test_metadata_cache_survives_new_client_without_key_and_is_kind_scoped(self):
        client = self.client(FakeSession(FakeResponse(movie_details())))
        expected = client.enrich({"tmdbId": 101}, "movie")
        session = FakeSession()
        offline = tmdb.TmdbClient(self.root, session=session, now=1_800_000_001)
        self.assertEqual(offline.enrich({"tmdbId": 101}, "movie"), expected)
        self.assertEqual(offline.stats["cacheHits"], 1)
        self.assertEqual(offline.enrich({"tmdbId": 101}, "series")["matchStatus"], "unavailable")
        self.assertFalse(session.calls)

    def test_stale_details_survive_transient_errors_and_budget_exhaustion(self):
        expected = self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101}, "movie")
        for index, (session, budget) in enumerate(((FakeSession(FakeResponse({}, 503), FakeResponse({}, 503), FakeResponse({}, 503)), 3), (FakeSession(), 0))):
            client = tmdb.TmdbClient(self.root, token="key", session=session, now=1_800_000_000 + tmdb.DETAIL_TTL + 1 + index * (tmdb.MISS_TTL + 1), max_requests=budget)
            with mock.patch.object(tmdb.time, "sleep"):
                self.assertEqual(client.enrich({"tmdbId": 101}, "movie"), expected)
            self.assertEqual(client.stats["staleFallbacks"], 1)
        cache_entry = json.loads(next(self.root.rglob("*.json")).read_text())
        self.assertEqual(cache_entry["data"], expected)

    def test_negative_cache_refreshes_after_one_day(self):
        session = FakeSession(FakeResponse({"results": [], "total_pages": 0}))
        self.client(session).enrich({"title": "Missing", "year": 2025}, "movie")
        session = FakeSession()
        fresh = tmdb.TmdbClient(self.root, token="key", session=session, now=1_800_000_000 + tmdb.MISS_TTL - 1)
        self.assertEqual(fresh.enrich({"title": "MISSING", "year": 2025}, "movie")["matchStatus"], "no_match")
        self.assertFalse(session.calls)
        expired_session = FakeSession(FakeResponse({"results": [], "total_pages": 0}))
        expired = tmdb.TmdbClient(self.root, token="key", session=expired_session, now=1_800_000_000 + tmdb.MISS_TTL + 1)
        expired.enrich({"title": "Missing", "year": 2025}, "movie")
        self.assertEqual(len(expired_session.calls), 1)

    def test_rate_limit_retries_are_bounded_and_count_against_budget(self):
        session = FakeSession(FakeResponse({}, 429, {"Retry-After": "999999"}), FakeResponse(movie_details()))
        client = self.client(session)
        with mock.patch.object(tmdb.time, "sleep") as sleep:
            self.assertEqual(client.enrich({"tmdbId": 101}, "movie")["matchStatus"], "matched_id")
        sleep.assert_called_once_with(tmdb.MAX_RETRY_DELAY)
        self.assertEqual(client.stats["requests"], 2)
        limited = self.client(FakeSession(FakeResponse({}, 429)), max_requests=1)
        with mock.patch.object(tmdb.time, "sleep"):
            self.assertEqual(limited.enrich({"tmdbId": 102}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(limited.stats["requests"], 1)
        self.assertEqual(limited.stats["budgetExhausted"], 1)

    def test_trending_fetches_at_most_five_pages_and_caches_rank_order(self):
        session = FakeSession(*[FakeResponse({"total_pages": 99, "results": [{"id": page * 10}, {"id": page * 10 + 1}]}) for page in range(1, 6)])
        client = self.client(session)
        ranking = client.trending("movie")
        self.assertEqual(len(session.calls), 5)
        self.assertEqual(ranking["10"], 1)
        self.assertEqual(ranking["51"], 10)
        self.assertEqual(session.calls[0][0], tmdb.API_ROOT + "/trending/movie/week")
        self.assertEqual(client.trending("movie"), ranking)
        self.assertEqual(len(session.calls), 5)

    def test_incomplete_trending_does_not_replace_a_stale_complete_cache(self):
        old_session = FakeSession(FakeResponse({"results": [{"id": 101}], "total_pages": 1}))
        expected = self.client(old_session).trending("series")
        new_session = FakeSession(FakeResponse({"results": [{"id": 202}], "total_pages": 4}), FakeResponse({}, 401))
        client = tmdb.TmdbClient(self.root, token="key", session=new_session, now=1_800_000_000 + tmdb.TRENDING_TTL + 1)
        self.assertEqual(client.trending("series"), expected)
        self.assertEqual(client.stats["staleFallbacks"], 1)

    def test_redirects_oversize_json_and_network_failures_never_escape_or_cache_secrets(self):
        responses = (
            FakeResponse({}, 302, {"Location": "https://private.invalid/key"}),
            FakeResponse({}, headers={"Content-Length": str(tmdb.MAX_RESPONSE_BYTES + 1)}),
            FakeResponse(chunks=[b"{" + b"x" * 100]),
            FakeResponse(chunks=[b"x" * (tmdb.MAX_RESPONSE_BYTES + 1)]),
        )
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="secret", session=FakeSession(response))
                self.assertEqual(client.enrich({"tmdbId": 101}, "movie")["matchStatus"], "unavailable")
                cached = next(Path(directory).rglob("*.json")).read_text()
                self.assertEqual(json.loads(cached)["data"]["matchStatus"], "unavailable")
                self.assertNotIn("secret", cached)
                self.assertNotIn("private.invalid", cached)
                self.assertTrue(response.closed)
        session = FakeSession(*[requests.ConnectionError("https://api.themoviedb.org?api_key=secret") for _ in range(3)])
        with mock.patch.object(tmdb.time, "sleep"):
            self.assertEqual(self.client(session).enrich({"tmdbId": 101}, "movie")["matchStatus"], "unavailable")

    def test_api_key_auth_and_no_key_gracefully_preserve_provider_id(self):
        session = FakeSession(FakeResponse(movie_details()))
        client = tmdb.TmdbClient(self.root, api_key="api-key-secret", session=session)
        client.enrich({"tmdbId": 101}, "movie")
        self.assertEqual(session.calls[0][1]["params"]["api_key"], "api-key-secret")
        self.assertNotIn("Authorization", session.calls[0][1]["headers"])
        offline_session = FakeSession()
        offline = tmdb.TmdbClient(self.root, session=offline_session)
        result = offline.enrich({"tmdbId": 999, "password": "private"}, "movie")
        self.assertEqual(result["tmdbId"], "999")
        self.assertEqual(result["matchStatus"], "unavailable")
        self.assertFalse(offline_session.calls)

    def test_unsafe_artwork_trailers_and_invalid_numbers_are_omitted(self):
        details = movie_details()
        details.update({"poster_path": "//evil.invalid/x.jpg", "backdrop_path": "/../private.jpg", "vote_average": float("nan"), "vote_count": True, "runtime": -3})
        details["videos"] = {"results": [{"site": "YouTube", "type": "Trailer", "key": "bad&token=secret"}]}
        result = self.client(FakeSession(FakeResponse(details))).enrich({"tmdbId": 101}, "movie")
        for key in ("posterUrl", "backdropUrl", "rating", "voteCount", "runtimeMinutes", "trailerUrl"):
            self.assertIsNone(result[key], key)

    def test_invalid_kinds_fail_with_a_generic_safe_error(self):
        with self.assertRaisesRegex(ValueError, "Metadata kind"):
            self.client().enrich({"password": "secret"}, "https://private.invalid")

    def test_elapsed_budget_stops_requests_and_preserves_stale_cache(self):
        expected = self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101}, "movie")
        session = FakeSession()
        with mock.patch.object(tmdb.time, "monotonic", return_value=100) as clock:
            client = tmdb.TmdbClient(self.root, token="key", session=session,
                                     now=1_800_000_000 + tmdb.DETAIL_TTL + 1,
                                     max_elapsed_seconds=300)
            clock.return_value = 401
            self.assertEqual(client.enrich({"tmdbId": 101}, "movie"), expected)
        self.assertFalse(session.calls)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["staleFallbacks"], 1)
        self.assertEqual(client.stats["elapsedBudgetExhausted"], 1)
        retry_session = FakeSession(FakeResponse({}, 429))
        with mock.patch.object(tmdb.time, "monotonic", side_effect=[0, 0, 301]), mock.patch.object(tmdb.time, "sleep"):
            retrying = self.client(retry_session)
            self.assertEqual(retrying.enrich({"tmdbId": 102}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(retrying.stats["requests"], 1)
        self.assertEqual(retrying.stats["elapsedBudgetExhausted"], 1)

    def test_restored_cache_is_revalidated_before_export(self):
        client = self.client(FakeSession(FakeResponse(movie_details())))
        expected = client.enrich({"tmdbId": 101}, "movie")
        cache_path = next(self.root.rglob("*.json"))
        entry = json.loads(cache_path.read_text())
        entry["data"]["posterUrl"] = "https://private.invalid/token"
        entry["data"]["cast"][0]["private"] = "secret"
        entry["data"]["adult"] = "false"
        cache_path.write_text(json.dumps(entry), encoding="utf-8")
        result = self.client().enrich({"tmdbId": 101}, "movie")
        self.assertIsNone(result["posterUrl"])
        self.assertIsNone(result["adult"])
        self.assertEqual(result["cast"], expected["cast"])
        self.assertNotIn("private.invalid", json.dumps(result))

    def test_enrichment_priority_favors_missing_before_stale_then_fresh_without_requests(self):
        self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101}, "movie")
        self.client(FakeSession(FakeResponse({"results": [], "total_pages": 0}))).enrich({"title": "Missing Film", "year": 2025}, "movie")
        older_session = FakeSession()
        older = tmdb.TmdbClient(self.root, token="key", session=older_session, now=1_800_000_000 + tmdb.DETAIL_TTL + 1)
        initial_stats = dict(older.stats)
        self.assertEqual(older.enrichment_priority({"tmdbId": 999}, "movie"), 0)
        self.assertEqual(older.enrichment_priority({"tmdbId": 101}, "movie"), 1)
        self.assertEqual(older.enrichment_priority({"title": "MISSING FILM (2025)"}, "movie"), 2)
        self.assertEqual(older.enrichment_priority({"tmdbId": 101}, "series"), 0)
        self.assertFalse(older_session.calls)
        self.assertEqual(older.stats, initial_stats)
        fresh = self.client(FakeSession())
        self.assertEqual(fresh.enrichment_priority({"tmdbId": 101}, "movie"), 3)
        self.assertEqual(fresh.enrichment_priority({"title": "Missing Film", "year": 2025}, "movie"), 3)
        self.assertEqual(fresh.stats["requests"], 0)

    def test_trending_older_than_seven_days_is_not_published_as_current(self):
        self.client(FakeSession(FakeResponse({"results": [{"id": 101}], "total_pages": 1}))).trending("movie")
        offline = tmdb.TmdbClient(self.root, session=FakeSession(), now=1_800_000_000 + tmdb.MAX_TRENDING_AGE + 1)
        self.assertEqual(offline.trending("movie"), {})
        self.assertEqual(offline.stats["staleFallbacks"], 0)

    def test_failed_attempts_are_deferred_but_unattempted_titles_stay_first(self):
        failed = self.client(FakeSession(FakeResponse({}, 503), FakeResponse({}, 503), FakeResponse({}, 503)))
        with mock.patch.object(tmdb.time, "sleep"):
            self.assertEqual(failed.enrich({"tmdbId": 101}, "movie")["matchStatus"], "unavailable")
        next_run = self.client(FakeSession())
        self.assertEqual(next_run.enrichment_priority({"tmdbId": 101}, "movie"), 3)
        self.assertEqual(next_run.enrichment_priority({"tmdbId": 102}, "movie"), 0)
        self.assertEqual(next_run.enrich({"tmdbId": 101}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(next_run.stats["requests"], 0)
        untouched = self.client(FakeSession(), max_requests=0)
        untouched.enrich({"tmdbId": 102}, "movie")
        self.assertEqual(untouched.enrichment_priority({"tmdbId": 102}, "movie"), 0)

    def test_reset_elapsed_budget_allows_a_later_phase_without_changing_stats(self):
        session = FakeSession(FakeResponse(movie_details()))
        with mock.patch.object(tmdb.time, "monotonic", return_value=0) as clock:
            client = self.client(session, max_elapsed_seconds=30)
            clock.return_value = 100
            client.enrich({"tmdbId": 999}, "movie")
            initial_stats = dict(client.stats)
            client.reset_elapsed_budget(20)
            self.assertEqual(client.stats, initial_stats)
            self.assertEqual(client.max_elapsed_seconds, 20)
            self.assertEqual(client.enrich({"tmdbId": 101}, "movie")["matchStatus"], "matched_id")
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
