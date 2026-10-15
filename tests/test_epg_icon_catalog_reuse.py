from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import epg_catalog_stream as catalog
import generate_missing_icon_overrides as icons


class IconCatalogReuseTests(unittest.TestCase):
    def test_full_first_pass_matches_xml_reader_and_preserves_ambiguity(self):
        xml = b'''<tv>
          <channel id="Alpha.us"><display-name>Shared</display-name><display-name lang="fr">Secondary</display-name><display-name>Blank Ambiguous</display-name><icon src="alpha.png"/></channel>
          <channel id="alpha.US"><display-name>Other</display-name><icon src="https://logos.test/other.png"/></channel>
          <channel id="Unselected.us"><display-name>Shared</display-name><icon src="https://logos.test/beta.png"/></channel>
          <channel id="NoLogo.us"><display-name>Shared</display-name></channel>
          <channel id=""><display-name>Blank Ambiguous</display-name><icon src="https://logos.test/blank.png"/></channel>
          <programme channel="Alpha.us" start="20261015000000 +0000" stop="20261015010000 +0000"><title>News</title></programme>
        </tv>'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xml.gz"
            source.write_bytes(gzip.compress(xml))
            sidecar = root / "icons.json"
            base = "https://epgshare01.online/epgshare01/"
            catalog.stream_catalog_and_programmes_once(
                path=source, fixed_wanted_ids=["Alpha.us"], select_provisional_ids=lambda _: (),
                now_epoch=1_800_000_000, window_start=1_799_900_000,
                minimum_unique_channels=1, source_base_url=base, icon_catalog_path=sidecar,
            )
            expected = icons.extract_source_icon_catalog(source, ["Alpha.us"], ["Shared", "Secondary", "Blank Ambiguous"], base_url=base)
            with mock.patch.object(icons.etree, "iterparse", side_effect=AssertionError("second XML pass")):
                actual = icons.read_source_icon_catalog(sidecar, source, ["Alpha.us"], ["Shared", "Secondary", "Blank Ambiguous"], base_url=base)
            self.assertEqual(actual, expected)
            self.assertEqual(actual[1]["alpha.us"], {"Alpha.us", "alpha.US"})
            self.assertNotIn("shared", actual[2])
            self.assertNotIn("blank ambiguous", actual[2])
            self.assertEqual(actual[2]["secondary"], base + "alpha.png")
            source.write_bytes(source.read_bytes() + b"different input")
            with self.assertRaisesRegex(ValueError, "does not match"):
                icons.read_source_icon_catalog(sidecar, source, (), (), base_url=base)

    def test_provided_sidecar_tampering_and_invalid_schemas_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xml"
            source.write_text('<tv><channel id="Alpha.us"><display-name>Alpha</display-name><icon src="https://logos.test/alpha.png"/></channel></tv>')
            sidecar = root / "icons.json"
            catalog.stream_catalog_and_programmes_once(
                path=source, fixed_wanted_ids=(), select_provisional_ids=lambda _: (),
                now_epoch=1_800_000_000, window_start=1_799_900_000,
                minimum_unique_channels=1, icon_catalog_path=sidecar,
            )
            original = sidecar.read_text()
            for mutation in ("logical_hash", "source_bytes", "source_hash", "bool_version",
                             "bool_bytes", "bad_name", "too_many_names", "bad_record", "unsafe_icon"):
                with self.subTest(mutation=mutation):
                    data = json.loads(original)
                    if mutation == "logical_hash":
                        data["channel_records"][0]["display_names"] = ["Changed"]
                    elif mutation == "source_bytes":
                        data["source_bytes"] += 1
                    elif mutation == "source_hash":
                        data["source_sha256"] = "0" * 64
                    elif mutation == "bool_version":
                        data["schema_version"] = True
                    elif mutation == "bool_bytes":
                        data["source_bytes"] = True
                    elif mutation == "bad_name":
                        data["channel_records"][0]["display_names"] = [42]
                    elif mutation == "too_many_names":
                        data["channel_records"][0]["display_names"] = ["Alpha"] * (catalog.ICON_CATALOG_MAX_NAMES_PER_RECORD + 1)
                    elif mutation == "bad_record":
                        data["channel_records"][0]["unexpected"] = True
                    else:
                        data["channel_records"][0]["icon_url"] = "https://logos.test/icon.png?token=private"
                    if mutation != "logical_hash":
                        logical = {key: value for key, value in data.items() if key != "logical_sha256"}
                        data["logical_sha256"] = hashlib.sha256(json.dumps(
                            logical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()).hexdigest()
                    sidecar.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        icons.read_source_icon_catalog(sidecar, source, ["Alpha.us"], ["Alpha"])

    def test_catalog_writer_bounds_fail_without_dropping_name_evidence(self):
        for limit in ("ICON_CATALOG_MAX_NAMES_PER_RECORD", "ICON_CATALOG_MAX_TEXT_BYTES", "ICON_CATALOG_MAX_BYTES"):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.xml"
                source.write_text('<tv><channel id="Alpha.us"><display-name>Alpha</display-name><display-name>Other</display-name></channel></tv>')
                sidecar = root / "icons.json"
                with mock.patch.object(catalog, limit, 1), self.assertRaises(catalog.CatalogStreamError):
                    catalog.stream_catalog_and_programmes_once(
                        path=source, fixed_wanted_ids=(), select_provisional_ids=lambda _: (),
                        now_epoch=1_800_000_000, window_start=1_799_900_000,
                        minimum_unique_channels=1, icon_catalog_path=sidecar,
                    )
                self.assertFalse(sidecar.exists())

    def test_failed_parse_does_not_emit_reusable_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "bad.xml"
            source.write_text('<tv><channel id="Alpha.us"><display-name>Alpha</display-name></channel><programme')
            sidecar = root / "icons.json"
            with self.assertRaises(catalog.CatalogStreamError):
                catalog.stream_catalog_and_programmes_once(
                    path=source, fixed_wanted_ids=(), select_provisional_ids=lambda _: (),
                    now_epoch=1_800_000_000, window_start=1_799_900_000,
                    minimum_unique_channels=1, icon_catalog_path=sidecar,
                )
            self.assertFalse(sidecar.exists())


if __name__ == "__main__":
    unittest.main()
