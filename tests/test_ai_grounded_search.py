from __future__ import annotations

import base64
import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ai_grounded_search as ai  # noqa: E402


def candidate(
    key: str = "c001",
    *,
    epg_id: str = "MOTORSPORT.za",
    display_name: str = "SuperSport Motorsport",
    region: str = "ZA",
    feed: str = "ZA1",
) -> ai.ReviewCandidate:
    return ai.ReviewCandidate(key, epg_id, display_name, region, feed)


def review(
    review_id: str = "server_1:124000",
    *,
    channel_name: str = "DSTV : Super Motorsport (FHD).",
    candidates: tuple[ai.ReviewCandidate, ...] | None = None,
    category: str = "SPORTS | F1 MOTOGP",
    market: str = "ZA",
    discriminating_tokens: tuple[str, ...] = (),
) -> ai.ReviewRequest:
    return ai.ReviewRequest(
        review_id=review_id,
        channel_name=channel_name,
        category=category,
        market=market,
        candidates=(candidate(),) if candidates is None else candidates,
        discriminating_tokens=discriminating_tokens,
    )


def response(content: bytes, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, content=content)


def request_body(call: mock._Call) -> dict[str, object]:
    return json.loads(call.kwargs["data"].decode("utf-8"))


def request_rows(call: mock._Call) -> list[dict[str, object]]:
    body = request_body(call)
    text = body["contents"][0]["parts"][0]["text"]
    return json.loads(text.split("required grounded review input:\n", 1)[1])["requests"]


def abstain_generated(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "results": [
            {
                "review_id": row["review_id"],
                "decision": "ABSTAIN",
                "candidate_key": "",
                "confidence": "NONE",
                "identity_claim": "",
            }
            for row in rows
        ]
    }


def default_sources() -> list[dict[str, str]]:
    return [
        {
            "uri": "https://www.dstv.com/channels/motorsport#schedule",
            "title": "DStv channel guide",
        },
        {
            "uri": "https://supersport.com/motorsport/",
            "title": "SuperSport Motorsport",
        },
    ]


def grounding_for_claim(
    claim: str,
    *,
    sources: list[dict[str, str]] | None = None,
    queries: list[str] | None = None,
    supports: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_items = sources if sources is not None else default_sources()
    return {
        "webSearchQueries": (
            ["DSTV SuperSport Motorsport channel"] if queries is None else queries
        ),
        "groundingChunks": [{"web": item} for item in source_items],
        "groundingSupports": supports
        if supports is not None
        else [
            {
                "segment": {"text": claim},
                "groundingChunkIndices": list(range(len(source_items))),
            }
        ],
    }


def envelope(
    generated: object | str,
    *,
    grounding: dict[str, object] | None = None,
    prompt_tokens: int = 0,
    candidate_tokens: int = 0,
    total_tokens: int = 0,
    finish_reason: str = "STOP",
) -> bytes:
    text = generated if isinstance(generated, str) else json.dumps(
        generated, separators=(",", ":")
    )
    model_candidate: dict[str, object] = {
        "finishReason": finish_reason,
        "content": {"parts": [{"text": text}]},
    }
    if grounding is not None:
        model_candidate["groundingMetadata"] = grounding
    return json.dumps(
        {
            "candidates": [model_candidate],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": candidate_tokens,
                "totalTokenCount": total_tokens,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def suggestion_from_call(
    call: mock._Call,
    *,
    key: str = "c001",
    confidence: str = "HIGH",
    claim_override: str | None = None,
    sources: list[dict[str, str]] | None = None,
    queries: list[str] | None = None,
    supports: list[dict[str, object]] | None = None,
) -> bytes:
    rows = request_rows(call)
    selected = next(
        item for item in rows[0]["candidates"] if item["candidate_key"] == key
    )
    claim = claim_override if claim_override is not None else selected[
        "required_identity_claim"
    ]
    generated = {
        "results": [
            {
                "review_id": rows[0]["review_id"],
                "decision": "SUGGEST",
                "candidate_key": key,
                "confidence": confidence,
                "identity_claim": claim,
            }
        ]
    }
    return envelope(
        generated,
        grounding=grounding_for_claim(
            claim,
            sources=sources,
            queries=queries,
            supports=supports,
        ),
        prompt_tokens=101,
        candidate_tokens=17,
        total_tokens=118,
    )


def abstain_from_call(call: mock._Call) -> bytes:
    return envelope(abstain_generated(request_rows(call)))


class GroundedSearchHappyPathTests(unittest.TestCase):
    def test_dataclasses_are_immutable(self) -> None:
        item = candidate()
        with self.assertRaises(FrozenInstanceError):
            item.epg_id = "changed"  # type: ignore[misc]

    def test_grounded_success_exposes_exact_audit_evidence(self) -> None:
        transport = mock.Mock()

        def post(*_args, **_kwargs):
            return response(suggestion_from_call(transport.post.call_args))

        transport.post.side_effect = post
        outcome = ai.review_grounded_channels(
            (review(category="Sports https://panel.invalid admin@example.com"),),
            api_key="private-api-key",
            transport=transport,
            retry_backoff_seconds=0,
        )

        self.assertEqual(len(outcome.results), 1)
        result = outcome.results[0]
        self.assertEqual(result.review_id, "server_1:124000")
        self.assertIs(result.decision, ai.ReviewDecision.SUGGEST)
        self.assertEqual(result.candidate_key, "c001")
        self.assertEqual(result.selected_epg_id, "MOTORSPORT.za")
        self.assertIn("DSTV", result.identity_claim or "")
        self.assertIn("Super", result.identity_claim or "")
        self.assertIn("Motorsport", result.identity_claim or "")
        self.assertIn("MOTORSPORT.za", result.identity_claim or "")
        self.assertEqual(result.query, "DSTV SuperSport Motorsport channel")
        self.assertEqual(
            result.source_urls,
            (
                "https://dstv.com/channels/motorsport",
                "https://supersport.com/motorsport/",
            ),
        )
        self.assertEqual(
            result.source_authorities,
            ("dstv.com", "supersport.com"),
        )
        self.assertEqual(
            (outcome.prompt_tokens, outcome.candidate_tokens, outcome.total_tokens),
            (101, 17, 118),
        )
        self.assertEqual(
            (outcome.batches_attempted, outcome.batches_succeeded, outcome.http_attempts),
            (1, 1, 1),
        )

        call = transport.post.call_args
        self.assertEqual(
            call.args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash-lite:generateContent",
        )
        self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], "private-api-key")
        self.assertNotIn(b"private-api-key", call.kwargs["data"])
        self.assertNotIn(b"server_1:124000", call.kwargs["data"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["timeout"], 25.0)
        body = request_body(call)
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertEqual(
            body["generationConfig"]["responseMimeType"], "application/json"
        )
        self.assertIn("responseJsonSchema", body["generationConfig"])
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 8192)
        rows = request_rows(call)
        self.assertIn("[REDACTED_URL]", rows[0]["category"])
        self.assertIn("[REDACTED_EMAIL]", rows[0]["category"])
        self.assertEqual(rows[0]["review_id"], "r000001")
        self.assertEqual(rows[0]["candidates"][0]["candidate_key"], "c001")

    def test_compatibility_entry_point_is_same_function(self) -> None:
        self.assertIs(ai.review_flagged_channels, ai.review_grounded_channels)

    def test_singleton_candidate_is_valid(self) -> None:
        transport = mock.Mock()
        transport.post.side_effect = lambda *_a, **_k: response(
            abstain_from_call(transport.post.call_args)
        )
        result = ai.review_grounded_channels(
            (review(),), api_key="key", transport=transport, retry_backoff_seconds=0
        )
        self.assertIs(result.results[0].decision, ai.ReviewDecision.ABSTAIN)

    def test_nine_rows_are_batched_four_four_one_and_restored_in_order(self) -> None:
        transport = mock.Mock()

        def post(*_args, **_kwargs):
            return response(abstain_from_call(transport.post.call_args))

        transport.post.side_effect = post
        requests = tuple(review(f"private-{number}") for number in range(9))
        outcome = ai.review_grounded_channels(
            requests,
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        )
        self.assertEqual([item.review_id for item in outcome.results], [
            f"private-{number}" for number in range(9)
        ])
        self.assertEqual([len(request_rows(call)) for call in transport.post.call_args_list], [4, 4, 1])
        self.assertEqual((outcome.batches_attempted, outcome.batches_succeeded), (3, 3))


class GroundedSearchInputSafetyTests(unittest.TestCase):
    def test_more_than_fifty_rows_are_rejected_before_network(self) -> None:
        transport = mock.Mock()
        with self.assertRaisesRegex(ValueError, "At most 50"):
            ai.review_grounded_channels(
                tuple(review(f"r-{number}") for number in range(51)),
                api_key="key",
                transport=transport,
            )
        transport.post.assert_not_called()

    def test_candidate_count_is_one_through_eight(self) -> None:
        for invalid in ((), tuple(candidate(f"c{number:03d}") for number in range(1, 10))):
            with self.subTest(count=len(invalid)):
                with self.assertRaisesRegex(ValueError, "1-8 candidates"):
                    ai.review_grounded_channels(
                        (review(candidates=invalid),), api_key="key", transport=mock.Mock()
                    )

    def test_candidate_key_must_be_opaque_cnnn(self) -> None:
        for key in ("MOTORSPORT.za", "c1", "candidate_1", "c0001", "C001"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "opaque cNNN"):
                    ai.review_grounded_channels(
                        (review(candidates=(candidate(key),)),),
                        api_key="key",
                        transport=mock.Mock(),
                    )

    def test_raw_secret_assignment_is_rejected_without_network(self) -> None:
        transport = mock.Mock()
        outcome = ai.review_grounded_channels(
            (review(category="Sports api_key=AIzaEXAMPLEPRIVATEVALUE123456"),),
            api_key="key",
            transport=transport,
        )
        self.assertEqual(outcome.results[0].error_code, "UNSAFE_REQUEST")
        transport.post.assert_not_called()

    def test_encoded_configured_secret_is_rejected_without_network(self) -> None:
        secret = "correct-horse-battery-staple"
        encoded = base64.b64encode(secret.encode()).decode()
        transport = mock.Mock()
        outcome = ai.review_grounded_channels(
            (review(category=f"Sports {encoded}"),),
            api_key="key",
            sensitive_values=(secret,),
            transport=transport,
        )
        self.assertEqual(outcome.results[0].error_code, "UNSAFE_REQUEST")
        transport.post.assert_not_called()

    def test_reflected_api_key_is_rejected_without_network(self) -> None:
        api_key = "AIzaReflectedPrivateKey1234567890"
        transport = mock.Mock()
        outcome = ai.review_grounded_channels(
            (review(category=f"Sports {api_key}"),),
            api_key=api_key,
            transport=transport,
        )
        self.assertEqual(outcome.results[0].error_code, "UNSAFE_REQUEST")
        transport.post.assert_not_called()

    def test_unsafe_row_does_not_block_safe_row_and_order_is_preserved(self) -> None:
        transport = mock.Mock()
        transport.post.side_effect = lambda *_a, **_k: response(
            abstain_from_call(transport.post.call_args)
        )
        outcome = ai.review_grounded_channels(
            (
                review("unsafe", category="password=hunter2"),
                review("safe"),
            ),
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        )
        self.assertEqual([item.review_id for item in outcome.results], ["unsafe", "safe"])
        self.assertEqual(outcome.results[0].error_code, "UNSAFE_REQUEST")
        self.assertIs(outcome.results[1].decision, ai.ReviewDecision.ABSTAIN)
        transport.post.assert_called_once()

    def test_explicit_discriminating_tokens_must_be_in_channel(self) -> None:
        with self.assertRaisesRegex(ValueError, "must occur"):
            ai.review_grounded_channels(
                (review(discriminating_tokens=("football",)),),
                api_key="key",
                transport=mock.Mock(),
            )

    def test_missing_key_and_invalid_timeout_fail_closed(self) -> None:
        missing = ai.review_grounded_channels((review(),), api_key="")
        self.assertEqual(missing.results[0].error_code, "MISSING_API_KEY")
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            ai.review_grounded_channels((review(),), api_key="key", timeout_seconds=0)


class GroundedSearchTransportTests(unittest.TestCase):
    def test_retries_only_429_and_5xx_then_succeeds(self) -> None:
        transport = mock.Mock()
        attempt = 0

        def post(*_args, **_kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return response(b"{}", 429)
            if attempt == 2:
                return response(b"{}", 503)
            return response(abstain_from_call(transport.post.call_args))

        transport.post.side_effect = post
        with mock.patch.object(ai.time, "sleep") as sleep:
            outcome = ai.review_grounded_channels(
                (review(),), api_key="key", transport=transport
            )
        self.assertEqual(transport.post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])
        self.assertEqual(outcome.http_attempts, 3)
        self.assertIs(outcome.results[0].decision, ai.ReviewDecision.ABSTAIN)

    def test_400_redirect_and_transport_exception_are_not_retried(self) -> None:
        for side_effect, code in (
            (response(b"{}", 400), "HTTP_ERROR"),
            (response(b"{}", 302), "HTTP_ERROR"),
            (OSError("offline"), "TRANSPORT_ERROR"),
        ):
            with self.subTest(side_effect=side_effect):
                transport = mock.Mock()
                if isinstance(side_effect, BaseException):
                    transport.post.side_effect = side_effect
                else:
                    transport.post.return_value = side_effect
                outcome = ai.review_grounded_channels(
                    (review(),),
                    api_key="key",
                    transport=transport,
                    retry_backoff_seconds=0,
                )
                transport.post.assert_called_once()
                self.assertEqual(outcome.results[0].error_code, code)

    def test_retry_exhaustion_is_bounded(self) -> None:
        transport = mock.Mock()
        transport.post.return_value = response(b"{}", 429)
        outcome = ai.review_grounded_channels(
            (review(),),
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        )
        self.assertEqual(transport.post.call_count, 3)
        self.assertEqual(outcome.http_attempts, 3)
        self.assertEqual(outcome.results[0].error_code, "RATE_LIMITED")

    def test_oversized_response_fails_closed_without_retry(self) -> None:
        transport = mock.Mock()
        transport.post.return_value = response(b"x" * (ai.MAX_RESPONSE_BODY_BYTES + 1))
        outcome = ai.review_grounded_channels(
            (review(),), api_key="key", transport=transport, retry_backoff_seconds=0
        )
        transport.post.assert_called_once()
        self.assertEqual(outcome.results[0].error_code, "INVALID_RESPONSE")

    def test_malformed_success_response_is_not_retried(self) -> None:
        transport = mock.Mock()
        transport.post.return_value = response(b"not-json")
        outcome = ai.review_grounded_channels(
            (review(),), api_key="key", transport=transport, retry_backoff_seconds=0
        )
        transport.post.assert_called_once()
        self.assertEqual(outcome.results[0].error_code, "INVALID_RESPONSE")


class GroundedIdentityPolicyTests(unittest.TestCase):
    def run_suggestion(
        self,
        *,
        response_builder,
        request: ai.ReviewRequest | None = None,
    ) -> ai.ReviewResult:
        transport = mock.Mock()
        transport.post.side_effect = lambda *_a, **_k: response(
            response_builder(transport.post.call_args)
        )
        outcome = ai.review_grounded_channels(
            (request or review(),),
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        )
        return outcome.results[0]

    def test_suggest_requires_high_confidence(self) -> None:
        result = self.run_suggestion(
            response_builder=lambda call: suggestion_from_call(call, confidence="MEDIUM")
        )
        self.assertEqual(result.error_code, "NON_HIGH_SUGGESTION")

    def test_detached_id_and_negated_claim_are_rejected(self) -> None:
        for claim in (
            "DSTV Super Motorsport is SuperSport Motorsport. MOTORSPORT.za.",
            "DSTV Super Motorsport is not the same channel as MOTORSPORT.za.",
            "MOTORSPORT.za",
        ):
            with self.subTest(claim=claim):
                result = self.run_suggestion(
                    response_builder=lambda call, claim=claim: suggestion_from_call(
                        call, claim_override=claim
                    )
                )
                self.assertEqual(result.error_code, "UNSAFE_IDENTITY_CLAIM")

    def test_candidate_key_cannot_be_borrowed_from_another_row(self) -> None:
        transport = mock.Mock()

        def post(*_a, **_k):
            rows = request_rows(transport.post.call_args)
            claim = rows[0]["candidates"][0]["required_identity_claim"]
            generated = {
                "results": [
                    {
                        "review_id": rows[0]["review_id"],
                        "decision": "SUGGEST",
                        "candidate_key": "c002",
                        "confidence": "HIGH",
                        "identity_claim": claim,
                    },
                    {
                        "review_id": rows[1]["review_id"],
                        "decision": "ABSTAIN",
                        "candidate_key": "",
                        "confidence": "NONE",
                        "identity_claim": "",
                    },
                ]
            }
            return response(envelope(generated, grounding=grounding_for_claim(claim)))

        transport.post.side_effect = post
        second = review(
            "second",
            candidates=(candidate("c002", epg_id="Other.za", display_name="Other"),),
        )
        outcome = ai.review_grounded_channels(
            (review("first"), second),
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        )
        self.assertEqual(outcome.results[0].error_code, "UNKNOWN_CANDIDATE")
        self.assertIs(outcome.results[1].decision, ai.ReviewDecision.ABSTAIN)

    def test_abstain_needs_no_grounding(self) -> None:
        transport = mock.Mock()
        transport.post.side_effect = lambda *_a, **_k: response(
            abstain_from_call(transport.post.call_args)
        )
        outcome = ai.review_grounded_channels(
            (review(),), api_key="key", transport=transport, retry_backoff_seconds=0
        )
        result = outcome.results[0]
        self.assertIs(result.decision, ai.ReviewDecision.ABSTAIN)
        self.assertEqual(result.source_urls, ())

    def test_duplicate_json_key_invalidates_response(self) -> None:
        transport = mock.Mock()

        def post(*_a, **_k):
            row = request_rows(transport.post.call_args)[0]
            raw = (
                '{"results":[{"review_id":"%s","review_id":"%s",'
                '"decision":"ABSTAIN","candidate_key":"",'
                '"confidence":"NONE","identity_claim":""}]}'
            ) % (row["review_id"], row["review_id"])
            return response(envelope(raw))

        transport.post.side_effect = post
        outcome = ai.review_grounded_channels(
            (review(),), api_key="key", transport=transport, retry_backoff_seconds=0
        )
        self.assertEqual(outcome.results[0].error_code, "INVALID_RESPONSE")


class GroundingEvidenceTests(unittest.TestCase):
    def run_with(
        self,
        *,
        sources: list[dict[str, str]] | None = None,
        queries: list[str] | None = None,
        supports_factory=None,
    ) -> ai.ReviewResult:
        transport = mock.Mock()

        def post(*_a, **_k):
            rows = request_rows(transport.post.call_args)
            claim = rows[0]["candidates"][0]["required_identity_claim"]
            supports = supports_factory(claim) if supports_factory else None
            return response(
                suggestion_from_call(
                    transport.post.call_args,
                    sources=sources,
                    queries=queries,
                    supports=supports,
                )
            )

        transport.post.side_effect = post
        return ai.review_grounded_channels(
            (review(),),
            api_key="key",
            transport=transport,
            retry_backoff_seconds=0,
        ).results[0]

    def test_one_source_is_insufficient(self) -> None:
        result = self.run_with(sources=[default_sources()[0]])
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_two_urls_on_same_registrable_domain_are_insufficient(self) -> None:
        result = self.run_with(
            sources=[
                {"uri": "https://news.example.co.za/a", "title": "A"},
                {"uri": "https://guide.example.co.za/b", "title": "B"},
            ]
        )
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_split_citations_do_not_support_the_same_complete_claim(self) -> None:
        result = self.run_with(
            supports_factory=lambda claim: [
                {
                    "segment": {"text": claim.split(" with exact EPG ID", 1)[0]},
                    "groundingChunkIndices": [0],
                },
                {
                    "segment": {"text": "MOTORSPORT.za"},
                    "groundingChunkIndices": [1],
                },
            ]
        )
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_citations_for_another_claim_are_insufficient(self) -> None:
        result = self.run_with(
            supports_factory=lambda _claim: [
                {
                    "segment": {"text": "An unrelated channel identity claim."},
                    "groundingChunkIndices": [0, 1],
                }
            ]
        )
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_missing_search_query_is_insufficient(self) -> None:
        result = self.run_with(queries=[])
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_google_proxy_uses_web_domain(self) -> None:
        result = self.run_with(
            sources=[
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/a",
                    "domain": "www.dstv.com",
                    "title": "DStv",
                },
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/b",
                    "domain": "supersport.com",
                    "title": "SuperSport",
                },
            ]
        )
        self.assertIs(result.decision, ai.ReviewDecision.SUGGEST)
        self.assertEqual(result.source_authorities, ("dstv.com", "supersport.com"))

    def test_google_proxy_accepts_only_domain_like_title_fallback(self) -> None:
        result = self.run_with(
            sources=[
                {
                    "uri": "https://www.google.com/url?q=one",
                    "title": "https://dstv.com/",
                },
                {
                    "uri": "https://www.google.com/url?q=two",
                    "title": "supersport.com",
                },
            ]
        )
        self.assertIs(result.decision, ai.ReviewDecision.SUGGEST)

    def test_opaque_google_proxy_fails_closed(self) -> None:
        result = self.run_with(
            sources=[
                {
                    "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/a",
                    "title": "Official DStv page",
                },
                default_sources()[1],
            ]
        )
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_domain_field_has_priority_over_direct_host(self) -> None:
        result = self.run_with(
            sources=[
                {
                    "uri": "https://one.example/a",
                    "domain": "publisher-one.test",
                    "title": "First",
                },
                {
                    "uri": "https://two.example/b",
                    "domain": "publisher-two.test",
                    "title": "Second",
                },
            ]
        )
        self.assertEqual(
            result.source_authorities,
            ("publisher-one.test", "publisher-two.test"),
        )

    def test_invalid_source_index_fails_closed(self) -> None:
        result = self.run_with(
            supports_factory=lambda claim: [
                {
                    "segment": {"text": claim},
                    "groundingChunkIndices": [0, 99],
                }
            ]
        )
        self.assertEqual(result.error_code, "INSUFFICIENT_GROUNDING")

    def test_registrable_domain_normalization(self) -> None:
        self.assertEqual(ai._registrable_domain("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(ai._registrable_domain("news.example.com"), "example.com")
        self.assertEqual(ai._registrable_domain("foo.github.io"), "foo.github.io")
        self.assertIsNone(ai._registrable_domain("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
