"""Indexed, explainable candidate retrieval for unresolved channel names."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from rapidfuzz import fuzz

from .compat import (
    catalog_stream,
    parse_candidate_context_v8,
    parse_channel_context_v8,
    streaming,
)
from .models import (
    CandidateEvidence,
    ContractError,
    ProgrammeState,
    ProtectedSemantics,
    ensure_finite_scores,
    sha256_json,
)
from .normalization import (
    NameViews,
    jaccard,
    protected_conflicts,
    semantics_from_context,
)
from .policy import DEFAULT_POLICY, MatchingPolicy


@dataclass(frozen=True, slots=True)
class AliasEdge:
    alias: str
    market: str
    epg_id: str
    provenance: str


@dataclass(frozen=True, slots=True)
class LabSubject:
    server_id: str
    stream_id: str
    channel_name: str
    category_id: str
    category_name: str
    row_guard_sha256: str
    provider_identity_sha256: str
    route_explicit: bool
    market: str
    route_plan: tuple[str, ...]
    views: NameViews
    semantics: ProtectedSemantics
    context: Any

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True, slots=True)
class CatalogItem:
    epg_id: str
    display_name: str
    xml_display_name: str
    feed: str
    region: str
    views: NameViews
    xml_views: NameViews
    semantics: ProtectedSemantics
    context: Any


@dataclass(frozen=True, slots=True)
class RankedCandidates:
    candidates: tuple[CandidateEvidence, ...]
    compatible_count: int
    score_ppm: int
    margin_ppm: int
    tier: int
    work_candidates: int


MATCHER_INPUT_FIELDS = (
    "server_id", "stream_id", "channel_name", "canonical_name",
    "category_id", "category_name", "channel_number", "country_codes",
    "language_codes", "genre", "subgenres", "sport_codes", "religion_codes",
    "tags", "enabled", "action", "source", "epg_feed", "epg_id", "reason", "notes",
)


def mapping_row_guard(row: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "skytv-matching-row-guard-v1",
            "values": [[field, str(row.get(field, ""))] for field in MATCHER_INPUT_FIELDS],
        }
    )


def provider_identity_guard(row: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "skytv-provider-identity-v1",
            "server_id": str(row.get("server_id", "")),
            "stream_id": str(row.get("stream_id", "")),
            "channel_name": str(row.get("channel_name", "")),
            "category_id": str(row.get("category_id", "")),
            "category_name": str(row.get("category_name", "")),
        }
    )


def subject_from_mapping_row(engine: Any, row: Mapping[str, Any]) -> LabSubject:
    server_id = streaming.normalize_server_id(row.get("server_id", ""))
    stream_id = streaming.clean_identifier(row.get("stream_id", ""), 120)
    channel_name = streaming.clean_identifier(row.get("channel_name", ""), 300)
    category_id = streaming.clean_identifier(row.get("category_id", ""), 120)
    category_name = streaming.clean_text(row.get("category_name", ""), 200)
    if not stream_id or not channel_name:
        raise ContractError("A Matching Lab subject has a blank identity.")
    context = parse_channel_context_v8(engine, channel_name, category_name)
    market = str(context.explicit_market or "").upper()
    return LabSubject(
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_id=category_id,
        category_name=category_name,
        row_guard_sha256=mapping_row_guard(row),
        provider_identity_sha256=provider_identity_guard(row),
        route_explicit=bool(context.route_explicit),
        market=market,
        route_plan=tuple(str(value).upper() for value in context.route_plan),
        views=NameViews.from_context(context),
        semantics=semantics_from_context(
            context,
            market=market if len(tuple(context.route_plan)) == 1 else "",
        ),
        context=context,
    )


def _safe_ratio(left: str, right: str) -> int:
    if not left or not right:
        return 0
    score = float(fuzz.WRatio(left, right))
    ensure_finite_scores((score,))
    return max(0, min(1_000_000, int(round(score * 10_000))))


def _fraction_ppm(value: float) -> int:
    ensure_finite_scores((value,))
    return max(0, min(1_000_000, int(round(value * 1_000_000))))


def _candidate_tier(methods: set[str]) -> int:
    if "HUMAN_ALIAS_EXACT" in methods or "CURATED_ALIAS_EXACT" in methods:
        return 5
    if "STRICT_EXACT" in methods:
        return 4
    if methods.intersection({"RELAXED_EXACT", "COMPACT_EXACT", "BAG_EXACT"}):
        return 3
    fuzzy = methods.intersection({
        "TOKEN_RETRIEVAL", "CHAR_NGRAM_RETRIEVAL", "TRANSLITERATION_RETRIEVAL",
        "ACRONYM_RETRIEVAL", "XML_DISPLAY_RETRIEVAL",
    })
    return 2 if len(fuzzy) >= 2 else 1


class CandidateIndex:
    """One deterministic, catalog-hash-bound multi-retriever index."""

    def __init__(
        self,
        *,
        engine: Any,
        candidates: Sequence[Mapping[str, str]],
        xml_display_names: Mapping[str, str] | None = None,
        aliases: Sequence[AliasEdge] = (),
        policy: MatchingPolicy = DEFAULT_POLICY,
    ) -> None:
        self.engine = engine
        self.policy = policy
        xml_names = dict(xml_display_names or {})
        self.items: list[CatalogItem] = []
        self.by_epg_id: dict[str, int] = {}
        for raw in sorted(
            (dict(value) for value in candidates),
            key=lambda value: (str(value.get("epg_id", "")).casefold(), str(value.get("epg_id", ""))),
        ):
            epg_id = streaming.clean_identifier(raw.get("epg_id", ""), 300)
            display_name = streaming.clean_text(raw.get("display_name", ""), 300)
            feed = streaming.clean_text(raw.get("feed", ""), 80).upper()
            region = streaming.clean_text(raw.get("region", ""), 40).upper()
            if not epg_id or not display_name or not feed or not region:
                raise ContractError("A Matching Lab catalog candidate is incomplete.")
            if epg_id in self.by_epg_id:
                raise ContractError("The Matching Lab catalog contains a duplicate EPG ID.")
            context = parse_candidate_context_v8(engine, raw)
            xml_display = streaming.clean_text(xml_names.get(epg_id, ""), 300)
            item = CatalogItem(
                epg_id=epg_id,
                display_name=display_name,
                xml_display_name=xml_display,
                feed=feed,
                region=region,
                views=NameViews.from_context(context),
                xml_views=NameViews.from_text(xml_display),
                semantics=semantics_from_context(context, market=region),
                context=context,
            )
            self.by_epg_id[epg_id] = len(self.items)
            self.items.append(item)

        self._view_indexes: dict[str, dict[str, set[int]]] = {
            name: defaultdict(set)
            for name in ("strict", "relaxed", "compact", "bag", "acronym", "transliterated")
        }
        self._xml_view_indexes: dict[str, dict[str, set[int]]] = {
            name: defaultdict(set)
            for name in ("strict", "relaxed", "compact", "bag", "acronym", "transliterated")
        }
        self._token_postings: dict[str, set[int]] = defaultdict(set)
        self._ngram_postings: dict[str, set[int]] = defaultdict(set)
        self._xml_token_postings: dict[str, set[int]] = defaultdict(set)
        self._xml_ngram_postings: dict[str, set[int]] = defaultdict(set)
        self._region_items: dict[str, set[int]] = defaultdict(set)
        for index, item in enumerate(self.items):
            self._region_items[item.region].add(index)
            for name in self._view_indexes:
                value = str(getattr(item.views, name) or "")
                if value:
                    self._view_indexes[name][value].add(index)
                xml_value = str(getattr(item.xml_views, name) or "")
                if xml_value:
                    self._xml_view_indexes[name][xml_value].add(index)
            for token in set(item.views.tokens):
                self._token_postings[token].add(index)
            for trigram in item.views.trigrams:
                self._ngram_postings[trigram].add(index)
            for token in set(item.xml_views.tokens):
                self._xml_token_postings[token].add(index)
            for trigram in item.xml_views.trigrams:
                self._xml_ngram_postings[trigram].add(index)

        alias_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
        alias_provenance: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for edge in aliases:
            view = NameViews.from_text(edge.alias)
            key = (view.strict, str(edge.market or "").upper())
            if edge.epg_id in self.by_epg_id:
                alias_targets[key].add(edge.epg_id)
                alias_provenance[(key[0], key[1], edge.epg_id)].add(edge.provenance)
        self._aliases: dict[tuple[str, str], tuple[int, ...]] = {}
        self._alias_methods: dict[tuple[str, str, int], str] = {}
        for key, epg_ids in alias_targets.items():
            # A conflicting alias is negative evidence; never pick one target silently.
            if len(epg_ids) != 1:
                continue
            epg_id = next(iter(epg_ids))
            index = self.by_epg_id[epg_id]
            self._aliases[key] = (index,)
            provenance = alias_provenance[(key[0], key[1], epg_id)]
            self._alias_methods[(key[0], key[1], index)] = (
                "CURATED_ALIAS_EXACT" if "curated" in provenance else "HUMAN_ALIAS_EXACT"
            )

    def _allowed_regions(self, subject: LabSubject) -> set[str]:
        if subject.route_explicit and subject.route_plan:
            return set(subject.route_plan)
        return set(self._region_items)

    @staticmethod
    def _top_counts(values: Counter[int], limit: int) -> tuple[int, ...]:
        return tuple(
            index for index, _count in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:limit]
        )

    def _pool(self, subject: LabSubject) -> tuple[set[int], dict[int, set[str]]]:
        allowed_regions = self._allowed_regions(subject)
        allowed = set().union(*(self._region_items.get(region, set()) for region in allowed_regions))
        hits: dict[int, set[str]] = defaultdict(set)

        alias_key = (subject.views.strict, subject.market)
        for index in self._aliases.get(alias_key, ()):
            if index in allowed:
                hits[index].add(self._alias_methods[(alias_key[0], alias_key[1], index)])

        for name, method in (
            ("strict", "STRICT_EXACT"), ("relaxed", "RELAXED_EXACT"),
            ("compact", "COMPACT_EXACT"), ("bag", "BAG_EXACT"),
            ("acronym", "ACRONYM_RETRIEVAL"),
            ("transliterated", "TRANSLITERATION_RETRIEVAL"),
        ):
            value = str(getattr(subject.views, name) or "")
            if not value:
                continue
            for index in self._view_indexes[name].get(value, set()).intersection(allowed):
                hits[index].add(method)
            # Current XML display names are useful discovery evidence, but are
            # deliberately kept in one review-only provenance class.  They do
            # not receive the structural exact tier derived from stable IDs.
            for index in self._xml_view_indexes[name].get(value, set()).intersection(allowed):
                hits[index].add("XML_DISPLAY_RETRIEVAL")

        token_counts: Counter[int] = Counter()
        xml_name_hits: set[int] = set()
        for token in set(subject.views.tokens):
            xml_posting = self._xml_token_postings.get(token, set())
            xml_name_hits.update(xml_posting.intersection(allowed))
            posting = self._token_postings.get(token, set()).union(xml_posting)
            if len(posting) <= 5_000:
                token_counts.update(posting.intersection(allowed))
        for index in self._top_counts(token_counts, 24):
            hits[index].add("TOKEN_RETRIEVAL")
            if index in xml_name_hits:
                hits[index].add("XML_DISPLAY_RETRIEVAL")

        ngram_counts: Counter[int] = Counter()
        for trigram in subject.views.trigrams:
            xml_posting = self._xml_ngram_postings.get(trigram, set())
            xml_name_hits.update(xml_posting.intersection(allowed))
            posting = self._ngram_postings.get(trigram, set()).union(xml_posting)
            if len(posting) <= 5_000:
                ngram_counts.update(posting.intersection(allowed))
        for index in self._top_counts(ngram_counts, 24):
            hits[index].add("CHAR_NGRAM_RETRIEVAL")
            if index in xml_name_hits:
                hits[index].add("XML_DISPLAY_RETRIEVAL")

        pool = set(hits)
        if len(pool) > self.policy.maximum_retrieval_candidates:
            ordered = sorted(
                pool,
                key=lambda index: (
                    -_candidate_tier(hits[index]),
                    -token_counts[index],
                    -ngram_counts[index],
                    self.items[index].epg_id.casefold(),
                    self.items[index].epg_id,
                ),
            )
            pool = set(ordered[: self.policy.maximum_retrieval_candidates])
            hits = {index: hits[index] for index in pool}
        return pool, hits

    def rank(self, subject: LabSubject) -> RankedCandidates:
        pool, hit_methods = self._pool(subject)
        ranked: list[tuple[int, int, str, str, CandidateEvidence]] = []
        for index in sorted(pool):
            item = self.items[index]
            methods = set(hit_methods[index])
            if (
                subject.views.strict
                and item.xml_views.strict
                and subject.views.strict == item.xml_views.strict
            ):
                methods.add("XML_DISPLAY_RETRIEVAL")
            conflicts = protected_conflicts(
                subject.semantics,
                item.semantics,
                route_explicit=subject.route_explicit,
            )
            token_score = max(
                _fraction_ppm(jaccard(subject.views.tokens, item.views.tokens)),
                _fraction_ppm(jaccard(subject.views.tokens, item.xml_views.tokens)),
            )
            ngram_score = max(
                _fraction_ppm(jaccard(subject.views.trigrams, item.views.trigrams)),
                _fraction_ppm(jaccard(subject.views.trigrams, item.xml_views.trigrams)),
            )
            transliteration_score = max(
                _safe_ratio(subject.views.transliterated, item.views.transliterated),
                _safe_ratio(subject.views.transliterated, item.xml_views.transliterated),
            )
            acronym_score = (
                1_000_000
                if subject.views.acronym
                and subject.views.acronym
                in {item.views.acronym, item.xml_views.acronym}
                else 0
            )
            try:
                contextual = float(self.engine_match_score(subject.context, item.context))
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(
                    "The frozen contextual scorer failed during Matching Lab ranking."
                ) from exc
            ensure_finite_scores((contextual,))
            contextual_score = max(0, min(1_000_000, int(round(contextual * 10_000))))
            score = (
                contextual_score * self.policy.contextual_weight_ppm
                + token_score * self.policy.token_weight_ppm
                + ngram_score * self.policy.ngram_weight_ppm
                + transliteration_score * self.policy.transliteration_weight_ppm
                + acronym_score * self.policy.acronym_weight_ppm
            ) // 1_000_000
            tier = _candidate_tier(methods)
            evidence = CandidateEvidence(
                candidate_key="pending",
                epg_id=item.epg_id,
                display_name=item.xml_display_name or item.display_name,
                feed=item.feed,
                region=item.region,
                score_ppm=score,
                methods=tuple(methods),
                conflicts=conflicts,
                features_ppm=(
                    ("ACRONYM_SCORE", acronym_score),
                    ("CONTEXTUAL_SCORE", contextual_score),
                    ("NGRAM_SCORE", ngram_score),
                    ("TOKEN_SCORE", token_score),
                    ("TRANSLITERATION_SCORE", transliteration_score),
                ),
                semantics=item.semantics,
                programme_state=ProgrammeState.NOT_CHECKED,
            )
            ranked.append((0 if not conflicts else 1, -tier, item.epg_id.casefold(), item.epg_id, evidence))

        ranked.sort(
            key=lambda value: (
                value[0],
                value[1],
                -value[4].score_ppm,
                value[2],
                value[3],
            )
        )
        retained = tuple(
            replace(value[4], candidate_key=f"c{position:03d}")
            for position, value in enumerate(ranked[: self.policy.retained_candidates], start=1)
        )
        compatible = tuple(candidate for candidate in retained if not candidate.conflicts)
        top_score = compatible[0].score_ppm if compatible else 0
        second_score = compatible[1].score_ppm if len(compatible) > 1 else 0
        top_tier = _candidate_tier(set(compatible[0].methods)) if compatible else 0
        return RankedCandidates(
            candidates=retained,
            compatible_count=sum(1 for value in ranked if not value[4].conflicts),
            score_ppm=top_score,
            margin_ppm=max(0, top_score - second_score),
            tier=top_tier,
            work_candidates=len(pool),
        )

    def engine_match_score(self, query_context: Any, candidate_context: Any) -> float:
        """Use the frozen contextual scorer as one feature, never as a safety gate."""

        resolver = getattr(self, "resolver", None)
        if resolver is not None and callable(getattr(resolver, "_fuzzy_score", None)):
            return float(resolver._fuzzy_score(query_context, candidate_context))
        # The contextual engine's public context objects still provide stable
        # strict keys if a custom runtime does not expose its scorer.
        return float(fuzz.WRatio(query_context.strict_key, candidate_context.strict_key))

    def attach_resolver(self, resolver: Any) -> None:
        self.resolver = resolver


def human_alias_edges(
    rows: Sequence[Mapping[str, Any]],
    *,
    excluded_keys: Iterable[tuple[str, str]] = (),
) -> tuple[AliasEdge, ...]:
    excluded = frozenset(excluded_keys)
    result: list[AliasEdge] = []
    for row in rows:
        action = streaming.clean_text(row.get("action", ""), 40).upper()
        if action not in {"MANUAL", "APPROVED"}:
            continue
        try:
            enabled = streaming.parse_bool(
                row.get("enabled", ""), default=False, field_name="enabled"
            )
        except Exception:
            continue
        source = streaming.clean_text(row.get("source", ""), 40).casefold()
        epg_id = streaming.clean_identifier(row.get("epg_id", ""), 300)
        alias = streaming.clean_identifier(row.get("channel_name", ""), 300)
        try:
            identity = (
                streaming.normalize_server_id(row.get("server_id", "")),
                streaming.clean_identifier(row.get("stream_id", ""), 120),
            )
        except streaming.BuildError:
            continue
        if not enabled or source not in {"epgshare", "epgshare01"} or not epg_id or not alias:
            continue
        if identity in excluded:
            continue
        # The candidate index will discard aliases whose current target is absent.
        market = catalog_stream.infer_catalog_route(epg_id).region
        result.append(
            AliasEdge(alias=alias, market=market, epg_id=epg_id, provenance="human")
        )
    return tuple(
        sorted(result, key=lambda edge: (edge.alias.casefold(), edge.market, edge.epg_id))
    )


__all__ = (
    "AliasEdge",
    "CandidateIndex",
    "CatalogItem",
    "LabSubject",
    "RankedCandidates",
    "human_alias_edges",
    "mapping_row_guard",
    "provider_identity_guard",
    "subject_from_mapping_row",
)
