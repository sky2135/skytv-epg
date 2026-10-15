from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from provider_event_guide import parse_provider_event


def epoch(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp())


NOW = epoch(2026, 10, 15, 12)


class ProviderEventGuideTests(unittest.TestCase):
    def parse(self, value, **kwargs):
        kwargs.setdefault("reference_epoch", NOW)
        return parse_provider_event(value, **kwargs)

    def test_current_provider_dual_date_labels_corroborate_at_their_precision(self):
        result = self.parse(
            "US (ESPN+ 001) | 2026 Example Cup_ Day 1 (Team Red vs. Team Blue) "
            "Oct 15 7:00AM ET (2026-10-15 07:00:00)"
        )
        self.assertEqual(result.name, "2026 Example Cup Day 1 (Team Red vs. Team Blue)")
        self.assertEqual(result.start_epoch, epoch(2026, 10, 15, 11))
        self.assertEqual(result.timing_status, "scheduled")
        self.assertEqual(result.timing_evidence, "corroborated_provider_datetime")
        self.assertEqual(result.time_label, "Oct 15, 2026 7:00 AM EDT")
        jitter = self.parse(
            "US (ESPN+ 002) | Red vs Blue Oct15 10:00PM ET (2026-10-15 22:00:15)"
        )
        self.assertEqual(jitter.start_epoch, epoch(2026, 10, 16, 2, 0, 15))
        self.assertEqual(jitter.name, "Red vs Blue")
        self.assertIsNone(jitter.explicit_end_epoch)

    def test_conflicting_dates_are_never_turned_into_scheduled_live_windows(self):
        for text in (
            "US (ESPN+ 001) | Red vs Blue Oct 15 7:00AM ET (2026-10-15 08:00:00)",
            "US (ESPN+ 001) | Red vs Blue Oct 15 7:00AM ET (2026-10-15 07:01:00)",
            "PPV EVENT 01: Red vs Blue (2026-10-15 07:00:00 ET) (2026-10-15 07:00:15 ET)",
        ):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertIsNone(result.start_epoch)
                self.assertEqual(result.timing_status, "ambiguous")

    def test_stale_september_name_decodes_to_the_past_not_next_year(self):
        result = self.parse("US (ESPN+ 001) | Red vs Blue Sep 30 10:00AM")
        self.assertEqual(result.start_epoch, epoch(2026, 9, 30, 14))
        self.assertLess(result.start_epoch, NOW)
        self.assertEqual(result.timing_evidence, "inferred_nearby_year")

    def test_yearless_dates_require_a_nearby_year_and_support_new_year(self):
        rollover = self.parse(
            "PPV EVENT 01: New Year Match (01.02 8:00 PM ET)",
            reference_epoch=epoch(2026, 12, 30, 12),
        )
        self.assertEqual(rollover.start_epoch, epoch(2027, 1, 3, 1))
        self.assertEqual(rollover.time_label, "Jan 2, 2027 8:00 PM EST")
        summer = self.parse(
            "PPV EVENT 01: Summer Match (07.15 9:00 AM ET)", reference_epoch=epoch(2026, 7, 1, 12)
        )
        self.assertEqual(summer.start_epoch, epoch(2026, 7, 15, 13))
        distant = self.parse("US (ESPN+ 001) | Red vs Blue Feb 1 7:00PM ET")
        self.assertIsNone(distant.start_epoch)
        self.assertEqual(distant.timing_evidence, "year_unresolved")

    def test_month_names_accept_explicit_year_before_or_after_the_clock(self):
        for label in (
            "Oct 15 2026 7:00PM ET", "Oct 15, 2026 7:00PM ET",
            "2026 Oct 15 7:00PM ET", "Oct 15 7:00PM ET 2026",
        ):
            with self.subTest(label=label):
                result = self.parse("PPV EVENT 01: Red vs Blue " + label)
                self.assertEqual(result.start_epoch, epoch(2026, 10, 15, 23))
                self.assertEqual(result.name, "Red vs Blue")

    def test_utc_and_numeric_offset_clocks_preserve_the_actual_instant(self):
        for label, expected in (
            ("2026-10-15T19:30:15Z", epoch(2026, 10, 15, 19, 30, 15)),
            ("2026-10-15 19:30 UTC", epoch(2026, 10, 15, 19, 30)),
            ("2026-10-15T19:30:00+02:00", epoch(2026, 10, 15, 17, 30)),
            ("2026-10-15T19:30:00-0400", epoch(2026, 10, 15, 23, 30)),
        ):
            with self.subTest(label=label):
                result = self.parse("PPV EVENT 01: Red vs Blue " + label)
                self.assertEqual(result.start_epoch, expected)
                self.assertEqual(result.name, "Red vs Blue")

    def test_naive_clocks_need_a_known_route_or_documented_legacy_format(self):
        unknown = self.parse("AU: NETFLIX PPV 1 - Red vs Blue Oct 15 7:00PM")
        self.assertIsNone(unknown.start_epoch)
        self.assertEqual(unknown.timing_evidence, "unknown_timezone")
        known = self.parse("ESPNPLAY 001 | Red vs Blue Oct 15 7:00PM", family="us_espn_play")
        self.assertEqual(known.start_epoch, epoch(2026, 10, 15, 23))
        legacy = self.parse("PPV EVENT 01: Red vs Blue (2026-10-15 19:00:00)")
        self.assertEqual(legacy.start_epoch, epoch(2026, 10, 15, 23))
        for zone in ("PDT", "JST", "HST", "KST", "SGT", "NZST", "jst", "ET+4", "Asia/Tokyo"):
            with self.subTest(zone=zone):
                unsupported = self.parse("US (ESPN+ 001) | Red vs Blue Oct 15 7:00PM " + zone)
                self.assertIsNone(unsupported.start_epoch)
                unsupported_end = self.parse("US (ESPN+ 001) | Red vs Blue Oct 15 7:00PM-9:00PM " + zone)
                self.assertIsNone(unsupported_end.start_epoch)
                self.assertIsNone(unsupported_end.explicit_end_epoch)
        outside_legacy = self.parse("PPV EVENT 01: Red vs Blue (2026-10-15 19:00:00) JST")
        self.assertIsNone(outside_legacy.start_epoch)

    def test_nonexistent_and_ambiguous_eastern_times_are_rejected(self):
        cases = (
            ("2026-03-08 02:30:00 ET", "invalid"),
            ("2026-11-01 01:30:00 ET", "ambiguous"),
            ("2026-07-15 09:00:00 EST", "invalid"),
            ("2026-12-15 09:00:00 EDT", "invalid"),
        )
        for stamp, status in cases:
            with self.subTest(stamp=stamp):
                result = self.parse("PPV EVENT 01: Red vs Blue (" + stamp + ")")
                self.assertIsNone(result.start_epoch)
                self.assertEqual(result.timing_status, status)

    def test_explicit_est_and_edt_disambiguate_fall_back_hour(self):
        edt = self.parse("PPV EVENT 01: Red vs Blue (2026-11-01 01:30:00 EDT)")
        est = self.parse("PPV EVENT 01: Red vs Blue (2026-11-01 01:30:00 EST)")
        self.assertEqual(edt.start_epoch, epoch(2026, 11, 1, 5, 30))
        self.assertEqual(est.start_epoch, epoch(2026, 11, 1, 6, 30))

    def test_explicit_end_points_are_retained_without_a_guessed_duration(self):
        full = self.parse("PPV EVENT 01: Red vs Blue (2026-10-15T19:00Z) to (2026-10-15T22:00Z)")
        self.assertEqual(full.start_epoch, epoch(2026, 10, 15, 19))
        self.assertEqual(full.explicit_end_epoch, epoch(2026, 10, 15, 22))
        self.assertEqual(full.name, "Red vs Blue")
        clock = self.parse("US (ESPN+ 001) | Red vs Blue Oct 15 11:00PM-1:00AM ET")
        self.assertEqual(clock.start_epoch, epoch(2026, 10, 16, 3))
        self.assertEqual(clock.explicit_end_epoch, epoch(2026, 10, 16, 5))
        self.assertEqual(clock.name, "Red vs Blue")
        single = self.parse("PPV EVENT 01: Red vs Blue (2026-10-15 19:00:00 ET)")
        self.assertIsNone(single.explicit_end_epoch)

    def test_invalid_calendar_offset_or_range_never_gets_an_epoch(self):
        for label in (
            "(2026-02-30 19:00:00 ET)", "(2026-10-15 25:00:00 ET)",
            "(10.15 13:00PM ET)", "2026-10-15T19:30:00+25:00",
            "(2026-10-15 07:000 ET)", "(2026-10-15 07:00:001 ET)",
            "(2098-01-01 00:00:00)",
            "(2026-10-15T19:00Z) to (2026-10-15T18:00Z)",
            "(2026-10-15T19:00Z) to (2026-10-18T19:00Z)",
        ):
            with self.subTest(label=label):
                result = self.parse("PPV EVENT 01: Red vs Blue " + label)
                self.assertIsNone(result.start_epoch)

    def test_provider_off_air_no_event_and_blank_states_override_old_dates(self):
        for payload, status, expected in (
            ("No Event Scheduled", "no_event", "No Event Scheduled"),
            ("OFF AIR", "off_air", "Off Air"),
            ("Coming Soon", "tba", "Event To Be Announced"),
            ("", "tba", "Event To Be Announced"),
        ):
            with self.subTest(payload=payload):
                result = self.parse("US (ESPN+ 001) | " + payload + " (2026-10-15 07:00:00)")
                self.assertEqual(result.timing_status, status)
                self.assertEqual(result.name, expected)
                self.assertIsNone(result.start_epoch)
        self.assertEqual(self.parse(None).timing_status, "tba")

    def test_slot_and_quality_cleanup_preserves_event_identity(self):
        for label in (
            "US (ESPN 003) | Red vs Blue (HD)", "US (ESPNPLAY 003) | Red vs Blue FHD",
            "US ESPNPLAY 003 | Red vs Blue HD", "(FLSP 003) | Red vs Blue HD",
            "USA - FLO 3: Red vs Blue HD", "NFL | 03 - Red vs Blue HD",
            "US: DAZN PPV 3 - Red vs Blue HD", "PPV EVENT 03: Red vs Blue HD",
        ):
            with self.subTest(label=label):
                self.assertEqual(self.parse(label).name, "Red vs Blue")

    def test_controls_and_size_bounds_fail_closed_without_poisoning_output(self):
        result = self.parse("US (ESPN+ 001) | Red\x00 vs Blue Oct 15 7:00PM ET")
        self.assertNotIn("\x00", result.name)
        self.assertIsNone(result.start_epoch)
        self.assertEqual(result.timing_evidence, "control_characters")
        clean_spaces = self.parse("US (ESPN+ 001) | Red\n vs\t Blue Oct 15 7:00PM ET")
        self.assertEqual(clean_spaces.start_epoch, epoch(2026, 10, 15, 23))
        self.assertEqual(self.parse("A" * 2049).timing_status, "invalid")
        for unsafe in ("\ud800", "\uffff", "\u202e"):
            with self.subTest(unsafe=repr(unsafe)):
                unsafe_result = self.parse("US (ESPN+ 001) | Red" + unsafe + " vs Blue Oct 15 7:00PM ET")
                self.assertIsNone(unsafe_result.start_epoch)
                self.assertNotIn(unsafe, unsafe_result.name)
        too_many = self.parse("Match " + " (2026-10-15 19:00:00 ET)" * 5)
        self.assertIsNone(too_many.start_epoch)
        self.assertEqual(too_many.timing_status, "ambiguous")

    def test_parenthesized_us_espn_play_bank_prefix_and_bare_slots(self):
        result = self.parse("(US) ESPN PLAY 1 | Red vs Blue Oct15 7:00PM ET", family="us_espn_play")
        self.assertEqual(result.name, "Red vs Blue")
        self.assertEqual(result.start_epoch, epoch(2026, 10, 15, 23))
        bare = self.parse("(US) ESPN PLAY1", family="us_espn_play")
        self.assertEqual(bare.timing_status, "tba")
        self.assertEqual(bare.name, "Event To Be Announced")

    def test_australian_espn_play_accepts_explicit_utc_but_never_assumes_eastern(self):
        result = self.parse("(AU) ESPN PLAY 1 | Red vs Blue 2026-10-15T19:00:00Z", family="au_espn_play")
        self.assertEqual(result.name, "Red vs Blue")
        self.assertEqual(result.start_epoch, epoch(2026, 10, 15, 19))
        for date in ("Oct15 7:00PM", "(2026-10-15 19:00:00)"):
            with self.subTest(date=date):
                naive = self.parse("(AU) ESPN PLAY1 | Red vs Blue " + date, family="au_espn_play")
                self.assertIsNone(naive.start_epoch)
                self.assertEqual(naive.timing_evidence, "unknown_timezone")

    def test_linear_espn_names_are_not_removed_as_play_bank_prefixes(self):
        for name in ("ESPN", "ESPN2", "(US) ESPN 2", "(AU) ESPN", "(US) ESPN PLAY 1000", "(US) ESPN PLAY 0"):
            with self.subTest(name=name):
                self.assertEqual(self.parse(name).name, name)

    def test_explicit_start_marker_is_removed_with_its_date_only(self):
        result = self.parse(
            "MLB01: Team Red vs Team Blue start:2026-09-30 19:00:00",
            family="league_event", category="USA | MLB",
        )
        self.assertEqual(result.name, "Team Red vs Team Blue")
        self.assertEqual(result.start_epoch, epoch(2026, 9, 30, 23))
        self.assertLess(result.start_epoch, NOW)
        self.assertEqual(
            self.parse("PPV EVENT 01: Fresh Start (2026-10-15 19:00:00 ET)").name,
            "Fresh Start",
        )
        self.assertEqual(self.parse("PPV EVENT 01: Game start: time TBA").timing_status, "tba")

    def test_labeled_mlb_start_stop_ranges_preserve_both_exact_endpoints(self):
        cases = (
            (
                "MLB 02 : Brewers x Dodgers Start:2026-10-16 01:00:00 stop 2026-10-16 08:13:20",
                "Brewers x Dodgers", epoch(2026, 10, 16, 5), epoch(2026, 10, 16, 12, 13, 20),
            ),
            (
                "MLB 02 | Dodgers x Phillies start:2026-07-21 23:40:00 stop:2026-07-22 06:53:20",
                "Dodgers x Phillies", epoch(2026, 7, 22, 3, 40), epoch(2026, 7, 22, 10, 53, 20),
            ),
            (
                "MLB02| Red x Blue Start:2026-10-16T01:00:00Z END:2026-10-16T08:13:20Z",
                "Red x Blue", epoch(2026, 10, 16, 1), epoch(2026, 10, 16, 8, 13, 20),
            ),
        )
        for label, name, start, end in cases:
            with self.subTest(label=label):
                result = self.parse(label, family="league_event", category="|NA| USA MLB")
                self.assertEqual(result.name, name)
                self.assertEqual(result.start_epoch, start)
                self.assertEqual(result.explicit_end_epoch, end)
                self.assertEqual(result.timing_status, "scheduled")

    def test_labeled_ranges_reject_invalid_endpoints_and_remove_stop_labels(self):
        for start, end in (
            ("2026-10-16 08:00:00", "2026-10-16 01:00:00"),
            ("2026-10-16 01:00:00", "2026-10-16 01:00:00"),
            ("2026-10-16 01:00:00", "2026-10-18 01:00:01"),
            ("2026-10-16 01:00:00", "2026-10-16 25:00:00"),
        ):
            with self.subTest(start=start, end=end):
                result = self.parse(
                    f"MLB 02 | Red x Blue start:{start} stop:{end}",
                    family="league_event", category="|NA| USA MLB",
                )
                self.assertEqual(result.name, "Red x Blue")
                self.assertEqual(result.timing_status, "invalid")
                self.assertIsNone(result.start_epoch)
                self.assertIsNone(result.explicit_end_epoch)
        boundary = self.parse(
            "MLB02| Red x Blue start:2026-10-16 01:00:00 end:2026-10-18 01:00:00",
            family="league_event", category="|NA| USA MLB",
        )
        self.assertEqual(boundary.explicit_end_epoch - boundary.start_epoch, 48 * 3600)

    def test_labeled_ranges_never_override_unknown_zones_or_conflicting_dates(self):
        for label in (
            "MLB02| Red x Blue start:2026-10-16 01:00:00 PST stop:2026-10-16 08:00:00",
            "MLB02| Red x Blue start:2026-10-16 01:00:00 stop:2026-10-16 08:00:00 JST",
            "MLB02| Red x Blue start:2026-10-16 01:00:00 stop:2026-10-16 08:00:00 (2026-10-16 09:00:00)",
        ):
            with self.subTest(label=label):
                result = self.parse(label, family="league_event", category="|NA| USA MLB")
                self.assertIsNone(result.start_epoch)
                self.assertIsNone(result.explicit_end_epoch)
                self.assertNotRegex(result.name.casefold(), r"\b(?:start|stop|end)\s*:?")
        unmarked = self.parse(
            "MLB02| Red x Blue 2026-10-16 01:00:00 stop:2026-10-16 08:00:00",
            family="league_event", category="|NA| USA MLB",
        )
        self.assertIsNone(unmarked.start_epoch)
        unknown = self.parse(
            "AU: Red x Blue start:2026-10-16 01:00:00 stop:2026-10-16 08:00:00"
        )
        self.assertIsNone(unknown.start_epoch)
        self.assertEqual(unknown.timing_evidence, "unknown_timezone")

    def test_mixed_case_unsupported_zones_reject_both_labeled_range_endpoints(self):
        for zone in ("Jst", "Hst", "Kst", "Sgt", "Hkt", "Nzst", "Nzdt", "Akst", "Akdt", "Acst", "Acdt", "Awst"):
            for endpoint in ("start", "stop"):
                with self.subTest(zone=zone, endpoint=endpoint):
                    start_zone = f" {zone}" if endpoint == "start" else ""
                    stop_zone = f" {zone}" if endpoint == "stop" else ""
                    result = self.parse(
                        f"MLB02| Red x Blue start:2026-10-16 01:00:00{start_zone} "
                        f"stop:2026-10-16 08:00:00{stop_zone}",
                        family="league_event", category="|NA| USA MLB",
                    )
                    self.assertIsNone(result.start_epoch)
                    self.assertIsNone(result.explicit_end_epoch)
                    self.assertEqual(result.timing_evidence, "unknown_timezone")

    def test_nfl_clock_without_a_date_never_gets_a_scheduled_epoch(self):
        result = self.parse("NFL |01-TNF 8:15pm Eagles at Bears", family="league_event", category="NFL")
        self.assertEqual(result.name, "TNF 8:15pm Eagles at Bears")
        self.assertEqual(result.timing_status, "unscheduled")
        self.assertIsNone(result.start_epoch)
        self.assertIsNone(result.explicit_end_epoch)

    def test_unnumbered_summer_league_day_without_year_or_clock_stays_unscheduled(self):
        result = self.parse(
            "NBA Summer League Mavericks vs Thunder jul 16",
            family="league_event", category="NBA",
        )
        self.assertEqual(result.name, "NBA Summer League Mavericks vs Thunder jul 16")
        self.assertEqual(result.timing_status, "unscheduled")
        self.assertIsNone(result.start_epoch)

    def test_flsp_without_pipe_keeps_source_event_name_and_explicit_date(self):
        result = self.parse(
            "(FLSP 001) bikes: Tour of Example (2026-10-15 12:00:00)",
            family="flosports", category="FLOSPORTS",
        )
        self.assertEqual(result.name, "bikes: Tour of Example")
        self.assertEqual(result.start_epoch, epoch(2026, 10, 15, 16))


if __name__ == "__main__":
    unittest.main()
