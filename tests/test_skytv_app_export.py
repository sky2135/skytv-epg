from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_app_export import (  # noqa: E402
    load_preserved_extensions_from_directory,
    prepare_app_export_mapping,
    write_app_epg_files,
)


def xmltv_stamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000"
    )


class SkyTvAppExportTests(unittest.TestCase):
    def test_final_mapping_build_and_extension_preservation(self) -> None:
        generated_at = 1_783_881_035
        mapping = pd.DataFrame(
            [
                {
                    "server_id": "server_1",
                    "stream_id": "101",
                    "channel_name": "Real Channel HD",
                    "action": "AUTO_EPGSHARE",
                    "source": "epgshare",
                    "epg_id": "REAL.CHANNEL.test",
                    "epg_feed": "TEST",
                    "feed_key": "TEST",
                    "effective_feed_key": "TEST",
                    "effective_epg_id": "REAL.CHANNEL.test",
                    "has_programmes": "True",
                },
                {
                    "server_id": "server_1",
                    "stream_id": "202",
                    "channel_name": "Channel Without Guide",
                    "action": "NO_EPG_DUMMY",
                    "source": "epgshare",
                    "epg_id": "dummy-id-is-not-used-in-xmltv",
                    "epg_feed": "DUMMY_CHANNELS",
                    "feed_key": "DUMMY_CHANNELS",
                    "effective_feed_key": "DUMMY_CHANNELS",
                    "effective_epg_id": "dummy-id-is-not-used-in-xmltv",
                    "has_programmes": "True",
                },
                {
                    "server_id": "server_1",
                    "stream_id": "303",
                    "channel_name": "Needs Human Review",
                    "action": "REVIEW",
                    "source": "",
                    "epg_id": "SHOULD.NOT.APPEAR",
                    "epg_feed": "TEST",
                    "feed_key": "TEST",
                    "effective_feed_key": "TEST",
                    "effective_epg_id": "SHOULD.NOT.APPEAR",
                },
            ]
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml_path = root / "server_1_tivimate.xml.gz"
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="REAL.CHANNEL.test"><display-name>Real Channel</display-name></channel>
  <channel id="Channel Without Guide"><display-name>Channel Without Guide</display-name></channel>
  <programme channel="REAL.CHANNEL.test" start="{xmltv_stamp(generated_at - 60)}" stop="{xmltv_stamp(generated_at + 1800)}"><title>Morning News</title></programme>
  <programme channel="REAL.CHANNEL.test" start="{xmltv_stamp(generated_at + 1800)}" stop="{xmltv_stamp(generated_at + 3600)}"><title lang="pa">ਪੰਜਾਬੀ ਖ਼ਬਰਾਂ</title></programme>
  <programme channel="Channel Without Guide" start="{xmltv_stamp(generated_at)}" stop="{xmltv_stamp(generated_at + 3600)}"><title>No programme information</title></programme>
</tv>
""".encode("utf-8")
            with gzip.open(xml_path, "wb") as handle:
                handle.write(xml)

            preserve_dir = root / "existing"
            preserve_dir.mkdir()
            old_payload = {
                "schemaVersion": 1,
                "serverId": "server_1",
                "generatedAt": generated_at - 86_400,
                "windowStart": 0,
                "windowEnd": 1,
                "streamToEpg": {"old": "OLD::OLD"},
                "programmes": {},
                "streamLogos": {"101": "https://example.test/logo.png"},
                "logoMetadata": {"version": "8.5"},
            }
            with gzip.open(preserve_dir / "server_1_epg.json.gz", "wt", encoding="utf-8") as handle:
                json.dump(old_payload, handle, ensure_ascii=False)
            old_manifest = {
                "schemaVersion": 1,
                "serverId": "server_1",
                "generatedAt": generated_at - 86_400,
                "dataFile": "server_1_epg.json.gz",
                "logoStreams": 1,
                "logoLayerVersion": "8.5",
            }
            (preserve_dir / "server_1_epg_manifest.json").write_text(
                json.dumps(old_manifest), encoding="utf-8"
            )

            payload_extensions, manifest_extensions, details = (
                load_preserved_extensions_from_directory(preserve_dir, "server_1")
            )
            output = root / "output"
            result = write_app_epg_files(
                mapping=mapping,
                server_id="server_1",
                xmltv_path=xml_path,
                output_dir=output,
                source_manifest={
                    "generatedAt": generated_at,
                    "generatedAtIso": "2026-07-12T18:30:35Z",
                    "mappingSha256": "source-mapping-sha",
                },
                dummy_feeds={"DUMMY_CHANNELS"},
                preserved_payload_extensions=payload_extensions,
                preserved_manifest_extensions=manifest_extensions,
                preservation_details=details,
            )

            with gzip.open(result["dataPath"], "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            manifest = json.loads(result["manifestPath"].read_text(encoding="utf-8"))

            self.assertEqual(payload["generatedAt"], generated_at)
            self.assertEqual(
                payload["streamToEpg"],
                {
                    "101": "TEST::REAL.CHANNEL.test",
                    "202": "DUMMY_CHANNELS::dummy-id-is-not-used-in-xmltv",
                },
            )
            self.assertNotIn("303", payload["streamToEpg"])
            self.assertEqual(
                payload["streamLogos"]["101"], "https://example.test/logo.png"
            )
            self.assertEqual(payload["logoMetadata"]["version"], "8.5")
            titles = [
                row[2]
                for rows in payload["programmes"].values()
                for row in rows
            ]
            self.assertIn("Morning News", titles)
            self.assertIn("ਪੰਜਾਬੀ ਖ਼ਬਰਾਂ", titles)
            self.assertIn("No programme information", titles)
            self.assertEqual(manifest["logoStreams"], 1)
            self.assertEqual(manifest["logoLayerVersion"], "8.5")
            self.assertEqual(manifest["mappingSha256"], "source-mapping-sha")
            self.assertEqual(
                manifest["dataSha256"],
                hashlib.sha256(result["dataPath"].read_bytes()).hexdigest(),
            )
            self.assertEqual(manifest["mappedStreams"], 2)
            self.assertEqual(manifest["streamsWithProgrammes"], 2)
            self.assertEqual(manifest["coveragePercent"], 100.0)

    def test_alias_columns_are_accepted(self) -> None:
        mapping = pd.DataFrame(
            [
                {
                    "stream_id": "9",
                    "channel_name": "Alias Channel",
                    "action": "APPROVED",
                    "source": "panel",
                    "selected_epg_id": "alias.id",
                    "selected_feed": "server xmltv.php",
                }
            ]
        )
        prepared = prepare_app_export_mapping(mapping, "server_2")
        self.assertEqual(prepared.iloc[0]["effective_feed_key"], "PANEL")
        self.assertEqual(prepared.iloc[0]["effective_epg_id"], "alias.id")
        self.assertEqual(prepared.iloc[0]["app_schedule_key"], "PANEL::alias.id")


if __name__ == "__main__":
    unittest.main()
