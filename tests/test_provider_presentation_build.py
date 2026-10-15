from __future__ import annotations

import csv
import gzip
import io
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from scripts import build_epg_streaming as runner
from scripts import provider_presentation as presentation
from tests.test_build_epg_streaming import _snapshot_mapping_bytes as _mapping_bytes, _mapping_row


NOW = int(datetime(2026, 10, 15, 15, tzinfo=timezone.utc).timestamp())


def inventory_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=(
        "server_id", "stream_id", "channel_name", "category_name",
    ), lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row[key] for key in writer.fieldnames} for row in rows)
    return output.getvalue().encode("utf-8")


def header_row(stream_id="header"):
    raw = _mapping_row(
        server_id="server_3", stream_id=stream_id,
        channel_name="##### USA CW #####", category_name="|NA| USA CW & MY",
        epg_id="Blank.Dummy.us", epg_feed="DUMMY_CHANNELS", source="dummy",
    )
    raw.update(enabled="FALSE", action="IGNORE",
               reason=presentation.HEADING_CLASSIFICATION_REASON,
               notes="auto-map-v1 method=heading_placeholder; verified fixture")
    return raw


class ProviderPresentationBuildTests(unittest.TestCase):
    def test_only_verified_headers_receive_welcome_and_do_not_inflate_coverage(self):
        raw_rows = [header_row(key) for key in (
            "header", "manual", "open", "moved", "ordinary", "missing",
        )]
        raw_rows[1].update(action="MANUAL", enabled="TRUE")
        raw_rows[2]["reason"] = runner.EFFECTIVE_QUARANTINE_REASON
        raw_rows[4]["channel_name"] = "USA - CW 53 (WWHO) COLUMBUS"
        parsed = runner.parse_mapping_csv(_mapping_bytes(raw_rows), {"server_3"})
        original = list(parsed)
        current = [dict(row) for row in raw_rows[:-1]]
        current[0]["channel_name"] = "══ 𝐔𝐒𝐀 𝐂𝐖 ══"
        current[3]["category_name"] = "|NA| USA NBC"
        projected, stats = runner.apply_provider_event_title_overrides(
            parsed, inventory_bytes(current), {"server_3"}
        )
        self.assertEqual(parsed, original)
        self.assertEqual(stats["groupHeaderRows"], 1)
        branded = projected[0]
        self.assertEqual(branded.runtime_synthetic_reason, runner.GROUP_HEADER_RUNTIME_REASON)
        self.assertTrue(branded.runtime_eligible)
        self.assertTrue(branded.uses_local_synthetic)
        self.assertEqual(runner.synthetic_programme_title(branded), presentation.BRAND_TITLE)
        self.assertEqual(projected[1:], original[1:])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = runner.create_database(root / "guide.sqlite3")
            try:
                runner.insert_synthetic_guides(
                    connection=connection, rows=[branded],
                    window_start=NOW, window_end=NOW + 86400,
                    reference_epoch=NOW,
                )
                schedules = runner.load_schedule_stats(connection, NOW)
                entries, streams, _ = runner.build_xml_entries(
                    rows=[branded], connection=connection, schedule_stats=schedules,
                    icon_overrides=runner.empty_icon_overrides(),
                )
                self.assertIn(raw_rows[0]["channel_name"], entries)
                self.assertIn(current[0]["channel_name"], entries)
                destination = root / "guide.xml.gz"
                runner.write_tivimate_xmltv(
                    destination=destination, entries=entries,
                    connection=connection, window_start=NOW,
                )
                xml = gzip.decompress(destination.read_bytes()).decode("utf-8")
                self.assertIn(presentation.BRAND_TITLE, xml)
                self.assertIn("https://wa.me/12368462036", xml)
                coverage = runner.build_guide_coverage([branded], streams, scope="server_3")
                self.assertEqual(coverage["counts"]["ignoredNonChannel"], 1)
                self.assertEqual(coverage["counts"]["usefulGuide"], 0)
                self.assertEqual(coverage["denominators"]["actionableNonQuarantinedRows"], 0)
                future = runner.build_future_programme_coverage([branded], schedules, now_epoch=NOW)
                self.assertEqual(future["eligibleStreams"], 0)
                ordinary_raw = _mapping_row(server_id="server_3", stream_id="empty-channel",
                    channel_name="Uncovered News", category_name="USA NEWS", epg_id="News.test")
                ordinary = runner.parse_mapping_csv(_mapping_bytes([ordinary_raw]), {"server_3"})[0]
                app = runner.write_app_epg(destination=root / "app.json.gz", rows=[branded, ordinary],
                    connection=connection, generated_at=NOW, window_start=NOW,
                    window_end=NOW + 86400, icon_overrides=runner.empty_icon_overrides())
                self.assertEqual(app["mappedStreams"], 2)
                self.assertEqual(app["groupHeaderStreams"], 1)
                self.assertEqual(app["coverageEligibleStreams"], 1)
                self.assertEqual(app["coverageStreamsWithProgrammes"], 0)
                self.assertEqual(app["coveragePercent"], 0)
                public = root / "public"
                (public / "epg").mkdir(parents=True)
                (public / "EPG").mkdir(exist_ok=True)
                with self.assertRaisesRegex(runner.BuildError, "coverage 0.00%"):
                    runner.build_server(server_id="server_3", rows=[branded, ordinary],
                        connection=connection, schedule_stats=schedules, staging_public=public,
                        generated_at=NOW, xml_window_start=NOW,
                        app_window_start=NOW, app_window_end=NOW + 86400,
                        icon_overrides=runner.empty_icon_overrides(), source_provenance={},
                        minimum_coverage=40)
            finally:
                connection.close()

    def test_legacy_header_needs_current_provider_evidence(self):
        raw = header_row()
        raw.update(enabled="TRUE", action="AUTO_DUMMY", notes="",
                   reason=presentation.HEADING_LEGACY_CLASSIFICATION_REASON)
        parsed = runner.parse_mapping_csv(_mapping_bytes([raw]), {"server_3"})
        absent, absent_stats = runner.apply_provider_event_title_overrides(
            parsed, inventory_bytes([]), {"server_3"}
        )
        self.assertEqual(absent, parsed)
        self.assertEqual(absent_stats["groupHeaderRows"], 0)
        projected, stats = runner.apply_provider_event_title_overrides(
            parsed, inventory_bytes([raw]), {"server_3"}
        )
        self.assertEqual(stats["groupHeaderRows"], 1)
        self.assertEqual(runner.synthetic_programme_title(projected[0]), presentation.BRAND_TITLE)

    def test_rotating_event_preserves_previous_verified_xml_name_alias(self):
        old_name = "US (ESPN+ 001) | Team A vs. Team B Sep 30 7:00PM ET"
        new_name = "US (ESPN+ 001) | Team C vs. Team D Oct 15 7:00PM ET"
        raw = _mapping_row(server_id="server_3", stream_id="event",
            channel_name=old_name, category_name="|NA| USA ESPN+", epg_id="")
        raw.update(enabled="FALSE", action="REVIEW")
        parsed = runner.parse_mapping_csv(_mapping_bytes([raw]), {"server_3"}, require_enabled_servers=False)
        current = dict(raw, channel_name=new_name)
        projected, _ = runner.apply_provider_event_title_overrides(
            parsed, inventory_bytes([current]), {"server_3"}
        )
        self.assertIn(old_name, projected[0].provider_name_aliases)
        with tempfile.TemporaryDirectory() as temporary:
            connection = runner.create_database(Path(temporary) / "guide.sqlite3")
            try:
                runner.insert_synthetic_guides(connection=connection, rows=projected,
                    window_start=NOW, window_end=NOW + 2 * 86400, reference_epoch=NOW)
                entries, streams, _ = runner.build_xml_entries(rows=projected,
                    connection=connection, schedule_stats=runner.load_schedule_stats(connection, NOW),
                    icon_overrides=runner.empty_icon_overrides())
            finally:
                connection.close()
        self.assertEqual(streams, {"event"})
        self.assertEqual(entries[old_name].channel_key, entries[new_name].channel_key)
        self.assertEqual(entries[old_name].source_key, entries[new_name].source_key)
        moved = dict(current, category_name="|NA| USA NEWS")
        unsafe, _ = runner.apply_provider_event_title_overrides(
            parsed, inventory_bytes([moved]), {"server_3"}
        )
        self.assertEqual(unsafe[0].provider_name_aliases, ())
        self.assertFalse(unsafe[0].runtime_eligible)

    def test_timed_games_switch_at_boundaries_and_stale_names_do_not_claim_live(self):
        raw = _mapping_row(server_id="server_3", stream_id="event",
            channel_name="US (ESPN+ 001) | Team A vs. Team B Oct 15 7:00PM ET (2026-10-15 19:00:00)",
            category_name="|NA| USA ESPN+", epg_id="ESPN+.Dummy.us",
            epg_feed="DUMMY_CHANNELS", source="dummy")
        raw["action"] = "AUTO_DUMMY"
        row = runner.parse_mapping_csv(_mapping_bytes([raw]), {"server_3"})[0]
        start = int(datetime(2026, 10, 15, 23, tzinfo=timezone.utc).timestamp())
        event = runner.decode_synthetic_event_title(row, reference_epoch=NOW)
        self.assertEqual(event.start_epoch, start)
        self.assertEqual(event.name, "Team A vs. Team B")
        self.assertTrue(runner._event_programme_details(event, start_epoch=start - 1)[0].startswith("Next game:"))
        title, description = runner._event_programme_details(event, start_epoch=start)
        self.assertEqual(title, "Live (scheduled): Team A vs. Team B")
        self.assertIn("not a verified stream signal", description)
        self.assertEqual(runner._event_programme_details(event, start_epoch=start + 6 * 3600)[0], "No upcoming event announced")
        stale = replace(row, channel_name="US (ESPN+ 001) | Old Match Sep 30 10:00AM ET")
        self.assertEqual(runner.synthetic_programme_title(stale, reference_epoch=NOW), "No upcoming event announced")

    def test_explicit_end_is_used_without_guessing_duration(self):
        event = runner.SyntheticEventTitle(name="Team A vs Team B", start_epoch=NOW,
            time_label="Oct 15, 2026 11:00 AM EDT", explicit_end_epoch=NOW + 3600,
            timing_status="scheduled")
        self.assertTrue(runner._event_programme_details(event, start_epoch=NOW + 3599)[0].startswith("Live (scheduled):"))
        self.assertEqual(runner._event_programme_details(event, start_epoch=NOW + 3600)[0], "No upcoming event announced")


if __name__ == "__main__":
    unittest.main()
