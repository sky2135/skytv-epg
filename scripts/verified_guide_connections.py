"""Reviewed same-feed EPGShare connections for the UK Sky Sports lineup.

Names identify a reviewed channel family; they never prove that a schedule is
current. Callers must select from these exact IDs only after the ordinary
programme-usability gate has passed. Provider playlist IDs are output aliases,
not EPGShare schedule IDs, and are deliberately absent from this module.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from collections.abc import Collection, Iterable, Mapping


@dataclass(frozen=True)
class ReviewedGuideConnection:
    identity: str
    canonical_name: str
    candidate_ids: tuple[str, ...]
    epg_feed: str = "UK1"
    evidence: str = "reviewed_same_country_linear_channel_identity"

    @property
    def key(self) -> str:
        return self.identity

    @property
    def family(self) -> str:
        return self.identity.removeprefix("sky_sports_").removesuffix("_uk")


# Exact identities observed in the EPGShare UK1 channel catalog. This is an
# ordered list of same-feed alternatives, never a general fuzzy-name alias.
# In particular Action does not fall back to NFL, and Plus does not fall back
# to 4seven, Mix, a numbered event stream or the old Sky Sports Arena identity.
_CONNECTIONS = {
    "news": ReviewedGuideConnection(
        "sky_sports_news_uk", "Sky Sports News", ("SkySp.News.HD.uk",),
    ),
    "main event": ReviewedGuideConnection(
        "sky_sports_main_event_uk", "Sky Sports Main Event", ("SkySpMainEvHD.uk",),
    ),
    "premier league": ReviewedGuideConnection(
        "sky_sports_premier_league_uk", "Sky Sports Premier League", ("SkySp.PL.HD.uk",),
    ),
    "football": ReviewedGuideConnection(
        "sky_sports_football_uk", "Sky Sports Football",
        ("Sky.Sports.Football.HD.uk", "SkySp.Fball.HD.uk", "SkySp.Fball.uk"),
    ),
    "cricket": ReviewedGuideConnection(
        "sky_sports_cricket_uk", "Sky Sports Cricket",
        ("SkySpCricket.HD.uk", "SkySp.Cricket.uk"),
    ),
    "action": ReviewedGuideConnection(
        "sky_sports_action_uk", "Sky Sports Action",
        ("SkySp.ActionHD.uk", "SkySp.Action.uk"),
    ),
    "plus": ReviewedGuideConnection(
        "sky_sports_plus_uk", "Sky Sports+", ("SkySp+HD.uk", "SkySp+.uk"),
    ),
    "racing": ReviewedGuideConnection(
        "sky_sports_racing_uk", "Sky Sports Racing",
        ("SkySp.Racing.HD.uk", "SkySp.Racing.uk"),
    ),
    "f1": ReviewedGuideConnection(
        "sky_sports_f1_uk", "Sky Sports F1", ("SkySp.F1.HD.uk", "SkySp.F1.uk"),
    ),
}

_QUALITY = r"(?:sd|hd|fhd|uhd|4k|2160p|1080p|720p)"
_UK_PREFIX = re.compile(
    rf"\A(?:uk|gb)(?:\s+{_QUALITY})?\s*(?:\|+|:|-)\s*", re.I,
)
_UK_CATEGORY = re.compile(r"(?:\A|[^a-z])(?:uk|gb|united kingdom)(?:\Z|[^a-z])", re.I)
_FOREIGN_CATEGORY = re.compile(
    r"(?:\A|[^a-z])(?:de|germany|german|it|italy|italian|ie|ireland|irish|"
    r"us|usa|united states|ca|canada|au|australia|nz|new zealand|"
    r"in|india|pk|pakistan|fr|france|es|spain|pt|portugal)(?:\Z|[^a-z])", re.I,
)
_PLAIN = re.compile(
    rf"\Asky\s+sports\s*(?P<family>news|main\s+event|premier\s+league|"
    rf"football|cricket|action|plus|\+|racing|f1)"
    rf"(?P<quality>(?:\s+(?:{_QUALITY}|hevc)|\s*\({_QUALITY}\))*)\Z", re.I,
)
_EXPLICIT_REJECTIONS = frozenset({"SKIP", "IGNORE", "REJECTED"})
_REVIEW_ACTIONS = frozenset({"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED"})
_ACTIVE_ACTIONS = frozenset({"KEEP_PANEL", "AUTO_EPGSHARE", "AUTO_DUMMY", "APPROVED"})
_PROTECTED_REVIEW_NOTE = re.compile(
    r"\b(?:manual|human|operator)\s+(?:review|decision|mapping|selection|override|reuse)\b|"
    r"\b(?:do\s+not|never)\s+(?:auto(?:matically)?[- ]?(?:match|map|change|replace|update)|remap|replace|change|override)\b|"
    r"\b(?:stream[- ]?id\s+reuse|reused\s+stream[- ]?id|possible\s+(?:stream[- ]?id\s+)?reuse)\b|"
    r"\b(?:provider\s+)?identity\s+(?:mismatch|review|change(?:d)?)\b|"
    r"\bschema\s+review\b|\bopen\s+(?:sync\s+)?alerts?\b|"
    r"\b(?:mapping|guide|schedule)\s+(?:is\s+)?locked\b|\blocked\s+(?:mapping|guide|schedule)\b",
    re.I,
)


def _value(row: object, key: str, default: object = "") -> object:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _enabled(row: object) -> bool:
    value = _value(row, "enabled", False)
    return value is True or (
        isinstance(value, str) and value.strip().casefold() in {"1", "true", "yes", "y", "on", "enabled"}
    )


def _row_countries(row: object) -> Iterable[str]:
    countries = _value(_value(row, "metadata", None), "countries", ())
    if not countries:
        countries = _value(row, "country_codes", _value(row, "countries", ()))
    if isinstance(countries, str):
        return [country for country in re.split(r"[|,;\s]+", countries) if country]
    return countries


def _row_allowed(row: object, *, allow_review: bool) -> bool:
    action = str(_value(row, "action")).strip().upper()
    reason = str(_value(row, "reason")).strip()
    if action in _EXPLICIT_REJECTIONS or action == "MANUAL":
        return False
    if reason.casefold().startswith("effective snapshot quarantine:"):
        return False
    if _PROTECTED_REVIEW_NOTE.search(f"{reason} {_value(row, 'notes')}"):
        return False
    if action in _REVIEW_ACTIONS:
        return allow_review
    return action in _ACTIVE_ACTIONS and _enabled(row)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return ""
    if any(unicodedata.category(char).startswith("C") for char in value):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def reviewed_connection(
    name: object,
    category: object = "",
    countries: Iterable[str] = (),
) -> ReviewedGuideConnection | None:
    """Return a reviewed UK linear identity, without selecting any schedule.

    An explicit UK prefix or UK category is required unless country metadata
    independently binds a plain channel name to GB. Foreign country metadata
    vetoes a match. Technical quality suffixes do not change a linear feed;
    timeshift, regional, numbered, event and alternate-feed suffixes do.
    """
    text = _text(name, 300)
    if not text:
        return None
    if _FOREIGN_CATEGORY.search(_text(category, 200)):
        return None
    country_set = {str(country).strip().upper() for country in countries if country}
    if country_set - {"GB", "UK"}:
        return None
    match = _UK_PREFIX.match(text)
    has_uk_prefix = match is not None
    if match:
        text = text[match.end():]
    # A trailing country marker is accepted only as the complete final suffix.
    has_uk_suffix = bool(re.search(r"\s*\[(?:UK|GB)\]\Z", text, re.I))
    if has_uk_suffix:
        text = re.sub(r"\s*\[(?:UK|GB)\]\Z", "", text, flags=re.I)
    if not (has_uk_prefix or has_uk_suffix or country_set or _UK_CATEGORY.search(_text(category, 200))):
        return None
    matched = _PLAIN.fullmatch(text)
    if not matched:
        return None
    family = re.sub(r"\s+", " ", matched.group("family")).casefold()
    return _CONNECTIONS.get("plus" if family == "+" else family)


def connection_for_row(
    row: object, *, allow_review: bool = False,
) -> ReviewedGuideConnection | None:
    """Respect decisions/quarantine before considering a reviewed connection.

    ``allow_review`` is for a caller that has independently bound an unresolved
    row to its current same-server provider stream. This function does not
    perform that binding. Explicit/manual decisions and effective snapshot
    quarantines are never overridden by this helper.
    """
    if not _row_allowed(row, allow_review=allow_review):
        return None
    return reviewed_connection(
        _value(row, "channel_name"),
        _value(row, "category_name"),
        _row_countries(row),
    )


def wanted_connection_ids(
    rows: Iterable[object], *, allow_review: bool = False,
) -> set[str]:
    """Reserve reviewed candidates for the existing one-pass source reader."""
    return {
        epg_id
        for row in rows
        if (connection := connection_for_row(row, allow_review=allow_review)) is not None
        for epg_id in connection.candidate_ids
    }


def selection_connection_ids(
    rows: Iterable[object], *, allow_review: bool = False,
) -> set[str]:
    """Reserve a superset before registry enrichment, without approving a row.

    A raw Sheet row can lack GB metadata that the builder later obtains from
    its public registry. Reserve IDs for an exact reviewed Sky Sports spelling
    in that case as well. This selection-only UK assumption is never returned
    as a binding and must not be used for activation. Existing foreign-country
    or foreign-market evidence, exclusions and quarantines still veto it.
    """
    wanted: set[str] = set()
    for row in rows:
        if not _row_allowed(row, allow_review=allow_review):
            continue
        countries = tuple(_row_countries(row))
        connection = reviewed_connection(
            _value(row, "channel_name"), _value(row, "category_name"),
            countries or ("GB",),
        )
        if connection is not None:
            wanted.update(connection.candidate_ids)
    return wanted


def select_usable_connection(
    connection: ReviewedGuideConnection,
    usable_ids: Collection[str],
) -> str | None:
    """Choose only an exact candidate that passed the caller's programme gate."""
    return next((epg_id for epg_id in connection.candidate_ids if epg_id in usable_ids), None)
