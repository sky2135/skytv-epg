from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from vod_title_cleanup import preprocess_provider_decorations as clean
from vod_title_cleanup import provider_cleanup_requires_corroboration as needs_proof


class ProviderDecorationDiscoveryTests(unittest.TestCase):
    def test_observed_multilingual_terminal_labels_expose_original_display_year(self):
        cases = {
            "The Mongoose (2026) [HIN+ENG]": "The Mongoose (2026)",
            "The Conductor (2012) [RUS]": "The Conductor (2012)",
            "Tricky Old Dogs (2018) [FRN]": "Tricky Old Dogs (2018)",
            "Resident Evil (2026) (Turkish)": "Resident Evil (2026)",
            "Kothamrito (2022) (Bangla)": "Kothamrito (2022)",
            "Viral Sebi (2023) (Malyalam)": "Viral Sebi (2023)",
            "Ittaa Kittaa (2024) (Gujarati)": "Ittaa Kittaa (2024)",
            "Titanic (1997)[Tam, Tel, Hin, Eng]": "Titanic (1997)",
            "Everything Everywhere All at Once (2022)(Eng, Hin)": "Everything Everywhere All at Once (2022)",
            "Ayu Rekha (2024) (Bangla Audio)": "Ayu Rekha (2024)",
            "DeSu7 (2026) (Bangla) (CAM)": "DeSu7 (2026)",
            "Furiosa: A Mad Max Saga (2024) (Multi Languages)": "Furiosa: A Mad Max Saga (2024)",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(clean(original), expected)
                self.assertFalse(needs_proof(original))

    def test_pipe_labels_are_fields_not_additional_title_words(self):
        cases = {
            "Anakapalli - 2026 | MultiAudio": "Anakapalli - 2026",
            "The Court (2026) | Multi Audio": "The Court (2026)",
            "Aadu 3 - 2026 | Kannada": "Aadu 3 - 2026",
            "Jennifer (2026) | Malayalam": "Jennifer (2026)",
            "Thellakaagitham (2026) | Telugu": "Thellakaagitham (2026)",
            "Aakhri Sawal (2026) | [Tam + Tel]": "Aakhri Sawal (2026)",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(clean(original), expected)

    def test_country_wrappers_need_a_distinct_year_context(self):
        for code in ("US", "GB", "FR", "JP", "CA", "KR", "DE", "ES", "IN"):
            with self.subTest(code=code):
                self.assertEqual(clean(f"The Programme (2025) ({code})"), "The Programme (2025)")
                self.assertEqual(clean(f"The Programme ({code})"), f"The Programme ({code})")
                self.assertEqual(clean(f"The Programme ({code})", source_year="2025"), "The Programme")
        for title in ("Us (2019)", "IT (2017)", "(2025) (US)", "Film (us)"):
            self.assertEqual(clean(title), title)

    def test_terminal_punctuation_does_not_erase_title_punctuation(self):
        for title in ("Rescued by Ruby (2022).", "Janhit Mein Jaari (2022).", "Dr. No (1962).",
                      "What? (2020)!", "Film [2021]。"):
            with self.subTest(title=title):
                self.assertEqual(clean(title), title[:-1])
        for title in ("Dr. No", "What?", "(2022).", "Movie [HI)", "Film (2026) -"):
            self.assertEqual(clean(title), title)

    def test_new_spaced_provider_prefixes_require_independent_evidence(self):
        for prefix in ("EX", "NF", "LAT", "GR", "AL", "BG", "DK", "TG", "TM", "MA",
                       "AF", "STH", "SOM", "KD", "SC", "TOP", "PK", "IR", "IS"):
            with self.subTest(prefix=prefix):
                original=f"{prefix} - Example Film (2025)"
                self.assertEqual(clean(original), "Example Film (2025)")
                self.assertTrue(needs_proof(original))
        self.assertEqual(clean("NF | Example Film (2025)"), "Example Film (2025)")
        self.assertTrue(needs_proof("NF | Example Film (2025)"))

    def test_compact_prefixes_are_observed_locales_with_year_context(self):
        for prefix in ("DE", "PK", "IT", "FR"):
            with self.subTest(prefix=prefix):
                self.assertEqual(clean(f"{prefix}-Example Film (2025)"), "Example Film (2025)")
                self.assertTrue(needs_proof(f"{prefix}-Example Film (2025)"))
                self.assertEqual(clean(f"{prefix}-Example Film"), f"{prefix}-Example Film")
                self.assertEqual(clean(f"{prefix}-Example Film", source_year="2025"), "Example Film")
        for title in ("EX-Husbands (2024)", "NF-Example Film (2025)", "ABC - Example Film (2025)",
                      "FR: A Real Title (2025)", "In - A Real Title (2025)"):
            self.assertEqual(clean(title), title)
            self.assertFalse(needs_proof(title))

    def test_original_numeric_titles_sequels_and_language_words_are_preserved(self):
        titles = ("1947", "1917", "1984", "2001", "2049", "Blade Runner 2049", "Class of 1984",
                  "English Vinglish", "Tutt Paini English Ne", "Movie Punjabi", "English", "4K",
                  "2001: A Space Odyssey", "Film 2", "Doctor Strange 2", "Part II",
                  "BTS: THE RETURN", "NFT: Cursed Images", "IT: Chapter Two", "Before 2000")
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(clean(title), title)
                self.assertEqual(clean(title + " (2026)"), title + " (2026)")
        self.assertEqual(clean("1917 (2019) (French)"), "1917 (2019)")
        self.assertEqual(clean("Blade Runner 2049 (2017) (French)"), "Blade Runner 2049 (2017)")

    def test_unknown_cuts_event_parts_and_unbalanced_wrappers_remain_identity_bearing(self):
        for suffix in ("Director's Cut", "Prelims", "Early Prelims", "Main Card", "Part II", "Unknown", "Kids", "D", "AI"):
            title = f"A Real Film (2026) ({suffix})"
            self.assertEqual(clean(title), title)
        for title in ("Film (2026) [HIN+UNKNOWN]", "Film (2026) [English Vinglish]", "Film (2026) [FRN)"):
            self.assertEqual(clean(title), title)

    def test_context_is_not_inferred_from_categories_audio_or_unwrapped_real_words(self):
        for title in ("Movie (French)", "Movie | French", "Movie (Multi Audio)", "Multi Audio (2025)",
                      "Movie French", "Hindi Medium", "A Punjabi"):
            self.assertEqual(clean(title), title)
        self.assertEqual(clean("Movie (French)", source_year="2025"), "Movie")
        self.assertEqual(clean("Movie (2026) (French)", source_year="2025"), "Movie (2026)")
        self.assertEqual(clean("Movie (French)", source_year="invalid"), "Movie (French)")

    def test_arabic_classifier_is_series_and_explicit_raw_provider_context_only(self):
        title = "AR - مسلسل وصفة الحب"
        self.assertEqual(clean(title, kind="series"), "وصفة الحب")
        self.assertTrue(needs_proof(title, kind="series"))
        for kind in (None, "movie"):
            self.assertEqual(clean(title, kind=kind), "مسلسل وصفة الحب")
        for title in ("مسلسل وصفة الحب", "فيلم وصفة الحب", "EN - مسلسل وصفة الحب", "AR: مسلسل وصفة الحب"):
            self.assertIn("مسلسل" if "مسلسل" in title else "فيلم", clean(title, kind="series"))

    def test_existing_only_labels_keep_original_parser_ownership_and_limits(self):
        for title in ("Movie (2025) (Hindi)", "Movie (2025) [HD]", "Movie (2025) (CAM)"):
            self.assertEqual(clean(title), title)
        for title in ("Movie (2026)" + " [HD]" * 20, "Movie (2026)" + " (Turkish)" * 20,
                      "NF - " * 20 + "Movie (2026)", "x" * 3000):
            self.assertEqual(clean(title), title)


if __name__ == "__main__":
    unittest.main()
