from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import named_person_subjects as subjects  # noqa: E402


class NamedPersonSubjectTests(unittest.TestCase):
    def test_reviewed_corrections_and_duplicate_keys(self) -> None:
        self.assertEqual(subjects.canonical_name("ARJIT SINGH"), "Arijit Singh")
        self.assertEqual(subjects.canonical_name("HRTHIK ROSHAN"), "Hrithik Roshan")
        self.assertEqual(
            subjects.normalized_key(subjects.canonical_name("RAHAT FATEHA ALI KHAN")),
            subjects.normalized_key(subjects.canonical_name("RAHAT FATEH ALI KHAN")),
        )
        self.assertEqual(subjects.canonical_name("FAKHIR MEHMOOD"), "Faakhir Mehmood")
        self.assertEqual(subjects.canonical_name("GOHAR MUMTAZ"), "Goher Mumtaz")
        self.assertEqual(subjects.canonical_name("HUMAIRA CHANNA"), "Humera Channa")
        self.assertEqual(subjects.canonical_name("RAJKUMAAR RAO"), "Rajkummar Rao")

    def test_exact_category_extraction(self) -> None:
        cases = (
            (
                "Bollywood Singers 24/7",
                "HINDI-MOHAMMAD AZIZ SNOGS HD",
                ("singer", "MOHAMMAD AZIZ"),
            ),
            (
                "PUNJABI SINGERS 24/7",
                "PUNJABI-SINGER H DHAMI HD",
                ("singer", "H DHAMI"),
            ),
            (
                "PAKISTANI SINGERS 24/7",
                "PAKISTANI SINGER | MUSTAFA ZAHID HD",
                ("singer", "MUSTAFA ZAHID"),
            ),
            (
                "Bollywood Movies/Actors 24/7",
                "HINDI-ACTOR HRTHIK ROSHAN HD",
                ("actor", "HRTHIK ROSHAN"),
            ),
            (
                "Bollywood Singers 24/7",
                "Bollywood song",
                ("singer", ""),
            ),
        )
        for category, channel, expected in cases:
            with self.subTest(channel=channel):
                self.assertEqual(
                    subjects.classify_person_subject(category, channel), expected
                )

    def test_reviewed_exact_channel_allowlist(self) -> None:
        for (category, channel), expected in (
            subjects.REVIEWED_EXACT_PERSON_CHANNELS.items()
        ):
            with self.subTest(category=category, channel=channel):
                actual = subjects.classify_person_subject(
                    category.upper(), f"  {channel.upper()}  "
                )
                self.assertEqual(actual, expected)
                self.assertEqual(subjects.canonical_name(actual[1]), expected[1])

    def test_broad_categories_never_guess_unreviewed_titles(self) -> None:
        cases = (
            ("US : 24X7", "24/7: Friends"),
            ("US : 24X7", "24/7: Totally Unknown Person"),
            ("|NA| 24/7 ENGLISH", "ENG - 24/7 RANDOM MOVIES"),
            ("KANNADA MOVIES 24/7", "KANNADA-GENERIC MOVIES HD"),
        )
        for category, channel in cases:
            with self.subTest(category=category, channel=channel):
                self.assertEqual(
                    subjects.classify_person_subject(category, channel), ("", "")
                )
if __name__ == "__main__":
    unittest.main(verbosity=2)
