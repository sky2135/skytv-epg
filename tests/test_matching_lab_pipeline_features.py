from __future__ import annotations

import csv
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from matching_lab.compat import streaming, sync
from matching_lab.models import DecisionState
from matching_lab.pipeline import run_shadow
from matching_lab.state import ObservationLedger
from matching_lab.validation import validate_bundle


AS_OF = "2026-09-18T12:00:00Z"
VALIDATED_AS_OF = "2026-09-18T13:00:00Z"


def _mapping_row() -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "region_code": "north_america",
            "genre": "general",
            "primary_language": "en",
            "stream_id": "101",
            "enabled": "FALSE",
            "channel_name": "US: Good Channel",
            "canonical_name": "Good Channel",
            "category_id": "general",
            "category_name": "US | General",
            "country_codes": "US",
            "language_codes": "en",
            "audience_codes": "general",
            "content_rating": "general",
            "channel_role": "linear",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "metadata_status": "review",
            "metadata_source": "provider_category",
            "metadata_confidence": "0.8",
            "metadata_locked": "FALSE",
        }
    )
    return row


def _csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _xml_bytes() -> bytes:
    channels = (
        ("Good.Channel.us2", "Good Channel"),
        ("Good.Channel.us_locals1", "Good Channel"),
    )
    rows = ["<?xml version='1.0' encoding='UTF-8'?><tv>"]
    rows.extend(
        f'<channel id="{epg_id}"><display-name>{name}</display-name></channel>'
        for epg_id, name in channels
    )
    for epg_id, _name in channels:
        rows.extend(
            (
                f'<programme channel="{epg_id}" start="20260918130000 +0000" '
                'stop="20260918170000 +0000"><title>Programme One</title></programme>',
                f'<programme channel="{epg_id}" start="20260918170000 +0000" '
                'stop="20260918200000 +0000"><title>Programme Two</title></programme>',
            )
        )
    rows.append("</tv>")
    return "".join(rows).encode("utf-8")


def _gemini_envelope() -> bytes:
    generated = {
        "results": [
            {
                "review_id": "r000001",
                "decision": "SUGGEST",
                "candidate_key": "c001",
                "confidence": "HIGH",
            }
        ]
    }
    document = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": json.dumps(generated, separators=(",", ":"))}
                    ]
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 9,
            "candidatesTokenCount": 3,
            "totalTokenCount": 12,
        },
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


class _CountingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.content = _gemini_envelope()

    def post(self, url: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append((url, dict(kwargs)))
        return SimpleNamespace(status_code=200, content=self.content)


class MatchingLabPipelineFeatureTests(unittest.TestCase):
    def test_ai_cache_validator_and_ledger_replay_are_end_to_end_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            cache = root / "advisory-cache.sqlite3"
            ledger = root / "observations.sqlite3"
            first_output = root / "first-bundle"
            replay_output = root / "replay-bundle"
            mappings.write_bytes(
                _csv_bytes(tuple(streaming.SHEET_COLUMNS), [_mapping_row()])
            )
            alerts.write_bytes(_csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(_xml_bytes())
            catalog.write_text(
                "20260918120000\n"
                "-- epg_ripper_US2 --\n"
                "Good.Channel.us2\n"
                "-- epg_ripper_US_LOCALS1 --\n"
                "Good.Channel.us_locals1\n",
                encoding="utf-8",
            )

            api_key = "integration-test-api-key"
            transport = _CountingTransport()
            common = {
                "mappings_csv": mappings,
                "alerts_csv": alerts,
                "all_source_file": source,
                "all_source_catalog_file": catalog,
                "as_of": AS_OF,
                "minimum_unique_channels": 2,
                "ai_api_key": api_key,
                "ai_cache_path": cache,
                "ai_transport": transport,
                "ai_sensitive_values": (api_key,),
                "ledger_path": ledger,
            }
            first = run_shadow(output_dir=first_output, **common)
            replay = run_shadow(output_dir=replay_output, **common)

            self.assertEqual(first.run_id, replay.run_id)
            self.assertEqual(first.proposal_count, 1)
            self.assertEqual(first.counts["AI_ELIGIBLE"], 1)
            self.assertEqual(first.counts["AI_REQUESTED"], 1)
            self.assertEqual(first.counts["AI_SUPPORTED"], 1)
            self.assertEqual(len(transport.calls), 1)
            for name in ("manifest.json", "proposals.jsonl", "summary.json"):
                self.assertEqual(
                    (first_output / name).read_bytes(),
                    (replay_output / name).read_bytes(),
                )
                self.assertNotIn(api_key.encode("utf-8"), (first_output / name).read_bytes())

            first_validation = validate_bundle(
                first_output,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of=VALIDATED_AS_OF,
            )
            replay_validation = validate_bundle(
                replay_output,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of=VALIDATED_AS_OF,
            )
            self.assertEqual(first_validation.run_id, replay_validation.run_id)
            self.assertEqual(first_validation.proposal_count, 1)
            proposal = first_validation.proposals[0]
            self.assertGreaterEqual(
                sum(not candidate.conflicts for candidate in proposal.candidates), 2
            )
            self.assertIs(proposal.state, DecisionState.NEEDS_REVIEW)
            self.assertIn("AI_SUPPORTS_LOCAL", proposal.reason_codes)
            self.assertFalse(proposal.auto_apply_eligible)
            self.assertEqual(proposal.selected_candidate_key, "c001")

            with ObservationLedger(ledger) as observations:
                stats = observations.verify_chain()
                self.assertEqual(stats.runs, 1)
                self.assertEqual(stats.proposals, 1)
                self.assertEqual(stats.validations, 0)
                self.assertEqual(stats.events, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
