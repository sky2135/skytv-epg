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

import diagnose_vod_matches as diagnostic  # noqa: E402
from test_vod_tmdb import FakeResponse, FakeSession, movie_details  # noqa: E402


def entry(number=1, *, title="Example Film (2026)", supplied_id=None):
    content_id = f"server_1:movie:{number}"
    return {"row": number, "contentId": content_id, "providerTitle": title,
            "publishedStatus": "no_match", "tmdbId": None,
            "matcherInput": {"id": str(number), "contentId": content_id, "title": title,
                             "tmdbId": supplied_id, "releaseDate": None, "languages": ["en"]}}


def document(*rows):
    return {"snapshotGeneratedAt": "2026-10-21T13:53:05Z", "sourceProof": {"sha256": "a" * 64},
            "records": list(rows or [entry()])}


def candidate(identifier=101, title="Example Film", release="2026-10-10"):
    return {"id": identifier, "title": title, "original_title": title, "release_date": release}


def search(*candidates, pages=1):
    return FakeResponse({"total_pages": pages, "total_results": len(candidates), "results": list(candidates)})


class VodMatchDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, *responses, max_requests=900):
        return diagnostic.TraceClient(self.root / "private-cache", token="read-token-secret",
                                      session=FakeSession(*responses), max_requests=max_requests)

    def reason(self, client, row=None):
        report = diagnostic.diagnose(document(row or entry()), client, yearless=False)
        self.assertEqual(report["recordCount"], 1)
        return report["records"][0]["reason"]

    def test_exact_search_rejection_branches(self):
        cases = [
            (search(pages=0), "no_search_results"),
            (search(candidate(), pages=2), "multiple_search_pages"),
            (search(candidate(101), candidate(102)), "multiple_exact_title_year_matches"),
            (search(candidate(release=None)), "same_title_missing_full_date"),
            (search(candidate(release="2025-10-10")), "same_title_different_primary_year"),
            (search(candidate(title="A Different Film")), "returned_titles_do_not_match"),
        ]
        for number, (response, expected) in enumerate(cases):
            with self.subTest(reason=expected):
                client = diagnostic.TraceClient(self.root / str(number), token="read-token-secret",
                                                session=FakeSession(response))
                self.assertEqual(self.reason(client), expected)
                self.assertEqual(len(client.session.calls), 1)

    def test_supplied_id_details_must_still_match_independent_title_and_year(self):
        for title, release in (("Another Film", "2026-10-10"), ("Example Film", "2025-10-10")):
            with self.subTest(title=title, release=release):
                client = self.client(FakeResponse({**movie_details(), "title": title,
                                                  "release_date": release}))
                # Each variant must execute against a fresh cache.
                client.cache_dir = self.root / title.replace(" ", "_") / release
                reason = self.reason(client, entry(supplied_id="101"))
                self.assertEqual(reason, "details_identity_conflict")
                self.assertEqual(client.session.calls[0][0], "https://api.themoviedb.org/3/movie/101")

    def test_missing_supplied_id_has_its_own_diagnostic(self):
        client = self.client(FakeResponse(status=404))
        self.assertEqual(self.reason(client, entry(supplied_id="101")), "details_not_found")

    def test_authoritative_matcher_input_preserves_provider_id_absent_from_public_catalog(self):
        client = self.client(FakeResponse({**movie_details(), "release_date": "2026-10-10"}))
        row = entry(supplied_id="101")
        report = diagnostic.diagnose(document(row), client, yearless=False)
        result = report["records"][0]
        self.assertEqual(result["reason"], "matched_on_replay")
        self.assertEqual(result["replayTmdbId"], "101")
        self.assertEqual(result["replayReleaseDate"], "2026-10-10")
        self.assertEqual(len(client.session.calls), 1)
        self.assertIn("/movie/101", client.session.calls[0][0])
        self.assertIsNone(row["tmdbId"])

    def test_trace_and_report_exclude_authentication_api_urls_and_response_extras(self):
        extra = {"private_provider_url": "https://provider.invalid/user/password/1.mp4",
                 "api_key": "response-extra-secret", "raw_http_response": "response-extra-secret"}
        client = self.client(
            FakeResponse({"total_pages": 1, "results": [{**candidate(), **extra}], **extra}),
            FakeResponse({**movie_details(), "release_date": "2026-10-10", **extra}),
        )
        report = diagnostic.diagnose(document(), client, yearless=False)
        serialized = json.dumps(report)
        for private in ("read-token-secret", "response-extra-secret", "provider.invalid", "api_key",
                        "api.themoviedb.org", "raw_http_response", "private_provider_url"):
            self.assertNotIn(private, serialized)
        trace = report["records"][0]["trace"]
        self.assertEqual(trace[0]["candidates"][0]["sourceURL"], "https://www.themoviedb.org/movie/101")
        self.assertEqual(trace[1]["candidate"]["tmdbId"], "101")

    def test_repeated_query_is_reused_but_content_ids_remain_distinct(self):
        client = self.client(search(pages=0))
        original = document(entry(1), entry(2))
        before = copy.deepcopy(original)
        report = diagnostic.diagnose(original, client, yearless=False)
        self.assertEqual(report["uniqueQueries"], 1)
        self.assertEqual(report["recordCount"], 2)
        self.assertEqual(client.stats["requests"], 1)
        self.assertEqual([row["contentId"] for row in report["records"]], ["server_1:movie:1", "server_1:movie:2"])
        self.assertEqual([row["reusedDiagnosticQuery"] for row in report["records"]], [False, True])
        report["records"][1]["trace"].append({"mutated": True})
        self.assertNotEqual(report["records"][0]["trace"], report["records"][1]["trace"])
        self.assertEqual(original, before)

    def test_yearless_review_does_not_change_the_strict_matcher_reason_or_publish_a_match(self):
        client = self.client(search(pages=0), search(candidate(release="2025-10-10")))
        report = diagnostic.diagnose(document(), client)
        row = report["records"][0]
        self.assertEqual(row["reason"], "no_search_results")
        self.assertEqual(row["replayStatus"], "no_match")
        self.assertIsNone(row["replayTmdbId"])
        self.assertEqual([trace["phase"] for trace in row["trace"]], ["matcher", "yearless_review"])
        self.assertEqual(client.session.calls[0][1]["params"]["year"], "2026")
        self.assertNotIn("year", client.session.calls[1][1]["params"])

    def test_request_cap_returns_an_explicit_partial_report(self):
        client = self.client(search(pages=0), max_requests=1)
        report = diagnostic.diagnose(document(entry(1), entry(2, title="Second Film (2026)")), client,
                                     yearless=False)
        self.assertEqual(report["inputCount"], 2)
        self.assertEqual(report["recordCount"], 1)
        self.assertEqual(report["stoppedReason"], "budget")
        self.assertEqual(report["stats"]["requests"], 1)

    def test_time_cap_returns_an_explicit_partial_report(self):
        client = self.client(search(pages=0))
        clock = [0]
        enrich = client.enrich

        def advance_after_enrich(item, kind):
            result = enrich(item, kind)
            clock[0] = 601
            return result

        with mock.patch.object(diagnostic.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(client, "enrich", side_effect=advance_after_enrich):
            report = diagnostic.diagnose(document(entry(1), entry(2, title="Second Film (2026)")), client,
                                         yearless=False)
        self.assertEqual(report["recordCount"], 1)
        self.assertEqual(report["stoppedReason"], "budget")
        self.assertEqual(client.stats["requests"], 1)

    def test_invalid_inputs_are_fully_checked_before_any_lookup(self):
        bad_later = entry(2)
        bad_later["matcherInput"]["contentId"] = "server_1:movie:9"
        malformed_id = entry()
        malformed_id["contentId"] = malformed_id["matcherInput"]["contentId"] = "server_1:movie:not-a-number"
        bad_documents = [None, [], {}, {"records": []}, {"records": [None]},
                         document(entry(), entry()), document(entry(), bad_later), document(malformed_id)]
        for value in bad_documents:
            with self.subTest(value=value):
                client = self.client()
                with self.assertRaises(ValueError):
                    diagnostic.diagnose(value, client, yearless=False)
                self.assertEqual(client.session.calls, [])

    def test_invalid_budgets_are_checked_before_any_lookup(self):
        for seconds in (0, -1, 901, float("inf"), float("nan")):
            with self.subTest(seconds=seconds):
                client = self.client()
                with self.assertRaises(ValueError):
                    diagnostic.diagnose(document(), client, max_seconds=seconds)
                self.assertEqual(client.session.calls, [])


if __name__ == "__main__":
    unittest.main()
