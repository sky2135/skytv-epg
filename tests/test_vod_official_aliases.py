from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_official_aliases as aliases


def details(names, *, kind="movie", identifier=123):
    field = "title" if kind == "movie" else "name"
    return {"id": identifier, "translations": {"id": identifier,
            "translations": [{"iso_639_1": "fr", "iso_3166_1": "FR", "data": {field: name}}
                             for name in names]}}


class OfficialAliasTests(unittest.TestCase):
    def test_movie_and_series_use_their_own_official_name_fields(self):
        movie = aliases.translation_aliases(details(["Nouveau Monde"]), "movie", "Nouveau Monde")
        self.assertTrue(movie.complete)
        self.assertTrue(movie.matched)
        self.assertEqual(movie.aliases, ("Nouveau Monde",))
        series = aliases.translation_aliases(details(["حريم السلطان"], kind="series"), "series", "حريم السلطان")
        self.assertTrue(series.matched)
        self.assertFalse(aliases.translation_aliases(details(["Nouveau Monde"]), "series", "Nouveau Monde").matched)

    def test_nfc_equivalence_case_spacing_and_punctuation_are_exact(self):
        value = aliases.translation_aliases(details(["Méfiez-vous du Boïuna"]), "movie", "  ME\u0301FIEZ—VOUS du Boi\u0308una  ")
        self.assertTrue(value.matched)
        self.assertEqual(value.aliases, ("Méfiez-vous du Boïuna",))
        self.assertFalse(aliases.translation_aliases(details(["Méfiez-vous"]), "movie", "Mefiez-vous").matched)

    def test_indic_vowel_signs_do_not_collapse_distinct_titles(self):
        for first, second in (("काला", "काली"), ("ਕਾਲਾ", "ਕਾਲੀ"), ("காலா", "காலி")):
            with self.subTest(first=first, second=second):
                self.assertNotEqual(aliases.exact_display_key(first), aliases.exact_display_key(second))
                self.assertFalse(aliases.translation_aliases(details([first]), "movie", second).matched)
                self.assertTrue(aliases.translation_aliases(details([first]), "movie", first).matched)

    def test_translation_does_not_remove_words_or_numbered_installments(self):
        for display in ("مسلسل حريم السلطان", "Nouveau Monde 2", "Part II Nouveau Monde", "Nouveau"):
            official = "حريم السلطان" if "مسلسل" in display else "Nouveau Monde"
            with self.subTest(display=display):
                self.assertFalse(aliases.translation_aliases(details([official]), "movie", display).matched)

    def test_missing_empty_or_nonmatching_translation_is_not_a_positive(self):
        missing = aliases.translation_aliases({"id": 123}, "movie", "Example")
        self.assertFalse(missing.complete)
        self.assertEqual(missing.reason, "translations_missing")
        empty = aliases.translation_aliases(details([]), "movie", "Example")
        self.assertTrue(empty.complete)
        self.assertFalse(empty.matched)
        self.assertFalse(aliases.translation_aliases(details(["Other"]), "movie", "Example").matched)

    def test_resource_must_belong_to_the_requested_detail_record(self):
        for kind in ("movie", "series"):
            for identifier in (124, None, True, False, 0, "", "0123", {}, "https://private.example"):
                value = details(["Example"], kind=kind)
                value["translations"]["id"] = identifier
                with self.subTest(kind=kind, identifier=identifier):
                    result = aliases.translation_aliases(value, kind, "Example")
                    self.assertFalse(result.complete)
                    self.assertFalse(result.matched)

    def test_appended_resource_can_inherit_valid_outer_identity(self):
        for kind, title in (("movie", "Nouveau Monde"), ("series", "حريم السلطان")):
            value = details([title], kind=kind)
            del value["translations"]["id"]
            with self.subTest(kind=kind):
                result = aliases.translation_aliases(value, kind, title)
                self.assertTrue(result.complete)
                self.assertTrue(result.matched)
                self.assertEqual(result.aliases, (title,))

    def test_absent_nested_id_cannot_bypass_outer_id_or_resource_validation(self):
        for kind in ("movie", "series"):
            for identifier in (None, True, False, 0, "", "0123", {}, "https://private.example"):
                value = details(["Example"], kind=kind)
                del value["translations"]["id"]
                value["id"] = identifier
                with self.subTest(kind=kind, identifier=identifier):
                    result = aliases.translation_aliases(value, kind, "Example")
                    self.assertFalse(result.complete)
                    self.assertFalse(result.matched)
            for rows in (None, {}, "invalid", [{"data": None}],
                         [{"data": {"title" if kind == "movie" else "name": 123}}],
                         [{"data": {}}] * (aliases.MAX_TRANSLATIONS + 1)):
                value = {"id": 123, "translations": {"translations": rows}}
                with self.subTest(kind=kind, rows_type=type(rows).__name__):
                    result = aliases.translation_aliases(value, kind, "Example")
                    self.assertFalse(result.complete)
                    self.assertFalse(result.matched)

    def test_malformed_or_oversized_tail_cannot_hide_competing_alias_evidence(self):
        for tail in (None, {}, {"data": None}, {"data": {"title": 123}},
                     {"data": {"title": "x" * (aliases.MAX_TITLE_CHARS + 1)}},
                     {"data": {"title": "https://private.example"}},
                     {"data": {"title": "Bad\x00title"}}):
            value = details(["Example"])
            value["translations"]["translations"].append(tail)
            with self.subTest(tail=tail):
                result = aliases.translation_aliases(value, "movie", "Example")
                self.assertFalse(result.complete)
                self.assertFalse(result.matched)
        result = aliases.translation_aliases(details(["Example"] * (aliases.MAX_TRANSLATIONS + 1)), "movie", "Example")
        self.assertFalse(result.complete)

    def test_selected_proof_names_are_bounded_after_complete_validation(self):
        names = ["Example" + ("!" * index) for index in range(20)]
        result = aliases.translation_aliases(details(names), "movie", "Example")
        self.assertTrue(result.complete)
        self.assertTrue(result.matched)
        self.assertEqual(len(result.aliases), aliases.MAX_SELECTED_ALIASES)
        self.assertEqual(result, aliases.translation_aliases(details(list(reversed(names))), "movie", "Example"))

    def test_absent_title_field_is_allowed_but_bad_field_type_is_not(self):
        value = details(["Example"])
        value["translations"]["translations"].append({"data": {"overview": "Overview only"}})
        self.assertTrue(aliases.translation_aliases(value, "movie", "Example").matched)

    def test_invalid_display_is_not_truncated_into_a_known_official_name(self):
        for display in (None, [], "", "!!!", "https://private.example", "x" * 301, "Exam\x00ple"):
            with self.subTest(display=display):
                self.assertFalse(aliases.translation_aliases(details(["Example"]), "movie", display).matched)

    def test_invalid_kind_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            aliases.translation_aliases(details(["Example"]), "episode", "Example")


if __name__ == "__main__":
    unittest.main()
