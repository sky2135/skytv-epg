"""Fail-closed adapter for explicitly authorized automatic channel matching.

The frozen Smart Rules v8.4 matcher remains the source of candidate proposals.
This module deliberately adds a narrower production boundary around it:

* the default boundary considers only previously unseen identities;
* callers may explicitly allowlist existing disabled ``REVIEW`` identities;
* Server 1's native EPG ID is never supplied to the matcher;
* only an explicit set of deterministic structural matcher methods may become
  automatic; fuzzy, containment, near-exact, and legacy methods remain REVIEW;
* a candidate is not enabled until the same combined XMLTV source snapshot
  declares the exact case-sensitive ID and its programme gate proves at least
  two informative entries spanning a six-hour future horizon;
* any second ID blocks automation; and
* verified adult/24x7/event/numbered-bank classifications may use an exact
  dummy guide without pretending that the dummy has real programme evidence;
* decorative headings remain disabled ``IGNORE`` rows;
* personalization metadata is never approved or changed here.

The adapter is intentionally free of network and Google Sheets code.  Callers
must obtain healthy, bounded catalog data and XMLTV evidence, then pass those
snapshots into the pure decision functions below.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


STRICT_MATCHER_VERSION = "8.4"
STRICT_MATCHER_BUILD_ID = "SKYTV-CONTEXTUAL-RULES-8.4-2026-07-19"
STRICT_MATCHER_SOURCE_SHA256 = (
    "289d19993cf33caef3358f9f5b5d2989625c7659adc48494cb81c02610034f99"
)
STRICT_ENGINE_SOURCE_SHA256 = (
    "8040562b85758a6b0c7b59a7d0e7918f313f3ccf7829498401a8815d097bddf4"
)
MIN_INFORMATIVE_FUTURE_PROGRAMMES = 2
MIN_FUTURE_HORIZON_SECONDS = 6 * 60 * 60

# These methods are deterministic structural identity operations.  They still
# pass the adapter's independent exact-ID, single-market, ambiguity and strong
# programme gates before a real EPGShare mapping can be written.  Fuzzy,
# containment, near-exact, and legacy rules are deliberately absent.
SAFE_REAL_METHODS = frozenset(
    {
        "approved_knowledge",
        "canonical_identity",
        "category_language_default",
        "descriptor_relaxed",
        "edition_aware",
        "spacing_compact",
        "strict",
        "token_multiset",
        "verified_station_identity",
    }
)

# These pinned rules classify streams that intentionally have no stable linear
# schedule.  Their exact dummy ID and classification family are verified below;
# they never borrow the real-programme finalization path.
SAFE_DUMMY_METHODS = frozenset(
    {
        "safety_rule",
        "virtual_360_event_bank",
        "synthetic_numbered_genre_slot",
        "inventory_numbered_bank",
    }
)
DUMMY_REVIEW_METHODS = frozenset(
    {
        "safety_rule",
        "virtual_360_event_bank",
        "synthetic_numbered_genre_slot",
        "heading_placeholder",
        "inventory_numbered_bank",
    }
)

# Kept explicit both as documentation and as a regression-testable contract.
FORBIDDEN_AUTOMATIC_METHODS = frozenset(
    {
        "contextual_fuzzy",
        "regional_context_containment",
        "near_exact_orthography",
        "category_language_equivalence",
        "regional_catalog_extension",
        "verified_legacy_rule",
        "verified_legacy_exact",
        "panel",
        "unmatched",
        "unsupported_catalog_scope",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STRONG_ADULT_EVIDENCE_RE = re.compile(
    r"\b(?:for\s+adults?|xxx|playboy|brazzers|"
    r"porn(?:o|hub|ography|ographic)?|erotic(?:a|ism)?|onlyfans|hustler|"
    r"red\s*light|sextreme)\b|(?:^|\W)18\s*\+(?:\W|$)",
    re.IGNORECASE,
)
_SAFE_ADULT_PHRASE_RE = re.compile(
    r"\badult[\s._-]+(?:swim|contemporary|alternative|education)\b|"
    r"\b(?:pop|music)[\s._-]+adult\b",
    re.IGNORECASE,
)
# Deliberately narrow parental-safety skeleton.  Apply this only inside
# ``_security_normalize_text``; channel identity and ordinary metadata must
# retain their exact Unicode spelling.  Keys are lowercase because casefolding
# happens before translation, which also covers the corresponding capitals.
_ADULT_CONFUSABLE_SKELETON = str.maketrans(
    {
        "α": "a",  # Greek alpha
        "а": "a",  # Cyrillic a (for example: Аdult)
        "ϲ": "c",  # Greek lunate sigma
        "σ": "c",  # NFKC/casefold result of Greek lunate sigma
        "с": "c",  # Cyrillic es (for example: erotiс)
        "ԁ": "d",  # Cyrillic Komi de (for example: Aԁult)
        "ε": "e",  # Greek epsilon
        "е": "e",  # Cyrillic ie (for example: еrotic)
        "ι": "i",  # Greek iota
        "і": "i",  # Ukrainian i (for example: erotіc)
        "ӏ": "l",  # Cyrillic palochka (for example: aduӏt)
        "ο": "o",  # Greek omicron (for example: Pοrn)
        "о": "o",  # Cyrillic o (for example: Pоrn)
        "ρ": "p",  # Greek rho
        "р": "p",  # Cyrillic er (for example: рorn)
        "ѕ": "s",  # Cyrillic dze
        "τ": "t",  # Greek tau
        "т": "t",  # Cyrillic te
        "υ": "u",  # Greek upsilon (for example: adυlt)
        "χ": "x",  # Greek chi (for example: ΧΧΧ)
        "х": "x",  # Cyrillic ha (for example: ххх)
        "×": "x",  # multiplication sign (for example: ×××)
        "у": "y",  # Cyrillic u (for example: plaуboy)
    }
)
_DUMMY_ID_RE = re.compile(r"(?:^|[._/\\-])dummy(?:[._/\\-]|$)", re.IGNORECASE)
_ADULT_DUMMY_IDS = frozenset({"adult.programming.dummy.us"})
_CONTINUOUS_DUMMY_IDS = frozenset({"24.7.dummy.us", "movie.dummy.us"})
_EVENT_DUMMY_IDS = frozenset(
    {
        "ppv.events.dummy.us",
        "espn+.dummy.us",
        "flo.events.dummy.us",
        "fite.tv.dummy.us",
        "trillertv.dummy.us",
    }
)
_NUMBERED_BANK_DUMMY_IDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "virtual_360_event_bank": frozenset({"ppv.events.dummy.us"}),
        "synthetic_numbered_genre_slot": frozenset(
            {"movie.dummy.us", "ppv.events.dummy.us"}
        ),
        "inventory_numbered_bank": frozenset(
            {"movie.dummy.us", "ppv.events.dummy.us"}
        ),
    }
)
_GENERIC_NUMBER_TOKENS = frozenset(
    {
        "channel",
        "channels",
        "event",
        "events",
        "feed",
        "feeds",
        "live",
        "movie",
        "movies",
        "no",
        "number",
        "ppv",
        "slot",
        "slots",
        "sport",
        "sports",
        "stream",
        "streams",
        "tv",
    }
)
_GENERIC_DECORATION_TOKENS = frozenset(
    {"us", "usa", "uk", "ca", "in", "hd", "fhd", "uhd", "sd", "4k", "8k"}
)
_SHEET_PATCH_COLUMNS = frozenset(
    {"action", "source", "epg_feed", "epg_id", "enabled", "reason", "notes"}
)
_REVIEW_ONLY_SCAN_METHODS = (
    "_contextual_containment_match",
    "_category_language_equivalence_match",
    "_regional_catalog_extension_match",
    "_near_exact_orthographic_match",
    "_contextual_fuzzy_match",
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _valid_sha256(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(_text(value).casefold()))


def _bounded(value: object, maximum: int = 500) -> str:
    text = re.sub(r"\s+", " ", _text(value))
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def _security_normalize_text(value: object) -> str:
    """Fold Unicode disguises before applying parental-safety rules.

    Compatibility normalization makes full-width Latin text comparable with
    ASCII.  Canonical decomposition plus mark removal also prevents a combining
    accent from disguising a blocked word.  Invisible format characters and
    lone surrogates are discarded; controls are discarded except that genuine
    whitespace controls become one plain boundary.  Inserting an invisible
    character inside a word therefore cannot split the classifier evidence.
    """

    normalized = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKC", str(value or ""))
    )
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if unicodedata.combining(character) or category in {"Cf", "Cs"}:
            continue
        if category == "Cc":
            # Preserve a real word boundary for tabs/newlines used between
            # fields; discard every other control character.
            if character.isspace():
                characters.append(" ")
            continue
        characters.append(character)
    folded = "".join(characters).casefold().translate(_ADULT_CONFUSABLE_SKELETON)
    return re.sub(r"\s+", " ", folded).strip()


def _append_note(existing: object, addition: object) -> str:
    old = _bounded(existing)
    new = _bounded(addition)
    if old and new:
        # Provenance is the safety record, so preserve it in full if the Sheet's
        # 500-character notes limit requires truncating the discovery note.
        return _bounded(f"{new} | {old}")
    return old or new


def automatic_mapping_binding_sha256(
    *,
    server_id: object,
    stream_id: object,
    channel_name: object,
    category_name: object,
    epg_id: object,
    match_method: object,
    market: object,
    source_sha256: object,
) -> str:
    """Bind deterministic approval provenance to one exact mapping row."""

    fields = (
        _text(server_id),
        _text(stream_id),
        _text(channel_name),
        _text(category_name),
        _text(epg_id),
        _text(match_method).casefold(),
        _market_code(market),
        _text(source_sha256).casefold(),
    )
    if (
        not all(fields[index] for index in (0, 1, 2, 4, 5, 6))
        or not _valid_sha256(fields[7])
        or fields[6] in {"ALL", "UNKNOWN", "AMBIGUOUS"}
    ):
        raise ValueError("automatic mapping provenance fields are invalid")
    digest = hashlib.sha256()
    for value in (
        "auto-map-v2",
        *fields,
        STRICT_MATCHER_VERSION,
        STRICT_MATCHER_BUILD_ID,
        STRICT_MATCHER_SOURCE_SHA256,
        STRICT_ENGINE_SOURCE_SHA256,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def automatic_mapping_provenance_note(
    *,
    server_id: object,
    stream_id: object,
    channel_name: object,
    category_name: object,
    epg_id: object,
    match_method: object,
    market: object,
    source_sha256: object,
) -> str:
    """Return compact target-bound v2 provenance plus the legacy sync marker."""

    method = _text(match_method).casefold()
    normalized_market = _market_code(market)
    source_hash = _text(source_sha256).casefold()
    binding = automatic_mapping_binding_sha256(
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_name=category_name,
        epg_id=epg_id,
        match_method=method,
        market=normalized_market,
        source_sha256=source_hash,
    )
    # ``auto-map-v1`` remains as a compatibility token for the existing Sheet
    # write boundary.  Learned memory accepts only the complete bound v2 record.
    return (
        f"auto-map-v2 method={method}; market={normalized_market}; "
        f"source_sha256={source_hash}; binding_sha256={binding} | auto-map-v1"
    )


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = _text(value)
    if text.isdigit():
        return (0, int(text), text)
    return (1, text.casefold(), text)


def _has_adult_evidence(channel_name: str, category_name: str) -> bool:
    # Keep this boundary aligned with build_epg_streaming.infer_genre.
    context = _security_normalize_text(f"{category_name} {channel_name}")
    bare_adult_evidence = _SAFE_ADULT_PHRASE_RE.sub(" ", context)
    return bool(
        _STRONG_ADULT_EVIDENCE_RE.search(context)
        or re.search(r"\badults?\b", bare_adult_evidence, re.IGNORECASE)
    )


def _safe_dummy_classification(
    *,
    method: str,
    epg_id: str,
    matcher_reason: str,
    channel_name: str,
    category_name: str,
) -> bool:
    """Verify a pinned no-schedule classification without programme fiction.

    Dummy guides are useful only when the matcher has established *why* a real
    one-to-one schedule cannot exist.  Bind every accepted method to the exact
    dummy family it is allowed to produce.  ``safety_rule`` covers several
    unrelated pinned rules, so its target and evidence are narrowed again here.
    """

    normalized_method = _text(method).casefold()
    normalized_id = _text(epg_id).casefold()
    if normalized_method not in SAFE_DUMMY_METHODS:
        return False

    numbered_targets = _NUMBERED_BANK_DUMMY_IDS.get(normalized_method)
    if numbered_targets is not None:
        return normalized_id in numbered_targets

    if normalized_method != "safety_rule":
        return False
    if normalized_id in _ADULT_DUMMY_IDS:
        return _has_adult_evidence(channel_name, category_name)
    if normalized_id in _EVENT_DUMMY_IDS:
        return bool(
            re.search(
                r"\b(?:event|events|slot|slots|ppv|sports\s+pass|whip[- ]around)\b",
                matcher_reason,
                re.IGNORECASE,
            )
        )
    if normalized_id in _CONTINUOUS_DUMMY_IDS:
        return bool(
            re.search(
                r"\b(?:24\s*[/x.-]\s*7|24[- ]hour|continuous)\b",
                matcher_reason,
                re.IGNORECASE,
            )
        )
    return False


def _is_generic_numbered_or_blank(channel_name: str) -> bool:
    words = re.findall(r"[a-z0-9]+", _text(channel_name).casefold())
    core = [word for word in words if word not in _GENERIC_DECORATION_TOKENS]
    if not core:
        return True
    if all(word.isdigit() for word in core):
        return True
    return (
        any(word.isdigit() for word in core)
        and all(word.isdigit() or word in _GENERIC_NUMBER_TOKENS for word in core)
    )


def _market_code(value: object) -> str:
    code = _text(value).upper()
    return "UK" if code == "GB" else code


def _case_variants(values: Iterable[str]) -> Mapping[str, tuple[str, ...]]:
    grouped: dict[str, set[str]] = {}
    for raw in values:
        value = _text(raw)
        if value:
            grouped.setdefault(value.casefold(), set()).add(value)
    return MappingProxyType(
        {
            key: tuple(sorted(items, key=lambda item: (item.casefold(), item)))
            for key, items in grouped.items()
        }
    )


@dataclass(frozen=True)
class MatcherIdentity:
    """Identity of the frozen matcher source used for a proposal."""

    version: str
    build_id: str
    source_sha256: str
    engine_source_sha256: str

    @classmethod
    def from_resolver(
        cls,
        resolver: Any,
        contextual_source_path: str | Path,
        engine_source_path: str | Path,
    ) -> "MatcherIdentity":
        engine = resolver.engine
        return cls(
            version=_text(getattr(engine, "SMART_RULES_VERSION", "")),
            build_id=_text(getattr(engine, "MATCHER_BUILD_ID", "")),
            source_sha256=hashlib.sha256(
                Path(contextual_source_path).read_bytes()
            ).hexdigest(),
            engine_source_sha256=hashlib.sha256(
                Path(engine_source_path).read_bytes()
            ).hexdigest(),
        )

    @property
    def is_expected(self) -> bool:
        return (
            self.version == STRICT_MATCHER_VERSION
            and self.build_id == STRICT_MATCHER_BUILD_ID
            and self.source_sha256 == STRICT_MATCHER_SOURCE_SHA256
            and self.engine_source_sha256 == STRICT_ENGINE_SOURCE_SHA256
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    """Exact IDs declared by one immutable combined-source snapshot.

    ``healthy`` must represent the caller's strict download preflight: every
    configured matcher catalog was fetched and parsed successfully, and the
    exact IDs here were obtained from the combined XMLTV source identified by
    ``source_sha256``.  A partial catalog must be marked unhealthy because
    missing candidates can turn an ambiguous family into a false unique match.
    """

    exact_ids: frozenset[str]
    source_sha256: str
    regions_by_id: Mapping[str, frozenset[str]]
    kinds_by_id: Mapping[str, frozenset[str]]
    healthy: bool = True
    health_reason: str = ""
    _variants_by_casefold: Mapping[str, tuple[str, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        cleaned = frozenset(_text(value) for value in self.exact_ids if _text(value))
        regions: dict[str, frozenset[str]] = {}
        for raw_id, raw_regions in self.regions_by_id.items():
            epg_id = _text(raw_id)
            if not epg_id or epg_id not in cleaned:
                continue
            values = (
                (raw_regions,)
                if isinstance(raw_regions, str)
                else tuple(raw_regions or ())
            )
            regions[epg_id] = frozenset(
                _text(value).upper() for value in values if _text(value)
            )
        kinds: dict[str, frozenset[str]] = {}
        for raw_id, raw_kinds in self.kinds_by_id.items():
            epg_id = _text(raw_id)
            if not epg_id or epg_id not in cleaned:
                continue
            values = (
                (raw_kinds,)
                if isinstance(raw_kinds, str)
                else tuple(raw_kinds or ())
            )
            kinds[epg_id] = frozenset(
                _text(value).casefold() for value in values if _text(value)
            )
        object.__setattr__(self, "exact_ids", cleaned)
        object.__setattr__(self, "source_sha256", _text(self.source_sha256).casefold())
        object.__setattr__(self, "regions_by_id", MappingProxyType(regions))
        object.__setattr__(self, "kinds_by_id", MappingProxyType(kinds))
        object.__setattr__(
            self, "_variants_by_casefold", _case_variants(cleaned)
        )

    @classmethod
    def from_ids(
        cls,
        ids: Iterable[str],
        *,
        source_sha256: str,
        regions_by_id: Mapping[str, Iterable[str] | str],
        kinds_by_id: Mapping[str, Iterable[str] | str],
        healthy: bool = True,
        health_reason: str = "",
    ) -> "CatalogSnapshot":
        return cls(
            exact_ids=frozenset(_text(value) for value in ids if _text(value)),
            source_sha256=source_sha256,
            regions_by_id={
                _text(key): value for key, value in regions_by_id.items()
            },
            kinds_by_id={
                _text(key): value for key, value in kinds_by_id.items()
            },
            healthy=healthy,
            health_reason=health_reason,
        )

    @classmethod
    def from_matcher_catalog(
        cls,
        declared_ids: Iterable[str],
        *,
        real_candidates: Iterable[Mapping[str, str]],
        dummy_ids: Mapping[str, str],
        source_sha256: str,
        healthy: bool = True,
        health_reason: str = "",
    ) -> "CatalogSnapshot":
        """Bind combined-source declarations to matcher region/type metadata."""

        exact_ids = frozenset(
            _text(value) for value in declared_ids if _text(value)
        )
        regions: dict[str, set[str]] = {}
        kinds: dict[str, set[str]] = {}
        for candidate in real_candidates:
            epg_id = _text(candidate.get("epg_id"))
            if epg_id not in exact_ids:
                continue
            region = _text(candidate.get("region")).upper()
            if region:
                regions.setdefault(epg_id, set()).add(region)
            kinds.setdefault(epg_id, set()).add("real")
        for raw_id in dummy_ids.values():
            epg_id = _text(raw_id)
            if epg_id not in exact_ids:
                continue
            regions.setdefault(epg_id, set()).add("DUMMY")
            kinds.setdefault(epg_id, set()).add("dummy")
        return cls.from_ids(
            exact_ids,
            source_sha256=source_sha256,
            regions_by_id=regions,
            kinds_by_id=kinds,
            healthy=healthy,
            health_reason=health_reason,
        )

    @property
    def usable(self) -> bool:
        return self.healthy and bool(self.exact_ids) and _valid_sha256(self.source_sha256)

    def contains_unambiguous_exact(self, epg_id: str) -> bool:
        target = _text(epg_id)
        return (
            target in self.exact_ids
            and self._variants_by_casefold.get(target.casefold()) == (target,)
        )

    def target_regions(self, epg_id: str) -> frozenset[str]:
        return self.regions_by_id.get(_text(epg_id), frozenset())

    def target_kinds(self, epg_id: str) -> frozenset[str]:
        return self.kinds_by_id.get(_text(epg_id), frozenset())


@dataclass(frozen=True)
class ScheduleEvidence:
    """Evidence gathered from the exact combined XMLTV source parse."""

    declared_ids: frozenset[str]
    informative_future_programmes: Mapping[str, int]
    latest_informative_future_stop: Mapping[str, int]
    gate_passed_by_id: Mapping[str, bool]
    checked_at_epoch: int
    source_sha256: str
    _variants_by_casefold: Mapping[str, tuple[str, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        declared = frozenset(
            _text(value) for value in self.declared_ids if _text(value)
        )
        counts: dict[str, int] = {}
        for raw_id, raw_count in self.informative_future_programmes.items():
            epg_id = _text(raw_id)
            if not epg_id:
                continue
            if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise ValueError("informative future programme counts must be integers")
            count = raw_count
            if count < 0:
                raise ValueError("informative future programme counts cannot be negative")
            counts[epg_id] = count
        horizons: dict[str, int] = {}
        for raw_id, raw_stop in self.latest_informative_future_stop.items():
            epg_id = _text(raw_id)
            if not epg_id:
                continue
            if isinstance(raw_stop, bool) or not isinstance(raw_stop, int):
                raise ValueError("informative future stop epochs must be integers")
            stop = raw_stop
            if stop < 0:
                raise ValueError("informative future stop epochs cannot be negative")
            horizons[epg_id] = stop
        gates: dict[str, bool] = {}
        for raw_id, passed in self.gate_passed_by_id.items():
            epg_id = _text(raw_id)
            if not epg_id:
                continue
            if not isinstance(passed, bool):
                raise ValueError("programme gate results must be booleans")
            gates[epg_id] = passed
        if isinstance(self.checked_at_epoch, bool) or not isinstance(
            self.checked_at_epoch, int
        ):
            raise ValueError("checked_at_epoch must be an integer")
        checked_at = self.checked_at_epoch
        if checked_at < 0:
            raise ValueError("checked_at_epoch cannot be negative")
        object.__setattr__(self, "declared_ids", declared)
        object.__setattr__(
            self, "informative_future_programmes", MappingProxyType(counts)
        )
        object.__setattr__(
            self, "latest_informative_future_stop", MappingProxyType(horizons)
        )
        object.__setattr__(self, "gate_passed_by_id", MappingProxyType(gates))
        object.__setattr__(self, "checked_at_epoch", checked_at)
        object.__setattr__(self, "source_sha256", _text(self.source_sha256).casefold())
        object.__setattr__(
            self, "_variants_by_casefold", _case_variants(declared)
        )

    @property
    def usable(self) -> bool:
        return (
            bool(self.declared_ids)
            and self.checked_at_epoch > 0
            and _valid_sha256(self.source_sha256)
        )

    def verifies(self, epg_id: str) -> bool:
        target = _text(epg_id)
        return (
            self.usable
            and target in self.declared_ids
            and self._variants_by_casefold.get(target.casefold()) == (target,)
            and int(self.informative_future_programmes.get(target, 0))
            >= MIN_INFORMATIVE_FUTURE_PROGRAMMES
            and int(self.latest_informative_future_stop.get(target, 0))
            >= self.checked_at_epoch + MIN_FUTURE_HORIZON_SECONDS
            and bool(self.gate_passed_by_id.get(target, False))
        )


@dataclass(frozen=True)
class MatchProposal:
    """A non-authoritative matcher result for one previously unseen stream."""

    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    matcher_action: str
    target_source: str
    target_feed: str
    target_epg_id: str
    match_method: str
    matcher_reason: str
    second_epg_id: str
    route_explicit: bool
    explicit_market: str
    route_plan: tuple[str, ...]
    eligible_for_finalization: bool
    decision_reason: str
    matcher_identity: MatcherIdentity
    catalog_source_sha256: str

    @property
    def key(self) -> tuple[str, str]:
        return self.server_id, self.stream_id


@dataclass(frozen=True)
class FinalizedMatch:
    """Final mapping decision, expressible using existing Sheet columns only."""

    proposal: MatchProposal
    approved: bool
    reason: str
    schedule_source_sha256: str = ""

    def sheet_patch(self, *, existing_notes: object = "") -> dict[str, str]:
        proposal = self.proposal
        has_target = bool(proposal.target_epg_id)
        inactive_action = (
            "IGNORE" if proposal.matcher_action == "IGNORE" else "REVIEW"
        )
        patch: dict[str, str] = {
            "action": proposal.matcher_action if self.approved else inactive_action,
            "enabled": "TRUE" if self.approved else "FALSE",
            "reason": _bounded(self.reason),
        }

        # Expose a provisional target for a reviewer, but never make it runnable
        # unless ``approved`` is true.  Server 1 always remains in EPGShare's
        # namespace even when there is no candidate.
        if has_target or proposal.server_id == "server_1":
            patch["source"] = proposal.target_source or "epgshare01"
            patch["epg_feed"] = proposal.target_feed or "ALL_SOURCES1"
            patch["epg_id"] = proposal.target_epg_id

        if self.approved and proposal.matcher_action == "AUTO_EPGSHARE":
            provenance = automatic_mapping_provenance_note(
                server_id=proposal.server_id,
                stream_id=proposal.stream_id,
                channel_name=proposal.channel_name,
                category_name=proposal.category_name,
                epg_id=proposal.target_epg_id,
                match_method=proposal.match_method,
                market=proposal.explicit_market,
                source_sha256=self.schedule_source_sha256,
            )
        else:
            provenance = (
                f"auto-map-v1 method={proposal.match_method or 'none'}; "
                f"market={proposal.explicit_market or 'unknown'}; "
                f"matcher={proposal.matcher_identity.version}; "
                f"matcher_build={proposal.matcher_identity.build_id}; "
                f"matcher_sha256={proposal.matcher_identity.source_sha256}; "
                f"engine_sha256={proposal.matcher_identity.engine_source_sha256}; "
                f"catalog_sha256={proposal.catalog_source_sha256 or 'unavailable'}; "
                f"source_sha256={self.schedule_source_sha256 or 'not-finalized'}"
            )
        patch["notes"] = _append_note(existing_notes, provenance)
        if not set(patch).issubset(_SHEET_PATCH_COLUMNS):
            raise AssertionError("auto-map adapter attempted to alter unsupported columns")
        return patch


@dataclass(frozen=True)
class MatcherPreflight:
    ready: bool
    reason: str


def prepare_resolver_strict(
    resolver: Any,
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
    *,
    include_legacy_self_tests: bool = True,
) -> MatcherPreflight:
    """Prepare and verify the resolver's indexes for one volatile live catalog.

    The frozen 222-case historical regression suite is intentionally executed
    by both GitHub workflows against its stable synthetic fixture before this
    function is reached.  It must *not* be executed against EPGShare's live
    catalog: a historical positive ID can legitimately be renamed or removed,
    which would otherwise stop every production run even though the downloaded
    catalog is complete and healthy.

    This runtime boundary instead verifies that the complete live candidate and
    dummy sets were loaded exactly into the pinned engine and v8 indexes.  The
    adapter's exact-ID, real/dummy, market, ambiguity and programme gates remain
    independent fail-closed checks after preparation.  The keyword argument is
    retained for backward API compatibility; it no longer changes this rule.
    """

    del include_legacy_self_tests
    try:
        if not isinstance(real_candidates, list) or not isinstance(dummy_ids, dict):
            raise TypeError("matcher catalog containers are invalid")
        expected_ids: list[str] = []
        expected_folds: set[str] = set()
        for candidate in real_candidates:
            if not isinstance(candidate, Mapping):
                raise TypeError("matcher candidate is not a mapping")
            epg_id = _text(candidate.get("epg_id"))
            feed = _text(candidate.get("feed")).upper()
            region = _text(candidate.get("region")).upper()
            if not epg_id or not feed or not region:
                raise ValueError("matcher candidate is incomplete")
            folded = epg_id.casefold()
            if folded in expected_folds:
                raise ValueError("matcher candidate IDs are case-ambiguous")
            expected_ids.append(epg_id)
            expected_folds.add(folded)
        if not expected_ids:
            raise ValueError("matcher real catalog is empty")

        expected_dummies = {
            _text(key).casefold(): _text(value)
            for key, value in dummy_ids.items()
            if _text(key) and _text(value)
        }
        if len(expected_dummies) != len(dummy_ids):
            raise ValueError("matcher dummy catalog is invalid")

        prepare = getattr(resolver, "prepare", None)
        if not callable(prepare):
            raise TypeError("matcher prepare function is unavailable")
        prepare(real_candidates, dummy_ids)

        engine = getattr(resolver, "engine", None)
        candidates = getattr(engine, "CANDIDATES", None)
        lookup = getattr(engine, "ID_LOOKUP", None)
        prepared_dummies = getattr(engine, "DUMMY_IDS", None)
        build_profiles = getattr(engine, "build_inventory_profiles_v8", None)
        if (
            not isinstance(candidates, list)
            or not isinstance(lookup, dict)
            or not isinstance(prepared_dummies, dict)
            or not callable(build_profiles)
        ):
            raise TypeError("matcher engine indexes are unavailable")
        prepared_ids = [_text(item.get("epg_id")) for item in candidates]
        if prepared_ids != expected_ids:
            raise ValueError("matcher engine did not load the exact live catalog")
        if set(lookup) != expected_folds or any(
            _text(lookup[folded].get("epg_id")) != epg_id
            for folded, epg_id in zip(
                (value.casefold() for value in expected_ids), expected_ids
            )
        ):
            raise ValueError("matcher exact-ID lookup does not match the live catalog")
        if prepared_dummies != expected_dummies:
            raise ValueError("matcher dummy index does not match the live catalog")

        state = getattr(resolver, "state", None)
        fingerprint = _text(getattr(state, "fingerprint", ""))
        by_region = getattr(state, "by_region", None)
        all_contexts = by_region.get("ALL") if isinstance(by_region, dict) else None
        if not fingerprint or not isinstance(all_contexts, list):
            raise TypeError("matcher v8 indexes are unavailable")
        indexed_ids = [
            _text(getattr(context, "candidate", {}).get("epg_id"))
            for context in all_contexts
        ]
        if len(indexed_ids) != len(expected_ids) or set(indexed_ids) != set(expected_ids):
            raise ValueError("matcher v8 indexes do not match the live catalog")
    except Exception as exc:  # The production boundary is deliberately fail closed.
        return MatcherPreflight(
            False,
            _bounded(f"Smart Rules preflight failed ({type(exc).__name__}): {exc}"),
        )
    return MatcherPreflight(True, "Pinned Smart Rules live-catalog indexes verified")


def _canonical_target(match: Mapping[str, Any]) -> tuple[str, str, str]:
    action = _text(match.get("action")).upper()
    epg_id = _text(match.get("epg_id"))
    if action == "AUTO_EPGSHARE" or _text(match.get("source")).casefold() in {
        "epgshare",
        "epgshare01",
        "epgshare_candidate",
    }:
        return "epgshare01", "ALL_SOURCES1", epg_id
    if action == "AUTO_DUMMY" or _text(match.get("source")).casefold() == "dummy":
        return "dummy", "DUMMY_CHANNELS", epg_id
    return "", "", ""


def _resolve_without_review_only_scans(
    resolver: Any,
    row: Mapping[str, Any],
    *,
    category_profile: Mapping[str, Any] | None,
    inventory_signal: Mapping[str, Any] | None,
) -> tuple[Any, Mapping[str, Any]]:
    """Resolve once while suppressing paths that can only yield REVIEW.

    Some frozen v8 fallbacks scan an entire regional catalog.  Repeating those
    scans for a large first inventory is unnecessary because this adapter never
    auto-enables their methods.  Instance-level overrides leave the frozen source
    untouched and are restored in ``finally``.  The resolver/engine already use
    mutable global indexes and must therefore be called sequentially, not shared
    between worker threads.
    """

    instance_values = getattr(resolver, "__dict__", {})
    sentinel = object()
    saved: dict[str, object] = {
        name: instance_values.get(name, sentinel)
        for name in (*_REVIEW_ONLY_SCAN_METHODS, "_legacy_verified_match")
    }

    def no_match(*_args: Any, **_kwargs: Any) -> None:
        return None

    def station_only(
        station_row: dict[str, Any], query: Any, *, pre_panel: bool
    ) -> dict[str, Any] | None:
        # All pre-panel legacy rules and all post-panel generic legacy fallbacks
        # have review-only method labels in this adapter.  Preserve only exact US
        # call-sign/PBS identity, whose distinct method is allowlisted.
        if pre_panel or "US" not in tuple(getattr(query, "route_plan", ()) or ()):
            return None
        for name in ("callsign_match_v7", "pbs_brand_match_v7"):
            matcher = getattr(resolver.engine, name, None)
            if not callable(matcher):
                continue
            match = matcher(station_row)
            if match:
                result = dict(match)
                result["match_method"] = "verified_station_identity"
                return result
        return None

    try:
        for name in _REVIEW_ONLY_SCAN_METHODS:
            setattr(resolver, name, no_match)
        setattr(resolver, "_legacy_verified_match", station_only)
        return resolver.resolve(
            dict(row),
            panel_is_usable=False,
            category_profile=category_profile,
            inventory_signal=inventory_signal,
        )
    finally:
        for name, old_value in saved.items():
            if old_value is sentinel:
                try:
                    delattr(resolver, name)
                except AttributeError:
                    pass
            else:
                setattr(resolver, name, old_value)


def _review_only_proposal(
    *,
    server_id: str,
    stream_id: str,
    channel_name: str,
    category_name: str,
    identity: MatcherIdentity,
    catalog: CatalogSnapshot,
    reason: str,
) -> MatchProposal:
    return MatchProposal(
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_name=category_name,
        matcher_action="REVIEW",
        target_source="epgshare01" if server_id == "server_1" else "",
        target_feed="ALL_SOURCES1" if server_id == "server_1" else "",
        target_epg_id="",
        match_method="",
        matcher_reason="",
        second_epg_id="",
        route_explicit=False,
        explicit_market="",
        route_plan=(),
        eligible_for_finalization=False,
        decision_reason=_bounded(reason),
        matcher_identity=identity,
        catalog_source_sha256=catalog.source_sha256,
    )


def _proposal_from_match(
    *,
    server_id: str,
    stream_id: str,
    channel_name: str,
    category_name: str,
    match: Mapping[str, Any],
    query: Any,
    identity: MatcherIdentity,
    catalog: CatalogSnapshot,
) -> MatchProposal:
    action = _text(match.get("action")).upper()
    method = _text(match.get("match_method")).casefold()
    matcher_reason = _bounded(match.get("reason"))
    second_epg_id = _text(match.get("second_epg_id"))
    target_source, target_feed, target_epg_id = _canonical_target(match)
    route_explicit = bool(getattr(query, "route_explicit", False))
    explicit_market = _text(getattr(query, "explicit_market", "")).upper()
    route_plan = tuple(
        _text(value).upper()
        for value in (getattr(query, "route_plan", ()) or ())
        if _text(value)
    )
    eligible = True
    rejection = ""
    sheet_action = action if action in {"AUTO_EPGSHARE", "AUTO_DUMMY"} else "REVIEW"

    if action == "AUTO_DUMMY" and method == "heading_placeholder":
        # Decorative headings are inventory furniture, not channels.  Preserve
        # the classification while keeping them permanently disabled and out of
        # the unresolved mapping queue.
        eligible = False
        sheet_action = "IGNORE"
        rejection = "Decorative heading/placeholder is disabled and ignored"
    elif action == "AUTO_EPGSHARE":
        if method not in SAFE_REAL_METHODS:
            eligible = False
            rejection = f"Matcher method {method or 'unknown'} is review-only"
        if target_source != "epgshare01":
            eligible = False
            rejection = "EPGShare action did not return an EPGShare target"
    elif action == "AUTO_DUMMY":
        if target_source != "dummy":
            eligible = False
            rejection = "AUTO_DUMMY did not return a dummy target"
        elif not _safe_dummy_classification(
            method=method,
            epg_id=target_epg_id,
            matcher_reason=matcher_reason,
            channel_name=channel_name,
            category_name=category_name,
        ):
            eligible = False
            rejection = "Dummy classification is not in an approved no-schedule family"
    else:
        eligible = False
        rejection = (
            "Matcher returned a review-only candidate"
            if target_epg_id
            else matcher_reason or "No safe candidate was found"
        )

    if method in FORBIDDEN_AUTOMATIC_METHODS:
        eligible = False
        rejection = f"Matcher method {method} is explicitly review-only"
    if not target_epg_id:
        eligible = False
        rejection = rejection or "Matcher did not return an exact EPG ID"
    elif not catalog.contains_unambiguous_exact(target_epg_id):
        eligible = False
        rejection = "Target is absent from the exact ALL catalog or has case ambiguity"
    elif action == "AUTO_EPGSHARE":
        target_kinds = catalog.target_kinds(target_epg_id)
        target_regions = frozenset(
            _market_code(value) for value in catalog.target_regions(target_epg_id)
        )
        if _DUMMY_ID_RE.search(target_epg_id) or target_kinds != frozenset({"real"}):
            eligible = False
            rejection = "Target is a dummy or lacks unambiguous real-catalog type evidence"
        elif len(target_regions) != 1 or _market_code(explicit_market) not in target_regions:
            eligible = False
            rejection = "Target catalog region does not match the explicit channel market"
    elif action == "AUTO_DUMMY":
        target_kinds = catalog.target_kinds(target_epg_id)
        if target_kinds != frozenset({"dummy"}):
            eligible = False
            rejection = "Target lacks unambiguous dummy-catalog type evidence"
    if second_epg_id:
        eligible = False
        rejection = "A second EPG ID makes the match ambiguous"
    if action == "AUTO_EPGSHARE":
        if not route_explicit or explicit_market in {"", "ALL", "UNKNOWN", "AMBIGUOUS"}:
            eligible = False
            rejection = "Market is unknown or was not established by explicit evidence"
        elif (
            len(route_plan) != 1
            or _market_code(route_plan[0]) != _market_code(explicit_market)
        ):
            eligible = False
            rejection = "Market route is ambiguous and requires review"
        if _is_generic_numbered_or_blank(channel_name):
            eligible = False
            rejection = "Blank or generic numbered channel identity requires review"
        if _has_adult_evidence(channel_name, category_name):
            eligible = False
            rejection = "Adult category/name evidence cannot receive a real EPG mapping"
    if server_id == "server_1" and (
        action == "KEEP_PANEL"
        or _text(match.get("source")).casefold() == "panel"
        or target_source == "panel"
    ):
        eligible = False
        target_source, target_feed, target_epg_id = "epgshare01", "ALL_SOURCES1", ""
        rejection = "Server 1 native panel mappings are forbidden"

    return MatchProposal(
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_name=category_name,
        matcher_action=sheet_action,
        target_source=target_source,
        target_feed=target_feed,
        target_epg_id=target_epg_id,
        match_method=method,
        matcher_reason=matcher_reason,
        second_epg_id=second_epg_id,
        route_explicit=route_explicit,
        explicit_market=explicit_market,
        route_plan=route_plan,
        eligible_for_finalization=eligible,
        decision_reason=_bounded(rejection or matcher_reason or "Exact candidate requires XMLTV verification"),
        matcher_identity=identity,
        catalog_source_sha256=catalog.source_sha256,
    )


def propose_new_channel_matches(
    resolver: Any,
    *,
    server_id: str,
    channels: Sequence[Mapping[str, Any]],
    category_names: Mapping[str, str],
    existing_keys: Iterable[tuple[str, str]],
    target_keys: Iterable[tuple[str, str]] | None = None,
    catalog: CatalogSnapshot,
    matcher_identity: MatcherIdentity,
    preflight: MatcherPreflight,
) -> dict[tuple[str, str], MatchProposal]:
    """Return provisional decisions for an explicit safe identity boundary.

    Inventory profiles are built from the complete current server lineup so a
    newly discovered member of a numbered bank retains the frozen matcher's
    lineup-level safety signal.  By default ``resolver.resolve`` is called only
    for previously unseen rows.  A caller may instead supply ``target_keys`` to
    re-evaluate an explicitly authorized set of existing disabled ``REVIEW``
    rows.  The explicit set is an allowlist, never a request to scan every
    existing mapping.
    """

    normalized_server = str(server_id or "")
    if not re.fullmatch(r"server_[1-9]\d*", normalized_server):
        raise ValueError("server_id must use canonical server_N form")
    category_map = {_text(key): _text(value) for key, value in category_names.items()}
    existing = {
        (_text(item[0]).casefold(), _text(item[1]))
        for item in existing_keys
        if len(item) == 2
    }
    targets = None
    if target_keys is not None:
        targets = {
            (_text(item[0]).casefold(), _text(item[1]))
            for item in target_keys
            if len(item) == 2
        }
        if any(key[0] != normalized_server.casefold() for key in targets):
            raise ValueError("target_keys contains an identity for another server")
    inventory = [dict(channel) for channel in channels]

    seen_inventory_keys: set[tuple[str, str]] = set()
    normalized_rows: list[tuple[str, dict[str, Any]]] = []
    for channel in inventory:
        stream_id = _text(channel.get("stream_id") or channel.get("id"))
        if not stream_id:
            raise ValueError("every inventory channel requires stream_id")
        key = (normalized_server, stream_id)
        if key in seen_inventory_keys:
            raise ValueError(f"duplicate inventory identity: {key!r}")
        seen_inventory_keys.add(key)
        normalized_rows.append((stream_id, channel))

    inventory_keys = {
        (normalized_server.casefold(), stream_id)
        for stream_id, _channel in normalized_rows
    }
    if targets is None:
        selected_rows = [
            item
            for item in normalized_rows
            if (normalized_server.casefold(), item[0]) not in existing
        ]
    else:
        missing_targets = targets.difference(inventory_keys)
        if missing_targets:
            raise ValueError("target_keys contains an identity absent from inventory")
        selected_rows = [
            item
            for item in normalized_rows
            if (normalized_server.casefold(), item[0]) in targets
        ]
    if not selected_rows:
        return {}

    boundary_failure = ""
    if not matcher_identity.is_expected:
        boundary_failure = "Frozen matcher identity/version preflight failed"
    elif not preflight.ready:
        boundary_failure = preflight.reason or "Smart Rules preflight failed"
    elif not catalog.usable:
        boundary_failure = catalog.health_reason or "EPGShare catalog snapshot is unhealthy"

    if boundary_failure:
        return {
            (normalized_server, stream_id): _review_only_proposal(
                server_id=normalized_server,
                stream_id=stream_id,
                channel_name=_text(
                    channel.get("name")
                    or channel.get("channel_name")
                    or channel.get("stream_display_name")
                ),
                category_name=category_map.get(
                    _text(channel.get("category_id")),
                    _text(channel.get("category_name")),
                ),
                identity=matcher_identity,
                catalog=catalog,
                reason=boundary_failure,
            )
            for stream_id, channel in sorted(
                selected_rows, key=lambda item: _stream_sort_key(item[0])
            )
        }

    build_profiles = getattr(resolver.engine, "build_inventory_profiles_v8", None)
    if not callable(build_profiles):
        raise RuntimeError("Smart Rules inventory profile builder is unavailable")
    profiles, signals = build_profiles(inventory, category_map)
    proposals: dict[tuple[str, str], MatchProposal] = {}
    for stream_id, channel in sorted(
        selected_rows, key=lambda item: _stream_sort_key(item[0])
    ):
        category_id = _text(channel.get("category_id"))
        category_name = category_map.get(
            category_id, _text(channel.get("category_name"))
        )
        channel_name = _text(
            channel.get("name")
            or channel.get("channel_name")
            or channel.get("stream_display_name")
        )
        if not channel_name:
            proposals[(normalized_server, stream_id)] = _review_only_proposal(
                server_id=normalized_server,
                stream_id=stream_id,
                channel_name="",
                category_name=category_name,
                identity=matcher_identity,
                catalog=catalog,
                reason="Inventory channel name is blank",
            )
            continue
        panel_epg_id = (
            ""
            if normalized_server == "server_1"
            else _text(channel.get("epg_channel_id"))
        )
        row = {
            "channel_name": channel_name,
            "category_name": category_name,
            "panel_epg_id": panel_epg_id,
        }
        try:
            query, raw_match = _resolve_without_review_only_scans(
                resolver,
                row,
                category_profile=profiles.get(category_id),
                inventory_signal=signals.get(stream_id),
            )
            if not isinstance(raw_match, Mapping):
                raise TypeError("matcher result is not a mapping")
            proposal = _proposal_from_match(
                server_id=normalized_server,
                stream_id=stream_id,
                channel_name=channel_name,
                category_name=category_name,
                match=raw_match,
                query=query,
                identity=matcher_identity,
                catalog=catalog,
            )
        except Exception as exc:  # Keep inventory sync useful while disabling automation.
            proposal = _review_only_proposal(
                server_id=normalized_server,
                stream_id=stream_id,
                channel_name=channel_name,
                category_name=category_name,
                identity=matcher_identity,
                catalog=catalog,
                reason=f"Matcher failed closed ({type(exc).__name__}): {exc}",
            )
        proposals[proposal.key] = proposal
    return proposals


def finalize_proposal(
    proposal: MatchProposal,
    evidence: ScheduleEvidence,
) -> FinalizedMatch:
    """Finalize a real schedule or a verified no-schedule classification.

    Real EPGShare targets require exact, informative XMLTV evidence below.
    Exact dummy classifications deliberately bypass that programme gate: a
    dummy exists precisely because the stream has no stable real schedule.
    Their method, ID, catalog type and classification evidence were already
    independently checked while constructing the immutable proposal.
    """

    if not proposal.eligible_for_finalization:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason=proposal.decision_reason,
            schedule_source_sha256=evidence.source_sha256 if evidence.usable else "",
        )
    if proposal.matcher_action == "AUTO_DUMMY":
        return FinalizedMatch(
            proposal=proposal,
            approved=True,
            reason=_bounded(
                "Automatically classified as a no-schedule stream: "
                f"method={proposal.match_method}; dummy_id={proposal.target_epg_id}; "
                f"matcher={proposal.matcher_identity.version}; "
                f"catalog_sha256={proposal.catalog_source_sha256}"
            ),
            # This is catalog provenance, not a claim that a real programme
            # schedule was validated for the dummy channel.
            schedule_source_sha256=proposal.catalog_source_sha256,
        )
    if not evidence.usable:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason="Combined XMLTV evidence is unavailable or lacks a valid source SHA-256",
        )
    if proposal.catalog_source_sha256 != evidence.source_sha256:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason=(
                "Catalog proposal and programme evidence came from different "
                "combined-source SHA-256 snapshots"
            ),
            schedule_source_sha256=evidence.source_sha256,
        )
    if proposal.target_epg_id not in evidence.declared_ids:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason="Provisional EPG ID was not declared exactly in the combined XMLTV source",
            schedule_source_sha256=evidence.source_sha256,
        )
    variants = evidence._variants_by_casefold.get(
        proposal.target_epg_id.casefold(), ()
    )
    if variants != (proposal.target_epg_id,):
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason="Combined XMLTV contains ambiguous case variants for the provisional EPG ID",
            schedule_source_sha256=evidence.source_sha256,
        )
    count = int(
        evidence.informative_future_programmes.get(proposal.target_epg_id, 0)
    )
    if not bool(evidence.gate_passed_by_id.get(proposal.target_epg_id, False)):
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason="Exact EPG ID did not pass the configured programme safety gate",
            schedule_source_sha256=evidence.source_sha256,
        )
    if count < MIN_INFORMATIVE_FUTURE_PROGRAMMES:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason=(
                "Exact EPG ID has fewer than the required informative future "
                "programmes"
            ),
            schedule_source_sha256=evidence.source_sha256,
        )
    latest_stop = int(
        evidence.latest_informative_future_stop.get(proposal.target_epg_id, 0)
    )
    if latest_stop < evidence.checked_at_epoch + MIN_FUTURE_HORIZON_SECONDS:
        return FinalizedMatch(
            proposal=proposal,
            approved=False,
            reason="Exact EPG ID has no verified informative future horizon",
            schedule_source_sha256=evidence.source_sha256,
        )
    return FinalizedMatch(
        proposal=proposal,
        approved=True,
        reason=_bounded(
            f"Automatically mapped after exact XMLTV verification: "
            f"method={proposal.match_method}; informative_future_programmes={count}; "
            f"future_horizon_epoch={latest_stop}; "
            f"matcher={proposal.matcher_identity.version}; "
            f"source_sha256={evidence.source_sha256}"
        ),
        schedule_source_sha256=evidence.source_sha256,
    )


__all__ = [
    "CatalogSnapshot",
    "DUMMY_REVIEW_METHODS",
    "FinalizedMatch",
    "FORBIDDEN_AUTOMATIC_METHODS",
    "MatchProposal",
    "MatcherIdentity",
    "MatcherPreflight",
    "MIN_FUTURE_HORIZON_SECONDS",
    "MIN_INFORMATIVE_FUTURE_PROGRAMMES",
    "SAFE_DUMMY_METHODS",
    "SAFE_REAL_METHODS",
    "ScheduleEvidence",
    "STRICT_ENGINE_SOURCE_SHA256",
    "STRICT_MATCHER_BUILD_ID",
    "STRICT_MATCHER_SOURCE_SHA256",
    "STRICT_MATCHER_VERSION",
    "automatic_mapping_binding_sha256",
    "automatic_mapping_provenance_note",
    "finalize_proposal",
    "prepare_resolver_strict",
    "propose_new_channel_matches",
]
