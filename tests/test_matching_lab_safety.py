from __future__ import annotations

import csv
import gzip
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from matching_lab.artifacts import (
    package_code_sha256,
    strict_json_loads,
    write_private_bundle,
)
from matching_lab.compat import (
    automatch,
    parse_candidate_context_v8,
    parse_channel_context_v8,
    streaming,
    sync,
)
from matching_lab.models import (
    CandidateEvidence,
    ContractError,
    DecisionState,
    ProgrammeState,
    ProtectedSemantics,
    require_opaque_identifier,
    safe_display_text,
    sha256_bytes,
)
from matching_lab.normalization import (
    NameViews,
    protected_conflicts,
    semantics_from_context,
    words,
)
from matching_lab.pipeline import (
    _cohort_risk_codes,
    _decision,
    _eligible_review_row,
    _programme_candidate,
    _proposal,
    run_shadow,
)
from matching_lab.policy import DEFAULT_POLICY, MatchingPolicy
from matching_lab.retrieval import (
    CandidateIndex,
    LabSubject,
    RankedCandidates,
    subject_from_mapping_row,
)
import matching_lab.retrieval as retrieval


AS_OF = "2026-09-15T12:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _candidate(
    *,
    state: ProgrammeState = ProgrammeState.PASS,
    methods: tuple[str, ...] = ("HUMAN_ALIAS_EXACT",),
    score: int = 1_000_000,
) -> CandidateEvidence:
    return CandidateEvidence(
        candidate_key="c001",
        epg_id="Good.Channel.us2",
        display_name="Good Channel",
        feed="US2",
        region="US",
        score_ppm=score,
        methods=methods,
        conflicts=(),
        features_ppm=(("CONTEXTUAL_SCORE", score),),
        semantics=ProtectedSemantics(market="US"),
        programme_state=state,
        programme_count=2 if state is ProgrammeState.PASS else 0,
        programme_first_start_epoch=1 if state is not ProgrammeState.NOT_CHECKED else None,
        programme_latest_stop_epoch=30_000 if state is ProgrammeState.PASS else None,
        programme_reason="verified" if state is ProgrammeState.PASS else "insufficient",
    )


def _subject() -> SimpleNamespace:
    return SimpleNamespace(
        server_id="server_1",
        stream_id="101",
        channel_name="US: Good Channel",
        category_name="US | General",
        row_guard_sha256=SHA_A,
        provider_identity_sha256=SHA_B,
        route_explicit=True,
        market="US",
        route_plan=("US",),
    )


def _ranking(candidate: CandidateEvidence) -> RankedCandidates:
    return RankedCandidates(
        candidates=(candidate,),
        compatible_count=1,
        score_ppm=candidate.score_ppm,
        margin_ppm=1_000_000,
        tier=5,
        work_candidates=1,
    )


def _fake_context(value: str) -> SimpleNamespace:
    tokens = words(value)
    normalized = " ".join(tokens)
    return SimpleNamespace(
        strict_key=normalized,
        relaxed_key=normalized,
        compact_key="".join(tokens),
        edition_key=normalized,
        bag_key=" ".join(sorted(tokens)),
        tokens=tokens,
        core_name=value,
        direction="",
        timeshift="",
        has_plus=False,
        has_extra=False,
        has_alternate=False,
        numbers=(),
        languages=(),
        content=(),
    )


def _mapping_row(*, stream_id: str, channel_name: str) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": "server_1",
            "server_label": "Server 1",
            "region_code": "north_america",
            "genre": "general",
            "primary_language": "en",
            "stream_id": stream_id,
            "enabled": "FALSE",
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": "general",
            "category_name": "US | General",
            "country_codes": "US",
            "language_codes": "en",
            "audience_codes": "general",
            "content_rating": "general",
            "channel_role": "linear",
            "action": "REVIEW",
            "source": "epgshare01",
            "epg_feed": "ALL_SOURCES1",
            "metadata_status": "review",
            "metadata_source": "provider_category",
            "metadata_confidence": "0.8",
            "metadata_locked": "FALSE",
        }
    )
    return row


def _csv_bytes(headers: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _xml_bytes() -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?><tv>"
        '<channel id="Good.Channel.us2"><display-name>Good Channel</display-name></channel>'
        '<channel id="Other.Channel.us2"><display-name>Other Channel</display-name></channel>'
        '<programme channel="Good.Channel.us2" start="20260915130000 +0000" '
        'stop="20260915170000 +0000"><title>Programme One</title></programme>'
        '<programme channel="Good.Channel.us2" start="20260915170000 +0000" '
        'stop="20260915200000 +0000"><title>Programme Two</title></programme>'
        "</tv>"
    ).encode("utf-8")


class MatchingLabArtifactSafetyTests(unittest.TestCase):
    def test_opaque_ids_preserve_unicode_compatibility_characters(self) -> None:
        epg_id = "TBSチャンネル１.jp"
        candidate = replace(_candidate(), epg_id=epg_id)

        self.assertEqual(candidate.epg_id, epg_id)
        self.assertEqual(
            require_opaque_identifier(epg_id, label="test EPG ID", maximum=300),
            epg_id,
        )
        self.assertEqual(safe_display_text("\u202bTBS Channel １"), "TBS Channel 1")
        with self.assertRaises(ContractError):
            require_opaque_identifier(
                " TBSチャンネル１.jp", label="test EPG ID", maximum=300
            )

    def test_state_paths_cannot_overlap_bundle_or_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            common = {
                "mappings_csv": mappings,
                "alerts_csv": root / "alerts.csv",
                "all_source_file": root / "guide.xml.gz",
                "all_source_catalog_file": root / "guide.txt",
                "output_dir": root / "bundle",
                "as_of": AS_OF,
            }
            with self.assertRaisesRegex(ContractError, "outside the bundle"):
                run_shadow(
                    **common,
                    ledger_path=root / "bundle" / "history.sqlite3",
                )
            with self.assertRaisesRegex(ContractError, "overlap an input"):
                run_shadow(**common, ledger_path=mappings)
            with self.assertRaisesRegex(ContractError, "server scope is unsupported"):
                run_shadow(**common, servers=("server_9",))
            with self.assertRaisesRegex(ContractError, "fixed.*shadow policy"):
                run_shadow(
                    **common,
                    policy=MatchingPolicy(strong_margin_ppm=90_000),
                )

    def test_code_hash_binds_reused_modules_static_data_and_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            package = repository / "matching_lab"
            for name in ("matching_lab", "scripts", "src", "knowledge", "config"):
                (repository / name).mkdir()
            (package / "lab.py").write_text("LAB = 1\n", encoding="utf-8")
            dependency = repository / "scripts" / "auto_match_inventory.py"
            dependency.write_text("VALUE = 1\n", encoding="utf-8")
            (repository / "knowledge" / "approved_channel_aliases.csv").write_text(
                "alias,target\n", encoding="utf-8"
            )
            (repository / "knowledge" / "schedule_equivalence_groups.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (repository / "config" / "channel_icons.csv").write_text(
                "channel,icon\n", encoding="utf-8"
            )
            (repository / "config" / "epg_sources.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (repository / "requirements.txt").write_text(
                "example==1.0\n", encoding="utf-8"
            )
            (repository / "requirements-sync.txt").write_text("", encoding="utf-8")

            original = package_code_sha256(package)
            dependency.write_text("VALUE = 2\n", encoding="utf-8")
            changed_dependency = package_code_sha256(package)
            (repository / "requirements.txt").write_text(
                "example==2.0\n", encoding="utf-8"
            )
            changed_requirement = package_code_sha256(package)

        self.assertNotEqual(original, changed_dependency)
        self.assertNotEqual(changed_dependency, changed_requirement)

    def test_strict_json_rejects_nested_duplicates_nonintegers_and_bad_encoding(self) -> None:
        invalid = (
            b'{"outer":{"key":1,"key":2}}',
            b'{"score":1.0}',
            b'{"score":1e2}',
            b'{"score":NaN}',
            b'{"score":Infinity}',
            b'{}{}',
            b"\xef\xbb\xbf{}",
            b'{"text":"\xff"}',
        )
        for content in invalid:
            with self.subTest(content=content):
                with self.assertRaises(ContractError):
                    strict_json_loads(content)

    def test_bundle_hashes_bind_canonical_bytes_and_expose_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)

            def manifest_factory(
                *, proposals_sha256: str, summary_sha256: str
            ) -> dict[str, object]:
                return {
                    "schema": "test.private-bundle.v1",
                    "proposals_sha256": proposals_sha256,
                    "summary_sha256": summary_sha256,
                }

            proposals, summary, manifest = write_private_bundle(
                output,
                proposals=({"z": 2, "a": 1},),
                summary={"z": 4, "a": 3},
                manifest_factory=manifest_factory,
            )
            self.assertEqual(proposals, b'{"a":1,"z":2}\n')
            self.assertEqual(summary, b'{"a":3,"z":4}\n')
            parsed = strict_json_loads(manifest)
            self.assertIsInstance(parsed, dict)
            assert isinstance(parsed, dict)
            self.assertEqual(parsed["proposals_sha256"], sha256_bytes(proposals))
            self.assertEqual(parsed["summary_sha256"], sha256_bytes(summary))
            for name in ("proposals.jsonl", "summary.json", "manifest.json"):
                self.assertEqual((output / name).stat().st_mode & 0o777, 0o600)

            tampered = proposals.replace(b'"z":2', b'"z":3')
            self.assertNotEqual(parsed["proposals_sha256"], sha256_bytes(tampered))

            with self.assertRaisesRegex(ContractError, "new and empty"):
                write_private_bundle(
                    output,
                    proposals=({"a": 1},),
                    summary={"a": 1},
                    manifest_factory=manifest_factory,
                )

    def test_content_addressed_proposal_detects_field_tampering(self) -> None:
        candidate = _candidate()
        ranking = _ranking(candidate)
        proposal = _proposal(
            run_id=SHA_A,
            created_at=AS_OF,
            expires_at="2026-09-15T18:00:00Z",
            subject=_subject(),
            state=DecisionState.AUTO_ELIGIBLE,
            reasons=("SHADOW_ONLY_NO_WRITE_AUTHORITY",),
            selected_key="c001",
            ranking=ranking,
            candidates=(candidate,),
            source_sha256=SHA_A,
            text_catalog_sha256=SHA_B,
            catalog_fingerprint_sha256=SHA_A,
            catalog_generation_token="20260915120000",
            policy=DEFAULT_POLICY,
            lab_code_sha256=SHA_B,
            auto_apply_eligible=False,
        )
        proposal.verify_id()
        tampered = replace(proposal, channel_name="Different Channel")
        with self.assertRaises(ContractError):
            tampered.verify_id()


class MatchingLabProtectedSemanticsTests(unittest.TestCase):
    def test_every_identity_dimension_conflicts_symmetrically(self) -> None:
        cases = (
            (
                "MARKET_MISMATCH",
                ProtectedSemantics(market="US"),
                ProtectedSemantics(market="CA"),
            ),
            (
                "DIRECTION_MISMATCH",
                ProtectedSemantics(direction="EAST"),
                ProtectedSemantics(direction="WEST"),
            ),
            (
                "TIMESHIFT_MISMATCH",
                ProtectedSemantics(timeshift="+1"),
                ProtectedSemantics(timeshift="+2"),
            ),
            (
                "PLUS_VARIANT_MISMATCH",
                ProtectedSemantics(has_plus=True),
                ProtectedSemantics(has_plus=False),
            ),
            (
                "EXTRA_VARIANT_MISMATCH",
                ProtectedSemantics(has_extra=True),
                ProtectedSemantics(has_extra=False),
            ),
            (
                "ALTERNATE_VARIANT_MISMATCH",
                ProtectedSemantics(has_alternate=True),
                ProtectedSemantics(has_alternate=False),
            ),
            (
                "NUMBER_MISMATCH",
                ProtectedSemantics(numbers=("1",)),
                ProtectedSemantics(numbers=("2",)),
            ),
            (
                "LANGUAGE_MISMATCH",
                ProtectedSemantics(languages=("punjabi",)),
                ProtectedSemantics(languages=("hindi",)),
            ),
            (
                "CONTENT_FAMILY_MISMATCH",
                ProtectedSemantics(content=("sports",)),
                ProtectedSemantics(content=("news",)),
            ),
        )
        for expected, left, right in cases:
            with self.subTest(expected=expected, direction="forward"):
                self.assertIn(
                    expected,
                    protected_conflicts(left, right, route_explicit=True),
                )
            with self.subTest(expected=expected, direction="reverse"):
                self.assertIn(
                    expected,
                    protected_conflicts(right, left, route_explicit=True),
                )

    def test_market_requires_explicit_route_and_set_overlap_is_compatible(self) -> None:
        left = ProtectedSemantics(
            market="US", languages=("english", "punjabi"), content=("sports",)
        )
        right = ProtectedSemantics(
            market="CA", languages=("punjabi",), content=("sports", "news")
        )
        implicit = protected_conflicts(left, right, route_explicit=False)
        self.assertNotIn("MARKET_MISMATCH", implicit)
        self.assertNotIn("LANGUAGE_MISMATCH", implicit)
        self.assertNotIn("CONTENT_FAMILY_MISMATCH", implicit)
        self.assertEqual(
            protected_conflicts(left, right, route_explicit=True),
            ("MARKET_MISMATCH",),
        )

    def test_uhd_quality_upgrade_is_a_one_way_conflict(self) -> None:
        hd = ProtectedSemantics(quality="hd")
        unspecified = ProtectedSemantics()
        uhd = ProtectedSemantics(quality="uhd")

        self.assertEqual(hd.quality, "HD")
        self.assertIn(
            "QUALITY_UPGRADE_MISMATCH",
            protected_conflicts(hd, uhd, route_explicit=True),
        )
        self.assertIn(
            "QUALITY_UPGRADE_MISMATCH",
            protected_conflicts(unspecified, uhd, route_explicit=True),
        )
        self.assertNotIn(
            "QUALITY_UPGRADE_MISMATCH",
            protected_conflicts(uhd, hd, route_explicit=True),
        )
        self.assertNotIn(
            "QUALITY_UPGRADE_MISMATCH",
            protected_conflicts(hd, hd, route_explicit=True),
        )

    def test_real_hd_to_uhd_and_4k_contexts_are_blocked(self) -> None:
        engine = automatch._load_frozen_engine()
        cases = (
            (
                "FI: MTV LIIGA HD",
                "FINLAND",
                "MTV.Liiga.UHD.fi",
                "MTV Liiga UHD",
                "FINLAND1",
                "FI",
            ),
            (
                "IT: SKY SPORT HD",
                "ITALY",
                "Sky.Sport.4K.it",
                "Sky Sport 4K",
                "ITALY1",
                "IT",
            ),
        )
        for channel_name, category_name, epg_id, display_name, feed, region in cases:
            with self.subTest(channel_name=channel_name, epg_id=epg_id):
                query = parse_channel_context_v8(engine, channel_name, category_name)
                candidate = parse_candidate_context_v8(
                    engine,
                    {
                        "epg_id": epg_id,
                        "display_name": display_name,
                        "feed": feed,
                        "region": region,
                    },
                )
                conflicts = protected_conflicts(
                    semantics_from_context(query, market=region),
                    semantics_from_context(candidate, market=region),
                    route_explicit=True,
                )
                self.assertIn("QUALITY_UPGRADE_MISMATCH", conflicts)


class MatchingLabDecisionSafetyTests(unittest.TestCase):
    def test_row_eligibility_revalidates_prefilled_review_rows(self) -> None:
        cases = (
            ({"action": "REVIEW", "enabled": "FALSE", "epg_id": ""}, (True, "")),
            (
                {"action": "REVIEW", "enabled": "TRUE", "epg_id": ""},
                (False, "ROW_NOT_DISABLED_REVIEW"),
            ),
            (
                {"action": "APPROVED", "enabled": "FALSE", "epg_id": ""},
                (False, "ROW_NOT_DISABLED_REVIEW"),
            ),
            (
                {
                    "action": "REVIEW",
                    "enabled": "FALSE",
                    "epg_id": "Good.Channel.us2",
                },
                (True, ""),
            ),
            (
                {"action": "REVIEW", "enabled": "not-a-bool", "epg_id": ""},
                (False, "INVALID_REVIEW_STATE"),
            ),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(_eligible_review_row(row), expected)

    def test_v2_standard_threshold_boundaries_and_conservative_exact_gate(self) -> None:
        fuzzy = _candidate(
            methods=("CHAR_NGRAM_RETRIEVAL", "TOKEN_RETRIEVAL"),
            score=800_000,
        )

        def decide(
            candidate: CandidateEvidence,
            *,
            margin: int,
            compatible_count: int = 1,
            risks: tuple[str, ...] = (),
        ) -> tuple[DecisionState, tuple[str, ...], str, bool]:
            ranking = RankedCandidates(
                candidates=(candidate,),
                compatible_count=compatible_count,
                score_ppm=candidate.score_ppm,
                margin_ppm=margin,
                tier=2,
                work_candidates=compatible_count,
            )
            return _decision(
                subject=_subject(),
                ranking=ranking,
                candidates=(candidate,),
                blocked_alert=False,
                eligibility_reason="",
                unsupported=False,
                policy=DEFAULT_POLICY,
                alert_snapshot_present=True,
                cohort_risk_codes=risks,
            )

        state, reasons, _selected, apply_eligible = decide(fuzzy, margin=100_000)
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertIn("LANE_STANDARD_STRONG", reasons)
        self.assertFalse(apply_eligible)

        below_score = replace(fuzzy, score_ppm=799_999)
        state, reasons, _selected, _apply = decide(below_score, margin=100_000)
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("LANE_HUMAN_REVIEW", reasons)

        state, _reasons, _selected, _apply = decide(fuzzy, margin=99_999)
        self.assertIs(state, DecisionState.NEEDS_REVIEW)

        number_risk = ("RISK_EXPLICIT_NUMBER",)
        for channel_name in ("RTE2 vs RTE2FM", "DAZN F1"):
            with self.subTest(channel_name=channel_name):
                state, reasons, _selected, _apply = decide(
                    fuzzy,
                    margin=100_000,
                    risks=number_risk,
                )
                self.assertIs(state, DecisionState.NEEDS_REVIEW)
                self.assertIn(
                    "NON_STRICT_AUTO_BLOCKED_CONSERVATIVE_COHORT", reasons
                )

        strict = replace(fuzzy, methods=("STRICT_EXACT",))
        state, reasons, _selected, _apply = decide(
            strict,
            margin=100_000,
            risks=number_risk,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertIn("LANE_CONSERVATIVE_STRICT_EXACT", reasons)

        state, reasons, _selected, _apply = decide(
            strict,
            margin=100_000,
            compatible_count=2,
            risks=number_risk,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("LANE_HUMAN_REVIEW", reasons)

    def test_cohort_risks_cover_south_asia_and_protected_variants(self) -> None:
        context = _fake_context("Hindi Plus 2 East")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Hindi Plus 2 East",
            category_id="hindi",
            category_name="INDIA | HINDI",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="IN",
            route_plan=("IN",),
            views=NameViews.from_context(context),
            semantics=ProtectedSemantics(
                market="IN",
                direction="EAST",
                timeshift="+1",
                has_plus=True,
                has_extra=True,
                numbers=("2",),
                languages=("hindi",),
            ),
            context=context,
        )

        self.assertEqual(
            set(_cohort_risk_codes(subject)),
            {
                "RISK_DIRECTION_VARIANT",
                "RISK_EDITION_VARIANT",
                "RISK_EXPLICIT_LANGUAGE",
                "RISK_EXPLICIT_NUMBER",
                "RISK_PLUS_VARIANT",
                "RISK_SOUTH_ASIA_COHORT",
                "RISK_TIMESHIFT_VARIANT",
            },
        )

    def test_uppercase_me_subbrand_is_a_conservative_edition(self) -> None:
        context = _fake_context("ABC ME MELBOURNE")
        subject = LabSubject(
            server_id="server_3",
            stream_id="259830",
            channel_name="AU - ABC ME MELBOURNE",
            category_id="australia",
            category_name="|AU| AUSTRALIA",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="AU",
            route_plan=("AU",),
            views=NameViews.from_context(context),
            semantics=ProtectedSemantics(market="AU"),
            context=context,
        )

        self.assertIn("RISK_EDITION_VARIANT", _cohort_risk_codes(subject))

    def test_real_parser_marks_rte2_and_dazn_f1_as_number_sensitive(self) -> None:
        engine = automatch._load_frozen_engine()
        cases = (
            ("RTE2", "IRELAND | TV"),
            ("RTE2FM", "IRELAND | RADIO"),
            ("DAZN F1", "SPAIN | SPORTS"),
        )
        for index, (channel_name, category_name) in enumerate(cases, start=1):
            row = _mapping_row(stream_id=str(index), channel_name=channel_name)
            row["category_name"] = category_name
            subject = subject_from_mapping_row(engine, row)
            with self.subTest(channel_name=channel_name):
                self.assertTrue(subject.semantics.numbers)
                self.assertIn(
                    "RISK_EXPLICIT_NUMBER", _cohort_risk_codes(subject)
                )

    def test_programme_failure_and_missing_alert_snapshot_cannot_promote(self) -> None:
        raw = _candidate(state=ProgrammeState.NOT_CHECKED)
        failed = _programme_candidate(
            raw,
            SimpleNamespace(
                passed=False,
                distinct_informative_programmes=1,
                first_start_epoch=100,
                latest_stop_epoch=200,
                reason="insufficient horizon",
            ),
        )
        self.assertIs(failed.programme_state, ProgrammeState.FAIL)
        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=_ranking(failed),
            candidates=(failed,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=MatchingPolicy(auto_apply_enabled=True),
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.PENDING_PROGRAMME)
        self.assertIn("PROGRAMME_GATE_FAILED", reasons)
        self.assertEqual(selected, "c001")
        self.assertFalse(apply_eligible)

        passed = _candidate(state=ProgrammeState.PASS)
        state, reasons, _selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=_ranking(passed),
            candidates=(passed,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=MatchingPolicy(auto_apply_enabled=True),
            alert_snapshot_present=False,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("ALERT_SNAPSHOT_MISSING", reasons)
        self.assertFalse(apply_eligible)

    def test_even_strong_shadow_evidence_never_grants_apply_authority(self) -> None:
        candidate = _candidate(state=ProgrammeState.PASS)
        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=_ranking(candidate),
            candidates=(candidate,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=MatchingPolicy(auto_apply_enabled=True),
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("SHADOW_ONLY_NO_WRITE_AUTHORITY", reasons)
        self.assertFalse(apply_eligible)

        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=_ranking(candidate),
            candidates=(candidate,),
            blocked_alert=False,
            eligibility_reason="ROW_NOT_DISABLED_REVIEW",
            unsupported=False,
            policy=MatchingPolicy(auto_apply_enabled=True),
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.CONFLICT)
        self.assertEqual(reasons, ("ROW_NOT_DISABLED_REVIEW",))
        self.assertEqual(selected, "")
        self.assertFalse(apply_eligible)

    def test_trusted_alias_can_cross_score_gate_but_stays_read_only(self) -> None:
        candidate = _candidate(
            state=ProgrammeState.PASS,
            methods=("CURATED_ALIAS_EXACT",),
            score=DEFAULT_POLICY.strong_proposal_score_ppm - 1,
        )
        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=_ranking(candidate),
            candidates=(candidate,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_TRUSTED_ALIAS", reasons)
        self.assertIn("TRUSTED_ALIAS_SCORE_OVERRIDE", reasons)
        self.assertIn("SHADOW_ONLY_NO_WRITE_AUTHORITY", reasons)
        self.assertFalse(apply_eligible)

        low_margin = replace(
            _ranking(candidate),
            margin_ppm=DEFAULT_POLICY.strong_margin_ppm - 1,
        )
        state, reasons, _selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=low_margin,
            candidates=(candidate,),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertNotIn("LANE_TRUSTED_ALIAS", reasons)
        self.assertFalse(apply_eligible)

    def test_frozen_exact_is_supplemental_to_independent_auto_lanes(self) -> None:
        standard = _candidate(
            state=ProgrammeState.PASS,
            methods=(
                "FROZEN_RESOLVER_EXACT",
                "STRICT_EXACT",
                "TOKEN_RETRIEVAL",
            ),
            score=950_000,
        )
        alternate = replace(
            _candidate(methods=("TOKEN_RETRIEVAL",), score=600_000),
            candidate_key="c002",
            epg_id="Other.Channel.us2",
            display_name="Other Channel",
        )

        def ranking(top: CandidateEvidence) -> RankedCandidates:
            return RankedCandidates(
                candidates=(top, alternate),
                compatible_count=2,
                score_ppm=top.score_ppm,
                margin_ppm=top.score_ppm - alternate.score_ppm,
                tier=5,
                work_candidates=2,
            )

        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=ranking(standard),
            candidates=(standard, alternate),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_STANDARD_STRONG", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_TOP", reasons)
        self.assertIn("MARGIN_GATE_PASSED", reasons)
        self.assertNotIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertNotIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertNotIn("AMBIGUOUS_CANDIDATES", reasons)
        self.assertFalse(apply_eligible)

        route_mismatch = replace(standard, region="CA")
        state, reasons, _selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=ranking(route_mismatch),
            candidates=(route_mismatch, alternate),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("TARGET_ROUTE_MISMATCH", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertNotIn("LANE_STANDARD_STRONG", reasons)
        self.assertFalse(apply_eligible)

        trusted_alias = replace(
            standard,
            methods=(
                "CURATED_ALIAS_EXACT",
                "FROZEN_RESOLVER_EXACT",
                "STRICT_EXACT",
            ),
        )
        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=ranking(trusted_alias),
            candidates=(trusted_alias, alternate),
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_TRUSTED_ALIAS", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_TOP", reasons)
        self.assertNotIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertNotIn("AMBIGUOUS_CANDIDATES", reasons)
        self.assertFalse(apply_eligible)

    def test_repository_curated_alias_has_a_narrow_score_margin_override(self) -> None:
        repository_alias = _candidate(
            state=ProgrammeState.PASS,
            methods=(
                "CURATED_ALIAS_EXACT",
                "REPOSITORY_CURATED_ALIAS_EXACT",
            ),
            score=DEFAULT_POLICY.minimum_candidate_score_ppm - 1,
        )
        alternate = replace(
            _candidate(methods=("TOKEN_RETRIEVAL",), score=700_000),
            candidate_key="c002",
            epg_id="Other.Channel.us2",
            display_name="Other Channel",
        )

        def decide(
            top: CandidateEvidence,
            *,
            candidates: tuple[CandidateEvidence, ...] | None = None,
            alert_snapshot_present: bool = True,
        ) -> tuple[DecisionState, tuple[str, ...], str, bool]:
            values = candidates or (top, alternate)
            ranking = RankedCandidates(
                candidates=values,
                compatible_count=sum(not item.conflicts for item in values),
                score_ppm=top.score_ppm,
                margin_ppm=0,
                tier=6,
                work_candidates=len(values),
            )
            return _decision(
                subject=_subject(),
                ranking=ranking,
                candidates=values,
                blocked_alert=False,
                eligibility_reason="",
                unsupported=False,
                policy=DEFAULT_POLICY,
                alert_snapshot_present=alert_snapshot_present,
            )

        state, reasons, selected, apply_eligible = decide(repository_alias)
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)
        self.assertIn("REPOSITORY_CURATED_ALIAS_ALLOWLISTED", reasons)
        self.assertIn("REPOSITORY_CURATED_ALIAS_SCORE_OVERRIDE", reasons)
        self.assertIn("REPOSITORY_CURATED_ALIAS_MARGIN_OVERRIDE", reasons)
        self.assertNotIn("MARGIN_GATE_PASSED", reasons)
        self.assertFalse(apply_eligible)

        combined_exact = replace(
            repository_alias,
            methods=(
                "CURATED_ALIAS_EXACT",
                "FROZEN_RESOLVER_EXACT",
                "REPOSITORY_CURATED_ALIAS_EXACT",
            ),
        )
        state, reasons, selected, apply_eligible = decide(combined_exact)
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_TOP", reasons)
        self.assertIn("TARGET_ROUTE_AGREES", reasons)
        self.assertNotIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertFalse(apply_eligible)

        ordinary_curated = replace(
            repository_alias,
            methods=("CURATED_ALIAS_EXACT",),
        )
        state, reasons, _selected, _apply_eligible = decide(ordinary_curated)
        self.assertIs(state, DecisionState.ABSTAIN)
        self.assertNotIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

        state, reasons, _selected, _apply_eligible = decide(
            repository_alias,
            alert_snapshot_present=False,
        )
        self.assertIs(state, DecisionState.ABSTAIN)
        self.assertNotIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

        failed_programme = replace(
            repository_alias,
            programme_state=ProgrammeState.FAIL,
        )
        state, reasons, _selected, _apply_eligible = decide(failed_programme)
        self.assertIs(state, DecisionState.ABSTAIN)
        self.assertNotIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

        second_curated = replace(
            alternate,
            methods=("CURATED_ALIAS_EXACT",),
        )
        state, reasons, _selected, _apply_eligible = decide(
            repository_alias,
            candidates=(repository_alias, second_curated),
        )
        self.assertIs(state, DecisionState.ABSTAIN)
        self.assertNotIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

        competing_frozen = replace(
            alternate,
            methods=("FROZEN_RESOLVER_EXACT",),
        )
        state, reasons, _selected, _apply_eligible = decide(
            repository_alias,
            candidates=(repository_alias, competing_frozen),
        )
        self.assertIsNot(state, DecisionState.AUTO_ELIGIBLE)
        self.assertNotIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

        failed_competing_frozen = replace(
            competing_frozen,
            programme_state=ProgrammeState.FAIL,
        )
        state, reasons, selected, _apply_eligible = decide(
            repository_alias,
            candidates=(repository_alias, failed_competing_frozen),
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_REPOSITORY_CURATED_ALIAS", reasons)

    def test_frozen_resolver_exact_requires_every_promotion_gate(self) -> None:
        candidate = _candidate(
            state=ProgrammeState.PASS,
            methods=("FROZEN_RESOLVER_EXACT",),
            score=DEFAULT_POLICY.minimum_candidate_score_ppm - 1,
        )

        def subject(**changes: object) -> SimpleNamespace:
            values = vars(_subject()).copy()
            values.update(changes)
            return SimpleNamespace(**values)

        def decide(
            value: CandidateEvidence,
            *,
            current_subject: SimpleNamespace | None = None,
            alert_snapshot_present: bool = True,
            current_ranking: RankedCandidates | None = None,
            risks: tuple[str, ...] = (),
            evidence: tuple[str, ...] = (),
        ) -> tuple[DecisionState, tuple[str, ...], str, bool]:
            selected_ranking = current_ranking or _ranking(value)
            return _decision(
                subject=current_subject or _subject(),
                ranking=selected_ranking,
                candidates=selected_ranking.candidates,
                blocked_alert=False,
                eligibility_reason="",
                unsupported=False,
                policy=DEFAULT_POLICY,
                alert_snapshot_present=alert_snapshot_present,
                cohort_risk_codes=risks,
                evidence_reason_codes=evidence,
            )

        state, reasons, selected, apply_eligible = decide(candidate)
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("FROZEN_RESOLVER_EXACT_TOP", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_SCORE_OVERRIDE", reasons)
        self.assertIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertNotIn("MARGIN_GATE_PASSED", reasons)
        self.assertIn("SHADOW_ONLY_NO_WRITE_AUTHORITY", reasons)
        self.assertFalse(apply_eligible)

        fuzzy_alternate = replace(
            _candidate(
                methods=("CHAR_NGRAM_RETRIEVAL", "TOKEN_RETRIEVAL"),
                score=candidate.score_ppm - 200_000,
            ),
            candidate_key="c002",
            epg_id="Other.Channel.us2",
            display_name="Other Channel",
        )
        expanded_ranking = RankedCandidates(
            candidates=(candidate, fuzzy_alternate),
            compatible_count=2,
            score_ppm=candidate.score_ppm,
            margin_ppm=200_000,
            tier=5,
            work_candidates=2,
        )
        state, reasons, selected, apply_eligible = decide(
            candidate,
            current_ranking=expanded_ranking,
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertEqual(selected, "c001")
        self.assertIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertIn("MARGIN_GATE_PASSED", reasons)
        self.assertNotIn("AMBIGUOUS_CANDIDATES", reasons)
        self.assertFalse(apply_eligible)

        cases = (
            (
                "implicit market",
                candidate,
                subject(route_explicit=False, market="", route_plan=("US",)),
                True,
                DecisionState.NEEDS_REVIEW,
                "MARKET_UNKNOWN",
            ),
            (
                "diaspora multi route",
                candidate,
                subject(market="NA_DIASPORA", route_plan=("US", "CA")),
                True,
                DecisionState.NEEDS_REVIEW,
                "MARKET_UNKNOWN",
            ),
            (
                "programme fail",
                replace(candidate, programme_state=ProgrammeState.FAIL),
                _subject(),
                True,
                DecisionState.PENDING_PROGRAMME,
                "PROGRAMME_GATE_FAILED",
            ),
            (
                "semantic conflict",
                replace(candidate, conflicts=("MARKET_MISMATCH",)),
                _subject(),
                True,
                DecisionState.CONFLICT,
                "PROTECTED_SEMANTICS_CONFLICT",
            ),
            (
                "missing alert snapshot",
                candidate,
                _subject(),
                False,
                DecisionState.NEEDS_REVIEW,
                "ALERT_SNAPSHOT_MISSING",
            ),
            (
                "target route mismatch",
                replace(candidate, region="CA"),
                _subject(),
                True,
                DecisionState.NEEDS_REVIEW,
                "TARGET_ROUTE_MISMATCH",
            ),
            (
                "explicit market route mismatch",
                candidate,
                subject(market="MENA", route_plan=("US",)),
                True,
                DecisionState.NEEDS_REVIEW,
                "TARGET_ROUTE_MISMATCH",
            ),
        )
        for label, value, current_subject, alerts_present, expected, reason in cases:
            with self.subTest(label=label):
                state, reasons, _selected, apply_eligible = decide(
                    value,
                    current_subject=current_subject,
                    alert_snapshot_present=alerts_present,
                )
                self.assertIs(state, expected)
                self.assertIn(reason, reasons)
                self.assertFalse(apply_eligible)

        alternate = replace(
            _candidate(
                methods=("STRICT_EXACT",),
                score=candidate.score_ppm - 200_000,
            ),
            candidate_key="c002",
            epg_id="Good.Channel.us_locals1",
            feed="US_LOCALS1",
        )
        ambiguous_ranking = RankedCandidates(
            candidates=(candidate, alternate),
            compatible_count=2,
            score_ppm=candidate.score_ppm,
            margin_ppm=200_000,
            tier=5,
            work_candidates=2,
        )
        state, reasons, selected, apply_eligible = decide(
            candidate,
            current_ranking=ambiguous_ranking,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertEqual(selected, "c001")
        self.assertIn("AMBIGUOUS_CANDIDATES", reasons)
        self.assertIn("MARGIN_GATE_PASSED", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertFalse(apply_eligible)

        # Preserve the audited legacy path: when the routed catalog contains
        # only one compatible target, its original risk/prefill behavior and
        # score/margin override remain unchanged.
        legacy_low_score = replace(candidate, score_ppm=99_999)
        legacy_low_ranking = RankedCandidates(
            candidates=(legacy_low_score,),
            compatible_count=1,
            score_ppm=legacy_low_score.score_ppm,
            margin_ppm=legacy_low_score.score_ppm,
            tier=5,
            work_candidates=1,
        )
        state, reasons, _selected, apply_eligible = decide(
            legacy_low_score,
            current_ranking=legacy_low_ranking,
            risks=("RISK_EXPLICIT_NUMBER",),
            evidence=(
                "PREFILLED_ID_NOT_CORROBORATED",
                "PREFILLED_ID_UNTRUSTED_EVIDENCE",
            ),
        )
        self.assertIs(state, DecisionState.AUTO_ELIGIBLE)
        self.assertIn("RISK_EXPLICIT_NUMBER", reasons)
        self.assertIn("PREFILLED_ID_UNTRUSTED_EVIDENCE", reasons)
        self.assertIn("LANE_FROZEN_RESOLVER_EXACT", reasons)
        self.assertNotIn("MARGIN_GATE_PASSED", reasons)
        self.assertFalse(apply_eligible)

        state, reasons, _selected, apply_eligible = decide(
            candidate,
            current_ranking=expanded_ranking,
            risks=("RISK_EXPLICIT_NUMBER",),
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("RISK_EXPLICIT_NUMBER", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertFalse(apply_eligible)

        close_fuzzy = replace(
            fuzzy_alternate,
            score_ppm=candidate.score_ppm - DEFAULT_POLICY.strong_margin_ppm + 1,
        )
        low_margin_ranking = RankedCandidates(
            candidates=(candidate, close_fuzzy),
            compatible_count=2,
            score_ppm=candidate.score_ppm,
            margin_ppm=DEFAULT_POLICY.strong_margin_ppm - 1,
            tier=5,
            work_candidates=2,
        )
        state, reasons, _selected, apply_eligible = decide(
            candidate,
            current_ranking=low_margin_ranking,
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("LOW_MARGIN", reasons)
        self.assertNotIn("AMBIGUOUS_CANDIDATES", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertFalse(apply_eligible)

        state, reasons, _selected, apply_eligible = decide(
            candidate,
            current_ranking=expanded_ranking,
            evidence=(
                "PREFILLED_ID_NOT_CORROBORATED",
                "PREFILLED_ID_UNTRUSTED_EVIDENCE",
            ),
        )
        self.assertIs(state, DecisionState.NEEDS_REVIEW)
        self.assertIn("PREFILLED_ID_UNTRUSTED_EVIDENCE", reasons)
        self.assertIn("FROZEN_RESOLVER_EXACT_GATES_FAILED", reasons)
        self.assertFalse(apply_eligible)

        conflicted_exact = replace(candidate, conflicts=("MARKET_MISMATCH",))
        compatible_fallback = replace(
            _candidate(
                methods=("CHAR_NGRAM_RETRIEVAL", "TOKEN_RETRIEVAL"),
                score=900_000,
            ),
            candidate_key="c002",
            epg_id="Fallback.Channel.us2",
            display_name="Fallback Channel",
        )
        fallback_ranking = RankedCandidates(
            candidates=(compatible_fallback, conflicted_exact),
            compatible_count=1,
            score_ppm=compatible_fallback.score_ppm,
            margin_ppm=compatible_fallback.score_ppm,
            tier=2,
            work_candidates=2,
        )
        state, reasons, selected, apply_eligible = _decision(
            subject=_subject(),
            ranking=fallback_ranking,
            candidates=fallback_ranking.candidates,
            blocked_alert=False,
            eligibility_reason="",
            unsupported=False,
            policy=DEFAULT_POLICY,
            alert_snapshot_present=True,
        )
        self.assertIs(state, DecisionState.CONFLICT)
        self.assertEqual(selected, "")
        self.assertIn("FROZEN_RESOLVER_EXACT_SEMANTIC_CONFLICT", reasons)
        self.assertFalse(apply_eligible)


class MatchingLabRetrievalSafetyTests(unittest.TestCase):
    def test_repository_override_accepts_identity_relationships_only(self) -> None:
        targets, _digest = retrieval.repository_curated_alias_targets()

        self.assertEqual(
            targets[(NameViews.from_text("FXM").strict, "US")],
            "FX.Movie.Channel.HD.us2",
        )
        self.assertNotIn(
            (NameViews.from_text("BBC 2 Scotland").strict, "UK"),
            targets,
        )

    def test_only_fixed_repository_aliases_receive_repository_authority(self) -> None:
        subject_context = _fake_context("FXM")
        candidate_context = _fake_context("FX Movie Channel")
        subject = LabSubject(
            server_id="server_2",
            stream_id="608673",
            channel_name="US FXM",
            category_id="movies",
            category_name="US | Movies",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw = {
            "epg_id": "FX.Movie.Channel.HD.us2",
            "display_name": "FX Movie Channel",
            "feed": "US2",
            "region": "US",
        }
        _targets, repository_digest = (
            retrieval.repository_curated_alias_targets()
        )
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            repository_index = CandidateIndex(
                engine=object(),
                candidates=[raw],
                repository_aliases_sha256=repository_digest,
            )
        repository_index.attach_resolver(
            SimpleNamespace(
                _approved_alias_match=mock.Mock(
                    return_value={
                        "epg_id": "FX.Movie.Channel.HD.us2",
                        "match_method": "approved_knowledge",
                    }
                )
            )
        )
        ranked = repository_index.rank(subject)
        self.assertIn("CURATED_ALIAS_EXACT", ranked.candidates[0].methods)
        self.assertIn(
            "REPOSITORY_CURATED_ALIAS_EXACT",
            ranked.candidates[0].methods,
        )

        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            in_memory_index = CandidateIndex(
                engine=object(),
                candidates=[raw],
                aliases=(
                    retrieval.AliasEdge(
                        alias="FXM",
                        market="US",
                        epg_id="FX.Movie.Channel.HD.us2",
                        provenance="curated",
                    ),
                ),
            )
        in_memory = in_memory_index.rank(subject)
        self.assertIn("CURATED_ALIAS_EXACT", in_memory.candidates[0].methods)
        self.assertNotIn(
            "REPOSITORY_CURATED_ALIAS_EXACT",
            in_memory.candidates[0].methods,
        )

        with self.assertRaisesRegex(ContractError, "changed after matcher setup"):
            with mock.patch.object(
                retrieval,
                "parse_candidate_context_v8",
                return_value=candidate_context,
            ):
                CandidateIndex(
                    engine=object(),
                    candidates=[raw],
                    repository_aliases_sha256="0" * 64,
                )

    def test_attached_resolver_curated_alias_retrieves_current_catalog_target(self) -> None:
        subject_context = _fake_context("Approved Alias")
        candidate_context = _fake_context("Opaque Catalog Name")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Approved Alias",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw = {
            "epg_id": "opaque.station.us2",
            "display_name": "Opaque Catalog Name",
            "feed": "US2",
            "region": "US",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            index = CandidateIndex(engine=object(), candidates=[raw])
        resolver = SimpleNamespace(
            _approved_alias_match=mock.Mock(
                return_value={"epg_id": "opaque.station.us2"}
            )
        )
        index.attach_resolver(resolver)

        ranked = index.rank(subject)

        resolver._approved_alias_match.assert_called_once_with(subject_context)
        self.assertEqual([item.epg_id for item in ranked.candidates], ["opaque.station.us2"])
        self.assertIn("CURATED_ALIAS_EXACT", ranked.candidates[0].methods)
        self.assertEqual(ranked.tier, 6)

    def test_frozen_contextual_exact_retrieves_only_current_allowed_target(self) -> None:
        subject_context = _fake_context("Provider Wrapped Name")
        candidate_context = _fake_context("Opaque Catalog Name")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Provider Wrapped Name",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw = {
            "epg_id": "opaque.station.us2",
            "display_name": "Opaque Catalog Name",
            "feed": "US2",
            "region": "US",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            index = CandidateIndex(engine=object(), candidates=[raw])
        resolver = SimpleNamespace(
            _contextual_exact_match=mock.Mock(
                return_value={
                    "action": "AUTO_EPGSHARE",
                    "source": "epgshare",
                    "epg_id": "opaque.station.us2",
                    "match_method": "canonical_identity",
                }
            )
        )
        index.attach_resolver(resolver)

        ranked = index.rank(subject)

        resolver._contextual_exact_match.assert_called_once_with(subject_context)
        self.assertEqual([item.epg_id for item in ranked.candidates], ["opaque.station.us2"])
        self.assertIn("FROZEN_RESOLVER_EXACT", ranked.candidates[0].methods)
        self.assertEqual(ranked.tier, 5)

    def test_frozen_contextual_exact_is_not_invoked_for_implicit_or_diaspora_route(self) -> None:
        subject_context = _fake_context("Provider Wrapped Name")
        candidate_context = _fake_context("Opaque Catalog Name")
        raw = {
            "epg_id": "opaque.station.us2",
            "display_name": "Opaque Catalog Name",
            "feed": "US2",
            "region": "US",
        }

        for label, route_explicit, market, route_plan in (
            ("implicit", False, "", ("US",)),
            ("diaspora", True, "NA_DIASPORA", ("US", "CA")),
        ):
            with self.subTest(label=label), mock.patch.object(
                retrieval,
                "parse_candidate_context_v8",
                return_value=candidate_context,
            ):
                index = CandidateIndex(engine=object(), candidates=[raw])
                exact = mock.Mock(
                    return_value={
                        "action": "AUTO_EPGSHARE",
                        "source": "epgshare",
                        "epg_id": "opaque.station.us2",
                        "match_method": "strict",
                    }
                )
                index.attach_resolver(SimpleNamespace(_contextual_exact_match=exact))
                subject = LabSubject(
                    server_id="server_1",
                    stream_id="101",
                    channel_name="Provider Wrapped Name",
                    category_id="general",
                    category_name="General",
                    row_guard_sha256=SHA_A,
                    provider_identity_sha256=SHA_B,
                    route_explicit=route_explicit,
                    market=market,
                    route_plan=route_plan,
                    views=NameViews.from_context(subject_context),
                    semantics=ProtectedSemantics(market=market),
                    context=subject_context,
                )

                ranked = index.rank(subject)

                exact.assert_not_called()
                self.assertTrue(
                    all(
                        "FROZEN_RESOLVER_EXACT" not in candidate.methods
                        for candidate in ranked.candidates
                    )
                )

    def test_frozen_contextual_exact_rejects_untrusted_results(self) -> None:
        subject_context = _fake_context("Provider Wrapped Name")
        candidate_context = _fake_context("Opaque Catalog Name")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Provider Wrapped Name",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw_candidates = [
            {
                "epg_id": "opaque.station.us2",
                "display_name": "Opaque Catalog Name",
                "feed": "US2",
                "region": "US",
            },
            {
                "epg_id": "opaque.station.ca2",
                "display_name": "Opaque Canadian Name",
                "feed": "CA2",
                "region": "CA",
            },
        ]
        invalid_results = (
            {
                "action": "REVIEW",
                "source": "epgshare",
                "epg_id": "opaque.station.us2",
                "match_method": "strict",
            },
            {
                "action": "AUTO_EPGSHARE",
                "source": "epgshare_candidate",
                "epg_id": "opaque.station.us2",
                "match_method": "strict",
            },
            {
                "action": "AUTO_EPGSHARE",
                "source": "epgshare",
                "epg_id": "opaque.station.us2",
                "match_method": "near_exact_orthography",
            },
            {
                "action": "AUTO_EPGSHARE",
                "source": "epgshare",
                "epg_id": "opaque.station.ca2",
                "match_method": "strict",
            },
            {
                "action": "AUTO_EPGSHARE",
                "source": "epgshare",
                "epg_id": "missing.station.us2",
                "match_method": "strict",
            },
        )
        for result in invalid_results:
            with self.subTest(result=result), mock.patch.object(
                retrieval,
                "parse_candidate_context_v8",
                side_effect=lambda _engine, row: _fake_context(row["display_name"]),
            ):
                index = CandidateIndex(engine=object(), candidates=raw_candidates)
                index.attach_resolver(
                    SimpleNamespace(_contextual_exact_match=mock.Mock(return_value=result))
                )

                ranked = index.rank(subject)

                self.assertTrue(
                    all(
                        "FROZEN_RESOLVER_EXACT" not in candidate.methods
                        for candidate in ranked.candidates
                    )
                )

        for method in (
            "fuzzy",
            "safety_rule",
            "exact_numbered_event_bank",
            "panel",
            "contextual_containment",
            "near_exact_orthography",
            "unknown_future_method",
        ):
            with self.subTest(method=method), mock.patch.object(
                retrieval,
                "parse_candidate_context_v8",
                side_effect=lambda _engine, row: _fake_context(row["display_name"]),
            ):
                index = CandidateIndex(engine=object(), candidates=raw_candidates)
                index.attach_resolver(
                    SimpleNamespace(
                        _contextual_exact_match=mock.Mock(
                            return_value={
                                "action": "AUTO_EPGSHARE",
                                "source": "epgshare",
                                "epg_id": "opaque.station.us2",
                                "match_method": method,
                            }
                        )
                    )
                )

                ranked = index.rank(subject)

                self.assertTrue(
                    all(
                        "FROZEN_RESOLVER_EXACT" not in candidate.methods
                        for candidate in ranked.candidates
                    )
                )

    def test_curated_alias_target_outside_allowed_region_is_rejected(self) -> None:
        subject_context = _fake_context("Approved Alias")
        candidate_context = _fake_context("Opaque Catalog Name")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Approved Alias",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw = {
            "epg_id": "opaque.station.ca",
            "display_name": "Opaque Catalog Name",
            "feed": "CA1",
            "region": "CA",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            index = CandidateIndex(engine=object(), candidates=[raw])
        index.attach_resolver(
            SimpleNamespace(
                _approved_alias_match=mock.Mock(
                    return_value={"epg_id": "opaque.station.ca"}
                )
            )
        )

        ranked = index.rank(subject)

        self.assertEqual(ranked.candidates, ())
        self.assertEqual(ranked.work_candidates, 0)

    def test_curated_alias_target_absent_from_current_catalog_is_ignored(self) -> None:
        subject_context = _fake_context("Approved Alias")
        candidate_context = _fake_context("Opaque Catalog Name")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Approved Alias",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(subject_context),
            semantics=ProtectedSemantics(market="US"),
            context=subject_context,
        )
        raw = {
            "epg_id": "opaque.station.us2",
            "display_name": "Opaque Catalog Name",
            "feed": "US2",
            "region": "US",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=candidate_context,
        ):
            index = CandidateIndex(engine=object(), candidates=[raw])
        index.attach_resolver(
            SimpleNamespace(
                _approved_alias_match=mock.Mock(
                    return_value={"epg_id": "missing.station.us2"}
                )
            )
        )

        ranked = index.rank(subject)

        self.assertEqual(ranked.candidates, ())
        self.assertEqual(ranked.work_candidates, 0)

    def test_attached_frozen_scorer_failure_aborts_instead_of_falling_back(self) -> None:
        context = _fake_context("Alpha News")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Alpha News",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(context),
            semantics=ProtectedSemantics(market="US"),
            context=context,
        )
        raw = {
            "epg_id": "Alpha.News.us2",
            "display_name": "Alpha News",
            "feed": "US2",
            "region": "US",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            return_value=context,
        ):
            index = CandidateIndex(engine=object(), candidates=[raw])
        index.attach_resolver(
            SimpleNamespace(
                _fuzzy_score=mock.Mock(side_effect=RuntimeError("scorer failed"))
            )
        )
        with self.assertRaisesRegex(ContractError, "contextual scorer failed"):
            index.rank(subject)

    def test_current_xml_display_name_can_retrieve_an_opaque_id_for_review(self) -> None:
        context = _fake_context("Morning News")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Morning News",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(context),
            semantics=ProtectedSemantics(market="US"),
            context=context,
        )
        raw = {
            "epg_id": "opaque.station.us2",
            "display_name": "Opaque Station",
            "feed": "US2",
            "region": "US",
        }
        with mock.patch.object(
            retrieval,
            "parse_candidate_context_v8",
            side_effect=lambda _engine, row: _fake_context(row["display_name"]),
        ):
            index = CandidateIndex(
                engine=object(),
                candidates=[raw],
                xml_display_names={"opaque.station.us2": "Morning News"},
            )
            ranked = index.rank(subject)
        self.assertEqual([item.epg_id for item in ranked.candidates], ["opaque.station.us2"])
        self.assertIn("XML_DISPLAY_RETRIEVAL", ranked.candidates[0].methods)
        self.assertNotIn("STRICT_EXACT", ranked.candidates[0].methods)

    def test_candidate_order_and_bounds_ignore_catalog_input_order(self) -> None:
        raw_candidates = [
            {
                "epg_id": f"Alpha.{number:02d}.us2",
                "display_name": "Alpha News",
                "feed": "US2",
                "region": "US",
            }
            for number in range(12)
        ]
        policy = MatchingPolicy(
            maximum_retrieval_candidates=8,
            retained_candidates=3,
        )
        context = _fake_context("Alpha News")
        subject = LabSubject(
            server_id="server_1",
            stream_id="101",
            channel_name="Alpha News",
            category_id="general",
            category_name="US | General",
            row_guard_sha256=SHA_A,
            provider_identity_sha256=SHA_B,
            route_explicit=True,
            market="US",
            route_plan=("US",),
            views=NameViews.from_context(context),
            semantics=ProtectedSemantics(market="US"),
            context=context,
        )

        def rank(values: list[dict[str, str]]) -> RankedCandidates:
            with mock.patch.object(
                retrieval,
                "parse_candidate_context_v8",
                side_effect=lambda _engine, row: _fake_context(row["display_name"]),
            ):
                index = CandidateIndex(
                    engine=object(), candidates=values, policy=policy
                )
                return index.rank(subject)

        forward = rank(raw_candidates)
        reverse = rank(list(reversed(raw_candidates)))
        self.assertEqual(forward.work_candidates, 8)
        self.assertEqual(forward.compatible_count, 8)
        self.assertEqual(len(forward.candidates), 3)
        self.assertEqual(
            [candidate.epg_id for candidate in forward.candidates],
            ["Alpha.00.us2", "Alpha.01.us2", "Alpha.02.us2"],
        )
        self.assertEqual(
            [candidate.public_dict() for candidate in forward.candidates],
            [candidate.public_dict() for candidate in reverse.candidates],
        )


class MatchingLabNoWriterTests(unittest.TestCase):
    def test_shadow_pipeline_invokes_no_google_sheet_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "mappings.csv"
            alerts = root / "alerts.csv"
            source = root / "all.xml.gz"
            catalog = root / "all.txt"
            output = root / "output"
            mappings.write_bytes(
                _csv_bytes(
                    tuple(streaming.SHEET_COLUMNS),
                    [_mapping_row(stream_id="101", channel_name="US: Good Channel")],
                )
            )
            alerts.write_bytes(_csv_bytes(tuple(sync.ALERT_COLUMNS), []))
            with gzip.GzipFile(filename=str(source), mode="wb", mtime=0) as handle:
                handle.write(_xml_bytes())
            catalog.write_text(
                "20260915120000\n"
                "-- epg_ripper_US2 --\n"
                "Good.Channel.us2\n"
                "Other.Channel.us2\n",
                encoding="utf-8",
            )

            writer_names = (
                "append_google_sheet_rows",
                "append_sync_alert_rows",
                "update_google_sheet_review_rows",
            )
            patches = [
                mock.patch.object(
                    sync,
                    name,
                    side_effect=AssertionError(f"shadow pipeline invoked {name}"),
                )
                for name in writer_names
            ]
            writers = [patcher.start() for patcher in patches]
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])

            result = run_shadow(
                mappings_csv=mappings,
                alerts_csv=alerts,
                all_source_file=source,
                all_source_catalog_file=catalog,
                output_dir=output,
                as_of=AS_OF,
                minimum_unique_channels=2,
            )
            self.assertEqual(result.proposal_count, 1)
            for writer in writers:
                writer.assert_not_called()
            proposal = json.loads((output / "proposals.jsonl").read_text("utf-8"))
            self.assertFalse(proposal["decision"]["auto_apply_eligible"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
