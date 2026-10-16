from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_epg_streaming as builder
from epg_catalog_stream import stream_catalog_and_programmes_once
from epg_programme_details import MAX_DETAILS_BYTES, details_json, parse_details_json, xmltv_programme_details
from epg_selection_spool import EpgSelectionSpoolWriter, SpoolError, copy_and_open_verified_spool
from lxml import etree
if __package__:
    from .test_build_epg_streaming import _mapping_bytes, _mapping_row, _xmltv_time
    from . import test_epg_spool_reuse as spool_tests
else:
    from test_build_epg_streaming import _mapping_bytes, _mapping_row, _xmltv_time
    import test_epg_spool_reuse as spool_tests


NOW = 1_800_000_000


def source_xml(*, missing_stop: bool = False) -> str:
    stop = "" if missing_stop else f' stop="{_xmltv_time(NOW + 3600)}"'
    return f'''<tv><channel id="Alpha.in"><display-name>Alpha</display-name></channel>
      <programme channel="Alpha.in" start="{_xmltv_time(NOW)}"{stop}>
        <title>News Special</title><sub-title>Election coverage</sub-title>
        <desc>Source programme description.</desc><category>News</category>
        <episode-num system="xmltv_ns">1.2.</episode-num>
        <episode-num system="onscreen">S2 E3</episode-num>
        <icon src="https://art.example/news.jpg"/>
        <rating system="US"><value>TV-PG</value></rating>
        <star-rating system="IMDb"><value>8/10</value></star-rating>
        <credits><director>Jane Smith</director><actor role="Presenter">Alex Doe</actor></credits>
        <date>20261016</date><language>English</language><orig-language>French</orig-language>
        <country>Canada</country><live/><new/><premiere/>
        <previously-shown start="20251016000000 +0000" channel="Alpha.in"/>
        <video><quality>HDTV</quality><aspect>16:9</aspect></video>
        <audio><stereo>stereo</stereo></audio>
        <subtitles type="teletext"><language>English</language></subtitles>
        <length units="minutes">60</length>
      </programme></tv>'''


def mapping():
    return builder.parse_mapping_csv(_mapping_bytes([_mapping_row(
        server_id="server_1", stream_id="101", channel_name="Alpha",
        category_name="News", epg_id="Alpha.in",
    )]), {"server_1"})


def read_json(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class RichProgrammeExportTests(unittest.TestCase):
    def test_rich_source_survives_direct_and_sealed_one_pass_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xml"
            source.write_text(source_xml(), encoding="utf-8")
            direct = builder.create_database(root / "direct.sqlite3")
            builder.ingest_xmltv_source(connection=direct, path=source, source_key="epgshare01",
                                        wanted_ids={"Alpha.in"}, window_start=NOW)
            spool_path = root / "selected.sqlite3"
            with EpgSelectionSpoolWriter(spool_path) as spool:
                selected = stream_catalog_and_programmes_once(
                    path=source, fixed_wanted_ids=("Alpha.in",),
                    select_provisional_ids=lambda _catalog: (), window_start=NOW,
                    now_epoch=NOW, channel_sink=spool.add_channel,
                    programme_sink=spool.add_programme, minimum_unique_channels=1,
                )
                spool.seal(source_sha256=selected.source_sha256, source_bytes=source.stat().st_size,
                           catalog_sha256=selected.catalog.fingerprint_sha256, window_start=NOW,
                           created_at_epoch=NOW, fixed_requested_ids=selected.requested_fixed_ids,
                           provisional_requested_ids=selected.requested_provisional_ids,
                           resolved_source_ids=selected.resolved_source_ids, stats=selected.stats)
            imported = copy_and_open_verified_spool(
                source=spool_path, destination=root / "imported.sqlite3",
                expected_source_sha256=builder.sha256_file(source),
                expected_source_bytes=source.stat().st_size, required_ids=("Alpha.in",),
                required_window_start=NOW, consumer_epoch=NOW,
            )
            try:
                rows = mapping()
                data = []
                for label, connection in (("direct", direct), ("spool", imported.connection)):
                    path = root / f"{label}.json.gz"
                    report = builder.write_app_epg(
                        destination=path, rows=rows, connection=connection, generated_at=NOW,
                        window_start=NOW, window_end=NOW + 7200,
                        icon_overrides=builder.empty_icon_overrides(), schema_version=2,
                    )
                    data.append(read_json(path))
                    self.assertEqual(report["programmeFieldCounts"]["description"], 1)
                self.assertEqual(data[0], data[1])
                programme = next(iter(data[0]["programmes"].values()))[0]
                self.assertEqual(programme["description"], "Source programme description.")
                self.assertEqual(programme["subtitle"], "Election coverage")
                self.assertEqual(programme["categories"], ["News"])
                self.assertEqual(programme["episode"][0], {"system": "xmltv_ns", "value": "1.2."})
                self.assertEqual(programme["ratings"], [{"system": "US", "value": "TV-PG"}])
                self.assertEqual(programme["starRatings"][0]["value"], "8/10")
                self.assertEqual(programme["credits"][1]["character"], "Presenter")
                self.assertEqual(programme["icon"], "https://art.example/news.jpg")
                self.assertEqual(programme["broadcastType"], ["live", "new", "premiere", "previously-shown"])
                self.assertEqual(programme["video"]["aspect"], "16:9")
                self.assertEqual(programme["sourceType"], "epgshare")
                self.assertNotIn("stopInferred", programme)
                legacy = root / "legacy.json.gz"
                builder.write_app_epg(destination=legacy, rows=rows, connection=direct,
                                      generated_at=NOW, window_start=NOW, window_end=NOW + 7200,
                                      icon_overrides=builder.empty_icon_overrides())
                old = read_json(legacy)
                self.assertEqual(old["schemaVersion"], 1)
                self.assertEqual(next(iter(old["programmes"].values())),
                                 [[NOW, NOW + 3600, "News Special"]])
            finally:
                direct.close()
                imported.connection.close()

    def test_missing_facts_stay_absent_and_inferred_stop_is_marked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xml"
            source.write_text(f'<tv><channel id="Alpha.in"/><programme channel="Alpha.in" '
                              f'start="{_xmltv_time(NOW)}"><title>Just a title</title></programme></tv>',
                              encoding="utf-8")
            connection = builder.create_database(root / "spool.sqlite3")
            try:
                builder.ingest_xmltv_source(connection=connection, path=source,
                                            source_key="epgshare01", wanted_ids={"Alpha.in"}, window_start=NOW)
                item = next(builder.iter_schedule_rows(connection, "epgshare01", "Alpha.in",
                                                       window_start=NOW, window_end=None, include_details=True))
                programme = builder.rich_programme_payload(item, title=item[2], source_key="epgshare01")
                self.assertEqual(programme, {"start": NOW, "stop": NOW + 3600,
                                            "title": "Just a title", "categories": [],
                                            "sourceType": "epgshare", "stopInferred": True})
            finally:
                connection.close()

    def test_artwork_privacy_and_panel_icon_policy(self):
        for url in ("https://user:secret@example.test/a.png", "https://example.test/a.png?token=secret",
                    "https://example.test/a.png#secret", "file:///private/art.png"):
            with self.subTest(url=url):
                element = etree.fromstring(f'<programme><icon src="{url}"/></programme>')
                self.assertNotIn("icon", xmltv_programme_details(element))
        element = etree.fromstring('<programme><icon src="https://example.test/a.png"/></programme>')
        self.assertNotIn("icon", xmltv_programme_details(element, allow_icons=False))
        empty = etree.fromstring('<programme><icon/></programme>')
        self.assertNotIn("icon", xmltv_programme_details(empty, source_base_url="https://example.test/"))

    def test_duplicate_source_row_keeps_available_rich_facts(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.xml"
                document = etree.fromstring(source_xml().encode())
                rich = document.find("programme")
                sparse = etree.fromstring(etree.tostring(rich))
                for child in list(sparse):
                    if child.tag not in {"title", "sub-title", "desc", "category"}:
                        sparse.remove(child)
                if reverse:
                    document.insert(1, sparse)
                else:
                    document.append(sparse)
                source.write_bytes(etree.tostring(document))
                connection = builder.create_database(root / "direct.sqlite3")
                try:
                    builder.ingest_xmltv_source(connection=connection, path=source,
                                                source_key="epgshare01", wanted_ids={"Alpha.in"}, window_start=NOW)
                    retained = json.loads(connection.execute("SELECT details_json FROM programmes").fetchone()[0])
                    self.assertEqual(retained["ratings"][0]["value"], "TV-PG")
                finally:
                    connection.close()

    def test_optional_details_stay_bounded_with_multibyte_text(self):
        element = etree.Element("programme")
        for index in range(80):
            person = etree.SubElement(etree.SubElement(element, "credits"), "actor")
            person.text = f"{index}:" + "漢" * 400
            person.set("role", "字" * 400)
            person.set("guest", "字" * 400)
            episode = etree.SubElement(element, "episode-num", system="字" * 400)
            episode.text = f"{index}:" + "字" * 400
        for name in ("date", "language", "orig-language", "country", "length"):
            etree.SubElement(element, name).text = "字" * 400
        facts = xmltv_programme_details(element, stop_inferred=True)
        encoded = details_json(facts)
        self.assertLessEqual(len(encoded.encode("utf-8")), MAX_DETAILS_BYTES)
        self.assertTrue(facts["stopInferred"])
        self.assertLessEqual(len(facts.get("credits", [])), 32)
        self.assertEqual(parse_details_json(encoded), facts)

    def test_invalid_or_deep_spool_details_fail_with_controlled_validation(self):
        for value in ('{"ratings":[{"unexpected":"bad"}]}',
                      '{"credits":[{"name":"bad\\u0000text"}]}',
                      '{"stopInferred":false}', '[' * 2000 + '0' + ']' * 2000):
            with self.subTest(value=value[:60]), self.assertRaises(ValueError):
                parse_details_json(value)

    def test_tampered_rich_details_and_old_spool_version_are_rejected(self):
        for mode in ("details", "old-version"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, spool = spool_tests.create_spool(root)
                connection = sqlite3.connect(spool)
                if mode == "details":
                    connection.execute("UPDATE programmes SET details_json = ?",
                                       ('{"icon":"https://example.test/a.png?token=private"}',))
                else:
                    connection.execute("UPDATE epg_spool_manifest SET value = '1' WHERE key = 'schema_version'")
                connection.commit()
                connection.close()
                with self.assertRaises(SpoolError):
                    copy_and_open_verified_spool(source=spool, destination=root / "copy.sqlite3",
                                                expected_source_sha256=builder.sha256_file(source),
                                                expected_source_bytes=source.stat().st_size,
                                                required_ids=("Alpha.in",), required_window_start=spool_tests.NOW,
                                                consumer_epoch=spool_tests.NOW)


if __name__ == "__main__":
    unittest.main()
