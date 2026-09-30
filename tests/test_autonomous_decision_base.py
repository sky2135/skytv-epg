from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Mapping
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_autonomous_decision_base as decision_base  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from matching_lab.artifacts import jsonl_bytes, package_code_sha256  # noqa: E402
from matching_lab.models import (  # noqa: E402
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    canonical_json_bytes,
    sha256_bytes,
)
from matching_lab.retrieval import (  # noqa: E402
    mapping_row_guard,
    provider_identity_guard,
)
from matching_lab.validation import ValidationResult  # noqa: E402


CREATED_AT = "2026-09-15T12:00:00Z"
EXPIRES_AT = "2026-09-15T18:00:00Z"
VALID_AS_OF = "2026-09-15T12:01:00Z"
SHADOW_RUN_ID = sha256_bytes(b"shadow run")
SOURCE_SHA256 = sha256_bytes(b"epg xml")
TEXT_SHA256 = sha256_bytes(b"epg text")
CATALOG_SHA256 = sha256_bytes(b"catalog")
POLICY_SHA256 = sha256_bytes(b"shadow policy")
CODE_SHA256 = sha256_bytes(b"shadow code")


def _csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _row(
    *,
    server_id: str,
    stream_id: str,
    channel_name: str,
    enabled: str = "FALSE",
    action: str = "REVIEW",
    source: str = "epgshare01",
    epg_feed: str = "ALL_SOURCES1",
    epg_id: str = "",
    notes: str = "",
    reason: str = "",
    genre: str = "general",
    category_name: str = "US | General",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": server_id,
            "server_label": server_id.replace("_", " ").title(),
            "region_code": "US",
            "genre": genre,
            "primary_language": "en",
            "stream_id": stream_id,
            "enabled": enabled,
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": "us-general",
            "category_name": category_name,
            "country_codes": "US",
            "language_codes": "en",
            "audience_codes": "general",
            "content_rating": "general",
            "channel_role": "linear",
            "sort_priority": "1000",
            "action": action,
            "source": source,
            "epg_feed": epg_feed,
            "epg_id": epg_id,
            "metadata_status": "review" if action == "REVIEW" else "auto",
            "metadata_source": "provider_category",
            "metadata_confidence": "0.8",
            "metadata_locked": "FALSE",
            "reason": reason,
            "notes": notes,
        }
    )
    return row


def _alert(stream_id: str) -> dict[str, str]:
    row = {column: "" for column in sync.ALERT_COLUMNS}
    row.update(
        {
            "detected_at": CREATED_AT,
            "server_id": "server_1",
            "stream_id": stream_id,
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": "Alerted Channel",
            "provider_channel_name": "Reused Channel",
            "sheet_category_name": "US | General",
            "provider_category_name": "US | Other",
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
        }
    )
    return row


def _candidate() -> CandidateEvidence:
    return CandidateEvidence(
        candidate_key="c001",
        epg_id="Good.Channel.us2",
        display_name="Good Channel",
        feed="US2",
        region="US",
        score_ppm=1_000_000,
        methods=("STRICT_EXACT",),
        conflicts=(),
        features_ppm=(("TOKEN_SCORE", 1_000_000),),
        semantics=ProtectedSemantics(market="US"),
        programme_state=ProgrammeState.PASS,
        programme_count=2,
        programme_first_start_epoch=1_789_476_400,
        programme_latest_stop_epoch=1_789_501_600,
        programme_reason="Exact ID has a non-placeholder current/future programme guide.",
    )


def _proposal(row: Mapping[str, str], state: DecisionState) -> ProposalRecord:
    selected = state is DecisionState.AUTO_ELIGIBLE
    candidates = (_candidate(),) if selected else ()
    draft = ProposalRecord(
        schema="skytv.smart-match-proposal.v2",
        proposal_id="0" * 64,
        run_id=SHADOW_RUN_ID,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        server_id=str(row["server_id"]),
        stream_id=str(row["stream_id"]),
        channel_name=str(row["channel_name"]),
        category_name=str(row["category_name"]),
        row_guard_sha256=mapping_row_guard(row),
        provider_identity_sha256=provider_identity_guard(row),
        state=state,
        reason_codes=("OPEN_SYNC_ALERT",) if state is DecisionState.BLOCKED_ALERT else (),
        route_explicit=selected,
        market="US" if selected else "",
        selected_candidate_key="c001" if selected else "",
        score_ppm=1_000_000 if selected else 0,
        margin_ppm=1_000_000 if selected else 0,
        candidates=candidates,
        source_sha256=SOURCE_SHA256,
        text_catalog_sha256=TEXT_SHA256,
        catalog_fingerprint_sha256=CATALOG_SHA256,
        catalog_generation_token="202609151200",
        policy_sha256=POLICY_SHA256,
        lab_code_sha256=CODE_SHA256,
        auto_apply_eligible=False,
    )
    return replace(draft, proposal_id=draft.computed_id())


class AutonomousDecisionBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mappings = self.root / "mappings.csv"
        self.alerts = self.root / "alerts.csv"
        self.bundle = self.root / "shadow"

        existing_synthetic = _row(
            server_id="server_3",
            stream_id="30",
            channel_name="Existing Synthetic",
            epg_id="Old.Real.us2",
            genre="movies",
        )
        decision_base.automatch._apply_coverage_fallback(
            key=("server_3", "30"),
            original=dict(existing_synthetic),
            patched=existing_synthetic,
            source_sha256=SOURCE_SHA256,
        )
        self.rows = [
            _row(
                server_id="server_1",
                stream_id="10",
                channel_name="Existing Real",
                enabled="TRUE",
                action="AUTO_EPGSHARE",
                epg_id="Existing.Real.us2",
            ),
            _row(
                server_id="server_2",
                stream_id="20",
                channel_name="Existing Native",
                enabled="TRUE",
                action="KEEP_PANEL",
                source="panel",
                epg_feed="panel",
                epg_id="native.20",
            ),
            existing_synthetic,
            _row(server_id="server_1", stream_id="101", channel_name="Good Channel"),
            _row(
                server_id="server_1",
                stream_id="102",
                channel_name="Mystery Movie Network",
                genre="movies",
            ),
            _row(server_id="server_1", stream_id="103", channel_name="Alerted Channel"),
            _row(server_id="server_1", stream_id="104", channel_name="==== SPORTS ===="),
            _row(
                server_id="server_1",
                stream_id="105",
                channel_name="Manual Mystery",
                epg_id="Manual.Candidate.us2",
                notes="Human-selected candidate; do not overwrite.",
            ),
            _row(
                server_id="server_2",
                stream_id="106",
                channel_name="PL+ 001 | Team A v Team B (2026-08-28 18:50:00)",
                source="panel",
                epg_feed="panel",
                epg_id="native.106",
                genre="unknown",
                category_name="LIVE | PL+ (EPL)",
            ),
        ]
        mapping_content = _csv_bytes(tuple(streaming.SHEET_COLUMNS), self.rows)
        alert_content = _csv_bytes(tuple(sync.ALERT_COLUMNS), [_alert("103")])
        self.mappings.write_bytes(mapping_content)
        self.alerts.write_bytes(alert_content)

        review_states = {
            "101": DecisionState.AUTO_ELIGIBLE,
            "102": DecisionState.ABSTAIN,
            "103": DecisionState.BLOCKED_ALERT,
            "104": DecisionState.ABSTAIN,
            "105": DecisionState.ABSTAIN,
            "106": DecisionState.ABSTAIN,
        }
        proposals = tuple(
            _proposal(row, review_states[row["stream_id"]])
            for row in self.rows
            if row["action"] == "REVIEW"
        )
        self.validation = ValidationResult(
            bundle_dir=self.bundle,
            run_id=SHADOW_RUN_ID,
            generated_at=CREATED_AT,
            expires_at=EXPIRES_AT,
            validated_as_of=VALID_AS_OF,
            proposal_count=len(proposals),
            counts=Counter(proposal.state.value for proposal in proposals),
            proposals=proposals,
            mappings_checked=True,
            alerts_checked=True,
        )
        self._write_fake_validated_bundle(mapping_content, alert_content)

    def _write_fake_validated_bundle(
        self, mapping_content: bytes, alert_content: bytes
    ) -> None:
        self.bundle.mkdir()
        proposal_content = jsonl_bytes(
            proposal.public_dict() for proposal in self.validation.proposals
        )
        summary_content = canonical_json_bytes({"run_id": SHADOW_RUN_ID}) + b"\n"
        manifest = {
            "run_id": SHADOW_RUN_ID,
            "generated_at": CREATED_AT,
            "expires_at": EXPIRES_AT,
            "proposal_count": self.validation.proposal_count,
            "lab_code_sha256": package_code_sha256(REPO_ROOT / "matching_lab"),
            "input_sha256": {
                "MAPPING_FILE": sha256_bytes(mapping_content),
                "ALERTS_FILE": sha256_bytes(alert_content),
                "EPG_XML": SOURCE_SHA256,
                "EPG_TEXT": TEXT_SHA256,
            },
            "proposals_sha256": sha256_bytes(proposal_content),
            "summary_sha256": sha256_bytes(summary_content),
        }
        (self.bundle / "proposals.jsonl").write_bytes(proposal_content)
        (self.bundle / "summary.json").write_bytes(summary_content)
        (self.bundle / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")

    @staticmethod
    def _records(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def test_deterministic_complete_ledger_and_terminal_decisions(self) -> None:
        with mock.patch.object(
            decision_base, "validate_bundle", return_value=self.validation
        ) as validator:
            first = decision_base.build_decision_base(
                mappings_csv=self.mappings,
                alerts_csv=self.alerts,
                shadow_bundle=self.bundle,
                output_dir=self.root / "decision-a",
                as_of=VALID_AS_OF,
                emit_mapping_patch=True,
            )
            second = decision_base.build_decision_base(
                mappings_csv=self.mappings,
                alerts_csv=self.alerts,
                shadow_bundle=self.bundle,
                output_dir=self.root / "decision-b",
                as_of=VALID_AS_OF,
                emit_mapping_patch=True,
            )
        self.assertEqual(validator.call_count, 4)
        self.assertEqual(first.run_id, second.run_id)
        for name in ("decision_ledger.jsonl", "mapping_patch.jsonl", "manifest.json"):
            self.assertEqual(
                (first.output_dir / name).read_bytes(),
                (second.output_dir / name).read_bytes(),
            )

        records = self._records(first.ledger_path)
        self.assertEqual(len(records), 9)
        self.assertEqual(len({row["ledger_key_sha256"] for row in records}), 9)
        by_stream = {str(row["identity"]["stream_id"]): row for row in records}
        self.assertEqual(by_stream["10"]["decision"], "RETAIN_REAL")
        self.assertEqual(by_stream["20"]["decision"], "RETAIN_NATIVE")
        self.assertEqual(by_stream["30"]["decision"], "RETAIN_SYNTHETIC")
        self.assertTrue(by_stream["30"]["upgrade_candidate"])
        self.assertEqual(by_stream["101"]["decision"], "VERIFIED_EPGSHARE_REAL")
        self.assertEqual(
            by_stream["101"]["selected_target"]["epg_id"], "Good.Channel.us2"
        )
        self.assertEqual(by_stream["102"]["decision"], "LOCAL_SYNTHETIC")
        self.assertEqual(
            by_stream["102"]["selected_target"]["epg_id"], "Synthetic.Movie.local"
        )
        self.assertEqual(
            by_stream["102"]["classification"]["synthetic"][
                "projected_programme_class"
            ],
            "DEFER_TO_PRODUCTION_BUILDER",
        )
        self.assertEqual(by_stream["103"]["decision"], "QUARANTINED")
        self.assertIn("OPEN_SYNC_ALERT", by_stream["103"]["evidence"]["reason_codes"])
        self.assertEqual(by_stream["104"]["decision"], "IGNORE")
        self.assertEqual(by_stream["105"]["decision"], "QUARANTINED")
        self.assertIn(
            "NO_AUTONOMOUS_OVERWRITE", by_stream["105"]["evidence"]["reason_codes"]
        )
        self.assertEqual(by_stream["106"]["decision"], "QUARANTINED")
        self.assertIn(
            "NATIVE_REVALIDATION_REQUIRED",
            by_stream["106"]["evidence"]["reason_codes"],
        )
        self.assertTrue(by_stream["106"]["upgrade_candidate"])
        self.assertIn(
            "NATIVE_ID_PRESENT",
            {item["kind"] for item in by_stream["106"]["upgrade_evidence"]},
        )

        patch_streams = {
            str(row["identity"]["stream_id"])
            for row in self._records(first.patch_path)
        }
        self.assertNotIn("105", patch_streams)
        self.assertNotIn("103", patch_streams)
        self.assertNotIn("106", patch_streams)
        self.assertTrue({"101", "102", "104"}.issubset(patch_streams))
        patches = {
            str(row["identity"]["stream_id"]): row
            for row in self._records(first.patch_path)
        }
        self.assertEqual(
            patches["102"]["changes"]["epg_id"],
            by_stream["102"]["selected_target"]["epg_id"],
        )

        manifest = json.loads(first.manifest_path.read_text("utf-8"))
        reconciliation = manifest["reconciliation"]
        self.assertEqual(reconciliation["literal_total"], 9)
        self.assertEqual(reconciliation["assigned_target_total"], 5)
        self.assertFalse(reconciliation["meets_literal_assignment_target"])
        self.assertEqual(
            manifest["quarantine_counts"]["NATIVE_REVALIDATION_REQUIRED"], 1
        )
        self.assertEqual(
            reconciliation["real"]
            + reconciliation["native"]
            + reconciliation["synthetic"]
            + reconciliation["ignored"]
            + reconciliation["quarantined"],
            reconciliation["literal_total"],
        )
        self.assertEqual(
            reconciliation["assigned_target_total"] + reconciliation["uncovered"],
            reconciliation["channel_rows_excluding_ignored"],
        )
        self.assertEqual(
            manifest["artifacts"]["decision_ledger"]["sha256"], first.ledger_sha256
        )
        self.assertNotIn("literal_coverage_percent", reconciliation)
        self.assertEqual(
            reconciliation["actual_programme_coverage"]["status"], "NOT_MEASURED"
        )

    def test_complete_ledger_requires_current_alerts(self) -> None:
        with self.assertRaisesRegex(ContractError, "requires the current Sync Alerts"):
            decision_base.build_decision_base(
                mappings_csv=self.mappings,
                alerts_csv=None,
                shadow_bundle=self.bundle,
                output_dir=self.root / "unsafe-ledger",
                as_of=VALID_AS_OF,
            )
        unchecked = replace(self.validation, alerts_checked=False)
        with mock.patch.object(
            decision_base, "validate_bundle", return_value=unchecked
        ):
            with self.assertRaisesRegex(ContractError, "not validated against current"):
                decision_base.build_decision_base(
                    mappings_csv=self.mappings,
                    alerts_csv=self.alerts,
                    shadow_bundle=self.bundle,
                    output_dir=self.root / "unchecked-ledger",
                    as_of=VALID_AS_OF,
                )

    def test_exact_server_one_migration_is_not_a_manual_prefill(self) -> None:
        row = _row(
            server_id="server_1",
            stream_id="107",
            channel_name="DSTV : Super Motorsport (FHD).",
            source="panel",
            epg_feed="server xmltv.php",
            epg_id="supersportmotorsport.za",
            reason=decision_base.automatch._LEGACY_SERVER1_REASON,
            notes=next(iter(decision_base.automatch._LEGACY_SERVER1_NOTES)),
        )
        self.assertTrue(decision_base.automatch._exact_legacy_server1_migration(row))
        self.assertFalse(
            decision_base._is_manual_prefill(row, source_kind="panel")
        )
        row["notes"] += " edited"
        self.assertTrue(decision_base._is_manual_prefill(row, source_kind="panel"))

    def test_changed_mapping_expired_or_missing_proposal_fails_closed(self) -> None:
        changed = self.root / "changed.csv"
        changed.write_text(
            self.mappings.read_text("utf-8").replace(
                "Mystery Movie Network", "Changed Network", 1
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            decision_base, "validate_bundle", return_value=self.validation
        ):
            with self.assertRaisesRegex(ContractError, "Mapping input changed"):
                decision_base.build_decision_base(
                    mappings_csv=changed,
                    alerts_csv=self.alerts,
                    shadow_bundle=self.bundle,
                    output_dir=self.root / "changed-output",
                    as_of=VALID_AS_OF,
                )
        with mock.patch.object(
            decision_base,
            "validate_bundle",
            side_effect=ContractError("The shadow bundle has expired."),
        ):
            with self.assertRaisesRegex(ContractError, "expired"):
                decision_base.build_decision_base(
                    mappings_csv=self.mappings,
                    alerts_csv=self.alerts,
                    shadow_bundle=self.bundle,
                    output_dir=self.root / "expired-output",
                    as_of="2026-09-15T19:00:00Z",
                )
        incomplete = replace(
            self.validation,
            proposal_count=self.validation.proposal_count - 1,
            proposals=self.validation.proposals[:-1],
        )
        with mock.patch.object(
            decision_base, "validate_bundle", return_value=incomplete
        ):
            with self.assertRaisesRegex(
                ContractError, "validated proposal objects|does not exactly cover"
            ):
                decision_base.build_decision_base(
                    mappings_csv=self.mappings,
                    alerts_csv=self.alerts,
                    shadow_bundle=self.bundle,
                    output_dir=self.root / "incomplete-output",
                    as_of=VALID_AS_OF,
                )

    def test_tampered_bundle_fails_even_after_validator_boundary(self) -> None:
        tampered = self.root / "tampered-shadow"
        shutil.copytree(self.bundle, tampered)
        (tampered / "proposals.jsonl").write_bytes(b"")
        with mock.patch.object(
            decision_base, "validate_bundle", return_value=self.validation
        ):
            with self.assertRaisesRegex(ContractError, "changed after validation"):
                decision_base.build_decision_base(
                    mappings_csv=self.mappings,
                    alerts_csv=self.alerts,
                    shadow_bundle=tampered,
                    output_dir=self.root / "tampered-output",
                    as_of=VALID_AS_OF,
                )

    def test_server_one_panel_target_is_never_retained(self) -> None:
        row = _row(
            server_id="server_1",
            stream_id="999",
            channel_name="Invalid Native",
            enabled="TRUE",
            action="KEEP_PANEL",
            source="panel",
            epg_feed="panel",
            epg_id="native.999",
        )
        with self.assertRaisesRegex(ContractError, "Server 1"):
            decision_base._decision_record(
                row=row,
                row_number=2,
                proposal=None,
                run_id=sha256_bytes(b"decision run"),
                source_hashes={"mapping_file": sha256_bytes(b"mapping")},
            )

    def test_forged_rollback_action_mismatch_and_manual_heading_fail_safe(self) -> None:
        forged = dict(self.rows[2])
        forged["notes"] = forged["notes"].replace("binding_sha256=", "binding_sha256=f")
        retained = decision_base._decision_record(
            row=forged,
            row_number=2,
            proposal=None,
            run_id=sha256_bytes(b"decision run"),
            source_hashes={"mapping_file": sha256_bytes(b"mapping")},
        )
        self.assertEqual(retained["decision"], "RETAIN_SYNTHETIC")
        self.assertFalse(retained["upgrade_candidate"])

        mismatch = _row(
            server_id="server_2",
            stream_id="998",
            channel_name="Mismatched Dummy",
            enabled="TRUE",
            action="AUTO_DUMMY",
            source="epgshare01",
            epg_feed="ALL_SOURCES1",
            epg_id="Wrong.Source.us2",
        )
        with self.assertRaisesRegex(ContractError, "inconsistent action/source"):
            decision_base._decision_record(
                row=mismatch,
                row_number=2,
                proposal=None,
                run_id=sha256_bytes(b"decision run"),
                source_hashes={"mapping_file": sha256_bytes(b"mapping")},
            )

        manual_heading = _row(
            server_id="server_1",
            stream_id="997",
            channel_name="==== MANUAL ====",
            epg_id="Human.Selected.us2",
        )
        protected = decision_base._decision_record(
            row=manual_heading,
            row_number=2,
            proposal=_proposal(manual_heading, DecisionState.ABSTAIN),
            run_id=sha256_bytes(b"decision run"),
            source_hashes={"mapping_file": sha256_bytes(b"mapping")},
        )
        self.assertEqual(protected["decision"], "QUARANTINED")
        self.assertIn(
            "MANUAL_CANDIDATE_PROTECTED", protected["evidence"]["reason_codes"]
        )


if __name__ == "__main__":
    unittest.main()
