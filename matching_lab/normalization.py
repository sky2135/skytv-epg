"""Deterministic retrieval views and protected-semantics checks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from .models import ProtectedSemantics


WORD_RE = re.compile(r"[a-z0-9]+")
SOUTH_ASIA_MARKETS = frozenset({"AF", "BD", "BT", "IN", "LK", "MV", "NP", "PK"})
SOUTH_ASIA_TERMS = frozenset(
    {
        "assam",
        "assamese",
        "bangla",
        "bangladesh",
        "bengali",
        "bgl",
        "bho",
        "bhoj",
        "bhojpuri",
        "guj",
        "gujarati",
        "hindi",
        "hin",
        "india",
        "indian",
        "kan",
        "kannada",
        "mal",
        "malayalam",
        "mar",
        "marathi",
        "mly",
        "nepal",
        "nepali",
        "odia",
        "ori",
        "oriya",
        "pakistan",
        "pakistani",
        "panjabi",
        "pb",
        "pjb",
        "pun",
        "punj",
        "punjab",
        "punjabi",
        "sinhala",
        "tamil",
        "tam",
        "telugu",
        "tlg",
        "urdu",
    }
)
EXPLICIT_LANGUAGE_TERMS = SOUTH_ASIA_TERMS | frozenset(
    {
        "ar",
        "arabic",
        "en",
        "eng",
        "english",
        "espanol",
        "fr",
        "french",
        "spanish",
    }
)


def ascii_fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", str(value or "")))
    return "".join(character for character in text if not unicodedata.combining(character))


def words(value: object) -> tuple[str, ...]:
    return tuple(WORD_RE.findall(ascii_fold(value).casefold()))


def acronym(tokens: Iterable[str]) -> str:
    result: list[str] = []
    for token in tokens:
        if not token:
            continue
        result.append(token if token.isdigit() else token[0])
    return "".join(result)


def character_ngrams(value: object, size: int = 3) -> frozenset[str]:
    compact = "".join(words(value))
    if not compact:
        return frozenset()
    if len(compact) <= size:
        return frozenset({compact})
    return frozenset(compact[index : index + size] for index in range(len(compact) - size + 1))


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a.intersection(b)) / len(a.union(b))


def lexical_cohort_risk_codes(
    channel_name: object,
    category_name: object,
    market: object,
) -> tuple[str, ...]:
    """Return replayable market/language risks from public identity text."""

    combined = f"{channel_name or ''} {category_name or ''}"
    tokens = set(words(combined))
    risks: set[str] = set()
    if str(market or "").upper() in SOUTH_ASIA_MARKETS or tokens.intersection(
        SOUTH_ASIA_TERMS
    ):
        risks.add("RISK_SOUTH_ASIA_COHORT")
    if tokens.intersection(EXPLICIT_LANGUAGE_TERMS):
        risks.add("RISK_EXPLICIT_LANGUAGE")
    if any(any(character.isdigit() for character in token) for token in tokens):
        risks.add("RISK_EXPLICIT_NUMBER")
    if "+" in combined or "plus" in tokens:
        risks.add("RISK_PLUS_VARIANT")
    if tokens.intersection({"east", "eastern", "pacific", "west", "western"}):
        risks.add("RISK_DIRECTION_VARIANT")
    if re.search(r"(?:\+|-)\s*\d+", combined) or tokens.intersection(
        {"timeshift", "time-shift"}
    ):
        risks.add("RISK_TIMESHIFT_VARIANT")
    if tokens.intersection({"alt", "alternate", "extra", "xtra"}):
        risks.add("RISK_EDITION_VARIANT")
    return tuple(sorted(risks))


@dataclass(frozen=True, slots=True)
class NameViews:
    strict: str
    relaxed: str
    compact: str
    edition: str
    bag: str
    tokens: tuple[str, ...]
    acronym: str
    trigrams: frozenset[str]
    transliterated: str

    @classmethod
    def from_context(cls, context: Any) -> "NameViews":
        context_tokens = tuple(str(value).casefold() for value in tuple(context.tokens))
        transliterated = " ".join(words(getattr(context, "core_name", "") or getattr(context, "strict_key", "")))
        return cls(
            strict=str(context.strict_key or ""),
            relaxed=str(context.relaxed_key or ""),
            compact=str(context.compact_key or ""),
            edition=str(context.edition_key or ""),
            bag=str(context.bag_key or ""),
            tokens=context_tokens,
            acronym=acronym(context_tokens),
            trigrams=character_ngrams(str(context.relaxed_key or context.strict_key or "")),
            transliterated=transliterated,
        )

    @classmethod
    def from_text(cls, value: object) -> "NameViews":
        token_values = words(value)
        normalized = " ".join(token_values)
        compact = "".join(token_values)
        return cls(
            strict=normalized,
            relaxed=normalized,
            compact=compact,
            edition=normalized,
            bag=" ".join(sorted(token_values)),
            tokens=token_values,
            acronym=acronym(token_values),
            trigrams=character_ngrams(normalized),
            transliterated=normalized,
        )


def semantics_from_context(context: Any, *, market: str = "") -> ProtectedSemantics:
    return ProtectedSemantics(
        market=str(market or "").upper(),
        direction=str(context.direction or ""),
        timeshift=str(context.timeshift or ""),
        has_plus=bool(context.has_plus),
        has_extra=bool(context.has_extra),
        has_alternate=bool(context.has_alternate),
        numbers=tuple(str(value) for value in context.numbers),
        languages=tuple(str(value) for value in context.languages),
        content=tuple(str(value) for value in context.content),
    )


def protected_conflicts(
    query: ProtectedSemantics,
    candidate: ProtectedSemantics,
    *,
    route_explicit: bool,
) -> tuple[str, ...]:
    """Return symmetric identity contradictions; absence is not agreement."""

    conflicts: set[str] = set()
    if route_explicit and query.market and candidate.market and query.market != candidate.market:
        conflicts.add("MARKET_MISMATCH")
    if query.numbers or candidate.numbers:
        if query.numbers != candidate.numbers:
            conflicts.add("NUMBER_MISMATCH")
    if (query.direction or candidate.direction) and query.direction != candidate.direction:
        conflicts.add("DIRECTION_MISMATCH")
    if (query.timeshift or candidate.timeshift) and query.timeshift != candidate.timeshift:
        conflicts.add("TIMESHIFT_MISMATCH")
    if query.has_plus != candidate.has_plus:
        conflicts.add("PLUS_VARIANT_MISMATCH")
    if query.has_extra != candidate.has_extra:
        conflicts.add("EXTRA_VARIANT_MISMATCH")
    if query.has_alternate != candidate.has_alternate:
        conflicts.add("ALTERNATE_VARIANT_MISMATCH")
    if query.languages and candidate.languages and not set(query.languages).intersection(candidate.languages):
        conflicts.add("LANGUAGE_MISMATCH")
    if query.content and candidate.content and not set(query.content).intersection(candidate.content):
        conflicts.add("CONTENT_FAMILY_MISMATCH")
    return tuple(sorted(conflicts))


__all__ = (
    "NameViews",
    "acronym",
    "ascii_fold",
    "character_ngrams",
    "jaccard",
    "lexical_cohort_risk_codes",
    "protected_conflicts",
    "semantics_from_context",
    "words",
)
