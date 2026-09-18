from __future__ import annotations

import csv
import gzip
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


from matching_lab.artifacts import strict_json_loads
from matching_lab.compat import streaming, sync
from matching_lab.models import ContractError, ProtectedSemantics
from matching_lab.normalization import protected_conflicts
from matching_lab.pipeline import run_shadow
from matching_lab.validation import validate_bundle


AS_OF = "2026-09-15T12:00:00Z"
NOW = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())


def mapping_row(
    *,
    stream_id: str,
    channel_name: str,
    action: str = "REVIEW",
    enabled: str = "FALSE",
    epg_id: str = "",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "region_code": "north_america",
            "genre": "general",
            "primary_language": "en",
            "stream_id": stream_id,
            "enabled": enabled,
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": "general",
            "category_name": "US | General",
            "country_codes": "US",
            "language_codes": "en",
            "audience_codes": "general",
            "content_rating": "general",
            "channel_role": "linear",
            "action": action,
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "epg_id": epg_id,
            "metadata_status": "review",
            "metadata_source": "provider_category",
            "metadata_confidence": "0.8",
            "metadata_locked": "FALSE",
        }
    )
    return row


def csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def xml_bytes() -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?><tv>"
        '<channel id="Good.Channel.us2"><display-name>Good Channel</display-name></channel>'
        '<channel id="Good.Channel.2.us2"><display-name>Good Channel 2</display-name></channel>'
        '<programme channel="Good.Channel.us2" start="20260915130000 +0000" '
        'stop="20260915170000 +0000"><title>Programme One</title></programme>'
        '<programme channel="Good.Channel.us2" start="20260915170000 +0000" '
        'stop="20260915200000 +0000"><title>Programme Two</title></programme>'
        "</tv>"
    ).encode("utf-8")


class MatchingLabContractTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys_floats_and_bom(self) -> None:
        for content in (b'{"a":1,"a":2}', b'{"score":0.5}', b'\xef\xbb\xbf{}'):
            with self.subTest(content=content):
                with self.assertRaises(ContractError):
                    strict_json_loads(content)

    def test_protected_semantics_are_symmetric(self) -> None:
        plain = ProtectedSemantics(market="US", numbers=())
        numbered = ProtectedSemantics(market="US", numbers=("2",))
        plus = ProtectedSemantics(market="US", has_plus=True)
        self.assertIn(
            "NUMBER_MISMATCH",
            protected_conflicts(plain, numbered, route_explicit=True),
        )
        self.assertIn(
            "NUMBER_MISMATCH",
            protected_conflicts(numbered, plain, route_explicit=True),
        )
        self.assertIn(
            "PLUS_VARIANT_MISMATCH",
            protected_conflicts(plain, plus, route_explicit=True),
        )

    def test_shadow_run_is_deterministic_and_has_no_write_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            output_a = root / "run-a"
            output_b = root / "run-b"
            mappings.write_bytes(
                csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [
                        mapping_row(
                            stream_id="100",
                            channel_name="Good Channel",
                            action="APPROVED",
                            enabled="TRUE",
                            epg_id="Good.Channel.us2",
                        ),
                        mapping_row(stream_id="101", channel_name="Good Channel"),
                    ],
                )
            )
            alerts.write_bytes(csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(xml_bytes())
            catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US2 --\n"
                "Good.Channel.us2\n"
                "Good.Channel.2.us2\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                sync,
                "update_google_sheet_review_rows",
                side_effect=AssertionError("the shadow Lab attempted a Sheet write"),
            ) as sheet_writer:
                first = run_shadow(
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    all_source_file=source,
                    all_source_catalog_file=catalog,
                    output_dir=output_a,
                    as_of=AS_OF,
                    minimum_unique_channels=2,
                )
            sheet_writer.assert_not_called()
            second = run_shadow(
                mappings_csv=mappings,
                alerts_csv=alerts,
                all_source_file=source,
                all_source_catalog_file=catalog,
                output_dir=output_b,
                as_of=AS_OF,
                minimum_unique_channels=2,
            )

            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(first.proposal_count, 1)
            for name in ("manifest.json", "proposals.jsonl", "summary.json"):
                self.assertEqual((output_a / name).read_bytes(), (output_b / name).read_bytes())
            proposal = json.loads((output_a / "proposals.jsonl").read_text("utf-8"))
            self.assertEqual(proposal["decision"]["state"], "AUTO_ELIGIBLE")
            self.assertFalse(proposal["decision"]["auto_apply_eligible"])
            self.assertIn("SHADOW_ONLY_NO_WRITE_AUTHORITY", proposal["decision"]["reason_codes"])
            self.assertEqual(proposal["candidates"][0]["epg_id"], "Good.Channel.us2")
            self.assertEqual(proposal["candidates"][0]["programme"]["state"], "PASS")
            validated = validate_bundle(
                output_a,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of="2026-09-15T12:01:00Z",
            )
            self.assertEqual(validated.run_id, first.run_id)
            self.assertEqual(validated.proposal_count, 1)
            self.assertTrue(validated.mappings_checked)
            self.assertTrue(validated.alerts_checked)
            proposal_path = output_a / "proposals.jsonl"
            proposal_path.write_bytes(
                proposal_path.read_bytes().replace(b"Good Channel", b"Evil Channel")
            )
            with self.assertRaises(ContractError):
                validate_bundle(output_a, as_of="2026-09-15T12:01:00Z")

    def test_open_alert_blocks_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            mappings.write_bytes(
                csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [
                        mapping_row(
                            stream_id="101",
                            channel_name="Good Channel",
                            epg_id="Good.Channel.us2",
                        )
                    ],
                )
            )
            alert = {column: "" for column in sync.ALERT_COLUMNS}
            alert.update(
                {
                    "detected_at": AS_OF,
                    "server_id": "server_1",
                    "stream_id": "101",
                    "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                    "status": "OPEN",
                }
            )
            alerts.write_bytes(csv_bytes(tuple(sync.ALERT_COLUMNS), [alert]))
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(xml_bytes())
            catalog.write_text(
                "20260915120000\n-- epg_ripper_US2 --\n"
                "Good.Channel.us2\nGood.Channel.2.us2\n",
                encoding="utf-8",
            )
            output = root / "run"
            run_shadow(
                mappings_csv=mappings,
                alerts_csv=alerts,
                all_source_file=source,
                all_source_catalog_file=catalog,
                output_dir=output,
                as_of=AS_OF,
                minimum_unique_channels=2,
            )
            proposal = json.loads((output / "proposals.jsonl").read_text("utf-8"))
            self.assertEqual(proposal["decision"]["state"], "BLOCKED_ALERT")
            self.assertEqual(proposal["candidates"], [])
            validated = validate_bundle(
                output,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of="2026-09-15T12:01:00Z",
            )
            self.assertEqual(validated.proposal_count, 1)
            self.assertTrue(validated.mappings_checked)
            self.assertTrue(validated.alerts_checked)

    def test_prefilled_review_ids_are_revalidated_without_ranking_bias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            output = root / "run"
            mappings.write_bytes(
                csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [
                        mapping_row(
                            stream_id="201",
                            channel_name="Good Channel",
                            epg_id="Good.Channel.2.us2",
                        ),
                        mapping_row(
                            stream_id="202",
                            channel_name="Good Channel",
                            epg_id="Missing.Channel.us2",
                        ),
                    ],
                )
            )
            alerts.write_bytes(csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(xml_bytes())
            catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US2 --\n"
                "Good.Channel.us2\n"
                "Good.Channel.2.us2\n",
                encoding="utf-8",
            )

            result = run_shadow(
                mappings_csv=mappings,
                alerts_csv=alerts,
                all_source_file=source,
                all_source_catalog_file=catalog,
                output_dir=output,
                as_of=AS_OF,
                minimum_unique_channels=2,
            )

            self.assertEqual(result.proposal_count, 2)
            proposals = [
                json.loads(line)
                for line in (output / "proposals.jsonl").read_text("utf-8").splitlines()
            ]
            by_stream = {
                proposal["identity"]["stream_id"]: proposal for proposal in proposals
            }
            for proposal in proposals:
                self.assertEqual(
                    proposal["candidates"][0]["epg_id"], "Good.Channel.us2"
                )
                self.assertIn(
                    "PREFILLED_ID_UNTRUSTED_EVIDENCE",
                    proposal["decision"]["reason_codes"],
                )
            self.assertIn(
                "PREFILLED_ID_CATALOG_REVALIDATED",
                by_stream["201"]["decision"]["reason_codes"],
            )
            self.assertIn(
                "PREFILLED_ID_NOT_CORROBORATED",
                by_stream["202"]["decision"]["reason_codes"],
            )
            validate_bundle(
                output,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of="2026-09-15T12:01:00Z",
            )

    def test_shadow_preserves_exact_nfkc_sensitive_epg_id(self) -> None:
        exact_epg_id = "TBS.Channel.１.jp"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            output = root / "run"
            row = mapping_row(
                stream_id="unicode-１",
                channel_name="TBS Channel １",
            )
            row.update(
                {
                    "canonical_name": "TBS Channel １",
                    "region_code": "",
                    "category_name": "General",
                    "country_codes": "",
                }
            )
            mappings.write_bytes(
                csv_bytes(tuple(streaming.SHEET_COLUMNS), [row])
            )
            alerts.write_bytes(csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            xml = (
                "<?xml version='1.0' encoding='UTF-8'?><tv>"
                f'<channel id="{exact_epg_id}"><display-name>'
                "TBS Channel １</display-name></channel>"
                '<channel id="Other.Channel.jp"><display-name>'
                "Other Channel</display-name></channel>"
                f'<programme channel="{exact_epg_id}" '
                'start="20260915130000 +0000" stop="20260915170000 +0000">'
                "<title>Programme One</title></programme>"
                f'<programme channel="{exact_epg_id}" '
                'start="20260915170000 +0000" stop="20260915200000 +0000">'
                "<title>Programme Two</title></programme>"
                "</tv>"
            ).encode("utf-8")
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(xml)
            catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_JP1 --\n"
                f"{exact_epg_id}\n"
                "Other.Channel.jp\n",
                encoding="utf-8",
            )

            run_shadow(
                mappings_csv=mappings,
                alerts_csv=alerts,
                all_source_file=source,
                all_source_catalog_file=catalog,
                output_dir=output,
                as_of=AS_OF,
                minimum_unique_channels=2,
            )

            proposal = json.loads(
                (output / "proposals.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(proposal["identity"]["stream_id"], "unicode-１")
            self.assertEqual(proposal["identity"]["channel_name"], "TBS Channel 1")
            self.assertIn(
                exact_epg_id,
                {candidate["epg_id"] for candidate in proposal["candidates"]},
            )
            validated = validate_bundle(
                output,
                mappings_csv=mappings,
                alerts_csv=alerts,
                as_of="2026-09-15T12:01:00Z",
            )
            self.assertEqual(validated.proposal_count, 1)


if __name__ == "__main__":
    unittest.main()
