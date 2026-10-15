from __future__ import annotations

import csv
import sys
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import named_person_subjects as subjects  # noqa: E402


AUDIT_PATH = REPO_ROOT / "assets/logos/named_person_portrait_source_audit.csv"
CATALOG_PATH = REPO_ROOT / "assets/logos/icon_catalog.csv"
PRIVATE_IDENTITY_COLUMNS = {
    "server_id",
    "stream_id",
    "channel_name",
    "category_name",
    "epg_id",
    "server_url",
    "username",
    "password",
}
EXPECTED_FIELDS = {
    "subject_name",
    "subject_key",
    "role",
    "research_status",
    "source_page_url",
    "original_media_url",
    "creator",
    "source_date",
    "width_px",
    "height_px",
    "license_id",
    "license_url",
    "identity_evidence",
    "crop_suitability",
    "review_notes",
    "fallback_asset_id",
    "reviewed_utc",
}
CREATIVE_COMMONS_LICENSE_PATHS = {
    "CC-BY-2.0": "/licenses/by/2.0",
    "CC-BY-2.5": "/licenses/by/2.5",
    "CC-BY-3.0": "/licenses/by/3.0",
    "CC-BY-4.0": "/licenses/by/4.0",
    "CC-BY-SA-2.0": "/licenses/by-sa/2.0",
    "CC-BY-SA-3.0": "/licenses/by-sa/3.0",
    "CC-BY-SA-4.0": "/licenses/by-sa/4.0",
    "CC0-1.0": "/publicdomain/zero/1.0",
}
REVIEWED_PUBLIC_DOMAIN_LICENSES = frozenset(
    {
        "PD",
        "PD-Bangladesh-PID",
        "PD-Pakistan-US-1996",
        "PD-Self",
        "PD-US",
        "PD-USGov",
    }
)
LEGACY_CC0_DEED_URL = "http://creativecommons.org/publicdomain/zero/1.0/deed.en"
PD_US_GOV_LICENSE_PATH = "/wiki/Template:PD-USGov-Military-Navy"


def assert_reviewed_license(
    test: unittest.TestCase, row: dict[str, str]
) -> None:
    license_id = row["license_id"].strip()
    license_url = row["license_url"].strip()
    allowed_ids = set(CREATIVE_COMMONS_LICENSE_PATHS) | set(
        REVIEWED_PUBLIC_DOMAIN_LICENSES
    )
    test.assertIn(license_id, allowed_ids)

    parsed = urlparse(license_url)
    test.assertIsNone(parsed.username)
    test.assertIsNone(parsed.password)
    test.assertFalse(parsed.query)

    if license_id in CREATIVE_COMMONS_LICENSE_PATHS:
        test.assertEqual(parsed.hostname, "creativecommons.org")
        test.assertFalse(parsed.fragment)
        expected_path = CREATIVE_COMMONS_LICENSE_PATHS[license_id]
        if license_url == LEGACY_CC0_DEED_URL:
            test.assertEqual(license_id, "CC0-1.0")
        else:
            test.assertEqual(parsed.scheme, "https")
            test.assertEqual(parsed.path.rstrip("/"), expected_path)
        return

    test.assertEqual(parsed.scheme, "https")
    test.assertEqual(parsed.hostname, "commons.wikimedia.org")
    if license_id == "PD-USGov":
        test.assertEqual(parsed.path, PD_US_GOV_LICENSE_PATH)
        test.assertFalse(parsed.fragment)
    else:
        test.assertTrue(parsed.path.startswith("/wiki/File:"))
        test.assertEqual(parsed.fragment, "Licensing")
        test.assertEqual(license_url.removesuffix("#Licensing"), row["source_page_url"])


class NamedPersonSourceAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        with AUDIT_PATH.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            self.fields = list(reader.fieldnames or ())
            self.rows = list(reader)

    def test_public_audit_is_complete_unique_and_private_safe(self) -> None:
        self.assertEqual(set(self.fields), EXPECTED_FIELDS)
        self.assertTrue(
            PRIVATE_IDENTITY_COLUMNS.isdisjoint(
                field.casefold() for field in self.fields
            )
        )
        self.assertEqual(len(self.rows), 221)
        self.assertEqual(
            len({row["subject_key"] for row in self.rows}), len(self.rows)
        )
        self.assertTrue(all(row["subject_name"].strip() for row in self.rows))
        self.assertTrue(all(row["subject_key"].strip() for row in self.rows))
        for row in self.rows:
            with self.subTest(subject=row["subject_name"]):
                self.assertEqual(
                    row["subject_key"], subjects.slugify(row["subject_name"])
                )
                self.assertIn(row["role"], {"actor", "singer"})
                for field in (
                    "source_page_url",
                    "original_media_url",
                    "license_url",
                ):
                    self.assertNotEqual(row[field].strip().casefold(), "n/a")
        self.assertEqual(
            Counter(row["research_status"] for row in self.rows),
            Counter(
                {
                    "approved": 168,
                    "conditional": 22,
                    "no_verified_free_portrait": 31,
                }
            ),
        )

    def test_every_approved_source_has_identity_and_rights_evidence(self) -> None:
        approved = [
            row for row in self.rows if row["research_status"] == "approved"
        ]
        self.assertEqual(len(approved), 168)
        for row in approved:
            with self.subTest(subject=row["subject_name"]):
                self.assertTrue(
                    row["source_page_url"].startswith(
                        "https://commons.wikimedia.org/wiki/File:"
                    )
                )
                self.assertTrue(row["original_media_url"].startswith("https://"))
                self.assertTrue(row["creator"].strip())
                assert_reviewed_license(self, row)
                for field in (
                    "source_page_url",
                    "original_media_url",
                    "license_url",
                ):
                    value = row[field]
                    parsed = urlparse(value)
                    self.assertFalse(any(character.isspace() for character in value))
                    self.assertEqual(value.count("http"), 1)
                    self.assertIn(parsed.scheme, {"http", "https"})
                    self.assertTrue(parsed.hostname)
                    self.assertIsNone(parsed.username)
                    self.assertIsNone(parsed.password)
                self.assertTrue(row["identity_evidence"].strip())
                self.assertTrue(row["crop_suitability"].strip())
                self.assertTrue(row["reviewed_utc"].strip())

    def test_nonapproved_people_keep_the_neutral_fallback(self) -> None:
        for row in self.rows:
            if row["research_status"] == "approved":
                continue
            with self.subTest(subject=row["subject_name"]):
                expected = (
                    "category-music-microphone-v3"
                    if row["role"] == "singer"
                    else "category-movie-clapperboard-v3"
                )
                self.assertEqual(row["fallback_asset_id"], expected)

    def test_production_portraits_have_approved_audit_sources(self) -> None:
        with CATALOG_PATH.open(encoding="utf-8-sig", newline="") as handle:
            catalog_rows = list(csv.DictReader(handle))

        audit_by_key = {row["subject_key"]: row for row in self.rows}
        portrait_rows = [
            row for row in catalog_rows if row["asset_kind"] == "person_photo"
        ]
        portrait_keys: set[str] = set()
        for row in portrait_rows:
            subject_key = subjects.slugify(row["subject_name"])
            portrait_keys.add(subject_key)
            with self.subTest(subject=row["subject_name"]):
                self.assertIn(subject_key, audit_by_key)
                self.assertEqual(
                    audit_by_key[subject_key]["research_status"], "approved"
                )

        nonapproved_keys = {
            row["subject_key"]
            for row in self.rows
            if row["research_status"] != "approved"
        }
        self.assertTrue(portrait_keys.isdisjoint(nonapproved_keys))

    def test_every_reviewed_exact_parser_subject_has_an_audit_record(self) -> None:
        by_name = {row["subject_name"]: row for row in self.rows}
        reviewed = set(subjects.REVIEWED_EXACT_PERSON_CHANNELS.values())
        self.assertEqual(len({subject for _role, subject in reviewed}), 33)
        for role, subject in reviewed:
            with self.subTest(subject=subject):
                self.assertIn(subject, by_name)
                self.assertEqual(by_name[subject]["role"], role)

    def test_synthetic_subject_examples_resolve_to_reviewed_audit_identities(self) -> None:
        # Invented labels exercise the supported syntax and exact corrections.
        # The public portrait audit must not require provider inventory exports.
        examples = (
            ("Bollywood Singers 24/7", "HINDI-SINGER ARJIT SINGH SONGS HD", "singer", "Arijit Singh"),
            ("PUNJABI SINGERS 24/7", "PUNJABI-SINGER GURUR RANDHAWA HD", "singer", "Guru Randhawa"),
            ("PAKISTANI SINGERS 24/7", "PAKISTANI SINGER | ALI FAZAR HD", "singer", "Ali Zafar"),
            ("Bollywood Movies/Actors 24/7", "HINDI-ACTOR HRTHIK ROSHAN MOVIES HD", "actor", "Hrithik Roshan"),
        )
        audit_by_key = {row["subject_key"]: row for row in self.rows}
        for category, channel, expected_role, expected_name in examples:
            with self.subTest(subject=expected_name):
                role, raw_subject = subjects.classify_person_subject(category, channel)
                self.assertEqual(role, expected_role)
                canonical = subjects.canonical_name(raw_subject)
                self.assertEqual(canonical, expected_name)
                subject_key = subjects.slugify(canonical)
                self.assertIn(subject_key, audit_by_key)
                self.assertEqual(audit_by_key[subject_key]["role"], role)

    def test_unreviewed_synthetic_person_has_no_approved_portrait_identity(self) -> None:
        role, raw_subject = subjects.classify_person_subject(
            "Bollywood Movies/Actors 24/7", "HINDI-ACTOR UNREVIEWED EXAMPLE PERSON MOVIES HD"
        )
        self.assertEqual(role, "actor")
        unknown_key = subjects.slugify(subjects.canonical_name(raw_subject))
        self.assertNotIn(unknown_key, {row["subject_key"] for row in self.rows})
        with CATALOG_PATH.open(encoding="utf-8-sig", newline="") as handle:
            portraits = [row for row in csv.DictReader(handle) if row["asset_kind"] == "person_photo"]
        self.assertNotIn(unknown_key, {subjects.slugify(row["subject_name"]) for row in portraits})


if __name__ == "__main__":
    unittest.main(verbosity=2)
