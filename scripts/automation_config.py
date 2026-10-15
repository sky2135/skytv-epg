"""Shared validation of bounded inventory and publication run limits."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


REVIEW_APPLY_LIMITS = (0, 25, 100, 500, 2500, 5000, 30000)
COVERAGE_FALLBACK_LIMITS = (0, 500, 2500, 5000, 30000)
AI_REVIEW_LIMITS = (10, 25, 50, 100, 200)


def _validate_limit(name: str, value: object, choices: tuple[int, ...]) -> int:
    # Match strings before converting them: this rejects non-decimal values,
    # enormous integer payloads, and objects with executable conversion hooks.
    if type(value) is int and value in choices:
        return value
    if type(value) is str and value in {str(choice) for choice in choices}:
        return int(value)
    allowed = ", ".join(str(choice) for choice in choices)
    raise ValueError(f"{name} must be one of: {allowed}.")


def validate_run_limits(
    *,
    review_apply_limit: object = None,
    coverage_fallback_limit: object = None,
    ai_review_limit: object = None,
) -> dict[str, int]:
    """Validate supplied limits and return their normalized integer values.

    Omitted limits are ignored so publication can validate only its relevant
    settings. Values must be an allowed integer or its exact ASCII decimal
    string; booleans, floats, signs, whitespace, and alternate formats fail.
    """
    validated: dict[str, int] = {}
    for name, value, choices in (
        ("review_apply_limit", review_apply_limit, REVIEW_APPLY_LIMITS),
        ("coverage_fallback_limit", coverage_fallback_limit, COVERAGE_FALLBACK_LIMITS),
        ("ai_review_limit", ai_review_limit, AI_REVIEW_LIMITS),
    ):
        if value is not None:
            validated[name] = _validate_limit(name, value, choices)
    return validated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-apply-limit")
    parser.add_argument("--coverage-fallback-limit")
    parser.add_argument("--ai-review-limit")
    args = parser.parse_args(argv)
    if all(value is None for value in vars(args).values()):
        parser.error("provide at least one run limit")
    try:
        validated = validate_run_limits(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    print("Validated run limits: " + ", ".join(f"{name}={value}" for name, value in validated.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
