from __future__ import annotations

import csv
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from matching_lab.compat import automatch, streaming, sync
from matching_lab.dummy_shadow import run_dummy_shadow


AS_OF = "2026-09-15T12:00:00Z"


def _csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _mapping_row(
    stream_id: str,
    *,
    channel_name: str,
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": "espn-events",
            "category_name": "SPORTS | ESPN+",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": "",
            "metadata_status": "review",
            "metadata_locked": "FALSE",
        }
    )
    return row


class DummyShadowIntegrationTests(unittest.TestCase):
    def test_real_matcher_classifies_exact_bank_but_not_linear_espn2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            output = root / "dummy-shadow"

            mappings.write_bytes(
                _csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [
                        _mapping_row("101", channel_name="(US) ESPN PLAY 1"),
                        _mapping_row("102", channel_name="ESPN2"),
                    ],
                )
            )
            alerts.write_bytes(_csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            xml = (
                "<?xml version='1.0' encoding='UTF-8'?><tv>"
                '<channel id="Good.Channel.us2">'
                "<display-name>Good Channel</display-name></channel>"
                '<channel id="ESPN+.Dummy.us">'
                "<display-name>ESPN Plus Events</display-name></channel>"
                "</tv>"
            ).encode("utf-8")
            with source.open("wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
                    handle.write(xml)
            catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US2 --\n"
                "Good.Channel.us2\n"
                "-- epg_ripper_DUMMY_CHANNELS --\n"
                "ESPN+.Dummy.us\n",
                encoding="utf-8",
            )

            finalized: list[tuple[object, object]] = []
            production_finalize = automatch.finalize_proposal

            def capture_finalize(proposal: object, evidence: object) -> object:
                result = production_finalize(proposal, evidence)
                finalized.append((proposal, result))
                return result

            with mock.patch.object(
                automatch,
                "finalize_proposal",
                side_effect=capture_finalize,
            ):
                result = run_dummy_shadow(
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    all_source_file=source,
                    all_source_catalog_file=catalog,
                    output_dir=output,
                    as_of=AS_OF,
                    minimum_unique_channels=2,
                )

            self.assertEqual(result.classification_count, 1)
            self.assertEqual(result.counts["REVIEW_ROWS"], 2)
            self.assertEqual(result.counts["DUMMY_CLASSIFIED"], 1)
            self.assertEqual(result.counts["DUMMY_UNBLOCKED"], 1)

            records = [
                json.loads(line)
                for line in (output / "classifications.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["identity"]["stream_id"], "101")
            self.assertNotEqual(record["identity"]["stream_id"], "102")
            self.assertEqual(
                record["decision"]["method"], "exact_numbered_event_bank"
            )
            self.assertEqual(record["decision"]["epg_id"], "ESPN+.Dummy.us")
            self.assertFalse(record["decision"]["apply_eligible"])
            self.assertFalse(record["decision"]["write_authority"])

            self.assertEqual(len(finalized), 1)
            proposal, production_result = finalized[0]
            self.assertEqual(proposal.stream_id, "101")
            self.assertEqual(proposal.match_method, "exact_numbered_event_bank")
            self.assertFalse(proposal.eligible_for_finalization)
            self.assertFalse(production_result.approved)


if __name__ == "__main__":
    unittest.main()
