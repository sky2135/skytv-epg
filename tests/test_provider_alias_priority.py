from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as builder


NOW = 1_800_000_000


def mapping_rows(*, primary_source="epgshare01", include_primary=True):
    event = {
        "server_id": "server_3", "stream_id": "1", "enabled": "TRUE",
        "channel_name": "NFL | 01 - Red at Blue", "category_name": "|NA| USA NFL",
        "action": "APPROVED", "source": "epgshare01", "epg_feed": "ALL_SOURCES1",
        "epg_id": "Event.Real.test",
    }
    primary = {
        **event, "stream_id": "2", "channel_name": "Primary Channel",
        "category_name": "Sports", "source": primary_source,
        "epg_id": "Primary.Real.test" if primary_source == "epgshare01" else "Primary.Dummy.test",
        "epg_feed": "ALL_SOURCES1" if primary_source == "epgshare01" else "DUMMY_CHANNELS",
        "action": "APPROVED" if primary_source == "epgshare01" else "AUTO_DUMMY",
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(event), lineterminator="\n")
    writer.writeheader()
    writer.writerows([event, primary] if include_primary else [event])
    return builder.parse_mapping_csv(output.getvalue().encode(), {"server_3"})


def add_schedule(connection, row, *, hours):
    connection.execute(
        "INSERT INTO channels VALUES (?, ?, ?, ?, '')",
        (row.source_key, row.epg_id, row.epg_id, row.channel_name),
    )
    connection.execute(
        "INSERT INTO programmes VALUES (?, ?, ?, ?, 'Fixture programme', '', '', '[]', 1)",
        (row.source_key, row.epg_id, NOW, NOW + hours * 3600),
    )


class ProviderAliasPriorityTests(unittest.TestCase):
    def build_entries(self, rows):
        with tempfile.TemporaryDirectory() as temporary:
            connection = builder.create_database(Path(temporary) / "schedules.sqlite3")
            try:
                # The event's longer future horizon must not let an optional
                # presentation alias replace another stream's primary name.
                for index, row in enumerate(rows):
                    add_schedule(connection, row, hours=12 if index == 0 else 1)
                connection.commit()
                entries, _streams, _conflicts = builder.build_xml_entries(
                    rows=rows, connection=connection,
                    schedule_stats=builder.load_schedule_stats(connection, NOW),
                    icon_overrides=builder.empty_icon_overrides(),
                )
                return entries
            finally:
                connection.close()

    def test_real_event_aliases_cannot_replace_real_or_synthetic_primary_names(self):
        for source in ("epgshare01", "dummy"):
            for field in ("provider_epg_id_aliases", "provider_name_aliases"):
                with self.subTest(primary_source=source, alias_field=field):
                    rows = mapping_rows(primary_source=source)
                    rows[0] = replace(rows[0], **{field: ("Primary Channel",)})
                    entries = self.build_entries(rows)
                    entry = entries["Primary Channel"]
                    self.assertEqual((entry.source_key, entry.channel_key), (rows[1].source_key, rows[1].epg_id))
                    self.assertEqual(entry.entry_type, "channel_name")
                    self.assertEqual(entries["Event.Real.test"].channel_key, rows[0].epg_id)

    def test_uncontested_aliases_still_link_to_the_current_real_guide(self):
        for field in ("provider_epg_id_aliases", "provider_name_aliases"):
            with self.subTest(alias_field=field):
                rows = mapping_rows(include_primary=False)
                rows[0] = replace(rows[0], **{field: ("Cached provider label",)})
                entries = self.build_entries(rows)
                self.assertEqual(entries["Cached provider label"].channel_key, "Event.Real.test")
                self.assertEqual(entries["Cached provider label"].source_key, "epgshare01")


if __name__ == "__main__":
    unittest.main()
