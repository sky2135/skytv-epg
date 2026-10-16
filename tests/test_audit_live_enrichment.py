from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_live_enrichment as audit
import build_epg_streaming as builder
if __package__:
    from .test_rich_programme_export import NOW, mapping, source_xml
else:
    from test_rich_programme_export import NOW, mapping, source_xml


def fixture(root: Path) -> Path:
    """Publish actual source-rich and synthetic guides through the builder."""
    source = root / "source.xml"
    source.write_text(source_xml(), encoding="utf-8")
    connection = builder.create_database(root / "guide.sqlite3")
    public = root / "public"
    (public / "EPG").mkdir(parents=True)
    (public / "epg").mkdir(exist_ok=True)
    try:
        rows = mapping()
        rows.append(replace(rows[0], stream_id="102", channel_name="Generated Channel",
                            canonical_name="Generated Channel", epg_id="Generated.Dummy",
                            requested_source="dummy", effective_source="dummy", epg_feed="DUMMY_CHANNELS"))
        builder.ingest_xmltv_source(connection=connection, path=source,
                                   source_key="epgshare01", wanted_ids={"Alpha.in"}, window_start=NOW)
        builder.insert_synthetic_guides(connection=connection, rows=rows,
                                        window_start=NOW, window_end=NOW + 7200, reference_epoch=NOW)
        _, app = builder.build_server(
            server_id="server_1", rows=rows, connection=connection,
            schedule_stats=builder.load_schedule_stats(connection, NOW), staging_public=public,
            generated_at=NOW, xml_window_start=NOW, app_window_start=NOW,
            app_window_end=NOW + 7200, icon_overrides=builder.empty_icon_overrides(),
            source_provenance={}, minimum_coverage=0,
        )
    finally:
        connection.close()
    build = {"serverId": "server_1", "dataFile": "EPG/server_1_epg.json.gz",
             "manifestFile": "EPG/server_1_epg_manifest.json", "richSchemaVersion": 2,
             "richDataFile": "EPG/server_1_epg_v2.json.gz",
             "richManifestFile": "EPG/server_1_epg_v2_manifest.json",
             "richDataSha256": app["richDataSha256"], "richCompressedBytes": app["richCompressedBytes"],
             "metadataFile": "EPG/server_1_metadata.json.gz", "mappedStreams": app["mappedStreams"],
             "programmeRows": app["programmeRows"]}
    (public / "EPG" / "index.json").write_text(json.dumps({"schemaVersion": 1, "generatedAt": NOW, "builds": [build]}), encoding="utf-8")
    return public


def read(path: Path):
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data):
    if path.name.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(data, handle)
    else:
        path.write_text(json.dumps(data), encoding="utf-8")


def repair_rich_checksums(public: Path):
    epg = public / "EPG"
    path = epg / "server_1_epg_v2.json.gz"
    digest, size = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size
    rich_manifest = read(epg / "server_1_epg_v2_manifest.json")
    rich_manifest.update(dataSha256=digest, compressedBytes=size)
    write(epg / "server_1_epg_v2_manifest.json", rich_manifest)
    legacy_manifest = read(epg / "server_1_epg_manifest.json")
    legacy_manifest.update(richDataSha256=digest, richCompressedBytes=size)
    write(epg / "server_1_epg_manifest.json", legacy_manifest)
    index = read(epg / "index.json")
    index["builds"][0].update(richDataSha256=digest, richCompressedBytes=size)
    write(epg / "index.json", index)


class AuditLiveEnrichmentTests(unittest.TestCase):
    def test_real_builder_artifacts_verify_and_synthetic_facts_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            output = public / "reports" / "live_enrichment.json"
            report = audit.audit_public(public, output)
            self.assertEqual(report["status"], "verified")
            self.assertEqual(report["totals"]["metadata"]["channelRows"], 2)
            self.assertEqual(report["totals"]["realProgrammeRows"], 1)
            self.assertGreater(report["totals"]["syntheticProgrammeRows"], 0)
            self.assertEqual(report["totals"]["realProgrammeFieldCounts"]["description"], 1)
            self.assertEqual(report["totals"]["realProgrammeFieldCounts"]["episode"], 1)
            self.assertEqual(report["totals"]["realProgrammeFieldCounts"]["ratings"], 1)
            self.assertGreater(report["totals"]["allProgrammeFieldCounts"]["categories"], report["totals"]["realProgrammeFieldCounts"]["categories"])
            rendered = output.read_text(encoding="utf-8")
            for private_detail in ("Alpha", "Generated Channel", "Source programme description", "Election coverage", "art.example", '"101"'):
                self.assertNotIn(private_detail, rendered)

    def test_data_checksum_and_manifest_size_tampering_are_rejected(self):
        for mode in ("checksum", "size"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                public = fixture(Path(temporary))
                path = public / "EPG" / "server_1_epg_manifest.json"
                manifest = read(path)
                manifest["dataSha256" if mode == "checksum" else "compressedBytes"] = "0" * 64 if mode == "checksum" else 1
                write(path, manifest)
                with self.assertRaises(audit.AuditError):
                    audit.audit_public(public)

    def test_v2_projection_mismatch_is_rejected_even_with_correct_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            path = public / "EPG" / "server_1_epg_v2.json.gz"
            rich = read(path)
            next(iter(rich["programmes"].values()))[0]["title"] = "Changed title"
            write(path, rich)
            repair_rich_checksums(public)
            with self.assertRaisesRegex(audit.AuditError, "projection"):
                audit.audit_public(public)

    def test_generation_parity_is_rejected_even_with_correct_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            path = public / "EPG" / "server_1_epg_v2.json.gz"
            rich = read(path)
            rich["generatedAt"] += 1
            write(path, rich)
            repair_rich_checksums(public)
            with self.assertRaisesRegex(audit.AuditError, "generation"):
                audit.audit_public(public)

    def test_manifest_detail_count_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            path = public / "EPG" / "server_1_epg_v2_manifest.json"
            manifest = read(path)
            manifest["programmeFieldCounts"]["description"] += 100
            write(path, manifest)
            with self.assertRaisesRegex(audit.AuditError, "detail counts"):
                audit.audit_public(public)

    def test_path_reference_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            path = public / "EPG" / "index.json"
            index = read(path)
            index["builds"][0]["richDataFile"] = "../private.json.gz"
            write(path, index)
            with self.assertRaisesRegex(audit.AuditError, "references"):
                audit.audit_public(public)

    def test_decompression_and_compressed_byte_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            public = fixture(Path(temporary))
            with mock.patch.object(audit, "MAX_EXPANDED_BYTES", 64):
                with self.assertRaisesRegex(audit.AuditError, "Expanded"):
                    audit.audit_public(public)
            with mock.patch.object(audit, "MAX_COMPRESSED_BYTES", 16):
                with self.assertRaises(audit.AuditError):
                    audit.audit_public(public)


if __name__ == "__main__":
    unittest.main()
