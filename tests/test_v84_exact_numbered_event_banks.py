from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_auto_match_v1 import (  # noqa: E402
    DUMMY_REVIEW_METHODS,
    SAFE_DUMMY_METHODS,
    _has_exact_numbered_event_bank_evidence,
    _safe_dummy_classification,
)
from skytv_epg_contextual_v8 import install_contextual_v8  # noqa: E402


def prepared_resolver():
    alias = f"skytv_v84_exact_bank_test_engine_{len(sys.modules)}"
    path = SRC_DIR / "skytv_epg_engine.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the matcher engine")
    engine = importlib.util.module_from_spec(spec)
    sys.modules[alias] = engine
    spec.loader.exec_module(engine)
    resolver = install_contextual_v8(engine)
    dummy_names = (
        "Blank.Dummy.us",
        "ESPN+.Dummy.us",
        "Flo.Events.Dummy.us",
        "PPV.EVENTS.Dummy.us",
    )
    resolver.prepare([], {name.casefold(): name for name in dummy_names})
    return resolver


class ExactNumberedEventBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = prepared_resolver()

    def resolve(self, category: str, channel: str) -> dict[str, object]:
        _query, result = self.resolver.resolve(
            {
                "category_name": category,
                "channel_name": channel,
                "panel_epg_id": "",
            }
        )
        return result

    def test_only_the_three_audited_numbered_shapes_become_dummy(self) -> None:
        cases = (
            ("SPORTS | ESPN+", "(US) ESPN PLAY 1", "ESPN+.Dummy.us"),
            (" sports|espn+ ", "(AU) ESPN PLAY 100", "ESPN+.Dummy.us"),
            (
                " | na | USA ESPN+ ",
                "US (ESPN+ 001) | Baseball: Bears vs Lions",
                "ESPN+.Dummy.us",
            ),
            (
                "|NA| USA FLO",
                "USA - FLO 63: 2022 BISON DUALS",
                "Flo.Events.Dummy.us",
            ),
        )
        for category, channel, expected_id in cases:
            with self.subTest(category=category, channel=channel):
                result = self.resolve(category, channel)
                self.assertEqual(result["action"], "AUTO_DUMMY")
                self.assertEqual(result["source"], "dummy")
                self.assertEqual(result["epg_id"], expected_id)
                self.assertEqual(
                    result["match_method"], "exact_numbered_event_bank"
                )

    def test_nearby_category_variants_do_not_enter_the_bank_rule(self) -> None:
        cases = (
            ("SPORTS | ESPN", "(US) ESPN PLAY 1"),
            ("SPORTS | ESPN+ EXTRA", "(US) ESPN PLAY 1"),
            ("|NA| USA ESPN+ EXTRA", "US (ESPN+ 001) | Baseball"),
            ("|NA| CANADA ESPN+", "US (ESPN+ 001) | Baseball"),
            ("|NA| USA FLO EXTRA", "USA - FLO 63: 2022 BISON DUALS"),
            ("|NA| USA FLOSPORTS", "USA - FLO 63: 2022 BISON DUALS"),
            ("|NA| USA SPORTS", "FLO 63"),
        )
        for category, channel in cases:
            with self.subTest(category=category, channel=channel):
                result = self.resolve(category, channel)
                self.assertNotEqual(result["action"], "AUTO_DUMMY")
                self.assertNotEqual(
                    result["match_method"], "exact_numbered_event_bank"
                )

    def test_named_linear_espn_channels_and_name_variants_are_untouched(self) -> None:
        cases = (
            ("SPORTS | ESPN+", "ESPN"),
            ("SPORTS | ESPN+", "ESPN2"),
            ("SPORTS | ESPN+", "ESPNU College Sports"),
            ("SPORTS | ESPN+", "(US) ESPN PLAY ESPN2"),
            ("|NA| USA ESPN+", "US (ESPN 001) | Baseball"),
            ("|NA| USA ESPN+", "US (ESPN+ ABC) | Baseball"),
            ("|NA| USA ESPN+", "US (ESPN+ 000) | Baseball"),
            ("|NA| USA FLO", "USA - FLO ABC: BISON DUALS"),
            ("|NA| USA FLO", "FLO 63"),
        )
        for category, channel in cases:
            with self.subTest(category=category, channel=channel):
                result = self.resolve(category, channel)
                self.assertNotEqual(result["action"], "AUTO_DUMMY")
                self.assertNotEqual(
                    result["match_method"], "exact_numbered_event_bank"
                )

    def test_exact_bank_is_review_only_and_rechecks_category_name_and_dummy_id(self) -> None:
        self.assertNotIn("exact_numbered_event_bank", SAFE_DUMMY_METHODS)
        self.assertIn("exact_numbered_event_bank", DUMMY_REVIEW_METHODS)
        positives = (
            ("ESPN+.Dummy.us", "(US) ESPN PLAY 9", "SPORTS | ESPN+"),
            (
                "ESPN+.Dummy.us",
                "US (ESPN+ 500) | Soccer",
                "|NA| USA ESPN+",
            ),
            (
                "Flo.Events.Dummy.us",
                "USA - FLO 12: THE RUMBLE",
                "|NA| USA FLO",
            ),
        )
        for epg_id, channel, category in positives:
            with self.subTest(epg_id=epg_id, channel=channel):
                self.assertTrue(
                    _has_exact_numbered_event_bank_evidence(
                        epg_id=epg_id,
                        channel_name=channel,
                        category_name=category,
                    )
                )
                self.assertFalse(
                    _safe_dummy_classification(
                        method="exact_numbered_event_bank",
                        epg_id=epg_id,
                        matcher_reason="Exact audited numbered event bank",
                        channel_name=channel,
                        category_name=category,
                    )
                )

        negatives = (
            ("Flo.Events.Dummy.us", "(US) ESPN PLAY 9", "SPORTS | ESPN+"),
            ("ESPN+.Dummy.us", "ESPN2", "SPORTS | ESPN+"),
            ("ESPN+.Dummy.us", "US (ESPN+ 001) | Soccer", "USA ESPN+"),
            (
                "Flo.Events.Dummy.us",
                "USA - FLO 12: THE RUMBLE",
                "|NA| USA FLO EXTRA",
            ),
        )
        for epg_id, channel, category in negatives:
            with self.subTest(epg_id=epg_id, channel=channel):
                self.assertFalse(
                    _has_exact_numbered_event_bank_evidence(
                        epg_id=epg_id,
                        channel_name=channel,
                        category_name=category,
                    )
                )


if __name__ == "__main__":
    unittest.main()
