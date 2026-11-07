"""Reviewed same-feed EPGShare connections for specific linear channels.

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
    country: str = "GB"
    languages: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.identity

    @property
    def family(self) -> str:
        return self.identity.removeprefix("sky_sports_").removesuffix("_uk")

    def feed_for_id(self, epg_id: str) -> str:
        """Retain the observed source feed when a reviewed alternate is used."""
        if epg_id in self.candidate_ids and epg_id in _SONY_IN4_IDS:
            return "IN4"
        return self.epg_feed


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

# Sony's current India lineup retains the numbered TEN identities. Provider
# labels commonly omit TEN or keep the older Sony Ten spelling. Language is
# part of the identity: 3 is Hindi, and 4 must explicitly say Tamil or Telugu.
# Sony ESPN is not a numbered TEN alias. IN1 candidates precede the separately
# observed IN4 spellings; no fallback crosses a language edition.
_SONY_CONNECTIONS = {
    ("1", "en"): ReviewedGuideConnection(
        "sony_sports_ten_1_in", "Sony Sports Ten 1",
        ("Sony.Ten.1.HD.in", "Sony.Ten.1.in", "SONY.SPORTS.TEN.1.HD.in", "SONY.SPORTS.TEN.1.in"),
        epg_feed="IN1", country="IN", languages=("en",),
    ),
    ("2", "en"): ReviewedGuideConnection(
        "sony_sports_ten_2_in", "Sony Sports Ten 2",
        ("Sony.Ten.2.HD.in", "SONY.SPORTS.TEN.2.HD.in", "SONY.SPORTS.TEN.2.in"),
        epg_feed="IN1", country="IN", languages=("en",),
    ),
    ("3", "hi"): ReviewedGuideConnection(
        "sony_sports_ten_3_hindi_in", "Sony Sports Ten 3 Hindi",
        ("Sony.Ten.3.HD.Hindi.in", "Sony.Ten.3.Hindi.in", "SONY.SPORTS.TEN.3.HD.in", "SONY.SPORTS.TEN.3.in"),
        epg_feed="IN1", country="IN", languages=("hi",),
    ),
    ("4", "ta"): ReviewedGuideConnection(
        "sony_sports_ten_4_tamil_in", "Sony Sports Ten 4 Tamil",
        ("Sony.Ten.4.HD.Tamil.in", "Sony.Ten.4.Tamil.in", "SONY.SPORTS.TEN.4.TAMIL.in"),
        epg_feed="IN1", country="IN", languages=("ta",),
    ),
    ("4", "te"): ReviewedGuideConnection(
        "sony_sports_ten_4_telugu_in", "Sony Sports Ten 4 Telugu",
        ("Sony.Ten.4.HD.Telugu.in", "Sony.Ten.4.Telugu.in"),
        epg_feed="IN1", country="IN", languages=("te",),
    ),
    ("5", "en"): ReviewedGuideConnection(
        "sony_sports_ten_5_in", "Sony Sports Ten 5",
        ("Sony.Ten.5.HD.in", "Sony.Ten.5.in", "SONY.SPORTS.TEN.5.HD.in", "SONY.SPORTS.TEN.5.in"),
        epg_feed="IN1", country="IN", languages=("en",),
    ),
}
_SONY_IN4_IDS = frozenset(
    epg_id for connection in _SONY_CONNECTIONS.values()
    for epg_id in connection.candidate_ids if epg_id.startswith("SONY.SPORTS.TEN.")
)
_HUB_SPORTS_4 = ReviewedGuideConnection(
    "hub_sports_4_sg", "Hub Sports 4", ("Hub.Sports.4.HD.sg",),
    epg_feed="SG1", country="SG",
)

_QUALITY = r"(?:sd|hd|fhd|uhd|4k|2160p|1080p|720p)"
_SONY_PREFIX = re.compile(r"\A(?:in|india|cric|cri)\s*(?:\|+|:|-)\s*", re.I)
_SONY_PLAIN = re.compile(r"\Asony\s*(?:sports(?:\s*ten)?|ten)\s*(?P<number>[1-5])(?P<suffix>.*)\Z", re.I)
_SONY_LANGUAGE_TOKENS = {
    "english": "en", "eng": "en", "en": "en",
    "hindi": "hi", "hin": "hi", "hi": "hi",
    "tamil": "ta", "tam": "ta", "ta": "ta",
    "telugu": "te", "tel": "te", "te": "te",
}
_SONY_QUALIFIER_TOKENS = frozenset({"sd", "hd", "fhd", "uhd", "4k", "2160p", "1080p", "720p", "hevc"})
_SONY_FOREIGN_CATEGORY = re.compile(
    r"(?:\A|[^a-z])(?:uk|gb|british|united kingdom|de|germany|german|it|italy|italian|"
    r"ie|ireland|irish|us|usa|united states|ca|canada|au|australia|nz|new zealand|"
    r"pk|pakistan|bd|bangladesh|lk|sri lanka|sg|singapore|my|malaysia|id|indonesia|"
    r"jp|japan|cn|china|hk|hong kong|kr|korea|za|south africa|fr|france|es|spain|pt|portugal)(?:\Z|[^a-z])", re.I,
)
_HUB_PREFIX = re.compile(
    rf"\A(?:sg|sgp|singapore|cric|cri)(?:\s+{_QUALITY})?\s*(?:\|+|:|-)\s*", re.I,
)
_HUB_PLAIN = re.compile(r"\Ahub\s*sports\s*4(?P<suffix>.*)\Z", re.I)
_HUB_FOREIGN_CATEGORY = re.compile(
    r"(?:\A|[^a-z])(?:uk|gb|british|united kingdom|de|germany|german|it|italy|italian|"
    r"ie|ireland|irish|us|usa|united states|ca|canada|au|australia|nz|new zealand|"
    r"in|india|pk|pakistan|bd|bangladesh|lk|sri lanka|my|malaysia|id|indonesia|"
    r"jp|japan|cn|china|hk|hong kong|kr|korea|za|south africa|fr|france|es|spain|pt|portugal)(?:\Z|[^a-z])", re.I,
)
_UK_PREFIX = re.compile(
    rf"\A(?:uk|gb)(?:\s+{_QUALITY})?\s*(?:\|+|:|-)\s*", re.I,
)
_UK_CATEGORY = re.compile(r"(?:\A|[^a-z])(?:uk|gb|united kingdom)(?:\Z|[^a-z])", re.I)
_FOREIGN_CATEGORY = re.compile(
    r"(?:\A|[^a-z])(?:de|germany|german|it|italy|italian|ie|ireland|irish|"
    r"us|usa|united states|ca|canada|au|australia|nz|new zealand|"
    r"in|india|pk|pakistan|sg|singapore|fr|france|es|spain|pt|portugal)(?:\Z|[^a-z])", re.I,
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


def _reviewed_sony_connection(
    text: str, category: object, country_set: set[str],
) -> ReviewedGuideConnection | None:
    category_text = _text(category, 200)
    if isinstance(category, str) and category and not category_text:
        return None
    if _SONY_FOREIGN_CATEGORY.search(category_text):
        return None
    # The named TEN lineup is specifically Indian, so an absent/unknown
    # market is allowed. Positive evidence of another market still vetoes it.
    known_countries = country_set - {"UNKNOWN", "ZZ", "XX"}
    if known_countries - {"IN"}:
        return None
    for _ in range(2):
        prefix = _SONY_PREFIX.match(text)
        if prefix is None:
            break
        text = text[prefix.end():]
    text = re.sub(r"\s*\[(?:IN|INDIA)\]\Z", "", text, flags=re.I)
    matched = _SONY_PLAIN.fullmatch(text)
    if matched is None:
        return None
    suffix = matched.group("suffix").strip().casefold()
    # Balanced presentation brackets are allowed around known language or
    # quality words only. Timeshifts, backup feeds and extra numerals survive
    # tokenization and therefore fail the exact qualifier allowlist below.
    suffix = re.sub(r"\(([^()]*)\)|\[([^\[\]]*)\]", lambda m: " " + (m[1] or m[2] or "") + " ", suffix)
    tokens = suffix.split()
    if any(token not in _SONY_QUALIFIER_TOKENS and token not in _SONY_LANGUAGE_TOKENS for token in tokens):
        return None
    languages = {_SONY_LANGUAGE_TOKENS[token] for token in tokens if token in _SONY_LANGUAGE_TOKENS}
    if len(languages) > 1:
        return None
    number = matched.group("number")
    language = next(iter(languages), "hi" if number == "3" else "en")
    if number == "4" and not languages:
        return None
    return _SONY_CONNECTIONS.get((number, language))


def _reviewed_hub_connection(
    text: str, category: object, country_set: set[str],
) -> ReviewedGuideConnection | None:
    category_text = _text(category, 200)
    if isinstance(category, str) and category and not category_text:
        return None
    if _HUB_FOREIGN_CATEGORY.search(category_text):
        return None
    if (country_set - {"UNKNOWN", "ZZ", "XX"}) - {"SG"}:
        return None
    for _ in range(2):
        prefix = _HUB_PREFIX.match(text)
        if prefix is None:
            break
        text = text[prefix.end():]
    text = re.sub(r"\s*\[(?:SG|SINGAPORE)\]\Z", "", text, flags=re.I)
    matched = _HUB_PLAIN.fullmatch(text)
    if matched is None:
        return None
    suffix = re.sub(r"\(([^()]*)\)", r" \1 ", matched.group("suffix").casefold()).strip()
    if any(token not in _SONY_QUALIFIER_TOKENS for token in suffix.split()):
        return None
    return _HUB_SPORTS_4


def reviewed_connection(
    name: object,
    category: object = "",
    countries: Iterable[str] = (),
) -> ReviewedGuideConnection | None:
    """Return a reviewed linear identity, without selecting any schedule.

    An explicit UK prefix or UK category is required unless country metadata
    independently binds a plain channel name to GB. Foreign country metadata
    vetoes a match. Technical quality suffixes do not change a linear feed;
    timeshift, regional, numbered, event and alternate-feed suffixes do. The
    explicitly reviewed Sony TEN lineup is Indian and Hub Sports 4 is from
    Singapore even when country metadata is absent. Numbered and language
    editions remain separate; programme titles do not establish audio language.
    """
    text = _text(name, 300)
    if not text:
        return None
    country_set = {str(country).strip().upper() for country in countries if country}
    sony = _reviewed_sony_connection(text, category, country_set)
    if sony is not None:
        return sony
    hub = _reviewed_hub_connection(text, category, country_set)
    if hub is not None:
        return hub
    if _FOREIGN_CATEGORY.search(_text(category, 200)):
        return None
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

    Market-specific identities (including Sony India) are considered first.
    A raw Sheet row can also lack GB metadata that the builder later obtains
    from its public registry. Reserve exact reviewed Sky Sports candidates in
    that case as a selection-only superset, never as activation authority.
    Existing foreign-country/category evidence and decision gates still veto.
    """
    wanted: set[str] = set()
    for row in rows:
        if not _row_allowed(row, allow_review=allow_review):
            continue
        countries = tuple(_row_countries(row))
        connection = reviewed_connection(
            _value(row, "channel_name"), _value(row, "category_name"),
            countries,
        )
        if connection is None and not countries:
            connection = reviewed_connection(
                _value(row, "channel_name"), _value(row, "category_name"), ("GB",),
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
