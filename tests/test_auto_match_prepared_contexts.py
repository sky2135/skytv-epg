from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import auto_match_inventory as integration


class PreparedCandidateContextTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(SOURCE_SUFFIX_RE=re.compile(r"\.[a-z]{2,}(?:\d+|_locals\d*)?$", re.I))

    def resolver(self, contexts):
        return SimpleNamespace(
            engine=self.engine, state=SimpleNamespace(by_region={"ALL": contexts}),
            _compatible=lambda *args, **kwargs: True,
            _fuzzy_score=lambda *args, **kwargs: 0,
        )

    def candidate(self, epg_id="Example.HD.us2", region="US"):
        return {"epg_id": epg_id, "region": region, "feed": region + "2",
                "display_name": epg_id, "normalized": "example"}

    def test_reused_semantics_equal_fresh_parser_for_identity_boundaries(self):
        cases = [
            ("Example.HD.us2", "US"), ("Canal+.fr", "FR"),
            ("Example+1.uk", "UK"), ("Example.East.us", "US"),
            ("Example.West.us", "US"), ("Example.Extra.us", "US"),
            ("Example.Alternate.us", "US"), ("Example.4K.us", "US"),
            ("Example.2.us", "US"), ("EN:.MBC1.Masr.sa", "SA"),
            ("Example.Norway.(NO,NO).no", "NO"),
            ("u.and.Example.uk", "UK"), ("Example.EN.stereo.us", "US"),
            ("Example.zz", "ZZ"), ("Example.no", "US"),
        ]
        for epg_id, region in cases:
            with self.subTest(epg_id=epg_id, region=region):
                raw = self.candidate(epg_id, region)
                prepared_raw = {**raw, "normalized": "prepared engine normalization", "family_key": "engine only"}
                context = integration.parse_candidate_context_v8(self.engine, prepared_raw)
                resolver = self.resolver([context])
                expected = integration.parse_candidate_context_v8(self.engine, raw)
                with patch.object(integration, "parse_candidate_context_v8", side_effect=AssertionError("reparsed")):
                    actual = integration._catalog_candidate_context(
                        resolver, raw, integration._prepared_candidate_contexts(resolver))
                self.assertEqual(actual, expected)
                self.assertIs(actual.candidate, raw)
                self.assertNotIn("family_key", actual.candidate)

    def test_unprepared_market_or_exact_id_uses_original_parser(self):
        raw = self.candidate()
        context = integration.parse_candidate_context_v8(self.engine, raw)
        resolver = self.resolver([context])
        index = integration._prepared_candidate_contexts(resolver)
        for changed in (self.candidate(region="CA"), self.candidate("example.HD.us2")):
            with self.subTest(candidate=changed):
                with patch.object(integration, "parse_candidate_context_v8", wraps=integration.parse_candidate_context_v8) as parser:
                    actual = integration._catalog_candidate_context(resolver, changed, index)
                parser.assert_called_once_with(self.engine, changed)
                self.assertEqual(actual, integration.parse_candidate_context_v8(self.engine, changed))

    def test_conflicting_prepared_entries_fail_back_to_fresh_parse(self):
        raw = self.candidate()
        context = integration.parse_candidate_context_v8(self.engine, raw)
        conflicting = replace(context, strict_key="different")
        resolver = self.resolver([context, conflicting, context])
        self.assertEqual(integration._prepared_candidate_contexts(resolver), {})
        with patch.object(integration, "parse_candidate_context_v8", wraps=integration.parse_candidate_context_v8) as parser:
            actual = integration._catalog_candidate_context(resolver, raw, {})
        parser.assert_called_once()
        self.assertEqual(actual, context)

    def test_absent_or_foreign_prepared_state_is_not_trusted(self):
        for resolver in (None, SimpleNamespace(), self.resolver([SimpleNamespace(candidate=self.candidate())])):
            self.assertEqual(integration._prepared_candidate_contexts(resolver), {})

    def test_advisory_catalog_reuses_contexts_without_changing_output(self):
        raw = self.candidate()
        context = integration.parse_candidate_context_v8(self.engine, raw)
        prepared = self.resolver([context])
        unprepared = self.resolver([])
        arguments = dict(proposals={}, review_keys=(), corroborated_real_candidates=[raw])
        with patch.object(integration, "parse_candidate_context_v8", wraps=integration.parse_candidate_context_v8) as parser:
            baseline = integration._stage_ai_review_shortlists_with_metrics(resolver=unprepared, **arguments)
        self.assertEqual(parser.call_count, 1)
        with patch.object(integration, "parse_candidate_context_v8", side_effect=AssertionError("reparsed")):
            actual = integration._stage_ai_review_shortlists_with_metrics(resolver=prepared, **arguments)
        self.assertEqual(actual, baseline)


if __name__ == "__main__":
    unittest.main()
