import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verified_guide_connections import (
    connection_for_row, reviewed_connection, select_usable_connection,
    wanted_connection_ids, selection_connection_ids,
)


def row(**changes):
    values = dict(
        action="AUTO_DUMMY", enabled=True, reason="", channel_name="UK || SKY SPORTS CRICKET",
        category_name="SPORTS | SKY UK 4K", metadata=SimpleNamespace(countries=["GB"]),
    )
    values.update(changes)
    return SimpleNamespace(**values)


class VerifiedGuideConnectionsTest(unittest.TestCase):
    def test_screenshot_channel_families_have_distinct_reviewed_identities(self):
        families = ["NEWS", "MAIN EVENT", "PREMIER LEAGUE", "FOOTBALL", "CRICKET", "ACTION", "PLUS", "RACING"]
        connections = [reviewed_connection(f"UK || SKY SPORTS {family}") for family in families]
        self.assertTrue(all(connections))
        self.assertEqual(8, len({connection.identity for connection in connections}))

    def test_f1_linear_channel_is_distinct_from_event_cameras(self):
        connection = reviewed_connection("UK: SKY SPORTS F1 HD")
        self.assertEqual("SkySp.F1.HD.uk", connection.candidate_ids[0])
        for name in ["(F1TV 02) | F1 LIVE", "UK: SKY SPORTS F1 HD BACKUP", "UK: SKY SPORTS F1 2"]:
            self.assertIsNone(reviewed_connection(name, "SPORTS | F1 MOTOGP", ["GB"]))

    def test_plus_never_aliases_4seven_or_an_unrelated_sports_feed(self):
        plus = reviewed_connection("UK || SKY SPORTS PLUS")
        self.assertEqual(("SkySp+HD.uk", "SkySp+.uk"), plus.candidate_ids)
        self.assertIsNone(select_usable_connection(plus, {"4seven.uk", "SkySp.Mix.HD.uk", "SkySpMainEvHD.uk"}))
        self.assertEqual(plus, reviewed_connection("Sky Sports+ HD", countries=["GB"]))

    def test_action_does_not_guess_a_seasonal_nfl_rename(self):
        action = reviewed_connection("UK || SKY SPORTS ACTION")
        self.assertIsNone(select_usable_connection(action, {"Sky.Sports.NFL.uk"}))
        self.assertEqual("SkySp.Action.uk", select_usable_connection(action, {"SkySp.Action.uk"}))

    def test_plain_name_needs_positive_uk_evidence_and_foreign_country_vetoes(self):
        self.assertIsNone(reviewed_connection("Sky Sports News HD"))
        self.assertIsNotNone(reviewed_connection("Sky Sports News HD", "SPORTS | UK SPORTS"))
        self.assertIsNotNone(reviewed_connection("Sky Sports News HD", countries=["GB"]))
        self.assertIsNone(reviewed_connection("UK || SKY SPORTS NEWS", countries=["DE"]))
        self.assertIsNone(reviewed_connection("UK || SKY SPORTS NEWS", countries=["GB", "IE"]))
        for name in ["DE: SKY SPORTS NEWS", "IT - SKY SPORT NEWS", "IN: SKY SPORTS NEWS"]:
            self.assertIsNone(reviewed_connection(name, "SPORTS | UK SPORTS", ["GB"]))

    def test_quality_suffixes_are_limited_to_same_linear_identity(self):
        for name in ["UK FHD : Sky Sports News", "Sky Sports News (FHD)", "Sky Sports News SD Hevc", "Sky Sports News UHD [UK]"]:
            self.assertIsNotNone(reviewed_connection(name, countries=["GB"]), name)
        for name in ["UK || SKY SPORTS PLUS 1", "UK || SKY SPORTS NEWS +1", "UK || SKY SPORTS ULTRA HD 1", "UK || SKY SPORTS NEWS IE", "UK || SKY SPORTS NEWS LOCAL"]:
            self.assertIsNone(reviewed_connection(name), name)

    def test_control_markup_and_overlong_names_cannot_become_connections(self):
        for name in ["UK || SKY SPORTS NEWS\n", "UK || SKY SPORTS NEWS\x00", "<Sky Sports News>", "UK || SKY SPORTS NEWS" + " " * 400]:
            self.assertIsNone(reviewed_connection(name))

    def test_explicit_decisions_manual_and_quarantine_are_never_overridden(self):
        for action in ["SKIP", "IGNORE", "REJECTED", "MANUAL"]:
            self.assertIsNone(connection_for_row(row(action=action), allow_review=True))
        self.assertIsNone(connection_for_row(row(reason="Effective snapshot quarantine: OPEN Sync Alerts stream-ID reuse review"), allow_review=True))
        self.assertIsNone(connection_for_row(row(enabled=False)))
        for note in ["Manual reuse review required", "Manual mapping: keep this guide", "Do not remap this channel",
                     "OPEN Sync Alerts stream-ID reuse review", "Provider identity changed", "Guide is locked"]:
            self.assertIsNone(connection_for_row(row(notes=note), allow_review=True), note)
        for reason in [
            "Effective snapshot quarantine: provider identity or schema review; the Google Sheet Mappings row was not changed.",
            "Effective snapshot quarantine: OPEN Sync Alerts stream-ID reuse review; the Google Sheet Mappings row was not changed.",
        ]:
            self.assertIsNone(connection_for_row(row(reason=reason), allow_review=True))

    def test_review_requires_caller_to_bind_current_provider_identity(self):
        unresolved = row(enabled=False, action="REVIEW")
        self.assertIsNone(connection_for_row(unresolved))
        self.assertIsNotNone(connection_for_row(unresolved, allow_review=True))
        self.assertIsNone(connection_for_row(row(enabled=False, action="APPROVED"), allow_review=True))

    def test_missing_or_expired_preferred_id_uses_only_a_gated_same_feed_alternative(self):
        cricket = reviewed_connection("UK || SKY SPORTS CRICKET")
        self.assertIsNone(select_usable_connection(cricket, set()))
        self.assertEqual("SkySp.Cricket.uk", select_usable_connection(cricket, {"SkySp.Cricket.uk"}))
        racing = reviewed_connection("UK || SKY SPORTS RACING")
        self.assertEqual("SkySp.Racing.HD.uk", select_usable_connection(racing, {"SkySp.Racing.HD.uk"}))

    def test_reservation_is_bounded_and_deduplicated_and_excludes_rejected_rows(self):
        rows = [row(), row(), row(action="IGNORE", channel_name="UK || SKY SPORTS NEWS")]
        self.assertEqual({"SkySpCricket.HD.uk", "SkySp.Cricket.uk"}, wanted_connection_ids(rows))

    def test_raw_sheet_rows_reserve_the_same_candidates_as_mapping_rows(self):
        raw = dict(action="AUTO_DUMMY", enabled="TRUE", reason="", channel_name="Sky Sports Cricket HD",
                   category_name="SPORTS", country_codes="GB")
        connection = connection_for_row(raw)
        self.assertEqual("cricket", connection.family)
        self.assertEqual("sky_sports_cricket_uk", connection.key)
        self.assertEqual(wanted_connection_ids([row()]), wanted_connection_ids([raw]))
        self.assertIsNone(connection_for_row({**raw, "enabled": "FALSE"}))
        self.assertIsNone(connection_for_row({**raw, "country_codes": "DE"}))
        self.assertIsNone(connection_for_row({**raw, "country_codes": "GB|DE"}))
        self.assertIsNone(connection_for_row({**raw, "reason": "Effective snapshot quarantine: provider identity review"}))

    def test_raw_review_rows_need_explicit_identity_verified_mode(self):
        raw = dict(action="REVIEW", enabled="FALSE", reason="", channel_name="UK || SKY SPORTS NEWS")
        self.assertEqual(set(), wanted_connection_ids([raw]))
        self.assertEqual({"SkySp.News.HD.uk"}, wanted_connection_ids([raw], allow_review=True))

    def test_selection_only_reserves_later_enriched_plain_name_without_approval(self):
        raw = dict(action="AUTO_DUMMY", enabled="TRUE", channel_name="Sky Sports F1 HD", category_name="SPORTS")
        self.assertIsNone(connection_for_row(raw))
        self.assertEqual(set(), wanted_connection_ids([raw]))
        self.assertEqual({"SkySp.F1.HD.uk", "SkySp.F1.uk"}, selection_connection_ids([raw]))
        enriched = {**raw, "country_codes": "GB"}
        self.assertEqual(wanted_connection_ids([enriched]), selection_connection_ids([raw]))
        self.assertIsNone(connection_for_row(raw))

    def test_selection_only_cannot_reserve_foreign_or_unrelated_channels(self):
        raw = dict(action="REVIEW", enabled="FALSE", channel_name="Sky Sports News HD", category_name="SPORTS")
        for changes in [
            {"country_codes": "DE"}, {"category_name": "EUROPE | GERMANY"},
            {"channel_name": "IT - SKY SPORTS NEWS"}, {"action": "IGNORE"},
            {"reason": "Effective snapshot quarantine: provider identity review"},
            {"channel_name": "(F1TV 02) | F1 LIVE"}, {"channel_name": "UK || SKY SPORTS PLUS 1"},
        ]:
            self.assertEqual(set(), selection_connection_ids([{**raw, **changes}], allow_review=True), changes)
        self.assertEqual({"SkySp.News.HD.uk"}, selection_connection_ids([raw], allow_review=True))
        self.assertEqual(set(), selection_connection_ids([raw]))


if __name__ == "__main__":
    unittest.main()
