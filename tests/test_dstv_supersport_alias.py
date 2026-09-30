from __future__ import annotations

import unittest

from matching_lab.compat import parse_channel_context_v8
from matching_lab.normalization import NameViews
from matching_lab.retrieval import repository_curated_alias_targets
from matching_lab.validation import _repository_alias_validation_engine


class DstvSuperSportAliasTests(unittest.TestCase):
    def test_official_dstv_sports_names_use_south_african_schedules(self) -> None:
        engine = _repository_alias_validation_engine()
        targets, _digest = repository_curated_alias_targets()

        dstv_sports = "SPORTS | DSTV SUPER (FHD)"
        cases = (
            ("DSTV : Super Motorsport (FHD).", "RACING | F1 MOTOGP", "MOTORSPORT.za"),
            ("DSTV : SUPER MOTORSPORT (FHD.)", dstv_sports, "MOTORSPORT.za"),
            ("DSTV : SUPERSPORT CRICKET (FHD)", dstv_sports, "CRICKET.za"),
            ("DSTV : SUPERSPORTS PREMIER LEAGUE (FHD).", dstv_sports, "SS.Premier.League.za"),
            ("DSTV : SUPERSPORTS LALIGA (FHD).", dstv_sports, "SS.La.Liga.za"),
            ("DSTV : SUPERSPORTS FOOTBALL (FHD).", dstv_sports, "SS.Football.za"),
            ("DSTV : SUPERSPORTS PSL (FHD).", dstv_sports, "PSL.za"),
            ("DSTV : SuperSport Grandstand (FHD).", dstv_sports, "GRANDSTAND.za"),
            ("DSTV : SuperSport Rugby (FHD).", dstv_sports, "RUGBY.za"),
            ("DSTV : SuperSport Golf (FHD)", "SPORTS | GOLF", "GOLF.za"),
            ("DSTV : SuperSport Golf (FHD).", dstv_sports, "GOLF.za"),
            ("DSTV : SuperSport Action (FHD).", dstv_sports, "SS.Action.za"),
            ("DSTV : SuperSport Tennis (FHD)", "SPORTS | TENNIS", "TENNIS.za"),
            ("DSTV : SuperSport Tennis (FHD).", dstv_sports, "TENNIS.za"),
            ("DSTV : SUPERSPORTS VARIETY 1 (FHD).", dstv_sports, "SS.Variety.1.za"),
            ("DSTV : SUPERSPORTS VARIETY 2 (FHD).", dstv_sports, "SS.Variety.2.za"),
            ("DSTV : SUPERSPORTS VARIETY 3 (FHD).", dstv_sports, "SS.Variety.3.za"),
            ("DSTV : SUPERSPORTS VARIETY 4 (FHD).", dstv_sports, "SS.Variety.4.za"),
            ("DSTV : Supersport blitz (FHD)", dstv_sports, "BLITZ.za"),
            ("DSTV: Supersport Schools (FHD)", "SPORTS | DSTV SUPER FHD", "SuperSport.School.HD.za"),
            ("DSTV: Maximo 1 Champions (FHD)", dstv_sports, "MAXIMO.1.za"),
            ("DSTV: ESPN 2 (FHD)", dstv_sports, "ESPN.2.HD.za"),
        )
        for provider_name, category_name, expected_epg_id in cases:
            with self.subTest(provider_name=provider_name):
                context = parse_channel_context_v8(
                    engine,
                    provider_name,
                    category_name,
                )
                self.assertTrue(context.route_explicit)
                self.assertEqual(tuple(context.route_plan), ("ZA",))
                alias = NameViews.from_context(context).strict
                self.assertEqual(targets[(alias, "ZA")], expected_epg_id)


if __name__ == "__main__":
    unittest.main()
