from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_contextual_v8 import install_contextual_v8  # noqa: E402


def prepared_resolver(rows: tuple[tuple[str, str, str], ...]):
    alias = f"skytv_v84_structural_recovery_engine_{len(sys.modules)}"
    path = SRC_DIR / "skytv_epg_engine.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the matcher engine")
    engine = importlib.util.module_from_spec(spec)
    sys.modules[alias] = engine
    spec.loader.exec_module(engine)
    resolver = install_contextual_v8(engine)
    candidates = [
        {
            "epg_id": epg_id,
            "feed": feed,
            "region": region,
            "display_name": engine.epg_id_to_name(epg_id),
            "normalized": engine.normalize_name(engine.epg_id_to_name(epg_id)),
        }
        for epg_id, feed, region in rows
    ]
    resolver.prepare(candidates, {})
    return resolver


class StructuralConflictRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = prepared_resolver(
            (
                ("ABC.Australia.id", "ALL_ID", "ID"),
                ("ABC.Australia.au", "ALL_AU", "AU"),
                ("MTV.Urheilu.2.fi", "ALL_FI", "FI"),
                ("TV.2.Sport.1.Norway.(NO,NO).no", "ALL_NO", "NO"),
                ("TV.2.Sport.1.Norway.(NO,NO).se", "ALL_SE", "SE"),
                ("Eleven.Sports.2.pl", "ALL_PL", "PL"),
                ("CANAL+.1.pl", "ALL_PL", "PL"),
                ("Novasportsextra3HD.gr", "ALL_GR", "GR"),
                ("Novasportsextra4HD.gr", "ALL_GR", "GR"),
                ("m4sport+.hu", "ALL_HU", "HU"),
                ("m4sport.hu", "ALL_HU", "HU"),
                ("Discovery.Channel.br", "ALL_BR", "BR"),
                ("Investigation.Discovery.br", "ALL_BR", "BR"),
                ("Sky.Sport.4K.it", "ALL_IT", "IT"),
                ("MTV.Liiga.UHD.fi", "ALL_FI", "FI"),
                ("Ziggo.Sport.2.nl", "ALL_NL", "NL"),
                ("Super!.it", "ALL_IT", "IT"),
                ("GREAT!.tv", "ALL_UK", "UK"),
                ("njam!.nl", "ALL_NL", "NL"),
                ("Giallo.TV.it", "ALL_IT", "IT"),
                ("Klan.al", "ALL_AL", "AL"),
            )
        )

    def resolve(self, category: str, channel: str) -> dict[str, object]:
        _query, result = self.resolver.resolve(
            {"category_name": category, "channel_name": channel}
        )
        return result

    def exact(self, category: str, channel: str) -> dict[str, object] | None:
        query, _result = self.resolver.resolve(
            {"category_name": category, "channel_name": channel}
        )
        return self.resolver._contextual_exact_match(query)

    def test_indonesia_country_and_vplus_provider_wrappers_are_scoped(self) -> None:
        result = self.exact(
            "|AS| INDONESIA", "(ID) (V+) ABC AUSTRALIA HD"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "AUTO_EPGSHARE")
        self.assertEqual(result["epg_id"], "ABC.Australia.id")

        wrong_market = self.exact(
            "|AS| AUSTRALIA", "(ID) (V+) ABC AUSTRALIA HD"
        )
        self.assertTrue(
            wrong_market is None
            or wrong_market.get("epg_id") != "ABC.Australia.id"
        )

        unknown_provider = self.exact(
            "|AS| INDONESIA", "(ID) (V+ EXTRA) ABC AUSTRALIA HD"
        )
        self.assertTrue(
            unknown_provider is None
            or unknown_provider.get("epg_id") != "ABC.Australia.id"
        )

    def test_terminal_quality_plus_is_metadata_but_real_plus_is_preserved(self) -> None:
        result = self.exact("|EU| FINLAND", "FI - MTV URHEILU 2 HD+")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "AUTO_EPGSHARE")
        self.assertEqual(result["epg_id"], "MTV.Urheilu.2.fi")

        plus_brand = self.exact("|EU| HUNGARY", "M4 SPORT+")
        self.assertIsNotNone(plus_brand)
        assert plus_brand is not None
        self.assertEqual(plus_brand["action"], "AUTO_EPGSHARE")
        self.assertEqual(plus_brand["epg_id"], "m4sport+.hu")

    def test_norway_catalog_metadata_requires_norway_region(self) -> None:
        result = self.exact("|EU| NORWAY", "NO - TV 2 SPORT 1 FHD+")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "AUTO_EPGSHARE")
        self.assertEqual(result["epg_id"], "TV.2.Sport.1.Norway.(NO,NO).no")

        wrong_region = self.exact("|EU| SWEDEN", "SE - TV 2 SPORT 1 FHD+")
        self.assertTrue(
            wrong_region is None
            or wrong_region.get("epg_id")
            != "TV.2.Sport.1.Norway.(NO,NO).no"
        )

    def test_polish_canal_bouquet_wrapper_preserves_real_channel_brand(self) -> None:
        eleven = self.exact(
            "|PL| SPORTOWE", "PL | CANAL+ | ELEVEN SPORTS 2 HD"
        )
        self.assertIsNotNone(eleven)
        assert eleven is not None
        self.assertEqual(eleven["action"], "AUTO_EPGSHARE")
        self.assertEqual(eleven["epg_id"], "Eleven.Sports.2.pl")

        canal = self.exact("|PL| SPORTOWE", "PL | CANAL+ | CANAL+ 1 HD")
        self.assertIsNotNone(canal)
        assert canal is not None
        self.assertEqual(canal["action"], "AUTO_EPGSHARE")
        self.assertEqual(canal["epg_id"], "CANAL+.1.pl")

        wrong_market = self.exact(
            "|EU| FRANCE SPORTS", "PL | CANAL+ | ELEVEN SPORTS 2 HD"
        )
        self.assertTrue(
            wrong_market is None
            or wrong_market.get("epg_id") != "Eleven.Sports.2.pl"
        )

    def test_joined_novasports_extra_keeps_the_exact_channel_number(self) -> None:
        four = self.exact(
            "|GR| Αθλητικά", "GR - NOVA SPORTS EXTRA 4 RAW EON"
        )
        self.assertIsNotNone(four)
        assert four is not None
        self.assertEqual(four["action"], "AUTO_EPGSHARE")
        self.assertEqual(four["epg_id"], "Novasportsextra4HD.gr")

        three = self.exact(
            "|GR| Αθλητικά", "GR - NOVA SPORTS EXTRA 3 RAW sat"
        )
        self.assertIsNotNone(three)
        assert three is not None
        self.assertEqual(three["action"], "AUTO_EPGSHARE")
        self.assertEqual(three["epg_id"], "Novasportsextra3HD.gr")
        self.assertNotEqual(three.get("epg_id"), "Novasportsextra4HD.gr")

    def test_id_discovery_keeps_its_brand_identity(self) -> None:
        result = self.exact("|AM| BRASIL", "BR: ID Discovery HD")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["epg_id"], "Investigation.Discovery.br")
        self.assertNotEqual(result["epg_id"], "Discovery.Channel.br")

    def test_plain_or_hd_names_do_not_borrow_uhd_event_guides(self) -> None:
        for category, channel, rejected_id in (
            ("|IT| SPORT", "IT - SKY SPORT HD", "Sky.Sport.4K.it"),
            ("|EU| FINLAND", "FI: MTV LIIGA HD", "MTV.Liiga.UHD.fi"),
        ):
            with self.subTest(channel=channel):
                result = self.exact(category, channel)
                self.assertTrue(
                    result is None or result.get("epg_id") != rejected_id
                )

    def test_decorative_superscript_is_not_a_channel_number(self) -> None:
        result = self.exact("|NL| SPORT", "NL - ZIGGO SPORT HD ²")
        self.assertTrue(
            result is None or result.get("epg_id") != "Ziggo.Sport.2.nl"
        )

    def test_punctuated_brand_does_not_borrow_a_tv_descriptor(self) -> None:
        for channel in ("IT: Super TV HD", "IT: Su per TV HD"):
            with self.subTest(channel=channel):
                result = self.exact("|IT| ITALY", channel)
                self.assertTrue(
                    result is None or result.get("epg_id") != "Super!.it"
                )

    def test_punctuation_guard_keeps_legitimate_exact_brand_spellings(self) -> None:
        for category, channel, expected_id in (
            ("UK | TV", "UK: GREAT TV", "GREAT!.tv"),
            ("|NL| TV", "NL - NJAM HD", "njam!.nl"),
        ):
            with self.subTest(channel=channel):
                result = self.exact(category, channel)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["epg_id"], expected_id)

    def test_optional_tv_descriptor_remains_relaxable_without_punctuation_conflict(self) -> None:
        for category, channel, expected_id in (
            ("|EU| ALBANIA", "ALB: Klan TV", "Klan.al"),
            ("|IT| ITALY", "IT: Giallo", "Giallo.TV.it"),
        ):
            with self.subTest(channel=channel):
                result = self.exact(category, channel)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["epg_id"], expected_id)


if __name__ == "__main__":
    unittest.main()
