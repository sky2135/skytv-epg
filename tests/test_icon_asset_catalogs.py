from __future__ import annotations

import csv
import hashlib
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
LOGO_ROOT = REPO_ROOT / "assets" / "logos"
CATALOG_PATH = LOGO_ROOT / "icon_catalog.csv"
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
SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def read_catalog(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    return list(reader.fieldnames or ()), rows


def assert_reviewed_portrait_license(
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


def png_chunks(payload: bytes) -> list[tuple[bytes, bytes]]:
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG file")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise AssertionError("truncated PNG chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(payload):
            raise AssertionError("truncated PNG payload")
        chunks.append((kind, payload[start:end]))
        offset = end + 4
        if kind == b"IEND":
            break
    return chunks


def png_alpha_extrema(payload: bytes) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``((width, height), (min_alpha, max_alpha))`` for 8-bit RGBA."""
    chunks = png_chunks(payload)
    ihdr = next(data for kind, data in chunks if kind == b"IHDR")
    width, height, depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if (depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise AssertionError("icon PNG must be non-interlaced 8-bit RGBA")
    raw = zlib.decompress(b"".join(data for kind, data in chunks if kind == b"IDAT"))
    stride = width * 4
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise AssertionError("unexpected PNG scanline size")

    previous = bytearray(stride)
    minimum = 255
    maximum = 0
    offset = 0
    for _row_number in range(height):
        filter_type = raw[offset]
        source = raw[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        current = bytearray(stride)
        for index, value in enumerate(source):
            left = current[index - 4] if index >= 4 else 0
            up = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - up),
                    abs(estimate - upper_left),
                )
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
            current[index] = (value + predictor) & 0xFF
        alpha = current[3::4]
        minimum = min(minimum, min(alpha))
        maximum = max(maximum, max(alpha))
        previous = current
    return (width, height), (minimum, maximum)


def assert_safe_background_free_svg(test: unittest.TestCase, path: Path) -> None:
    payload = path.read_text(encoding="utf-8")
    lowered = payload.casefold()
    test.assertNotIn("<!doctype", lowered)
    test.assertNotIn("<!entity", lowered)
    root = ET.fromstring(payload)
    test.assertEqual(root.tag.rsplit("}", 1)[-1], "svg")
    test.assertEqual(root.attrib.get("viewBox"), "0 0 512 512")
    forbidden_elements = {
        "script",
        "foreignobject",
        "image",
        "use",
        "style",
        "text",
        "lineargradient",
        "radialgradient",
        "filter",
        "pattern",
    }
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].casefold()
        test.assertNotIn(tag, forbidden_elements, path)
        attributes = {
            key.rsplit("}", 1)[-1].casefold(): value
            for key, value in element.attrib.items()
        }
        for key, value in attributes.items():
            test.assertFalse(key.startswith("on"), path)
            test.assertNotIn("url(", value.casefold(), path)
            test.assertNotIn("javascript:", value.casefold(), path)
            test.assertNotIn(key, {"href", "xlink:href"}, path)
        if tag == "rect":
            full_canvas = (
                attributes.get("x", "0") in {"0", "0.0"}
                and attributes.get("y", "0") in {"0", "0.0"}
                and attributes.get("width") in {"512", "512.0", "100%"}
                and attributes.get("height") in {"512", "512.0", "100%"}
            )
            test.assertFalse(full_canvas, f"opaque background rectangle in {path}")


class PublicIconCatalogTests(unittest.TestCase):
    def test_catalog_is_private_safe_and_assets_are_unique(self) -> None:
        fields, rows = read_catalog(CATALOG_PATH)
        self.assertGreaterEqual(len(rows), 31)
        self.assertTrue(
            PRIVATE_IDENTITY_COLUMNS.isdisjoint(field.casefold() for field in fields)
        )
        self.assertEqual(len({row["asset_id"] for row in rows}), len(rows))
        self.assertEqual(len({row["local_file"] for row in rows}), len(rows))

    def test_every_catalog_png_is_verified_and_background_free(self) -> None:
        logo_root = LOGO_ROOT.resolve()
        _, rows = read_catalog(CATALOG_PATH)
        for row in rows:
            with self.subTest(asset=row["asset_id"]):
                relative = Path(row["local_file"])
                self.assertFalse(relative.is_absolute())
                path = (LOGO_ROOT / relative).resolve()
                self.assertTrue(path.is_relative_to(logo_root))
                self.assertEqual(path.suffix.casefold(), ".png")
                self.assertTrue(path.is_file(), path)
                payload = path.read_bytes()
                dimensions, alpha = png_alpha_extrema(payload)
                self.assertEqual(dimensions, (512, 512))
                self.assertEqual(alpha, (0, 255))
                expected_hash = row["output_sha256"].casefold()
                self.assertIsNotNone(SHA256_PATTERN.fullmatch(expected_hash))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_hash)

    def test_every_generated_png_has_a_safe_transparent_svg_master(self) -> None:
        _, rows = read_catalog(CATALOG_PATH)
        generated = [row for row in rows if row["asset_kind"] == "generated_category"]
        self.assertEqual(len(generated), 24)
        for row in generated:
            with self.subTest(asset=row["asset_id"]):
                png = LOGO_ROOT / row["local_file"]
                svg = png.with_suffix(".svg")
                self.assertTrue(svg.is_file(), svg)
                assert_safe_background_free_svg(self, svg)

    def test_generated_icons_use_the_approved_filled_neutral_v3_style(self) -> None:
        _, rows = read_catalog(CATALOG_PATH)
        generated = [row for row in rows if row["asset_kind"] == "generated_category"]
        allowed_paints = {"none", "#f7f8fa", "#1b2230"}
        for row in generated:
            with self.subTest(asset=row["asset_id"]):
                self.assertTrue(row["asset_id"].endswith("-v3"))
                self.assertTrue(row["local_file"].endswith("-v3.png"))
                svg = (LOGO_ROOT / row["local_file"]).with_suffix(".svg")
                root = ET.fromstring(svg.read_text(encoding="utf-8"))
                groups = [
                    element
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1].casefold() == "g"
                ]
                self.assertEqual(len(groups), 1)
                group = groups[0]
                self.assertEqual(group.attrib.get("fill"), "#F7F8FA")
                self.assertEqual(group.attrib.get("stroke"), "#1B2230")
                self.assertEqual(group.attrib.get("stroke-linecap"), "round")
                self.assertEqual(group.attrib.get("stroke-linejoin"), "round")

                open_strokes: dict[str, list[str]] = {}
                for element in root.iter():
                    attributes = {
                        key.rsplit("}", 1)[-1].casefold(): value
                        for key, value in element.attrib.items()
                    }
                    for paint_name in ("fill", "stroke"):
                        paint = attributes.get(paint_name)
                        if paint is not None:
                            self.assertIn(paint.casefold(), allowed_paints)
                    if (
                        element.tag.rsplit("}", 1)[-1].casefold() == "path"
                        and attributes.get("fill") == "none"
                    ):
                        open_strokes.setdefault(attributes["d"], []).append(
                            attributes.get("stroke", "")
                        )
                for path_data, strokes in open_strokes.items():
                    self.assertEqual(
                        sorted(strokes),
                        ["#1B2230", "#F7F8FA"],
                        f"open stroke must have a filled inner line: {path_data}",
                    )

        self.assertFalse(any((LOGO_ROOT / "generated").glob("category-*-v2.*")))

    def test_generated_art_is_cc0_and_old_name_cards_are_retired(self) -> None:
        _, rows = read_catalog(CATALOG_PATH)
        generated = [row for row in rows if row["asset_kind"] == "generated_category"]
        for row in generated:
            with self.subTest(asset=row["asset_id"]):
                self.assertEqual(row["license_id"], "CC0-1.0")
                self.assertEqual(row["license_url"], CC0_URL)
                self.assertEqual(row["creator"], "SKY TV")

        self.assertFalse((LOGO_ROOT / "named_person_fallback_catalog.csv").exists())
        self.assertFalse(any((LOGO_ROOT / "generated" / "people").glob("*.png")))
        notice = (LOGO_ROOT / "GENERATED_ART_LICENSE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("CC0 1.0 Universal", notice)
        self.assertIn(CC0_URL, notice)
        self.assertIn("generated/category-*.png", notice)
        self.assertNotIn("person-fallback", notice)
        self.assertIn("does not cover files under `people/`", notice)

    def test_third_party_portraits_are_transparent_and_fully_attributed(self) -> None:
        _, rows = read_catalog(CATALOG_PATH)
        portraits = [row for row in rows if row["asset_kind"] == "person_photo"]
        self.assertGreaterEqual(len(portraits), 7)

        for row in portraits:
            with self.subTest(asset=row["asset_id"]):
                self.assertTrue(row["local_file"].endswith("-cutout-v2.png"))
                self.assertTrue(
                    row["source_page_url"].startswith(
                        "https://commons.wikimedia.org/wiki/File:"
                    )
                )
                self.assertTrue(row["creator"].strip())
                assert_reviewed_portrait_license(self, row)
                self.assertTrue(row["attribution_text"].strip())
                self.assertIn("background removed", row["modifications"])
                self.assertIsNotNone(
                    SHA1_PATTERN.fullmatch(row["source_sha1"].casefold())
                )
                self.assertTrue(row["retrieved_utc"].strip())
                self.assertEqual(row["review_status"], "approved")
                self.assertTrue(row["rights_notes"].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
