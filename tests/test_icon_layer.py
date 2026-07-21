from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skytv_epg_icons as icons  # noqa: E402


class IconLayerTests(unittest.TestCase):
    def test_exact_config_and_local_file_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "icons.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=icons.ICON_CONFIG_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    "enabled": "true",
                    "server_id": "*",
                    "epg_id": "PTC.PUNJABI.in",
                    "local_file": "india/ptc-punjabi-in.png",
                    "priority": "100",
                })
                writer.writerow({
                    "enabled": "true",
                    "server_id": "server_2",
                    "channel_name": "Custom Channel",
                    "icon_url": "https://example.test/custom.png",
                    "priority": "200",
                })
            loaded = icons.load_icon_overrides(
                path, public_base_url="https://owner.github.io/epg"
            )
        choice = loaded.lookup("server_1", "PTC.PUNJABI.in", "anything")
        self.assertIsNotNone(choice)
        self.assertEqual(
            choice.url,
            "https://owner.github.io/epg/logos/india/ptc-punjabi-in.png",
        )
        scoped = loaded.lookup("server_2", "", "Custom Channel")
        self.assertEqual(scoped.url, "https://example.test/custom.png")
        self.assertIsNone(loaded.lookup("server_1", "", "Custom Channel"))

    def test_source_icon_extraction_and_no_fuzzy_logo_matching(self) -> None:
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Fox.One.test"><display-name>Fox One</display-name><icon src="/logos/fox-one.png"/></channel>
  <channel id="Fox.Two.test"><display-name>Fox Two</display-name><icon src="https://logos.test/fox-two.png"/></channel>
</tv>
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.xml.gz"
            with gzip.open(path, "wb") as handle:
                handle.write(xml.encode("utf-8"))
            found = icons.extract_source_icons(
                path,
                {"Fox.One.test"},
                base_url="https://source.test/catalog.xml.gz",
            )
        self.assertEqual(
            found["fox.one.test"],
            "https://source.test/logos/fox-one.png",
        )
        self.assertNotIn("fox.two.test", found)
        self.assertNotIn("fox 1 test", found)

    def test_resolve_and_streaming_injection(self) -> None:
        prepared = pd.DataFrame([
            {
                "server_id": "server_1",
                "stream_id": "1",
                "channel_name": "Provider Fox One",
                "epg_id": "Fox.One.test",
                "feed_key": "TEST",
            },
            {
                "server_id": "server_1",
                "stream_id": "2",
                "channel_name": "No Logo Channel",
                "epg_id": "No.Logo.test",
                "feed_key": "TEST",
            },
        ])
        assignments, report = icons.resolve_icon_assignments(
            prepared,
            server_id="server_1",
            source_icons_by_feed={
                "TEST": {"fox.one.test": "https://logos.test/fox-one.png"}
            },
        )
        self.assertEqual(
            assignments["Provider Fox One"], "https://logos.test/fox-one.png"
        )
        self.assertEqual(
            assignments["Fox.One.test"], "https://logos.test/fox-one.png"
        )
        self.assertEqual(len(report), 2)

        generated = '''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Fox.One.test">
    <display-name>Fox One</display-name>
  </channel>
  <channel id="Provider Fox One">
    <display-name>Provider Fox One</display-name>
  </channel>
  <channel id="No Logo Channel">
    <display-name>No Logo Channel</display-name>
  </channel>
  <programme start="20260721000000 +0000" stop="20260721010000 +0000" channel="Fox.One.test"><title>Programme</title></programme>
</tv>
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "guide.xml.gz"
            with gzip.open(path, "wb") as handle:
                handle.write(generated.encode("utf-8"))
            stats = icons.inject_icons_into_xmltv(path, assignments)
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                output = handle.read()
        self.assertEqual(stats["xmltv_channels"], 3)
        self.assertEqual(stats["icons_inserted"], 2)
        self.assertEqual(stats["channels_without_icons"], 1)
        self.assertEqual(output.count("https://logos.test/fox-one.png"), 2)
        self.assertNotIn("No.Logo.test", assignments)

    def test_rejects_unsafe_urls_and_paths(self) -> None:
        self.assertEqual(icons.safe_icon_url("file:///tmp/logo.png"), "")
        self.assertEqual(icons.safe_icon_url("https://user:pass@example.test/x.png"), "")
        self.assertEqual(icons.local_logo_url("https://example.test", "../secret.png"), "")
        self.assertEqual(icons.local_logo_url("https://example.test", "logo.exe"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
