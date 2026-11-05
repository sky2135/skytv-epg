from __future__ import annotations

import copy
import json
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vod_rule_sampling as sampling


def record(number, *, server="server_1", kind="movie", verified=False,
           reason="title_mismatch", identity=None, title=None, language="en"):
    content_id = f"{server}:{kind}:{number}"
    return {
        "contentId": content_id, "kind": kind, "identityKey": identity or content_id,
        "item": {"title": title or f"Example {number} (2025)", "languages": [language]},
        "baselineVerified": verified, "baselineReason": "validated_cache" if verified else reason,
        "baselineStatus": "matched_search" if verified else "no_match",
        "baselineMetadata": {"tmdbId": str(number), "matchStatus": "matched_search"} if verified else {},
    }


class RuleSamplingTests(unittest.TestCase):
    def setUp(self):
        self.reviewed = mock.patch.object(sampling, "_reviewed_ids", return_value=frozenset())
        self.reviewed.start()
        self.addCleanup(self.reviewed.stop)
        self.rejections = mock.patch.object(sampling, "_reviewed_rejection_ids", return_value=frozenset())
        self.rejections.start()
        self.addCleanup(self.rejections.stop)

    def test_selection_is_order_independent_and_never_mutates_inputs(self):
        records = [record(index, verified=index % 7 == 0, language=("en", "hi", "pa")[index % 3]) for index in range(1, 2200)]
        original = copy.deepcopy(records)
        expected = sampling.select_evaluation(records)
        random.Random(42).shuffle(records)
        self.assertEqual(sampling.select_evaluation(records), expected)
        self.assertEqual(sorted(records, key=lambda row: row["contentId"]), sorted(original, key=lambda row: row["contentId"]))
        self.assertEqual(len(expected["forceRecheckContentIds"]), 200)

    def test_every_copy_of_a_selected_original_identity_is_included(self):
        rows = [record(1, identity="same"), record(2, server="server_2", identity="same"),
                record(3, server="server_3", identity="same"), record(4, verified=True)]
        result = sampling.select_evaluation(rows)
        self.assertEqual(set(result["selectedContentIds"]), {row["contentId"] for row in rows})
        self.assertEqual(result["summary"]["coreUnverifiedIdentities"], 1)
        self.assertEqual(result["forceRecheckContentIds"], [rows[-1]["contentId"]])

    def test_core_rotates_reasons_and_server_kind_before_language_strata(self):
        reasons = ("title_mismatch", "year_mismatch", "missing_identity", "no_search_results", "ambiguous_title_year", "alias_candidate_limit")
        rows = []
        number = 1
        for reason in reasons:
            for server in ("server_1", "server_2", "server_3"):
                for kind in ("movie", "series"):
                    for index in range(70):
                        rows.append(record(number, reason=reason, server=server, kind=kind,
                                           title=f"Plain title {number}", language=("en", "hi", "pa")[index % 3]))
                        number += 1
        result = sampling.select_evaluation(rows)
        self.assertEqual(result["summary"]["coreUnverifiedIdentities"], 1200)
        self.assertEqual(set(result["summary"]["coreByReason"].values()), {200})
        self.assertEqual(set(result["summary"]["coreByServerKind"].values()), {200})

    def test_additional_format_clusters_are_beyond_the_core_sample(self):
        rows = [record(index, title=f"Plain title {index}") for index in range(1, 2001)]
        rows += [record(index, title=f"EX - Example {index} (2025)") for index in range(2001, 3001)]
        result = sampling.select_evaluation(rows)
        self.assertEqual(result["summary"]["coreUnverifiedIdentities"], 1200)
        self.assertEqual(result["summary"]["extraFormatIdentities"], 300)
        self.assertEqual(len(result["selectedContentIds"]), 1500)
        self.assertEqual(len(set(result["selectedContentIds"])), 1500)

    def test_full_scope_avoids_a_huge_selected_id_list_and_forces_only_controls(self):
        rows = [record(index, verified=True, server=f"server_{index % 3 + 1}", kind="series" if index % 2 else "movie") for index in range(1, 501)]
        rows += [record(1000 + index) for index in range(1, 51)]
        result = sampling.select_evaluation(rows, "full")
        self.assertIsNone(result["selectedContentIds"])
        self.assertEqual(result["summary"]["selectedContentRecords"], 550)
        self.assertEqual(len(result["forceRecheckContentIds"]), 200)
        positives = {row["contentId"] for row in rows if row["baselineVerified"]}
        self.assertLessEqual(set(result["forceRecheckContentIds"]), positives)

    def test_reviewed_numeric_and_semantic_controls_are_retained(self):
        rows = [record(index, verified=True) for index in range(1, 301)]
        reviewed = record(900, verified=True, title="Reviewed positive")
        numeric = record(901, verified=True, title="1917 (2019)")
        semantic = record(902, verified=True, title="IT: Chapter Two (2019)")
        rows += [reviewed, numeric, semantic]
        with mock.patch.object(sampling, "_reviewed_ids", return_value=frozenset({reviewed["contentId"]})):
            result = sampling.select_evaluation(rows)
        self.assertTrue({reviewed["contentId"], numeric["contentId"], semantic["contentId"]}.issubset(result["forceRecheckContentIds"]))
        self.assertEqual(len(result["forceRecheckContentIds"]), 200)
        self.assertEqual(result["summary"]["reviewedPositiveControls"], 1)
        self.assertGreaterEqual(result["summary"]["numericPositiveControls"], 1)

    def test_a_mixed_outcome_identity_stays_in_the_unverified_cohort(self):
        rows = [record(1, identity="mixed", verified=True), record(2, identity="mixed")]
        result = sampling.select_evaluation(rows)
        self.assertEqual(result["summary"]["coreUnverifiedIdentities"], 1)
        self.assertFalse(result["forceRecheckContentIds"])
        self.assertEqual(len(result["selectedContentIds"]), 2)

    def test_reviewed_rejection_scopes_are_mandatory_without_becoming_matcher_hints(self):
        rows = [record(index, title=f"Plain title {index}") for index in range(1, 3001)]
        rows += [record(index, verified=True) for index in range(4001, 4301)]
        initial = sampling.select_evaluation(rows)
        absent = next(row for row in rows if not row["baselineVerified"]
                      and row["contentId"] not in initial["selectedContentIds"])
        duplicate = record(9999, server="server_2", identity=absent["identityKey"])
        rows.append(duplicate)
        rejections = frozenset({absent["contentId"], rows[-2]["contentId"]})
        with mock.patch.object(sampling, "_reviewed_rejection_ids", return_value=rejections):
            result = sampling.select_evaluation(rows)
            reversed_result = sampling.select_evaluation(reversed(rows))
        self.assertEqual(result, reversed_result)
        self.assertLessEqual(rejections, set(result["selectedContentIds"]))
        self.assertIn(duplicate["contentId"], result["selectedContentIds"])
        self.assertEqual(result["summary"]["availableReviewedRejectionControls"], 2)
        self.assertEqual(result["summary"]["selectedReviewedRejectionControls"], 2)
        self.assertEqual(result["summary"]["forcedPositiveControls"], 200)
        self.assertNotIn(absent["contentId"], result["forceRecheckContentIds"])
        self.assertNotIn("rejectedTmdbId", json.dumps(result))
        self.assertNotEqual(result["summary"]["sourceFingerprint"], initial["summary"]["sourceFingerprint"])

    def test_baseline_metadata_never_becomes_a_matcher_hint(self):
        result = sampling.select_evaluation([record(1, verified=True)])
        self.assertEqual(set(result), {"selectedContentIds", "forceRecheckContentIds", "summary"})
        rendered = json.dumps(result)
        self.assertNotIn("tmdbId", rendered)
        self.assertNotIn("Example 1", rendered)

    def test_invalid_scope_duplicate_ids_or_incompatible_groups_fail_closed(self):
        with self.assertRaises(ValueError):
            sampling.select_evaluation([], "production")
        with self.assertRaises(ValueError):
            sampling.select_evaluation([record(1), record(1)])
        with self.assertRaises(ValueError):
            sampling.select_evaluation([record(1, identity="same"), record(2, kind="series", identity="same")])
        for field, value in (("contentId", "server_1:movie:../../secret"), ("kind", "tv"),
                             ("baselineVerified", 1), ("identityKey", ""), ("item", None)):
            row = record(1)
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                sampling.select_evaluation([row])

    def test_bounds_reject_oversized_records_or_partial_group_truncation(self):
        with mock.patch.object(sampling, "MAX_RECORDS", 2), self.assertRaises(ValueError):
            sampling.select_evaluation([record(1), record(2), record(3)])
        with mock.patch.object(sampling, "MAX_SMOKE_ID_BYTES", 10), self.assertRaises(ValueError):
            sampling.select_evaluation([record(1, identity="same"), record(2, identity="same")])

    def test_empty_catalog_returns_no_scheduled_work(self):
        result = sampling.select_evaluation([])
        self.assertEqual(result["selectedContentIds"], [])
        self.assertEqual(result["forceRecheckContentIds"], [])
        self.assertEqual(result["summary"]["sourceRecords"], 0)


if __name__ == "__main__":
    unittest.main()
