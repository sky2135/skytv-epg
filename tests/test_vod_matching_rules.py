from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


def translated_details(identifier=101, *, title="Example Film", translated="Film exemple", year="2025"):
    details = movie_details(identifier)
    details.update(title=title, release_date=f"{year}-04-03" if year else None,
                   images={"posters": [{"file_path": "/abc123.jpg"}]},
                   translations={"id": identifier, "translations": [{"data": {"title": translated}}]})
    return details


def provider_item(title="EX - Example Film (2025)", **changes):
    result = {"title": title, "tmdbId": "101", "artwork": {
        "posterUrl": "https://image.tmdb.org/t/p/w500/abc123.jpg"}}
    result.update(changes)
    return result


def search(*identifiers):
    return FakeResponse({"page": 1, "total_pages": 1, "total_results": len(identifiers),
                         "results": [{"id": identifier, "title": "Unrelated primary title",
                                      "release_date": "2025-04-03"} for identifier in identifiers]})


class SafeMatchingRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, *responses):
        return tmdb.TmdbClient(self.root, token="offline-fixture-token", session=FakeSession(*responses),
                               now=1_800_000_000)

    def proof(self, client, source, kind="movie"):
        value, _ = client._read_cache(tmdb._enrichment_identity(source, kind)[0])
        return value["identity"]

    def test_official_translation_is_cached_at_existing_key_and_bound_to_display(self):
        source = {"title": "Film exemple (2025)", "tmdbId": "101"}
        client = self.client(FakeResponse(translated_details()))
        key_before = tmdb._enrichment_identity(source, "movie")[0]
        result = client.enrich(source, "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("101", "matched_id"))
        proof = self.proof(client, source)
        self.assertEqual(proof["translationDisplayKey"], "film exemple")
        self.assertTrue(tmdb._identity_binding_valid(proof))
        self.assertEqual(tmdb._enrichment_identity(source, "movie")[0], key_before)
        client.session = FakeSession()
        self.assertEqual(client.enrich(source, "movie"), result)
        self.assertEqual(client.session.calls, [])

    def test_translation_vowel_marks_cannot_transfer_between_colliding_legacy_keys(self):
        source = {"title": "काला (2025)", "tmdbId": "101"}
        different = {"title": "काली (2025)", "tmdbId": "101"}
        client = self.client(FakeResponse(translated_details(translated="काला")))
        self.assertEqual(tmdb._enrichment_identity(source, "movie")[0],
                         tmdb._enrichment_identity(different, "movie")[0])
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_id")
        key, identifier, title, year = tmdb._enrichment_identity(different, "movie")
        self.assertIsNone(client._main_identity_cache(key, different, "movie", identifier, title, year)[0])

    def test_complete_translated_search_must_disambiguate_every_candidate(self):
        client = self.client(search(101, 102), FakeResponse(translated_details(101)),
                             FakeResponse(translated_details(102)))
        self.assertEqual(client.enrich({"title": "Film exemple (2025)"}, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "ambiguous_title_year")
        self.assertEqual(len(client.session.calls), 3)

    def test_incomplete_competing_translation_does_not_establish_unique_alias(self):
        other = translated_details(102, translated="Other film")
        other.pop("translations")
        client = self.client(search(101, 102), FakeResponse(translated_details(101)), FakeResponse(other))
        self.assertEqual(client.enrich({"title": "Film exemple (2025)"}, "movie")["matchStatus"], "unavailable")
        self.assertEqual(client.last_match_diagnostic["reason"], "incomplete_search")

    def test_translation_and_original_survive_bounded_identity_name_cache(self):
        details = translated_details()
        details["alternative_titles"] = {"titles": [{"title": f"Alias {index}"} for index in range(100)]}
        client = self.client(FakeResponse(details))
        source = {"title": "Film exemple (2025)", "tmdbId": "101"}
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_id")
        proof = self.proof(client, source)
        self.assertEqual(len(proof["names"]), 102)
        self.assertIn("film exemple", proof["names"])
        self.assertIn("original film", proof["names"])
        client.session = FakeSession()
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_id")

    def test_new_prefix_requires_exact_official_poster_and_preserves_provider_fields(self):
        source = provider_item()
        before = copy.deepcopy(source)
        key_before = tmdb._enrichment_identity(source, "movie")[0]
        client = self.client(FakeResponse(translated_details()))
        result = client.enrich(source, "movie")
        self.assertEqual(result["matchStatus"], "matched_poster")
        self.assertEqual(source, before)
        self.assertEqual(tmdb._enrichment_identity(source, "movie")[0], key_before)
        proof = self.proof(client, source)
        self.assertEqual(proof["providerTitle"], {"policyVersion": 1, "sourceTitleKey": "ex example film",
                         "sourceYear": "2025", "matchTitleKey": "example film", "matchYear": "2025"})
        self.assertIn("images", client.session.calls[0][1]["params"]["append_to_response"])
        client.session = FakeSession()
        self.assertEqual(client.enrich(source, "movie"), result)
        self.assertEqual(client.session.calls, [])

    def test_new_prefix_without_artwork_does_not_promote_provider_id(self):
        client = self.client()
        source = provider_item(artwork={})
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.session.calls, [])

    def test_new_prefix_rejects_wrong_poster_year_and_script_collision(self):
        for changes, reason in (({"poster_path": "/other.jpg", "images": {"posters": []}}, "title_mismatch"),
                                ({"release_date": "2024-01-01"}, "year_mismatch"),
                                ({"title": "काली", "original_title": "काली"}, "title_mismatch")):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                details = translated_details(translated="Unrelated")
                details.update(changes)
                source = provider_item("EX - काला (2025)" if "title" in changes else "EX - Example Film (2025)")
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(FakeResponse(details)),
                                         now=1_800_000_000)
                self.assertEqual(client.enrich(source, "movie")["matchStatus"], "no_match")
                self.assertEqual(client.last_match_diagnostic["reason"], reason)

    def test_terminal_language_cleanup_reveals_year_without_poster_exception(self):
        source = {"title": "Example Film (2025) (GERMAN)", "tmdbId": "101"}
        client = self.client(FakeResponse(movie_details()))
        self.assertEqual(tmdb._title_and_year(source)[1], None)
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_id")
        self.assertEqual(self.proof(client, source)["providerTitle"]["matchYear"], "2025")
        self.assertNotIn("images", client.session.calls[0][1]["params"]["append_to_response"])

    def test_terminal_cleanup_cannot_promote_colliding_indic_official_title(self):
        details = translated_details(title="काली", translated="Unrelated")
        details["original_title"] = "काली"
        client = self.client(FakeResponse(details))
        source = {"title": "काला (2025) (GERMAN)", "tmdbId": "101"}
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "title_mismatch")

    def test_arabic_series_classifier_needs_raw_context_and_official_poster(self):
        details = {"id": 34899, "name": "Magnificent Century", "original_name": "Muhteşem Yüzyıl",
                   "first_air_date": "2011-01-05", "poster_path": "/abc123.jpg", "images": {"posters": []},
                   "translations": {"id": 34899, "translations": [{"data": {"name": "حريم السلطان"}}]}}
        source = provider_item("AR - مسلسل حريم السلطان", tmdbId="34899")
        client = self.client(FakeResponse(details))
        self.assertEqual(client.enrich(source, "series")["matchStatus"], "matched_poster")
        proof = self.proof(client, source, "series")
        self.assertEqual(proof["translationDisplayKey"], "حريم السلطان")
        self.assertEqual(proof["providerTitle"]["sourceTitleKey"], "مسلسل حريم السلطان")
        self.assertEqual(tmdb._title_and_year({"title": "مسلسل حريم السلطان"}, cleanup=True, kind="series")[0],
                         "مسلسل حريم السلطان")

    def seed(self, client, *, title="example film", kind="movie", identifier="101", year="2025"):
        client.poster_index.add(kind, identifier, ["/abc123.jpg"], {"names": [title], "year": year})

    def test_missing_source_year_uses_unique_verified_poster_and_fresh_details(self):
        source = provider_item("Example Film", tmdbId=None)
        client = self.client(FakeResponse(translated_details()))
        self.seed(client)
        self.assertEqual(client.poster_candidate(source, "movie"), "101")
        result = client.enrich(source, "movie")
        self.assertEqual((result["tmdbId"], result["matchStatus"]), ("101", "matched_poster"))
        self.assertEqual(len(client.session.calls), 1)
        self.assertTrue(client.session.calls[0][0].endswith("/movie/101"))
        proof = self.proof(client, source)
        self.assertEqual(proof["providerTitle"]["sourceYear"], None)
        self.assertEqual(proof["providerTitle"]["matchYear"], None)
        client.session = FakeSession()
        self.assertEqual(client.enrich(source, "movie"), result)
        self.assertEqual(client.session.calls, [])

    def test_missing_source_year_requires_complete_images_and_exact_script(self):
        for label, source_title, detail_changes, expected in (
            ("missing images", "Example Film", {"images": None}, "unavailable"),
            ("different official title", "Example Film", {"title": "Other", "original_title": "Other"}, "no_match"),
            ("script collision", "काला", {"title": "काली", "original_title": "काली"}, "no_match"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                details = translated_details(translated="Unrelated")
                details.update(detail_changes)
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(FakeResponse(details)),
                                         now=1_800_000_000)
                self.seed(client, title=tmdb._normalized_title(source_title))
                self.assertEqual(client.enrich(provider_item(source_title, tmdbId=None), "movie")["matchStatus"], expected)

    def test_missing_year_index_never_uses_unknown_year_cross_kind_or_collision(self):
        for kind, year, collision in (("movie", None, False), ("series", "2025", False), ("movie", "2025", True)):
            with self.subTest(kind=kind, year=year, collision=collision), tempfile.TemporaryDirectory() as directory:
                client = tmdb.TmdbClient(Path(directory), token="fixture", session=FakeSession(), now=1_800_000_000)
                self.seed(client, kind=kind, year=year)
                if collision:
                    self.seed(client, identifier="102")
                source = provider_item("Example Film", tmdbId=None)
                self.assertIsNone(client.poster_candidate(source, "movie"))
                self.assertEqual(client.enrich(source, "movie")["matchStatus"], "no_match")
                self.assertEqual(client.session.calls, [])

    def test_missing_year_diagnostic_does_not_claim_year_conflict(self):
        client = self.client(FakeResponse(movie_details()))
        self.assertEqual(client.enrich({"title": "Different Title", "tmdbId": "101"}, "movie")["matchStatus"], "no_match")
        self.assertEqual(client.last_match_diagnostic["reason"], "title_mismatch")

    def test_binding_tampering_and_source_change_fail_closed(self):
        client = self.client(FakeResponse(translated_details()))
        source = provider_item()
        self.assertEqual(client.enrich(source, "movie")["matchStatus"], "matched_poster")
        proof = self.proof(client, source)
        for field, value in (("policyVersion", True), ("matchTitleKey", "other"), ("matchYear", 2025),
                             ("sourceYear", "2025-01-01"), ("extra", "unexpected")):
            changed = copy.deepcopy(proof)
            changed["providerTitle"][field] = value
            self.assertFalse(tmdb._identity_binding_valid(changed), (field, value))
        changed_source = provider_item("NF - Example Film (2025)")
        _, _, title, year = tmdb._enrichment_identity(changed_source, "movie")
        self.assertIsNone(tmdb._current_query_identity(proof, changed_source, "movie", title, year))


if __name__ == "__main__":
    unittest.main()
