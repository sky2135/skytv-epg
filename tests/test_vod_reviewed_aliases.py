from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vod_reviewed_corrections as reviewed
import vod_tmdb as tmdb


CASES = (
    ("824104", "Deathgasm 2: Goremageddon (2026) (English)",
     "Deathgasm II: Goremageddon", "https://deathgasm2.com/"),
    ("712656", "Bring Me the Horizon: L.I.V.E. in São Paulo (Live Immersive Virtual Experiment) (2026)",
     "Bring Me the Horizon: L.I.V.E. in São Paulo",
     "https://www.sonymusic.com/sonymusic/bring-me-the-horizon-unveil-trailer-and-launch-tickets-for-live-in-sao-paulo/"),
)


class NoNetwork:
    def get(self, *_args, **_kwargs):
        raise AssertionError("Reviewed aliases cannot force a lookup or identity")


class ReviewedMovieAliasesTests(unittest.TestCase):
    def setUp(self):
        reviewed._rules.cache_clear()
        self.addCleanup(reviewed._rules.cache_clear)

    def item(self, record_id, title):
        return {"id": record_id, "contentId": f"server_1:movie:{record_id}", "title": title,
                "tmdbId": None, "releaseDate": None, "languages": ["en"]}

    def test_document_contains_only_source_backed_search_aliases_for_these_records(self):
        document = json.loads((ROOT / "data/vod_reviewed_corrections.json").read_text(encoding="utf-8"))
        self.assertEqual(document["reviewedAt"], "2026-10-22")
        validated = reviewed._validate_document(document)
        for record_id, title, alias, evidence in CASES:
            with self.subTest(record_id=record_id):
                rule = validated[("server_1", "movie", record_id)]
                self.assertEqual(rule["displayNames"], [title])
                self.assertEqual(rule["searchTitle"], alias)
                self.assertEqual(rule["year"], "2026")
                self.assertEqual(rule["metadata"], {})
                self.assertEqual(rule["evidenceURLs"], [evidence])
                self.assertFalse(rule["rejectIdentity"])
                self.assertNotIn("title", rule)
                self.assertNotIn("rejectedTmdbIds", rule)

    def test_record_server_kind_and_recycled_title_guards_are_required(self):
        for record_id, title, _alias, _evidence in CASES:
            with self.subTest(record_id=record_id):
                item = self.item(record_id, title)
                self.assertIsNotNone(reviewed.correction_for("server_1", "movie", item))
                self.assertIsNone(reviewed.correction_for("server_2", "movie", item))
                self.assertIsNone(reviewed.correction_for("server_3", "movie", item))
                self.assertIsNone(reviewed.correction_for("server_1", "series", item))
                self.assertIsNone(reviewed.correction_for("server_1", "movie", dict(item, id="9999999999")))
                recycled = dict(item, title="An unrelated replacement movie (2026)")
                self.assertIsNone(reviewed.correction_for("server_1", "movie", recycled))

    def test_alias_changes_matcher_query_only_without_dates_ids_or_display_changes(self):
        for record_id, title, alias, _evidence in CASES:
            with self.subTest(record_id=record_id):
                original = self.item(record_id, title)
                before = copy.deepcopy(original)
                corrected = reviewed.apply_reviewed_correction("server_1", "movie", original)
                self.assertEqual(original, before)
                self.assertEqual(corrected, before)
                _key, identifier, search_title, year = tmdb._enrichment_identity(corrected, "movie")
                self.assertEqual((identifier, search_title, year), (None, alias, "2026"))
                self.assertIsNone(corrected["releaseDate"])
                self.assertIsNone(corrected["tmdbId"])

    def test_alias_does_not_force_match_without_external_identity_verification(self):
        with tempfile.TemporaryDirectory() as cache:
            client = tmdb.TmdbClient(Path(cache), session=NoNetwork())
            for record_id, title, _alias, _evidence in CASES:
                with self.subTest(record_id=record_id):
                    result = client.enrich(self.item(record_id, title), "movie")
                    self.assertEqual(result["matchStatus"], "unavailable")
                    self.assertIsNone(result["tmdbId"])
                    self.assertIsNone(result["releaseDate"])
            self.assertEqual(client.stats["requests"], 0)


if __name__ == "__main__":
    unittest.main()
