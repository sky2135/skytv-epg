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

    def test_sony_screenshot_aliases_keep_numbered_india_feeds_separate(self):
        cases = [
            ("CRIC || SONY SPORTS 1 HD", "Sony.Ten.1.HD.in", "en"),
            ("CRIC || SONY SPORTS 2 ᴴᴰ", "Sony.Ten.2.HD.in", "en"),
            ("CRIC || SONY SPORTS 3 HD", "Sony.Ten.3.HD.Hindi.in", "hi"),
            ("CRIC || SONY SPORTS 4 TAMIL", "Sony.Ten.4.HD.Tamil.in", "ta"),
            ("CRI: SONY SPORTS TEN 4 HD TELUGU", "Sony.Ten.4.HD.Telugu.in", "te"),
            ("CRIC || SONY SPORTS 5 FHD", "Sony.Ten.5.HD.in", "en"),
        ]
        identities = set()
        for name, preferred, language in cases:
            with self.subTest(name=name):
                connection = reviewed_connection(name, "SPORTS | CRICKET")
                self.assertIsNotNone(connection)
                self.assertEqual(connection.country, "IN")
                self.assertEqual(connection.languages, (language,))
                self.assertEqual(connection.candidate_ids[0], preferred)
                identities.add(connection.identity)
        self.assertEqual(len(identities), 6)

    def test_sony_aliases_cover_old_ten_and_current_sports_ten_spelling(self):
        expected = reviewed_connection("CRIC || SONY SPORTS 1 HD")
        for name in ["Sony Ten 1", "Sony Ten1 FHD", "Sony Sports Ten 1 HD",
                     "IN: Sony Sports 1 (HD)", "IN | CRIC || Sony Sports Ten1 English FHD",
                     "Sony Sports 1 HD [IN]", "SonySportsTen1FHD"]:
            self.assertEqual(expected, reviewed_connection(name), name)
        hindi = reviewed_connection("Sony Sports 3 HD")
        self.assertEqual(hindi, reviewed_connection("Sony Ten 3 (Hindi) FHD"))
        self.assertEqual(hindi, reviewed_connection("IN: Sony Sports Ten 3 HD Hindi"))

    def test_sony_language_editions_and_missing_language_never_cross(self):
        tamil = reviewed_connection("Sony Sports 4 Tamil HD")
        telugu = reviewed_connection("Sony Sports 4 Telugu HD")
        self.assertNotEqual(tamil, telugu)
        self.assertTrue(set(tamil.candidate_ids).isdisjoint(telugu.candidate_ids))
        for name in ["Sony Sports 4 HD", "Sony Ten4", "Sony Sports4FHD",
                     "Sony Sports 4 Hindi", "Sony Sports 4 English", "Sony Sports 4 Tamil Telugu",
                     "Sony Sports 3 English", "Sony Sports 1 Hindi", "Sony Sports 5 Hindi"]:
            self.assertIsNone(reviewed_connection(name), name)

    def test_sony_old_espn_and_star_first_are_not_numbered_or_other_brand_aliases(self):
        for name in ["CRI: SONY ESPN HD", "Sony ESPN", "CRIC || STAR SPORTS FIRST",
                     "Star Sports First", "Star Sports Khel", "Star Sports 2 Kannada",
                     "Sony Sports 10", "Sony Sports 1 +1", "Sony Sports 1 Backup",
                     "Sony Sports 1 EXTRA", "Sony Sports Ten 1 2", "Sony Sports 1 HD (Pakistan)"]:
            self.assertIsNone(reviewed_connection(name, "SPORTS | CRICKET"), name)

    def test_sony_india_unique_brand_allows_unknown_market_but_vetoes_foreign(self):
        self.assertIsNotNone(reviewed_connection("Sony Sports Ten 1 HD"))
        self.assertIsNotNone(reviewed_connection("Sony Sports Ten 1 HD", countries=["unknown"]))
        self.assertIsNotNone(reviewed_connection("Sony Sports Ten 1 HD", "INDIA | SPORTS", ["IN"]))
        for countries in [["GB"], ["SG"], ["PK"], ["IN", "GB"]]:
            self.assertIsNone(reviewed_connection("Sony Sports Ten 1 HD", countries=countries), countries)
        for category in ["UK | SPORTS", "USA | ASIAN", "SINGAPORE | SPORTS", "PAKISTAN | SPORTS", "JAPAN | SPORTS"]:
            self.assertIsNone(reviewed_connection("Sony Sports Ten 1 HD", category), category)
        for name in ["UK: Sony Sports 1 HD", "PK | Sony Ten 1", "SG: Sony Sports Ten 1"]:
            self.assertIsNone(reviewed_connection(name), name)

    def test_sony_alternate_ids_keep_the_observed_feed_and_language(self):
        connection = reviewed_connection("Sony Sports 3 HD")
        self.assertEqual(connection.epg_feed, "IN1")
        self.assertEqual(connection.feed_for_id("Sony.Ten.3.HD.Hindi.in"), "IN1")
        fallback = select_usable_connection(connection, {"SONY.SPORTS.TEN.3.HD.in"})
        self.assertEqual(fallback, "SONY.SPORTS.TEN.3.HD.in")
        self.assertEqual(connection.feed_for_id(fallback), "IN4")
        self.assertIsNone(select_usable_connection(connection, {"Sony.Ten.4.HD.Tamil.in"}))

    def test_market_specific_reservations_do_not_assume_gb_for_sony_or_hub(self):
        sony = dict(action="AUTO_DUMMY", enabled="TRUE", channel_name="CRIC || SONY SPORTS 3 HD", category_name="SPORTS | CRICKET")
        hub = {**sony, "channel_name": "Hub Sports 4 FHD"}
        for raw in [sony, hub]:
            connection = connection_for_row(raw)
            self.assertIsNotNone(connection)
            self.assertEqual(wanted_connection_ids([raw]), selection_connection_ids([raw]))
            self.assertEqual(set(connection.candidate_ids), selection_connection_ids([raw]))
        self.assertEqual(connection_for_row(sony).country, "IN")
        self.assertEqual(connection_for_row(hub).country, "SG")

    def test_new_market_rules_preserve_review_and_exclusion_gates(self):
        for name in ["Sony Sports 1 HD", "Hub Sports 4 FHD"]:
            raw = dict(action="REVIEW", enabled="FALSE", channel_name=name, category_name="SPORTS | CRICKET")
            self.assertIsNone(connection_for_row(raw))
            self.assertIsNotNone(connection_for_row(raw, allow_review=True))
            for changes in [{"action": "IGNORE"}, {"action": "MANUAL"},
                            {"reason": "Effective snapshot quarantine: provider identity review"},
                            {"notes": "Manual mapping: keep this guide"}]:
                self.assertIsNone(connection_for_row({**raw, **changes}, allow_review=True), (name, changes))

    def test_hub_sports4_is_exact_singapore_identity_without_audio_guess(self):
        expected = reviewed_connection("Hub Sports 4 FHD", "SPORTS | CRICKET")
        self.assertIsNotNone(expected)
        self.assertEqual(expected.country, "SG")
        self.assertEqual(expected.languages, ())
        self.assertEqual(expected.epg_feed, "SG1")
        self.assertEqual(expected.candidate_ids, ("Hub.Sports.4.HD.sg",))
        for name in ["Hub Sports 4", "SG: Hub Sports4HD", "Singapore | Hub Sports 4 (FHD)",
                     "SG FHD : Hub Sports 4", "Hub Sports 4 [SG]", "HubSports4HD"]:
            self.assertEqual(expected, reviewed_connection(name), name)

    def test_hub_sports4_never_aliases_premier_or_numbered_alternate(self):
        for name in ["Hub Premier 4", "Hub Premier Sports 4", "Hub Sports Premier 4",
                     "Hub Sports 1", "Hub Sports 41", "Hub Sports 4 +1", "Hub Sports 4 Backup",
                     "Hub Sports 4 1", "Hub Sports 4 English", "IN: Hub Sports 4"]:
            self.assertIsNone(reviewed_connection(name), name)
        for countries in [["GB"], ["IN"], ["MY"], ["SG", "IN"]]:
            self.assertIsNone(reviewed_connection("Hub Sports 4", countries=countries), countries)
        for category in ["INDIA | SPORTS", "UK | SPORTS", "USA | ASIAN", "MALAYSIA | SPORTS"]:
            self.assertIsNone(reviewed_connection("Hub Sports 4", category), category)
        self.assertIsNotNone(reviewed_connection("Hub Sports 4", "SINGAPORE | SPORTS", ["SG"]))
        self.assertIsNone(reviewed_connection("Sky Sports News HD", "SINGAPORE | SPORTS", ["GB"]))


if __name__ == "__main__":
    unittest.main()
