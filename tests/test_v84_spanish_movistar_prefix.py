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


AUDITED_MOVISTAR_IDENTITIES = (
    ("ACCION", "M+.Acción.es", "m plus accion"),
    ("CINE ESPANOL", "M+.Cine.Español.es", "m plus cine espanol"),
    ("CLASICOS", "M+.Clásicos.es", "m plus clasicos"),
    ("COMEDIA", "M+.Comedia.es", "m plus comedia"),
    ("COPA DEL REY", "M+.Copa.del.Rey.es", "m plus copa del rey"),
    ("DEPORTES", "M+.Deportes.es", "m plus deportes"),
    ("DEPORTES 4", "M+.Deportes.4.es", "m plus deportes 4"),
    ("DEPORTES 5", "M+.Deportes.5.es", "m plus deportes 5"),
    ("DEPORTES 7", "M+.Deportes.7.es", "m plus deportes 7"),
    ("DOCUMENTALES", "M+.Documentales.es", "m plus documentales"),
    ("DRAMA", "M+.Drama.es", "m plus drama"),
    ("ELLAS #V", "M+.Ellas.V.es", "m plus ellas v"),
    ("GOLF", "M+.Golf.es", "m plus golf"),
    (
        "LIGA DE CAMPEONES 7",
        "M+.Liga.de.Campeones.7.es",
        "m plus liga de campeones 7",
    ),
    (
        "LIGA DE CAMPEONES 8",
        "M+.Liga.de.Campeones.8.es",
        "m plus liga de campeones 8",
    ),
    (
        "LIGA DE CAMPEONES 13",
        "M+.Liga.de.Campeones.13.es",
        "m plus liga de campeones 13",
    ),
    ("ORIGINALES", "M+.Originales.es", "m plus originales"),
)


def prepared_resolver():
    alias = f"skytv_v84_movistar_engine_{len(sys.modules)}"
    path = SRC_DIR / "skytv_epg_engine.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the matcher engine")
    engine = importlib.util.module_from_spec(spec)
    sys.modules[alias] = engine
    spec.loader.exec_module(engine)
    resolver = install_contextual_v8(engine)
    epg_ids = [value[1] for value in AUDITED_MOVISTAR_IDENTITIES]
    epg_ids.extend(("M+.Deportes.6.es", "M+.Liga.de.Campeones.9.es"))
    resolver.prepare(
        [
            {
                "epg_id": epg_id,
                "feed": "ALL_ES",
                "region": "ES",
                "display_name": engine.epg_id_to_name(epg_id),
                "normalized": engine.normalize_name(engine.epg_id_to_name(epg_id)),
            }
            for epg_id in epg_ids
        ],
        {},
    )
    return resolver


class SpanishMovistarPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = prepared_resolver()

    def exact(self, channel_name: str, category_name: str = "|ES| ESPAÑA HEVC"):
        query, _result = self.resolver.resolve(
            {"category_name": category_name, "channel_name": channel_name}
        )
        return query, self.resolver._contextual_exact_match(query)

    def test_all_17_audited_m_dot_identities_resolve_exactly(self) -> None:
        self.assertEqual(len(AUDITED_MOVISTAR_IDENTITIES), 17)
        for tail, expected_epg_id, expected_key in AUDITED_MOVISTAR_IDENTITIES:
            with self.subTest(tail=tail):
                query, result = self.exact(f"ES - M.{tail} HD")
                self.assertEqual(query.route_plan, ("ES",))
                self.assertEqual(query.strict_key, expected_key)
                self.assertTrue(query.has_plus)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["epg_id"], expected_epg_id)
                self.assertEqual(result["action"], "AUTO_EPGSHARE")

    def test_channel_numbers_are_preserved_and_neighbours_are_not_selected(self) -> None:
        expected = {
            "ES - M. DEPORTES 4 HD": "M+.Deportes.4.es",
            "ES - M. DEPORTES 5 HD": "M+.Deportes.5.es",
            "ES - M. DEPORTES 7 HD": "M+.Deportes.7.es",
            "ES - M. LIGA DE CAMPEONES 7 HD": "M+.Liga.de.Campeones.7.es",
            "ES - M. LIGA DE CAMPEONES 8 HD": "M+.Liga.de.Campeones.8.es",
            "ES - M. LIGA DE CAMPEONES 13 HD": "M+.Liga.de.Campeones.13.es",
        }
        for channel_name, expected_epg_id in expected.items():
            with self.subTest(channel_name=channel_name):
                query, result = self.exact(channel_name)
                self.assertEqual(query.numbers, frozenset(expected_epg_id.split(".")[-2:-1]))
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["epg_id"], expected_epg_id)

    def test_non_audited_m_dot_shapes_are_not_rewritten(self) -> None:
        for channel_name, expected_key in (
            ("ES - M. LALIGA 1 HD", "m laliga 1"),
            ("ES - M. DEPORTES 3 LIVEEVENT", "m deportes 3 liveevent"),
            ("ES - M. DEPORTES 6 HD", "m deportes 6"),
            ("ES - M. DISNEY+", "m disney plus"),
            ("ES - M. SERIESMANIA HD", "m seriesmania"),
        ):
            with self.subTest(channel_name=channel_name):
                query, result = self.exact(channel_name)
                self.assertEqual(query.strict_key, expected_key)
                self.assertTrue(result is None or not result["epg_id"].startswith("M+."))

    def test_m_dot_requires_spanish_route_and_anchored_core_prefix(self) -> None:
        french, french_result = self.exact(
            "FR - M. ACCION HD", "|EU| FRANCE GÉNÉRAL"
        )
        self.assertEqual(french.route_plan, ("FR",))
        self.assertEqual(french.strict_key, "m accion")
        self.assertIsNone(french_result)

        unanchored, unanchored_result = self.exact("ES - CANAL M. ACCION HD")
        self.assertEqual(unanchored.strict_key, "canal m accion")
        self.assertIsNone(unanchored_result)


if __name__ == "__main__":
    unittest.main()
