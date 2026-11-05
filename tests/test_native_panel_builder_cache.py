from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_epg_streaming as builder
import private_panel_cache as cache
from test_build_epg_streaming import _mapping_bytes, _mapping_row, _xmltv_time

NOW = 1800000000
CREDS = ("https://private.example", "private-user", "secret-password")


def guide(records):
    channels = ''.join(f'<channel id="{key}"><display-name>Fixture</display-name></channel>' for key, _, _ in records)
    programmes = ''.join(
        f'<programme channel="{key}" start="{_xmltv_time(start)}" stop="{_xmltv_time(stop)}"><title>Real show {key}</title></programme>'
        for key, start, stop in records
    )
    return ('<tv>' + channels + programmes + '</tv>').encode()


class NativePanelBuilderCacheTests(unittest.TestCase):
    def build(self, root, *, cached_body, server="server_3", age=3600, credentials=CREDS):
        source = root / "saved.xml"
        source.write_bytes(cached_body)
        directory = root / "native-cache"
        self.assertTrue(cache.save_source(server, source, directory, credentials=credentials, captured_at=NOW-age))
        rows = [_mapping_row(server_id=server, stream_id=str(index), channel_name="Channel " + key,
                             category_name="General", epg_id=key, source="panel", epg_feed="panel")
                for index, key in enumerate(("Current", "Expired", "Missing", "Case"), 1)]
        mapping = root / "mapping.csv"
        mapping.write_bytes(_mapping_bytes(rows))
        combined = root / "combined.xml"
        combined.write_text('<tv/>', encoding='utf-8')
        public = root / "public"
        with mock.patch.dict(os.environ, {"SKYTV_PANEL_CACHE_DIR": str(directory)}), mock.patch.object(
            builder, "panel_credentials", return_value=CREDS
        ), mock.patch.object(builder, "download_panel_xmltv", side_effect=builder.PanelSourceUnavailable(server,"HTTP 502")):
            self.assertEqual(builder.main([
                "--mapping-file", str(mapping), "--all-source-file", str(combined),
                "--work-dir", str(root / "work"), "--public-dir", str(public),
                "--servers", server, "--now-epoch", str(NOW), "--minimum-coverage", "100",
            ]), 0)
        return public, directory, mapping

    def test_outage_reuses_only_exact_still_valid_channels_and_retains_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public, directory, mapping = self.build(root, cached_body=guide([
                ("Current", NOW-1800, NOW+1800), ("Expired", NOW-7200, NOW-3600),
                ("case", NOW-1800, NOW+1800),
            ]))
            with gzip.open(public / "EPG/server_3_metadata.json.gz", 'rt', encoding='utf-8') as handle:
                metadata = json.load(handle)["streamMetadata"]
            self.assertNotEqual(metadata["1"]["guideMode"], "synthetic")
            for key in ("2", "3", "4"):
                self.assertEqual(metadata[key]["guideFallbackReason"], "nativePanelUnavailable")
            manifest = json.loads((public / "reports/server_3/server_3_tivimate_manifest.json").read_text())
            self.assertEqual(manifest["nativePanelUnavailableSyntheticStreams"], 3)
            self.assertEqual(manifest["guideCoverage"]["counts"]["nativePanelReal"], 1)
            provenance = manifest["sourceProvenance"]["panel:server_3"]
            self.assertEqual(provenance["status"], "cache_reused")
            self.assertEqual(provenance["cacheCapturedAt"], NOW-3600)
            self.assertEqual(json.loads((directory / "server_3.json").read_text())["capturedAt"], NOW-3600)
            self.assertEqual(json.loads((public / "health.json").read_text())["unavailableNativePanelServers"], ["server_3"])
            original_rows = builder.parse_mapping_csv(mapping.read_bytes(), {"server_3"})
            self.assertEqual(manifest["mappingSha256"], builder.server_mapping_sha256(original_rows))
            raw = b''.join(path.read_bytes() for path in public.rglob('*') if path.is_file())
            self.assertNotIn(CREDS[0].encode(), raw)
            self.assertNotIn(CREDS[2].encode(), raw)

    def test_expired_or_wrong_account_cache_never_restores_real_coverage(self):
        for kwargs in ({"age":36*3600+1}, {"credentials":(CREDS[0], "different-account", CREDS[2])}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                public, _, _ = self.build(Path(tmp), cached_body=guide([("Current", NOW-600, NOW+3600)]), **kwargs)
                manifest = json.loads((public / "reports/server_3/server_3_tivimate_manifest.json").read_text())
                self.assertEqual(manifest["nativePanelUnavailableSyntheticStreams"], 4)
                self.assertEqual(manifest["guideCoverage"]["counts"]["nativePanelReal"], 0)

    def test_cache_without_a_real_stop_or_only_distant_future_cannot_qualify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connection = builder.create_database(root / "db.sqlite")
            try:
                source = root / "source.xml"
                source.write_bytes(guide([("Current", NOW+7*3600, NOW+8*3600)]).replace(
                    b'</tv>', f'<programme channel="Current" start="{_xmltv_time(NOW-600)}"><title>Inferred</title></programme></tv>'.encode()))
                builder.ingest_xmltv_source(connection=connection, path=source, source_key="panel:server_3",
                                             wanted_ids={"Current"}, window_start=NOW-86400)
                self.assertEqual(builder.usable_cached_panel_ids(connection, "panel:server_3", now_epoch=NOW), set())
            finally:
                connection.close()

    def test_malformed_cached_document_leaves_no_partial_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            public, _, _ = self.build(Path(tmp), cached_body=guide([("Current", NOW-600, NOW+3600)])[:-3])
            manifest = json.loads((public / "reports/server_3/server_3_tivimate_manifest.json").read_text())
            self.assertTrue(manifest["sourceProvenance"]["panel:server_3"]["cacheRejected"])
            self.assertEqual(manifest["guideCoverage"]["counts"]["nativePanelReal"], 0)

    def test_fresh_download_saves_only_after_full_xml_and_timing_validation(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                source=root / 'fresh.xml'
                source.write_bytes(guide([('Current',NOW-600,NOW+3600)]))
                directory=root / 'native-cache'
                self.assertTrue(cache.save_source('server_3',source,directory,credentials=CREDS,captured_at=NOW-3600))
                if malformed:
                    source.write_bytes(source.read_bytes()[:-3])
                connection=builder.create_database(root / 'db.sqlite')
                try:
                    with mock.patch.object(builder,'panel_credentials',return_value=CREDS), mock.patch.object(
                        builder,'download_panel_xmltv',return_value=(source,{'sha256':builder.sha256_file(source),'bytes':source.stat().st_size})
                    ):
                        arguments=dict(connection=connection,rows=[],server_id='server_3',wanted_ids={'Current'},
                                       destination=root/'download.xml',window_start=NOW-86400,now_epoch=NOW,
                                       maximum_expanded_bytes=1024*1024,cache_dir=directory)
                        if malformed:
                            with self.assertRaises(builder.BuildError):
                                builder.ingest_native_panel_with_cache(**arguments)
                        else:
                            builder.ingest_native_panel_with_cache(**arguments)
                    stored=json.loads((directory/'server_3.json').read_text())
                    self.assertEqual(stored['capturedAt'],NOW-3600 if malformed else NOW)
                finally:
                    connection.close()


if __name__ == '__main__':
    unittest.main()
