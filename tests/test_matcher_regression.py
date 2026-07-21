from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPO_ROOT / "src" / "skytv_epg_engine.py"
V8_PATH = REPO_ROOT / "src" / "skytv_epg_contextual_v8.py"
OPT_PATH = REPO_ROOT / "src" / "skytv_epg_optimizations.py"
ICON_PATH = REPO_ROOT / "src" / "skytv_epg_icons.py"
RUNNER_PATH = REPO_ROOT / "scripts" / "build_all_servers.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "main.yml"
NOTEBOOK_PATH = REPO_ROOT / "SKYTV_EPG_v8_4_Colab_Only.ipynb"
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_contextual_v8 import (  # noqa: E402
    DEFAULT_APPROVED_ALIASES_V8,
    V8_POSITIVE_CASES,
    V8_SAFETY_CASES,
    install_contextual_v8,
)
from skytv_epg_optimizations import (  # noqa: E402
    install_performance_optimizations,
    optimization_status,
)


def load_engine(alias: str):
    spec = importlib.util.spec_from_file_location(alias, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frozen compatibility engine module")
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


def _v7_case_lists(engine) -> dict[str, list[tuple]]:
    tree = ast.parse(inspect.getsource(engine.run_smart_rules_v7_self_test))
    function = tree.body[0]
    result: dict[str, list[tuple]] = {}
    for node in function.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name in {"positive_cases", "safety_cases"}:
            result[name] = ast.literal_eval(node.value)
    return result


def catalog_for_all_self_tests(engine):
    cases = _v7_case_lists(engine)
    epg_ids: set[str] = set()
    for row in cases["positive_cases"]:
        epg_ids.add(str(row[-1]))
    for row in cases["safety_cases"]:
        epg_ids.update(str(value) for value in row[-1])
    for row in V8_POSITIVE_CASES:
        epg_ids.add(str(row[-1]))
    for row in V8_SAFETY_CASES:
        epg_ids.update(str(value) for value in row[-1])
    for row in DEFAULT_APPROVED_ALIASES_V8:
        epg_ids.update(str(value) for value in row["epg_ids"])

    real_candidates = []
    for epg_id in sorted(epg_ids, key=str.casefold):
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
    def test_integrity_manifest_hashes(self) -> None:
        manifest = json.loads((REPO_ROOT / "MATCHER_INTEGRITY.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["matcherVersion"], "8.4")
        self.assertEqual(manifest["builderVersion"], "7.1")
        self.assertEqual(manifest["legacyEngineSha256"], hashlib.sha256(ENGINE_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["contextualV8Sha256"], hashlib.sha256(V8_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["optimizationSha256"], hashlib.sha256(OPT_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["iconLayerSha256"], hashlib.sha256(ICON_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["productionRunnerSha256"], hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["workflowSha256"], hashlib.sha256(WORKFLOW_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["notebookSha256"], hashlib.sha256(NOTEBOOK_PATH.read_bytes()).hexdigest())

    def test_notebook_preserves_legacy_cells_and_embeds_v8(self) -> None:
        manifest = json.loads((REPO_ROOT / "MATCHER_INTEGRITY.json").read_text(encoding="utf-8"))
        notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))
        cell3 = "".join(notebook["cells"][3]["source"])
        cell4 = "".join(notebook["cells"][4]["source"])
        v8_cell = "".join(notebook["cells"][6]["source"])
        self.assertEqual((cell3 + "\n" + cell4).encode("utf-8"), ENGINE_PATH.read_bytes())
        self.assertEqual(hashlib.sha256(cell3.encode()).hexdigest(), manifest["legacyEngineCell3Sha256"])
        self.assertEqual(hashlib.sha256(cell4.encode()).hexdigest(), manifest["legacyEngineCell4Sha256"])
        self.assertEqual(hashlib.sha256(v8_cell.encode()).hexdigest(), manifest["contextualNotebookCellSha256"])
        metadata = notebook["metadata"]["skytv_runtime_optimization"]
        self.assertTrue(metadata["legacyMatcherFrozen"])
        self.assertEqual(metadata["matcherVersion"], "8.4")
        self.assertEqual(metadata["matcherArchitecture"], "contextual_v8_with_frozen_v7_compatibility")
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell.get("execution_count"))
                self.assertEqual(cell.get("outputs", []), [])

    def test_complete_v7_and_v8_regression_suite(self) -> None:
        engine = load_engine("skytv_v8_regression")
        resolver = install_contextual_v8(engine)
        install_performance_optimizations(engine, max_catalog_workers=4)
        candidates, dummies = catalog_for_all_self_tests(engine)

        first = engine.run_smart_rules_v8_self_test(candidates, dummies)
        self.assertEqual(len(first), 222)
        self.assertTrue(bool(first["passed"].all()))
        self.assertEqual(int((first["suite"] == "legacy_v7").sum()), 54)
        self.assertEqual(int((first["suite"] == "contextual_v8").sum()), 168)

        second = engine.run_smart_rules_v8_self_test(candidates, dummies)
        self.assertTrue(bool(second["passed"].all()))
        status = optimization_status(engine)
        self.assertEqual(status["legacy_index_builds"], 1)
        self.assertGreaterEqual(status["legacy_index_reuses"], 1)
        self.assertEqual(status["contextual_index_builds"], 1)
        self.assertGreaterEqual(status["contextual_index_reuses"], 2)
        self.assertEqual(resolver.state.fingerprint != "", True)

    def test_contextual_examples_and_safety_policy(self) -> None:
        engine = load_engine("skytv_v8_examples")
        resolver = install_contextual_v8(engine)
        candidates, dummies = catalog_for_all_self_tests(engine)
        resolver.prepare(candidates, dummies)

        cases = [
            ("PUNJABI", "PB: PTC PUNJABI", "PTC.PUNJABI.in"),
            ("PUNJABI", "PB | PTC PUNJABI | USA", "PTC.Punjabi.TV.ca2"),
            ("PUNJABI", "PB: PTC PUNJABI USA HD", "PTC.Punjabi.TV.ca2"),
            ("PUNJABI", "PB: PTC Punjabi Gold HD", "PTC.PUNJABI.GOLD.in"),
            ("PUNJABI", "PB: PTC SIMRAN", "PTC.Simran.in"),
            ("PUNJABI", "PB: PTC CHAKDE", "PTC.CHAK.DE.in"),
            ("PUNJABI", "PB: MH ONE", "MH.ONE.in"),
        ]
        for category, channel, expected in cases:
            with self.subTest(channel=channel):
                query, match = resolver.resolve({"category_name": category, "channel_name": channel})
                self.assertEqual(match["action"], "AUTO_EPGSHARE")
                self.assertEqual(str(match["epg_id"]).casefold(), expected.casefold())
                self.assertTrue(query.core_name)

        query, match = resolver.resolve({
            "category_name": "TAMIL | MOVIES",
            "channel_name": "TM: KTV FHD (usa)",
        })
        self.assertEqual(query.route_plan, ("US", "CA"))
        self.assertNotEqual(match["action"], "AUTO_EPGSHARE")
        self.assertNotIn(str(match.get("epg_id", "")).casefold(), {"ktv.in", "ktv.hd.in"})

    def test_structural_parser_variation_classes(self) -> None:
        engine = load_engine("skytv_v8_parser_variations")
        install_contextual_v8(engine)
        cases = [
            ("PUNJABI", "PBHD: PTC PUNJABI", "PTC PUNJABI", ("IN",), "PBHD"),
            ("PUNJABI", "PB-PTC PUNJABI", "PTC PUNJABI", ("IN",), "PB"),
            ("PUNJABI", "USA PTC PUNJABI", "PTC PUNJABI", ("US", "CA"), "USA"),
            ("PUNJABI", "US_PTC PUNJABI", "PTC PUNJABI", ("US", "CA"), "US"),
            ("TELUGU", "D2H: ETV TELUGU HD", "ETV TELUGU HD", ("IN",), "D2H"),
            ("TELUGU", "D 2 H: ETV TELUGU HD", "ETV TELUGU HD", ("IN",), "D 2 H"),
            ("PUNJABI", "TATA PLAY: PTC PUNJABI HD", "PTC PUNJABI HD", ("IN",), "TATA PLAY"),
        ]
        for category, channel, expected_core, expected_route, expected_wrapper in cases:
            with self.subTest(channel=channel):
                query = engine.parse_channel_context_v8(channel, category)
                self.assertEqual(query.core_name, expected_core)
                self.assertEqual(query.route_plan, expected_route)
                self.assertIn(expected_wrapper, query.wrapper_tokens)

        brand = engine.parse_channel_context_v8("USA Network East", "USA")
        self.assertEqual(brand.core_name, "USA Network East")
        self.assertEqual(brand.route_plan, ("US",))
        self.assertEqual(brand.wrapper_tokens, ())

    def test_fuzzy_similarity_is_never_automatic(self) -> None:
        engine = load_engine("skytv_v8_fuzzy_policy")
        resolver = install_contextual_v8(engine)
        epg_id = "Discovery.Science.in"
        display = engine.epg_id_to_name(epg_id)
        resolver.prepare(
            [{
                "epg_id": epg_id, "feed": "IN4", "region": "IN",
                "display_name": display, "normalized": engine.normalize_name(display),
            }],
            {},
        )
        _query, match = resolver.resolve({
            "category_name": "INDIA | DOCUMENTARY",
            "channel_name": "IN: Discovry Sciense",
        })
        self.assertNotEqual(match["action"], "AUTO_EPGSHARE")
        if match["match_method"] == "contextual_fuzzy":
            self.assertEqual(match["action"], "REVIEW")

    def test_approved_alias_memory_round_trip(self) -> None:
        engine = load_engine("skytv_v8_alias_memory_source")
        resolver = install_contextual_v8(engine)
        candidates, dummies = catalog_for_all_self_tests(engine)
        resolver.prepare(candidates, dummies)
        mapping = pd.DataFrame([{
            "server_id": "server_1", "stream_id": "55",
            "category_name": "PUNJABI", "channel_name": "PB: My Provider PTC",
            "action": "APPROVED", "source": "epgshare",
            "epg_id": "PTC.PUNJABI.in", "epg_feed": "IN4",
            "detected_region": "IN",
        }])
        memory = resolver.export_approved_alias_memory(mapping)
        self.assertEqual(len(memory), 1)
        self.assertNotIn("stream_id", memory.columns)
        self.assertNotIn("server_id", memory.columns)

        other_engine = load_engine("skytv_v8_alias_memory_target")
        other_resolver = install_contextual_v8(other_engine)
        other_resolver.prepare(candidates, dummies)
        changed = other_resolver.load_approved_aliases(memory)
        self.assertEqual(changed, 1)
        _query, match = other_resolver.resolve({
            "category_name": "PUNJABI", "channel_name": "PB: My Provider PTC",
        })
        self.assertEqual(match["action"], "AUTO_EPGSHARE")
        self.assertEqual(str(match["epg_id"]).casefold(), "ptc.punjabi.in")
        self.assertEqual(match["match_method"], "approved_knowledge")

    def test_schedule_fingerprint_equivalence(self) -> None:
        engine = load_engine("skytv_v8_schedule")
        resolver = install_contextual_v8(engine)
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def stamp(value: datetime) -> str:
            return value.strftime("%Y%m%d%H%M%S +0000")

        rows = ["<tv>"]
        for channel in ("A.test", "B.test", "C.test"):
            rows.append(f'<channel id="{channel}"/>')
        for index in range(10):
            start = now + timedelta(hours=index)
            stop = start + timedelta(hours=1)
            for channel in ("A.test", "B.test"):
                rows.append(
                    f'<programme channel="{channel}" start="{stamp(start)}" '
                    f'stop="{stamp(stop)}"><title>Shared {index}</title></programme>'
                )
            rows.append(
                f'<programme channel="C.test" start="{stamp(start)}" '
                f'stop="{stamp(stop)}"><title>Different {index}</title></programme>'
            )
        rows.append("</tv>")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schedule.xml"
            path.write_text("\n".join(rows), encoding="utf-8")
            fingerprints = engine.build_schedule_fingerprints_v8(
                path,
                ["A.test", "B.test", "C.test"],
                now_epoch=int(now.timestamp()),
            )
        self.assertEqual(engine.schedule_similarity_v8(
            fingerprints["a.test"], fingerprints["b.test"]
        ), 1.0)
        self.assertEqual(engine.schedule_similarity_v8(
            fingerprints["a.test"], fingerprints["c.test"]
        ), 0.0)
        groups = engine.discover_schedule_equivalence_groups_v8(fingerprints)
        self.assertEqual(groups, [["A.test", "B.test"]])
        self.assertEqual(resolver.register_schedule_equivalences(groups), 2)

    def test_parallel_catalog_download_preserves_original_order(self) -> None:
        module = load_engine("skytv_v8_catalog_order")
        install_contextual_v8(module)
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
            "A": {"region": "US", "kind": "real", "txt_url": "https://test/A.txt", "use_for_matching": True},
            "B": {"region": "UK", "kind": "real", "txt_url": "https://test/B.txt", "use_for_matching": True},
            "D": {"region": "DUMMY", "kind": "dummy", "txt_url": "https://test/D.txt", "use_for_matching": True},
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
