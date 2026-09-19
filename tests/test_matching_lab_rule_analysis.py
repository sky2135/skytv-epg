from __future__ import annotations

import json
import io
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from matching_lab import PROPOSAL_SCHEMA
from matching_lab.models import (
    AIReviewEvidence,
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
)
from matching_lab.__main__ import main
from matching_lab.rule_analysis import RuleAnalysisResult, analyze_rule_candidates
from matching_lab.validation import ValidationResult


AS_OF = "2026-09-18T01:00:00Z"
CREATED_AT = "2026-09-18T00:00:00Z"
EXPIRES_AT = "2026-09-18T06:00:00Z"


def _proposal(
    stream_id: str,
    *,
    channel_name: str,
    epg_id: str,
    server_id: str = "server_1",
    programme_state: ProgrammeState = ProgrammeState.PASS,
    conflicts: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (
        "LANE_HUMAN_REVIEW",
        "PROGRAMME_GATE_PASSED",
        "SHADOW_ONLY_NO_WRITE_AUTHORITY",
    ),
    ai: AIReviewEvidence | None = None,
) -> ProposalRecord:
    candidate = CandidateEvidence(
        candidate_key="c001",
        epg_id=epg_id,
        display_name=f"Guide for {epg_id}",
        feed="US",
        region="US",
        score_ppm=900_000,
        methods=("STRICT_EXACT",),
        conflicts=conflicts,
        features_ppm=(
            ("ACRONYM_SCORE", 900_000),
            ("CONTEXTUAL_SCORE", 900_000),
            ("NGRAM_SCORE", 900_000),
            ("TOKEN_SCORE", 900_000),
            ("TRANSLITERATION_SCORE", 900_000),
        ),
        semantics=ProtectedSemantics(market="US", languages=("en",)),
        programme_state=programme_state,
        programme_count=3 if programme_state is ProgrammeState.PASS else 0,
        programme_first_start_epoch=(
            1_789_675_200 if programme_state is ProgrammeState.PASS else None
        ),
        programme_latest_stop_epoch=(
            1_789_718_400 if programme_state is ProgrammeState.PASS else None
        ),
        programme_reason=(
            "Exact ID has a non-placeholder current/future programme guide."
            if programme_state is ProgrammeState.PASS
            else "Programme evidence is insufficient."
        ),
    )
    draft = ProposalRecord(
        schema=PROPOSAL_SCHEMA,
        proposal_id="0" * 64,
        run_id="1" * 64,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_name="US | General",
        row_guard_sha256=(stream_id[-1] if stream_id[-1] in "abcdef0123456789" else "2")
        * 64,
        provider_identity_sha256="3" * 64,
        state=DecisionState.NEEDS_REVIEW,
        reason_codes=reasons,
        route_explicit=True,
        market="US",
        selected_candidate_key="c001",
        score_ppm=900_000,
        margin_ppm=900_000,
        candidates=(candidate,),
        source_sha256="4" * 64,
        text_catalog_sha256="5" * 64,
        catalog_fingerprint_sha256="6" * 64,
        catalog_generation_token="202609180000",
        policy_sha256="7" * 64,
        lab_code_sha256="8" * 64,
        ai=ai,
    )
    return replace(draft, proposal_id=draft.computed_id())


class MatchingLabRuleAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        for name in ("manifest.json", "proposals.jsonl", "summary.json"):
            (self.bundle / name).write_text(f"{name}\n", encoding="utf-8")

    def _validation(
        self, proposals: tuple[ProposalRecord, ...]
    ) -> ValidationResult:
        return ValidationResult(
            bundle_dir=self.bundle.resolve(),
            run_id="1" * 64,
            generated_at=CREATED_AT,
            expires_at=EXPIRES_AT,
            validated_as_of=AS_OF,
            proposal_count=len(proposals),
            counts={},
            proposals=proposals,
            mappings_checked=True,
            alerts_checked=True,
        )

    def _run(
        self,
        proposals: tuple[ProposalRecord, ...],
        output_name: str = "analysis",
    ) -> tuple[Path, object]:
        output = self.root / output_name
        with mock.patch(
            "matching_lab.rule_analysis.validate_bundle",
            return_value=self._validation(proposals),
        ) as validator:
            result = analyze_rule_candidates(
                bundle_dir=self.bundle,
                output_dir=output,
                as_of=AS_OF,
                mappings_csv=self.root / "mappings.csv",
                alerts_csv=self.root / "alerts.csv",
            )
        validator.assert_called_once()
        return output, result

    def test_groups_normalized_identity_with_one_target(self) -> None:
        proposals = (
            _proposal(
                "101",
                channel_name="News One HD",
                epg_id="News.One.us",
                server_id="server_1",
            ),
            _proposal(
                "102",
                channel_name="NEWS-ONE HD",
                epg_id="News.One.us",
                server_id="server_2",
            ),
        )
        output, result = self._run(proposals)
        records = [
            json.loads(line)
            for line in (output / "rule_candidates.jsonl").read_text().splitlines()
        ]
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.consistent_count, 1)
        self.assertEqual(records[0]["status"], "CONSISTENT_TARGET")
        self.assertEqual(records[0]["group"]["normalized_identity"], "news one hd")
        self.assertEqual(records[0]["evidence_count"], 2)
        self.assertEqual(records[0]["distinct_server_count"], 2)
        self.assertFalse(records[0]["write_authority"])
        self.assertTrue(records[0]["requires_human_review"])
        self.assertEqual(
            {item["proposal_id"] for item in records[0]["evidence"]},
            {proposal.proposal_id for proposal in proposals},
        )
        self.assertTrue(
            all(item["row_guard_sha256"] for item in records[0]["evidence"])
        )

    def test_flags_contradictory_targets(self) -> None:
        output, result = self._run(
            (
                _proposal("201", channel_name="Example TV", epg_id="Example.A.us"),
                _proposal(
                    "202",
                    channel_name="example.tv",
                    epg_id="Example.B.us",
                    server_id="server_3",
                ),
            )
        )
        record = json.loads(
            (output / "rule_candidates.jsonl").read_text().splitlines()[0]
        )
        self.assertEqual(result.contradictory_count, 1)
        self.assertEqual(record["status"], "CONTRADICTORY_TARGETS")
        self.assertEqual(
            [target["epg_id"] for target in record["targets"]],
            ["Example.A.us", "Example.B.us"],
        )
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["counts"]["CONTRADICTORY_TARGET_GROUPS"], 1)

    def test_same_server_repetition_is_not_cross_server_corroboration(self) -> None:
        output, result = self._run(
            (
                _proposal(
                    "211",
                    channel_name="Repeated Provider Name",
                    epg_id="Repeated.us",
                    server_id="server_1",
                ),
                _proposal(
                    "212",
                    channel_name="Repeated-Provider-Name",
                    epg_id="Repeated.us",
                    server_id="server_1",
                ),
            )
        )

        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.consistent_count, 0)
        self.assertEqual((output / "rule_candidates.jsonl").read_bytes(), b"")
        counts = json.loads((output / "summary.json").read_text())["counts"]
        self.assertEqual(counts["RECURRING_GROUPS"], 1)
        self.assertEqual(
            counts["EXCLUDED_SINGLE_SERVER_CONSISTENT_GROUPS"], 1
        )
        self.assertEqual(counts["CONSISTENT_TARGET_GROUPS"], 0)

    def test_non_supporting_ai_evidence_is_excluded_from_rule_mining(self) -> None:
        base_reasons = {
            "LANE_HUMAN_REVIEW",
            "PROGRAMME_GATE_PASSED",
            "SHADOW_ONLY_NO_WRITE_AUTHORITY",
        }
        advisory_cases = (
            (
                "AI_DISAGREES",
                AIReviewEvidence(
                    request_sha256="a" * 64,
                    model="test-model",
                    prompt_version="matching-lab-advisory-v1",
                    decision="SUGGEST",
                    candidate_key="c002",
                    confidence="HIGH",
                ),
            ),
            (
                "AI_ABSTAINED",
                AIReviewEvidence(
                    request_sha256="b" * 64,
                    model="test-model",
                    prompt_version="matching-lab-advisory-v1",
                    decision="ABSTAIN",
                ),
            ),
            (
                "AI_LOW_CONFIDENCE",
                AIReviewEvidence(
                    request_sha256="c" * 64,
                    model="test-model",
                    prompt_version="matching-lab-advisory-v1",
                    decision="SUGGEST",
                    candidate_key="c001",
                    confidence="MEDIUM",
                ),
            ),
            (
                "AI_REVIEW_ERROR",
                AIReviewEvidence(
                    request_sha256="d" * 64,
                    model="test-model",
                    prompt_version="matching-lab-advisory-v1",
                    decision="ERROR",
                    error_code="TEST_ERROR",
                ),
            ),
        )
        excluded = tuple(
            _proposal(
                str(220 + index),
                channel_name="AI Warning Name",
                epg_id="Warning.us",
                server_id=("server_1" if index % 2 else "server_2"),
                reasons=tuple(sorted(base_reasons | {reason_code})),
                ai=evidence,
            )
            for index, (reason_code, evidence) in enumerate(advisory_cases, start=1)
        )
        supported_evidence = AIReviewEvidence(
            request_sha256="e" * 64,
            model="test-model",
            prompt_version="matching-lab-advisory-v1",
            decision="SUGGEST",
            candidate_key="c001",
            confidence="HIGH",
        )
        supported = (
            _proposal(
                "231",
                channel_name="AI Supported Name",
                epg_id="Supported.us",
                server_id="server_1",
                reasons=tuple(sorted(base_reasons | {"AI_SUPPORTS_LOCAL"})),
                ai=supported_evidence,
            ),
            _proposal(
                "232",
                channel_name="AI-Supported-Name",
                epg_id="Supported.us",
                server_id="server_2",
                reasons=tuple(sorted(base_reasons | {"AI_SUPPORTS_LOCAL"})),
                ai=replace(supported_evidence, request_sha256="f" * 64),
            ),
        )

        output, result = self._run(excluded + supported)
        records = [
            json.loads(line)
            for line in (output / "rule_candidates.jsonl").read_text().splitlines()
        ]
        self.assertEqual(result.candidate_count, 1)
        self.assertEqual(result.consistent_count, 1)
        self.assertEqual(
            records[0]["group"]["normalized_identity"], "ai supported name"
        )
        counts = json.loads((output / "summary.json").read_text())["counts"]
        self.assertEqual(counts["EXCLUDED_AI_NON_SUPPORT"], 4)
        self.assertEqual(counts["ELIGIBLE_EVIDENCE_ROWS"], 2)

    def test_excludes_nonpassing_conflicted_and_unknown_alert_evidence(self) -> None:
        proposals = (
            _proposal(
                "301",
                channel_name="Safe Name",
                epg_id="Safe.us",
                programme_state=ProgrammeState.FAIL,
            ),
            _proposal(
                "302",
                channel_name="Safe Name",
                epg_id="Safe.us",
                conflicts=("NUMBER_MISMATCH",),
            ),
            _proposal(
                "303",
                channel_name="Safe Name",
                epg_id="Safe.us",
                reasons=("ALERT_SNAPSHOT_MISSING", "LANE_HUMAN_REVIEW"),
            ),
        )
        output, result = self._run(proposals)
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual((output / "rule_candidates.jsonl").read_bytes(), b"")
        counts = json.loads((output / "summary.json").read_text())["counts"]
        self.assertEqual(counts["EXCLUDED_PROGRAMME_NOT_PASS"], 1)
        self.assertEqual(counts["EXCLUDED_CONFLICT"], 1)
        self.assertEqual(counts["EXCLUDED_ALERT_OR_UNKNOWN"], 1)

    def test_is_deterministic_read_only_offline_and_private_permissions(self) -> None:
        proposals = (
            _proposal("401", channel_name="Offline TV", epg_id="Offline.us"),
            _proposal(
                "402",
                channel_name="Offline-TV",
                epg_id="Offline.us",
                server_id="server_2",
            ),
        )
        mappings = self.root / "mappings.csv"
        alerts = self.root / "alerts.csv"
        mappings.write_bytes(b"mapping sentinel\n")
        alerts.write_bytes(b"alerts sentinel\n")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (*self.bundle.iterdir(), mappings, alerts)
        }
        validation = self._validation(proposals)
        with (
            mock.patch(
                "matching_lab.rule_analysis.validate_bundle",
                return_value=validation,
            ),
            mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError("network access is forbidden"),
            ),
        ):
            first = analyze_rule_candidates(
                bundle_dir=self.bundle,
                output_dir=self.root / "first",
                as_of=AS_OF,
                mappings_csv=mappings,
                alerts_csv=alerts,
            )
            second = analyze_rule_candidates(
                bundle_dir=self.bundle,
                output_dir=self.root / "second",
                as_of=AS_OF,
                mappings_csv=mappings,
                alerts_csv=alerts,
            )
        self.assertEqual(first.candidate_count, second.candidate_count)
        for filename in ("rule_candidates.jsonl", "summary.json"):
            self.assertEqual(
                (self.root / "first" / filename).read_bytes(),
                (self.root / "second" / filename).read_bytes(),
            )
        for path, state in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
        self.assertEqual(
            stat.S_IMODE((self.root / "first").stat().st_mode), 0o700
        )
        for filename in ("rule_candidates.jsonl", "summary.json"):
            self.assertEqual(
                stat.S_IMODE((self.root / "first" / filename).stat().st_mode),
                0o600,
            )
        summary_text = (self.root / "first" / "summary.json").read_text()
        self.assertNotIn("Offline TV", summary_text)
        self.assertNotIn("Offline.us", summary_text)

    def test_validates_before_rejecting_output_inside_bundle(self) -> None:
        validation = self._validation(())
        with mock.patch(
            "matching_lab.rule_analysis.validate_bundle",
            return_value=validation,
        ) as validator:
            with self.assertRaisesRegex(ContractError, "separate"):
                analyze_rule_candidates(
                    bundle_dir=self.bundle,
                    output_dir=self.bundle / "analysis",
                    as_of=AS_OF,
                )
        validator.assert_called_once()
        self.assertFalse((self.bundle / "analysis").exists())

    def test_analyze_rules_cli_forwards_read_only_inputs(self) -> None:
        output = self.root / "cli-analysis"
        expected = RuleAnalysisResult(
            output_dir=output,
            candidate_count=3,
            consistent_count=2,
            contradictory_count=1,
            evidence_row_count=7,
        )
        stdout = io.StringIO()
        with (
            mock.patch(
                "matching_lab.rule_analysis.analyze_rule_candidates",
                return_value=expected,
            ) as analyzer,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "analyze-rules",
                    "--bundle-dir",
                    str(self.bundle),
                    "--output-dir",
                    str(output),
                    "--as-of",
                    AS_OF,
                    "--mappings-csv",
                    str(self.root / "mappings.csv"),
                    "--alerts-csv",
                    str(self.root / "alerts.csv"),
                ]
            )
        self.assertEqual(exit_code, 0)
        analyzer.assert_called_once_with(
            bundle_dir=self.bundle,
            output_dir=output,
            as_of=AS_OF,
            mappings_csv=self.root / "mappings.csv",
            alerts_csv=self.root / "alerts.csv",
        )
        self.assertIn("3 private candidates", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
