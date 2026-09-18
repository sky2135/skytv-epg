"""Versioned, conservative shadow policy for the matching laboratory."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ContractError, sha256_json


@dataclass(frozen=True, slots=True)
class MatchingPolicy:
    policy_id: str = "smart-match-lab-shadow-v2"
    version: str = "2.0.0"
    maximum_retrieval_candidates: int = 64
    retained_candidates: int = 8
    minimum_candidate_score_ppm: int = 550_000
    human_review_score_ppm: int = 650_000
    # The v2 standard lane is deliberately more permissive on calibrated
    # similarity, but requires a wider separation from the runner-up.  A
    # conservative cohort has additional exactness gates in ``pipeline``.
    strong_proposal_score_ppm: int = 800_000
    strong_margin_ppm: int = 100_000
    expiry_seconds: int = 6 * 60 * 60
    # This is a hashed safety assertion, not a runtime switch. Matching Lab v1
    # accepts only the default policy and has no apply operation.
    auto_apply_enabled: bool = False
    contextual_weight_ppm: int = 400_000
    token_weight_ppm: int = 250_000
    ngram_weight_ppm: int = 200_000
    transliteration_weight_ppm: int = 100_000
    acronym_weight_ppm: int = 50_000

    def __post_init__(self) -> None:
        if not 8 <= self.maximum_retrieval_candidates <= 256:
            raise ContractError("maximum_retrieval_candidates is invalid.")
        if not 2 <= self.retained_candidates <= 8:
            raise ContractError("retained_candidates must be between two and eight.")
        thresholds = (
            self.minimum_candidate_score_ppm,
            self.human_review_score_ppm,
            self.strong_proposal_score_ppm,
        )
        if any(not 0 <= value <= 1_000_000 for value in thresholds):
            raise ContractError("A Matching Lab score threshold is invalid.")
        if tuple(sorted(thresholds)) != thresholds:
            raise ContractError("Matching Lab score thresholds must be monotonic.")
        if not 0 <= self.strong_margin_ppm <= 1_000_000:
            raise ContractError("strong_margin_ppm is invalid.")
        if not 300 <= self.expiry_seconds <= 24 * 60 * 60:
            raise ContractError("expiry_seconds is invalid.")
        if type(self.auto_apply_enabled) is not bool:
            raise ContractError("auto_apply_enabled must be exactly boolean.")
        weights = (
            self.contextual_weight_ppm,
            self.token_weight_ppm,
            self.ngram_weight_ppm,
            self.transliteration_weight_ppm,
            self.acronym_weight_ppm,
        )
        if sum(weights) != 1_000_000 or any(value < 0 for value in weights):
            raise ContractError("Matching Lab feature weights must total 1000000.")

    def public_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "maximum_retrieval_candidates": self.maximum_retrieval_candidates,
            "retained_candidates": self.retained_candidates,
            "minimum_candidate_score_ppm": self.minimum_candidate_score_ppm,
            "human_review_score_ppm": self.human_review_score_ppm,
            "strong_proposal_score_ppm": self.strong_proposal_score_ppm,
            "strong_margin_ppm": self.strong_margin_ppm,
            "expiry_seconds": self.expiry_seconds,
            "auto_apply_enabled": self.auto_apply_enabled,
            "weights_ppm": {
                "contextual": self.contextual_weight_ppm,
                "token": self.token_weight_ppm,
                "ngram": self.ngram_weight_ppm,
                "transliteration": self.transliteration_weight_ppm,
                "acronym": self.acronym_weight_ppm,
            },
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.public_dict())


DEFAULT_POLICY = MatchingPolicy()


__all__ = ("DEFAULT_POLICY", "MatchingPolicy")
