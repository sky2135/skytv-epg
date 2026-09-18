from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from matching_lab.models import (
    AIReviewEvidence,
    CandidateEvidence,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    RunManifest,
)
from matching_lab.state import (
    GENESIS_SHA256,
    LedgerCapacityError,
    LedgerError,
    LedgerIntegrityError,
    ObservationLedger,
    ValidationObservation,
)


RUN_ID = "1" * 64
POLICY_SHA256 = "2" * 64
CODE_SHA256 = "3" * 64
CREATED_AT = "2026-09-18T01:00:00Z"
EXPIRES_AT = "2026-09-18T07:00:00Z"

PRIVATE_CHANNEL = "PRIVATE_CHANNEL_NAME_MUST_NOT_BE_STORED"
PRIVATE_CATEGORY = "PRIVATE_CATEGORY_NAME_MUST_NOT_BE_STORED"
PRIVATE_DISPLAY = "PRIVATE_CANDIDATE_DISPLAY_MUST_NOT_BE_STORED"
PRIVATE_AI_PROSE = "PRIVATE_AI_PROSE_MUST_NOT_BE_STORED"
PRIVATE_AI_MODEL = "private-ai-model-must-not-be-stored"


def manifest(*, proposal_count: int = 1) -> RunManifest:
    return RunManifest(
        schema="skytv.smart-match-manifest.v1",
        run_id=RUN_ID,
        mode="SHADOW",
        generated_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        input_sha256={"MAPPINGS": "4" * 64, "SOURCE": "5" * 64},
        policy_sha256=POLICY_SHA256,
        lab_code_sha256=CODE_SHA256,
        proposal_count=proposal_count,
        proposals_sha256="6" * 64,
        summary_sha256="7" * 64,
        counts={"NEEDS_REVIEW": proposal_count},
    )


def proposal() -> ProposalRecord:
    candidate = CandidateEvidence(
        candidate_key="candidate:one",
        epg_id="Example.Channel.us2",
        display_name=PRIVATE_DISPLAY,
        feed="ALL_SOURCES1",
        region="US2",
        score_ppm=900_000,
        methods=("TOKEN", "NGRAM"),
        conflicts=(),
        features_ppm=(("TOKEN", 950_000), ("NGRAM", 850_000)),
        semantics=ProtectedSemantics(market="US"),
        programme_state=ProgrammeState.PASS,
        programme_count=2,
        programme_first_start_epoch=1_795_000_000,
        programme_latest_stop_epoch=1_795_010_000,
        programme_reason=PRIVATE_AI_PROSE,
    )
    draft = ProposalRecord(
        schema="skytv.smart-match-proposal.v1",
        proposal_id="0" * 64,
        run_id=RUN_ID,
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
        server_id="server_1",
        stream_id="stream-42",
        channel_name=PRIVATE_CHANNEL,
        category_name=PRIVATE_CATEGORY,
        row_guard_sha256="8" * 64,
        provider_identity_sha256="9" * 64,
        state=DecisionState.NEEDS_REVIEW,
        reason_codes=("MULTI_SIGNAL_STRONG_PROPOSAL",),
        route_explicit=True,
        market="US",
        selected_candidate_key=candidate.candidate_key,
        score_ppm=candidate.score_ppm,
        margin_ppm=250_000,
        candidates=(candidate,),
        source_sha256="a" * 64,
        text_catalog_sha256="b" * 64,
        catalog_fingerprint_sha256="c" * 64,
        catalog_generation_token="20260918T010000Z",
        policy_sha256=POLICY_SHA256,
        lab_code_sha256=CODE_SHA256,
        auto_apply_eligible=False,
        ai=AIReviewEvidence(
            request_sha256="d" * 64,
            model=PRIVATE_AI_MODEL,
            prompt_version="private-prompt-version",
            decision="NEEDS_REVIEW",
            candidate_key=candidate.candidate_key,
            confidence="MEDIUM",
        ),
    )
    result = replace(draft, proposal_id=draft.computed_id())
    result.verify_id()
    return result


def validation(
    proposal_id: str,
    *,
    observed_at: str = "2026-09-18T02:00:00Z",
    outcome: str = "REJECTED",
) -> ValidationObservation:
    return ValidationObservation(
        proposal_id=proposal_id,
        observed_at=observed_at,
        outcome=outcome,
        reason_codes=("STALE_ROW",),
        validator_version="proposal-validator-v1",
        current_row_guard_sha256="e" * 64,
        current_provider_identity_sha256="f" * 64,
        context_sha256="0" * 64,
    )


class ObservationLedgerTests(unittest.TestCase):
    def test_idempotent_record_and_reopen(self) -> None:
        item = proposal()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path) as ledger:
                run_event = ledger.record_run(manifest())
                proposal_event = ledger.record_proposal(item)
                self.assertEqual(ledger.record_run(manifest()), run_event)
                self.assertEqual(ledger.record_proposal(item), proposal_event)
                self.assertEqual(ledger.verify_chain().events, 2)

            with ObservationLedger(path) as reopened:
                self.assertEqual(reopened.record_run(manifest()), run_event)
                self.assertEqual(reopened.record_proposal(item), proposal_event)
                stats = reopened.verify_chain()
                self.assertEqual(stats.runs, 1)
                self.assertEqual(stats.proposals, 1)
                self.assertEqual(stats.validations, 0)
                self.assertEqual(stats.events, 2)
                self.assertNotEqual(stats.head_sha256, GENESIS_SHA256)

    def test_proposal_requires_recorded_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path) as ledger:
                with self.assertRaisesRegex(
                    LedgerIntegrityError, "run must be recorded first"
                ):
                    ledger.record_proposal(proposal())
                self.assertEqual(ledger.stats().events, 0)
                self.assertEqual(ledger.stats().proposals, 0)

    def test_validation_events_are_idempotent_and_hash_chained(self) -> None:
        item = proposal()
        first = validation(item.proposal_id)
        second = validation(
            item.proposal_id,
            observed_at="2026-09-18T03:00:00Z",
            outcome="ACCEPTED",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path) as ledger:
                ledger.record_run(manifest())
                ledger.record_proposal(item)
                first_event = ledger.record_validation(first)
                self.assertEqual(ledger.record_validation(first), first_event)
                second_event = ledger.record_validation(second)
                self.assertNotEqual(first_event, second_event)
                self.assertEqual(ledger.latest_validation(item.proposal_id), second)
                stats = ledger.verify_chain()
                self.assertEqual(stats.validations, 2)
                self.assertEqual(stats.events, 4)

    def test_private_proposal_and_ai_text_never_reaches_sqlite_files(self) -> None:
        item = proposal()
        private_markers = (
            PRIVATE_CHANNEL,
            PRIVATE_CATEGORY,
            PRIVATE_DISPLAY,
            PRIVATE_AI_PROSE,
            PRIVATE_AI_MODEL,
            "private-prompt-version",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path) as ledger:
                ledger.record_run(manifest())
                ledger.record_proposal(item)
                ledger.record_validation(validation(item.proposal_id))
                ledger.verify_chain()
                ledger._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                files = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
                storage = b"".join(
                    candidate.read_bytes() for candidate in files if candidate.exists()
                )
                for marker in private_markers:
                    self.assertNotIn(marker.encode("utf-8"), storage)

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_symlink_database_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.sqlite3"
            target.touch()
            link = root / "ledger-link.sqlite3"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symbolic link creation is unavailable: {exc}")
            with self.assertRaisesRegex(LedgerError, "non-symlink"):
                ObservationLedger(link)

    def test_tail_truncation_is_detected_by_durable_checkpoint(self) -> None:
        item = proposal()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path) as ledger:
                ledger.record_run(manifest())
                ledger.record_proposal(item)
                self.assertEqual(ledger.verify_chain().events, 2)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "DELETE FROM events WHERE sequence = (SELECT MAX(sequence) FROM events)"
                )
                connection.commit()
            finally:
                connection.close()

            with ObservationLedger(path) as reopened:
                with self.assertRaisesRegex(
                    LedgerIntegrityError, "event-count checkpoint"
                ):
                    reopened.verify_chain()

    def test_event_capacity_fails_closed_and_rolls_back_snapshot(self) -> None:
        item = proposal()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "matching-lab.sqlite3"
            with ObservationLedger(path, max_events=1) as ledger:
                ledger.record_run(manifest())
                with self.assertRaisesRegex(LedgerCapacityError, "event limit"):
                    ledger.record_proposal(item)
                stats = ledger.verify_chain()
                self.assertEqual(stats.runs, 1)
                self.assertEqual(stats.proposals, 0)
                self.assertEqual(stats.events, 1)


if __name__ == "__main__":
    unittest.main()
