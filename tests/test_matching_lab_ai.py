from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from matching_lab import PROPOSAL_SCHEMA
from matching_lab import ai
from matching_lab.models import (
    AIReviewEvidence,
    CandidateEvidence,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
)


def _candidate(key: str, *, epg_id: str, display_name: str) -> CandidateEvidence:
    return CandidateEvidence(
        candidate_key=key,
        epg_id=epg_id,
        display_name=display_name,
        feed="US1",
        region="US",
        score_ppm=900_000 if key == "c001" else 780_000,
        methods=("TOKEN_RETRIEVAL",),
        conflicts=(),
        features_ppm=(("TOKEN_SCORE", 900_000),),
        semantics=ProtectedSemantics(market="US"),
        programme_state=ProgrammeState.PASS,
        programme_count=3,
    )


def _proposal(
    *,
    state: DecisionState = DecisionState.AUTO_ELIGIBLE,
    auto_apply_eligible: bool | None = None,
) -> ProposalRecord:
    if auto_apply_eligible is None:
        auto_apply_eligible = state is DecisionState.AUTO_ELIGIBLE
    draft = ProposalRecord(
        schema=PROPOSAL_SCHEMA,
        proposal_id="0" * 64,
        run_id="1" * 64,
        created_at="2026-09-18T00:00:00Z",
        expires_at="2026-09-18T06:00:00Z",
        server_id="server_1",
        stream_id="42",
        channel_name="Example News",
        category_name="US | News",
        row_guard_sha256="2" * 64,
        provider_identity_sha256="3" * 64,
        state=state,
        reason_codes=("LOCAL_POLICY",),
        route_explicit=True,
        market="US",
        selected_candidate_key="c001",
        score_ppm=900_000,
        margin_ppm=120_000,
        candidates=(
            _candidate(
                "c001",
                epg_id="Real.News.HD.us",
                display_name="Example News",
            ),
            _candidate(
                "c002",
                epg_id="Other.News.HD.us",
                display_name="Other News",
            ),
        ),
        source_sha256="4" * 64,
        text_catalog_sha256="5" * 64,
        catalog_fingerprint_sha256="6" * 64,
        catalog_generation_token="20260918",
        policy_sha256="7" * 64,
        lab_code_sha256="8" * 64,
        auto_apply_eligible=auto_apply_eligible,
    )
    result = replace(draft, proposal_id=draft.computed_id())
    result.verify_id()
    return result


def _gemini_envelope(
    *,
    decision: str,
    candidate_key: str = "",
    confidence: str = "NONE",
) -> bytes:
    generated = {
        "results": [
            {
                "review_id": "r000001",
                "decision": decision,
                "candidate_key": candidate_key,
                "confidence": confidence,
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
            "promptTokenCount": 11,
            "candidatesTokenCount": 3,
            "totalTokenCount": 14,
        },
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


class _StaticTransport:
    def __init__(
        self,
        *,
        decision: str,
        candidate_key: str = "",
        confidence: str = "NONE",
    ) -> None:
        self.content = _gemini_envelope(
            decision=decision,
            candidate_key=candidate_key,
            confidence=confidence,
        )
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append((url, dict(kwargs)))
        return SimpleNamespace(status_code=200, content=self.content)


class _NoNetworkTransport:
    def post(self, url: str, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("The replay cache must prevent a network call.")


def _wire_rows(transport: _StaticTransport) -> list[dict[str, object]]:
    body = json.loads(transport.calls[0][1]["data"].decode("utf-8"))
    prompt = body["contents"][0]["parts"][0]["text"]
    marker = "required structured result:\n"
    return json.loads(prompt.split(marker, 1)[1])["requests"]


class MatchingLabAIReviewTests(unittest.TestCase):
    def test_wire_uses_only_opaque_keys_and_high_exact_supports_local(self) -> None:
        proposal = _proposal()
        request = ai.request_from_proposal(proposal)
        transport = _StaticTransport(
            decision="SUGGEST",
            candidate_key="c001",
            confidence="HIGH",
        )

        batch = ai.review_advisories(
            (request,), api_key="test-key", transport=transport
        )

        self.assertEqual(len(transport.calls), 1)
        wire_body = transport.calls[0][1]["data"]
        self.assertNotIn(b"Real.News.HD.us", wire_body)
        self.assertNotIn(b"Other.News.HD.us", wire_body)
        self.assertNotIn(proposal.proposal_id.encode("ascii"), wire_body)
        rows = _wire_rows(transport)
        self.assertEqual(
            [candidate["candidate_key"] for candidate in rows[0]["candidates"]],
            ["c001", "c002"],
        )
        self.assertEqual(
            [candidate["epg_id"] for candidate in rows[0]["candidates"]],
            ["c001", "c002"],
        )
        outcome = batch.outcomes[0]
        self.assertEqual(outcome.disposition, ai.AdvisoryDisposition.SUPPORTS_LOCAL)
        self.assertFalse(outcome.requires_review)
        self.assertEqual(outcome.suggested_candidate_key, "c001")
        self.assertEqual(
            (batch.prompt_tokens, batch.candidate_tokens, batch.total_tokens),
            (11, 3, 14),
        )

        attached = ai.attach_advisory(proposal, outcome)
        self.assertEqual(attached.state, DecisionState.AUTO_ELIGIBLE)
        self.assertFalse(attached.auto_apply_eligible)
        self.assertEqual(attached.selected_candidate_key, "c001")

    def test_literal_epg_ids_are_redacted_from_all_ai_text_fields(self) -> None:
        original = _proposal()
        real_id = original.candidates[0].epg_id
        candidates = (
            replace(original.candidates[0], display_name=f"Watch {real_id}"),
            original.candidates[1],
        )
        draft = replace(
            original,
            proposal_id="0" * 64,
            channel_name=f"Provider {real_id}",
            category_name=f"Category {real_id}",
            candidates=candidates,
        )
        proposal = replace(draft, proposal_id=draft.computed_id())
        request = ai.request_from_proposal(proposal)
        payload = json.dumps(ai.canonical_request_payload(request), sort_keys=True)

        self.assertNotIn(real_id.casefold(), payload.casefold())
        self.assertIn("[epg-id]", payload)

    def test_non_high_abstain_and_disagreement_demote_auto_eligible(self) -> None:
        cases = (
            (
                "medium",
                "SUGGEST",
                "c001",
                "MEDIUM",
                ai.AdvisoryDisposition.LOW_CONFIDENCE,
            ),
            (
                "low",
                "SUGGEST",
                "c001",
                "LOW",
                ai.AdvisoryDisposition.LOW_CONFIDENCE,
            ),
            (
                "abstain",
                "ABSTAIN",
                "",
                "NONE",
                ai.AdvisoryDisposition.ABSTAINED,
            ),
            (
                "disagreement",
                "SUGGEST",
                "c002",
                "HIGH",
                ai.AdvisoryDisposition.DISAGREES,
            ),
        )
        for label, decision, key, confidence, expected in cases:
            with self.subTest(label=label):
                proposal = _proposal()
                transport = _StaticTransport(
                    decision=decision,
                    candidate_key=key,
                    confidence=confidence,
                )
                outcome = ai.review_advisories(
                    (ai.request_from_proposal(proposal),),
                    api_key="test-key",
                    transport=transport,
                ).outcomes[0]

                self.assertEqual(outcome.disposition, expected)
                self.assertTrue(outcome.requires_review)
                attached = ai.attach_advisory(proposal, outcome)
                self.assertEqual(attached.state, DecisionState.NEEDS_REVIEW)
                self.assertFalse(attached.auto_apply_eligible)
                self.assertEqual(attached.selected_candidate_key, "c001")
                self.assertIn(expected.value, attached.reason_codes)

    def test_out_of_set_suggestion_fails_closed_without_raw_output(self) -> None:
        proposal = _proposal()
        transport = _StaticTransport(
            decision="SUGGEST",
            candidate_key="c999",
            confidence="HIGH",
        )

        outcome = ai.review_advisories(
            (ai.request_from_proposal(proposal),),
            api_key="test-key",
            transport=transport,
        ).outcomes[0]

        self.assertEqual(outcome.disposition, ai.AdvisoryDisposition.ERROR)
        self.assertTrue(outcome.requires_review)
        self.assertEqual(outcome.suggested_candidate_key, "")
        self.assertEqual(outcome.evidence.decision, "ERROR")
        self.assertEqual(outcome.evidence.error_code, "INVALID_RESPONSE")
        attached = ai.attach_advisory(proposal, outcome)
        self.assertEqual(attached.state, DecisionState.NEEDS_REVIEW)
        self.assertFalse(attached.auto_apply_eligible)
        self.assertEqual(attached.selected_candidate_key, "c001")

    def test_cache_replay_avoids_network(self) -> None:
        proposal = _proposal()
        request = ai.request_from_proposal(proposal)
        transport = _StaticTransport(
            decision="SUGGEST",
            candidate_key="c001",
            confidence="HIGH",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ai-replay.sqlite3"
            first = ai.review_advisories(
                (request,),
                api_key="test-key",
                cache_path=cache_path,
                transport=transport,
            )
            replay = ai.review_advisories(
                (request,),
                api_key="",
                cache_path=cache_path,
                transport=_NoNetworkTransport(),
            )

        self.assertEqual(first.cache_hits, 0)
        self.assertEqual(replay.cache_hits, 1)
        self.assertEqual(replay.batches_attempted, 0)
        self.assertTrue(replay.outcomes[0].evidence.cached)
        self.assertEqual(
            replay.outcomes[0].disposition,
            ai.AdvisoryDisposition.SUPPORTS_LOCAL,
        )
        self.assertEqual(
            ai.attach_advisory(proposal, first.outcomes[0]).public_dict(),
            ai.attach_advisory(proposal, replay.outcomes[0]).public_dict(),
        )

    def test_cache_namespace_is_durable_and_distinguishes_explicit_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.sqlite3"
            second_path = Path(directory) / "second.sqlite3"
            first = ai.replay_cache_namespace_sha256(first_path)
            replay = ai.replay_cache_namespace_sha256(first_path)
            explicit_retry = ai.replay_cache_namespace_sha256(second_path)

        self.assertEqual(first, replay)
        self.assertNotEqual(first, explicit_retry)

    def test_fixed_error_is_cached_until_operator_explicitly_retries(self) -> None:
        request = ai.request_from_proposal(_proposal())
        transport = _StaticTransport(
            decision="SUGGEST",
            candidate_key="c999",
            confidence="HIGH",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ai-replay.sqlite3"
            first = ai.review_advisories(
                (request,),
                api_key="test-key",
                cache_path=cache_path,
                transport=transport,
            )
            replay = ai.review_advisories(
                (request,),
                api_key="",
                cache_path=cache_path,
                transport=_NoNetworkTransport(),
            )
        self.assertEqual(first.outcomes[0].evidence.decision, "ERROR")
        self.assertEqual(replay.cache_hits, 1)
        self.assertEqual(replay.outcomes[0].evidence.error_code, "INVALID_RESPONSE")

    def test_cache_rejects_deleted_rows_before_another_model_call(self) -> None:
        request = ai.request_from_proposal(_proposal())
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ai-replay.sqlite3"
            ai.review_advisories(
                (request,),
                api_key="test-key",
                cache_path=cache_path,
                transport=_StaticTransport(
                    decision="SUGGEST",
                    candidate_key="c001",
                    confidence="HIGH",
                ),
            )
            with sqlite3.connect(cache_path) as connection:
                connection.execute("DELETE FROM advisory_cache")
                connection.commit()

            with self.assertRaisesRegex(
                ai.AdvisoryCacheError, "checkpoint does not match"
            ):
                ai.review_advisories(
                    (request,),
                    api_key="test-key",
                    cache_path=cache_path,
                    transport=_NoNetworkTransport(),
                )

    def test_cache_rejects_conflicting_evidence_for_same_request(self) -> None:
        request = ai.request_from_proposal(_proposal())
        digest = ai.request_sha256(request)
        first = AIReviewEvidence(
            request_sha256=digest,
            model=ai.DEFAULT_MODEL,
            prompt_version=ai.PROMPT_VERSION,
            decision="SUGGEST",
            candidate_key="c001",
            confidence="HIGH",
        )
        conflicting = replace(first, candidate_key="c002")

        with tempfile.TemporaryDirectory() as directory:
            with ai._ReplayCache(Path(directory) / "ai-replay.sqlite3") as cache:
                cache.put(first)
                with self.assertRaisesRegex(
                    ai.AdvisoryCacheError, "conflicting evidence"
                ):
                    cache.put(conflicting)

    def test_attach_support_never_promotes_existing_review_state(self) -> None:
        proposal = _proposal(
            state=DecisionState.NEEDS_REVIEW,
            auto_apply_eligible=False,
        )
        evidence = AIReviewEvidence(
            request_sha256="9" * 64,
            model=ai.DEFAULT_MODEL,
            prompt_version=ai.PROMPT_VERSION,
            decision="SUGGEST",
            candidate_key="c001",
            confidence="HIGH",
        )
        outcome = ai.AdvisoryOutcome(
            review_id=proposal.proposal_id,
            disposition=ai.AdvisoryDisposition.SUPPORTS_LOCAL,
            evidence=evidence,
            suggested_candidate_key="c001",
            requires_review=False,
        )

        attached = ai.attach_advisory(proposal, outcome)

        self.assertEqual(attached.state, DecisionState.NEEDS_REVIEW)
        self.assertFalse(attached.auto_apply_eligible)
        self.assertEqual(attached.selected_candidate_key, proposal.selected_candidate_key)
        self.assertNotEqual(attached.proposal_id, proposal.proposal_id)
        attached.verify_id()


if __name__ == "__main__":
    unittest.main()
