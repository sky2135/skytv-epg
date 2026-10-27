from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as builder  # noqa: E402
from epg_catalog_stream import stream_catalog_and_programmes_once  # noqa: E402
from epg_selection_spool import (  # noqa: E402
    EpgSelectionSpoolWriter,
    SpoolError,
    copy_and_open_verified_spool,
)


NOW = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())


def stamp(offset: int) -> str:
    return datetime.fromtimestamp(NOW + offset, tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S +0000"
    )


def write_source(path: Path, source_ids: tuple[str, ...] = ("Alpha.in",)) -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Alpha.in">
    <display-name>Alpha</display-name>
    <icon src="https://logos.example/alpha.png"/>
  </channel>
  <programme channel="Alpha.in" start="{stamp(-1800)}" stop="{stamp(3600)}">
    <title>Morning News</title><category>News</category>
  </programme>
  <programme channel="Alpha.in" start="{stamp(3600)}" stop="{stamp(8 * 3600)}">
    <title>Afternoon News</title><category>News</category>
  </programme>
</tv>
"""
    if source_ids != ("Alpha.in",):
        channels = ''.join(f'<channel id="{epg_id}"><display-name>{epg_id}</display-name></channel>' for epg_id in source_ids)
        programmes = ''.join(
            f'<programme channel="{epg_id}" start="{stamp(start)}" stop="{stamp(stop)}"><title>{epg_id} Programme {start}</title></programme>'
            for epg_id in source_ids for start, stop in ((-1800,3600),(3600,8*3600))
        )
        xml = f'<tv>{channels}{programmes}</tv>'
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            compressed.write(xml.encode("utf-8"))


def create_spool(
    root: Path, fixed_ids: tuple[str, ...] = ("Alpha.in",),
    source_ids: tuple[str, ...] = ("Alpha.in",),
    alias_override: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    source = root / "all.xml.gz"
    spool_path = root / "selected.sqlite3"
    write_source(source, source_ids)
    with EpgSelectionSpoolWriter(spool_path) as spool:
        result = stream_catalog_and_programmes_once(
            path=source,
            fixed_wanted_ids=fixed_ids,
            select_provisional_ids=lambda _catalog: (),
            window_start=NOW - 86400,
            now_epoch=NOW,
            channel_sink=spool.add_channel,
            programme_sink=spool.add_programme,
            minimum_unique_channels=1,
        )
        spool.seal(
            source_sha256=result.source_sha256,
            source_bytes=source.stat().st_size,
            catalog_sha256=result.catalog.fingerprint_sha256,
            window_start=NOW - 86400,
            created_at_epoch=NOW,
            fixed_requested_ids=result.requested_fixed_ids,
            provisional_requested_ids=result.requested_provisional_ids,
            resolved_source_ids=result.resolved_source_ids,
            stats=result.stats,
            requested_id_aliases=result.requested_id_aliases if alias_override is None else alias_override,
        )
    return source, spool_path


def mapping_bytes(records: tuple[tuple[str,str,str], ...] = (("server_1","101","Alpha.in"),)) -> bytes:
    fields = (
        "server_id",
        "server_label",
        "stream_id",
        "enabled",
        "channel_name",
        "category_id",
        "category_name",
        "action",
        "source",
        "epg_feed",
        "epg_id",
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for server_id, stream_id, epg_id in records:
        writer.writerow(
        {
            "server_id": server_id,
            "server_label": server_id,
            "stream_id": stream_id,
            "enabled": "TRUE",
            "channel_name": "Alpha" if stream_id == "101" else "Alpha "+stream_id,
            "category_id": "news",
            "category_name": "News",
            "action": "APPROVED",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": epg_id,
        }
    )
    return output.getvalue().encode("utf-8")


class SelectionSpoolTests(unittest.TestCase):
    def open_spool(self, root, source, spool, required_ids):
        return copy_and_open_verified_spool(
            source=spool, destination=root / 'builder.sqlite3',
            expected_source_sha256=builder.sha256_file(source),
            expected_source_bytes=source.stat().st_size,
            required_ids=required_ids, required_window_start=NOW, consumer_epoch=NOW,
        )

    def test_unique_case_alias_history_binds_one_canonical_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root, ('alpha.IN',))
            imported = self.open_spool(root, source, spool, ('alpha.IN',))
            try:
                self.assertEqual(dict(imported.requested_id_aliases), {'alpha.IN':'Alpha.in'})
                self.assertEqual(imported.requested_ids, {'alpha.IN','Alpha.in'})
                self.assertEqual(imported.connection.execute(
                    'SELECT channel_key,source_channel_id FROM channels').fetchall(), [('Alpha.in','Alpha.in')])
                self.assertEqual(imported.connection.execute('SELECT COUNT(*) FROM programmes').fetchone()[0], 2)
                self.assertEqual(imported.stats['selected_programmes'], 2)
                with self.assertRaises(TypeError):
                    imported.requested_id_aliases['other'] = 'Alpha.in'
            finally:
                imported.connection.close()

    def test_runtime_aliases_preserve_mapping_hash_and_skip_other_policies(self) -> None:
        base = builder.parse_mapping_csv(mapping_bytes((('server_1','101','alpha.IN'),)), {'server_1'})[0]
        panel = replace(base, server_id='server_2', stream_id='201', requested_source='panel', effective_source='panel', action='KEEP_PANEL')
        synthetic = replace(base, stream_id='102', requested_source='dummy', effective_source='dummy', action='AUTO_DUMMY')
        disabled = replace(base, stream_id='103', enabled=False)
        rows = [base,panel,synthetic,disabled]
        original_hash = builder.canonical_mapping_sha256(rows)
        normalized = builder.apply_verified_epgshare_aliases(rows, {'alpha.IN':'Alpha.in'})
        self.assertEqual([row.runtime_epg_id for row in normalized], ['Alpha.in','','',''])
        self.assertEqual([row.epg_id for row in normalized], ['alpha.IN']*4)
        self.assertEqual(builder.canonical_mapping_sha256(normalized), original_hash)
        self.assertEqual(normalized[0].schedule_key, 'ALL_SOURCES1::Alpha.in')
        with self.assertRaises(builder.BuildError):
            builder.apply_verified_epgshare_aliases(rows, {'alpha.IN':'Unrelated.in'})

    def test_multiple_provider_rows_keep_alias_outputs_and_share_canonical_programmes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = (('server_1','101','alpha.IN'),('server_1','102','Alpha.in'),('server_2','201','ALPHA.in'))
            source, spool = create_spool(root, tuple(row[2] for row in records))
            mapping = root/'mapping.csv'
            original = mapping_bytes(records)
            mapping.write_bytes(original)
            public = root/'public'
            self.assertEqual(builder.main([
                '--mapping-file',str(mapping),'--all-source-file',str(source),
                '--epgshare-spool-file',str(spool),'--public-dir',str(public),
                '--work-dir',str(root/'work'),'--icon-config',str(root/'none.csv'),
                '--servers','server_1','server_2','--past-days','1','--now-epoch',str(NOW),
            ]), 0)
            self.assertEqual(mapping.read_bytes(), original)
            original_rows = builder.parse_mapping_csv(original, {'server_1','server_2'})
            for server_id, stream_ids in (('server_1',{'101','102'}),('server_2',{'201'})):
                with gzip.open(public/'EPG'/f'{server_id}_epg.json.gz', 'rt', encoding='utf-8') as handle:
                    app = json.load(handle)
                self.assertEqual(set(app['streamToEpg']), stream_ids)
                self.assertEqual(set(app['streamToEpg'].values()), {'ALL_SOURCES1::Alpha.in'})
                self.assertEqual(set(app['programmes']), {'ALL_SOURCES1::Alpha.in'})
                self.assertEqual(len(app['programmes']['ALL_SOURCES1::Alpha.in']), 2)
                manifest = json.loads((public/'reports'/server_id/f'{server_id}_tivimate_manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['mappingSha256'],builder.canonical_mapping_sha256([r for r in original_rows if r.server_id==server_id]))
            with gzip.open(public/'epg/server_1_tivimate.xml.gz','rt',encoding='utf-8') as handle:
                xml = handle.read()
            self.assertIn('id="alpha.IN"',xml)
            self.assertIn('id="Alpha.in"',xml)
            db = sqlite3.connect(root/'work/spool/selected_epg.sqlite3')
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM programmes').fetchone()[0], 2)
            finally:
                db.close()
            # A direct parse with already-canonical mappings yields identical
            # app stream/schedule content; alias handling does not invent it.
            canonical_map = root/'canonical.csv'
            canonical_map.write_bytes(mapping_bytes(tuple((s,i,'Alpha.in') for s,i,_ in records)))
            direct = root/'direct'
            self.assertEqual(builder.main([
                '--mapping-file',str(canonical_map),'--all-source-file',str(source),
                '--public-dir',str(direct),'--work-dir',str(root/'direct-work'),
                '--icon-config',str(root/'none.csv'),'--servers','server_1','server_2',
                '--past-days','1','--now-epoch',str(NOW),
            ]),0)
            for server in ('server_1','server_2'):
                with gzip.open(public/'EPG'/f'{server}_epg.json.gz','rt',encoding='utf-8') as handle:
                    via_spool = json.load(handle)
                with gzip.open(direct/'EPG'/f'{server}_epg.json.gz','rt',encoding='utf-8') as handle:
                    direct_app = json.load(handle)
                self.assertEqual(via_spool['streamToEpg'],direct_app['streamToEpg'])
                self.assertEqual(via_spool['programmes'],direct_app['programmes'])

    def test_distinct_exact_source_case_forms_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root, ('Alpha.in','alpha.IN','ALPHA.in'), ('Alpha.in','alpha.IN'))
            imported = self.open_spool(root,source,spool,('Alpha.in','alpha.IN','ALPHA.in'))
            try:
                self.assertEqual(dict(imported.requested_id_aliases),{})
                self.assertEqual(imported.connection.execute('SELECT COUNT(*) FROM channels').fetchone()[0],2)
                self.assertEqual(imported.connection.execute("SELECT source_channel_id FROM epg_spool_requests WHERE channel_key='ALPHA.in'").fetchone(),('',))
                self.assertEqual(imported.connection.execute('SELECT DISTINCT channel_key FROM programmes ORDER BY channel_key').fetchall(),[('Alpha.in',),('alpha.IN',)])
            finally:
                imported.connection.close()
            mapping=root/'mapping.csv'
            mapping.write_bytes(mapping_bytes((('server_1','101','Alpha.in'),('server_1','102','alpha.IN'))))
            public=root/'public'
            self.assertEqual(builder.main([
                '--mapping-file',str(mapping),'--all-source-file',str(source),
                '--epgshare-spool-file',str(spool),'--public-dir',str(public),
                '--work-dir',str(root/'work'),'--icon-config',str(root/'none.csv'),
                '--servers','server_1','--past-days','1','--now-epoch',str(NOW),
            ]),0)
            with gzip.open(public/'EPG/server_1_epg.json.gz','rt',encoding='utf-8') as handle:
                app=json.load(handle)
            self.assertEqual(app['streamToEpg'],{'101':'ALL_SOURCES1::Alpha.in','102':'ALL_SOURCES1::alpha.IN'})
            self.assertEqual(set(app['programmes']),{'ALL_SOURCES1::Alpha.in','ALL_SOURCES1::alpha.IN'})
            self.assertNotEqual(app['programmes']['ALL_SOURCES1::Alpha.in'],app['programmes']['ALL_SOURCES1::alpha.IN'])

    def test_unrelated_alias_and_missing_canonical_binding_fail_closed(self) -> None:
        for sql in (
            "UPDATE epg_spool_requests SET source_channel_id='Unrelated.in' WHERE channel_key='alpha.IN'",
            "DELETE FROM epg_spool_requests WHERE channel_key='Alpha.in'",
        ):
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                source,spool=create_spool(root,('alpha.IN',))
                db=sqlite3.connect(spool)
                db.execute(sql)
                db.commit()
                db.close()
                with self.assertRaisesRegex(SpoolError,'invalid requested ID alias'):
                    self.open_spool(root,source,spool,('alpha.IN',))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SpoolError,'invalid requested ID alias'):
                create_spool(Path(temporary),('alpha.IN',),alias_override={'alpha.IN':'Unrelated.in'})

    def test_forged_alias_cannot_merge_two_selected_exact_case_forms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source,spool=create_spool(root,('Alpha.in','alpha.IN','ALPHA.in'),('Alpha.in','alpha.IN'))
            db=sqlite3.connect(spool)
            db.execute("UPDATE epg_spool_requests SET source_channel_id='Alpha.in' WHERE channel_key='ALPHA.in'")
            db.commit()
            db.close()
            with self.assertRaisesRegex(SpoolError,'invalid requested ID alias'):
                self.open_spool(root,source,spool,('ALPHA.in',))

    def test_one_pass_callbacks_produce_builder_compatible_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            imported = copy_and_open_verified_spool(
                source=spool,
                destination=root / "builder.sqlite3",
                expected_source_sha256=builder.sha256_file(source),
                expected_source_bytes=source.stat().st_size,
                required_ids=("Alpha.in",),
                required_window_start=NOW,
                consumer_epoch=NOW,
            )
            try:
                self.assertEqual(
                    imported.connection.execute(
                        "SELECT source_key, channel_key, source_channel_id FROM channels"
                    ).fetchall(),
                    [("epgshare01", "Alpha.in", "Alpha.in")],
                )
                self.assertEqual(
                    imported.connection.execute(
                        "SELECT COUNT(*) FROM programmes"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(imported.manifest["source_sha256"], builder.sha256_file(source))
                self.assertEqual(imported.stats["stored_programmes"], 2)
            finally:
                imported.connection.close()

    def test_source_hash_request_coverage_and_window_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            cases = (
                {
                    "expected_source_sha256": "0" * 64,
                    "required_ids": ("Alpha.in",),
                    "required_window_start": NOW,
                    "message": "does not match",
                },
                {
                    "expected_source_sha256": builder.sha256_file(source),
                    "required_ids": ("Alpha.in",),
                    "required_window_start": NOW - 2 * 86400,
                    "message": "history window",
                },
            )
            for index, case in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaisesRegex(SpoolError, case["message"]):
                        copy_and_open_verified_spool(
                            source=spool,
                            destination=root / f"failed-{index}.sqlite3",
                            expected_source_sha256=case["expected_source_sha256"],
                            expected_source_bytes=source.stat().st_size,
                            required_ids=case["required_ids"],
                            required_window_start=case["required_window_start"],
                            consumer_epoch=NOW,
                        )

    def test_active_id_missing_from_request_history_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            with self.assertRaisesRegex(SpoolError, "does not cover"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Missing.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_nonblank_resolution_without_channel_row_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            connection = sqlite3.connect(spool)
            connection.execute("DELETE FROM channels WHERE channel_key = 'Alpha.in'")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SpoolError, "inconsistent selected rows"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_modified_spool_fails_logical_content_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            connection = sqlite3.connect(spool)
            connection.execute("UPDATE programmes SET title = 'Tampered'")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SpoolError, "logical-content seal"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_type_confusion_is_rejected_before_python_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            connection = sqlite3.connect(spool)
            connection.execute("UPDATE channels SET display_name = zeroblob(16)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SpoolError, "cell types or sizes"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_schema_type_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            connection = sqlite3.connect(spool)
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql = replace(" 
                "sql, 'display_name TEXT', 'display_name BLOB') "
                "WHERE name = 'channels'"
            )
            connection.execute("PRAGMA schema_version=999")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SpoolError, "unsupported database schema"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_stale_and_wrong_writer_spools_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            with self.assertRaisesRegex(SpoolError, "build window"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "stale.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW + 7 * 3600,
                )

            connection = sqlite3.connect(spool)
            connection.execute(
                "UPDATE epg_spool_manifest SET value = 'different' "
                "WHERE key = 'writer_version'"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(SpoolError, "not a sealed"):
                copy_and_open_verified_spool(
                    source=spool,
                    destination=root / "wrong-writer.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_empty_selection_preserves_zero_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root, ())
            imported = copy_and_open_verified_spool(
                source=spool,
                destination=root / "builder.sqlite3",
                expected_source_sha256=builder.sha256_file(source),
                expected_source_bytes=source.stat().st_size,
                required_ids=(),
                required_window_start=NOW,
                consumer_epoch=NOW,
            )
            try:
                self.assertEqual(imported.manifest["requested_count"], "0")
                self.assertEqual(imported.stats["stored_programmes"], 0)
            finally:
                imported.connection.close()

    def test_missing_legacy_fixed_request_is_accepted_with_zero_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root, ("Alpha.in", "Missing.in"))
            imported = copy_and_open_verified_spool(
                source=spool,
                destination=root / "builder.sqlite3",
                expected_source_sha256=builder.sha256_file(source),
                expected_source_bytes=source.stat().st_size,
                required_ids=("Missing.in",),
                required_window_start=NOW,
                consumer_epoch=NOW,
            )
            try:
                self.assertEqual(
                    imported.connection.execute(
                        "SELECT source_channel_id FROM epg_spool_requests "
                        "WHERE channel_key = 'Missing.in'"
                    ).fetchone(),
                    ("",),
                )
                self.assertIsNone(
                    imported.connection.execute(
                        "SELECT 1 FROM channels WHERE channel_key = 'Missing.in'"
                    ).fetchone()
                )
            finally:
                imported.connection.close()

    def test_symlink_spool_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            link = root / "linked.sqlite3"
            link.symlink_to(spool)
            with self.assertRaisesRegex(SpoolError, "regular file"):
                copy_and_open_verified_spool(
                    source=link,
                    destination=root / "failed.sqlite3",
                    expected_source_sha256=builder.sha256_file(source),
                    expected_source_bytes=source.stat().st_size,
                    required_ids=("Alpha.in",),
                    required_window_start=NOW,
                    consumer_epoch=NOW,
                )

    def test_builder_reuses_spool_without_reparsing_all_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            mapping = root / "mapping.csv"
            mapping.write_bytes(mapping_bytes())
            public = root / "public"
            work = root / "work"
            with mock.patch.object(
                builder,
                "ingest_xmltv_source",
                side_effect=AssertionError("ALL_SOURCES1 was parsed twice"),
            ):
                result = builder.main(
                    [
                        "--mapping-file",
                        str(mapping),
                        "--all-source-file",
                        str(source),
                        "--epgshare-spool-file",
                        str(spool),
                        "--public-dir",
                        str(public),
                        "--work-dir",
                        str(work),
                        "--icon-config",
                        str(root / "missing-icons.csv"),
                        "--servers",
                        "server_1",
                        "--past-days",
                        "1",
                        "--now-epoch",
                        str(NOW),
                    ]
                )
            self.assertEqual(result, 0)
            manifest = __import__("json").loads(
                (public / "reports/server_1/server_1_tivimate_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            provenance = manifest["sourceProvenance"]["epgshare01"]
            self.assertTrue(provenance["selectionSpoolReused"])
            self.assertEqual(provenance["selectionSpoolSchemaVersion"], "3")
            self.assertEqual(provenance["url"], "local-fixture")
            self.assertEqual(provenance["inputMode"], "local-fixture")
            self.assertEqual(provenance["originEvidence"], "none")
            self.assertEqual(provenance["finalHost"], "")
            self.assertFalse(provenance["finalHostObservedByBuilder"])
            self.assertEqual(provenance["declaredOriginHost"], "")
            self.assertGreater(manifest["programmeRows"], 0)

    def test_builder_records_pre_downloaded_source_without_claiming_final_host(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, spool = create_spool(root)
            mapping = root / "mapping.csv"
            mapping.write_bytes(mapping_bytes())
            public = root / "public"
            origin_url = (
                "https://epgshare01.online/epgshare01/"
                "epg_ripper_ALL_SOURCES1.xml.gz"
            )
            result = builder.main(
                [
                    "--mapping-file",
                    str(mapping),
                    "--all-source-file",
                    str(source),
                    "--all-source-file-origin-url",
                    origin_url,
                    "--epgshare-spool-file",
                    str(spool),
                    "--public-dir",
                    str(public),
                    "--work-dir",
                    str(root / "work"),
                    "--icon-config",
                    str(root / "missing-icons.csv"),
                    "--servers",
                    "server_1",
                    "--past-days",
                    "1",
                    "--now-epoch",
                    str(NOW),
                ]
            )
            self.assertEqual(result, 0)
            manifest = __import__("json").loads(
                (public / "reports/server_1/server_1_tivimate_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            provenance = manifest["sourceProvenance"]["epgshare01"]
            self.assertTrue(provenance["selectionSpoolReused"])
            self.assertEqual(provenance["url"], origin_url)
            self.assertEqual(provenance["inputMode"], "pre-downloaded-file")
            self.assertEqual(provenance["originEvidence"], "caller-declared")
            self.assertEqual(provenance["declaredOriginHost"], "epgshare01.online")
            self.assertEqual(provenance["finalHost"], "")
            self.assertFalse(provenance["finalHostObservedByBuilder"])

    def test_file_origin_declaration_is_narrowly_validated(self) -> None:
        with self.assertRaisesRegex(
            builder.BuildError, "public EPGShare HTTPS URL"
        ):
            builder.declared_epgshare_file_origin(
                "https://epgshare01.online/source.xml.gz?secret=value"
            )
        with self.assertRaisesRegex(
            builder.BuildError, "public EPGShare HTTPS URL"
        ):
            builder.declared_epgshare_file_origin(
                "https://example.invalid/source.xml.gz"
            )

    def test_builder_requires_the_matching_source_file_with_a_spool(self) -> None:
        with self.assertRaisesRegex(builder.BuildError, "requires --all-source-file"):
            builder.main(["--epgshare-spool-file", "/tmp/example.sqlite3"])


if __name__ == "__main__":
    unittest.main()
