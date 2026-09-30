#!/usr/bin/env python3
"""Exact identities for provider slots whose event payload intentionally rotates.

The stream ID and numbered slot are durable; the text after the delimiter is
the current event.  These rules are deliberately anchored and are used only
when the provider category is unchanged, so an ordinary channel rename still
retains the stream-reuse quarantine.
"""
from __future__ import annotations

import re
import unicodedata


_SPACE_RE = re.compile(r"\s+")
_EVENT_CATEGORY_RE = re.compile(
    r"\b(?:PPV|LIVE\s+EVENTS?|EVENTS?)\b", re.IGNORECASE
)


def exact_text_key(value: object) -> str:
    """Normalize only Unicode, case, and whitespace for exact comparisons."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return _SPACE_RE.sub(" ", text.strip()).casefold()


_EVENT_SLOT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "us_espn_plus",
        re.compile(
            r"^US\s*\(\s*ESPN\+\s*(?P<slot>[0-9]{3})\s*\)\s*\|(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "us_espn_numbered",
        re.compile(
            r"^US\s*\(\s*ESPN\s*(?P<slot>[0-9]{3})\s*\)\s*\|(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "flosports",
        re.compile(
            r"^\(\s*FLSP\s+(?P<slot>[0-9]{3,4})\s*\)\s*\|(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "us_flo",
        re.compile(
            r"^US\s*\(\s*FLO\s+(?P<slot>[0-9]{1,4})\s*\)\s*\|(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "usa_flo",
        re.compile(
            r"^USA\s*-\s*FLO\s+(?P<slot>[0-9]{1,4})\s*:(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "ppv_event",
        re.compile(
            r"^PPV\s+EVENT\s+(?P<slot>[0-9]{2})\s*:(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "live_event",
        re.compile(
            r"^LIVE\s+EVENT\s+(?P<slot>[0-9]{2})\s*-(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "league_event",
        re.compile(
            r"^(?P<brand>NFL|NBA|WNBA|NHL|MLB|MLS|UEFA|CFL|CHL|UFC|WWE)"
            r"\s*\|\s*(?P<slot>[0-9]{2,3})\s*-(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "dazn_ppv",
        re.compile(
            r"^(?P<market>[A-Z]{2,3})\s*:\s*DAZN\+?\s+PPV\s+"
            r"(?P<slot>[0-9]{1,3})\s*-(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "netflix_ppv",
        re.compile(
            r"^(?P<market>[A-Z]{2,3})\s*:\s*NETFLIX\s+PPV\s+"
            r"(?P<slot>[0-9]{1,3})\s*-(?:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "btn_plus",
        re.compile(
            r"^BTN\+\s*(?P<slot>[0-9]{1,3})\s+HD\s*\(D\)"
            r"(?:\s*:\s*.*)?$",
            re.IGNORECASE,
        ),
    ),
)


def event_slot_identity(
    value: object, category: object = ""
) -> tuple[str, str] | None:
    """Return ``(family, slot)`` for one complete, supported slot name."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    for family, pattern in _EVENT_SLOT_PATTERNS:
        matched = pattern.fullmatch(text)
        if matched is None:
            continue
        # Broad commercial slot shapes are accepted only inside an explicit
        # event/PPV category.  The narrower ESPN+/Flo/league grammars are
        # self-identifying but still require an unchanged category at the
        # caller boundary.
        if family in {"dazn_ppv", "netflix_ppv", "btn_plus"} and not (
            _EVENT_CATEGORY_RE.search(str(category or ""))
        ):
            return None
        qualifiers = [
            matched.groupdict().get("market", ""),
            matched.groupdict().get("brand", ""),
            matched.group("slot"),
        ]
        slot_key = ":".join(
            exact_text_key(value) for value in qualifiers if exact_text_key(value)
        )
        return family, slot_key
    return None


def is_same_event_slot(
    old_name: object,
    new_name: object,
    old_category: object,
    new_category: object,
) -> bool:
    """Whether a change is only the rotating payload of one proven slot."""

    category_key = exact_text_key(old_category)
    if not category_key or category_key != exact_text_key(new_category):
        return False
    old_identity = event_slot_identity(old_name, old_category)
    return old_identity is not None and old_identity == event_slot_identity(
        new_name, new_category
    )


__all__ = ["event_slot_identity", "exact_text_key", "is_same_event_slot"]
