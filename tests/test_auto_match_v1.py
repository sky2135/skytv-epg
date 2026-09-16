from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_epg_auto_match_v1 import (  # noqa: E402
    CatalogSnapshot,
    MatcherIdentity,
    MatcherPreflight,
    MIN_FUTURE_HORIZON_SECONDS,
    MIN_INFORMATIVE_FUTURE_PROGRAMMES,
    ScheduleEvidence,
    STRICT_ENGINE_SOURCE_SHA256,
    STRICT_MATCHER_BUILD_ID,
    STRICT_MATCHER_SOURCE_SHA256,
    STRICT_MATCHER_VERSION,
    finalize_proposal,
    prepare_resolver_strict,
    propose_new_channel_matches,
)


GOOD_SHA = STRICT_MATCHER_SOURCE_SHA256
CATALOG_SHA = "b" * 64
SOURCE_SHA = "c" * 64


def identity() -> MatcherIdentity:
    return MatcherIdentity(
        version=STRICT_MATCHER_VERSION,
        build_id=STRICT_MATCHER_BUILD_ID,
        source_sha256=GOOD_SHA,
        engine_source_sha256=STRICT_ENGINE_SOURCE_SHA256,
    )


def catalog(
    *ids: str,
    region: str = "US",
    kind: str = "real",
) -> CatalogSnapshot:
    return CatalogSnapshot.from_ids(
        ids,
        source_sha256=CATALOG_SHA,
        regions_by_id={item: {region} for item in ids},
        kinds_by_id={item: {kind} for item in ids},
    )


def evidence(
    *ids: str,
    count: int = MIN_INFORMATIVE_FUTURE_PROGRAMMES,
    gate_passed: bool = True,
    source_sha256: str = CATALOG_SHA,
) -> ScheduleEvidence:
    checked_at = 1_000
    return ScheduleEvidence(
        declared_ids=frozenset(ids),
        informative_future_programmes={item: count for item in ids},
        latest_informative_future_stop={
            item: checked_at + MIN_FUTURE_HORIZON_SECONDS for item in ids
        },
        gate_passed_by_id={item: gate_passed for item in ids},
        checked_at_epoch=checked_at,
        source_sha256=source_sha256,
    )


def real_match(
    epg_id: str = "Good.Channel.us2",
    *,
    method: str = "strict",
    second_epg_id: str = "",
) -> dict[str, object]:
    return {
        "action": "AUTO_EPGSHARE",
        "source": "epgshare",
        "epg_id": epg_id,
        "epg_feed": "US2",
        "best_score": 100.0,
        "second_epg_id": second_epg_id,
        "second_epg_feed": "US2" if second_epg_id else "",
        "second_score": 100.0 if second_epg_id else "",
        "score_margin": 0.0 if second_epg_id else 100.0,
        "reason": "Synthetic exact result",
        "match_method": method,
    }


class FakeEngine:
    def __init__(self) -> None:
        self.profile_inputs: list[tuple[list[dict[str, object]], dict[str, str]]] = []

    def build_inventory_profiles_v8(self, channels, category_names):
        self.profile_inputs.append((channels, category_names))
        return (
            {str(item.get("category_id", "")): {"all_lineup": len(channels)} for item in channels},
            {str(item.get("stream_id", "")): {"role": "linear"} for item in channels},
        )


class FakeResolver:
    def __init__(
        self,
        matches: dict[str, object],
        *,
        route_explicit: bool = True,
        explicit_market: str = "US",
        route_plan: tuple[str, ...] = ("US",),
    ) -> None:
        self.engine = FakeEngine()
        self.matches = matches
        self.calls: list[dict[str, object]] = []
        self.query = SimpleNamespace(
            route_explicit=route_explicit,
            explicit_market=explicit_market,
            route_plan=route_plan,
        )

    def resolve(self, row, **kwargs):
        self.calls.append({"row": dict(row), **kwargs})
        result = self.matches[row["channel_name"]]
        if isinstance(result, Exception):
            raise result
        return self.query, result


class ScanProbeResolver(FakeResolver):
    SCAN_METHODS = (
        "_legacy_verified_match",
        "_contextual_containment_match",
        "_category_language_equivalence_match",
        "_regional_catalog_extension_match",
        "_near_exact_orthographic_match",
        "_contextual_fuzzy_match",
    )

    def __init__(self) -> None:
        super().__init__({})
        self.scan_calls = {name: 0 for name in self.SCAN_METHODS}

    def _record(self, name):
        self.scan_calls[name] += 1
        return None

    def _legacy_verified_match(self, *_args, **_kwargs):
        return self._record("_legacy_verified_match")

    def _contextual_containment_match(self, *_args, **_kwargs):
        return self._record("_contextual_containment_match")

    def _category_language_equivalence_match(self, *_args, **_kwargs):
        return self._record("_category_language_equivalence_match")

    def _regional_catalog_extension_match(self, *_args, **_kwargs):
        return self._record("_regional_catalog_extension_match")

    def _near_exact_orthographic_match(self, *_args, **_kwargs):
        return self._record("_near_exact_orthographic_match")

    def _contextual_fuzzy_match(self, *_args, **_kwargs):
        return self._record("_contextual_fuzzy_match")

    def resolve(self, row, **kwargs):
        self.calls.append({"row": dict(row), **kwargs})
        self._legacy_verified_match(row, self.query, pre_panel=True)
        self._contextual_containment_match(self.query)
        self._category_language_equivalence_match(self.query)
        self._regional_catalog_extension_match(self.query)
        self._near_exact_orthographic_match(self.query)
        self._legacy_verified_match(row, self.query, pre_panel=False)
        self._contextual_fuzzy_match(self.query)
        return self.query, {
            "action": "UNMATCHED",
            "source": "",
            "epg_id": "",
            "epg_feed": "",
            "reason": "no exact match",
            "match_method": "unmatched",
        }


class LiveCatalogPreflightResolver:
    """Small structural double whose historical self-test must never be called."""

    def __init__(self, *, corrupt_lookup: bool = False) -> None:
        self.corrupt_lookup = corrupt_lookup
        self.engine = SimpleNamespace(
            CANDIDATES=[],
            ID_LOOKUP={},
            DUMMY_IDS={},
            build_inventory_profiles_v8=lambda *_args, **_kwargs: ({}, {}),
            run_smart_rules_v8_self_test=self._historical_test_called,
        )
        self.state = SimpleNamespace(fingerprint="", by_region={})

    @staticmethod
    def _historical_test_called(*_args, **_kwargs):
        raise AssertionError("volatile live catalog was used for historical tests")

    def prepare(self, real_candidates, dummy_ids) -> None:
        prepared = [dict(item) for item in real_candidates]
        self.engine.CANDIDATES = prepared
        self.engine.ID_LOOKUP = {
            str(item["epg_id"]).casefold(): item for item in prepared
        }
        if self.corrupt_lookup:
            self.engine.ID_LOOKUP.clear()
        self.engine.DUMMY_IDS = {
            str(key).casefold(): str(value) for key, value in dummy_ids.items()
        }
        contexts = [SimpleNamespace(candidate=item) for item in prepared]
        self.state = SimpleNamespace(
            fingerprint="live-catalog-fingerprint",
            by_region={"ALL": contexts},
        )


def proposals_for(
    resolver: FakeResolver,
    *,
    server_id: str = "server_1",
    channels=None,
    existing_keys=(),
    target_keys=None,
    exact_catalog=None,
    matcher_identity=None,
    preflight=None,
):
    channels = channels or [
        {
            "stream_id": "10",
            "name": "New Channel",
            "category_id": "sports",
            "epg_channel_id": "native.provider.id",
        }
    ]
    return propose_new_channel_matches(
        resolver,
        server_id=server_id,
        channels=channels,
        category_names={"sports": "US | Sports"},
        existing_keys=existing_keys,
        target_keys=target_keys,
        catalog=exact_catalog or catalog("Good.Channel.us2"),
        matcher_identity=matcher_identity or identity(),
        preflight=preflight or MatcherPreflight(True, "ok"),
    )


class StrictAutoMatchV1Tests(unittest.TestCase):
    def test_runtime_preflight_checks_live_indexes_not_historical_positives(self) -> None:
        candidates = [
            {
                "epg_id": "Current Channel.us2",
                "feed": "US2",
                "region": "US",
                "display_name": "Current Channel",
                "normalized": "current channel",
            }
        ]
        dummies = {"movie.dummy.us": "Movie.Dummy.us"}

        ready = prepare_resolver_strict(
            LiveCatalogPreflightResolver(), candidates, dummies
        )
        corrupt = prepare_resolver_strict(
            LiveCatalogPreflightResolver(corrupt_lookup=True), candidates, dummies
        )

        self.assertTrue(ready.ready, ready.reason)
        self.assertIn("indexes verified", ready.reason)
        self.assertFalse(corrupt.ready)
        self.assertIn("lookup", corrupt.reason)

    def test_server1_exact_match_is_provisional_then_finalized(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})

        proposals = proposals_for(resolver)

        self.assertEqual(list(proposals), [("server_1", "10")])
        proposal = proposals[("server_1", "10")]
        self.assertTrue(proposal.eligible_for_finalization)
        self.assertEqual(resolver.calls[0]["row"]["panel_epg_id"], "")
        self.assertFalse(resolver.calls[0]["panel_is_usable"])
        final = finalize_proposal(proposal, evidence("Good.Channel.us2"))
        self.assertTrue(final.approved)
        patch = final.sheet_patch(existing_notes="discovery-note-" * 20)
        self.assertEqual(patch["action"], "AUTO_EPGSHARE")
        self.assertEqual(patch["source"], "epgshare01")
        self.assertEqual(patch["epg_feed"], "ALL_SOURCES1")
        self.assertEqual(patch["epg_id"], "Good.Channel.us2")
        self.assertEqual(patch["enabled"], "TRUE")
        self.assertIn("method=strict", patch["reason"])
        self.assertIn(f"source_sha256={CATALOG_SHA}", patch["notes"])
        self.assertFalse(any(key.startswith("metadata_") for key in patch))

    def test_only_unseen_rows_are_resolved_but_profiles_use_full_lineup(self) -> None:
        channels = [
            {"stream_id": "1", "name": "Existing", "category_id": "sports"},
            {"stream_id": "2", "name": "New", "category_id": "sports"},
        ]
        resolver = FakeResolver({"New": real_match()})

        proposals = proposals_for(
            resolver,
            channels=channels,
            existing_keys={("server_1", "1")},
        )

        self.assertEqual(set(proposals), {("server_1", "2")})
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["row"]["channel_name"], "New")
        self.assertEqual(len(resolver.engine.profile_inputs[0][0]), 2)

    def test_explicit_target_can_recheck_one_existing_identity_only(self) -> None:
        channels = [
            {"stream_id": "1", "name": "Existing", "category_id": "sports"},
            {"stream_id": "2", "name": "Unseen", "category_id": "sports"},
        ]
        resolver = FakeResolver({"Existing": real_match()})

        proposals = proposals_for(
            resolver,
            channels=channels,
            existing_keys={("server_1", "1")},
            target_keys={("server_1", "1")},
        )

        self.assertEqual(set(proposals), {("server_1", "1")})
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["row"]["channel_name"], "Existing")
        self.assertEqual(len(resolver.engine.profile_inputs[0][0]), 2)

    def test_explicit_target_rejects_an_identity_absent_from_inventory(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})

        with self.assertRaisesRegex(ValueError, "absent from inventory"):
            proposals_for(
                resolver,
                existing_keys={("server_1", "missing")},
                target_keys={("server_1", "missing")},
            )

    def test_review_only_catalog_scans_are_suppressed_and_restored(self) -> None:
        resolver = ScanProbeResolver()

        proposal = proposals_for(resolver)[("server_1", "10")]

        self.assertFalse(proposal.eligible_for_finalization)
        self.assertEqual(
            resolver.scan_calls,
            {name: 0 for name in ScanProbeResolver.SCAN_METHODS},
        )
        # The original instance/class methods are available again after resolve.
        resolver._contextual_fuzzy_match(resolver.query)
        self.assertEqual(resolver.scan_calls["_contextual_fuzzy_match"], 1)

    def test_fuzzy_and_broader_inference_methods_never_auto_enable(self) -> None:
        forbidden = (
            "canonical_identity",
            "contextual_fuzzy",
            "regional_context_containment",
            "near_exact_orthography",
            "category_language_equivalence",
            "category_language_default",
            "regional_catalog_extension",
            "edition_aware",
            "descriptor_relaxed",
            "spacing_compact",
            "token_multiset",
            "verified_legacy_rule",
            "verified_legacy_exact",
        )
        for method in forbidden:
            with self.subTest(method=method):
                raw = real_match(method=method)
                if method == "contextual_fuzzy":
                    raw.update(action="REVIEW", source="epgshare_candidate")
                resolver = FakeResolver({"New Channel": raw})
                proposal = proposals_for(resolver)[("server_1", "10")]
                self.assertFalse(proposal.eligible_for_finalization)
                patch = finalize_proposal(
                    proposal, evidence("Good.Channel.us2")
                ).sheet_patch()
                self.assertEqual(patch["action"], "REVIEW")
                self.assertEqual(patch["enabled"], "FALSE")
                self.assertEqual(patch["source"], "epgshare01")

    def test_any_second_id_blocks_automation(self) -> None:
        resolver = FakeResolver(
            {"New Channel": real_match(second_epg_id="Other.Channel.us2")}
        )
        proposal = proposals_for(
            resolver,
            exact_catalog=catalog("Good.Channel.us2", "Other.Channel.us2"),
        )[("server_1", "10")]

        self.assertFalse(proposal.eligible_for_finalization)
        self.assertIn("second EPG ID", proposal.decision_reason)

    def test_catalog_case_ambiguity_blocks_provisional_approval(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(
            resolver,
            exact_catalog=catalog("Good.Channel.us2", "good.channel.us2"),
        )[("server_1", "10")]

        self.assertFalse(proposal.eligible_for_finalization)
        self.assertIn("case ambiguity", proposal.decision_reason)

    def test_finalization_requires_exact_declaration_and_future_information(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(resolver)[("server_1", "10")]

        absent = finalize_proposal(proposal, evidence("Other.Channel.us2"))
        self.assertFalse(absent.approved)
        self.assertIn("not declared exactly", absent.reason)

        no_future = finalize_proposal(
            proposal,
            ScheduleEvidence(
                declared_ids=frozenset({"Good.Channel.us2"}),
                informative_future_programmes={"Good.Channel.us2": 0},
                latest_informative_future_stop={"Good.Channel.us2": 30_000},
                gate_passed_by_id={"Good.Channel.us2": True},
                checked_at_epoch=1_000,
                source_sha256=CATALOG_SHA,
            ),
        )
        self.assertFalse(no_future.approved)
        self.assertIn("fewer than", no_future.reason)

    def test_xmltv_case_ambiguity_blocks_finalization(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(resolver)[("server_1", "10")]
        ambiguous = ScheduleEvidence(
            declared_ids=frozenset(
                {"Good.Channel.us2", "good.channel.us2"}
            ),
            informative_future_programmes={"Good.Channel.us2": 5},
            latest_informative_future_stop={"Good.Channel.us2": 30_000},
            gate_passed_by_id={"Good.Channel.us2": True},
            checked_at_epoch=1_000,
            source_sha256=CATALOG_SHA,
        )

        final = finalize_proposal(proposal, ambiguous)

        self.assertFalse(final.approved)
        self.assertIn("ambiguous case variants", final.reason)

    def test_dummy_rule_is_review_only_and_does_not_touch_metadata(self) -> None:
        resolver = FakeResolver(
            {
                "New Channel": {
                    "action": "AUTO_DUMMY",
                    "source": "dummy",
                    "epg_id": "PPV.EVENTS.Dummy.us",
                    "epg_feed": "DUMMY_CHANNELS",
                    "reason": "Event slot",
                    "match_method": "safety_rule",
                }
            }
        )
        proposal = proposals_for(
            resolver,
            exact_catalog=catalog(
                "PPV.EVENTS.Dummy.us", region="DUMMY", kind="dummy"
            ),
        )[("server_1", "10")]
        patch = finalize_proposal(
            proposal, evidence("PPV.EVENTS.Dummy.us")
        ).sheet_patch()

        self.assertEqual(patch["action"], "REVIEW")
        self.assertEqual(patch["source"], "dummy")
        self.assertEqual(patch["epg_feed"], "DUMMY_CHANNELS")
        self.assertEqual(patch["enabled"], "FALSE")
        self.assertIn("review-only", patch["reason"])
        self.assertFalse(any(key.startswith("metadata_") for key in patch))

    def test_catalog_and_programme_evidence_require_same_source_sha(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(resolver)[("server_1", "10")]

        final = finalize_proposal(
            proposal,
            evidence("Good.Channel.us2", source_sha256=SOURCE_SHA),
        )

        self.assertFalse(final.approved)
        self.assertIn("different", final.reason)

    def test_adult_category_or_name_evidence_never_auto(self) -> None:
        cases = (
            ("XXX | Adults", "Private HD"),
            ("Movies", "FOR ADULTS 1"),
            ("Movies", "Porno Movies"),
            ("Movies", "Pornhub TV"),
            ("Movies", "OnlyFans Live"),
            ("Movies", "18 + Movies"),
            ("Movies", "Erotica"),
            ("Movies", "Eroticism TV"),
            ("Movies", "Red Light HD"),
            ("IN | ＡＤＵＬＴ", "Private HD"),
            ("Movies", "ＰＯＲＮ Movies"),
            ("Movies", "P\u200born Movies"),
            ("Movies", "Pörn Movies"),
            ("Movies", "Pоrn Movies"),
            ("Movies", "PОrn Movies"),
            ("Movies", "Pοrn Movies"),
            ("Movies", "PΟrn Movies"),
            ("Movies", "Аdult Movies"),
            ("Movies", "аdult Movies"),
            ("Movies", "Aԁult Movies"),
            ("Movies", "AԀult Movies"),
            ("Movies", "ΧΧΧ Movies"),
            ("Movies", "χχχ Movies"),
            ("Movies", "рorn Movies"),
            ("Movies", "РORN Movies"),
            ("Movies", "aduӏt Movies"),
            ("Movies", "ADUӀT Movies"),
            ("Movies", "ххх Movies"),
            ("Movies", "ХХХ Movies"),
            ("Movies", "××× Movies"),
            ("Movies", "еrotic Movies"),
            ("Movies", "ЕROTIC Movies"),
            ("Movies", "erotіc Movies"),
            ("Movies", "EROTІC Movies"),
            ("Movies", "erotiс Movies"),
            ("Movies", "EROTIС Movies"),
            ("Movies", "аdult swim рorn"),
            ("Movies", "αdυӏτ Movies"),
            ("Movies", "ΑDΥӀΤ Movies"),
            ("Movies", "ρorn Movies"),
            ("Movies", "ΡORN Movies"),
            ("Movies", "εroτιϲ Movies"),
            ("Movies", "ΕROΤΙϹ Movies"),
            ("Movies", "ѕeχτreme Movies"),
            ("Movies", "ЅEΧΤREME Movies"),
            ("Movies", "plaуboy Movies"),
            ("Movies", "PLAУBOY Movies"),
            ("Movies", "Ροrn Movies"),
            ("Movies", "αdult Movies"),
            ("Movies", "adυlt Movies"),
            ("Movies", "adulτ Movies"),
            ("Movies", "εrotic Movies"),
            ("Movies", "erotιc Movies"),
            ("Movies", "erotiϲ Movies"),
            ("Movies", "ѕextreme Movies"),
        )
        for category_name, channel_name in cases:
            with self.subTest(category_name=category_name, channel_name=channel_name):
                resolver = FakeResolver({channel_name: real_match()})
                proposal = propose_new_channel_matches(
                    resolver,
                    server_id="server_1",
                    channels=[
                        {
                            "stream_id": "10",
                            "name": channel_name,
                            "category_id": "adult",
                        }
                    ],
                    category_names={"adult": category_name},
                    existing_keys=set(),
                    catalog=catalog("Good.Channel.us2"),
                    matcher_identity=identity(),
                    preflight=MatcherPreflight(True, "ok"),
                )[("server_1", "10")]

                self.assertFalse(proposal.eligible_for_finalization)
                self.assertIn("Adult", proposal.decision_reason)

    def test_adult_swim_brand_is_not_false_adult_evidence(self) -> None:
        for channel_name in ("Adult Swim", "Аdult Swim", "αdυӏτ Swim"):
            with self.subTest(channel_name=channel_name):
                resolver = FakeResolver({channel_name: real_match()})
                proposal = proposals_for(
                    resolver,
                    channels=[
                        {
                            "stream_id": "10",
                            "name": channel_name,
                            "category_id": "sports",
                        }
                    ],
                )[("server_1", "10")]

                self.assertTrue(proposal.eligible_for_finalization)

    def test_blank_and_generic_numbered_identity_never_auto(self) -> None:
        for channel_name in ("", "123", "Channel 12 HD", "US | Stream 4"):
            with self.subTest(channel_name=channel_name):
                resolver = FakeResolver({channel_name: real_match()})
                proposal = proposals_for(
                    resolver,
                    channels=[
                        {
                            "stream_id": "10",
                            "name": channel_name,
                            "category_id": "sports",
                        }
                    ],
                )[("server_1", "10")]

                self.assertFalse(proposal.eligible_for_finalization)
                patch = finalize_proposal(
                    proposal, evidence("Good.Channel.us2")
                ).sheet_patch()
                self.assertEqual(patch["enabled"], "FALSE")

    def test_unknown_or_ambiguous_market_never_auto(self) -> None:
        cases = (
            {
                "route_explicit": False,
                "explicit_market": "ALL",
                "route_plan": ("ALL",),
            },
            {
                "route_explicit": True,
                "explicit_market": "NA_DIASPORA",
                "route_plan": ("US", "CA"),
            },
            {
                "route_explicit": True,
                "explicit_market": "US",
                "route_plan": ("US", "CA"),
            },
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                resolver = FakeResolver({"New Channel": real_match()}, **kwargs)
                proposal = proposals_for(resolver)[("server_1", "10")]
                self.assertFalse(proposal.eligible_for_finalization)
                self.assertIn("Market", proposal.decision_reason)

    def test_target_catalog_kind_and_market_are_bound_to_query(self) -> None:
        cases = (
            (
                real_match("Wrong.Channel.in"),
                catalog("Wrong.Channel.in", region="IN", kind="real"),
                "region",
            ),
            (
                real_match("PPV.EVENTS.Dummy.us"),
                catalog(
                    "PPV.EVENTS.Dummy.us", region="DUMMY", kind="dummy"
                ),
                "dummy",
            ),
        )
        for raw_match, exact_catalog, expected in cases:
            with self.subTest(expected=expected):
                resolver = FakeResolver({"New Channel": raw_match})
                proposal = proposals_for(
                    resolver, exact_catalog=exact_catalog
                )[("server_1", "10")]
                self.assertFalse(proposal.eligible_for_finalization)
                self.assertIn(expected, proposal.decision_reason.casefold())

    def test_uk_and_gb_are_the_only_supported_market_alias(self) -> None:
        resolver = FakeResolver(
            {"New Channel": real_match()},
            explicit_market="UK",
            route_plan=("UK",),
        )

        proposal = proposals_for(
            resolver,
            exact_catalog=catalog("Good.Channel.us2", region="GB"),
        )[("server_1", "10")]

        self.assertTrue(proposal.eligible_for_finalization)

    def test_future_horizon_must_extend_beyond_evidence_time(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(resolver)[("server_1", "10")]
        stale = ScheduleEvidence(
            declared_ids=frozenset({"Good.Channel.us2"}),
            informative_future_programmes={"Good.Channel.us2": 2},
            latest_informative_future_stop={"Good.Channel.us2": 1_000},
            gate_passed_by_id={"Good.Channel.us2": True},
            checked_at_epoch=1_000,
            source_sha256=CATALOG_SHA,
        )

        final = finalize_proposal(proposal, stale)

        self.assertFalse(final.approved)
        self.assertIn("future horizon", final.reason)

    def test_programme_gate_must_explicitly_pass(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})
        proposal = proposals_for(resolver)[("server_1", "10")]

        final = finalize_proposal(
            proposal,
            evidence("Good.Channel.us2", gate_passed=False),
        )

        self.assertFalse(final.approved)
        self.assertIn("programme safety gate", final.reason)

    def test_server2_panel_id_is_only_a_clue_and_panel_result_is_rejected(self) -> None:
        resolver = FakeResolver(
            {
                "New Channel": {
                    "action": "KEEP_PANEL",
                    "source": "panel",
                    "epg_id": "native.provider.id",
                    "epg_feed": "server xmltv.php",
                    "reason": "panel",
                    "match_method": "panel",
                }
            }
        )
        proposal = proposals_for(
            resolver,
            server_id="server_2",
            exact_catalog=catalog("native.provider.id"),
        )[("server_2", "10")]

        self.assertEqual(
            resolver.calls[0]["row"]["panel_epg_id"], "native.provider.id"
        )
        self.assertFalse(resolver.calls[0]["panel_is_usable"])
        self.assertFalse(proposal.eligible_for_finalization)
        self.assertEqual(
            finalize_proposal(
                proposal, evidence("native.provider.id")
            ).sheet_patch()["enabled"],
            "FALSE",
        )

    def test_unhealthy_catalog_or_matcher_preflight_skips_resolver(self) -> None:
        cases = (
            {
                "exact_catalog": CatalogSnapshot.from_ids(
                    ["Good.Channel.us2"],
                    source_sha256=CATALOG_SHA,
                    regions_by_id={"Good.Channel.us2": {"US"}},
                    kinds_by_id={"Good.Channel.us2": {"real"}},
                    healthy=False,
                    health_reason="required feed failed",
                )
            },
            {"preflight": MatcherPreflight(False, "self-test failed")},
            {
                "matcher_identity": MatcherIdentity(
                    version="8.5",
                    build_id=STRICT_MATCHER_BUILD_ID,
                    source_sha256=GOOD_SHA,
                    engine_source_sha256=STRICT_ENGINE_SOURCE_SHA256,
                )
            },
            {
                "matcher_identity": MatcherIdentity(
                    version=STRICT_MATCHER_VERSION,
                    build_id=STRICT_MATCHER_BUILD_ID,
                    source_sha256="a" * 64,
                    engine_source_sha256=STRICT_ENGINE_SOURCE_SHA256,
                )
            },
            {
                "matcher_identity": MatcherIdentity(
                    version=STRICT_MATCHER_VERSION,
                    build_id=STRICT_MATCHER_BUILD_ID,
                    source_sha256=GOOD_SHA,
                    engine_source_sha256="d" * 64,
                )
            },
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                resolver = FakeResolver({"New Channel": real_match()})
                proposal = proposals_for(resolver, **kwargs)[("server_1", "10")]
                self.assertFalse(proposal.eligible_for_finalization)
                self.assertEqual(resolver.calls, [])
                self.assertEqual(
                    finalize_proposal(
                        proposal, evidence("Good.Channel.us2")
                    ).sheet_patch()["enabled"],
                    "FALSE",
                )

    def test_matcher_identity_pins_both_frozen_source_files(self) -> None:
        resolver = SimpleNamespace(
            engine=SimpleNamespace(
                SMART_RULES_VERSION=STRICT_MATCHER_VERSION,
                MATCHER_BUILD_ID=STRICT_MATCHER_BUILD_ID,
            )
        )
        contextual_path = SRC_DIR / "skytv_epg_contextual_v8.py"
        engine_path = SRC_DIR / "skytv_epg_engine.py"

        exact = MatcherIdentity.from_resolver(
            resolver, contextual_path, engine_path
        )
        self.assertTrue(exact.is_expected)

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            altered_contextual = temporary_path / "contextual.py"
            altered_contextual.write_bytes(
                contextual_path.read_bytes() + b"\n# tampered\n"
            )
            altered_engine = temporary_path / "engine.py"
            altered_engine.write_bytes(engine_path.read_bytes() + b"\n# tampered\n")

            self.assertFalse(
                MatcherIdentity.from_resolver(
                    resolver, altered_contextual, engine_path
                ).is_expected
            )
            self.assertFalse(
                MatcherIdentity.from_resolver(
                    resolver, contextual_path, altered_engine
                ).is_expected
            )

    def test_matcher_exception_is_per_row_fail_closed(self) -> None:
        resolver = FakeResolver({"New Channel": RuntimeError("bad result")})

        proposal = proposals_for(resolver)[("server_1", "10")]

        self.assertFalse(proposal.eligible_for_finalization)
        self.assertIn("failed closed", proposal.decision_reason)
        patch = finalize_proposal(
            proposal, evidence("Good.Channel.us2")
        ).sheet_patch()
        self.assertEqual(patch["action"], "REVIEW")
        self.assertEqual(patch["enabled"], "FALSE")

    def test_duplicate_inventory_identity_is_rejected(self) -> None:
        resolver = FakeResolver({})
        channels = [
            {"stream_id": "10", "name": "One", "category_id": "sports"},
            {"stream_id": "10", "name": "Two", "category_id": "sports"},
        ]

        with self.assertRaisesRegex(ValueError, "duplicate inventory identity"):
            proposals_for(resolver, channels=channels)

    def test_server_id_must_use_canonical_form(self) -> None:
        resolver = FakeResolver({"New Channel": real_match()})

        for server_id in (
            "1",
            "Server 1",
            "Server_1",
            "SERVER_1",
            "server1",
            "server_0",
            "server_01",
        ):
            with self.subTest(server_id=server_id):
                with self.assertRaisesRegex(ValueError, "canonical server_N"):
                    proposals_for(resolver, server_id=server_id)


if __name__ == "__main__":
    unittest.main()
