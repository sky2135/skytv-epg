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

import generate_missing_icon_overrides as generator  # noqa: E402
import build_epg_streaming as production  # noqa: E402


MAPPING_COLUMNS = (
    "server_id",
    "stream_id",
    "enabled",
    "channel_name",
    "category_name",
    "genre",
    "channel_role",
    "action",
    "source",
    "epg_feed",
    "epg_id",
    "logo_url",
)


class MissingIconOverrideTests(unittest.TestCase):
    def test_checked_in_config_contains_no_private_channel_bindings(self) -> None:
        config = REPO_ROOT / "config" / "channel_icons.csv"
        with config.open("r", encoding="utf-8", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        workflow = (REPO_ROOT / ".github" / "workflows" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "--output-config .build/channel-sync/channel_icons.csv", workflow
        )
        self.assertIn("--brand-catalog config/brand_logos.csv", workflow)
        self.assertIn("--icon-config .build/channel-sync/channel_icons.csv", workflow)
        self.assertNotIn("--icon-config config/channel_icons.csv", workflow)

    def test_source_url_safety_matches_production(self) -> None:
        clean = "https://logos.test/channel.png"
        query_icon = "https://logos.test/channel.png?w=360&h=270"
        self.assertEqual(generator.safe_http_url(clean), clean)
        self.assertEqual(production.valid_http_url(clean), clean)
        self.assertEqual(generator.safe_http_url(query_icon), "")
        self.assertEqual(production.valid_http_url(query_icon), "")

    def test_exact_brand_catalog_precedes_generic_category_fallback(self) -> None:
        self.assertEqual(
            generator.exact_brand_id(
                {
                    "channel_name": "SP - TSN 1 HD",
                    "category_name": "|NA| USA SPORTS",
                }
            ),
            "tsn",
        )
        self.assertEqual(
            generator.exact_brand_id(
                {
                    "channel_name": "USA - CW 53 (WWHO) COLUMBUS",
                    "category_name": "|NA| USA CW & MY",
                }
            ),
            "cw",
        )
        self.assertEqual(
            generator.exact_brand_id(
                {
                    "channel_name": "(FLSP 001) | live: West Indies vs India",
                    "category_name": "|NA| USA FLO PPV",
                }
            ),
            "flosports",
        )
        self.assertEqual(
            generator.exact_brand_id(
                {
                    "channel_name": "##### USA CW #####",
                    "category_name": "|NA| USA CW & MY",
                }
            ),
            "",
        )
        self.assertEqual(
            generator.exact_brand_id(
                {
                    "channel_name": "UK - CBS JUSTICE",
                    "category_name": "|UK| ENTERTAINMENT",
                }
            ),
            "",
        )

        row = {
            "server_id": "server_3",
            "stream_id": "487807",
            "enabled": "TRUE",
            "channel_name": "SP - TSN 1 HD",
            "category_name": "|NA| USA SPORTS",
            "genre": "sports",
            "action": "KEEP_PANEL",
            "source": "panel",
            "epg_feed": "panel",
            "epg_id": "ca.TSN1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "mapping.csv"
            base_config = root / "base.csv"
            output_config = root / "private.csv"
            source = root / "source.xml"
            logos = root / "logos"
            catalog = root / "catalog.csv"
            logos.mkdir()
            with mapping.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            with base_config.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(
                    handle, fieldnames=generator.CONFIG_COLUMNS
                ).writeheader()
            with catalog.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(
                    handle,
                    fieldnames=(
                        "asset_id",
                        "subject_type",
                        "subject_name",
                        "local_file",
                        "asset_kind",
                        "license_id",
                        "review_status",
                    ),
                ).writeheader()
            source.write_text("<tv/>", encoding="utf-8")

            summary = generator.generate(
                mapping_csv=mapping,
                source_xmltv=source,
                base_config=base_config,
                output_config=output_config,
                logo_root=logos,
                asset_catalog=catalog,
                brand_catalog=REPO_ROOT / "config" / "brand_logos.csv",
            )
            with output_config.open("r", encoding="utf-8", newline="") as handle:
                configured = list(csv.DictReader(handle))

        self.assertEqual(summary["brand_logo_rows"], 1)
        self.assertEqual(summary["generated_fallback_rows"], 0)
        self.assertEqual(len(configured), 1)
        self.assertIn("TSN_Logo", configured[0]["icon_url"])
        self.assertTrue(configured[0]["notes"].startswith("BRAND_LOGO: tsn"))

    def test_production_coverage_and_repeat_run_are_exact(self) -> None:
        rows = [
            {
                "server_id": "server_1",
                "stream_id": "1",
                "enabled": "TRUE",
                "channel_name": "Synthetic Music",
                "genre": "music",
                "action": "AUTO_DUMMY",
                "source": "dummy",
                "epg_feed": "DUMMY_CHANNELS",
                "epg_id": "Music.Dummy.test",
            },
            {
                "server_id": "server_2",
                "stream_id": "2",
                "enabled": "TRUE",
                "channel_name": "Panel Radio",
                "genre": "music",
                "channel_role": "radio",
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_feed": "panel",
                "epg_id": "2",
            },
            {
                "server_id": "server_1",
                "stream_id": "3",
                "enabled": "TRUE",
                "channel_name": "Safe Source Icon",
                "genre": "news",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "TEST",
                "epg_id": "Safe.Icon.test",
            },
            {
                "server_id": "server_1",
                "stream_id": "4",
                "enabled": "TRUE",
                "channel_name": "Missing Source Icon",
                "genre": "unknown",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "TEST",
                "epg_id": "Missing.Icon.test",
            },
            {
                "server_id": "server_1",
                "stream_id": "5",
                "enabled": "TRUE",
                "channel_name": "HINDI-LATA MANGESHKAR SONGS HD",
                "category_name": "Bollywood Singers 24/7",
                "genre": "music",
                "action": "AUTO_DUMMY",
                "source": "dummy",
                "epg_feed": "DUMMY_CHANNELS",
                "epg_id": "Movie.Dummy.test",
            },
            {
                "server_id": "server_1",
                "stream_id": "6",
                "enabled": "TRUE",
                "channel_name": "Unsafe Source Icon",
                "genre": "sports",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "TEST",
                "epg_id": "Query.Icon.test",
            },
            {
                "server_id": "server_3",
                "stream_id": "7",
                "enabled": "TRUE",
                "channel_name": "Mapping Icon",
                "genre": "movies",
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_feed": "panel",
                "epg_id": "7",
                "logo_url": "https://logos.test/mapping.png",
            },
            {
                "server_id": "server_1",
                "stream_id": "8",
                "enabled": "TRUE",
                "channel_name": "HINDI-ARJIT SINGH SONGS HD",
                "category_name": "Bollywood Singers 24/7",
                "genre": "music",
                "action": "AUTO_DUMMY",
                "source": "dummy",
                "epg_feed": "DUMMY_CHANNELS",
                "epg_id": "Movie.Dummy.test",
            },
        ]
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Query.Icon.test"><icon src="https://logos.test/query.png?size=300"/></channel>
  <programme start="20260919000000 +0000" stop="20260919010000 +0000" channel="Safe.Icon.test"><title>Test</title></programme>
  <channel id="Safe.Icon.test"><icon src="https://logos.test/safe.png"/></channel>
</tv>
'''

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "mapping.csv"
            base_config = root / "base_icons.csv"
            output_config = root / "private" / "channel_icons.csv"
            source = root / "source.xml.gz"
            logos = root / "logos"
            asset_catalog = root / "icon_catalog.csv"
            for relative in (
                "generated/category-general-v3.png",
                "generated/category-radio-v3.png",
                "generated/category-sports-v3.png",
                "generated/category-music-notes-v3.png",
                "generated/category-music-microphone-v3.png",
                "generated/category-music-headphones-v3.png",
                "generated/category-music-guitar-v3.png",
                "generated/category-music-dhol-v3.png",
                "generated/category-music-sitar-v3.png",
                "people/lata-cutout-v2.png",
            ):
                target = logos / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"test image")

            with mapping.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            with base_config.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=generator.CONFIG_COLUMNS)
                writer.writeheader()
            with asset_catalog.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "asset_id",
                        "subject_type",
                        "subject_name",
                        "local_file",
                        "asset_kind",
                        "license_id",
                        "review_status",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "asset_id": "lata-mangeshkar-cutout-v2",
                        "subject_type": "person",
                        "subject_name": "Lata Mangeshkar",
                        "local_file": "people/lata-cutout-v2.png",
                        "asset_kind": "person_photo",
                        "license_id": "CC-BY-3.0",
                        "review_status": "approved",
                    }
                )
            with gzip.open(source, "wb") as handle:
                handle.write(xml.encode("utf-8"))

            summary = generator.generate(
                mapping_csv=mapping,
                source_xmltv=source,
                base_config=base_config,
                output_config=output_config,
                logo_root=logos,
                asset_catalog=asset_catalog,
            )
            first_output = output_config.read_bytes()
            repeated = generator.generate(
                mapping_csv=mapping,
                source_xmltv=source,
                base_config=base_config,
                output_config=output_config,
                logo_root=logos,
                asset_catalog=asset_catalog,
            )
            self.assertEqual(output_config.read_bytes(), first_output)
            self.assertEqual(
                base_config.read_text(encoding="utf-8"),
                ",".join(generator.CONFIG_COLUMNS) + "\n",
            )
            with output_config.open("r", encoding="utf-8", newline="") as handle:
                configured = list(csv.DictReader(handle))

            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                generator.generate(
                    mapping_csv=mapping,
                    source_xmltv=source,
                    base_config=base_config,
                    output_config=base_config,
                    logo_root=logos,
                    asset_catalog=asset_catalog,
                )

        self.assertEqual(summary, repeated)
        self.assertEqual(summary["enabled_mapping_rows"], 8)
        self.assertEqual(summary["generated_fallback_rows"], 1)
        self.assertEqual(
            summary["coverage"],
            {
                "generated_fallback": 1,
                "mapping_logo": 1,
                "named_portrait": 1,
                "named_symbol": 1,
                "real_channel_no_override": 3,
                "source_xmltv": 1,
            },
        )
        self.assertEqual(len(configured), 3)
        by_stream = {row["stream_id"]: row for row in configured}
        self.assertRegex(
            by_stream["1"]["local_file"],
            r"^generated/category-music-.+-v3\.png$",
        )
        self.assertEqual(
            by_stream["5"]["local_file"], "people/lata-cutout-v2.png"
        )
        self.assertEqual(
            by_stream["8"]["local_file"],
            "generated/category-music-microphone-v3.png",
        )
        self.assertEqual(by_stream["5"]["priority"], "400")
        self.assertEqual(by_stream["8"]["priority"], "200")
        self.assertEqual(summary["named_symbol_rows"], 1)
        self.assertEqual(summary["brand_logo_rows"], 0)
        self.assertEqual(summary["named_fallback_rows"], 0)
        self.assertNotIn("2", by_stream)
        self.assertNotIn("3", by_stream)
        self.assertNotIn("4", by_stream)
        self.assertNotIn("6", by_stream)
        self.assertNotIn("7", by_stream)

    def test_real_channels_use_only_unique_exact_catalog_logos(self) -> None:
        rows = [
            {
                "server_id": "server_3",
                "stream_id": "bbc",
                "enabled": "TRUE",
                "channel_name": "USA - BBC AMERICA HD",
                "category_name": "|NA| USA NEWS",
                "genre": "news",
                "channel_role": "linear",
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_feed": "panel",
                "epg_id": "panel.bbc",
            },
            {
                "server_id": "server_3",
                "stream_id": "ambiguous",
                "enabled": "TRUE",
                "channel_name": "USA - SHARED NEWS HD",
                "category_name": "|NA| USA NEWS",
                "genre": "news",
                "channel_role": "linear",
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_feed": "panel",
                "epg_id": "panel.shared",
            },
            {
                "server_id": "server_3",
                "stream_id": "missing",
                "enabled": "TRUE",
                "channel_name": "USA - REAL CHANNEL WITHOUT LOGO HD",
                "category_name": "|NA| USA GENERAL",
                "genre": "general",
                "channel_role": "linear",
                "action": "KEEP_PANEL",
                "source": "panel",
                "epg_feed": "panel",
                "epg_id": "panel.missing",
            },
        ]
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="BBC.America.test">
    <display-name>BBC America</display-name>
    <icon src="https://logos.test/bbc-america.png"/>
  </channel>
  <channel id="Shared.News.one">
    <display-name>Shared News</display-name>
    <icon src="https://logos.test/shared-one.png"/>
  </channel>
  <channel id="Shared.News.two">
    <display-name>Shared News HD</display-name>
    <icon src="https://logos.test/shared-two.png"/>
  </channel>
</tv>
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping = root / "mapping.csv"
            base_config = root / "base.csv"
            output_config = root / "private.csv"
            source = root / "source.xml"
            logos = root / "logos"
            catalog = root / "catalog.csv"
            logos.mkdir()
            with mapping.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=MAPPING_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            with base_config.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(
                    handle, fieldnames=generator.CONFIG_COLUMNS
                ).writeheader()
            with catalog.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(
                    handle,
                    fieldnames=(
                        "asset_id",
                        "subject_type",
                        "subject_name",
                        "local_file",
                        "asset_kind",
                        "license_id",
                        "review_status",
                    ),
                ).writeheader()
            source.write_text(xml, encoding="utf-8")

            summary = generator.generate(
                mapping_csv=mapping,
                source_xmltv=source,
                base_config=base_config,
                output_config=output_config,
                logo_root=logos,
                asset_catalog=catalog,
                brand_catalog=REPO_ROOT / "config" / "brand_logos.csv",
            )
            with output_config.open("r", encoding="utf-8", newline="") as handle:
                configured = list(csv.DictReader(handle))

        self.assertEqual(summary["generated_fallback_rows"], 0)
        self.assertEqual(summary["coverage"]["source_name_xmltv"], 1)
        self.assertEqual(summary["coverage"]["real_channel_no_override"], 2)
        self.assertEqual(len(configured), 1)
        self.assertEqual(configured[0]["stream_id"], "bbc")
        self.assertEqual(
            configured[0]["icon_url"], "https://logos.test/bbc-america.png"
        )
        self.assertTrue(
            configured[0]["notes"].startswith("SOURCE_NAME_LOGO:")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
