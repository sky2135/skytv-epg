"""Stable, reusable variant selection for generated channel icons.

The selector deliberately uses only public channel metadata.  It does not
bind an icon to a provider stream; the private override generator remains
responsible for that exact ``server_id``/``stream_id`` binding.

Variant choices must be stable across Python versions, hash seeds, row order,
and workflow runs.  For that reason, fallback selection uses SHA-256 rather
than Python's process-randomized :func:`hash`.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence


MOVIE_VARIANTS = (
    "movie-clapperboard",
    "movie-reel",
    "movie-projector",
    "movie-filmstrip",
    "movie-ticket",
)

MUSIC_VARIANTS = (
    "music-notes",
    "music-microphone",
    "music-headphones",
    "music-guitar",
    "music-dhol",
    "music-sitar",
)


_TECHNICAL_TOKEN_RE = re.compile(
    r"(?<![\w])(?:"
    r"sd|hd|fhd|uhd|qhd|2k|4k|8k|hevc|x264|x265|h264|h265|"
    r"\d{3,4}[pi]"
    r")(?![\w])",
    flags=re.IGNORECASE,
)
_ALWAYS_ON_TOKEN_RE = re.compile(
    r"(?<![\w])24\s*(?:/|x|×|-)\s*7(?![\w])",
    flags=re.IGNORECASE,
)
_NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)
_EXPLICIT_SERIES_NUMBER_RE = re.compile(
    r"\b(vol(?:ume)?|part|pt|episode|ep|channel|ch)\s*(?:no\s*)?#?\s*\d{1,3}\b",
    flags=re.IGNORECASE,
)
_TRAILING_SERIES_NUMBER_RE = re.compile(r"\b\d{1,3}\s*$")


def _clean_words(value: object) -> str:
    """Return normalized words after dropping display-quality tokens."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("_", " ")
    text = _ALWAYS_ON_TOKEN_RE.sub(" ", text)
    text = _TECHNICAL_TOKEN_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    return " ".join(text.split())


def _canonical_series_marker(match: re.Match[str]) -> str:
    marker = match.group(1).casefold()
    if marker.startswith("vol"):
        marker = "volume"
    elif marker in {"part", "pt"}:
        marker = "part"
    elif marker in {"episode", "ep"}:
        marker = "episode"
    else:
        marker = "channel"
    return f"{marker} #"


def normalize_series_name(channel_name: object) -> str:
    """Normalize a channel name into its stable numbered-series pattern.

    A one-to-three digit series number is replaced, while four-digit years are
    preserved.  Thus ``Movie 1 HD`` and ``Movie 2 4K`` share one pattern, but
    ``Movies 2025`` and ``Movies 2026`` remain distinct.
    """
    normalized = _clean_words(channel_name)
    normalized = _EXPLICIT_SERIES_NUMBER_RE.sub(
        _canonical_series_marker, normalized
    )
    normalized = _TRAILING_SERIES_NUMBER_RE.sub("#", normalized)
    return " ".join(normalized.split())


def series_pattern_key(category_name: object, channel_name: object) -> str:
    """Return the category-scoped key used for deterministic selection."""
    category = _clean_words(category_name)
    channel = normalize_series_name(channel_name)
    return f"{category}|{channel}"


def icon_variant_scope(server_id: object, category_name: object) -> str:
    """Return a normalized private scope for ordered pattern allocation."""
    server = " ".join(
        unicodedata.normalize("NFKC", str(server_id or "")).casefold().split()
    )
    return f"{server}\x1f{_clean_words(category_name)}"


def stable_variant(pattern_key: str, variants: Sequence[str]) -> str:
    """Choose one ordered variant with SHA-256, independent of row order."""
    choices = tuple(variants)
    if not choices:
        raise ValueError("At least one icon variant is required.")
    digest = hashlib.sha256(str(pattern_key).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(choices)
    return choices[index]


def _semantic_text(category_name: object, channel_name: object) -> str:
    return f"{_clean_words(category_name)} {_clean_words(channel_name)}".strip()


def _contains(text: str, *terms: str) -> bool:
    padded = f" {text} "
    return any(f" {term} " in padded for term in terms)


def select_movie_variant(category_name: object, channel_name: object) -> str:
    """Select a transparent movie-symbol family member.

    Clear genre words receive a related symbol.  Other channel families use a
    SHA-256 selection based on their normalized series pattern.
    """
    text = _semantic_text(category_name, channel_name)
    if _contains(text, "box office", "ppv", "premium", "pay per view"):
        return "movie-ticket"
    if _contains(text, "classic", "classics", "retro", "vintage", "oldies", "archive"):
        return "movie-reel"
    if _contains(text, "action", "thriller", "crime", "horror", "war", "western"):
        return "movie-clapperboard"
    if _contains(text, "animation", "animated", "anime", "family", "kids", "comedy"):
        return "movie-filmstrip"
    if _contains(
        text,
        "premiere",
        "premier",
        "cinema",
        "theatre",
        "theater",
        "drama",
        "romance",
    ):
        return "movie-projector"
    return stable_variant(
        series_pattern_key(category_name, channel_name), MOVIE_VARIANTS
    )


def allocate_movie_variants(
    records: Iterable[tuple[str, object, object]],
) -> dict[tuple[str, str], str]:
    """Color ordered movie-name patterns so a pattern boundary changes art.

    Each record is ``(scope, category_name, channel_name)``. Callers keep the
    scope private (normally server plus provider category). Numbered members of
    one normalized pattern share one variant. Different adjacent patterns form
    an edge in a small graph and receive different variants whenever the five
    available movie symbols permit it. A stable hash/semantic choice is used as
    the preferred color, so output is deterministic for the same ordered list.
    """
    sequences: dict[str, list[str]] = defaultdict(list)
    representatives: dict[tuple[str, str], tuple[object, object]] = {}
    for raw_scope, category_name, channel_name in records:
        scope = str(raw_scope)
        pattern = series_pattern_key(category_name, channel_name)
        key = (scope, pattern)
        representatives.setdefault(key, (category_name, channel_name))
        if not sequences[scope] or sequences[scope][-1] != pattern:
            sequences[scope].append(pattern)

    result: dict[tuple[str, str], str] = {}
    for scope, sequence in sequences.items():
        neighbours: dict[str, set[str]] = defaultdict(set)
        for pattern in sequence:
            neighbours.setdefault(pattern, set())
        for left, right in zip(sequence, sequence[1:]):
            if left == right:
                continue
            neighbours[left].add(right)
            neighbours[right].add(left)

        def node_tiebreak(pattern: str) -> tuple[int, bytes, str]:
            digest = hashlib.sha256(f"{scope}\0{pattern}".encode("utf-8")).digest()
            return (-len(neighbours[pattern]), digest, pattern)

        assigned: dict[str, str] = {}
        unassigned = set(neighbours)
        while unassigned:
            # DSATUR chooses the most constrained pattern at every step.  The
            # stable digest makes ties reproducible without relying on set or
            # input iteration order.
            pattern = min(
                unassigned,
                key=lambda candidate: (
                    -len(
                        {
                            assigned[neighbour]
                            for neighbour in neighbours[candidate]
                            if neighbour in assigned
                        }
                    ),
                    *node_tiebreak(candidate),
                ),
            )
            category_name, channel_name = representatives[(scope, pattern)]
            preferred = select_movie_variant(category_name, channel_name)
            first = MOVIE_VARIANTS.index(preferred)
            unavailable = {
                assigned[neighbour]
                for neighbour in neighbours[pattern]
                if neighbour in assigned
            }
            chosen = preferred
            for offset in range(len(MOVIE_VARIANTS)):
                candidate = MOVIE_VARIANTS[(first + offset) % len(MOVIE_VARIANTS)]
                if candidate not in unavailable:
                    chosen = candidate
                    break
            assigned[pattern] = chosen
            result[(scope, pattern)] = chosen
            unassigned.remove(pattern)
    return result


def select_music_variant(
    category_name: object,
    channel_name: object,
    *,
    named_singer: bool = False,
) -> str:
    """Select a transparent music-symbol family member.

    ``named_singer=True`` is an explicit fallback request for a singer whose
    approved portrait is unavailable; it always selects the microphone.
    """
    if named_singer:
        return "music-microphone"

    text = _semantic_text(category_name, channel_name)
    if _contains(text, "punjabi", "bhangra"):
        return "music-dhol"
    if _contains(text, "rock", "metal", "country", "acoustic", "folk", "guitar"):
        return "music-guitar"
    if _contains(
        text,
        "classical",
        "ghazal",
        "qawwali",
        "devotional",
        "bhajan",
        "bhajans",
        "sufi",
        "sitar",
    ):
        return "music-sitar"
    if _contains(
        text,
        "dance",
        "dj",
        "electronic",
        "edm",
        "techno",
        "house",
        "trance",
        "disco",
    ):
        return "music-headphones"
    if _contains(text, "rap", "hip hop", "hiphop", "karaoke", "vocal", "vocals"):
        return "music-microphone"
    # Keep unknown, pop, jazz, soul, reggae, and other broad music channels on
    # the neutral notes symbol.  Randomly assigning a culturally specific
    # instrument would communicate the wrong category.
    return "music-notes"


def select_icon_variant(
    icon_family: str,
    category_name: object,
    channel_name: object,
    *,
    named_singer: bool = False,
) -> str:
    """Select a movie or music variant through one integration entry point."""
    family = str(icon_family or "").strip().casefold()
    if family in {"movie", "movies"}:
        return select_movie_variant(category_name, channel_name)
    if family == "music":
        return select_music_variant(
            category_name,
            channel_name,
            named_singer=named_singer,
        )
    raise ValueError(f"Unsupported icon family: {icon_family!r}")
