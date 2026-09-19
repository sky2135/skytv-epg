from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

from matching_lab import LAB_VERSION, PROPOSAL_SCHEMA, RUN_MANIFEST_SCHEMA
import matching_lab.validation as validation
from matching_lab.ai import PROMPT_VERSION
from matching_lab.artifacts import package_code_sha256, write_private_bundle
from matching_lab.compat import streaming, sync
from matching_lab.models import (
    AIReviewEvidence,
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    RunManifest,
    canonical_json_bytes,
    sha256_bytes,
    sha256_json,
)
from matching_lab.normalization import lexical_cohort_risk_codes
from matching_lab.policy import DEFAULT_POLICY
from matching_lab.retrieval import mapping_row_guard, provider_identity_guard
from matching_lab.validation import validate_bundle


CREATED_AT = "2026-09-18T00:00:00Z"
EXPIRES_AT = "2026-09-18T06:00:00Z"
VALID_AS_OF = "2026-09-18T01:00:00Z"
AI_MODEL = "gemini-test"


@dataclass(frozen=True)
class BundleFixture:
    bundle_dir: Path
    mappings_csv: Path
    alerts_csv: Path
    proposals: tuple[ProposalRecord, ...]


def _csv_bytes(headers: Sequence[str], rows: Sequence[Mapping[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(headers), lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _mapping_row(stream_id: str) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "region_code": "north_america",
            "genre": "general",
            "primary_language": "en",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": f"Example Channel {stream_id}",
            "canonical_name": f"Example Channel {stream_id}",
            "category_id": "us",
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


def _alert_row(stream_id: str) -> dict[str, str]:
    return {
        "detected_at": CREATED_AT,
        "server_id": "server_1",
        "stream_id": stream_id,
        "alert_type": "POSSIBLE_STREAM_ID_REUSE",
        "sheet_channel_name": f"Example Channel {stream_id}",
        "provider_channel_name": f"Reused Channel {stream_id}",
        "sheet_category_name": "US | General",
        "provider_category_name": "US | Other",
        "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
        "status": "OPEN",
        "review_notes": "",
    }


def _candidate(
    stream_id: str,
    position: int,
    *,
    method: str = "STRICT_EXACT",
) -> CandidateEvidence:
    score = 900_000 if position == 1 else 700_000
    return CandidateEvidence(
        candidate_key=f"c{position:03d}",
        epg_id=f"Example.Channel.{stream_id}.{position}.us",
        display_name=f"Example Channel {stream_id} Variant {position}",
        feed="US",
        region="US",
        score_ppm=score,
        methods=(method,),
        conflicts=(),
        features_ppm=(
            ("ACRONYM_SCORE", score),
            ("CONTEXTUAL_SCORE", score),
            ("NGRAM_SCORE", score),
            ("TOKEN_SCORE", score),
            ("TRANSLITERATION_SCORE", score),
        ),
        semantics=ProtectedSemantics(market="US"),
        programme_state=ProgrammeState.PASS,
        programme_count=3,
        programme_first_start_epoch=1_789_675_200,
        programme_latest_stop_epoch=1_789_718_400,
        programme_reason="Exact ID has a non-placeholder current/future programme guide.",
    )


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Mapping[str, Any]) -> bytes:
    content = canonical_json_bytes(value) + b"\n"
    path.write_bytes(content)
    return content


class MatchingLabValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture_number = 0

    def _build_bundle(
        self,
        *,
        stream_ids: Sequence[str] = ("1",),
        stale_row_guard: bool = False,
        open_alert_streams: Sequence[str] = (),
        block_open_rows: bool = True,
        auto_apply: bool = False,
        ai_candidate_key: str = "",
        ai_reason_code: str = "",
    ) -> BundleFixture:
        self.fixture_number += 1
        prefix = f"fixture-{self.fixture_number}"
        rows = [_mapping_row(stream_id) for stream_id in stream_ids]
        mapping_content = _csv_bytes(streaming.SHEET_COLUMNS, rows)
        mappings_csv = self.root / f"{prefix}-mappings.csv"
        mappings_csv.write_bytes(mapping_content)
        table = sync.parse_mapping_csv(mapping_content)

        open_streams = frozenset(open_alert_streams)
        alert_rows = [_alert_row(stream_id) for stream_id in sorted(open_streams)]
        alert_content = _csv_bytes(sync.ALERT_COLUMNS, alert_rows)
        alerts_csv = self.root / f"{prefix}-alerts.csv"
        alerts_csv.write_bytes(alert_content)
        parsed_alerts = sync.parse_sync_alert_values(
            list(csv.reader(io.StringIO(alert_content.decode("utf-8"), newline="")))
        )
        open_keys = sync.open_alert_quarantine_keys(parsed_alerts)

        ai_enabled = bool(ai_candidate_key)
        ai_config = {
            "enabled": ai_enabled,
            "model": AI_MODEL if ai_enabled else "",
            "prompt_version": PROMPT_VERSION if ai_enabled else "",
            "maximum_requests": 10 if ai_enabled else 0,
        }
        input_hashes = {
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
            "AI_CONFIG": sha256_json(ai_config),
        }
        policy_sha256 = DEFAULT_POLICY.sha256
        lab_code_sha256 = package_code_sha256(
            Path(__file__).resolve().parents[1] / "matching_lab"
        )
        selected_servers = sorted({row["server_id"] for row in rows})
        run_id = sha256_json(
            {
                "schema": RUN_MANIFEST_SCHEMA,
                "mode": "shadow",
                "created_at": CREATED_AT,
                "servers": selected_servers,
                "inputs": input_hashes,
                "policy_sha256": policy_sha256,
                "lab_code_sha256": lab_code_sha256,
            }
        )

        proposals: list[ProposalRecord] = []
        for index, row in enumerate(rows):
            is_open = row["stream_id"] in open_streams
            if is_open and block_open_rows:
                state = DecisionState.BLOCKED_ALERT
                candidates: tuple[CandidateEvidence, ...] = ()
                selected_key = ""
                reasons = ("OPEN_SYNC_ALERT",)
                score = 0
                margin = 0
                ai = None
            else:
                method = "CURATED_ALIAS_EXACT" if auto_apply else "STRICT_EXACT"
                candidates = (
                    _candidate(row["stream_id"], 1, method=method),
                    _candidate(row["stream_id"], 2),
                )
                selected_key = "c001"
                score = 900_000
                margin = 200_000
                state = (
                    DecisionState.AUTO_ELIGIBLE
                    if auto_apply
                    else DecisionState.NEEDS_REVIEW
                )
                reasons_set = {
                    "CATALOG_CORROBORATED",
                    "MARKET_ROUTE_EXPLICIT",
                    "MARGIN_GATE_PASSED",
                    "PROGRAMME_GATE_PASSED",
                    "PROVIDER_REVALIDATION_REQUIRED",
                    "PROTECTED_SEMANTICS_COMPATIBLE",
                }
                risks = set(
                    lexical_cohort_risk_codes(
                        row["channel_name"], row["category_name"], "US"
                    )
                )
                reasons_set.update(risks)
                reasons_set.add(
                    "COHORT_CONSERVATIVE" if risks else "COHORT_STANDARD"
                )
                if auto_apply:
                    reasons_set.update(
                        {
                            "CURATED_ALIAS_EXACT",
                            "LANE_TRUSTED_ALIAS",
                            "SHADOW_ONLY_NO_WRITE_AUTHORITY",
                        }
                    )
                else:
                    reasons_set.update(
                        {"LANE_HUMAN_REVIEW", "MULTI_SIGNAL_STRONG_PROPOSAL"}
                    )
                if ai_candidate_key:
                    reasons_set.add(ai_reason_code or "AI_SUPPORTS_LOCAL")
                    ai = AIReviewEvidence(
                        request_sha256=sha256_bytes(
                            f"AI request {row['stream_id']}".encode("utf-8")
                        ),
                        model=AI_MODEL,
                        prompt_version=PROMPT_VERSION,
                        decision="SUGGEST",
                        candidate_key=ai_candidate_key,
                        confidence="HIGH",
                        error_code="",
                        cached=False,
                    )
                else:
                    ai = None
                reasons = tuple(sorted(reasons_set))

            draft = ProposalRecord(
                schema=PROPOSAL_SCHEMA,
                proposal_id="0" * 64,
                run_id=run_id,
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
                server_id=row["server_id"],
                stream_id=row["stream_id"],
                channel_name=row["channel_name"],
                category_name=row["category_name"],
                row_guard_sha256=(
                    "f" * 64 if stale_row_guard and index == 0 else mapping_row_guard(row)
                ),
                provider_identity_sha256=provider_identity_guard(row),
                state=state,
                reason_codes=reasons,
                route_explicit=True,
                market="US",
                selected_candidate_key=selected_key,
                score_ppm=score,
                margin_ppm=margin,
                candidates=candidates,
                source_sha256=input_hashes["EPG_XML"],
                text_catalog_sha256=input_hashes["EPG_TEXT"],
                catalog_fingerprint_sha256=input_hashes["EPG_TEXT_CATALOG"],
                catalog_generation_token="202609180000",
                policy_sha256=policy_sha256,
                lab_code_sha256=lab_code_sha256,
                auto_apply_eligible=auto_apply,
                ai=ai,
            )
            proposals.append(replace(draft, proposal_id=draft.computed_id()))

        counts = Counter(proposal.state.value for proposal in proposals)
        candidate_ids = {
            candidate.epg_id
            for proposal in proposals
            for candidate in proposal.candidates
        }
        ai_proposals = [proposal for proposal in proposals if proposal.ai is not None]
        ai_supported = sum(
            "AI_SUPPORTS_LOCAL" in proposal.reason_codes
            for proposal in ai_proposals
        )
        counts.update(
            {
                "REVIEW_ROWS": len(rows),
                "CATALOG_CANDIDATES": 25_000,
                "PROGRAMME_IDS_CHECKED": len(candidate_ids),
                "AI_ELIGIBLE": len(ai_proposals),
                "AI_REQUESTED": len(ai_proposals),
                "AI_DEFERRED": 0,
                "AI_SUPPORTED": ai_supported,
                "AI_REQUIRES_REVIEW": len(ai_proposals) - ai_supported,
            }
        )
        summary = {
            "schema": "skytv.matching-lab-summary.v1",
            "mode": "shadow",
            "run_id": run_id,
            "generated_at": CREATED_AT,
            "lab_version": LAB_VERSION,
            "policy_id": DEFAULT_POLICY.policy_id,
            "private_details_emitted": False,
            "counts": dict(sorted(counts.items())),
        }
        bundle_dir = self.root / f"{prefix}-bundle"

        def manifest_factory(
            *, proposals_sha256: str, summary_sha256: str
        ) -> dict[str, object]:
            return RunManifest(
                schema=RUN_MANIFEST_SCHEMA,
                run_id=run_id,
                mode="shadow",
                generated_at=CREATED_AT,
                expires_at=EXPIRES_AT,
                input_sha256=input_hashes,
                policy_sha256=policy_sha256,
                lab_code_sha256=lab_code_sha256,
                proposal_count=len(proposals),
                proposals_sha256=proposals_sha256,
                summary_sha256=summary_sha256,
                counts=dict(counts),
            ).public_dict()

        write_private_bundle(
            bundle_dir,
            proposals=[proposal.public_dict() for proposal in proposals],
            summary=summary,
            manifest_factory=manifest_factory,
        )
        return BundleFixture(
            bundle_dir=bundle_dir,
            mappings_csv=mappings_csv,
            alerts_csv=alerts_csv,
            proposals=tuple(proposals),
        )

    def test_generated_bundle_validates_with_current_private_snapshots(self) -> None:
        fixture = self._build_bundle()

        result = validate_bundle(
            fixture.bundle_dir,
            mappings_csv=fixture.mappings_csv,
            alerts_csv=fixture.alerts_csv,
            as_of=VALID_AS_OF,
        )

        self.assertEqual(result.proposal_count, 1)
        self.assertEqual(result.proposals, fixture.proposals)
        self.assertTrue(result.mappings_checked)
        self.assertTrue(result.alerts_checked)
        self.assertEqual(result.counts["AI_REQUESTED"], 0)

    def test_noncanonical_and_unknown_json_fields_fail_closed(self) -> None:
        noncanonical = self._build_bundle()
        manifest_path = noncanonical.bundle_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(_json(manifest_path), indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ContractError, "not canonical JSON"):
            validate_bundle(noncanonical.bundle_dir, as_of=VALID_AS_OF)

        unknown = self._build_bundle()
        summary_path = unknown.bundle_dir / "summary.json"
        manifest_path = unknown.bundle_dir / "manifest.json"
        summary = _json(summary_path)
        summary["unexpected"] = True
        summary_content = _write_json(summary_path, summary)
        manifest = _json(manifest_path)
        manifest["summary_sha256"] = sha256_bytes(summary_content)
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "invalid fields"):
            validate_bundle(unknown.bundle_dir, as_of=VALID_AS_OF)

        missing_ai_config = self._build_bundle()
        manifest_path = missing_ai_config.bundle_dir / "manifest.json"
        manifest = _json(manifest_path)
        del manifest["input_sha256"]["AI_CONFIG"]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "input-hash set"):
            validate_bundle(missing_ai_config.bundle_dir, as_of=VALID_AS_OF)

    def test_file_manifest_and_proposal_tampering_are_detected(self) -> None:
        file_tamper = self._build_bundle()
        summary_path = file_tamper.bundle_dir / "summary.json"
        summary = _json(summary_path)
        summary["policy_id"] = "tampered-policy"
        _write_json(summary_path, summary)
        with self.assertRaisesRegex(ContractError, "summary file hash"):
            validate_bundle(file_tamper.bundle_dir, as_of=VALID_AS_OF)

        manifest_tamper = self._build_bundle()
        manifest_path = manifest_tamper.bundle_dir / "manifest.json"
        manifest = _json(manifest_path)
        manifest["proposals_sha256"] = "0" * 64
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "proposals file hash"):
            validate_bundle(manifest_tamper.bundle_dir, as_of=VALID_AS_OF)

        proposal_tamper = self._build_bundle()
        proposal_path = proposal_tamper.bundle_dir / "proposals.jsonl"
        record = json.loads(proposal_path.read_text(encoding="utf-8"))
        record["identity"]["channel_name"] = "Tampered Channel"
        proposal_content = canonical_json_bytes(record) + b"\n"
        proposal_path.write_bytes(proposal_content)
        manifest_path = proposal_tamper.bundle_dir / "manifest.json"
        manifest = _json(manifest_path)
        manifest["proposals_sha256"] = sha256_bytes(proposal_content)
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "proposal_id"):
            validate_bundle(proposal_tamper.bundle_dir, as_of=VALID_AS_OF)

    def test_duplicate_and_out_of_order_records_are_rejected(self) -> None:
        duplicate = self._build_bundle()
        proposal_path = duplicate.bundle_dir / "proposals.jsonl"
        line = proposal_path.read_bytes()
        proposal_path.write_bytes(line + line)
        with self.assertRaisesRegex(ContractError, "duplicate proposal_id"):
            validate_bundle(duplicate.bundle_dir, as_of=VALID_AS_OF)

        unordered = self._build_bundle(stream_ids=("1", "2"))
        proposal_path = unordered.bundle_dir / "proposals.jsonl"
        lines = proposal_path.read_bytes().splitlines(keepends=True)
        proposal_path.write_bytes(b"".join(reversed(lines)))
        with self.assertRaisesRegex(ContractError, "deterministic stream order"):
            validate_bundle(unordered.bundle_dir, as_of=VALID_AS_OF)

    def test_expired_and_future_bundles_are_rejected(self) -> None:
        fixture = self._build_bundle()
        with self.assertRaisesRegex(ContractError, "expired"):
            validate_bundle(fixture.bundle_dir, as_of=EXPIRES_AT)
        with self.assertRaisesRegex(ContractError, "generated after"):
            validate_bundle(
                fixture.bundle_dir, as_of="2026-09-17T23:59:59Z"
            )

    def test_unsigned_shadow_bundle_cannot_request_auto_apply(self) -> None:
        fixture = self._build_bundle(auto_apply=True)
        with self.assertRaisesRegex(ContractError, "requests auto-apply"):
            validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)

    def test_trusted_alias_score_override_is_exactly_scoped(self) -> None:
        def reseal(
            fixture: BundleFixture,
            mutate: Any,
        ) -> None:
            proposal_path = fixture.bundle_dir / "proposals.jsonl"
            manifest_path = fixture.bundle_dir / "manifest.json"
            record = json.loads(proposal_path.read_text(encoding="utf-8"))
            mutate(record)
            record["proposal_id"] = sha256_json(
                {
                    key: value
                    for key, value in record.items()
                    if key != "proposal_id"
                }
            )
            proposal_content = canonical_json_bytes(record) + b"\n"
            proposal_path.write_bytes(proposal_content)
            manifest = _json(manifest_path)
            manifest["proposals_sha256"] = sha256_bytes(proposal_content)
            _write_json(manifest_path, manifest)

        def make_low_score_alias(record: dict[str, Any]) -> None:
            top_score = DEFAULT_POLICY.strong_proposal_score_ppm - 1
            second_score = DEFAULT_POLICY.human_review_score_ppm
            for candidate, score in zip(
                record["candidates"],
                (top_score, second_score),
                strict=True,
            ):
                candidate["score_ppm"] = score
                candidate["features_ppm"] = {
                    name: score for name in candidate["features_ppm"]
                }
            record["decision"]["score_ppm"] = top_score
            record["decision"]["margin_ppm"] = top_score - second_score
            record["decision"]["auto_apply_eligible"] = False
            reasons = set(record["decision"]["reason_codes"])
            reasons.add("TRUSTED_ALIAS_SCORE_OVERRIDE")
            record["decision"]["reason_codes"] = sorted(reasons)

        valid = self._build_bundle(auto_apply=True)
        reseal(valid, make_low_score_alias)
        result = validate_bundle(valid.bundle_dir, as_of=VALID_AS_OF)
        self.assertEqual(result.proposals[0].state, DecisionState.AUTO_ELIGIBLE)
        self.assertLess(
            result.proposals[0].score_ppm,
            DEFAULT_POLICY.strong_proposal_score_ppm,
        )
        self.assertIn(
            "TRUSTED_ALIAS_SCORE_OVERRIDE",
            result.proposals[0].reason_codes,
        )

        missing_reason = self._build_bundle(auto_apply=True)

        def remove_required_reason(record: dict[str, Any]) -> None:
            make_low_score_alias(record)
            record["decision"]["reason_codes"].remove(
                "TRUSTED_ALIAS_SCORE_OVERRIDE"
            )

        reseal(missing_reason, remove_required_reason)
        with self.assertRaisesRegex(ContractError, "AUTO_ELIGIBLE evidence"):
            validate_bundle(missing_reason.bundle_dir, as_of=VALID_AS_OF)

        strong_alias = self._build_bundle(auto_apply=True)

        def add_override_to_strong_alias(record: dict[str, Any]) -> None:
            record["decision"]["auto_apply_eligible"] = False
            reasons = set(record["decision"]["reason_codes"])
            reasons.add("TRUSTED_ALIAS_SCORE_OVERRIDE")
            record["decision"]["reason_codes"] = sorted(reasons)

        reseal(strong_alias, add_override_to_strong_alias)
        with self.assertRaisesRegex(ContractError, "AUTO_ELIGIBLE evidence"):
            validate_bundle(strong_alias.bundle_dir, as_of=VALID_AS_OF)

        non_alias = self._build_bundle(
            stream_ids=("alpha",),
            auto_apply=True,
        )

        def add_override_to_non_alias(record: dict[str, Any]) -> None:
            record["decision"]["auto_apply_eligible"] = False
            record["candidates"][0]["methods"] = ["STRICT_EXACT"]
            reasons = set(record["decision"]["reason_codes"])
            reasons.difference_update(
                {"CURATED_ALIAS_EXACT", "LANE_TRUSTED_ALIAS"}
            )
            reasons.update(
                {
                    "LANE_STANDARD_STRONG",
                    "STANDARD_LANE_THRESHOLD_PASSED",
                    "TRUSTED_ALIAS_SCORE_OVERRIDE",
                }
            )
            record["decision"]["reason_codes"] = sorted(reasons)

        reseal(non_alias, add_override_to_non_alias)
        with self.assertRaisesRegex(ContractError, "AUTO_ELIGIBLE evidence"):
            validate_bundle(non_alias.bundle_dir, as_of=VALID_AS_OF)

    def test_resealed_auto_state_without_alias_evidence_is_rejected(self) -> None:
        fixture = self._build_bundle()
        proposal_path = fixture.bundle_dir / "proposals.jsonl"
        summary_path = fixture.bundle_dir / "summary.json"
        manifest_path = fixture.bundle_dir / "manifest.json"
        record = json.loads(proposal_path.read_text(encoding="utf-8"))
        record["decision"]["state"] = "AUTO_ELIGIBLE"
        reasons = set(record["decision"]["reason_codes"])
        reasons.discard("MULTI_SIGNAL_STRONG_PROPOSAL")
        reasons.update(
            {"LEARNED_ALIAS_EXACT", "SHADOW_ONLY_NO_WRITE_AUTHORITY"}
        )
        record["decision"]["reason_codes"] = sorted(reasons)
        record["proposal_id"] = sha256_json(
            {key: value for key, value in record.items() if key != "proposal_id"}
        )
        proposal_content = canonical_json_bytes(record) + b"\n"
        proposal_path.write_bytes(proposal_content)

        summary = _json(summary_path)
        summary["counts"].pop("NEEDS_REVIEW")
        summary["counts"]["AUTO_ELIGIBLE"] = 1
        summary_content = _write_json(summary_path, summary)
        manifest = _json(manifest_path)
        manifest["counts"] = dict(summary["counts"])
        manifest["proposals_sha256"] = sha256_bytes(proposal_content)
        manifest["summary_sha256"] = sha256_bytes(summary_content)
        _write_json(manifest_path, manifest)

        with self.assertRaisesRegex(ContractError, "AUTO_ELIGIBLE evidence"):
            validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)

    def test_resealed_programme_pass_without_gate_evidence_is_rejected(self) -> None:
        fixture = self._build_bundle()
        proposal_path = fixture.bundle_dir / "proposals.jsonl"
        manifest_path = fixture.bundle_dir / "manifest.json"
        record = json.loads(proposal_path.read_text(encoding="utf-8"))
        programme = record["candidates"][0]["programme"]
        programme.update(
            {
                "state": "PASS",
                "count": 0,
                "first_start_epoch": None,
                "latest_stop_epoch": None,
                "reason": "",
            }
        )
        record["proposal_id"] = sha256_json(
            {key: value for key, value in record.items() if key != "proposal_id"}
        )
        proposal_content = canonical_json_bytes(record) + b"\n"
        proposal_path.write_bytes(proposal_content)
        manifest = _json(manifest_path)
        manifest["proposals_sha256"] = sha256_bytes(proposal_content)
        _write_json(manifest_path, manifest)

        with self.assertRaisesRegex(ContractError, "passing programme evidence"):
            validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)

    def test_resealed_candidate_score_without_feature_support_is_rejected(self) -> None:
        fixture = self._build_bundle()
        proposal_path = fixture.bundle_dir / "proposals.jsonl"
        manifest_path = fixture.bundle_dir / "manifest.json"
        record = json.loads(proposal_path.read_text(encoding="utf-8"))
        record["candidates"][0]["score_ppm"] = 999_000
        record["decision"]["score_ppm"] = 999_000
        record["decision"]["margin_ppm"] = 299_000
        record["proposal_id"] = sha256_json(
            {key: value for key, value in record.items() if key != "proposal_id"}
        )
        proposal_content = canonical_json_bytes(record) + b"\n"
        proposal_path.write_bytes(proposal_content)
        manifest = _json(manifest_path)
        manifest["proposals_sha256"] = sha256_bytes(proposal_content)
        _write_json(manifest_path, manifest)

        with self.assertRaisesRegex(ContractError, "feature evidence"):
            validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)

    def test_current_mappings_check_requires_complete_review_scope(self) -> None:
        fixture = self._build_bundle(stream_ids=("1", "2"))
        proposal_path = fixture.bundle_dir / "proposals.jsonl"
        summary_path = fixture.bundle_dir / "summary.json"
        manifest_path = fixture.bundle_dir / "manifest.json"
        retained_line = proposal_path.read_bytes().splitlines(keepends=True)[0]
        proposal_path.write_bytes(retained_line)

        summary = _json(summary_path)
        summary["counts"]["REVIEW_ROWS"] = 1
        summary["counts"]["NEEDS_REVIEW"] = 1
        summary["counts"]["PROGRAMME_IDS_CHECKED"] = 2
        summary_content = _write_json(summary_path, summary)
        manifest = _json(manifest_path)
        manifest["proposal_count"] = 1
        manifest["counts"] = dict(summary["counts"])
        manifest["proposals_sha256"] = sha256_bytes(retained_line)
        manifest["summary_sha256"] = sha256_bytes(summary_content)
        _write_json(manifest_path, manifest)

        validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)
        with self.assertRaisesRegex(ContractError, "every scoped REVIEW row"):
            validate_bundle(
                fixture.bundle_dir,
                mappings_csv=fixture.mappings_csv,
                as_of=VALID_AS_OF,
            )

    def test_optional_mappings_guard_rejects_stale_row_preimage(self) -> None:
        fixture = self._build_bundle(stale_row_guard=True)
        # The artifact is internally content-addressed; it becomes unsafe only
        # when bound to the supplied current private Mapping snapshot.
        validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)
        with self.assertRaisesRegex(ContractError, "row guard"):
            validate_bundle(
                fixture.bundle_dir,
                mappings_csv=fixture.mappings_csv,
                as_of=VALID_AS_OF,
            )

    def test_optional_open_alert_check_rejects_unblocked_proposal(self) -> None:
        fixture = self._build_bundle(
            open_alert_streams=("1",), block_open_rows=False
        )
        validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)
        with self.assertRaisesRegex(ContractError, "OPEN-alert decision"):
            validate_bundle(
                fixture.bundle_dir,
                alerts_csv=fixture.alerts_csv,
                as_of=VALID_AS_OF,
            )

    def test_bundle_directory_requires_exactly_three_artifacts(self) -> None:
        fixture = self._build_bundle()
        (fixture.bundle_dir / "debug.txt").write_text("private detail", encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "exactly three artifacts"):
            validate_bundle(fixture.bundle_dir, as_of=VALID_AS_OF)

    def test_terminal_reread_detects_same_name_bundle_replacement(self) -> None:
        fixture = self._build_bundle()
        original = validation._validate_current_mappings

        def replace_after_check(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            (fixture.bundle_dir / "summary.json").write_bytes(
                b'{"replaced":true}\n'
            )

        with mock.patch.object(
            validation,
            "_validate_current_mappings",
            side_effect=replace_after_check,
        ):
            with self.assertRaisesRegex(ContractError, "changed during validation"):
                validate_bundle(
                    fixture.bundle_dir,
                    mappings_csv=fixture.mappings_csv,
                    as_of=VALID_AS_OF,
                )

    def test_ai_evidence_and_aggregate_counts_are_cross_checked(self) -> None:
        valid = self._build_bundle(
            ai_candidate_key="c001", ai_reason_code="AI_SUPPORTS_LOCAL"
        )
        result = validate_bundle(valid.bundle_dir, as_of=VALID_AS_OF)
        self.assertEqual(result.counts["AI_ELIGIBLE"], 1)
        self.assertEqual(result.counts["AI_REQUESTED"], 1)
        self.assertEqual(result.counts["AI_SUPPORTED"], 1)
        self.assertEqual(result.counts["AI_REQUIRES_REVIEW"], 0)

        bad_counts = self._build_bundle(
            ai_candidate_key="c001", ai_reason_code="AI_SUPPORTS_LOCAL"
        )
        summary_path = bad_counts.bundle_dir / "summary.json"
        manifest_path = bad_counts.bundle_dir / "manifest.json"
        summary = _json(summary_path)
        manifest = _json(manifest_path)
        summary["counts"]["AI_SUPPORTED"] = 0
        summary["counts"]["AI_REQUIRES_REVIEW"] = 1
        summary_content = _write_json(summary_path, summary)
        manifest["counts"] = dict(summary["counts"])
        manifest["summary_sha256"] = sha256_bytes(summary_content)
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ContractError, "AI_SUPPORTED"):
            validate_bundle(bad_counts.bundle_dir, as_of=VALID_AS_OF)

        bad_evidence = self._build_bundle(
            ai_candidate_key="c002", ai_reason_code="AI_SUPPORTS_LOCAL"
        )
        with self.assertRaisesRegex(ContractError, "AI reason code"):
            validate_bundle(bad_evidence.bundle_dir, as_of=VALID_AS_OF)


if __name__ == "__main__":
    unittest.main()
