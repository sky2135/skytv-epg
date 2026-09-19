from __future__ import annotations

import csv
import hashlib
import struct
import sys
import unittest
from collections import Counter
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


class NamedPersonAssetTests(unittest.TestCase):
    def test_public_catalog_and_png_set_are_complete_and_private_safe(self) -> None:
        logo_root = REPO_ROOT / "assets" / "logos"
        catalog = logo_root / "named_person_fallback_catalog.csv"
        with catalog.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = set(reader.fieldnames or ())

        self.assertTrue(
            {"server_id", "stream_id", "channel_name", "epg_id"}.isdisjoint(fields)
        )
        self.assertEqual(len(rows), 186)
        self.assertEqual(len({row["asset_id"] for row in rows}), 186)
        self.assertEqual(len({row["subject_name"] for row in rows}), 186)
        self.assertEqual(
            Counter(row["person_role"] for row in rows),
            {"singer": 121, "actor": 65},
        )
        self.assertEqual({row["asset_kind"] for row in rows}, {"generated_named_fallback"})
        self.assertEqual({row["license_id"] for row in rows}, {"CC0-1.0"})
        self.assertEqual(
            {row["license_url"] for row in rows},
            {"https://creativecommons.org/publicdomain/zero/1.0/"},
        )

        names = {row["subject_name"] for row in rows}
        self.assertIn("Rahat Fateh Ali Khan", names)
        self.assertNotIn("Rahat Fateha Ali Khan", names)
        self.assertIn("Mustafa Zahid", names)
        self.assertNotIn("Lata Mangeshkar", names)

        catalog_paths: set[Path] = set()
        for row in rows:
            relative = Path(row["local_file"])
            self.assertEqual(relative.parts[:2], ("generated", "people"))
            path = logo_root / relative
            catalog_paths.add(path)
            payload = path.read_bytes()
            self.assertGreater(len(payload), 24, path)
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n", path)
            self.assertEqual(payload[12:16], b"IHDR", path)
            self.assertEqual(struct.unpack(">II", payload[16:24]), (512, 512), path)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["output_sha256"])

        disk_paths = set((logo_root / "generated" / "people").glob("*.png"))
        self.assertEqual(disk_paths, catalog_paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
