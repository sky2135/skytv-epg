from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import automation_config as config  # noqa: E402


class AutomationConfigTests(unittest.TestCase):
    def test_accepts_every_supported_limit_as_integer_and_string(self):
        for name, choices in (
            ("review_apply_limit", config.REVIEW_APPLY_LIMITS),
            ("coverage_fallback_limit", config.COVERAGE_FALLBACK_LIMITS),
            ("ai_review_limit", config.AI_REVIEW_LIMITS),
        ):
            for choice in choices:
                for value in (choice, str(choice)):
                    with self.subTest(name=name, value=value):
                        self.assertEqual(config.validate_run_limits(**{name: value}), {name: choice})

    def test_sync_and_publication_both_accept_thirty_thousand_fallback(self):
        self.assertEqual(
            config.validate_run_limits(
                review_apply_limit="30000", coverage_fallback_limit="30000", ai_review_limit="200"
            ),
            {"review_apply_limit": 30000, "coverage_fallback_limit": 30000, "ai_review_limit": 200},
        )
        self.assertEqual(
            config.validate_run_limits(coverage_fallback_limit="30000"),
            {"coverage_fallback_limit": 30000},
        )

    def test_rejects_unsupported_or_malformed_values_for_every_limit(self):
        malformed_values = (
            True, False, -1, 1, 30001, 25.0, float("nan"), float("inf"),
            float("-inf"), "", "-0", "+0", "01", "30000.0", "3e4",
            "30_000", " 30000", "30000\n", "３００００", "٣٠٠٠٠",
            "30000; touch unexpected", "$(echo 30000)", "9" * 10000,
            [], {}, object(),
        )
        for name in ("review_apply_limit", "coverage_fallback_limit", "ai_review_limit"):
            for value in malformed_values:
                with self.subTest(name=name, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, f"^{name} must be one of:"):
                        config.validate_run_limits(**{name: value})

    def test_validation_does_not_invoke_untrusted_conversion_hooks(self):
        class UnsafeValue:
            def __str__(self):
                raise AssertionError("must not convert arbitrary objects")

            def __int__(self):
                raise AssertionError("must not convert arbitrary objects")

        with self.assertRaises(ValueError):
            config.validate_run_limits(review_apply_limit=UnsafeValue())

    def test_cli_accepts_shared_run_limits(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = config.main([
                "--review-apply-limit", "30000", "--coverage-fallback-limit", "30000",
                "--ai-review-limit", "200",
            ])
        self.assertEqual(result, 0)
        self.assertIn("coverage_fallback_limit=30000", output.getvalue())

    def test_cli_reports_invalid_values_without_echoing_payload(self):
        payload = "30000; secret-payload"
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            config.main(["--coverage-fallback-limit", payload])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("coverage_fallback_limit must be one of:", error.getvalue())
        self.assertNotIn(payload, error.getvalue())

    def test_cli_requires_a_limit(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            config.main([])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
