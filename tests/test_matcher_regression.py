from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPO_ROOT / "src" / "skytv_epg_engine.py"
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_optimizations import install_performance_optimizations, optimization_status


def load_engine(alias: str):
    spec = importlib.util.spec_from_file_location(alias, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frozen engine module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def infer_feed(epg_id: str) -> tuple[str, str]:
    low = epg_id.casefold()
    if low.endswith(".us_locals1"):
        return "US_LOCALS1", "US"
    if "fanduel" in low and low.endswith(".us"):
        return "FANDUEL1", "US"
    if low.startswith(("mlb-", "nba-", "nfl-", "nhl-", "wnba-")) and low.endswith(".us"):
        return "US_SPORTS1", "US"
    if low.endswith(".us2"):
        return "US2", "US"
    if low.endswith(".ca2"):
        return "CA2", "CA"
    if low.endswith(".uk"):
        return "UK1", "UK"
    if low.endswith(".in") or low.endswith(".in2"):
        return "IN4", "IN"
    if low.endswith(".bein"):
        return "BEIN1", "BEIN"
    return "US2", "US"


def catalog_for_self_test(engine):
    tree = ast.parse(inspect.getsource(engine.run_smart_rules_v7_self_test))
    function = tree.body[0]
    case_lists = {}
    for node in function.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name in {"positive_cases", "safety_cases"}:
            case_lists[name] = ast.literal_eval(node.value)

    epg_ids: set[str] = set()
    for row in case_lists["positive_cases"]:
        epg_ids.add(str(row[-1]))
    for row in case_lists["safety_cases"]:
        epg_ids.update(str(value) for value in row[-1])

    real_candidates = []
    for epg_id in sorted(epg_ids):
        feed, region = infer_feed(epg_id)
        display_name = engine.epg_id_to_name(epg_id)
        real_candidates.append(
            {
                "epg_id": epg_id,
                "feed": feed,
                "region": region,
                "display_name": display_name,
                "normalized": engine.normalize_name(display_name),
            }
        )

    dummy_names = [
        "24.7.Dummy.us",
        "Adult.Programming.Dummy.us",
        "Adult.Section.Dummy.us",
        "Blank.Dummy.us",
        "Movie.Dummy.us",
        "Music.Choice.Dummy.us",
        "PPV.EVENTS.Dummy.us",
        "Sports.Dummy.us",
        "NEWS.dummy.us",
        "Religious.Dummy.us",
        "Shopping.Dummy.us",
    ]
    dummy_ids = {value.casefold(): value for value in dummy_names}
    return real_candidates, dummy_ids


class MatcherIntegrityTests(unittest.TestCase):
    def test_frozen_engine_hash(self) -> None:
        manifest = json.loads((REPO_ROOT / "MATCHER_INTEGRITY.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(ENGINE_PATH.read_bytes()).hexdigest()
        self.assertEqual(manifest["engineSha256"], digest)
        self.assertEqual(manifest["matcherVersion"], "7.0")
        self.assertEqual(manifest["builderVersion"], "7.1")

    def test_optimized_notebook_contains_unchanged_engine_cells(self) -> None:
        manifest = json.loads((REPO_ROOT / "MATCHER_INTEGRITY.json").read_text(encoding="utf-8"))
        notebook = json.loads(
            (REPO_ROOT / "SKYTV_EPG_v7_1_Optimized_Matcher_Frozen.ipynb").read_text(
                encoding="utf-8"
            )
        )
        cell3 = "".join(notebook["cells"][3]["source"])
        cell4 = "".join(notebook["cells"][4]["source"])
        self.assertEqual(
            hashlib.sha256(cell3.encode("utf-8")).hexdigest(),
            manifest["originalEngineCell3Sha256"],
        )
        self.assertEqual(
            hashlib.sha256(cell4.encode("utf-8")).hexdigest(),
            manifest["originalEngineCell4Sha256"],
        )
        self.assertEqual((cell3 + "\n" + cell4).encode("utf-8"), ENGINE_PATH.read_bytes())
        metadata = notebook.get("metadata", {}).get("skytv_runtime_optimization", {})
        self.assertTrue(metadata.get("matcherFrozen"))
        self.assertEqual(metadata.get("matcherVersion"), "7.0")

    def test_optimized_outputs_equal_frozen_outputs(self) -> None:
        frozen = load_engine("skytv_frozen_for_test")
        optimized = load_engine("skytv_optimized_for_test")
        install_performance_optimizations(optimized, max_catalog_workers=4)

        frozen_candidates, frozen_dummies = catalog_for_self_test(frozen)
        optimized_candidates, optimized_dummies = catalog_for_self_test(optimized)

        expected = frozen.run_smart_rules_v7_self_test(frozen_candidates, frozen_dummies)
        actual = optimized.run_smart_rules_v7_self_test(optimized_candidates, optimized_dummies)

        self.assertEqual(len(expected), 54)
        self.assertTrue(bool(expected["passed"].all()))
        self.assertTrue(bool(actual["passed"].all()))
        pd.testing.assert_frame_equal(expected, actual, check_dtype=True, check_like=False)

        # The same exact catalog is prepared again. The optimized wrapper must
        # reuse the already-built v7 indexes instead of rebuilding them.
        optimized.run_smart_rules_v7_self_test(optimized_candidates, optimized_dummies)
        status = optimization_status(optimized)
        self.assertEqual(status["matcher_index_builds"], 1)
        self.assertGreaterEqual(status["matcher_index_reuses"], 1)

    def test_parallel_catalog_download_preserves_original_order(self) -> None:
        import time

        module = load_engine("skytv_catalog_order_for_test")
        patch = install_performance_optimizations(module, max_catalog_workers=3)
        original_feeds = module.EPGSHARE_FEEDS
        original_make_session = module.make_session

        class Response:
            def __init__(self, content: bytes):
                self.status_code = 200
                self.content = content

        payloads = {
            "https://test/A.txt": (0.03, b"A.One.us2 A.Two.us2"),
            "https://test/B.txt": (0.001, b"B.One.uk B.Two.uk"),
            "https://test/D.txt": (0.01, b"Movie.Dummy.us"),
        }

        class Session:
            def get(self, url, timeout=None):
                delay, content = payloads[str(url)]
                time.sleep(delay)
                return Response(content)

            def close(self):
                return None

        module.EPGSHARE_FEEDS = {
            "A": {
                "region": "US", "kind": "real",
                "txt_url": "https://test/A.txt", "use_for_matching": True,
            },
            "B": {
                "region": "UK", "kind": "real",
                "txt_url": "https://test/B.txt", "use_for_matching": True,
            },
            "D": {
                "region": "DUMMY", "kind": "dummy",
                "txt_url": "https://test/D.txt", "use_for_matching": True,
            },
        }
        module.make_session = Session
        try:
            expected_candidates, expected_dummies = patch["original_download_catalog"](Session())
            actual_candidates, actual_dummies = module.download_epgshare_catalog(Session())
        finally:
            module.EPGSHARE_FEEDS = original_feeds
            module.make_session = original_make_session

        self.assertEqual(
            [(item["feed"], item["epg_id"]) for item in expected_candidates],
            [(item["feed"], item["epg_id"]) for item in actual_candidates],
        )
        self.assertEqual(expected_dummies, actual_dummies)


if __name__ == "__main__":
    unittest.main(verbosity=2)
