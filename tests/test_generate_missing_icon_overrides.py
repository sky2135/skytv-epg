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
        self.assertIn("--icon-config .build/channel-sync/channel_icons.csv", workflow)
        self.assertNotIn("--icon-config config/channel_icons.csv", workflow)

    def test_source_url_safety_matches_production(self) -> None:
        clean = "https://logos.test/channel.png"
        query_icon = "https://logos.test/channel.png?w=360&h=270"
        self.assertEqual(generator.safe_http_url(clean), clean)
        self.assertEqual(production.valid_http_url(clean), clean)
        self.assertEqual(generator.safe_http_url(query_icon), "")
        self.assertEqual(production.valid_http_url(query_icon), "")

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
            named_catalog = root / "named_catalog.csv"
            for relative in (
                "generated/category-general.png",
                "generated/category-music.png",
                "generated/category-radio.png",
                "generated/category-sports.png",
                "generated/people/person-fallback-arijit-singh.png",
                "generated/people/person-fallback-lata-mangeshkar.png",
                "people/lata.png",
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
                        "asset_id": "lata-mangeshkar",
                        "subject_type": "person",
                        "subject_name": "Lata Mangeshkar",
                        "local_file": "people/lata.png",
                        "asset_kind": "person_photo",
                        "license_id": "CC-BY-3.0",
                        "review_status": "approved",
                    }
                )
            with named_catalog.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "asset_id",
                        "subject_type",
                        "subject_name",
                        "person_role",
                        "local_file",
                        "asset_kind",
                        "license_id",
                        "review_status",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "asset_id": "person-fallback-arijit-singh",
                        "subject_type": "person",
                        "subject_name": "Arijit Singh",
                        "person_role": "singer",
                        "local_file": (
                            "generated/people/person-fallback-arijit-singh.png"
                        ),
                        "asset_kind": "original_named_fallback",
                        "license_id": "CC0-1.0",
                        "review_status": "fallback_ready",
                    }
                )
                writer.writerow(
                    {
                        "asset_id": "person-fallback-lata-mangeshkar",
                        "subject_type": "person",
                        "subject_name": "Lata Mangeshkar",
                        "person_role": "singer",
                        "local_file": (
                            "generated/people/person-fallback-lata-mangeshkar.png"
                        ),
                        "asset_kind": "original_named_fallback",
                        "license_id": "CC0-1.0",
                        "review_status": "fallback_ready",
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
                named_fallback_catalog=named_catalog,
            )
            first_output = output_config.read_bytes()
            repeated = generator.generate(
                mapping_csv=mapping,
                source_xmltv=source,
                base_config=base_config,
                output_config=output_config,
                logo_root=logos,
                asset_catalog=asset_catalog,
                named_fallback_catalog=named_catalog,
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
                    named_fallback_catalog=named_catalog,
                )

        self.assertEqual(summary, repeated)
        self.assertEqual(summary["enabled_mapping_rows"], 8)
        self.assertEqual(summary["generated_fallback_rows"], 4)
        self.assertEqual(
            summary["coverage"],
            {
                "generated_fallback": 4,
                "mapping_logo": 1,
                "named_fallback": 1,
                "named_portrait": 1,
                "source_xmltv": 1,
            },
        )
        self.assertEqual(len(configured), 6)
        by_stream = {row["stream_id"]: row for row in configured}
        self.assertEqual(by_stream["1"]["local_file"], "generated/category-music.png")
        self.assertEqual(by_stream["2"]["local_file"], "generated/category-radio.png")
        self.assertEqual(by_stream["4"]["local_file"], "generated/category-general.png")
        self.assertEqual(by_stream["6"]["local_file"], "generated/category-sports.png")
        self.assertEqual(by_stream["5"]["local_file"], "people/lata.png")
        self.assertEqual(
            by_stream["8"]["local_file"],
            "generated/people/person-fallback-arijit-singh.png",
        )
        self.assertEqual(by_stream["5"]["priority"], "400")
        self.assertEqual(by_stream["8"]["priority"], "300")
        self.assertNotIn("3", by_stream)
        self.assertNotIn("7", by_stream)


if __name__ == "__main__":
    unittest.main(verbosity=2)
