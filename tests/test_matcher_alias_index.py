from __future__ import annotations

import csv
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import matcher_alias_index as indexing
from skytv_epg_contextual_v8 import ContextualMatcherV8


def query(name="brand", **changes):
    value = dict(strict_key=name, relaxed_key=name, identity_keys=(name,), edition_key=name,
        timeshift="", route_plan=("US",), route_explicit=True, languages=frozenset(),
        quality="", has_plus=False, has_extra=False, has_alternate=False, direction="",
        numbers=frozenset(), content=frozenset(), tokens=tuple(name.split()))
    value.update(changes)
    return SimpleNamespace(**value)


def resolver():
    engine = SimpleNamespace(SOURCE_SUFFIX_RE=re.compile(r"\.[a-z]{2,}(?:\d+|_locals\d*)?$", re.I),
        ID_LOOKUP={identifier.casefold(): {"epg_id": identifier, "feed": "ALL_SOURCES1", "region": market}
            for identifier, market in (("First.us", "US"), ("Second.us", "US"),
                                      ("Shift.uk", "UK"), ("Target.ae", "AE"))})
    result = ContextualMatcherV8(engine)
    result.approved_aliases = []
    return result


class MatcherAliasIndexTests(unittest.TestCase):
    def assert_parity(self, matcher, queries):
        original = ContextualMatcherV8._approved_alias_match
        expected = [original(matcher, item) for item in queries]
        adapter = indexing.install_alias_index(matcher)
        actual = [matcher._approved_alias_match(item) for item in queries]
        self.assertEqual(actual, expected)
        return adapter, actual

    def test_query_key_union_retains_global_row_and_target_priority(self):
        matcher = resolver()
        matcher.register_approved_aliases([
            {"alias": "other", "regions": "US", "epg_ids": "Missing.us|Second.us|First.us", "note": "first row"},
            {"alias": "brand", "regions": "US", "epg_ids": "First.us", "note": "later row"}])
        _, results = self.assert_parity(matcher, [query(identity_keys=("brand", "other")), query(), query("absent")])
        self.assertEqual(results[0]["epg_id"], "Second.us")
        self.assertEqual(results[1]["epg_id"], "First.us")
        self.assertIsNone(results[2])

    def test_identical_alias_market_language_conditions_are_not_collapsed(self):
        matcher = resolver()
        matcher.register_approved_aliases([
            {"alias": "brand", "regions": "CA", "epg_ids": "Second.us"},
            {"alias": "brand", "regions": "US", "epg_ids": "First.us", "required_languages": "hindi"},
            {"alias": "brand", "regions": "ALL", "epg_ids": "Second.us"}])
        _, results = self.assert_parity(matcher, [query(), query(languages=frozenset({"hindi"})),
            query(route_plan=("UK",)), query(route_plan=("US", "CA"))])
        self.assertEqual(results[0]["epg_id"], "Second.us")
        self.assertEqual(results[1]["epg_id"], "First.us")
        self.assertIsNone(results[2])

    def test_timeshift_edition_key_is_shortlisted_but_plain_target_never_substitutes(self):
        matcher = resolver()
        matcher.register_approved_aliases([{"alias": "brand", "regions": "UK", "epg_ids": "First.us",
            "timeshift_epg_ids": {"1": ("Shift.uk",)}}])
        _, results = self.assert_parity(matcher, [
            query("brand plus 1", edition_key="brand", timeshift="1", route_plan=("UK",)),
            query("brand plus 2", edition_key="brand", timeshift="2", route_plan=("UK",)),
            query("brand west", edition_key="brand", route_plan=("UK",))])
        self.assertEqual(results[0]["epg_id"], "Shift.uk")
        self.assertIsNone(results[1])
        self.assertIsNone(results[2])

    def test_cross_storage_still_requires_exact_authority_and_protected_semantics(self):
        matcher = resolver()
        matcher.register_approved_aliases([{"alias": "brand", "regions": "MENA", "target_regions": "AE",
            "epg_ids": "Target.ae"}], _allow_cross_storage=True)
        _, results = self.assert_parity(matcher, [query(route_plan=("MENA",)),
            query("different", relaxed_key="brand", route_plan=("MENA",)),
            query(route_plan=("MENA",), timeshift="1"),
            query(route_plan=("MENA",), has_extra=True), query(route_plan=("US",))])
        self.assertEqual(results[0]["match_method"], "approved_storage_knowledge")
        self.assertEqual(results[1:], [None] * 4)

    def test_ordinary_late_registration_cannot_gain_cross_storage_privilege(self):
        matcher = resolver()
        adapter = indexing.install_alias_index(matcher)
        matcher.register_approved_aliases([{"alias": "brand", "regions": "MENA",
            "target_regions": "AE", "epg_ids": "Target.ae"}])
        self.assertIsNone(matcher._approved_alias_match(query(route_plan=("MENA",))))
        self.assertEqual(adapter.stats["builds"], 2)
        self.assertEqual(matcher._approved_alias_match(query(route_plan=("MENA",))),
            ContextualMatcherV8._approved_alias_match(matcher, query(route_plan=("MENA",))))

    def test_alias_registration_after_installation_refreshes_merges_and_notes(self):
        matcher = resolver()
        matcher.register_approved_aliases([{"alias": "brand", "regions": "US", "epg_ids": "Missing.us"}])
        adapter = indexing.install_alias_index(matcher)
        self.assertIsNone(matcher._approved_alias_match(query()))
        changed = matcher.register_approved_aliases([{"alias": "brand", "regions": "US", "epg_ids": "First.us", "note": "new"}])
        self.assertEqual(changed, 1)
        self.assertEqual(matcher._approved_alias_match(query()), ContextualMatcherV8._approved_alias_match(matcher, query()))
        self.assertIn("new", matcher._approved_alias_match(query())["reason"])
        self.assertEqual(adapter.stats["builds"], 2)

    def test_loader_registration_and_partial_failure_invalidate(self):
        matcher = resolver()
        indexing.install_alias_index(matcher)
        import pandas as pd
        matcher.load_approved_aliases(pd.DataFrame([{"alias": "brand", "regions": "US", "epg_ids": "First.us"}]))
        self.assertEqual(matcher._approved_alias_match(query())["epg_id"], "First.us")
        def broken_rows():
            yield {"alias": "second", "regions": "US", "epg_ids": "Second.us"}
            raise ValueError("fixture iterator failure")
        with self.assertRaisesRegex(ValueError, "fixture iterator failure"):
            matcher.register_approved_aliases(broken_rows())
        self.assertEqual(matcher._approved_alias_match(query("second"))["epg_id"], "Second.us")

    def test_list_replacement_append_and_live_policy_fields_preserve_behavior(self):
        matcher = resolver()
        adapter = indexing.install_alias_index(matcher)
        row = {"alias": "BRAND", "regions": ("US",), "epg_ids": ("First.us",)}
        matcher.approved_aliases = [row]
        self.assertEqual(matcher._approved_alias_match(query())["epg_id"], "First.us")
        row["required_languages"] = ("hindi",)
        self.assertIsNone(matcher._approved_alias_match(query()))
        matcher.approved_aliases.append({"alias": "second", "regions": ("US",), "epg_ids": ("Second.us",)})
        self.assertEqual(matcher._approved_alias_match(query("second"))["epg_id"], "Second.us")
        row["alias"] = "changed"
        adapter.refresh()
        self.assertEqual(matcher._approved_alias_match(query("changed", languages=frozenset({"hindi"})))["epg_id"], "First.us")

    def test_installation_is_idempotent_and_no_candidate_scan_on_absent_key(self):
        matcher = resolver()
        matcher.register_approved_aliases([{"alias": "brand", "regions": "US", "epg_ids": "First.us"}])
        adapter = indexing.install_alias_index(matcher)
        self.assertIs(indexing.install_alias_index(matcher), adapter)
        with mock.patch.object(indexing, "_normalize_key_v8", side_effect=AssertionError("rebuild")):
            self.assertIsNone(matcher._approved_alias_match(query("absent")))
        self.assertEqual(adapter.stats, {"builds": 1, "lookups": 1, "candidateRows": 0, "totalRows": 1})

    def test_index_is_only_a_shortlist_and_calls_original_policy_body(self):
        matcher = resolver()
        matcher.register_approved_aliases([{"alias": "brand", "regions": "US", "epg_ids": "First.us"}])
        adapter = indexing.install_alias_index(matcher)
        with mock.patch.object(adapter, "_original_lookup", wraps=adapter._original_lookup) as original:
            self.assertEqual(matcher._approved_alias_match(query())["epg_id"], "First.us")
        original.assert_called_once()
        self.assertIs(original.call_args.args[0].engine, matcher.engine)
        self.assertIs(original.call_args.args[0].approved_aliases[0], matcher.approved_aliases[0])

    def test_complete_pinned_alias_file_and_negative_variants_match_original(self):
        matcher = resolver()
        path = ROOT / "knowledge" / "approved_channel_aliases.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        queries = []
        for row in rows:
            markets = tuple(value.upper() for value in re.split(r"[|;]", row["regions"]) if value)
            market = markets[0] if markets else "ALL"
            for identifier in row["epg_ids"].split("|"):
                matched = re.search(r"\.([a-z]{2})(?:\d+|_locals\d*)?$", identifier, re.I)
                target_market = matched.group(1).upper() if matched else market
                matcher.engine.ID_LOOKUP[identifier.casefold()] = {
                    "epg_id": identifier, "region": target_market, "feed": "ALL_SOURCES1"}
            name = indexing._normalize_key_v8(row["alias"])
            queries.extend((query(name, route_plan=(market,)),
                query(name + " unexpected edition", route_plan=(market,))))
        matcher.load_approved_aliases(path)
        self.assertGreaterEqual(len(rows), 200)
        self.assert_parity(matcher, queries)


if __name__ == "__main__":
    unittest.main()
