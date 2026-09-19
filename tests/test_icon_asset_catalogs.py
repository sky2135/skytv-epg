from __future__ import annotations

import csv
import hashlib
import re
import struct
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOGO_ROOT = REPO_ROOT / "assets" / "logos"
CATALOG_PATHS = (
    LOGO_ROOT / "icon_catalog.csv",
    LOGO_ROOT / "named_person_fallback_catalog.csv",
)
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
CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def read_catalog(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    return list(reader.fieldnames or ()), rows


def png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG file")
    if payload[12:16] != b"IHDR":
        raise AssertionError("PNG does not begin with an IHDR chunk")
    return struct.unpack(">II", payload[16:24])


class PublicIconCatalogTests(unittest.TestCase):
    def test_catalogs_are_private_safe_and_assets_are_unique(self) -> None:
        asset_ids: list[str] = []
        local_files: list[str] = []

        for catalog_path in CATALOG_PATHS:
            with self.subTest(catalog=catalog_path.name):
                fields, rows = read_catalog(catalog_path)
                self.assertTrue(rows)
                self.assertTrue(
                    PRIVATE_IDENTITY_COLUMNS.isdisjoint(
                        field.casefold() for field in fields
                    )
                )
                asset_ids.extend(row["asset_id"] for row in rows)
                local_files.extend(row["local_file"] for row in rows)

        self.assertEqual(len(asset_ids), len(set(asset_ids)))
        self.assertEqual(len(local_files), len(set(local_files)))

    def test_every_catalog_asset_is_a_real_verified_512_png(self) -> None:
        logo_root = LOGO_ROOT.resolve()
        for catalog_path in CATALOG_PATHS:
            _, rows = read_catalog(catalog_path)
            for row in rows:
                with self.subTest(catalog=catalog_path.name, asset=row["asset_id"]):
                    relative = Path(row["local_file"])
                    self.assertFalse(relative.is_absolute())
                    path = (LOGO_ROOT / relative).resolve()
                    self.assertTrue(path.is_relative_to(logo_root))
                    self.assertEqual(path.suffix.casefold(), ".png")
                    self.assertTrue(path.is_file(), path)
                    payload = path.read_bytes()
                    self.assertEqual(png_dimensions(payload), (512, 512))
                    expected_hash = row["output_sha256"].casefold()
                    self.assertIsNotNone(SHA256_PATTERN.fullmatch(expected_hash))
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_hash)

    def test_generated_art_is_cc0_and_has_a_license_notice(self) -> None:
        _, main_rows = read_catalog(LOGO_ROOT / "icon_catalog.csv")
        _, fallback_rows = read_catalog(
            LOGO_ROOT / "named_person_fallback_catalog.csv"
        )
        generated_rows = [
            row for row in main_rows if row["asset_kind"] == "generated_category"
        ] + fallback_rows

        self.assertEqual(len(generated_rows), 201)
        for row in generated_rows:
            with self.subTest(asset=row["asset_id"]):
                self.assertEqual(row["license_id"], "CC0-1.0")
                self.assertEqual(row["license_url"], CC0_URL)
                self.assertEqual(row["creator"], "SKY TV")

        notice = (LOGO_ROOT / "GENERATED_ART_LICENSE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("CC0 1.0 Universal", notice)
        self.assertIn(CC0_URL, notice)
        self.assertIn("generated/category-*.png", notice)
        self.assertIn("generated/people/person-fallback-*.png", notice)
        self.assertIn("does not cover files under `people/`", notice)

    def test_third_party_portraits_have_source_and_license_records(self) -> None:
        _, rows = read_catalog(LOGO_ROOT / "icon_catalog.csv")
        portraits = [row for row in rows if row["asset_kind"] == "person_photo"]
        self.assertTrue(portraits)

        for row in portraits:
            with self.subTest(asset=row["asset_id"]):
                self.assertTrue(
                    row["source_page_url"].startswith(
                        "https://commons.wikimedia.org/wiki/File:"
                    )
                )
                self.assertTrue(row["creator"].strip())
                self.assertTrue(row["license_id"].startswith("CC-"))
                self.assertTrue(
                    row["license_url"].startswith(
                        "https://creativecommons.org/licenses/"
                    )
                )
                self.assertTrue(row["attribution_text"].strip())
                self.assertTrue(row["modifications"].strip())
                self.assertIsNotNone(
                    SHA1_PATTERN.fullmatch(row["source_sha1"].casefold())
                )
                self.assertTrue(row["retrieved_utc"].strip())
                self.assertEqual(row["review_status"], "approved")
                self.assertTrue(row["rights_notes"].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
