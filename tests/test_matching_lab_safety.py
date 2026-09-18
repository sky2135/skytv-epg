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
from matching_lab.compat import automatch, streaming, sync
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
from matching_lab.normalization import NameViews, protected_conflicts, words
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


class MatchingLabRetrievalSafetyTests(unittest.TestCase):
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
