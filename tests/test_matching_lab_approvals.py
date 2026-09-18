from __future__ import annotations

import dataclasses
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import matching_lab_approvals as approvals  # noqa: E402
from matching_lab.artifacts import strict_json_loads  # noqa: E402
from matching_lab.models import (  # noqa: E402
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    canonical_json_bytes,
    sha256_bytes,
    sha256_json,
)
from matching_lab.validation import ValidationResult  # noqa: E402


RUN_ID = "a" * 64
GENERATED_AT = "2026-09-18T00:41:00Z"
APPROVED_AT = "2026-09-18T01:00:00Z"
EXPIRES_AT = "2026-09-18T06:41:00Z"


def _proposal(
    stream_id: str,
    *,
    strong: bool = True,
    run_id: str = RUN_ID,
) -> ProposalRecord:
    candidate = CandidateEvidence(
        candidate_key=f"candidate-{stream_id}",
        epg_id=f"Guide.{stream_id}.us",
        display_name=f"Guide {stream_id}",
        feed="US1",
        region="US",
        score_ppm=930_000,
        methods=("CHAR_NGRAM_RETRIEVAL", "TOKEN_RETRIEVAL"),
        conflicts=(),
        features_ppm=(("NGRAM_SCORE", 920_000), ("TOKEN_SCORE", 940_000)),
        semantics=ProtectedSemantics(market="US"),
        programme_state=ProgrammeState.PASS,
        programme_count=2,
        programme_first_start_epoch=1_800_000_000,
        programme_latest_stop_epoch=1_800_050_000,
        programme_reason="Exact ID has a non-placeholder current/future programme guide.",
    )
    reasons = (
        "CATALOG_CORROBORATED",
        "MULTI_SIGNAL_STRONG_PROPOSAL" if strong else "LOW_MARGIN",
        "PROGRAMME_GATE_PASSED",
        "PROTECTED_SEMANTICS_COMPATIBLE",
        "PROVIDER_REVALIDATION_REQUIRED",
        "SHADOW_ONLY_NO_WRITE_AUTHORITY",
    )
    draft = ProposalRecord(
        schema="skytv.smart-match-proposal.v1",
        proposal_id="0" * 64,
        run_id=run_id,
        created_at=GENERATED_AT,
        expires_at=EXPIRES_AT,
        server_id="server_1",
        stream_id=stream_id,
        channel_name=f"Channel {stream_id}",
        category_name="US | General",
        row_guard_sha256=sha256_json(["row", stream_id]),
        provider_identity_sha256=sha256_json(["provider", stream_id]),
        state=DecisionState.NEEDS_REVIEW,
        reason_codes=reasons,
        route_explicit=True,
        market="US",
        selected_candidate_key=candidate.candidate_key,
        score_ppm=candidate.score_ppm,
        margin_ppm=120_000,
        candidates=(candidate,),
        source_sha256="b" * 64,
        text_catalog_sha256="c" * 64,
        catalog_fingerprint_sha256="d" * 64,
        catalog_generation_token="20260918004100",
        policy_sha256="e" * 64,
        lab_code_sha256="f" * 64,
        auto_apply_eligible=False,
    )
    return dataclasses.replace(draft, proposal_id=draft.computed_id())


def _fixture(
    root: Path,
    proposals: tuple[ProposalRecord, ...],
    *,
    run_id: str = RUN_ID,
    validated_at: str = APPROVED_AT,
) -> tuple[ValidationResult, Path, Path]:
    bundle = root / "bundle"
    bundle.mkdir()
    mappings = root / "mappings.csv"
    alerts = root / "alerts.csv"
    mappings.write_bytes(b"server_id,stream_id\nserver_1,2\n")
    alerts.write_bytes(b"detected_at,status\n")
    proposal_content = b"".join(
        canonical_json_bytes(proposal.public_dict()) + b"\n"
        for proposal in proposals
    )
    (bundle / "proposals.jsonl").write_bytes(proposal_content)
    manifest = {
        "run_id": run_id,
        "generated_at": GENERATED_AT,
        "expires_at": EXPIRES_AT,
        "proposals_sha256": sha256_bytes(proposal_content),
        "policy_sha256": "e" * 64,
        "lab_code_sha256": "f" * 64,
        "input_sha256": {
            "MAPPING_FILE": sha256_bytes(mappings.read_bytes()),
            "MAPPING_TABLE": sha256_json(["mapping-table"]),
            "ALERTS_FILE": sha256_bytes(alerts.read_bytes()),
            "OPEN_ALERT_KEYS": sha256_json([]),
        },
    }
    (bundle / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    validation = ValidationResult(
        bundle_dir=bundle.resolve(),
        run_id=run_id,
        generated_at=GENERATED_AT,
        expires_at=EXPIRES_AT,
        validated_as_of=validated_at,
        proposal_count=len(proposals),
        counts={},
        proposals=proposals,
        mappings_checked=True,
        alerts_checked=True,
    )
    return validation, mappings, alerts


def _rehash(document: dict[str, object]) -> dict[str, object]:
    payload = {
        key: value
        for key, value in document.items()
        if key not in {"approval_id", "content_sha256"}
    }
    content_sha256 = sha256_json(payload)
    run = document["run"]
    assert isinstance(run, dict)
    document["content_sha256"] = content_sha256
    document["approval_id"] = sha256_json(
        {
            "schema": approvals.APPROVAL_ID_SCHEMA,
            "run_id": run["run_id"],
            "content_sha256": content_sha256,
        }
    )
    return document


class MatchingLabApprovalTests(unittest.TestCase):
    def test_loader_rejects_malformed_or_noncanonical_approval_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            approval_file = Path(temporary) / "approval.json"
            for content in (
                b'{"schema":"x","schema":"y"}\n',
                b'{"score":0.5}\n',
                b"{}",
                b"{}\n",
            ):
                with self.subTest(content=content):
                    approval_file.write_bytes(content)
                    with self.assertRaises(ContractError):
                        approvals.load_approval_document(approval_file)

    def test_approve_all_strong_is_canonical_deterministic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validation, mappings, alerts = _fixture(
                root,
                (_proposal("10"), _proposal("3", strong=False), _proposal("2")),
            )
            output_a = root / "approval-a.json"
            output_b = root / "approval-b.json"
            with mock.patch.object(
                approvals, "validate_bundle", return_value=validation
            ) as validator:
                first = approvals.approve_all_strong_proposals(
                    bundle_dir=validation.bundle_dir,
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    output_file=output_a,
                    approved_at=APPROVED_AT,
                    reviewer_notes="",
                )
                second = approvals.approve_all_strong_proposals(
                    bundle_dir=validation.bundle_dir,
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    output_file=output_b,
                    approved_at=APPROVED_AT,
                )

            self.assertEqual(output_a.read_bytes(), output_b.read_bytes())
            self.assertEqual(first.approval_id, second.approval_id)
            self.assertEqual(first.proposal_count, 2)
            self.assertEqual(stat.S_IMODE(output_a.stat().st_mode), 0o600)
            document = strict_json_loads(output_a.read_bytes())
            self.assertIsInstance(document, dict)
            assert isinstance(document, dict)
            self.assertEqual(approvals.load_approval_document(output_a), document)
            self.assertEqual(document["reviewer_notes"], "")
            self.assertEqual(document["proposal_count"], 2)
            self.assertEqual(
                [proposal["stream_id"] for proposal in document["proposals"]],
                ["2", "10"],
            )
            payload = {
                key: value
                for key, value in document.items()
                if key not in {"approval_id", "content_sha256"}
            }
            self.assertEqual(document["content_sha256"], sha256_json(payload))
            self.assertEqual(
                document["approval_id"],
                sha256_json(
                    {
                        "schema": approvals.APPROVAL_ID_SCHEMA,
                        "run_id": RUN_ID,
                        "content_sha256": document["content_sha256"],
                    }
                ),
            )
            self.assertEqual(validator.call_count, 2)
            for call in validator.call_args_list:
                self.assertEqual(call.kwargs["mappings_csv"], mappings)
                self.assertEqual(call.kwargs["alerts_csv"], alerts)
                self.assertEqual(call.kwargs["as_of"], APPROVED_AT)

            uppercase = json.loads(output_b.read_text("utf-8"))
            uppercase["proposals"][0]["row_guard_sha256"] = uppercase["proposals"][
                0
            ]["row_guard_sha256"].upper()
            _rehash(uppercase)
            output_b.write_bytes(canonical_json_bytes(uppercase) + b"\n")
            with self.assertRaisesRegex(ContractError, "lowercase hexadecimal"):
                approvals.load_approval_document(output_b)

    def test_validate_rejects_tamper_duplicate_and_mismatched_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proposals = (_proposal("2"), _proposal("10"))
            validation, mappings, alerts = _fixture(root, proposals)
            approval_file = root / "approval.json"
            current_validation = dataclasses.replace(
                validation, validated_as_of="2026-09-18T01:01:00Z"
            )
            with mock.patch.object(
                approvals,
                "validate_bundle",
                side_effect=[validation, current_validation],
            ):
                approvals.approve_all_strong_proposals(
                    bundle_dir=validation.bundle_dir,
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    output_file=approval_file,
                    approved_at=APPROVED_AT,
                )
                result = approvals.validate_approval(
                    approval_file,
                    bundle_dir=validation.bundle_dir,
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    as_of="2026-09-18T01:01:00Z",
                )
            self.assertEqual(result.proposal_count, 2)

            document = json.loads(approval_file.read_text("utf-8"))
            document["proposals"][0]["selected_epg_id"] = "Tampered.us"
            _rehash(document)
            approval_file.write_bytes(canonical_json_bytes(document) + b"\n")
            with mock.patch.object(
                approvals, "validate_bundle", return_value=current_validation
            ):
                with self.assertRaisesRegex(ContractError, "exactly match"):
                    approvals.validate_approval(
                        approval_file,
                        bundle_dir=validation.bundle_dir,
                        mappings_csv=mappings,
                        alerts_csv=alerts,
                        as_of="2026-09-18T01:01:00Z",
                    )

            document["proposals"][0] = dict(document["proposals"][1])
            _rehash(document)
            approval_file.write_bytes(canonical_json_bytes(document) + b"\n")
            with self.assertRaisesRegex(ContractError, "duplicate proposal_id"):
                approvals.validate_approval(
                    approval_file,
                    bundle_dir=validation.bundle_dir,
                    mappings_csv=mappings,
                    alerts_csv=alerts,
                    as_of="2026-09-18T01:01:00Z",
                )

    def test_creation_rejects_expired_duplicate_and_wrong_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            expired_root = Path(temporary) / "expired"
            expired_root.mkdir()
            validation, mappings, alerts = _fixture(expired_root, (_proposal("2"),))
            output = expired_root / "approval.json"
            with mock.patch.object(
                approvals,
                "validate_bundle",
                side_effect=ContractError("The shadow bundle has expired."),
            ):
                with self.assertRaisesRegex(ContractError, "expired"):
                    approvals.approve_all_strong_proposals(
                        bundle_dir=validation.bundle_dir,
                        mappings_csv=mappings,
                        alerts_csv=alerts,
                        output_file=output,
                        approved_at=EXPIRES_AT,
                    )
            self.assertFalse(output.exists())

            duplicate_root = Path(temporary) / "duplicate"
            duplicate_root.mkdir()
            duplicate = _proposal("2")
            duplicate_validation, duplicate_mappings, duplicate_alerts = _fixture(
                duplicate_root, (duplicate, duplicate)
            )
            with mock.patch.object(
                approvals, "validate_bundle", return_value=duplicate_validation
            ):
                with self.assertRaisesRegex(ContractError, "duplicate proposal_id"):
                    approvals.approve_all_strong_proposals(
                        bundle_dir=duplicate_validation.bundle_dir,
                        mappings_csv=duplicate_mappings,
                        alerts_csv=duplicate_alerts,
                        output_file=duplicate_root / "approval.json",
                        approved_at=APPROVED_AT,
                    )

            mismatch_root = Path(temporary) / "mismatch"
            mismatch_root.mkdir()
            mismatched = _proposal("2", run_id="9" * 64)
            mismatch_validation, mismatch_mappings, mismatch_alerts = _fixture(
                mismatch_root, (mismatched,), run_id=RUN_ID
            )
            with mock.patch.object(
                approvals, "validate_bundle", return_value=mismatch_validation
            ):
                with self.assertRaisesRegex(ContractError, "another run_id"):
                    approvals.approve_all_strong_proposals(
                        bundle_dir=mismatch_validation.bundle_dir,
                        mappings_csv=mismatch_mappings,
                        alerts_csv=mismatch_alerts,
                        output_file=mismatch_root / "approval.json",
                        approved_at=APPROVED_AT,
                    )

    def test_creation_rejects_bundle_replacement_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validation, mappings, alerts = _fixture(root, (_proposal("2"),))
            manifest_path = validation.bundle_dir / "manifest.json"

            def replace_manifest(*_args: object, **_kwargs: object) -> ValidationResult:
                manifest = json.loads(manifest_path.read_text("utf-8"))
                manifest["input_sha256"]["MAPPING_TABLE"] = "1" * 64
                manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
                return validation

            with mock.patch.object(
                approvals, "validate_bundle", side_effect=replace_manifest
            ):
                with self.assertRaisesRegex(ContractError, "changed during"):
                    approvals.approve_all_strong_proposals(
                        bundle_dir=validation.bundle_dir,
                        mappings_csv=mappings,
                        alerts_csv=alerts,
                        output_file=root / "approval.json",
                        approved_at=APPROVED_AT,
                    )

    def test_cli_approve_strong_uses_blank_notes_and_reports_no_sheet_write(self) -> None:
        expected = approvals.ApprovalResult(
            approval_file=Path("/private/approval.json"),
            approval_id="1" * 64,
            content_sha256="2" * 64,
            run_id=RUN_ID,
            approved_at=APPROVED_AT,
            proposal_count=721,
        )
        with mock.patch.object(
            approvals, "approve_all_strong_proposals", return_value=expected
        ) as creator:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = approvals.main(
                    [
                        "approve-strong",
                        "--bundle-dir",
                        "bundle",
                        "--mappings-csv",
                        "mappings.csv",
                        "--alerts-csv",
                        "alerts.csv",
                        "--output-file",
                        "approval.json",
                        "--approved-at",
                        APPROVED_AT,
                    ]
                )
        self.assertEqual(status, 0)
        self.assertEqual(creator.call_args.kwargs["reviewer_notes"], "")
        self.assertIn("721 proposals", stdout.getvalue())
        self.assertIn("No Google Sheets write was performed.", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
