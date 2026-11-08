from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vod_series_language import classify_series_language  # noqa: E402


class SeriesLanguageTests(unittest.TestCase):
    def classify(self, title, original="hi", sources=(), *, verified=True):
        result = classify_series_language(title, original, sources, metadata_verified=verified)
        # Every case must preserve the existing app catalog vector contract.
        if result["language"] == "und":
            self.assertEqual(result["languages"], ["und"])
        elif result["language"] == "mul":
            self.assertGreaterEqual(len(result["languages"]), 2)
            self.assertFalse({"und", "mul"}.intersection(result["languages"]))
        else:
            self.assertEqual(result["languages"], [result["language"]])
        return result

    def test_bare_verified_examples_follow_original_not_provider_category(self):
        for title, original in (("Dupahiya", "hi"), ("Stranger Things", "en"), ("Anupamaa", "hi")):
            with self.subTest(title=title):
                result = self.classify(title, original, [{"category_name": "ENGLISH | AMAZON PRIME", "language": "en"}])
                self.assertEqual(result, {"language": original, "languages": [original],
                    "isMultilingual": False, "languageListComplete": True, "languageSource": "verified_original"})

    def test_explicit_suffix_beats_original_and_generic_provider_labels(self):
        for title in ("Stranger Things (Hindi)", "Stranger Things [Hindi Dubbed]", "Stranger Things - Hindi"):
            with self.subTest(title=title):
                result = self.classify(title, "en", [{"language": "English", "category_name": "ENGLISH | NETFLIX"}])
                self.assertEqual(result["languages"], ["hi"])
                self.assertEqual(result["languageSource"], "title_suffix")
                self.assertTrue(result["languageListComplete"])

    def test_explicit_suffix_needs_no_verified_external_match(self):
        result = self.classify("Show (Tamil)", "en", verified=False)
        self.assertEqual(result["languages"], ["ta"])

    def test_two_named_languages_are_complete_multilingual(self):
        result = self.classify("Show (Hindi/Tamil)", "en")
        self.assertEqual(result, {"language": "mul", "languages": ["hi", "ta"],
            "isMultilingual": True, "languageListComplete": True, "languageSource": "title_suffix"})

    def test_multilingual_does_not_invent_additional_tracks(self):
        for marker in ("Multi", "Multi Audio", "Multi-language", "Multilingual", "Dual Audio", "Bilingual"):
            with self.subTest(marker=marker):
                result = self.classify(f"Dupahiya ({marker})")
                self.assertEqual(result["languages"], ["hi"])
                self.assertEqual(result["language"], "hi")
                self.assertTrue(result["isMultilingual"])
                self.assertFalse(result["languageListComplete"])
                self.assertEqual(result["languageSource"], "provider_multilingual")

    def test_multi_keeps_original_and_explicit_named_components(self):
        result = self.classify("Show (Multi Audio) (Hindi)", "en")
        self.assertEqual(result["languages"], ["en", "hi"])
        self.assertFalse(result["languageListComplete"])
        self.assertEqual(result["languageSource"], "provider_multilingual")

    def test_multi_without_verified_original_stays_unknown_unless_named(self):
        result = self.classify("Show (Multi Audio)", "hi", verified=False)
        self.assertEqual(result["language"], "und")
        self.assertTrue(result["isMultilingual"])
        self.assertFalse(result["languageListComplete"])
        result = self.classify("Show (Hindi Multi Audio)", None, verified=False)
        self.assertEqual(result["languages"], ["hi"])
        self.assertFalse(result["languageListComplete"])

    def test_bare_unverified_original_does_not_become_provider_audio(self):
        for original in ("hi", None, "xx", "und", "mul", "hxx", 123):
            with self.subTest(original=original):
                result = self.classify("Dupahiya", original, [{"original_language": "hi", "spoken_languages": ["hi"], "category_name": "English"}], verified=False)
                self.assertEqual(result["language"], "und")
                self.assertEqual(result["languageSource"], "unknown")

    def test_allowlisted_iso_and_regional_tags(self):
        for raw, expected in (("hin", "hi"), ("pan", "pa"), ("eng", "en"), ("en-US", "en"), ("pt_BR", "pt"), ("zh-Hant-TW", "zh")):
            with self.subTest(raw=raw):
                self.assertEqual(self.classify("Show", raw)["language"], expected)
        for raw in ("xx", "zzz", "en-not-a-language", "en1", "en/movie"):
            with self.subTest(raw=raw):
                self.assertEqual(self.classify("Show", raw)["language"], "und")

    def test_title_words_and_unrecognized_suffix_are_not_language_labels(self):
        for title in ("Hindi Medium", "English Teacher", "The Tamil Story", "The Hindi", "Show (English Teacher)"):
            with self.subTest(title=title):
                self.assertEqual(self.classify(title, "fr")["languageSource"], "verified_original")
                self.assertEqual(self.classify(title, "fr")["language"], "fr")

    def test_codec_year_and_quality_wrappers_do_not_hide_language(self):
        for title in ("Show (Hindi) (2025) [1080p] x265", "Show (Hindi) 2025 FHD HEVC", "Show (Hindi) [WEB-DL 1080p x265]", "Show (Tamil) (S01) [H.265]", "Show (Hindi) (Season 1)"):
            with self.subTest(title=title):
                result = self.classify(title, "en")
                self.assertEqual(result["language"], "ta" if "Tamil" in title else "hi")
                self.assertEqual(result["languageSource"], "title_suffix")

    def test_actual_audio_fields_beat_conflicting_suffix_and_keep_inputs(self):
        sources = [{"audio_languages": ["en-US", "fr"]}]
        before = copy.deepcopy(sources)
        result = self.classify("Show (Hindi)", "hi", sources)
        self.assertEqual(result["languages"], ["en", "fr"])
        self.assertEqual(result["languageSource"], "provider_audio")
        self.assertTrue(result["languageListComplete"])
        self.assertEqual(sources, before)

    def test_specific_provider_audio_source_wins_without_union(self):
        result = self.classify("Show (Hindi)", "hi", [{"audio_language": ["en"]}, {"audio_language": [{"iso_639_1": "ta"}]}])
        self.assertEqual(result["languages"], ["ta"])
        self.assertEqual(result["languageSource"], "provider_audio")

    def test_trusted_multilingual_audio_without_named_tracks_keeps_uncertainty(self):
        result = self.classify("Show (Tamil)", "hi", [{"audio_language": ["Multi Audio"]}])
        self.assertEqual(result["languages"], ["hi"])
        self.assertEqual(result["languageSource"], "provider_audio")
        self.assertTrue(result["isMultilingual"])
        self.assertFalse(result["languageListComplete"])
        unknown = self.classify("Show", "hi", [{"audio_language": ["Multi Audio"]}], verified=False)
        self.assertEqual(unknown["languages"], ["und"])
        self.assertTrue(unknown["isMultilingual"])

    def test_invalid_audio_does_not_claim_full_inventory_or_poison_suffix(self):
        partial = self.classify("Show", "hi", [{"audio_languages": ["en", "xx"]}])
        self.assertEqual(partial["languages"], ["en"])
        self.assertFalse(partial["languageListComplete"])
        fallback = self.classify("Show (Tamil)", "en", [{"audio_languages": ["xx"]}])
        self.assertEqual(fallback["languages"], ["ta"])
        self.assertEqual(fallback["languageSource"], "title_suffix")

    def test_invalid_inputs_and_truthy_verification_do_not_enable_inference(self):
        self.assertEqual(classify_series_language(None, "hi", metadata_verified="yes")["language"], "und")
        self.assertEqual(self.classify(None, "xx", sources="English")["language"], "und")


if __name__ == "__main__":
    unittest.main()
