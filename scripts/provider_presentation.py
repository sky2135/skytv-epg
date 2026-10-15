"""Conservative identities for non-channel provider presentation rows."""
from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata


BRAND_TITLE = "Welcome to SkyMedia IPTV - 💬 +12368462036"
HEADING_CLASSIFICATION_REASON = "Decorative heading/placeholder is disabled and ignored"
HEADING_LEGACY_CLASSIFICATION_REASON = "Decorative category heading/placeholder has no playable linear schedule"

_DECORATION = "#*=~_─━═┄┅┈┉┌┐└┘╔╗╚╝╭╮╰╯│┃║•▪▫◆◇-–—"
_HEADING_RE = re.compile(
    rf"\A[{re.escape(_DECORATION)}]{{2,}}\s*(?P<label>.*?)\s*"
    rf"[{re.escape(_DECORATION)}]{{2,}}\Z"
)
_NUMERIC_GROUP_RE = re.compile(r"(?<!\w)(?:4k|8k|24\s*/\s*7)(?!\w)")
_WORDS_RE = re.compile(r"24\s*/\s*7|(?<!\w)(?:4k|8k)(?!\w)|[^\W\d_]+", re.UNICODE)
_ADULT_RE = re.compile(r"\b(?:adult|adults|xxx|porn|porno|erotic|erotica)\b|18\s*\+", re.I)
_MACHINE_HEADING_NOTE_RE = re.compile(r"\Aauto-map-v1 method=heading_placeholder; ")
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off", "disabled"})
_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on", "enabled"})


def _normalized(value: object, maximum: int) -> str | None:
    text = str(value or "")
    if not text or len(text) > maximum:
        return None
    if any(unicodedata.category(character).startswith("C") for character in text):
        return None
    text = unicodedata.normalize("NFKC", text)
    if len(text) > maximum or any(
        unicodedata.category(character).startswith("C") for character in text
    ):
        return None
    return re.sub(r"\s+", " ", text.strip()).casefold()


def heading_identity(
    name: object,
    category: object,
    *,
    require_category_corroboration: bool = True,
) -> str | None:
    """Identify an anchored decorated heading corroborated by its category.

    Decoration is presentation only. Its inner words must all occur in the
    category unless a caller has already verified machine heading provenance.
    Only the exact numeric group labels 4K, 8K and 24/7 are accepted;
    numbered event banks, dates, URLs, markup and controls cannot become a
    heading. This function never approves a mapping.
    """

    normalized_name = _normalized(name, 300)
    normalized_category = _normalized(category, 200)
    if not normalized_name or not normalized_category:
        return None
    if _ADULT_RE.search(f"{normalized_name} {normalized_category}"):
        return None
    matched = _HEADING_RE.fullmatch(normalized_name)
    if matched is None:
        return None
    label = matched.group("label").strip()
    without_numeric_groups = _NUMERIC_GROUP_RE.sub("", label)
    if not 2 <= len(label) <= 100 or any(character.isdigit() for character in without_numeric_groups):
        return None
    if any(
        not (character.isalnum() or character.isspace() or character in "&+|/")
        for character in label
    ):
        return None
    words = tuple(re.sub(r"\s+", "", word) for word in _WORDS_RE.findall(label))
    category_words = frozenset(re.sub(r"\s+", "", word) for word in _WORDS_RE.findall(normalized_category))
    if not words or (
        require_category_corroboration and not set(words).issubset(category_words)
    ):
        return None
    return " ".join(words)


def is_same_group_heading(
    old_name: object,
    new_name: object,
    old_category: object,
    new_category: object,
    *,
    require_category_corroboration: bool = True,
) -> bool:
    """Accept decoration/font changes only within the exact same category."""

    category_key = _normalized(old_category, 200)
    if not category_key or category_key != _normalized(new_category, 200):
        return False
    old_identity = heading_identity(
        old_name, old_category,
        require_category_corroboration=require_category_corroboration,
    )
    return old_identity is not None and old_identity == heading_identity(
        new_name, new_category,
        require_category_corroboration=require_category_corroboration,
    )


def is_machine_group_heading(row: Mapping[str, object]) -> bool:
    """Recognize current disabled or legacy active machine heading rows."""

    enabled = row.get("enabled")
    explicitly_disabled = enabled is False or (
        isinstance(enabled, str) and enabled.strip().casefold() in _FALSE_VALUES
    )
    explicitly_enabled = enabled is True or (
        isinstance(enabled, str) and enabled.strip().casefold() in _TRUE_VALUES
    )
    action = str(row.get("action", "")).strip().upper()
    classified_state = (
        action == "IGNORE" and explicitly_disabled
        and row.get("reason") == HEADING_CLASSIFICATION_REASON
    ) or (
        action == "AUTO_DUMMY" and explicitly_enabled
        and row.get("reason") in {
            HEADING_CLASSIFICATION_REASON, HEADING_LEGACY_CLASSIFICATION_REASON,
        }
    )
    notes = str(row.get("notes", ""))
    machine_provenance = bool(_MACHINE_HEADING_NOTE_RE.match(notes)) or (
        action == "AUTO_DUMMY"
        and explicitly_enabled
        and row.get("reason") == HEADING_LEGACY_CLASSIFICATION_REASON
        and not notes.strip()
    )
    return bool(
        classified_state
        and str(row.get("source", "")).strip().casefold() == "dummy"
        and str(row.get("epg_feed", "")).strip().upper() == "DUMMY_CHANNELS"
        and str(row.get("epg_id", "")).strip().casefold() == "blank.dummy.us"
        and str(row.get("genre", "")).strip().casefold() != "adult"
        and str(row.get("content_rating", "")).strip().casefold() != "adult"
        and machine_provenance
        and heading_identity(
            row.get("channel_name"), row.get("category_name"),
            require_category_corroboration=False,
        )
    )


__all__ = [
    "BRAND_TITLE", "HEADING_CLASSIFICATION_REASON", "HEADING_LEGACY_CLASSIFICATION_REASON", "heading_identity",
    "is_same_group_heading", "is_machine_group_heading",
]
