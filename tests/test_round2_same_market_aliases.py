from __future__ import annotations

import csv
import importlib.util
import sys
import unittest
from dataclasses import replace
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPOSITORY_ROOT / "src"
ENGINE_PATH = SRC_DIR / "skytv_epg_engine.py"
ALIASES_PATH = REPOSITORY_ROOT / "knowledge" / "approved_channel_aliases.csv"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_contextual_v8 import (  # noqa: E402
    install_contextual_v8,
    parse_channel_context_v8,
)


AUDITED_ALIASES = (
    ("rtv 21", "AL", "RTV.21.al"),
    ("super sports 2", "AL", "SuperSport.2.al"),
    ("super sports 3", "AL", "SuperSport.3.al"),
    ("super sports 4", "AL", "SuperSport.4.al"),
    ("super sports 5", "AL", "SuperSport.5.al"),
    ("super sports 6", "AL", "SuperSport.6.al"),
    ("kanal 10", "AL", "Kanal.10.al"),
    ("rtv 21 plus", "AL", "21.Plus.al"),
    ("klan plus", "AL", "Klan.Plus.al"),
    ("teve 1", "AL", "TeVe.1.al"),
    ("living", "AL", "Living.HD.al"),
    ("pro 1", "AL", "PRO1.al"),
    ("vizion plus", "AL", "Vizion.+.HD.al"),
    ("art kino 1", "AL", "Kino.1.al"),
    ("art kino 2", "AL", "Kino.2.al"),
    ("art kino 3", "AL", "Kino.3.al"),
    ("rtv 21 popullore", "AL", "21.Popullore.al"),
    ("asian aastha", "CA", "ATN.Aastha.ca"),
    ("asian ary news", "CA", "ATN.ARY.News.ca"),
    ("asian ary qtv", "CA", "ARY.QTV.ca"),
    ("asian atn bangla", "CA", "ATN.Bangla.ca"),
    ("asian atn food", "CA", "ATN.Food.Food.ca"),
    ("asian atn life", "CA", "ATN.Life.ca"),
    ("asian atn sony", "CA", "ATN.Sony.ca"),
    ("asian atn sports", "CA", "ATN.Sports.ca"),
    ("asian dd india", "CA", "ATN.DD.India.ca"),
    ("asian dd news", "CA", "ATN.DD.News.ca"),
    ("asian mtv india", "CA", "MTV.India.ca"),
    ("asian news18", "CA", "ATN.News.18.ca"),
    ("asian punjabi 5", "CA", "ATN.Punjabi.5.ca"),
    ("asian punjabi plus", "CA", "ATN.Punjabi.Plus.ca"),
    ("asian sab tv", "CA", "ATN.SAB.TV.ca"),
    ("asian sony aath", "CA", "ATN.Sony.Aath.ca"),
    ("asian tamil plus", "CA", "ATN.Tamil.Plus.ca"),
    ("asian times now", "CA", "ATN.Times.Now.ca"),
    ("asian zoom", "CA", "ATN.Zoom.ca"),
    ("tv 4 sports live 1", "SE", "[TV4SPL1].TV4.Sport.Live.1.se"),
    ("tv 4 sports live 2", "SE", "[TV4SPL2].TV4.Sport.Live.2.se"),
    ("tv 4 sports live 3", "SE", "[TV4SPL3].TV4.Sport.Live.3.se"),
    ("tv 4 sports live 4", "SE", "[TV4SPL4].TV4.Sport.Live.4.se"),
    ("tv 4 fotboll", "SE", "[TV4FOSV].TV4.Fotboll.se"),
    ("tv 4 hockey", "SE", "[TV4HOSV].TV4.Hockey.se"),
    ("tv 4 tennis", "SE", "[TV4TESV].TV4.Tennis.se"),
    ("v sports vinter", "SE", "[VSPOVIS].V.Sport.Vinter.se"),
    ("atg live", "SE", "[ATGLIVE].ATG.Live.se"),
    ("cnn international", "SE", "[CNNEU].CNN.International.se"),
    ("espn 2", "NL", "ESPN.2.nl"),
    ("espn 3", "NL", "ESPN.3.nl"),
    ("espn 4", "NL", "ESPN.4.nl"),
    ("ziggo sports 2", "NL", "Ziggo.Sport.2.nl"),
    ("ziggo sports 3", "NL", "Ziggo.Sport.3.nl"),
    ("ziggo sports 4", "NL", "Ziggo.Sport.4.nl"),
    ("ziggo sports 6", "NL", "Ziggo.Sport.6.nl"),
    ("npo 2 extra", "NL", "NPO.2.Extra.nl"),
    ("ct 1", "CZ", "ČT1.cz"),
    ("ct 2", "CZ", "ČT2.cz"),
    ("ct 24", "CZ", "ČT24.cz"),
    ("hbo 2", "CZ", "HBO2.cz"),
    ("hbo 3", "CZ", "HBO3.cz"),
    ("nova sports 1", "CZ", "Nova.Sport.1.cz"),
    ("max sports 1", "BG", "MAX.Sport.1.bg"),
    ("max sports 2", "BG", "MAX.Sport.2.bg"),
    ("rai 1", "IT", "Rai1.it"),
    ("automoto", "FR", "Automoto.la.chaine.fr"),
    ("hollywood", "PT", "Canal.Hollywood.HD.pt"),
    ("historia", "PT", "Canal.História.HD.pt"),
    ("digi 24", "RO", "Digi.24.ro"),
    ("noovo v tele", "CA", "Noovo.ca2"),
    (
        "at and t sportsnet pittsburgh",
        "US",
        "SportsNet.Pittsburgh.HD.us2",
    ),
    ("tva sherbrooke", "CA", "CHLT.Sherbrooke.ca2"),
    ("bein sports", "CA", "beIN.Sports.HD.(Canada).ca2"),
    (
        "assemblee nationale du quebec",
        "CA",
        "ASSEMBLÉE.NATIONALE.(CF.Cable.TV).ca2",
    ),
    ("fight sports", "ID", "Fight.Sports.id"),
    ("celestial movies", "ID", "Celestial.Movies.id"),
    ("asian food channel", "MY", "Asian.Food.Network.HD.my"),
)


HARD_EXCLUSIONS = (
    ("arta news", "AL", "A.News.al"),
    ("ora news", "AL", "A.News.al"),
    ("gaan bangla", "BD", "ATN.Bangla.ca"),
    (
        "bein sports global 4k",
        "BEIN",
        "bein_SPORTS_FTA_DIGITAL_Mono_AR.bein",
    ),
    ("discovery velocity", "CA", "Discovery.Channel.ca2"),
    ("ytv west", "CA", "HPItv.West.ca2"),
    (
        "lifetime movies",
        "ALL",
        "plex.tv.Lifetime.Movies.Love.&.Drama.plex",
    ),
    ("exxen sport 2", "TR", "S.SPORT.2.tr"),
    ("cbs drama", "UK", "U.and.Drama.uk"),
    ("sky cinema thriller", "UK", "Sky.Cinema.Hits.HD.uk"),
    ("sky sports premier league", "UK", "Sky.Sports.NFL.uk"),
    ("bein sports haber hd", "TR", "Bein.Sports.Haber.tr"),
    ("sbs viceland", "AU", "SBSVicelandPerth.au"),
)


def load_engine(alias: str):
    spec = importlib.util.spec_from_file_location(alias, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the frozen matcher engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def catalog_candidate(engine, epg_id: str, region: str) -> dict[str, str]:
    display_name = engine.epg_id_to_name(epg_id)
    return {
        "epg_id": epg_id,
        "feed": f"ALL_{region}",
        "region": region,
        "display_name": display_name,
        "normalized": engine.normalize_name(display_name),
    }


class Round2SameMarketAliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = load_engine("round2_same_market_alias_engine")
        cls.resolver = install_contextual_v8(cls.engine)
        targets: dict[str, str] = {}
        for _alias, region, epg_id in (*AUDITED_ALIASES, *HARD_EXCLUSIONS):
            targets.setdefault(epg_id, region)
        cls.resolver.prepare(
            [
                catalog_candidate(cls.engine, epg_id, region)
                for epg_id, region in targets.items()
            ],
            {},
        )
        cls.resolver.load_approved_aliases(ALIASES_PATH)

    @classmethod
    def approved(cls, alias: str, market: str):
        query = parse_channel_context_v8(cls.engine, alias, "")
        query = replace(
            query,
            explicit_market=market,
            route_plan=(market,),
            route_reason="focused same-market alias test",
            route_explicit=True,
        )
        return cls.resolver._approved_alias_match(query)

    def test_all_75_rows_are_exact_same_market_aliases(self) -> None:
        self.assertEqual(len(AUDITED_ALIASES), 75)
        self.assertEqual(
            len({(alias, region) for alias, region, _epg_id in AUDITED_ALIASES}),
            75,
        )

        with ALIASES_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(
                reader.fieldnames,
                [
                    "alias",
                    "regions",
                    "target_regions",
                    "epg_ids",
                    "relationship",
                    "note",
                ],
            )
            rows = list(reader)
        indexed: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            indexed.setdefault((row["alias"], row["regions"]), []).append(row)

        for alias, region, expected_epg_id in AUDITED_ALIASES:
            with self.subTest(alias=alias, region=region):
                configured = indexed.get((alias, region), [])
                self.assertEqual(len(configured), 1)
                self.assertEqual(configured[0]["target_regions"], "")
                self.assertEqual(configured[0]["epg_ids"], expected_epg_id)

                result = self.approved(alias, region)
                self.assertIsNotNone(result)
                self.assertEqual(result["epg_id"], expected_epg_id)
                self.assertEqual(result["match_method"], "approved_knowledge")

    def test_aliases_do_not_cross_market_boundaries(self) -> None:
        for alias, wrong_market, forbidden_epg_id in (
            ("living", "CA", "Living.HD.al"),
            ("automoto", "US", "Automoto.la.chaine.fr"),
            ("hollywood", "CA", "Canal.Hollywood.HD.pt"),
            ("cnn international", "US", "[CNNEU].CNN.International.se"),
            ("bein sports", "MENA", "beIN.Sports.HD.(Canada).ca2"),
        ):
            with self.subTest(alias=alias, wrong_market=wrong_market):
                result = self.approved(alias, wrong_market)
                self.assertTrue(result is None or result["epg_id"] != forbidden_epg_id)

    def test_adjacent_numbers_receive_no_curated_authority(self) -> None:
        for alias, market in (
            ("super sports 7", "AL"),
            ("art kino 4", "AL"),
            ("asian punjabi 6", "CA"),
            ("tv 4 sports live 5", "SE"),
            ("espn 5", "NL"),
            ("ziggo sports 5", "NL"),
            ("ct 3", "CZ"),
            ("max sports 3", "BG"),
            ("rai 2", "IT"),
        ):
            with self.subTest(alias=alias, market=market):
                self.assertIsNone(self.approved(alias, market))

    def test_base_plus_extra_and_popullore_editions_stay_distinct(self) -> None:
        self.assertEqual(self.approved("rtv 21", "AL")["epg_id"], "RTV.21.al")
        self.assertEqual(
            self.approved("rtv 21 plus", "AL")["epg_id"], "21.Plus.al"
        )
        self.assertEqual(
            self.approved("rtv 21 popullore", "AL")["epg_id"],
            "21.Popullore.al",
        )
        for alias, market in (
            ("klan", "AL"),
            ("vizion", "AL"),
            ("asian punjabi", "CA"),
            ("asian tamil", "CA"),
            ("npo 2", "NL"),
        ):
            with self.subTest(alias=alias, market=market):
                self.assertIsNone(self.approved(alias, market))

    def test_specific_rebrands_do_not_approve_generic_brand_names(self) -> None:
        for alias, market in (
            ("v", "CA"),
            ("at and t sportsnet", "US"),
            ("sportsnet", "US"),
            ("tva", "CA"),
        ):
            with self.subTest(alias=alias, market=market):
                self.assertIsNone(self.approved(alias, market))

    def test_known_false_consensus_rows_remain_hard_exclusions(self) -> None:
        configured_keys = {
            (row.get("alias", ""), row.get("regions", ""))
            for row in self.resolver.approved_aliases
        }
        for alias, market, forbidden_epg_id in HARD_EXCLUSIONS:
            with self.subTest(alias=alias, market=market):
                self.assertNotIn((alias, market), configured_keys)
                result = self.approved(alias, market)
                self.assertTrue(result is None or result["epg_id"] != forbidden_epg_id)


if __name__ == "__main__":
    unittest.main()
