from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


class ProviderTitleCleanupTests(unittest.TestCase):
    def test_observed_raw_provider_titles_get_only_title_and_year_cleanup(self):
        cases = [
            ("IN - Dangal  (2016)", "Dangal", "2016"),
            ("Street Fighter (2026) (Hindi) (CAM)", "Street Fighter", "2026"),
            ("Thaapi (2026) (Punjabi) (CAM)", "Thaapi", "2026"),
            ("ENG - Eat Pray Love 2010", "Eat Pray Love", "2010"),
            ("PB - Thana Sadar 2021", "Thana Sadar", "2021"),
            ("Solvan Saal 1958 Punjabi", "Solvan Saal", "1958"),
            ("Zombie Ka Aatank 2021", "Zombie Ka Aatank", "2021"),
            ("The Bloodline of Bathory (2026) (English)", "The Bloodline of Bathory", "2026"),
        ]
        for source, title, year in cases:
            with self.subTest(source=source):
                item = {"title": source, "releaseDate": None}
                original = copy.deepcopy(item)
                self.assertEqual(tmdb._title_and_year(item), (title, year))
                self.assertEqual(item, original)

    def test_known_enclosed_decorators_are_iterative_and_order_independent(self):
        for source in (
            "Example Film (2026) (Hindi) (CAM)",
            "Example Film [2026] [Punjabi] [HDTC]",
            "Example Film (English Dubbed) [HDTS] (2026)",
            "Example Film (2026) (HDCAM) [TC] [HD]",
            "Example Film [FHD] (2026) (Tamil) [SD]",
        ):
            with self.subTest(source=source):
                self.assertEqual(tmdb._title_and_year({"title": source}), ("Example Film", "2026"))

    def test_title_numbers_sequels_and_year_words_are_preserved(self):
        for source in ("1917", "1984", "2001", "2049", "2001: A Space Odyssey", "Blade Runner 2049",
                       "Class of 1984", "Summer of 1984", "In the Year 2000", "Film 2",
                       "Doctor Strange 2", "Part II", "Before 2000"):
            with self.subTest(source=source):
                self.assertEqual(tmdb._title_and_year({"title": source}), (source, None))
        self.assertEqual(tmdb._title_and_year({"title": "1917 (2019)"}), ("1917", "2019"))
        self.assertEqual(tmdb._title_and_year({"title": "Film 2 (2026)"}), ("Film 2", "2026"))
        self.assertEqual(tmdb._title_and_year({"title": "Blade Runner 2049 (2017)"}), ("Blade Runner 2049", "2017"))

    def test_explicit_source_date_protects_identity_year_in_bare_title(self):
        for source, release in (("Wonder Woman 1984", "2020-12-16"),
                                ("Blade Runner 2049", "2017-10-04")):
            with self.subTest(source=source):
                self.assertEqual(tmdb._title_and_year({"title": source, "releaseDate": release}),
                                 (source, release[:4]))
        self.assertEqual(tmdb._title_and_year({"title": "Example Film 2026", "year": "2025"}),
                         ("Example Film 2026", "2025"))
        self.assertEqual(tmdb._title_and_year({"title": "Example Film (2026)", "releaseDate": "1994-01-01"}),
                         ("Example Film", "2026"))

    def test_language_words_acronyms_and_real_subtitles_remain_identity_bearing(self):
        for source in ("English Vinglish", "Tutt Paini English Ne", "Johnny English", "A Punjabi",
                       "Hindi Medium", "Movie Punjabi", "English", "DUBBED",
                       "BTS: THE RETURN", "NFT: Cursed Images", "IT: Chapter Two",
                       "Star Wars: Episode IV - A New Hope", "In - A Real Title"):
            with self.subTest(source=source):
                self.assertEqual(tmdb._title_and_year({"title": source + " (2026)"}), (source, "2026"))
        for source in ("Movie [Director's Cut]", "Movie (Unknown)", "Movie [HI)", "(2026)", "[HD]"):
            with self.subTest(source=source):
                self.assertEqual(tmdb._title_and_year({"title": source}), (source, None))

    def test_multiple_display_years_are_not_all_erased_and_parsing_is_bounded(self):
        self.assertEqual(tmdb._title_and_year({"title": "Movie (2001) (2026)"}), ("Movie (2001)", "2026"))
        long_source = "Movie (2026)" + " [HD]" * 20
        title, year = tmdb._title_and_year({"title": long_source})
        self.assertIn("[HD]", title)
        self.assertIsNone(year)

    def test_normal_matcher_receives_clean_name_and_year_without_fabricated_release_date(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession(
                FakeResponse({"page": 1, "total_pages": 1, "total_results": 1, "results": [
                    {"id": 1784165, "title": "Thaapi", "release_date": "2026-10-16"}]}),
                FakeResponse({**movie_details(1784165), "title": "Thaapi", "original_title": "Thaapi",
                              "release_date": "2026-10-16"}))
            client = tmdb.TmdbClient(Path(directory), token="offline-test", session=session)
            source = {"title": "Thaapi (2026) (Punjabi) (CAM)", "releaseDate": None}
            metadata = client.enrich(source, "movie")
            self.assertEqual(session.calls[0][1]["params"]["query"], "Thaapi")
            self.assertEqual(session.calls[0][1]["params"]["year"], "2026")
            self.assertEqual(metadata["matchStatus"], "matched_search")
            self.assertEqual(metadata["releaseDate"], "2026-10-16")
            self.assertIsNone(source["releaseDate"])


if __name__ == "__main__":
    unittest.main()
