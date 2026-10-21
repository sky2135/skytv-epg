from __future__ import annotations

import json
import copy
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
import vod_reviewed_corrections as reviewed  # noqa: E402
from vod_reviewed_corrections import apply_reviewed_correction, correction_for  # noqa: E402


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


def evidence_item(**facts):
    return {"title": "Example Film (2025)", "contentId": "server_1:movie:999", "_identityEvidence": {
        "contentId": "server_1:movie:999", "fingerprint": "f" * 64,
        "directors": ["Director"], "cast": ["Actor", "Second Actor"], **facts}}


def evidence_details(identifier=101, *, matches=True):
    result = movie_details(identifier)
    result["credits"] = {"cast": [{"id": 9, "name": "Actor"}, {"id": 10, "name": "Second Actor"}],
                         "crew": [{"name": "Director" if matches else "Other Director", "job": "Director"}]}
    return result


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
        item = client.enrich({"tmdbId": "101", "title": "AR - Example Film (2025)", "languages": ["en"]}, "movie")
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
        self.assertEqual(options["params"], {"append_to_response": "credits,videos,alternative_titles"})
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
            FakeResponse({**movie_details(), "title": "Café: Film"}),
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

    def test_real_acronym_prefixes_survive_but_observed_decoration_is_removed(self):
        for label, expected in (("BTS: THE RETURN (2026)", "BTS: THE RETURN"),
                                ("NFT: Cursed Images (2026)", "NFT: Cursed Images"),
                                ("IT: Chapter Two (2019)", "IT: Chapter Two"),
                                ("XYZ - A Film (2026)", "XYZ - A Film"),
                                ("EN - A Film (2026)", "A Film"),
                                ("AR | A Film (2026)", "A Film"),
                                ("FHD: A Film (2026)", "A Film")):
            with self.subTest(label=label):
                self.assertEqual(tmdb._title_and_year({"title": label})[0], expected)
        title = "NFT: Cursed Images"
        details = {**movie_details(1436835), "title": title, "release_date": "2026-03-06"}
        session = FakeSession(FakeResponse({"results": [{"id": 1436835, "title": title, "release_date": "2026-03-06"}]}), FakeResponse(details))
        result = self.client(session).enrich({"title": title + " (2026)"}, "movie")
        self.assertEqual(result["matchStatus"], "matched_search")
        self.assertEqual(session.calls[0][1]["params"]["query"], title)

    def test_complete_pagination_finds_late_exact_match_and_deduplicates_ids(self):
        other = {"id": 90, "title": "Other Film", "release_date": "2025-01-01"}
        exact = {"id": 101, "title": "Example Film", "release_date": "2025-04-03"}
        session = FakeSession(*[FakeResponse({"page": page, "total_pages": 3, "total_results": 3, "results": [candidate]})
                                for page, candidate in enumerate((other, exact, exact), 1)], FakeResponse(movie_details()))
        client = self.client(session)
        self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["tmdbId"], "101")
        self.assertEqual([options["params"]["page"] for _url, options in session.calls[:3]], [1, 2, 3])
        self.assertEqual(client.last_match_diagnostic["searchPages"], 3)
        self.assertEqual(client.last_match_diagnostic["exactMatchCount"], 1)
        self.assertEqual(client.last_match_diagnostic["candidateIds"], ["101"])

    def test_late_page_competitor_proves_ambiguity_without_details(self):
        session = FakeSession(*[FakeResponse({"page": page, "total_pages": 3, "results": [
            {"id": identifier, "title": "Example Film", "release_date": "2025-04-03"}]})
            for page, identifier in ((1, 101), (2, 102))])
        client = self.client(session)
        self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "no_match")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.last_match_diagnostic["reason"], "ambiguous_title_year")

    def test_incomplete_pagination_never_accepts_first_page_candidate(self):
        first = {"page": 1, "total_pages": 2, "results": [{"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}
        for budget, extra in ((10, [FakeResponse({}, 401)]), (1, [])):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(first), *extra)
                client = tmdb.TmdbClient(Path(directory), token="secret", session=session, max_requests=budget)
                self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "unavailable")
                self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")
                self.assertEqual(client.stats["noMatches"], 0)
                self.assertLessEqual(client.stats["requests"], budget)

    def test_known_page_limit_defers_without_spending_on_unfinishable_search(self):
        session = FakeSession(FakeResponse({"total_pages": tmdb.MAX_SEARCH_PAGES + 1, "results": [
            {"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}))
        client = self.client(session)
        self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")
        self.assertEqual(len(session.calls), 1)

    def test_inconsistent_search_pages_or_truncated_result_counts_fail_closed(self):
        exact = {"id": 101, "title": "Example Film", "release_date": "2025-04-03"}
        first = {"page": 1, "total_pages": 2, "total_results": 2, "results": [exact]}
        for second in ({"page": 2, "total_pages": 3, "total_results": 2, "results": [exact]},
                       {"page": 2, "total_pages": 2, "total_results": 3, "results": [exact]},
                       {"page": 2, "total_pages": 2, "total_results": 2, "results": []},
                       {"page": 2, "total_pages": 2, "total_results": 2, "results": [{**exact, "title": "Changed Film"}]}):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="key", session=FakeSession(FakeResponse(first), FakeResponse(second)))
                self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "unavailable")
                self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")
        client = self.client(FakeSession(FakeResponse({"total_pages": 1, "total_results": 2, "results": [exact]})))
        self.assertEqual(client.enrich({"title": "Example Film (2025)"}, "movie")["matchStatus"], "unavailable")

    def test_search_discovers_official_alias_and_retains_bound_cache_proof(self):
        details = {**movie_details(), "alternative_titles": {"titles": [{"title": "Official Alias"}]}}
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}), FakeResponse(details))
        client = self.client(session)
        item = {"title": "Official Alias (2025)"}
        result = client.enrich(item, "movie")
        self.assertEqual(result["matchStatus"], "matched_search")
        self.assertEqual(client.last_match_diagnostic["reason"], "matched_alias")
        offline = self.client(FakeSession())
        self.assertEqual(offline.enrich(item, "movie"), result)
        self.assertTrue(offline.last_match_diagnostic["cacheHit"])
        self.assertEqual(offline.last_match_diagnostic["reason"], "matched_alias")

    def test_competing_official_aliases_are_ambiguous_and_partial_proof_defers(self):
        search = {"results": [{"id": identifier, "title": f"Different Film {identifier}", "release_date": "2025-04-03"} for identifier in (101, 102)]}
        def aliased(identifier):
            return {**movie_details(identifier), "alternative_titles": {"titles": [{"title": "Official Alias"}]}}
        for second, status, reason in ((FakeResponse(aliased(102)), "no_match", "ambiguous_title_year"),
                                       (FakeResponse({}, 401), "unavailable", "incomplete_search")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(search), FakeResponse(aliased(101)), second)
                client = tmdb.TmdbClient(Path(directory), token="key", session=session)
                self.assertEqual(client.enrich({"title": "Official Alias (2025)"}, "movie")["matchStatus"], status)
                self.assertEqual(client.last_match_diagnostic["reason"], reason)
                self.assertEqual(len(session.calls), 3)

    def test_alias_candidate_limit_preserves_unknown_and_does_not_spend_details_budget(self):
        search = {"results": [{"id": identifier, "title": f"Different Film {identifier}", "release_date": "2025-04-03"}
                              for identifier in range(101, 101 + tmdb.MAX_ALIAS_CANDIDATES + 1)]}
        session = FakeSession(FakeResponse(search))
        client = self.client(session)
        self.assertEqual(client.enrich({"title": "Official Alias (2025)"}, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "alias_candidate_limit")
        self.assertEqual(len(session.calls), 1)

    def test_old_negative_strategy_is_retried_without_invalidating_validated_positive(self):
        item = {"title": "Example Film (2025)"}
        self.client(FakeSession(FakeResponse({"results": [], "total_pages": 0}))).enrich(item, "movie")
        path = next(self.root.rglob("*.json"))
        entry = json.loads(path.read_text())
        entry["data"].pop("matchStrategy")
        path.write_text(json.dumps(entry), encoding="utf-8")
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}), FakeResponse(movie_details()))
        client = self.client(session)
        self.assertEqual(client.enrichment_priority(item, "movie"), 0)
        expected = client.enrich(item, "movie")
        self.assertEqual(expected["matchStatus"], "matched_search")
        entry = json.loads(path.read_text())
        entry["data"].pop("matchStrategy")
        path.write_text(json.dumps(entry), encoding="utf-8")
        offline = self.client(FakeSession())
        self.assertEqual(offline.enrich(item, "movie"), expected)
        self.assertTrue(offline.last_match_diagnostic["cacheHit"])

    def test_private_diagnostics_are_whitelisted_and_reset_between_records(self):
        raw = {"reason": ["private"], "candidateIds": ["101", "https://private.invalid/secret", True, "101"],
               "password": "secret", "searchPages": "3", "candidateCount": -2}
        result = tmdb._match_diagnostic(raw)
        self.assertEqual(result["reason"], "unavailable")
        self.assertEqual(result["candidateIds"], ["101"])
        self.assertNotIn("private", json.dumps(result))
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Wrong Title", "release_date": "2024-04-03"}]}))
        client = self.client(session)
        client.enrich({"title": "Example Film (2025)"}, "movie")
        client.enrich({"title": "No Year"}, "movie")
        self.assertEqual(client.last_match_diagnostic["candidateIds"], [])
        self.assertEqual(client.last_match_diagnostic["reason"], "missing_identity")

    def test_independent_director_and_two_cast_facts_can_resolve_bounded_ambiguity(self):
        search = {"results": [{"id": identifier, "title": "Example Film", "release_date": "2025-04-03"}
                              for identifier in (101, 102)]}
        session = FakeSession(FakeResponse(search), FakeResponse(evidence_details(101)), FakeResponse(evidence_details(102, matches=False)))
        client = self.client(session)
        item = evidence_item(runtimeMinutes=122)
        expected = client.enrich(item, "movie")
        self.assertEqual(expected["tmdbId"], "101")
        self.assertTrue(client.last_match_diagnostic["evidenceMatched"])
        self.assertEqual(len(session.calls), 3)
        offline = self.client(FakeSession())
        self.assertEqual(offline.enrich(item, "movie"), expected)
        self.assertTrue(offline.last_match_diagnostic["cacheHit"])
        # The selected work cannot enter the ordinary shared title/year cache.
        baseline = self.client(FakeSession(FakeResponse(search)))
        self.assertEqual(baseline.enrich({"title": item["title"]}, "movie")["matchStatus"], "no_match")

    def test_evidence_does_not_hide_competing_alias_or_partial_candidate_details(self):
        search = {"results": [
            {"id": 101, "title": "Example Film", "release_date": "2025-04-03"},
            {"id": 102, "title": "Example Film", "release_date": "2025-04-03"},
            {"id": 103, "title": "Other Primary Title", "release_date": "2025-04-03"}]}
        competitor = {**evidence_details(103), "title": "Other Primary Title", "original_title": "Other Original Title",
                      "alternative_titles": {"titles": [{"title": "Example Film"}]}}
        for third, status, reason in ((FakeResponse(competitor), "no_match", "ambiguous_title_year"),
                                      (FakeResponse({}, 401), "unavailable", "incomplete_search")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(search), FakeResponse(evidence_details(101)),
                                      FakeResponse(evidence_details(102, matches=False)), third)
                client = tmdb.TmdbClient(Path(directory), token="key", session=session)
                self.assertEqual(client.enrich(evidence_item(), "movie")["matchStatus"], status)
                self.assertEqual(client.last_match_diagnostic["reason"], reason)
                self.assertEqual(len(session.calls), 4)

    def test_single_exact_with_evidence_still_checks_an_official_alias_competitor(self):
        search = {"results": [
            {"id": 101, "title": "Example Film", "release_date": "2025-04-03"},
            {"id": 102, "title": "Other Primary Title", "release_date": "2025-04-03"}]}
        competitor = {**evidence_details(102), "title": "Other Primary Title", "original_title": "Other Original Title",
                      "alternative_titles": {"titles": [{"title": "Example Film"}]}}
        session = FakeSession(FakeResponse(search), FakeResponse(evidence_details(101)), FakeResponse(competitor))
        client = self.client(session)
        self.assertEqual(client.enrich(evidence_item(), "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "ambiguous_title_year")
        self.assertEqual(len(session.calls), 3)

    def test_runtime_or_provider_tmdb_id_alone_never_resolves_ambiguity(self):
        search = {"results": [{"id": identifier, "title": "Example Film", "release_date": "2025-04-03"}
                              for identifier in (101, 102)]}
        for facts in ({"directors": [], "cast": [], "runtimeMinutes": 122, "tmdbId": "101"},
                      {"directors": ["Director"], "cast": ["Actor", "Actor"]},
                      {"directors": ["Director"], "cast": ["Actor", "Second Actor"], "fingerprint": "bad"},
                      {"contentId": "server_1:movie:1000"}):
            with self.subTest(facts=facts), tempfile.TemporaryDirectory() as directory:
                session = FakeSession(FakeResponse(search))
                client = tmdb.TmdbClient(Path(directory), token="key", session=session)
                self.assertEqual(client.enrich(evidence_item(**facts), "movie")["matchStatus"], "no_match")
                self.assertEqual(len(session.calls), 1)

    def test_evidence_proof_is_bound_to_record_facts_fingerprint_and_full_display_title(self):
        search = {"results": [{"id": identifier, "title": "Example Film", "release_date": "2025-04-03"}
                              for identifier in (101, 102)]}
        session = FakeSession(FakeResponse(search), FakeResponse(evidence_details(101)), FakeResponse(evidence_details(102, matches=False)))
        expected = self.client(session).enrich(evidence_item(), "movie")
        self.assertEqual(expected["tmdbId"], "101")
        variants = []
        other_record = evidence_item()
        other_record["contentId"] = other_record["_identityEvidence"]["contentId"] = "server_1:movie:1000"
        variants.append(other_record)
        variants.append(evidence_item(fingerprint="a" * 64))
        variants.append(evidence_item(directors=["Different Director"]))
        other_title = evidence_item()
        other_title["title"] = "Example: Film (2025)"
        variants.append(other_title)
        for item in variants:
            with self.subTest(item=item):
                client = self.client(FakeSession(), max_requests=0)
                self.assertEqual(client.enrichment_priority(item, "movie"), 0)
                self.assertEqual(client.enrich(item, "movie")["matchStatus"], "unavailable")

    def test_external_id_evidence_is_corroborated_and_cannot_override_wrong_year(self):
        found = {"movie_results": [{"id": 101}]}
        correct = {**movie_details(101), "imdb_id": "tt1234567"}
        item = evidence_item(imdbId="tt1234567", directors=[], cast=[])
        session = FakeSession(FakeResponse(found), FakeResponse(correct))
        client = self.client(session)
        self.assertEqual(client.enrich(item, "movie")["tmdbId"], "101")
        self.assertTrue(session.calls[1][1]["params"]["append_to_response"].endswith(",external_ids"))
        with tempfile.TemporaryDirectory() as directory:
            wrong_year = {**correct, "release_date": "2024-04-03"}
            client = tmdb.TmdbClient(Path(directory), token="key", session=FakeSession(FakeResponse(found), FakeResponse(wrong_year)))
            self.assertEqual(client.enrich(item, "movie")["matchStatus"], "no_match")

    def test_conflicting_external_id_vetoes_even_a_unique_title_match(self):
        session = FakeSession(FakeResponse({"movie_results": [{"id": 101}]}),
                              FakeResponse({**movie_details(), "imdb_id": "tt7654321"}))
        client = self.client(session)
        self.assertEqual(client.enrich(evidence_item(imdbId="tt1234567"), "movie")["matchStatus"], "no_match")

    def test_conflicting_runtime_corroboration_vetoes_director_cast_route(self):
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}),
                              FakeResponse(evidence_details()))
        self.assertEqual(self.client(session).enrich(evidence_item(runtimeMinutes=200.5), "movie")["matchStatus"], "no_match")

    def test_evidence_candidate_cap_never_picks_from_a_partial_set(self):
        search = {"results": [{"id": identifier, "title": "Example Film" if identifier < 103 else "Different Film", "release_date": "2025-04-03"}
                              for identifier in range(101, 105)]}
        session = FakeSession(FakeResponse(search))
        client = self.client(session)
        self.assertEqual(client.enrich(evidence_item(), "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "ambiguous_title_year")
        self.assertEqual(len(session.calls), 1)

    def test_series_details_and_calendar_never_imply_provider_episodes_exist(self):
        details = {
            "id": 301, "original_name": "Series", "original_language": "es",
            "first_air_date": "2024-01-01", "last_air_date": "2026-10-01",
            "episode_run_time": [0, 43], "number_of_seasons": 3, "number_of_episodes": 24,
            "next_episode_to_air": {"season_number": 3, "episode_number": 5, "air_date": "2026-10-22"},
        }
        session = FakeSession(FakeResponse(details))
        result = self.client(session).enrich({"tmdbId": 301, "title": "Series", "year": 2024}, "series")
        self.assertEqual(session.calls[0][0], tmdb.API_ROOT + "/tv/301")
        self.assertEqual(result["nextEpisode"], {"season": 3, "episode": 5, "airDate": "2026-10-22"})
        self.assertEqual(result["runtimeMinutes"], 43)
        self.assertEqual(result["seasonCount"], 3)
        self.assertEqual(result["episodeCount"], 24)
        self.assertNotIn("availableEpisodes", result)

    def test_metadata_cache_survives_new_client_without_key_and_is_kind_scoped(self):
        client = self.client(FakeSession(FakeResponse(movie_details())))
        expected = client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        session = FakeSession()
        offline = tmdb.TmdbClient(self.root, session=session, now=1_800_000_001)
        self.assertEqual(offline.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), expected)
        self.assertEqual(offline.stats["cacheHits"], 1)
        self.assertEqual(offline.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "series")["matchStatus"], "unavailable")
        self.assertFalse(session.calls)

    def test_stale_details_survive_transient_errors_and_budget_exhaustion(self):
        expected = self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        for index, (session, budget) in enumerate(((FakeSession(FakeResponse({}, 503), FakeResponse({}, 503), FakeResponse({}, 503)), 3), (FakeSession(), 0))):
            client = tmdb.TmdbClient(self.root, token="key", session=session, now=1_800_000_000 + tmdb.DETAIL_TTL + 1 + index * (tmdb.MISS_TTL + 1), max_requests=budget)
            with mock.patch.object(tmdb.time, "sleep"):
                self.assertEqual(client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), expected)
            self.assertEqual(client.stats["staleFallbacks"], 1)
        cache_entry = json.loads(next(self.root.rglob("*.json")).read_text())
        self.assertEqual(cache_entry["data"]["metadata"], expected)

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
            self.assertEqual(client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "matched_id")
        sleep.assert_called_once_with(tmdb.MAX_RETRY_DELAY)
        self.assertEqual(client.stats["requests"], 2)
        limited = self.client(FakeSession(FakeResponse({}, 429)), max_requests=1)
        with mock.patch.object(tmdb.time, "sleep"):
            self.assertEqual(limited.enrich({"tmdbId": 102, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")
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
                self.assertEqual(client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")
                cached = next(Path(directory).rglob("*.json")).read_text()
                self.assertEqual(json.loads(cached)["data"]["metadata"]["matchStatus"], "unavailable")
                self.assertNotIn("secret", cached)
                self.assertNotIn("private.invalid", cached)
                self.assertTrue(response.closed)
        session = FakeSession(*[requests.ConnectionError("https://api.themoviedb.org?api_key=secret") for _ in range(3)])
        with mock.patch.object(tmdb.time, "sleep"):
            self.assertEqual(self.client(session).enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")

    def test_api_key_auth_and_no_key_gracefully_preserve_provider_id(self):
        session = FakeSession(FakeResponse(movie_details()))
        client = tmdb.TmdbClient(self.root, api_key="api-key-secret", session=session)
        client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
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
        result = self.client(FakeSession(FakeResponse(details))).enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        for key in ("posterUrl", "backdropUrl", "rating", "voteCount", "runtimeMinutes", "trailerUrl"):
            self.assertIsNone(result[key], key)

    def test_invalid_kinds_fail_with_a_generic_safe_error(self):
        with self.assertRaisesRegex(ValueError, "Metadata kind"):
            self.client().enrich({"password": "secret"}, "https://private.invalid")

    def test_elapsed_budget_stops_requests_and_preserves_stale_cache(self):
        expected = self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        session = FakeSession()
        with mock.patch.object(tmdb.time, "monotonic", return_value=100) as clock:
            client = tmdb.TmdbClient(self.root, token="key", session=session,
                                     now=1_800_000_000 + tmdb.DETAIL_TTL + 1,
                                     max_elapsed_seconds=300)
            clock.return_value = 401
            self.assertEqual(client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), expected)
        self.assertFalse(session.calls)
        self.assertEqual(client.stats["requests"], 0)
        self.assertEqual(client.stats["staleFallbacks"], 1)
        self.assertEqual(client.stats["elapsedBudgetExhausted"], 1)
        retry_session = FakeSession(FakeResponse({}, 429))
        with mock.patch.object(tmdb.time, "monotonic", side_effect=[0, 0, 301]), mock.patch.object(tmdb.time, "sleep"):
            retrying = self.client(retry_session)
            self.assertEqual(retrying.enrich({"tmdbId": 102, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(retrying.stats["requests"], 1)
        self.assertEqual(retrying.stats["elapsedBudgetExhausted"], 1)

    def test_restored_cache_is_revalidated_before_export(self):
        client = self.client(FakeSession(FakeResponse(movie_details())))
        expected = client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        cache_path = next(self.root.rglob("*.json"))
        entry = json.loads(cache_path.read_text())
        entry["data"]["metadata"]["posterUrl"] = "https://private.invalid/token"
        entry["data"]["metadata"]["cast"][0]["private"] = "secret"
        entry["data"]["metadata"]["adult"] = "false"
        cache_path.write_text(json.dumps(entry), encoding="utf-8")
        result = self.client().enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        self.assertIsNone(result["posterUrl"])
        self.assertIsNone(result["adult"])
        self.assertEqual(result["cast"], expected["cast"])
        self.assertNotIn("private.invalid", json.dumps(result))

    def test_enrichment_priority_favors_missing_before_stale_then_fresh_without_requests(self):
        self.client(FakeSession(FakeResponse(movie_details()))).enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        self.client(FakeSession(FakeResponse({"results": [], "total_pages": 0}))).enrich({"title": "Missing Film", "year": 2025}, "movie")
        older_session = FakeSession()
        older = tmdb.TmdbClient(self.root, token="key", session=older_session, now=1_800_000_000 + tmdb.DETAIL_TTL + 1)
        initial_stats = dict(older.stats)
        self.assertEqual(older.enrichment_priority({"tmdbId": 999, "title": "Example Film", "year": 2025}, "movie"), 0)
        self.assertEqual(older.enrichment_priority({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), 1)
        self.assertEqual(older.enrichment_priority({"title": "MISSING FILM (2025)"}, "movie"), 2)
        self.assertEqual(older.enrichment_priority({"tmdbId": 101, "title": "Example Film", "year": 2025}, "series"), 0)
        self.assertFalse(older_session.calls)
        self.assertEqual(older.stats, initial_stats)
        fresh = self.client(FakeSession())
        self.assertEqual(fresh.enrichment_priority({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), 3)
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
            self.assertEqual(failed.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")
        next_run = self.client(FakeSession())
        self.assertEqual(next_run.enrichment_priority({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie"), 3)
        self.assertEqual(next_run.enrichment_priority({"tmdbId": 102, "title": "Example Film", "year": 2025}, "movie"), 0)
        self.assertEqual(next_run.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(next_run.stats["requests"], 0)
        untouched = self.client(FakeSession(), max_requests=0)
        untouched.enrich({"tmdbId": 102, "title": "Example Film", "year": 2025}, "movie")
        self.assertEqual(untouched.enrichment_priority({"tmdbId": 102, "title": "Example Film", "year": 2025}, "movie"), 0)

    def test_reset_elapsed_budget_allows_a_later_phase_without_changing_stats(self):
        session = FakeSession(FakeResponse(movie_details()))
        with mock.patch.object(tmdb.time, "monotonic", return_value=0) as clock:
            client = self.client(session, max_elapsed_seconds=30)
            clock.return_value = 100
            client.enrich({"tmdbId": 999, "title": "Example Film", "year": 2025}, "movie")
            initial_stats = dict(client.stats)
            client.reset_elapsed_budget(20)
            self.assertEqual(client.stats, initial_stats)
            self.assertEqual(client.max_elapsed_seconds, 20)
            self.assertEqual(client.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")["matchStatus"], "matched_id")
        self.assertEqual(len(session.calls), 1)

    def test_provider_decoration_and_display_year_are_independent_identity(self):
        for label, expected in (("AR - Doctor (2021)", ("Doctor", "2021")),
                                ("Doctor - 2021", ("Doctor", "2021")),
                                ("EN - 1917 (2019) [HD]", ("1917", "2019")),
                                ("Lapandav (MARATHI)", ("Lapandav", "2016")),
                                ("2012", ("2012", "2016"))):
            with self.subTest(label=label):
                self.assertEqual(tmdb._title_and_year({"title": label, "releaseDate": "2016-10-25"}), expected)
        self.assertEqual(tmdb._title_and_year({"originalTitle": "Wrong cached title"}), ("", None))

    def test_supplied_ids_and_search_details_must_corroborate_display_identity(self):
        for title, year, details in (("Doctor", 2021, {**movie_details(284052), "title": "Doctor Strange", "release_date": "2016-10-25"}),
                                     ("Example Film", 2024, movie_details()),
                                     ("Example", 2025, movie_details())):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="key", session=FakeSession(FakeResponse(details)))
                result = client.enrich({"tmdbId": details["id"], "title": title, "year": year}, "movie")
                self.assertEqual(result["matchStatus"], "no_match")
                self.assertIsNone(result["tmdbId"])
                self.assertTrue(result["rejectedIdentity"])
                self.assertIsNone(result["posterUrl"])
        session = FakeSession(FakeResponse({"results": [{"id": 101, "title": "Cafe Film", "release_date": "2025-01-01"}]}), FakeResponse(movie_details()))
        result = self.client(session).enrich({"title": "Cafe Film (2025)"}, "movie")
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertTrue(result["rejectedIdentity"])

    def test_id_without_display_identity_never_uses_polluted_original_title(self):
        session = FakeSession()
        result = self.client(session).enrich({"tmdbId": 101, "originalTitle": "Original Film", "year": 2025}, "movie")
        self.assertTrue(result["rejectedIdentity"])
        self.assertIsNone(result["tmdbId"])
        self.assertFalse(session.calls)

    def test_bound_cache_does_not_cross_titles_sharing_one_bad_provider_id(self):
        first = self.client(FakeSession(FakeResponse(movie_details())))
        first.enrich({"tmdbId": 101, "title": "Example Film", "year": 2025}, "movie")
        session = FakeSession(FakeResponse(movie_details()))
        different = self.client(session).enrich({"tmdbId": 101, "title": "Doctor (2021)"}, "movie")
        self.assertEqual(different["matchStatus"], "no_match")
        self.assertTrue(different["rejectedIdentity"])
        self.assertEqual(len(session.calls), 1)

    def test_legacy_positive_cache_is_retained_only_with_exact_title_and_year(self):
        writer = self.client()
        legacy = tmdb._metadata(movie_details(), "movie", "matched_id")
        legacy.pop("rejectedIdentity")
        writer._write_cache("movie:id:101", legacy, tmdb.DETAIL_TTL)
        offline = tmdb.TmdbClient(self.root, session=FakeSession(), now=1_800_000_001)
        valid = offline.enrich({"tmdbId": 101, "title": "Original Film (2025)"}, "movie")
        self.assertEqual(valid["matchStatus"], "matched_id")
        self.assertEqual(valid["originalTitle"], "Original Film")
        for title in ("Doctor (2021)", "Original Film (1995)", "Original Film"):
            result = offline.enrich({"tmdbId": 101, "title": title}, "movie")
            self.assertTrue(result["rejectedIdentity"], title)
            self.assertIsNone(result["tmdbId"], title)
            self.assertIsNone(result["originalTitle"], title)

    def test_verified_api_aliases_preserve_translated_cache_without_fuzzy_matching(self):
        details = {**movie_details(), "title": "Example Film", "original_title": "मूल फ़िल्म",
                   "alternative_titles": {"titles": [{"title": "Official Alternate Name"}]}}
        item = {"tmdbId": 101, "title": "Official Alternate Name (2025)"}
        result = self.client(FakeSession(FakeResponse(details))).enrich(item, "movie")
        self.assertEqual(result["matchStatus"], "matched_id")
        offline = tmdb.TmdbClient(self.root, session=FakeSession(), now=1_800_000_001)
        self.assertEqual(offline.enrich(item, "movie"), result)
        path = next(self.root.rglob("*.json"))
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["data"]["metadata"]["originalTitle"] = "Doctor Strange"
        path.write_text(json.dumps(entry), encoding="utf-8")
        rejected = offline.enrich(item, "movie")
        self.assertTrue(rejected["rejectedIdentity"])
        self.assertIsNone(rejected["originalTitle"])

    def test_reviewed_corrections_are_record_scoped_and_preserve_provider_audio(self):
        poisoned = {"id": "622578", "title": "AR - Doctor (2021)", "tmdbId": "284052",
                    "artwork": {"posterUrl": "https://image.tmdb.org/t/p/w500/wrong.jpg", "backdropUrl": None},
                    "genre": "fantasy", "genres": ["fantasy"], "releaseDate": "2016-10-25",
                    "language": "ar", "languages": ["ar"], "originalLanguage": "en"}
        corrected = apply_reviewed_correction("server_3", "movie", poisoned)
        self.assertIsNone(corrected["tmdbId"])
        self.assertIsNone(corrected["artwork"]["posterUrl"])
        self.assertEqual(corrected["originalLanguage"], "ta")
        self.assertEqual(corrected["languages"], ["ar"])
        self.assertEqual(poisoned["tmdbId"], "284052")
        self.assertIsNone(correction_for("server_2", "movie", poisoned))
        self.assertIsNone(correction_for("server_3", "movie", {**poisoned, "title": "Doctor Strange (2016)"}))
        doree = {"id": "5731", "title": "Doree", "tmdbId": "23963", "releaseDate": "1985-05-07"}
        self.assertEqual(apply_reviewed_correction("server_2", "series", doree)["tmdbId"], "242528")
        fish = {"id": "403965", "title": "EN - Fisherman's Friends 2 (2021)", "releaseDate": "2021-01-01"}
        fixed_fish = apply_reviewed_correction("server_3", "movie", fish)
        self.assertEqual(fixed_fish["title"], "EN - Fisherman's Friends 2 (2022)")
        self.assertIsNone(fixed_fish["releaseDate"])
        self.assertIsNotNone(correction_for("server_3", "movie", fixed_fish))

    def test_reviewed_corrections_take_priority_and_reject_bad_cache_offline(self):
        item = {"id": "640913", "contentId": "server_3:movie:640913", "title": "AR - Romantic (2021)", "tmdbId": "449563", "releaseDate": "2019-02-13"}
        client = tmdb.TmdbClient(self.root, session=FakeSession(), now=1_800_000_000)
        self.assertEqual(client.enrichment_priority(item, "movie"), -1)
        result = client.enrich(item, "movie")
        self.assertTrue(result["rejectedIdentity"])
        self.assertIsNone(result["tmdbId"])

    def test_reviewed_known_bad_id_cannot_reenter_via_a_polluted_search_result(self):
        session = FakeSession(FakeResponse({"results": [{"id": 449563, "title": "Romantic", "release_date": "2021-10-29"}]}))
        item = {"id": "640913", "contentId": "server_3:movie:640913", "title": "AR - Romantic (2021)", "tmdbId": "449563"}
        result = self.client(session).enrich(item, "movie")
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertTrue(result["rejectedIdentity"])
        self.assertIsNone(result["tmdbId"])
        self.assertEqual(len(session.calls), 1)

    def test_reviewed_document_is_complete_public_and_validated_without_silent_overwrites(self):
        path = SCRIPTS_DIR.parent / "data/vod_reviewed_corrections.json"
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        rules = reviewed._validate_document(document)
        self.assertEqual(len(rules), 52)
        self.assertEqual(sum(row["rejectIdentity"] for row in rules.values()), 7)
        for marker in ("password", "api_key", "direct_source", "rawSource", ".invalid", "playbackUrl"):
            self.assertNotIn(marker, text)
        base = {"version": 1, "corrections": [copy.deepcopy(next(iter(rules.values())))]}
        invalid = []
        duplicate = copy.deepcopy(base)
        duplicate["corrections"].append(copy.deepcopy(duplicate["corrections"][0]))
        invalid.append(duplicate)
        for field, value in (("displayNames", []), ("displayNames", ["   "]),
                             ("evidenceURLs", ["http://www.netflix.com/title/123"]),
                             ("evidenceURLs", ["https://user:password@www.netflix.com/title/123"]),
                             ("evidenceURLs", ["https://127.0.0.1/private"]),
                             ("evidenceURLs", ["https://www.netflix.com/title/123?api_key=secret"]),
                             ("evidenceURLs", ["https://provider.invalid/private"]),
                             ("metadata", {"rawSource": "private"}),
                             ("metadata", {"languages": ["en"]}),
                             ("metadata", {"releaseDate": "2021"}),
                             ("metadata", {"originalLanguage": "EN"}),
                             ("rawSource", "https://private.invalid/password")):
            candidate = copy.deepcopy(base)
            candidate["corrections"][0][field] = value
            invalid.append(candidate)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                reviewed._validate_document(candidate)


if __name__ == "__main__":
    unittest.main()
