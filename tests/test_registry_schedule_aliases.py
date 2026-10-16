import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from channel_metadata_enrichment import ChannelRegistry
from registry_schedule_aliases import registry_schedule_aliases
from skytv_epg_contextual_v8 import _normalize_key_v8


class RegistryScheduleAliasesTest(unittest.TestCase):
    def setUp(self):
        self.registry = ChannelRegistry([
            {"id": "ExampleNews.us", "name": "Example News", "alt_names": ["Example Television News"], "country": "US"},
            {"id": "ExampleNews.ca", "name": "Example News", "alt_names": [], "country": "CA"},
        ], [], [{"code": "US", "name": "United States"}, {"code": "CA", "name": "Canada"}])
        self.candidate = {"epg_id": "Example.News.HD.us2", "display_name": "Example News HD", "region": "US"}

    def test_exact_alias_joins_unique_same_country_target(self):
        rows = registry_schedule_aliases([self.candidate], self.registry)
        self.assertIn("Example Television News", [r["alias"] for r in rows])
        self.assertTrue(all(r["epg_ids"] == self.candidate["epg_id"] and r["regions"] == "US" for r in rows))

    def test_two_epg_targets_are_ambiguous(self):
        second = {**self.candidate, "epg_id": "Example.News.us"}
        self.assertEqual([], registry_schedule_aliases([self.candidate, second], self.registry))

    def test_unknown_or_wrong_market_cannot_supply_aliases(self):
        for market in ("ALL", "UK"):
            self.assertEqual([], registry_schedule_aliases([{**self.candidate, "region": market}], self.registry))

    def test_reviewed_alias_and_edition_markers_are_preserved(self):
        rows = registry_schedule_aliases([self.candidate], self.registry, existing_aliases=[{"alias": "Example Television News"}])
        self.assertNotIn("Example Television News", [r["alias"] for r in rows])
        self.assertEqual([], registry_schedule_aliases([{**self.candidate, "display_name": "Example News +1"}], self.registry))

    def test_alias_collision_uses_the_frozen_matcher_normalizer(self):
        registry = ChannelRegistry([
            {"id": "UltraNews2.us", "name": "Ultra News 2", "alt_names": ["Example UHD 2"], "country": "US"},
            {"id": "Example2.us", "name": "Example 2", "country": "US"},
        ], [], [{"code": "US", "name": "United States"}])
        candidate = {"epg_id": "Ultra.News.2.us", "display_name": "Ultra News 2", "region": "US"}
        rows = registry_schedule_aliases([candidate], registry, matcher_normalizer=_normalize_key_v8)
        self.assertNotIn("Example UHD 2", [row["alias"] for row in rows])

    def test_static_alias_cannot_be_widened_by_technical_token_cleanup(self):
        registry = ChannelRegistry([
            {"id": "UltraNews2.us", "name": "Ultra News 2", "alt_names": ["Example UHD 2"], "country": "US"},
        ], [], [{"code": "US", "name": "United States"}])
        candidate = {"epg_id": "Ultra.News.2.us", "display_name": "Ultra News 2", "region": "US"}
        rows = registry_schedule_aliases([candidate], registry,
            existing_aliases=[{"alias": "Example 2"}], matcher_normalizer=_normalize_key_v8)
        self.assertNotIn("Example UHD 2", [row["alias"] for row in rows])

    def test_unrecognized_epg_competitor_still_blocks_matcher_key_collision(self):
        competitor = {"epg_id": "Other.News.us", "display_name": "Example Digital Television News", "region": "US"}
        rows = registry_schedule_aliases([self.candidate, competitor], self.registry,
                                         matcher_normalizer=_normalize_key_v8)
        self.assertNotIn("Example Television News", [row["alias"] for row in rows])

    def test_conflicting_duplicate_epg_display_names_are_order_independent(self):
        registry = ChannelRegistry([
            {"id": "ExampleNews.us", "name": "Example News", "alt_names": ["Example Television News", "Example News West"], "country": "US"},
        ], [], [{"code": "US", "name": "United States"}])
        other_name = {**self.candidate, "display_name": "Example News West"}
        for candidates in ([self.candidate, other_name], [other_name, self.candidate]):
            self.assertEqual([], registry_schedule_aliases(candidates, registry,
                                                           matcher_normalizer=_normalize_key_v8))

    def test_exact_source_spelling_does_not_add_a_redundant_approved_rule(self):
        rows = registry_schedule_aliases([self.candidate], self.registry,
                                         matcher_normalizer=_normalize_key_v8)
        self.assertEqual(["Example Television News"], [row["alias"] for row in rows])

    def test_relaxed_query_identity_cannot_cross_another_registry_channel(self):
        registry = ChannelRegistry([
            {"id": "UltraNews.us", "name": "Ultra News", "alt_names": ["The Example Channel"], "country": "US"},
            {"id": "Example.us", "name": "Example", "country": "US"},
        ], [], [{"code": "US", "name": "United States"}])
        candidate = {"epg_id": "Ultra.News.us", "display_name": "Ultra News", "region": "US"}
        rows = registry_schedule_aliases([candidate], registry, matcher_normalizer=_normalize_key_v8)
        self.assertNotIn("The Example Channel", [row["alias"] for row in rows])

if __name__ == "__main__": unittest.main()
