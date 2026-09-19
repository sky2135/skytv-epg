from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
ENGINE_PATH = SRC_DIR / "skytv_epg_engine.py"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_contextual_v8 import install_contextual_v8  # noqa: E402


def load_engine(alias: str):
    spec = importlib.util.spec_from_file_location(alias, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frozen compatibility engine module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class RouteGrammarV84Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = load_engine(f"route_grammar_engine_{self._testMethodName}")
        self.resolver = install_contextual_v8(self.engine)

    def context(self, category: str, channel: str):
        return self.engine.parse_channel_context_v8(channel, category)

    def test_audited_route_shapes_are_specific_and_structural(self) -> None:
        cases = (
            (
                "Malaysian pipe",
                "SPORTS | Astro Sports",
                "MY | Astro Arena FHD",
                ("MY",),
                "Astro Arena FHD",
                (),
            ),
            (
                "Malaysian Astro wrapper",
                "Asian Sports",
                "MY (Astro) AEC",
                ("MY",),
                "(Astro) AEC",
                (),
            ),
            (
                "Malaysian category Astro wrapper",
                "Malaysian",
                "MY (Astro) AI FM",
                ("MY",),
                "(Astro) AI FM",
                (),
            ),
            (
                "India Malayalam compound wrapper",
                "IN | Religious",
                "IN-MY | God TV",
                ("IN",),
                "God TV",
                ("malayalam",),
            ),
            (
                "beIN category over coarse AR wrapper",
                "|AR| BEIN SPORTS UHD",
                "AR - BEIN SPORTS 1 UHD",
                ("BEIN",),
                "BEIN SPORTS 1 UHD",
                ("arabic",),
            ),
            (
                "Greek Cypriot bucket",
                "|GR| Κυπριακά",
                "GR - CYTAVISION SPORTS 1 HD",
                ("CY",),
                "CYTAVISION SPORTS 1 HD",
                (),
            ),
            (
                "Canadian French compound",
                "CANADA | TV",
                "CA-FR Super Ecran 2",
                ("CA",),
                "Super Ecran 2",
                ("french",),
            ),
            (
                "EX-YU terminal country",
                "EUROPE | EX-YU",
                "EX-YU: ARENA SPORT 1 | RS",
                ("RS",),
                "ARENA SPORT 1",
                (),
            ),
            (
                "EX-YU Bosnia catalog code",
                "EUROPE | EX-YU",
                "EX-YU: HAYATOVCI | BIH",
                ("BA",),
                "HAYATOVCI",
                (),
            ),
        )
        for label, category, channel, route, core, languages in cases:
            with self.subTest(label=label):
                query = self.context(category, channel)
                self.assertEqual(query.route_plan, route)
                self.assertEqual(query.core_name, core)
                self.assertEqual(query.languages, frozenset(languages))
                self.assertTrue(query.route_explicit)

    def test_route_shapes_recover_exact_current_catalog_families(self) -> None:
        candidate_rows = (
            ("Astro.Arena.my", "MY", "Astro Arena"),
            (
                "beIN_SPORTS1_DIGITAL_Mono_AR.bein",
                "BEIN",
                "beIN SPORTS1",
            ),
            ("Cytavision.Sports.1.cy", "CY", "Cytavision Sports 1"),
            ("Super.Écran.2.ca2", "CA", "Super Écran 2"),
            ("Arena.Sport.1.rs", "RS", "Arena Sport 1"),
        )
        candidates = [
            {
                "epg_id": epg_id,
                "feed": region,
                "region": region,
                "display_name": display_name,
                "normalized": self.engine.normalize_name(display_name),
            }
            for epg_id, region, display_name in candidate_rows
        ]
        self.resolver.prepare(candidates, {})

        cases = (
            (
                "SPORTS | Astro Sports",
                "MY | Astro Arena FHD",
                "Astro.Arena.my",
            ),
            (
                "|AR| BEIN SPORTS UHD",
                "AR - BEIN SPORTS 1 UHD",
                "beIN_SPORTS1_DIGITAL_Mono_AR.bein",
            ),
            (
                "|GR| Κυπριακά",
                "GR - CYTAVISION SPORTS 1 HD",
                "Cytavision.Sports.1.cy",
            ),
            (
                "CANADA | TV",
                "CA-FR Super Ecran 2",
                "Super.Écran.2.ca2",
            ),
            (
                "EUROPE | EX-YU",
                "EX-YU: ARENA SPORT 1 | RS",
                "Arena.Sport.1.rs",
            ),
        )
        for category, channel, expected_id in cases:
            with self.subTest(channel=channel):
                _query, result = self.resolver.resolve(
                    {"category_name": category, "channel_name": channel}
                )
                self.assertEqual(result["action"], "AUTO_EPGSHARE")
                self.assertEqual(result["epg_id"], expected_id)

    def test_ambiguous_my_does_not_change_unrelated_routes_or_identity(self) -> None:
        malayalam = self.context("MALAYALAM | TV", "MY | Asianet")
        self.assertEqual(malayalam.route_plan, ("IN",))
        self.assertEqual(malayalam.core_name, "Asianet")
        self.assertIn("malayalam", malayalam.languages)

        marathi = self.context("MARATHI | TV", "MY: Marathi Special 1")
        self.assertEqual(marathi.route_plan, ("IN",))
        self.assertEqual(marathi.core_name, "Marathi Special 1")
        self.assertIn("marathi", marathi.languages)
        self.assertNotIn("malayalam", marathi.languages)

        mynetwork = self.context(
            "|NA| USA CW & MY", "USA: MY 33 (TVZ) Norfolk"
        )
        self.assertEqual(mynetwork.route_plan, ("US",))
        self.assertEqual(mynetwork.core_name, "MY 33 (TVZ) Norfolk")
        self.assertNotIn("malayalam", mynetwork.languages)

        plain_brand = self.context("USA | TV", "MY Radio")
        self.assertEqual(plain_brand.route_plan, ("US",))
        self.assertEqual(plain_brand.core_name, "MY Radio")
        self.assertNotIn("malayalam", plain_brand.languages)

        inner_brand = self.context("Malaysian", "MY (Astro) MY Radio")
        self.assertEqual(inner_brand.route_plan, ("MY",))
        self.assertEqual(inner_brand.core_name, "(Astro) MY Radio")
        self.assertNotIn("malayalam", inner_brand.languages)

        wrong_market = self.context("USA | TV", "IN-MY | God TV")
        self.assertEqual(wrong_market.route_plan, ("US",))
        self.assertNotEqual(wrong_market.core_name, "God TV")
        self.assertNotIn("malayalam", wrong_market.languages)

        no_pipe = self.context("IN | Religious", "IN-MY God TV")
        self.assertNotEqual(no_pipe.core_name, "God TV")
        self.assertNotIn("malayalam", no_pipe.languages)

        india_brand = self.context("IN | Religious", "MY God TV")
        self.assertEqual(india_brand.core_name, "MY God TV")
        self.assertNotIn("malayalam", india_brand.languages)

        for category, channel, route, core in (
            ("ALB | MUSIC", "ALB - MY MUSIC", ("AL",), "MY MUSIC"),
            ("IRAN", "IR - MY TV", ("IR",), "MY TV"),
            ("UK | TV", "My Kitchen Rules", ("UK",), "My Kitchen Rules"),
        ):
            with self.subTest(channel=channel):
                query = self.context(category, channel)
                self.assertEqual(query.route_plan, route)
                self.assertEqual(query.core_name, core)
                self.assertNotIn("malayalam", query.languages)

    def test_specific_overrides_do_not_leak_to_neighboring_categories(self) -> None:
        cases = (
            (
                "ordinary Arabic",
                "|AR| ARABIC FULL SD",
                "AR - Dubai TV",
                ("MENA",),
                "Dubai TV",
            ),
            (
                "strong country suffix still beats beIN category",
                "|AR| BEIN SPORTS UHD",
                "BEIN SPORTS 1 (USA)",
                ("US",),
                "BEIN SPORTS 1",
            ),
            (
                "ordinary Greek",
                "|GR| Αθλητικά",
                "GR - COSMOTE SPORTS 1 HD",
                ("GR",),
                "COSMOTE SPORTS 1 HD",
            ),
            (
                "Cyprus word outside exact Greek bucket",
                "|GR| Γενικά",
                "GR - RIK 1 CYPRUS",
                ("GR",),
                "RIK 1 CYPRUS",
            ),
            (
                "French market",
                "FRANCE | TV",
                "FR - CASA",
                ("FR",),
                "CASA",
            ),
            (
                "plain Canadian wrapper does not invent French",
                "CANADA | TV",
                "CA - Foo",
                ("CA",),
                "Foo",
            ),
            (
                "EX-YU name without pipe suffix",
                "EUROPE | EX-YU",
                "EX-YU: ARENA SPORT RS",
                ("EXYU",),
                "ARENA SPORT RS",
            ),
            (
                "EX-YU language suffix",
                "EUROPE | EX-YU",
                "EX-YU: CHANNEL | EN",
                ("EXYU",),
                "CHANNEL",
            ),
            (
                "unapproved EX-YU suffix",
                "EUROPE | EX-YU",
                "EX-YU: CHANNEL | CG",
                ("EXYU",),
                "CHANNEL CG",
            ),
            (
                "terminal country outside exact bucket",
                "EUROPE | SPORTS",
                "EX-YU: ARENA SPORT 1 | RS",
                ("EXYU",),
                "ARENA SPORT 1 RS",
            ),
            (
                "other EX-YU taxonomy bucket",
                "|BLN| SPORTS",
                "EX-YU: ARENA SPORT 1 | RS",
                ("EXYU",),
                "ARENA SPORT 1 RS",
            ),
        )
        for label, category, channel, route, core in cases:
            with self.subTest(label=label):
                query = self.context(category, channel)
                self.assertEqual(query.route_plan, route)
                self.assertEqual(query.core_name, core)


if __name__ == "__main__":
    unittest.main()
