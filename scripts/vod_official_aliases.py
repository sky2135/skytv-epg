"""Exact display aliases from complete, bounded official TMDB translations.

This module supplies identity evidence, not a match decision. Callers still
check media kind, years, competing candidates, rejected IDs and any independent
poster/external-ID proof before accepting or caching a work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import re
import unicodedata


MAX_TRANSLATIONS = 512
MAX_TITLE_CHARS = 300
MAX_SELECTED_ALIASES = 8


@dataclass(frozen=True)
class OfficialAliasEvidence:
    complete: bool
    aliases: tuple[str, ...] = ()
    display_key: str = ""
    reason: str = "invalid_translations"

    @property
    def matched(self) -> bool:
        return self.complete and bool(self.aliases)


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if re.fullmatch(r"[1-9][0-9]{0,8}", text) else None


def _title(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_TITLE_CHARS or "://" in value:
        return None
    if any(unicodedata.category(character) == "Cc" and not character.isspace() for character in value):
        return None
    return " ".join(unicodedata.normalize("NFC", value).split())


def exact_display_key(value: object) -> str:
    """Normalize punctuation/case while retaining meaningful script marks.

    Do not use the broad search signature here: removing Devanagari/Punjabi
    vowel marks could make two different official titles compare as equal.
    """
    title = _title(value)
    if title is None:
        return ""
    rendered = []
    for character in title.casefold():
        category = unicodedata.category(character)
        if category[0] in {"L", "N", "M"}:
            rendered.append(character)
        elif category == "Cf":
            # Joiner/directional formatting does not introduce title words.
            continue
        else:
            rendered.append(" ")
    return " ".join("".join(rendered).split())


def translation_aliases(
    details: Mapping[str, Any], kind: str, display_title: object,
) -> OfficialAliasEvidence:
    """Find literal official translated names equal to this display title.

    The whole translations list is validated even after a match is found.
    Selected names are bounded for the existing cache proof envelope, while
    every official row contributes to the complete/matched decision.
    """
    if kind not in {"movie", "series"}:
        raise ValueError("Official aliases require a movie or series kind")
    display_key = exact_display_key(display_title)
    if not display_key:
        return OfficialAliasEvidence(False, reason="invalid_display_title")
    if not isinstance(details, Mapping):
        return OfficialAliasEvidence(False, display_key=display_key)
    resource = details.get("translations")
    if resource is None:
        return OfficialAliasEvidence(False, display_key=display_key, reason="translations_missing")
    if not isinstance(resource, Mapping):
        return OfficialAliasEvidence(False, display_key=display_key)
    identifier = _identifier(details.get("id"))
    # Appended resources omit the redundant ID in real TMDB responses. Their
    # identity is inherited from the validated outer detail response. An
    # explicit nested ID must still be valid and agree with that outer ID.
    if identifier is None or ("id" in resource and _identifier(resource["id"]) != identifier):
        return OfficialAliasEvidence(False, display_key=display_key)
    rows = resource.get("translations")
    if not isinstance(rows, list) or len(rows) > MAX_TRANSLATIONS:
        return OfficialAliasEvidence(False, display_key=display_key)
    field = "title" if kind == "movie" else "name"
    matched = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("data"), Mapping):
            return OfficialAliasEvidence(False, display_key=display_key)
        value = row["data"].get(field)
        if value is None:
            continue
        title = _title(value)
        if title is None:
            return OfficialAliasEvidence(False, display_key=display_key)
        if title and exact_display_key(title) == display_key:
            matched.add(title)
    aliases = tuple(sorted(matched)[:MAX_SELECTED_ALIASES])
    return OfficialAliasEvidence(
        True, aliases=aliases, display_key=display_key,
        reason="matched_translation" if aliases else "no_translation_match",
    )
