from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_icon_inventory as inventory  # noqa: E402


MAPPING_FIELDS = [
    "server_id",
    "stream_id",
    "enabled",
    "channel_name",
    "category_name",
    "genre",
    "channel_role",
    "action",
    "epg_id",
    "logo_url",
]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class BuildIconInventoryTests(unittest.TestCase):
    def test_exact_identity_source_fallback_and_person_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "mapping.csv"
            config = root / "icons.csv"
            catalog = root / "catalog.csv"
            fallback_catalog = root / "fallback-catalog.csv"
            fallback_map = root / "fallback-map.csv"
            logo_root = root / "logos"
            xmltv = root / "source.xml.gz"
            inventory_out = root / "inventory.csv"
            research_out = root / "research.csv"
            (logo_root / "people").mkdir(parents=True)
            (logo_root / "people" / "lata.png").write_bytes(b"test image")
            (logo_root / "generated" / "people").mkdir(parents=True)
            fallback_local = "generated/people/lata-fallback.png"
            (logo_root / fallback_local).write_bytes(b"test fallback")

            write_csv(
                mapping,
                MAPPING_FIELDS,
                [
                    {
                        "server_id": "server_1",
                        "stream_id": "5001",
                        "enabled": "TRUE",
                        "channel_name": "HINDI-LATA MANGESHKAR SONGS HD",
                        "category_name": "Bollywood Singers 24/7",
                        "genre": "music",
                        "channel_role": "virtual",
                        "action": "AUTO_DUMMY",
                        "epg_id": "Movie.Dummy.us",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_1",
                        "stream_id": "999",
                        "enabled": "true",
                        "channel_name": "HINDI-LATA MANGESHKAR SONGS HD",
                        "category_name": "Bollywood Singers 24/7",
                        "genre": "music",
                        "channel_role": "virtual",
                        "action": "AUTO_EPGSHARE",
                        "epg_id": "Lata.Source.test",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_1",
                        "stream_id": "1000",
                        "enabled": "yes",
                        "channel_name": "Generic music mix",
                        "category_name": "Bollywood Singers 24/7",
                        "genre": "music",
                        "channel_role": "virtual",
                        "action": "AUTO_DUMMY",
                        "epg_id": "Movie.Dummy.us",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_2",
                        "stream_id": "88",
                        "enabled": "1",
                        "channel_name": "Example News",
                        "category_name": "News",
                        "genre": "news",
                        "channel_role": "linear",
                        "action": "AUTO_EPGSHARE",
                        "epg_id": "Example.News.test",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_3",
                        "stream_id": "77",
                        "enabled": "on",
                        "channel_name": "Example Radio",
                        "category_name": "Radio",
                        "genre": "unknown",
                        "channel_role": "radio",
                        "action": "KEEP_PANEL",
                        "epg_id": "Panel.test",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_2",
                        "stream_id": "89",
                        "enabled": "true",
                        "channel_name": "Late Example News",
                        "category_name": "News",
                        "genre": "news",
                        "channel_role": "linear",
                        "action": "AUTO_EPGSHARE",
                        "epg_id": "Late.Source.test",
                        "logo_url": "",
                    },
                    {
                        "server_id": "server_3",
                        "stream_id": "disabled",
                        "enabled": "FALSE",
                        "channel_name": "Disabled",
                        "category_name": "News",
                        "genre": "news",
                        "channel_role": "linear",
                        "action": "AUTO_DUMMY",
                        "epg_id": "",
                        "logo_url": "",
                    },
                ],
            )
            write_csv(
                config,
                [
                    "enabled",
                    "server_id",
                    "stream_id",
                    "epg_id",
                    "channel_name",
                    "icon_url",
                    "local_file",
                    "priority",
                ],
                [
                    {
                        "enabled": "true",
                        "server_id": "server_1",
                        "stream_id": "5001",
                        "epg_id": "",
                        "channel_name": "HINDI-LATA MANGESHKAR SONGS HD",
                        "icon_url": "",
                        "local_file": "people/lata.png",
                        "priority": "500",
                    },
                    {
                        "enabled": "true",
                        "server_id": "*",
                        "stream_id": "",
                        "epg_id": "Movie.Dummy.us",
                        "channel_name": "",
                        "icon_url": "https://unsafe-for-shared-id.test/icon.png",
                        "local_file": "",
                        "priority": "999",
                    },
                ],
            )
            write_csv(
                catalog,
                [
                    "asset_id",
                    "local_file",
                    "subject_type",
                    "asset_kind",
                    "license_id",
                    "review_status",
                ],
                [
                    {
                        "asset_id": "lata",
                        "local_file": "people/lata.png",
                        "subject_type": "person",
                        "asset_kind": "person_photo",
                        "license_id": "CC-BY-3.0",
                        "review_status": "approved",
                    }
                ],
            )
            write_csv(
                fallback_catalog,
                [
                    "asset_id",
                    "local_file",
                    "subject_type",
                    "asset_kind",
                    "license_id",
                    "review_status",
                ],
                [
                    {
                        "asset_id": "lata-fallback",
                        "local_file": fallback_local,
                        "subject_type": "person",
                        "asset_kind": "generated_named_fallback",
                        "license_id": "ORIGINAL",
                        "review_status": "fallback_ready",
                    }
                ],
            )
            write_csv(
                fallback_map,
                [
                    "server_id",
                    "stream_id",
                    "channel_name",
                    "local_file",
                    "priority",
                ],
                [
                    {
                        "server_id": "server_1",
                        "stream_id": "999",
                        "channel_name": "HINDI-LATA MANGESHKAR SONGS HD",
                        "local_file": fallback_local,
                        "priority": "300",
                    }
                ],
            )
            xml = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Movie.Dummy.us"><icon src="https://logos.test/dummy.png"/></channel>
  <channel id="Lata.Source.test"><icon src="/lata-source.png"/></channel>
  <channel id="Example.News.test"><icon src="https://logos.test/news.png"/></channel>
  <channel id="Panel.test"><icon src="https://logos.test/panel.png"/></channel>
  <programme channel="Example.News.test" start="20260101000000 +0000"><title>X</title></programme>
  <channel id="Late.Source.test"><icon src="https://logos.test/late.png"/></channel>
</tv>
"""
            with gzip.open(xmltv, "wt", encoding="utf-8") as handle:
                handle.write(xml)

            summary = inventory.run(
                mapping_csv=mapping,
                xmltv_path=xmltv,
                config_csv=config,
                catalog_csvs=[catalog, fallback_catalog],
                logo_root=logo_root,
                named_fallback_map=fallback_map,
                inventory_out=inventory_out,
                research_out=research_out,
                xmltv_base_url="https://source.test/catalog.xml.gz",
            )
            with inventory_out.open(encoding="utf-8", newline="") as handle:
                by_key = {
                    (row["server_id"], row["stream_id"]): row
                    for row in csv.DictReader(handle)
                }
            with research_out.open(encoding="utf-8", newline="") as handle:
                research = list(csv.DictReader(handle))

        self.assertEqual(summary["enabled_channels"], 6)
        self.assertEqual(summary["current_exact_overrides"], 2)
        self.assertEqual(summary["current_xmltv_icons"], 2)
        self.assertEqual(summary["current_missing"], 2)
        self.assertEqual(summary["named_person_channels"], 2)
        self.assertEqual(summary["named_person_complete"], 1)
        self.assertEqual(summary["named_person_needing_license_research"], 1)
        self.assertEqual(summary["named_person_needing_subject_review"], 0)

        exact = by_key[("server_1", "5001")]
        self.assertEqual(exact["current_icon_origin"], "exact_override")
        self.assertEqual(exact["current_asset_id"], "lata")
        self.assertEqual(exact["current_icon_status"], "portrait_ready")
        self.assertEqual(exact["next_action"], "none")
        same_name = by_key[("server_1", "999")]
        self.assertEqual(same_name["current_icon_origin"], "exact_override")
        self.assertEqual(same_name["current_asset_id"], "lata-fallback")
        self.assertEqual(same_name["current_icon_status"], "fallback_ready")
        self.assertEqual(same_name["next_action"], "research_free_portrait")
        late = by_key[("server_2", "89")]
        self.assertEqual(late["current_icon_origin"], "xmltv_source")
        self.assertEqual(
            late["current_icon_reference"], "https://logos.test/late.png"
        )
        dummy = by_key[("server_1", "1000")]
        self.assertEqual(dummy["current_icon_origin"], "none")
        self.assertEqual(dummy["person_role"], "")
        self.assertEqual(dummy["next_action"], "apply_original_fallback")
        radio = by_key[("server_3", "77")]
        self.assertEqual(radio["current_icon_origin"], "none")
        self.assertEqual(radio["suggested_asset_id"], "category-radio-v3")
        self.assertEqual(
            radio["suggested_local_file"], "generated/category-radio-v3.png"
        )
        self.assertEqual(radio["next_action"], "apply_original_fallback")
        statuses = {row["research_status"] for row in research}
        self.assertEqual(statuses, {"complete", "needs_license_research"})
        fallback_row = next(
            row for row in research if row["stream_id"] == "999"
        )
        self.assertEqual(fallback_row["fallback_asset_id"], "lata-fallback")
        self.assertEqual(fallback_row["fallback_local_file"], fallback_local)

    def test_override_optional_fields_must_also_match(self) -> None:
        override = inventory.ExactOverride(
            server_id="server_1",
            stream_id="7",
            epg_id="Expected.test",
            channel_name="Expected Name",
            reference="people/example.png",
            local_file="people/example.png",
            priority=10,
        )
        self.assertTrue(
            override.matches(
                {
                    "server_id": "server_1",
                    "stream_id": "7",
                    "epg_id": "Expected.test",
                    "channel_name": "Expected Name",
                }
            )
        )
        self.assertFalse(
            override.matches(
                {
                    "server_id": "server_1",
                    "stream_id": "7",
                    "epg_id": "Shared.Dummy.test",
                    "channel_name": "Expected Name",
                }
            )
        )

    def test_duplicate_enabled_exact_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mapping.csv"
            row = {
                "server_id": "server_1",
                "stream_id": "1",
                "enabled": "TRUE",
                "channel_name": "One",
                "category_name": "News",
                "genre": "news",
                "channel_role": "linear",
                "action": "AUTO_DUMMY",
                "epg_id": "",
                "logo_url": "",
            }
            write_csv(path, MAPPING_FIELDS, [row, row])
            with self.assertRaisesRegex(
                inventory.IconInventoryError, "Duplicate enabled exact identity"
            ):
                inventory.load_enabled_mapping(path)

    def test_dtd_and_tokenized_icon_urls_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            xmltv = Path(temporary) / "source.xml"
            xmltv.write_text(
                '<!DOCTYPE tv><tv><channel id="One.test">'
                '<icon src="https://logos.test/one.png"/></channel></tv>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                inventory.IconInventoryError, "DTD declarations are not allowed"
            ):
                inventory.extract_source_icons(xmltv, {"One.test"})
        self.assertEqual(
            inventory.safe_web_url("https://logos.test/one.png?token=secret"), ""
        )

    def test_named_fallback_is_not_a_completed_real_portrait(self) -> None:
        fallback = inventory.CatalogAsset(
            asset_id="fallback-person",
            local_file="generated/people/fallback-person.png",
            subject_type="person",
            asset_kind="generated_named_fallback",
            license_id="ORIGINAL",
            review_status="approved",
        )
        self.assertFalse(inventory.approved_person_asset(fallback))

    def test_original_png_can_inherit_reviewed_svg_rights(self) -> None:
        vector = inventory.CatalogAsset(
            asset_id="category-news",
            local_file="generated/category-news-v3.svg",
            subject_type="category",
            asset_kind="original_vector",
            license_id="ORIGINAL",
            review_status="approved",
        )
        found = inventory.catalog_asset_for_local_file(
            {vector.local_file: vector}, "generated/category-news-v3.png"
        )
        self.assertEqual(found, vector)


if __name__ == "__main__":
    unittest.main(verbosity=2)
