from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests


ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "scripts", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


IMDB = "tt1234567"


def item(*, kind="movie", title="Example Film (2025)", **fields):
    content_id = f"server_1:{kind}:999"
    return {"id": "999", "title": title, "contentId": content_id, "_identityEvidence": {
        "contentId": content_id, "fingerprint": "f" * 64, "imdbId": IMDB}, **fields}


def details(identifier=101, *, kind="movie", **fields):
    result = {**movie_details(identifier), "external_ids": {"imdb_id": IMDB}, **fields}
    if kind == "series":
        result.update(name="Example Series", original_name="Example Series", first_air_date="2025-04-03")
    return result


def found(*identifiers, kind="movie"):
    return {"movie_results" if kind == "movie" else "tv_results": [{"id": identifier} for identifier in identifiers]}


class ExternalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def client(self, *responses, cache=None, **kwargs):
        return tmdb.TmdbClient(cache or self.cache, token="private-token", session=FakeSession(*responses),
                               now=1_800_000_000, **kwargs)

    def assert_find_only(self, client):
        self.assertEqual(len(client.session.calls), 1)
        self.assertEqual(client.session.calls[0][0], tmdb.API_ROOT + f"/find/{IMDB}")
        self.assertEqual(client.session.calls[0][1]["params"], {"external_source": "imdb_id"})

    def test_external_identity_bypasses_title_search_and_candidate_detail_limit(self):
        client = self.client(FakeResponse(found(101)), FakeResponse(details()))
        result = client.enrich(item(), "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("101", "matched_search"))
        self.assertEqual([url for url, _options in client.session.calls],
                         [tmdb.API_ROOT + f"/find/{IMDB}", tmdb.API_ROOT + "/movie/101"])
        self.assertIn("external_ids", client.session.calls[1][1]["params"]["append_to_response"].split(","))
        self.assertEqual(client.stats["requests"], 2)
        self.assertNotIn("_identityEvidence", result)
        cached = "".join(path.read_text() for path in self.cache.rglob("*.json"))
        self.assertNotIn("private-token", cached)
        self.assertNotIn("private.invalid", cached)

    def test_official_alias_details_can_corroborate_an_external_identity(self):
        alias = "Official Alternate Title"
        client = self.client(FakeResponse(found(101)), FakeResponse(details(
            alternative_titles={"titles": [{"title": alias}]})))
        result = client.enrich(item(title=alias + " (2025)"), "movie")
        self.assertEqual(result["matchStatus"], "matched_search")
        self.assertEqual(result["tmdbId"], "101")

    def test_series_use_only_tv_results_and_tv_external_identity_details(self):
        payload = {"movie_results": [{"id": 202}], "tv_results": [{"id": 301, "media_type": "tv"}]}
        client = self.client(FakeResponse(payload), FakeResponse(details(301, kind="series")))
        result = client.enrich(item(kind="series", title="Example Series (2025)"), "series")
        self.assertEqual(result["tmdbId"], "301")
        self.assertEqual(client.session.calls[1][0], tmdb.API_ROOT + "/tv/301")
        self.assertTrue(all("/movie/" not in url for url, _options in client.session.calls))

    def test_matching_supplied_id_keeps_direct_id_status(self):
        client = self.client(FakeResponse(found(101)), FakeResponse(details()))
        result = client.enrich(item(tmdbId="101"), "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("101", "matched_id"))

    def test_disagreeing_supplied_or_reviewed_effective_id_defers_and_clears_id(self):
        correction = {"rejectIdentity": True, "metadata": {"tmdbId": "102"}, "rejectedTmdbIds": ["9999"]}
        for supplied, reviewed in (("102", None), (None, correction)):
            with self.subTest(reviewed=bool(reviewed)), tempfile.TemporaryDirectory() as folder:
                client = self.client(FakeResponse(found(101)), cache=Path(folder))
                with mock.patch.object(tmdb, "_reviewed", return_value=reviewed):
                    result = client.enrich(item(tmdbId=supplied), "movie")
                self.assertEqual(result["matchStatus"], "unavailable")
                self.assertIsNone(result["tmdbId"])
                self.assert_find_only(client)

    def test_reviewed_rejected_find_id_is_never_fetched(self):
        correction = {"rejectIdentity": False, "metadata": {}, "rejectedTmdbIds": ["101"]}
        client = self.client(FakeResponse(found(101)))
        with mock.patch.object(tmdb, "_reviewed", return_value=correction):
            result = client.enrich(item(), "movie")
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertIsNone(result["tmdbId"])
        self.assert_find_only(client)

    def test_inconsistent_find_preserves_verified_stale_date_without_accepting_new_id(self):
        original = item(tmdbId="101")
        writer = self.client(FakeResponse(found(101)), FakeResponse(details()))
        expected = writer.enrich(original, "movie")
        client = tmdb.TmdbClient(self.cache, token="private-token", session=FakeSession(FakeResponse(found(102))),
                                now=1_800_000_000 + tmdb.DETAIL_TTL + 1)
        result = client.enrich(original, "movie")
        self.assertEqual(result["tmdbId"], expected["tmdbId"])
        self.assertEqual(result["releaseDate"], expected["releaseDate"])
        self.assertEqual(client.last_match_diagnostic["reason"], "unavailable")
        self.assertEqual(client.stats["staleFallbacks"], 1)
        self.assert_find_only(client)

    def test_find_does_not_override_detail_title_year_or_external_id(self):
        variants = ({"title": "Another Film", "original_title": "Another Film"},
                    {"release_date": "2024-04-03"}, {"release_date": None},
                    {"external_ids": {"imdb_id": "tt7654321"}}, {"external_ids": {}},
                    {"imdb_id": "tt7654321", "external_ids": {"imdb_id": IMDB}},
                    {"imdb_id": IMDB, "external_ids": {"imdb_id": "tt7654321"}})
        for fields in variants:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as folder:
                client = self.client(FakeResponse(found(101)), FakeResponse(details(**fields)), cache=Path(folder))
                result = client.enrich(item(), "movie")
                self.assertEqual(result["matchStatus"], "no_match")
                self.assertIsNone(result["tmdbId"])
                self.assertIsNone(result["releaseDate"])
                self.assertEqual(len(client.session.calls), 2)
        client = self.client(FakeResponse(found(101)), FakeResponse(details(102)))
        self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")

    def test_duplicate_find_id_is_deduplicated_but_multiple_ids_remain_ambiguous(self):
        duplicate = self.client(FakeResponse(found(101, 101)), FakeResponse(details()))
        self.assertEqual(duplicate.enrich(item(), "movie")["tmdbId"], "101")
        with tempfile.TemporaryDirectory() as folder:
            multiple = self.client(FakeResponse(found(101, 102)), cache=Path(folder))
            self.assertEqual(multiple.enrich(item(), "movie")["matchStatus"], "unavailable")
            self.assertEqual(multiple.last_match_diagnostic["reason"], "ambiguous_title_year")
            self.assert_find_only(multiple)

    def test_malformed_find_payload_or_any_malformed_entry_defers_without_search(self):
        payloads = ({}, {"movie_results": None}, {"movie_results": {}},
                    {"movie_results": [None]}, {"movie_results": [{}]},
                    *({"movie_results": [{"id": value}]} for value in (True, "101", 101.0, 0, -1, 1_000_000_001)),
                    {"movie_results": [{"id": 101}, {"id": "102"}]},
                    {"movie_results": [{"id": 101, "media_type": "tv"}]},
                    {"movie_results": [{"id": 101}] * 101})
        for payload in payloads:
            with self.subTest(payload_type=str(payload)[:100]), tempfile.TemporaryDirectory() as folder:
                client = self.client(FakeResponse(payload), cache=Path(folder))
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
                self.assert_find_only(client)

    def test_empty_right_kind_results_fall_back_to_strict_search_ignoring_other_kinds(self):
        payload = {"movie_results": [], "tv_results": [{"id": 202}], "private": "private-token"}
        search = {"results": [{"id": 101, "title": "Example Film", "release_date": "2025-04-03"}]}
        client = self.client(FakeResponse(payload), FakeResponse(search), FakeResponse(details()))
        self.assertEqual(client.enrich(item(), "movie")["tmdbId"], "101")
        self.assertEqual([url for url, _options in client.session.calls],
                         [tmdb.API_ROOT + f"/find/{IMDB}", tmdb.API_ROOT + "/search/movie", tmdb.API_ROOT + "/movie/101"])
        self.assertNotIn("private-token", "".join(path.read_text() for path in self.cache.rglob("*.json")))

    def test_empty_find_can_complete_seven_or_eight_strict_search_pages(self):
        for total_pages in (7, 8):
            with self.subTest(total_pages=total_pages), tempfile.TemporaryDirectory() as folder:
                responses = [FakeResponse(found())]
                for page in range(1, total_pages + 1):
                    candidate = {"id": 101 if page == total_pages else 300 + page,
                                 "title": "Example Film" if page == total_pages else "Other Film",
                                 "release_date": "2025-04-03" if page == total_pages else "2024-04-03"}
                    responses.append(FakeResponse({"page": page, "total_pages": total_pages, "total_results": total_pages, "results": [candidate]}))
                responses.append(FakeResponse(details()))
                client = self.client(*responses, cache=Path(folder))
                self.assertEqual(client.enrich(item(), "movie")["tmdbId"], "101")
                self.assertEqual(client.last_match_diagnostic["searchPages"], total_pages)
                self.assertEqual(client.stats["requests"], total_pages + 2)

    def test_find_http_errors_or_transport_failures_do_not_fall_back(self):
        for status in (404, 401, 302):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                client = self.client(FakeResponse({}, status=status), cache=Path(folder))
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
                self.assert_find_only(client)
        with tempfile.TemporaryDirectory() as folder:
            client = self.client(*(requests.ConnectionError("private-token https://private.invalid") for _ in range(3)), cache=Path(folder))
            with mock.patch.object(tmdb.time, "sleep"):
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
            self.assertEqual(client.stats["requests"], 3)
            self.assertTrue(all(url.endswith("/find/" + IMDB) for url, _options in client.session.calls))

    def test_find_and_details_share_request_budget_without_partial_acceptance(self):
        for budget in (0, 1):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as folder:
                client = self.client(*( [FakeResponse(found(101))] if budget else []), cache=Path(folder), max_requests=budget)
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
                self.assertEqual(client.stats["requests"], budget)
                self.assertEqual(len(client.session.calls), budget)
        with tempfile.TemporaryDirectory() as folder:
            client = self.client(FakeResponse({}, status=503), FakeResponse({}, status=503), cache=Path(folder), max_requests=2)
            with mock.patch.object(tmdb.time, "sleep"):
                self.assertEqual(client.enrich(item(), "movie")["matchStatus"], "unavailable")
            self.assertEqual(client.stats["requests"], 2)

    def test_verified_cache_is_bound_to_record_facts_fingerprint_and_display_title(self):
        client = self.client(FakeResponse(found(101)), FakeResponse(details()))
        original = item()
        expected = client.enrich(original, "movie")
        offline = tmdb.TmdbClient(self.cache, session=FakeSession(), now=1_800_000_001)
        self.assertEqual(offline.enrich(original, "movie"), expected)
        self.assertFalse(offline.session.calls)
        variants = []
        other = copy.deepcopy(original)
        other["id"] = "1000"
        other["contentId"] = other["_identityEvidence"]["contentId"] = "server_1:movie:1000"
        variants.append(other)
        for field, value in (("imdbId", "tt7654321"), ("fingerprint", "a" * 64)):
            other = copy.deepcopy(original)
            other["_identityEvidence"][field] = value
            variants.append(other)
        variants.append({key: value for key, value in original.items() if key != "_identityEvidence"})
        other = copy.deepcopy(original)
        other["title"] = "Example: Film (2025)"
        variants.append(other)
        for other in variants:
            with self.subTest(content_id=other["contentId"], title=other["title"]):
                self.assertEqual(offline.enrichment_priority(other, "movie"), 0)
                self.assertEqual(offline.enrich(other, "movie")["matchStatus"], "unavailable")

    def test_invalid_or_unbound_imdb_evidence_never_becomes_a_find_route(self):
        for field, value in (("imdbId", "tt1234567/../../private"), ("fingerprint", "invalid"),
                             ("contentId", "server_1:movie:1000")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                invalid = item()
                invalid["_identityEvidence"][field] = value
                client = self.client(FakeResponse({"results": [], "total_pages": 0}), cache=Path(folder))
                self.assertEqual(client.enrich(invalid, "movie")["matchStatus"], "no_match")
                self.assertEqual(client.session.calls[0][0], tmdb.API_ROOT + "/search/movie")
                self.assertEqual(len(client.session.calls), 1)

    def test_provider_id_must_agree_with_the_bound_content_id(self):
        invalid = item(id="1000")
        client = self.client(FakeResponse({"results": [], "total_pages": 0}))
        self.assertEqual(client.enrich(invalid, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.session.calls[0][0], tmdb.API_ROOT + "/search/movie")
        self.assertEqual(len(client.session.calls), 1)

    def test_strategy_two_negative_is_retried_but_policy_two_positive_is_retained(self):
        original = item()
        writer = self.client(FakeResponse(found()), FakeResponse({"results": [], "total_pages": 0}))
        self.assertEqual(writer.enrich(original, "movie")["matchStatus"], "no_match")
        path = writer._cache_path(tmdb._enrichment_identity(original, "movie")[0])
        for cache_path in self.cache.rglob("*.json"):
            entry = json.loads(cache_path.read_text())
            entry["data"]["matchStrategy"] = 2
            cache_path.write_text(json.dumps(entry), encoding="utf-8")
        retry = self.client(FakeResponse(found(101)), FakeResponse(details()))
        self.assertEqual(retry.enrichment_priority(original, "movie"), 0)
        expected = retry.enrich(original, "movie")
        self.assertEqual(expected["matchStatus"], "matched_search")
        self.assertEqual(len(retry.session.calls), 2)
        entry = json.loads(path.read_text())
        self.assertEqual(entry["data"]["identityPolicy"], 2)
        entry["data"]["matchStrategy"] = 2
        path.write_text(json.dumps(entry), encoding="utf-8")
        offline = tmdb.TmdbClient(self.cache, session=FakeSession(), now=1_800_000_001)
        self.assertEqual(offline.enrich(original, "movie"), expected)
        self.assertEqual(offline.stats["requests"], 0)
        self.assertFalse(offline.session.calls)

    def test_external_identity_requires_year_and_obeys_elapsed_budget_before_find(self):
        missing_year = self.client()
        result = missing_year.enrich(item(title="Example Film"), "movie")
        self.assertEqual(result["matchStatus"], "no_match")
        self.assertEqual(missing_year.last_match_diagnostic["reason"], "missing_identity")
        self.assertFalse(missing_year.session.calls)
        elapsed = self.client(max_elapsed_seconds=0)
        self.assertEqual(elapsed.enrich(item(), "movie")["matchStatus"], "unavailable")
        self.assertEqual(elapsed.stats["requests"], 0)
        self.assertFalse(elapsed.session.calls)

    def test_find_request_uses_the_same_budget_needed_to_complete_fallback_pages(self):
        responses = [FakeResponse(found())]
        for page in (1, 2):
            responses.append(FakeResponse({"page": page, "total_pages": 7, "total_results": 7,
                "results": [{"id": 300 + page, "title": "Other Film", "release_date": "2024-04-03"}]}))
        client = self.client(*responses, max_requests=3)
        result = client.enrich(item(), "movie")
        self.assertEqual(result["matchStatus"], "unavailable")
        self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")
        self.assertEqual(client.stats["requests"], 3)
        self.assertEqual(client.last_match_diagnostic["searchPages"], 2)
        self.assertEqual(len(client.session.calls), 3)


if __name__ == "__main__":
    unittest.main()
