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
STREAMING_RUNNER_PATH = REPO_ROOT / "scripts" / "build_epg_streaming.py"
AUTO_MATCH_ADAPTER_PATH = REPO_ROOT / "src" / "skytv_epg_auto_match_v1.py"
AUTO_MATCH_INTEGRATION_PATH = REPO_ROOT / "scripts" / "auto_match_inventory.py"
BACKLOG_ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_review_backlog.py"
BACKLOG_ANALYZER_WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "backlog_analyzer.yml"
)
NATIVE_REVIEW_PATH = REPO_ROOT / "scripts" / "native_epg_review.py"
AI_REVIEW_PATH = REPO_ROOT / "scripts" / "ai_review_gemini.py"
AI_REVIEW_POLICY_PATH = REPO_ROOT / "scripts" / "ai_review_policy.py"
EPG_CATALOG_STREAM_PATH = REPO_ROOT / "scripts" / "epg_catalog_stream.py"
EPG_SELECTION_SPOOL_PATH = REPO_ROOT / "scripts" / "epg_selection_spool.py"
CHANNEL_INVENTORY_RUNNER_PATH = (
    REPO_ROOT / "scripts" / "sync_channel_inventory.py"
)
SHEET_SEED_EXPORTER_PATH = (
    REPO_ROOT / "scripts" / "export_google_sheet_seed.py"
)
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "main.yml"
CHANNEL_INVENTORY_WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "channel_inventory_sync.yml"
)
CHANNEL_INVENTORY_REQUIREMENTS_PATH = REPO_ROOT / "requirements-sync.txt"
APPROVED_ALIASES_PATH = REPO_ROOT / "knowledge" / "approved_channel_aliases.csv"
SCHEDULE_EQUIVALENCES_PATH = (
    REPO_ROOT / "knowledge" / "schedule_equivalence_groups.json"
)
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
    return load_module(alias, ENGINE_PATH)


def load_module(alias: str, path: Path):
    spec = importlib.util.spec_from_file_location(alias, path)
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
        self.assertEqual(manifest["schemaVersion"], 7)
        self.assertEqual(manifest["matcherVersion"], "8.4")
        self.assertEqual(manifest["builderVersion"], "7.1")
        self.assertEqual(manifest["streamingPipelineVersion"], "1.0")
        self.assertEqual(
            manifest["streamingPipelineBuildId"],
            "SKYTV-EPG-V1-2026-09-15",
        )
        self.assertEqual(manifest["githubRelease"], "1.0-private-google-sheets")
        self.assertEqual(
            manifest["architecture"],
            "contextual_v8_matcher_frozen_with_strict_auto_match_boundary_"
            "private_google_sheets_epg_v1_and_separate_exact_icon_layer",
        )
        self.assertEqual(
            manifest["productionArchitecture"],
            "single_epgshare_all_iterparse_corroborated_catalog_sealed_sqlite_"
            "spool_private_google_sheets_api_inventory_server1_epgshare_only",
        )
        decision_boundary = " ".join(manifest["decisionBoundary"])
        self.assertIn("private Google Sheet", decision_boundary)
        self.assertIn("Sync Alerts", decision_boundary)
        self.assertIn("previously unseen", decision_boundary)
        self.assertIn("disabled REVIEW", decision_boundary)
        self.assertIn("Gemini", decision_boundary)
        self.assertIn("two different servers", decision_boundary)
        self.assertIn("present and unchanged", decision_boundary)
        self.assertIn("provider drift", decision_boundary)
        self.assertIn("programme gate", decision_boundary)
        self.assertIn("backlog analyzer", decision_boundary)
        self.assertIn("read-only token", decision_boundary)
        self.assertIn("aggregate counts", decision_boundary)
        self.assertIn("server-local review clusters", decision_boundary)
        self.assertIn("durable alias memory", decision_boundary)
        self.assertIn("automatic evidence", decision_boundary)
        self.assertIn("terminal Sheet", decision_boundary)
        self.assertIn("exact numeric-stream and exact-name M3U join", decision_boundary)
        self.assertIn("every KEEP_PANEL write", decision_boundary)
        self.assertNotIn("published Google Sheet CSV", decision_boundary)
        self.assertEqual(manifest["legacyEngineSha256"], hashlib.sha256(ENGINE_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["contextualV8Sha256"], hashlib.sha256(V8_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["optimizationSha256"], hashlib.sha256(OPT_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["iconLayerSha256"], hashlib.sha256(ICON_PATH.read_bytes()).hexdigest())
        self.assertEqual(manifest["productionRunnerSha256"], hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest())
        self.assertEqual(
            manifest["legacyProductionRunnerSha256"],
            hashlib.sha256(RUNNER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["streamingRunnerSha256"],
            hashlib.sha256(STREAMING_RUNNER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["autoMatchAdapterSha256"],
            hashlib.sha256(AUTO_MATCH_ADAPTER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["autoMatchIntegrationSha256"],
            hashlib.sha256(AUTO_MATCH_INTEGRATION_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["backlogAnalyzerSha256"],
            hashlib.sha256(BACKLOG_ANALYZER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["backlogAnalyzerWorkflowSha256"],
            hashlib.sha256(BACKLOG_ANALYZER_WORKFLOW_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["nativeReviewSha256"],
            hashlib.sha256(NATIVE_REVIEW_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["geminiReviewSha256"],
            hashlib.sha256(AI_REVIEW_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["aiReviewPolicySha256"],
            hashlib.sha256(AI_REVIEW_POLICY_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["epgCatalogStreamSha256"],
            hashlib.sha256(EPG_CATALOG_STREAM_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["epgSelectionSpoolSha256"],
            hashlib.sha256(EPG_SELECTION_SPOOL_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["sheetSeedExporterSha256"],
            hashlib.sha256(SHEET_SEED_EXPORTER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(manifest["workflowSha256"], hashlib.sha256(WORKFLOW_PATH.read_bytes()).hexdigest())
        self.assertEqual(
            manifest["channelInventoryRunnerSha256"],
            hashlib.sha256(CHANNEL_INVENTORY_RUNNER_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["channelInventoryWorkflowSha256"],
            hashlib.sha256(CHANNEL_INVENTORY_WORKFLOW_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["channelInventoryRequirementsSha256"],
            hashlib.sha256(CHANNEL_INVENTORY_REQUIREMENTS_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["approvedAliasesSha256"],
            hashlib.sha256(APPROVED_ALIASES_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            manifest["scheduleEquivalencesSha256"],
            hashlib.sha256(SCHEDULE_EQUIVALENCES_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(manifest["notebookSha256"], hashlib.sha256(NOTEBOOK_PATH.read_bytes()).hexdigest())

    def test_frozen_function_hashes(self) -> None:
        manifest = json.loads((REPO_ROOT / "MATCHER_INTEGRITY.json").read_text(encoding="utf-8"))
        modules = {
            "engine": load_engine("skytv_v1_frozen_engine_hashes"),
            "v8": load_module("skytv_v1_frozen_v8_hashes", V8_PATH),
            "icons": load_module("skytv_v1_frozen_icon_hashes", ICON_PATH),
        }
        for reference, expected_hash in manifest["functionSha256"].items():
            module_name, *attributes = reference.split(".")
            value = modules[module_name]
            for attribute in attributes:
                value = getattr(value, attribute)
            with self.subTest(reference=reference):
                actual_hash = hashlib.sha256(
                    inspect.getsource(value).encode("utf-8")
                ).hexdigest()
                self.assertEqual(actual_hash, expected_hash)

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

    def test_country_topics_override_only_coarse_provider_namespaces(self) -> None:
        engine = load_engine("skytv_v8_country_topic_routes")
        install_contextual_v8(engine)
        cases = [
            ("AR | Arabic", "MENA"),
            ("AR | Argentina", "AR"),
            ("SA | Latin", "LATAM"),
            ("SA | Latin America", "LATAM"),
            ("SA | South America", "LATAM"),
            ("AM | Central America", "LATAM"),
            ("SA | Saudi Arabia", "SA"),
            ("|AR| SAOUDI", "SA"),
            ("AF | Africa", "AFRICA"),
            ("AF | Afghanistan", "AF"),
            ("|BLN| GENERAL", "EXYU"),
            ("|BLN| SERBIA", "RS"),
            ("|BLN| BOSNIA", "BA"),
            ("|EU| RUSSIA", "RU"),
            ("EUROPE | TURKEY", "TR"),
            ("|EU| ISRAEL", "IL"),
            ("|AS| PAKISTAN", "PK"),
            ("|AS| MALAYSIA", "MY"),
            ("|AR| UAE", "AE"),
            ("|AS| AUSTRALIA", "AU"),
            ("|AS| NEW ZEALAND", "NZ"),
            ("|AS| PHILIPPINES", "PH"),
            ("|EU| GERMANY", "DE"),
            ("|EU| AUSTRIA", "AT"),
            ("|EU| NORWAY", "NO"),
            ("|EU| FINLAND", "FI"),
            ("|EU| FRANCE SPORTS", "FR"),
            ("|AM| CANADA DAZN PPV", "CA"),
        ]
        for category, expected_market in cases:
            with self.subTest(category=category):
                query = engine.parse_channel_context_v8("Example Channel", category)
                self.assertEqual(query.explicit_market, expected_market)
                self.assertEqual(query.route_plan, (expected_market,))
                self.assertTrue(query.route_explicit)

    def test_event_titles_do_not_override_coarse_provider_namespaces(self) -> None:
        engine = load_engine("skytv_v8_country_event_title_routes")
        install_contextual_v8(engine)
        cases = [
            ("|AR| AUSTRALIAN OPEN", "MENA"),
            ("|EU| FRENCH OPEN", "EU"),
            ("|EU| FRENCH CONNECTION", "EU"),
            ("|AR| TURKISH AIRLINES EUROLEAGUE", "MENA"),
        ]
        for category, expected_market in cases:
            with self.subTest(category=category):
                query = engine.parse_channel_context_v8("Example Channel", category)
                self.assertEqual(query.explicit_market, expected_market)
                self.assertEqual(query.route_plan, (expected_market,))
                self.assertTrue(query.route_explicit)

    def test_canadian_language_namespace_stays_canadian(self) -> None:
        engine = load_engine("skytv_v8_canadian_language_namespace")
        install_contextual_v8(engine)

        context_only = engine.parse_channel_context_v8("Example Channel", "CA | French")
        self.assertEqual(context_only.category_namespace, "CA")
        self.assertEqual(context_only.category_market, "CA")
        self.assertEqual(context_only.route_plan, ("CA",))
        self.assertTrue(context_only.route_explicit)
        self.assertIn("french", context_only.languages)

        for channel, expected_core in (
            ("CA Teletoon", "Teletoon"),
            ("CA VISION", "VISION"),
            ("CA Disney XD", "Disney XD"),
            ("CA ICI Radio Tele Toronto HD", "ICI Radio Tele Toronto HD"),
        ):
            with self.subTest(channel=channel):
                query = engine.parse_channel_context_v8(channel, "CA | French")
                self.assertEqual(query.core_name, expected_core)
                self.assertEqual(query.route_plan, ("CA",))
                self.assertTrue(query.route_explicit)

    def test_anchored_parenthetical_country_wrappers_are_conservative(self) -> None:
        engine = load_engine("skytv_v8_parenthetical_country_routes")
        install_contextual_v8(engine)

        mexico = engine.parse_channel_context_v8("(MX) Canal Once", "Latin")
        self.assertEqual(mexico.core_name, "Canal Once")
        self.assertEqual(mexico.route_plan, ("MX",))
        self.assertEqual(mexico.wrapper_tokens, ("(MX)",))

        nested_provider = engine.parse_channel_context_v8(
            "(MX) (IZ) Canal Once", "Latin"
        )
        self.assertEqual(nested_provider.core_name, "Canal Once")
        self.assertEqual(nested_provider.route_plan, ("MX",))
        self.assertEqual(nested_provider.wrapper_tokens, ("(MX)", "(IZ)"))

        ambiguous_arabic = engine.parse_channel_context_v8(
            "(AR) Arabic News", "AR | Arabic"
        )
        self.assertEqual(ambiguous_arabic.core_name, "(AR) Arabic News")
        self.assertEqual(ambiguous_arabic.route_plan, ("MENA",))
        self.assertEqual(ambiguous_arabic.wrapper_tokens, ())

        ambiguous_saudi = engine.parse_channel_context_v8(
            "(SA) Latin News", "SA | Latin"
        )
        self.assertEqual(ambiguous_saudi.core_name, "(SA) Latin News")
        self.assertEqual(ambiguous_saudi.route_plan, ("LATAM",))
        self.assertEqual(ambiguous_saudi.wrapper_tokens, ())

        ambiguous_afghan = engine.parse_channel_context_v8(
            "(AF) Africa News", "AF | Africa"
        )
        self.assertEqual(ambiguous_afghan.core_name, "(AF) Africa News")
        self.assertEqual(ambiguous_afghan.route_plan, ("AFRICA",))
        self.assertEqual(ambiguous_afghan.wrapper_tokens, ())

        argentina = engine.parse_channel_context_v8(
            "(AR) Claro 24H TVE", "Argentina"
        )
        self.assertEqual(argentina.core_name, "Claro 24H TVE")
        self.assertEqual(argentina.route_plan, ("AR",))
        self.assertEqual(argentina.wrapper_tokens, ("(AR)",))

        # IZ is not globally disposable, and an arbitrary second parenthetical
        # remains part of the brand even after a valid country wrapper.
        standalone_unknown = engine.parse_channel_context_v8(
            "(IZ) Canal Once", "Latin"
        )
        self.assertEqual(standalone_unknown.core_name, "(IZ) Canal Once")
        self.assertEqual(standalone_unknown.wrapper_tokens, ())

        arbitrary_nested = engine.parse_channel_context_v8(
            "(MX) (Cinema) Canal Once", "Latin"
        )
        self.assertEqual(arbitrary_nested.core_name, "(Cinema) Canal Once")
        self.assertEqual(arbitrary_nested.wrapper_tokens, ("(MX)",))

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

    def test_reviewed_cross_server_aliases_are_market_and_variant_scoped(self) -> None:
        engine = load_engine("skytv_v8_reviewed_cross_server_aliases")
        resolver = install_contextual_v8(engine)
        catalog_rows = (
            ("RTSH.1.al", "ALL_AL", "AL"),
            ("Realitatea.Plus.ro", "ALL_RO", "RO"),
            ("SkyRacing1.au", "ALL_AU", "AU"),
            ("SkyRacing2.au", "ALL_AU", "AU"),
            ("7Sydney.au", "ALL_AU", "AU"),
            ("9Perth.au", "ALL_AU", "AU"),
            ("[HORSECT].Horse.&.Country.TV.se", "ALL_SE", "SE"),
            ("3.Plus.al", "ALL_AL", "AL"),
            ("TLC.Balkans.bg", "ALL_BG", "BG"),
            ("National.Geographic.Wild.ro", "ALL_RO", "RO"),
            ("STAR.gr", "ALL_GR", "GR"),
            ("Etno.TV.ro", "ALL_RO", "RO"),
            ("BBC.Two.HD.uk", "UK1", "UK"),
            ("FILM.CAFE.ro", "ALL_RO", "RO"),
            ("EXP.Histori.al", "ALL_AL", "AL"),
            ("Canal+.Sport.fr", "ALL_FR", "FR"),
            ("News24.au", "ALL_AU", "AU"),
            ("USA.Network.HD.us2", "US2", "US"),
            ("Hub.Premier.1.sg", "ALL_SG", "SG"),
            ("TVJ.Sports.jm", "ALL_JM", "JM"),
        )
        candidates = []
        for epg_id, feed, region in catalog_rows:
            display_name = engine.epg_id_to_name(epg_id)
            candidates.append(
                {
                    "epg_id": epg_id,
                    "feed": feed,
                    "region": region,
                    "display_name": display_name,
                    "normalized": engine.normalize_name(display_name),
                }
            )
        resolver.prepare(candidates, {})
        resolver.load_approved_aliases(APPROVED_ALIASES_PATH)

        cases = (
            ("EUROPE | ALBANIA", "ALB: RTSH 1", "RTSH.1.al"),
            ("EUROPE | ROMANIA", "RO: REALITATEA PLUS", "Realitatea.Plus.ro"),
            ("AUSTRALIA", "AU: SKY RACING 1 HD", "SkyRacing1.au"),
            ("|AU| AUSTRALIA", "AU - SKY RACING 1 HD", "SkyRacing1.au"),
            ("ASIA | AUSTRALIA", "|AU| SKY Racing 2 HD", "SkyRacing2.au"),
            ("|AU| AUSTRALIA", "AU - SKY RACING 2 HD", "SkyRacing2.au"),
            ("ASIA | AUSTRALIA", "|AU| Channel 7 Sydney HD", "7Sydney.au"),
            ("|AU| AUSTRALIA", "AU - CHANNEL 7 SYDNEY HD", "7Sydney.au"),
            ("ASIA | AUSTRALIA", "|AU| Channel 9 Perth", "9Perth.au"),
            ("|AU| AUSTRALIA", "AU - CHANNEL 9 PERTH", "9Perth.au"),
            ("EUROPE | SWEDEN", "SE: Horse & Country", "[HORSECT].Horse.&.Country.TV.se"),
            ("|EU| SWEDEN HD", "SE - HORSE & COUNTRY", "[HORSECT].Horse.&.Country.TV.se"),
            ("EUROPE | ALBANIA", "ALB: Tring 3+", "3.Plus.al"),
            ("EUROPE | BULGARIA", "BG: TLC", "TLC.Balkans.bg"),
            ("EUROPE | ROMANIA", "RO: NAT GEO WILD", "National.Geographic.Wild.ro"),
            ("EUROPE | GREEK", "GR: STAR", "STAR.gr"),
            ("EUROPE | ROMANIA", "RO: ETNO", "Etno.TV.ro"),
            ("|UK| GENERAL", "UK - BBC TWO SCOTLAND", "BBC.Two.HD.uk"),
            ("EUROPE | ROMANIA", "RO: FILM CAFE", "FILM.CAFE.ro"),
            ("EUROPE | ALBANIA", "ALB: Explorer Histori", "EXP.Histori.al"),
            ("|EU| FRANCE SPORTS", "FR - CANAL+ SPORT", "Canal+.Sport.fr"),
        )
        for category, channel, expected in cases:
            with self.subTest(channel=channel):
                _query, match = resolver.resolve(
                    {"category_name": category, "channel_name": channel}
                )
                self.assertEqual(match["action"], "AUTO_EPGSHARE")
                self.assertEqual(match["epg_id"], expected)
                self.assertEqual(match["match_method"], "approved_knowledge")

        approved_aliases = {
            str(row.get("alias", "")) for row in resolver.approved_aliases
        }
        self.assertNotIn("au abc news 24", approved_aliases)
        self.assertNotIn("usa network", approved_aliases)

        for category, channel in (
            ("EUROPE | ROMANIA", "RTSH 1"),
            ("EUROPE | ALBANIA", "RTSH 2"),
            ("EUROPE | ROMANIA", "REALITATEA"),
            ("ASIA | AUSTRALIA", "AU | ABC News 24 HD"),
            ("USA", "USA Network West"),
            ("OTHER | HUB PREMIER", "Hub Premier 1 FHD"),
            ("AFRICA | CARIBBEAN", "Carib TVJ Sports"),
            ("|EU| FRANCE SPORTS", "FR - CANAL+ SPORT 360"),
            ("|EU| FRANCE SPORTS", "FR - CANAL+ SPORT 1"),
            ("EUROPE | POLAND", "PL - CANAL+ SPORT"),
        ):
            with self.subTest(blocked_channel=channel):
                _query, match = resolver.resolve(
                    {"category_name": category, "channel_name": channel}
                )
                self.assertNotEqual(match.get("match_method"), "approved_knowledge")

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
