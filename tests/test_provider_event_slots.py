from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provider_event_slots as slots


class ProviderEventSlotTests(unittest.TestCase):
    def test_flsp_without_pipe_requires_its_matching_event_bank(self):
        name = "(FLSP 001) bikes: Tour of Example (2026-10-15 12:00:00)"
        for category in ("SPORTS | FLO", "|NA| USA FLOSPORTS", "FLSP Events"):
            with self.subTest(category=category):
                self.assertEqual(slots.event_slot_identity(name, category), ("flosports", "001"))
        for category in ("", "General", "USA Sports", "Flow Sports"):
            with self.subTest(category=category):
                self.assertIsNone(slots.event_slot_identity(name, category))

    def test_flsp_pipe_and_no_pipe_are_same_slot_only_in_unchanged_category(self):
        old = "(FLSP 001) | Old Event"
        new = "(FLSP 001) bikes: New Event"
        self.assertTrue(slots.is_same_event_slot(old, new, "FLOSPORTS", "FLOSPORTS"))
        self.assertFalse(slots.is_same_event_slot(old, new, "FLOSPORTS", "FLO"))
        self.assertFalse(slots.is_same_event_slot(old, "(FLSP 002) bikes: New Event", "FLOSPORTS", "FLOSPORTS"))
        self.assertEqual(slots.event_slot_identity("(FLSP 1000) | Existing format"), ("flosports", "1000"))

    def test_new_flsp_form_remains_anchored_numbered_and_bounded(self):
        for name in (
            "(FLSP 000) Event", "(FLSP 1) Event", "(FLSP 1000) Event",
            "Ordinary (FLSP 001) Event", "(FLO 001) Event", "(FLSP 001) " + "A" * 301,
            "(FLSP 001) Unsafe\x00 Event", "Flow Sports 1", "(FLSP 001)",
        ):
            with self.subTest(name=name[:60]):
                self.assertIsNone(slots.event_slot_identity(name, "FLOSPORTS"))

    def test_compact_mlb_requires_matching_category_and_retains_slot_identity(self):
        compact = "MLB01: Team Red vs Team Blue start:2026-09-30 19:00:00"
        self.assertEqual(slots.event_slot_identity(compact, "|NA| USA MLB"), ("league_event", "mlb:01"))
        self.assertTrue(slots.is_same_event_slot("MLB 01: Old Game", compact, "MLB", "MLB"))
        for category in ("", "NBA", "USA Sports", "MLBTV"):
            with self.subTest(category=category):
                self.assertIsNone(slots.event_slot_identity(compact, category))
        for name in ("MLB TV", "MLB Network", "NBA01: Game", "MLB0010: Game", "MLB00: Game"):
            with self.subTest(name=name):
                self.assertIsNone(slots.event_slot_identity(name, "MLB"))

    def test_unnumbered_nba_matchup_is_descriptive_without_a_stable_identity(self):
        for separator in ("vs", "vs.", "versus", "at"):
            name = f"NBA Summer League Mavericks {separator} Thunder jul 16"
            with self.subTest(separator=separator):
                self.assertEqual(slots.descriptive_event_family(name, "NBA"), "league_event")
                self.assertIsNone(slots.event_slot_identity(name, "NBA"))
                self.assertFalse(slots.is_same_event_slot(name, name, "NBA", "NBA"))
        name = "NBA Summer League Mavericks vs Thunder jul 16"
        self.assertEqual(slots.descriptive_event_family(name, "USA Sports"), "")

    def test_descriptive_matchup_rejects_linear_channels_and_unrelated_text(self):
        for name in (
            "NBA TV", "NBA League Pass", "NBA Summer League", "NBA Summer League Highlights",
            "NBA Mavericks vs Thunder", "Special NBA Summer League Mavericks vs Thunder",
            "NBA Summer League Mavericks vs", "NBA Summer League Mavericks vs Thunder\x00",
            "NBA Summer League " + "A" * 301 + " vs Thunder",
        ):
            with self.subTest(name=name[:60]):
                self.assertEqual(slots.descriptive_event_family(name, "NBA"), "")


    def test_exact_descriptive_presentation_does_not_authorize_game_rotation(self):
        name = "NBA Summer League Mavericks vs Thunder jul 16"
        other = "NBA Summer League Mavericks vs Bulls jul 17"
        self.assertTrue(slots.is_same_event_presentation(name, name, "NBA", "NBA"))
        self.assertFalse(slots.is_same_event_presentation(name, other, "NBA", "NBA"))
        self.assertFalse(slots.is_same_event_presentation(name, name, "NBA", "Sports"))
        self.assertFalse(slots.is_same_event_slot(name, name, "NBA", "NBA"))
        self.assertIsNone(slots.event_slot_identity(name, "NBA"))
        self.assertFalse(slots.is_same_event_presentation("NBA TV", "NBA TV", "NBA", "NBA"))

    def test_historical_mlb_pipe_and_current_colon_share_the_exact_slot(self):
        category = "|NA| USA MLB"
        old = "MLB 02 | Team Red x Team Blue start:2026-07-21 23:40:00 stop:2026-07-22 06:53:20"
        new = "MLB 02 : Team Green x Team Gold Start:2026-10-16 01:00:00 stop2026-10-16 08:13:20"
        compact = "MLB02| Team Green x Team Gold"
        for name in (old, new, compact):
            with self.subTest(name=name):
                self.assertEqual(slots.event_slot_identity(name, category), ("league_event", "mlb:02"))
        self.assertTrue(slots.is_same_event_slot(old, new, category, category))
        self.assertTrue(slots.is_same_event_slot(old, compact, category, category))

    def test_pipe_slot_identity_still_rejects_different_brand_slot_or_category(self):
        original = "MLB 02 | Old Game"
        category = "|NA| USA MLB"
        for name, new_category in (
            ("MLB 01 | New Game", category),
            ("NFL 02 | New Game", category),
            ("MLB 02 | New Game", "|NA| USA NBA"),
            ("MLB 02 | New Game", "MLB"),
            ("MLB 02 | New Game", ""),
        ):
            with self.subTest(name=name, category=new_category):
                self.assertFalse(slots.is_same_event_slot(original, name, category, new_category))
        for wrong_category in ("", "Sports", "NBA", "MLBTV"):
            with self.subTest(category=wrong_category):
                self.assertIsNone(slots.event_slot_identity(original, wrong_category))

    def test_pipe_does_not_turn_linear_or_unanchored_names_into_slots(self):
        for name in (
            "MLB Network | 02", "MLB TV | 02", "MLBTV02| Game", "Special MLB02| Game",
            "MLB 02 Game", "MLB|02 Game", "MLB2| Game", "MLB0020| Game",
        ):
            with self.subTest(name=name):
                self.assertIsNone(slots.event_slot_identity(name, "MLB"))


if __name__ == "__main__":
    unittest.main()
