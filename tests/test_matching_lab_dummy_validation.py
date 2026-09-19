from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest import mock

from matching_lab.__main__ import main
from matching_lab.artifacts import package_code_sha256
from matching_lab.compat import streaming, sync
from matching_lab.dummy_shadow import (
    DUMMY_CLASSIFICATION_SCHEMA,
    DUMMY_MANIFEST_SCHEMA,
    DUMMY_SUMMARY_SCHEMA,
)
from matching_lab.dummy_validation import validate_dummy_bundle
from matching_lab.models import (
    ContractError,
    canonical_json_bytes,
    safe_display_text,
    sha256_bytes,
    sha256_json,
)
from matching_lab.retrieval import mapping_row_guard, provider_identity_guard


CREATED_AT = "2026-09-18T00:00:00Z"
VALID_AS_OF = "2026-09-18T01:00:00Z"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _csv_bytes(headers: Sequence[str], rows: Sequence[Mapping[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(headers),
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _mapping_row(
    stream_id: str,
    *,
    channel_name: str | None = None,
    category_name: str = "US | Events",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    name = channel_name or f"Event Slot {stream_id}"
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": name,
            "canonical_name": name,
            "category_id": "events",
            "category_name": category_name,
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "metadata_status": "review",
            "metadata_locked": "FALSE",
        }
    )
    return row


def _alert_row(stream_id: str) -> dict[str, str]:
    row = {column: "" for column in sync.ALERT_COLUMNS}
    row.update(
        {
            "detected_at": CREATED_AT,
            "server_id": "server_1",
            "stream_id": stream_id,
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "status": "OPEN",
        }
    )
    return row


def _canonical_write(path: Path, value: Mapping[str, Any]) -> bytes:
    content = canonical_json_bytes(value) + b"\n"
    path.write_bytes(content)
    return content


class DummyBundleFixture:
    def __init__(
        self,
        root: Path,
        *,
        exact_bank: bool = False,
        nonbreaking_space: bool = False,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.bundle = root / "bundle"
        self.bundle.mkdir()
        self.mappings = root / "mappings.csv"
        self.alerts = root / "alerts.csv"
        channel_name = "(US) ESPN PLAY 1" if exact_bank else None
        category_name = "SPORTS | ESPN+" if exact_bank else "US | Events"
        rows = [
            _mapping_row(
                "101", channel_name=channel_name, category_name=category_name
            ),
            _mapping_row(
                "102", channel_name=channel_name, category_name=category_name
            ),
        ]
        if nonbreaking_space:
            for row in rows:
                row["channel_name"] = (
                    f"Event\N{NO-BREAK SPACE}Slot {row['stream_id']}"
                )
                row["canonical_name"] = row["channel_name"]
        mapping_content = _csv_bytes(streaming.SHEET_COLUMNS, rows)
        self.mappings.write_bytes(mapping_content)
        table = sync.parse_mapping_csv(mapping_content)
        parsed_rows = list(table.rows)

        alert_content = _csv_bytes(sync.ALERT_COLUMNS, [_alert_row("101")])
        self.alerts.write_bytes(alert_content)
        parsed_alerts = sync.parse_sync_alert_values(
            list(csv.reader(io.StringIO(alert_content.decode("utf-8"), newline="")))
        )
        open_keys = sync.open_alert_quarantine_keys(parsed_alerts)
        code_hash = package_code_sha256(
            Path(__file__).resolve().parents[1] / "matching_lab"
        )
        self.inputs = {
            "MAPPING_FILE": sha256_bytes(mapping_content),
            "MAPPING_TABLE": sync.mapping_table_fingerprint(table),
            "ALERTS_FILE": sha256_bytes(alert_content),
            "OPEN_ALERT_KEYS": sha256_json(
                [[server_id, stream_id] for server_id, stream_id in sorted(open_keys)]
            ),
            "EPG_XML": sha256_bytes(b"fixture XML"),
            "EPG_XML_CATALOG": sha256_bytes(b"fixture XML catalog"),
            "EPG_TEXT": sha256_bytes(b"fixture text"),
            "EPG_TEXT_CATALOG": sha256_bytes(b"fixture text catalog"),
        }
        self.run_id = sha256_json(
            {
                "schema": DUMMY_MANIFEST_SCHEMA,
                "mode": "dummy-shadow",
                "generated_at": CREATED_AT,
                "servers": ["server_1"],
                "input_sha256": self.inputs,
                "lab_code_sha256": code_hash,
            }
        )
        records = []
        for row in parsed_rows:
            blocked = row["stream_id"] == "101"
            if exact_bank:
                method = "exact_numbered_event_bank"
                epg_id = "ESPN+.Dummy.us"
                matcher_reason = "Exact ESPN PLAY numbered event bank"
            else:
                method = "safety_rule"
                epg_id = "PPV.EVENTS.Dummy.us"
                matcher_reason = "Verified event slot with no stable schedule"
            reasons = {
                "EXACT_XML_TEXT_DUMMY_ID",
                "SHADOW_ONLY_NO_WRITE_AUTHORITY",
                "VERIFIED_DUMMY_CLASSIFICATION",
            }
            if blocked:
                reasons.add("OPEN_SYNC_ALERT")
            unsigned = {
                "schema": DUMMY_CLASSIFICATION_SCHEMA,
                "run_id": self.run_id,
                "created_at": CREATED_AT,
                "identity": {
                    "server_id": row["server_id"],
                    "stream_id": row["stream_id"],
                    "channel_name": safe_display_text(
                        streaming.clean_text(row["channel_name"], 300), maximum=300
                    ),
                    "category_name": safe_display_text(
                        streaming.clean_text(row["category_name"], 200), maximum=200
                    ),
                    "row_guard_sha256": mapping_row_guard(row),
                    "provider_identity_sha256": provider_identity_guard(row),
                },
                "decision": {
                    "state": "BLOCKED_ALERT" if blocked else "CLASSIFIED",
                    "action": "AUTO_DUMMY",
                    "source": "dummy",
                    "epg_feed": "DUMMY_CHANNELS",
                    "epg_id": epg_id,
                    "method": method,
                    "matcher_reason": matcher_reason,
                    "reason_codes": sorted(reasons),
                    "apply_eligible": False,
                    "write_authority": False,
                },
                "evidence": {
                    "source_sha256": self.inputs["EPG_XML"],
                    "xml_catalog_sha256": self.inputs["EPG_XML_CATALOG"],
                    "text_file_sha256": self.inputs["EPG_TEXT"],
                    "text_catalog_sha256": self.inputs["EPG_TEXT_CATALOG"],
                    "text_generated": "20260918000000",
                    "mapping_table_sha256": self.inputs["MAPPING_TABLE"],
                    "alerts_file_sha256": self.inputs["ALERTS_FILE"],
                    "open_alerts_sha256": self.inputs["OPEN_ALERT_KEYS"],
                    "lab_code_sha256": code_hash,
                    "matcher_version": "8.4",
                    "matcher_build_id": "TEST-BUILD",
                    "matcher_sha256": HASH_A,
                    "matcher_engine_sha256": HASH_B,
                    "approved_aliases_sha256": HASH_C,
                    "schedule_equivalences_sha256": HASH_D,
                },
            }
            records.append(
                {"classification_id": sha256_json(unsigned), **unsigned}
            )
        self.records = records
        self.counts = {
            "REVIEW_ROWS": 2,
            "DUMMY_CLASSIFIED": 2,
            "DUMMY_UNBLOCKED": 1,
            "DUMMY_BLOCKED_ALERT": 1,
            "DUMMY_REJECTED": 0,
        }
        self.summary = {
            "schema": DUMMY_SUMMARY_SCHEMA,
            "mode": "dummy-shadow",
            "run_id": self.run_id,
            "generated_at": CREATED_AT,
            "private_details_emitted": False,
            "write_authority": False,
            "counts": dict(sorted(self.counts.items())),
        }
        self.manifest = {
            "schema": DUMMY_MANIFEST_SCHEMA,
            "mode": "dummy-shadow",
            "run_id": self.run_id,
            "generated_at": CREATED_AT,
            "servers": ["server_1"],
            "input_sha256": self.inputs,
            "lab_code_sha256": code_hash,
            "classification_count": 2,
            "counts": dict(sorted(self.counts.items())),
            "private_artifact": True,
            "write_authority": False,
        }
        self.write()

    def write(self) -> None:
        classification_content = b"".join(
            canonical_json_bytes(record) + b"\n" for record in self.records
        )
        (self.bundle / "classifications.jsonl").write_bytes(classification_content)
        summary_content = _canonical_write(self.bundle / "summary.json", self.summary)
        manifest = {
            **self.manifest,
            "classifications_sha256": sha256_bytes(classification_content),
            "summary_sha256": sha256_bytes(summary_content),
        }
        _canonical_write(self.bundle / "manifest.json", manifest)

    def reseal_records(self) -> None:
        for record in self.records:
            unsigned = dict(record)
            unsigned.pop("classification_id", None)
            record["classification_id"] = sha256_json(unsigned)
        self.write()


class DummyBundleValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, *, exact_bank: bool = False) -> DummyBundleFixture:
        return DummyBundleFixture(self.root, exact_bank=exact_bank)

    def test_validate_dummy_cli_forwards_read_only_inputs(self) -> None:
        bundle = self.root / "bundle"
        mappings = self.root / "mappings.csv"
        alerts = self.root / "alerts.csv"
        result = SimpleNamespace(
            classification_count=2,
            run_id=HASH_A,
            mappings_checked=True,
            alerts_checked=True,
        )
        with mock.patch(
            "matching_lab.dummy_validation.validate_dummy_bundle",
            return_value=result,
        ) as validator:
            status = main(
                [
                    "validate-dummy",
                    "--bundle-dir",
                    str(bundle),
                    "--mappings-csv",
                    str(mappings),
                    "--alerts-csv",
                    str(alerts),
                    "--as-of",
                    VALID_AS_OF,
                ]
            )
        self.assertEqual(status, 0)
        validator.assert_called_once_with(
            bundle,
            mappings_csv=mappings,
            alerts_csv=alerts,
            as_of=VALID_AS_OF,
        )

    def test_validates_exact_bundle_and_current_guards_without_writing(self) -> None:
        fixture = self.fixture()
        before = {
            path.name: path.read_bytes() for path in fixture.bundle.iterdir()
        }
        result = validate_dummy_bundle(
            fixture.bundle,
            mappings_csv=fixture.mappings,
            alerts_csv=fixture.alerts,
            as_of=VALID_AS_OF,
        )
        self.assertEqual(result.run_id, fixture.run_id)
        self.assertEqual(result.classification_count, 2)
        self.assertEqual(result.expires_at, "2026-09-18T06:00:00Z")
        self.assertTrue(result.mappings_checked)
        self.assertTrue(result.alerts_checked)
        self.assertEqual(
            before,
            {path.name: path.read_bytes() for path in fixture.bundle.iterdir()},
        )

    def test_current_mapping_display_whitespace_is_compared_canonically(self) -> None:
        fixture = DummyBundleFixture(
            self.root / "display-whitespace", nonbreaking_space=True
        )
        result = validate_dummy_bundle(
            fixture.bundle,
            mappings_csv=fixture.mappings,
            alerts_csv=fixture.alerts,
            as_of=VALID_AS_OF,
        )
        self.assertEqual(result.classification_count, 2)
        self.assertEqual(result.classifications[0].channel_name, "Event Slot 101")

    def test_exact_numbered_event_bank_requires_exact_shape(self) -> None:
        fixture = self.fixture(exact_bank=True)
        result = validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)
        self.assertEqual(result.classifications[0].method, "exact_numbered_event_bank")

        fixture.records[0]["identity"]["channel_name"] = "ESPN"
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "independently verified"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "third")
        fixture.records[0]["decision"]["matcher_reason"] = "  Verified   event slot  "
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "canonical bounded form"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_numbered_bank_ids_cannot_be_resealed_as_safety_rules(self) -> None:
        for name, epg_id in (
            ("espn", "ESPN+.Dummy.us"),
            ("flo", "Flo.Events.Dummy.us"),
        ):
            with self.subTest(epg_id=epg_id):
                fixture = DummyBundleFixture(self.root / name)
                fixture.records[0]["decision"]["epg_id"] = epg_id
                fixture.records[0]["decision"]["matcher_reason"] = (
                    "Verified event bank"
                )
                fixture.reseal_records()
                with self.assertRaisesRegex(ContractError, "outside its exact rule"):
                    validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_rejects_missing_matcher_reason_and_unsafe_safety_reason(self) -> None:
        fixture = self.fixture()
        fixture.records[0]["decision"].pop("matcher_reason")
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "invalid fields"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "second")
        fixture.records[0]["decision"]["matcher_reason"] = "Pinned classification"
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "independently verified"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_rejects_apply_or_write_authority_at_every_layer(self) -> None:
        for layer in ("record-apply", "record-write", "summary", "manifest"):
            with self.subTest(layer=layer):
                fixture_root = self.root / layer
                fixture = DummyBundleFixture(fixture_root)
                if layer == "record-apply":
                    fixture.records[0]["decision"]["apply_eligible"] = True
                    fixture.reseal_records()
                elif layer == "record-write":
                    fixture.records[0]["decision"]["write_authority"] = True
                    fixture.reseal_records()
                elif layer == "summary":
                    fixture.summary["write_authority"] = True
                    fixture.write()
                else:
                    fixture.manifest["write_authority"] = True
                    fixture.write()
                with self.assertRaises(ContractError):
                    validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_rejects_noncanonical_unknown_or_extra_artifacts(self) -> None:
        fixture = self.fixture()
        summary_path = fixture.bundle / "summary.json"
        summary_path.write_text(
            json.dumps(fixture.summary, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ContractError, "not canonical"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "unknown")
        fixture.records[0]["decision"]["unknown"] = "unsafe"
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "unknown=unknown"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "extra")
        (fixture.bundle / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "exactly three"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_rejects_bad_ids_hashes_run_binding_and_counts(self) -> None:
        fixture = self.fixture()
        fixture.records[0]["classification_id"] = HASH_A
        fixture.write()
        with self.assertRaisesRegex(ContractError, "classification_id"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "run")
        fixture.records[0]["run_id"] = HASH_A
        fixture.reseal_records()
        with self.assertRaisesRegex(ContractError, "another run_id"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "hash")
        fixture.manifest["classification_count"] = 1
        fixture.write()
        with self.assertRaisesRegex(ContractError, "classification count"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "counts")
        fixture.counts["DUMMY_UNBLOCKED"] = 0
        fixture.summary["counts"] = dict(sorted(fixture.counts.items()))
        fixture.manifest["counts"] = dict(sorted(fixture.counts.items()))
        fixture.write()
        with self.assertRaisesRegex(ContractError, "DUMMY_UNBLOCKED"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

        fixture = DummyBundleFixture(self.root / "cap")
        fixture.counts["REVIEW_ROWS"] = 30_001
        fixture.summary["counts"] = dict(sorted(fixture.counts.items()))
        fixture.manifest["counts"] = dict(sorted(fixture.counts.items()))
        fixture.write()
        with self.assertRaisesRegex(ContractError, "REVIEW count"):
            validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)

    def test_rejects_future_or_expired_as_of(self) -> None:
        fixture = self.fixture()
        with self.assertRaisesRegex(ContractError, "generated after"):
            validate_dummy_bundle(
                fixture.bundle, as_of="2026-09-17T23:59:59Z"
            )
        with self.assertRaisesRegex(ContractError, "expired"):
            validate_dummy_bundle(
                fixture.bundle, as_of="2026-09-18T06:00:00Z"
            )

    def test_optional_mapping_check_rejects_resealed_stale_guard(self) -> None:
        fixture = self.fixture()
        fixture.records[0]["identity"]["row_guard_sha256"] = HASH_A
        fixture.reseal_records()
        validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)
        with self.assertRaisesRegex(ContractError, "row guard"):
            validate_dummy_bundle(
                fixture.bundle,
                mappings_csv=fixture.mappings,
                as_of=VALID_AS_OF,
            )

    def test_optional_alert_check_rejects_resealed_unblocked_open_row(self) -> None:
        fixture = self.fixture()
        fixture.records[0]["decision"]["state"] = "CLASSIFIED"
        fixture.records[0]["decision"]["reason_codes"].remove("OPEN_SYNC_ALERT")
        fixture.counts["DUMMY_BLOCKED_ALERT"] = 0
        fixture.counts["DUMMY_UNBLOCKED"] = 2
        fixture.summary["counts"] = dict(sorted(fixture.counts.items()))
        fixture.manifest["counts"] = dict(sorted(fixture.counts.items()))
        fixture.reseal_records()
        validate_dummy_bundle(fixture.bundle, as_of=VALID_AS_OF)
        with self.assertRaisesRegex(ContractError, "stale OPEN-alert state"):
            validate_dummy_bundle(
                fixture.bundle,
                alerts_csv=fixture.alerts,
                as_of=VALID_AS_OF,
            )


if __name__ == "__main__":
    unittest.main()
