from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import matching_lab.ai as lab_ai
from matching_lab.compat import catalog_stream
from matching_lab.models import (
    AIReviewEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
)
from matching_lab.pipeline import _cohort_risk_codes, _decision, _proposal
from matching_lab.policy import DEFAULT_POLICY
from matching_lab.retrieval import CandidateIndex, subject_from_mapping_row
from matching_lab.validation import (
    PROGRAMME_PASS_REASON,
    _validate_proposal_decision,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPOSITORY_ROOT / "src" / "skytv_epg_engine.py"
ALIASES_PATH = REPOSITORY_ROOT / "knowledge" / "approved_channel_aliases.csv"
SRC_DIR = REPOSITORY_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_contextual_v8 import (  # noqa: E402
    install_contextual_v8,
    parse_candidate_context_v8,
)
from skytv_epg_auto_match_v1 import (  # noqa: E402
    CatalogSnapshot,
    MatcherIdentity,
    MatcherPreflight,
    MIN_FUTURE_HORIZON_SECONDS,
    ScheduleEvidence,
    finalize_proposal,
    propose_new_channel_matches,
)


STORAGE_ALIASES = (
    ("mbc iraq", "MENA", "MBC.Iraq.HD.ae", "AE"),
    ("mbc drama", "MENA", "MBC.Drama.ae", "AE"),
    ("mbc 2", "MENA", "MBC.2.ae", "AE"),
    ("mbc 4", "MENA", "MBC.4.ae", "AE"),
    ("mbc masr 1", "MENA", "EN:.MBC1.Masr.sa", "SA"),
    ("rotana classic", "MENA", "Rotana.Classic.ae", "AE"),
    ("al jazeera", "MENA", "Al.Jazeera.HD.ae", "AE"),
    ("dubai sports 2", "MENA", "Dubai.Sports.2.ae", "AE"),
    ("mbc max", "MENA", "EN:.MBC.MAX.sa", "SA"),
    ("rt arabic", "MENA", "RT.Arabic.HD.ae", "AE"),
    ("al sumaria tv", "IQ", "Al.Sumaria.TV.ae", "AE"),
    ("france 24 arabic", "MENA", "France.24.Arabic.ae", "AE"),
    ("france 24 english", "MENA", "France.24.English.ae", "AE"),
    ("rotana drama", "MENA", "Rotana.Drama.ae", "AE"),
)

SAME_ROUTE_ALIASES = (
    ("nova news", "BG", "NOVANEWS.bg", "BG"),
    ("tcm", "US", "Turner.Classic.Movies.HD.us2", "US"),
    ("esp news", "US", "ESPNEWS.HD.us2", "US"),
    ("showtime family zone", "US", "Showtime.Familyzone.HD.us2", "US"),
)

AS_OF = "2026-09-18T06:30:50Z"
AS_OF_EPOCH = int(
    datetime.fromisoformat(AS_OF.replace("Z", "+00:00"))
    .astimezone(timezone.utc)
    .timestamp()
)
SHA_A = "a" * 64
SHA_B = "b" * 64
ROUND3_STORAGE_NOTE = (
    "Audited v9 exact storage identity with one current programme-backed target."
)
ROUND3_STORAGE_SHA256 = (
    "87c2dda80cd07007653e2e15d39329a1bcd0e6cbafc86ffab4907689e58b4fa3"
)


def load_engine(alias: str):
    spec = importlib.util.spec_from_file_location(alias, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load frozen matcher engine")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def candidate(engine, epg_id: str, region: str) -> dict[str, str]:
    display_name = engine.epg_id_to_name(epg_id)
    return {
        "epg_id": epg_id,
        "feed": f"ALL_{region}",
        "region": region,
        "display_name": display_name,
        "normalized": engine.normalize_name(display_name),
    }


def query_context(engine, alias: str, market: str):
    category = {
        "IQ": "MIDDLE EAST | IRAQ",
        "LB": "MIDDLE EAST | LEBANON",
        "MENA": "MIDDLE EAST | ARABIC",
    }.get(market, "MIDDLE EAST | ARABIC")
    if market == "US":
        category = "USA | TV"
    elif market == "BG":
        category = "EUROPE | BULGARIA"
    query = engine.parse_channel_context_v8(alias, category)
    if not query.route_explicit or query.route_plan != (market,):
        # This helper tests static alias authority, not every provider wrapper.
        # Several audited rows reach virtual routes such as AFRICA and
        # NA_DIASPORA only through metadata that was already removed from the
        # alias itself.
        query = replace(
            query,
            explicit_market=market,
            route_plan=(market,),
            route_reason="focused static-storage alias test",
            route_explicit=True,
        )
    return query


def mapping_row(alias: str, market: str) -> dict[str, str]:
    category = "MIDDLE EAST | IRAQ" if market == "IQ" else "MIDDLE EAST | ARABIC"
    return {
        "server_id": "server_1",
        "stream_id": "101",
        "channel_name": alias,
        "category_id": "general",
        "category_name": category,
        "enabled": "FALSE",
        "action": "REVIEW",
    }


class CuratedStorageAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = load_engine(f"storage_alias_engine_{self._testMethodName}")
        self.resolver = install_contextual_v8(self.engine)
        rows = [
            candidate(self.engine, epg_id, region)
            for _alias, _market, epg_id, region in (*STORAGE_ALIASES, *SAME_ROUTE_ALIASES)
        ]
        self.resolver.prepare(rows, {})
        self.resolver.load_approved_aliases(ALIASES_PATH)

    def test_all_audited_storage_aliases_are_exact_and_explicit(self) -> None:
        for alias, market, expected_epg_id, _region in STORAGE_ALIASES:
            with self.subTest(alias=alias):
                result = self.resolver._approved_alias_match(
                    query_context(self.engine, alias, market)
                )
                self.assertIsNotNone(result)
                self.assertEqual(result["epg_id"], expected_epg_id)
                self.assertEqual(
                    result["match_method"], "approved_storage_knowledge"
                )

    def test_real_storage_alias_can_pass_the_production_adapter(self) -> None:
        epg_id = "MBC.2.ae"
        raw_candidate = candidate(self.engine, epg_id, "AE")
        catalog = CatalogSnapshot.from_matcher_catalog(
            {epg_id},
            real_candidates=(raw_candidate,),
            dummy_ids={},
            source_sha256=SHA_A,
        )
        identity = MatcherIdentity.from_resolver(
            self.resolver,
            REPOSITORY_ROOT / "src" / "skytv_epg_contextual_v8.py",
            ENGINE_PATH,
        )
        proposals = propose_new_channel_matches(
            self.resolver,
            server_id="server_1",
            channels=(
                {
                    "stream_id": "101",
                    "name": "MBC 2",
                    "category_id": "arabic",
                },
            ),
            category_names={"arabic": "MIDDLE EAST | ARABIC"},
            existing_keys=(),
            catalog=catalog,
            matcher_identity=identity,
            preflight=MatcherPreflight(True, "focused integration test"),
        )
        proposal = proposals[("server_1", "101")]
        self.assertEqual(proposal.match_method, "approved_storage_knowledge")
        self.assertTrue(proposal.eligible_for_finalization)

        checked_at = 1_000
        evidence = ScheduleEvidence(
            declared_ids=frozenset({epg_id}),
            informative_future_programmes={epg_id: 2},
            latest_informative_future_stop={
                epg_id: checked_at + MIN_FUTURE_HORIZON_SECONDS
            },
            gate_passed_by_id={epg_id: True},
            checked_at_epoch=checked_at,
            source_sha256=SHA_A,
        )
        self.assertTrue(finalize_proposal(proposal, evidence).approved)

    def test_every_static_storage_row_is_exact_in_each_authorized_market(self) -> None:
        with ALIASES_PATH.open(encoding="utf-8", newline="") as handle:
            rows = [
                row
                for row in csv.DictReader(handle)
                if str(row.get("target_regions", "")).strip()
            ]
        self.assertEqual(len(rows), 97)
        self.assertFalse(
            {
                str(row.get("epg_ids", "")).strip()
                for row in rows
            }.intersection(catalog_stream.KNOWN_CROSSWIRED_PROGRAMME_IDS)
        )
        catalog = [
            candidate(self.engine, epg_id, target_region)
            for epg_id, target_region in sorted(
                {
                    (
                        str(row["epg_ids"]).strip(),
                        str(row["target_regions"]).strip(),
                    )
                    for row in rows
                }
            )
        ]
        resolver = install_contextual_v8(self.engine)
        resolver.prepare(catalog, {})
        resolver.load_approved_aliases(ALIASES_PATH)

        for row in rows:
            for market in str(row["regions"]).split("|"):
                with self.subTest(alias=row["alias"], market=market):
                    query = query_context(self.engine, row["alias"], market)
                    self.assertTrue(query.route_explicit)
                    self.assertEqual(query.route_plan, (market,))
                    result = resolver._approved_alias_match(query)
                    self.assertIsNotNone(result)
                    self.assertEqual(result["epg_id"], row["epg_ids"])
                    self.assertEqual(
                        result["match_method"], "approved_storage_knowledge"
                    )

    def test_round3_storage_batch_is_closed_and_excludes_false_exact_names(self) -> None:
        with ALIASES_PATH.open(encoding="utf-8", newline="") as handle:
            rows = [
                row
                for row in csv.DictReader(handle)
                if row.get("note") == ROUND3_STORAGE_NOTE
            ]
        self.assertEqual(len(rows), 67)
        canonical = json.dumps(
            sorted(
                (
                    row["alias"],
                    row["regions"],
                    row["target_regions"],
                    row["epg_ids"],
                    row["relationship"],
                    row["note"],
                )
                for row in rows
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            ROUND3_STORAGE_SHA256,
        )

        configured = {
            (row["alias"], region, row["target_regions"], row["epg_ids"])
            for row in rows
            for region in row["regions"].split("|")
        }
        self.assertFalse(
            any("NA_DIASPORA" in row["regions"].split("|") for row in rows)
        )
        for false_exact in (
            ("ctv", "MENA", "AE", "CTV.ae"),
            ("a plus", "PK", "FR", "A+.fr"),
            ("kanal 4", "BG", "DK", "Kanal.4.dk"),
            ("drama", "BEIN", "UK", "U.and.Drama.uk"),
            ("own", "CA", "US", "Oprah.Winfrey.Network.HD.us2"),
            ("star maa gold", "NA_DIASPORA", "IN", "STAR.MAA.GOLD.in"),
            ("star maa music", "NA_DIASPORA", "IN", "STAR.MAA.MUSIC.in"),
            ("flower tv", "NA_DIASPORA", "IN", "Flower.TV.in"),
            (
                "sangeet marathi",
                "NA_DIASPORA",
                "IN",
                "Sangeet.Marathi.in",
            ),
            ("zee marathi", "NA_DIASPORA", "IN", "ZEE.MARATHI.HD.in"),
        ):
            with self.subTest(false_exact=false_exact):
                self.assertNotIn(false_exact, configured)

    def test_known_ambiguous_and_non_exact_names_are_not_approved(self) -> None:
        for channel_name in (
            "MBC 1",
            "MBC 3",
            "MBC Action",
            "MBC Masr 2",
            "MBC Dram",
            "MBC 20",
            # This reaches the alias only through relaxed/identity matching;
            # cross-storage aliases require strict identity equality.
            "MBC 2 TV",
        ):
            with self.subTest(channel_name=channel_name):
                result = self.resolver._approved_alias_match(
                    query_context(self.engine, channel_name, "MENA")
                )
                self.assertIsNone(result)

    def test_same_route_aliases_do_not_erase_direction_or_number(self) -> None:
        for alias, market, expected_epg_id, _region in SAME_ROUTE_ALIASES:
            with self.subTest(alias=alias):
                exact = self.resolver._approved_alias_match(
                    query_context(self.engine, alias, market)
                )
                self.assertIsNotNone(exact)
                self.assertEqual(exact["epg_id"], expected_epg_id)
                for variant in (f"{alias} west", f"{alias} 2"):
                    self.assertIsNone(
                        self.resolver._approved_alias_match(
                            query_context(self.engine, variant, market)
                        )
                    )

    def test_in_memory_row_cannot_grant_cross_storage_authority(self) -> None:
        resolver = install_contextual_v8(self.engine)
        resolver.prepare([candidate(self.engine, "MBC.2.ae", "AE")], {})
        resolver.register_approved_aliases(
            [
                {
                    "alias": "private mbc two",
                    "regions": "MENA",
                    "target_regions": "AE",
                    "epg_ids": "MBC.2.ae",
                }
            ]
        )
        self.assertIsNone(
            resolver._approved_alias_match(
                query_context(self.engine, "private mbc two", "MENA")
            )
        )

    def test_in_memory_target_cannot_merge_into_a_static_storage_row(self) -> None:
        resolver = install_contextual_v8(self.engine)
        resolver.prepare(
            [
                candidate(self.engine, "MBC.2.ae", "AE"),
                candidate(self.engine, "MBC.Action.ae", "AE"),
            ],
            {},
        )
        resolver.load_approved_aliases(ALIASES_PATH)
        resolver.register_approved_aliases(
            [
                {
                    "alias": "mbc 2",
                    "regions": "MENA",
                    "target_regions": "AE",
                    "epg_ids": "MBC.Action.ae",
                }
            ]
        )
        row = next(
            item
            for item in resolver.approved_aliases
            if item["alias"] == "mbc 2" and item["regions"] == ("MENA",)
        )
        self.assertEqual(row["epg_ids"], ("MBC.2.ae",))
        self.assertEqual(row["cross_storage_epg_ids"], ("MBC.2.ae",))

    def test_target_must_be_in_the_exact_static_storage_region(self) -> None:
        resolver = install_contextual_v8(self.engine)
        resolver.prepare([candidate(self.engine, "MBC.2.ae", "AE")], {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text(
                "alias,regions,target_regions,epg_ids,relationship,note\n"
                "private mbc two,MENA,SA,MBC.2.ae,test,test\n",
                encoding="utf-8",
            )
            resolver.load_approved_aliases(path)
        self.assertIsNone(
            resolver._approved_alias_match(
                query_context(self.engine, "private mbc two", "MENA")
            )
        )

    def test_language_metadata_prefix_is_not_part_of_candidate_identity(self) -> None:
        context = parse_candidate_context_v8(
            self.engine,
            candidate(self.engine, "EN:.MBC1.Masr.sa", "SA"),
        )
        self.assertEqual(context.strict_key, "mbc 1 masr")
        self.assertNotIn("english", context.languages)

    def test_matching_lab_accepts_only_the_allowlisted_out_of_route_target(self) -> None:
        catalog = [candidate(self.engine, "MBC.2.ae", "AE")]
        resolver = install_contextual_v8(self.engine)
        resolver.prepare(catalog, {})
        resolver.load_approved_aliases(ALIASES_PATH)
        index = CandidateIndex(engine=self.engine, candidates=catalog)
        index.attach_resolver(resolver)
        subject = subject_from_mapping_row(self.engine, mapping_row("MBC 2", "MENA"))

        ranking = index.rank(subject)

        self.assertEqual(len(ranking.candidates), 1)
        evidence = ranking.candidates[0]
        self.assertEqual(evidence.region, "AE")
        self.assertEqual(evidence.conflicts, ())
        self.assertIn("CURATED_STORAGE_ALIAS_EXACT", evidence.methods)

        passed = replace(
            evidence,
            programme_state=ProgrammeState.PASS,
            programme_count=2,
            programme_first_start_epoch=1,
            programme_latest_stop_epoch=30_000,
            programme_reason="verified",
        )
        state, reasons, _selected, apply_eligible = _decision(
            subject=subject,
            ranking=ranking,
            candidates=(passed,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertIn("STATIC_STORAGE_ROUTE_ALLOWLISTED", reasons)
        self.assertFalse(apply_eligible)

        pending_state, _reasons, _selected, _apply = _decision(
            subject=subject,
            ranking=ranking,
            candidates=(evidence,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(pending_state, DecisionState.PENDING_PROGRAMME)

    def _valid_storage_proposal(self, *, with_alternative: bool = False):
        catalog = [candidate(self.engine, "MBC.2.ae", "AE")]
        if with_alternative:
            catalog.append(
                candidate(self.engine, "MBC.Secondary.2.mena", "MENA")
            )
        resolver = install_contextual_v8(self.engine)
        resolver.prepare(catalog, {})
        resolver.load_approved_aliases(ALIASES_PATH)
        index = CandidateIndex(engine=self.engine, candidates=catalog)
        index.attach_resolver(resolver)
        subject = subject_from_mapping_row(self.engine, mapping_row("MBC 2", "MENA"))
        ranking = index.rank(subject)
        passed_top = replace(
            ranking.candidates[0],
            programme_state=ProgrammeState.PASS,
            programme_count=2,
            programme_first_start_epoch=AS_OF_EPOCH,
            programme_latest_stop_epoch=AS_OF_EPOCH + 7 * 60 * 60,
            programme_reason=PROGRAMME_PASS_REASON,
        )
        candidates = (
            passed_top,
            *(
                replace(
                    candidate_value,
                    programme_reason=(
                        "Candidate was not selected for programme verification."
                    ),
                )
                for candidate_value in ranking.candidates[1:]
            ),
        )
        state, reasons, selected, apply_eligible = _decision(
            subject=subject,
            ranking=ranking,
            candidates=candidates,
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
            cohort_risk_codes=_cohort_risk_codes(subject),
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        proposal = _proposal(
            run_id=SHA_A,
            created_at=AS_OF,
            expires_at="2026-09-18T12:30:50Z",
            subject=subject,
            state=state,
            reasons=reasons,
            selected_key=selected,
            ranking=ranking,
            candidates=candidates,
            source_sha256=SHA_A,
            text_catalog_sha256=SHA_B,
            catalog_fingerprint_sha256=SHA_A,
            catalog_generation_token="20260918063050",
            policy=DEFAULT_POLICY,
            lab_code_sha256=SHA_B,
            auto_apply_eligible=apply_eligible,
        )
        _validate_proposal_decision(proposal, line_number=1)
        return proposal

    def test_ai_abstention_safely_demotes_storage_alias_provenance(self) -> None:
        proposal = self._valid_storage_proposal(with_alternative=True)
        self.assertEqual(len(proposal.candidates), 2)
        evidence = AIReviewEvidence(
            request_sha256="9" * 64,
            model=lab_ai.DEFAULT_MODEL,
            prompt_version=lab_ai.PROMPT_VERSION,
            decision="ABSTAIN",
            candidate_key="",
            confidence="NONE",
            cached=False,
        )
        outcome = lab_ai.AdvisoryOutcome(
            review_id=proposal.proposal_id,
            disposition=lab_ai.AdvisoryDisposition.ABSTAINED,
            evidence=evidence,
            suggested_candidate_key="",
            requires_review=True,
        )

        demoted = lab_ai.attach_advisory(proposal, outcome)

        self.assertIs(demoted.state, DecisionState.NEEDS_REVIEW)
        self.assertIn("AI_ABSTAINED", demoted.reason_codes)
        self.assertIn("LANE_HUMAN_REVIEW", demoted.reason_codes)
        self.assertNotIn("LANE_TRUSTED_ALIAS", demoted.reason_codes)
        self.assertNotIn("CURATED_STORAGE_ALIAS_EXACT", demoted.reason_codes)
        self.assertNotIn(
            "STATIC_STORAGE_ROUTE_ALLOWLISTED", demoted.reason_codes
        )
        demoted.verify_id()
        _validate_proposal_decision(demoted, line_number=1)

    @staticmethod
    def _reseal(proposal, **changes):
        draft = replace(proposal, proposal_id="0" * 64, **changes)
        return replace(draft, proposal_id=draft.computed_id())

    def test_validator_rejects_storage_lane_tampering(self) -> None:
        proposal = self._valid_storage_proposal()

        cases = []
        cases.append(
            self._reseal(
                proposal,
                reason_codes=tuple(
                    reason
                    for reason in proposal.reason_codes
                    if reason != "STATIC_STORAGE_ROUTE_ALLOWLISTED"
                ),
            )
        )
        same_region = replace(
            proposal.candidates[0],
            region=proposal.market,
            semantics=replace(
                proposal.candidates[0].semantics,
                market=proposal.market,
            ),
        )
        cases.append(self._reseal(proposal, candidates=(same_region,)))
        cases.append(self._reseal(proposal, route_explicit=False))
        cases.append(
            self._reseal(
                proposal,
                reason_codes=tuple(
                    sorted((*proposal.reason_codes, "ALERT_SNAPSHOT_MISSING"))
                ),
            )
        )
        failed_programmes = replace(
            proposal.candidates[0],
            programme_state=ProgrammeState.FAIL,
            programme_count=0,
            programme_first_start_epoch=None,
            programme_latest_stop_epoch=None,
            programme_reason="insufficient",
        )
        cases.append(self._reseal(proposal, candidates=(failed_programmes,)))

        second = replace(
            proposal.candidates[0],
            candidate_key="c002",
            epg_id="MBC.2.Second.ae",
            methods=("STRICT_EXACT",),
            score_ppm=max(
                0,
                proposal.score_ppm - DEFAULT_POLICY.strong_margin_ppm + 1,
            ),
        )
        cases.append(
            self._reseal(
                proposal,
                candidates=(proposal.candidates[0], second),
                margin_ppm=DEFAULT_POLICY.strong_margin_ppm - 1,
            )
        )

        for tampered in cases:
            with self.subTest(change=tampered.public_dict()["decision"]):
                with self.assertRaises(ContractError):
                    _validate_proposal_decision(tampered, line_number=1)

    def test_other_exact_lanes_cannot_borrow_storage_reasons(self) -> None:
        proposal = self._valid_storage_proposal()
        for method in ("CURATED_ALIAS_EXACT", "FROZEN_RESOLVER_EXACT"):
            with self.subTest(method=method):
                candidate_value = replace(
                    proposal.candidates[0], methods=(method,)
                )
                tampered = self._reseal(
                    proposal,
                    candidates=(candidate_value,),
                )
                with self.assertRaises(ContractError):
                    _validate_proposal_decision(tampered, line_number=1)

    def test_storage_marker_does_not_remove_number_conflicts(self) -> None:
        catalog = [candidate(self.engine, "MBC.2.ae", "AE")]
        index = CandidateIndex(engine=self.engine, candidates=catalog)
        index.attach_resolver(
            SimpleNamespace(
                _approved_alias_match=lambda _query: {
                    "epg_id": "MBC.2.ae",
                    "match_method": "approved_storage_knowledge",
                }
            )
        )
        subject = subject_from_mapping_row(self.engine, mapping_row("MBC 3", "MENA"))

        ranking = index.rank(subject)

        self.assertEqual(len(ranking.candidates), 1)
        self.assertIn("NUMBER_MISMATCH", ranking.candidates[0].conflicts)
        self.assertNotIn("MARKET_MISMATCH", ranking.candidates[0].conflicts)

        state, reasons, _selected, _apply = _decision(
            subject=subject,
            ranking=ranking,
            candidates=ranking.candidates,
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.CONFLICT)
        self.assertIn("CURATED_STORAGE_ALIAS_SEMANTIC_CONFLICT", reasons)


if __name__ == "__main__":
    unittest.main()
