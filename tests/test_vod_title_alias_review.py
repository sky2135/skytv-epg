from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import vod_reviewed_corrections as reviewed
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession


CASES = (
    ("server_2", "2464200", "Solvan Saal 1958 Punjabi", "Solva Saal", "1958", "275784", "pa"),
    ("server_1", "826646", "The Bloodline of Bathory (2026) (English)",
     "The Bloodline of Bathory", "2026", "1756427", "en"),
    ("server_1", "825453", "Street Fighter (2026) (Hindi) (CAM)", "Street Fighter", "2026", "1153576", "hi"),
    ("server_1", "824536", "Thaapi (2026) (Punjabi) (CAM)", "Thaapi", "2026", "1784165", "pa"),
    ("server_3", "780271", "ENG - Eat Pray Love 2010", "Eat Pray Love", "2010", "38167", "en"),
    ("server_3", "611067", "PB - Thana Sadar 2021", "Thana Sadar", "2021", "898000", "pa"),
    ("server_3", "744220", "IN - Dangal (2016)", "Dangal", "2016", "360814", "hi"),
)


class ReviewedSolvaSaalAliasTests(unittest.TestCase):
    def setUp(self):
        reviewed._rules.cache_clear()
        self.addCleanup(reviewed._rules.cache_clear)

    def item(self, case=CASES[0], **changes):
        server, identifier, title, _alias, _year, _tmdb, language = case
        result = {"id": identifier, "contentId": f"{server}:movie:{identifier}",
                  "title": title, "languages": [language], "language": language,
                  "releaseDate": None, "tmdbId": None,
                  "artwork": {"posterUrl": None, "backdropUrl": None}}
        result.update(changes)
        return result

    def test_rule_is_exact_scoped_search_only_without_forced_id_date_or_audio(self):
        document = json.loads((ROOT / "data/vod_reviewed_corrections.json").read_text(encoding="utf-8"))
        rules = reviewed._validate_document(document)
        for case in CASES:
            server, record, display, alias, year, external_id, _language = case
            with self.subTest(server=server, record=record):
                rule = rules[(server, "movie", record)]
                self.assertEqual(rule["displayNames"], [display])
                self.assertEqual(rule["searchTitle"], alias)
                self.assertEqual(rule["year"], year)
                self.assertEqual(rule["metadata"], {})
                self.assertFalse(rule["rejectIdentity"])
                self.assertEqual(rule["evidenceURLs"], [f"https://www.themoviedb.org/movie/{external_id}"])
                self.assertNotIn("title", rule)
                original = self.item(case)
                before = copy.deepcopy(original)
                corrected = reviewed.apply_reviewed_correction(server, "movie", original)
                self.assertEqual(original, before)
                self.assertEqual(corrected, before)
                _key, identifier, search_title, query_year = tmdb._enrichment_identity(corrected, "movie")
                self.assertEqual((identifier, search_title, query_year), (None, alias, year))

    def test_all_reviewed_search_contexts_receive_priority_before_uncached_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            client = tmdb.TmdbClient(Path(directory), session=FakeSession())
            for case in CASES:
                with self.subTest(content_id=self.item(case)["contentId"]):
                    self.assertEqual(-1, client.enrichment_priority(self.item(case), "movie"))
            self.assertEqual(0, client.enrichment_priority({"title": "An ordinary uncached film (2026)"}, "movie"))
            self.assertEqual(client.stats["requests"], 0)

    def test_alias_does_not_follow_recycled_id_different_server_or_media_kind(self):
        for case in CASES:
            server = case[0]
            item = self.item(case)
            self.assertIsNotNone(reviewed.correction_for(server, "movie", item))
            variants = [(other, "movie", item) for other in ("server_1", "server_2", "server_3") if other != server]
            variants += [(server, "series", item), (server, "movie", self.item(case, id="9999999999")),
                         (server, "movie", self.item(case, title="An unrelated replacement (1958)"))]
            for target_server, kind, changed in variants:
                with self.subTest(server=target_server, kind=kind, changed=changed):
                    self.assertIsNone(reviewed.correction_for(target_server, kind, changed))

    def test_alias_still_requires_successful_external_title_year_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            offline = tmdb.TmdbClient(Path(directory), session=FakeSession())
            for case in CASES:
                with self.subTest(content_id=self.item(case)["contentId"]):
                    result = offline.enrich(self.item(case), "movie")
                    self.assertEqual("unavailable", result["matchStatus"])
                    self.assertIsNone(result["tmdbId"])
                    self.assertIsNone(result["releaseDate"])
            self.assertEqual(offline.stats["requests"], 0)
        with tempfile.TemporaryDirectory() as directory:
            details = {"id": 275784, "title": "Solva Saal", "original_title": "Solva Saal",
                       "release_date": "1958-02-14", "original_language": "hi"}
            client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(
                FakeResponse({"page": 1, "total_pages": 1, "total_results": 1, "results": [details]}),
                FakeResponse(details)))
            item = self.item()
            result = client.enrich(item, "movie")
            self.assertEqual("matched_search", result["matchStatus"])
            self.assertEqual("275784", result["tmdbId"])
            self.assertEqual("1958-02-14", result["releaseDate"])
            self.assertEqual(item["languages"], ["pa"])
            self.assertEqual(item["title"], "Solvan Saal 1958 Punjabi")


if __name__ == "__main__":
    unittest.main()
