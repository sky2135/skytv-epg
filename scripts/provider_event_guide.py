"""Decode bounded provider event labels without inventing a game or duration.

Dates describe a provider-announced schedule, not verified live stream status.
The caller owns pre-event/live-window/post-event presentation and real-guide
priority. Uncertain clocks never receive an epoch that could become "Live".
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


MAX_EVENT_LABEL_CHARACTERS = 2_048
YEARLESS_NEARBY_DAYS = 31
MAX_EXPLICIT_DATE_DISTANCE_DAYS = 550
MAX_EXPLICIT_EVENT_SECONDS = 48 * 3600
_EASTERN = ZoneInfo("America/New_York")
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_ZONE = r"(?P<zone>Z|UTC|GMT|EST|EDT|ET|[+-]\d{2}:?\d{2})(?![\w:+-])"
_OPTIONAL_ZONE = rf"(?:\s*{_ZONE})?(?![A-Za-z0-9:])"
_CLOCK = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*(?P<meridiem>AM|PM)?"
_CLOCK_12 = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>AM|PM)"
_ISO = re.compile(
    rf"(?<!\d)(?P<year>\d{{4}})-(?P<month>\d{{1,2}})-(?P<day>\d{{1,2}})[ T]{_CLOCK}{_OPTIONAL_ZONE}", re.I
)
_SHORT = re.compile(
    rf"\(\s*(?P<month>\d{{1,2}})[./](?P<day>\d{{1,2}})(?:[./](?P<year>\d{{4}}))?\s+{_CLOCK_12}{_OPTIONAL_ZONE}\s*\)", re.I
)
_MONTH_NAME = re.compile(
    rf"\b(?:(?P<prefix_year>\d{{4}})\s+)?(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\.?\s+)?"
    rf"(?P<month_name>{_MONTH_PATTERN})\.?\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*,?\s+(?P<year>\d{{4}}))?\s*,?\s+{_CLOCK_12}{_OPTIONAL_ZONE}"
    rf"(?:\s*,?\s+(?P<trailing_year>\d{{4}})(?!\d))?", re.I
)
_END_CLOCK = re.compile(
    rf"^\s*(?:[-–—]|to|until|->)\s*{_CLOCK}{_OPTIONAL_ZONE}(?=$|\s|[)\]])", re.I
)
_END_CLOCK_12 = re.compile(
    rf"^\s*(?:[-–—]|to|until|->)\s*{_CLOCK_12}{_OPTIONAL_ZONE}(?=$|\s|[)\]])", re.I
)
_RANGE_SEPARATOR = re.compile(r"^\s*[)\]]?\s*(?:[-–—]|to|until|->)\s*[(\[]?\s*$", re.I)
_UNSUPPORTED_ZONE = re.compile(r"^\s*(?:PST|PDT|MST|MDT|CST|CDT|PT|MT|CT|CET|CEST|BST|IST|AEST|AEDT|(?:ET|EST|EDT|GMT|UTC)[A-Z0-9_:+-]+)(?!\w)", re.I)
_COUNTRY = r"US|USA|UK|GB|CA|CAN|CANADA|IN|INDIA|FR|DE|ES|NL|BR|LAT|PK|AU|NZ|UAE"
_PREFIXES = (
    re.compile(r"^\(\s*(?:US|AU)\s*\)\s+ESPN\s+PLAY\s*(?!0+(?=\s*(?:[|:–—-]|$)))[0-9]{1,3}(?=\s*(?:[|:–—-]|$))\s*(?:[|:–—-]\s*)?", re.I),
    re.compile(r"^(?:US|USA)?\s*\(\s*ESPN\s*(?:\+|PLAY)?\s*\d{1,4}\s*\)\s*[|:\-–—]*\s*", re.I),
    re.compile(r"^\(\s*(?:FLSP|FLO)\s+\d{1,4}\s*\)\s*[|:\-–—]*\s*", re.I),
    re.compile(rf"^(?:\[\s*(?:{_COUNTRY})\s*\]\s*|(?:{_COUNTRY})\s*(?:[|:]|\s[-–—])\s*)", re.I),
    re.compile(r"^(?:(?:US|USA)\s+)?ESPN\s*(?:\+|PLAY)?\s*\d{1,4}\s*[|:\-–—]\s*", re.I),
    re.compile(r"^(?:NFL|NBA|WNBA|NHL|MLB|MLS|UEFA|CFL|CHL|UFC|WWE)\s*(?:\|\s*)?\d{1,3}\s*[-–—:]\s*", re.I),
    re.compile(r"^(?:DAZN\+?|NETFLIX)\s+PPV\s+\d{1,3}\s*[-–—:]\s*", re.I),
    re.compile(r"^BTN\+\s*\d{1,3}\s+(?:4K|UHD|FHD|HD|SD)\s*(?:\(D\))?\s*:\s*(?:B1G\+\s*\|\s*)?", re.I),
    re.compile(r"^(?:FLO\s+\d{1,4}|(?:PPV\s+)?(?:LIVE\s+)?EVENT(?:\s*(?:NO\.?|#)?\s*\d+)?)\s*[|:\-–—]*\s*", re.I),
)
_KNOWN_EASTERN_FAMILIES = frozenset({
    "us_espn_plus", "us_espn_numbered", "us_espn_play", "us_flo", "usa_flo", "flosports", "btn_plus",
})


@dataclass(frozen=True)
class ProviderEventGuide:
    name: str
    start_epoch: int | None = None
    time_label: str = ""
    explicit_end_epoch: int | None = None
    timing_status: str = "unscheduled"
    timing_evidence: str = "missing_datetime"
    timezone: str | None = None


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = "".join(
        " " if character.isspace() else character
        for character in text
        if (unicodedata.category(character) not in {"Cc", "Cs"} or character.isspace())
        and character not in "\ufffe\uffff"
        and character not in "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"
    )
    return " ".join(text.split())


def _clean_name(text: str, *, had_date: bool = False) -> str:
    text = re.sub(r"\[(?:LIVE|LIVE EVENT|EVENT|4K|UHD|FHD|HD|SD|HEVC)\]", " ", text, flags=re.I)
    text = re.sub(r"\((?:LIVE|LIVE EVENT|EVENT|4K|UHD|FHD|HD|SD|HEVC)\)", " ", text, flags=re.I)
    text = re.sub(r"[#*_]{2,}", " ", text)
    for _ in range(5):
        old = text
        for pattern in _PREFIXES:
            text = pattern.sub("", text).strip()
        if text == old:
            break
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"(?<=\w)_(?=\s|\w)", " ", text)
    text = re.sub(r"(?:\s+|^)(?:(?:4K|UHD|FHD|HD|SD|HEVC|H\.?26[45]|1080P|2160P)\s*)+$", "", text, flags=re.I)
    text = text.strip(" |:;,\t-–—")
    if had_date:
        text = re.sub(r"\s+\b(?:at|on|scheduled(?:\s+for)?)$", "", text, flags=re.I).rstrip()
    return " ".join(text.split())[:180].strip()


def _zone(hint: str, allow_eastern: bool):
    hint = hint.upper()
    if hint in {"ET", "EST", "EDT"} or (not hint and allow_eastern):
        return _EASTERN, "America/New_York", hint
    if hint in {"Z", "UTC", "GMT"}:
        return timezone.utc, "UTC", ""
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", hint):
        digits = hint[1:].replace(":", "")
        hours, minutes = int(digits[:2]), int(digits[2:])
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            return None, None, "invalid_timezone"
        delta = timedelta(hours=hours, minutes=minutes) * (1 if hint[0] == "+" else -1)
        zone = timezone(delta)
        return zone, zone.tzname(None), ""
    return None, None, "unknown_timezone"


def _wall_clock(naive: datetime, zone, hint: str):
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if (candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive
                and all(value.utcoffset() != candidate.utcoffset() for value in candidates)):
            candidates.append(candidate)
    if hint in {"EST", "EDT"}:
        candidates = [candidate for candidate in candidates if candidate.tzname() == hint]
    if len(candidates) != 1:
        return None, "ambiguous_local_time" if len(candidates) > 1 else "invalid_local_time"
    return candidates[0], ""


def _decode(match: re.Match[str], *, reference_epoch: int, allow_eastern: bool, inherited_zone: str = ""):
    parts = match.groupdict()
    if parts.get("month_name") and str(parts.get("zone") or "").startswith(("+", "-")):
        return None, None, "unknown_timezone"
    zone, zone_name, hint = _zone(parts.get("zone") or inherited_zone, allow_eastern)
    if zone is None:
        return None, zone_name, hint
    try:
        month = _MONTHS[parts["month_name"][:3].casefold()] if parts.get("month_name") else int(parts["month"])
        day, hour = int(parts["day"]), int(parts["hour"])
        minute, second = int(parts.get("minute") or 0), int(parts.get("second") or 0)
        if parts.get("meridiem"):
            if not 1 <= hour <= 12:
                raise ValueError("invalid 12-hour clock")
            hour = hour % 12 + (12 if parts["meridiem"].upper() == "PM" else 0)
        reference = datetime.fromtimestamp(reference_epoch, zone)
        supplied_years = {int(parts[key]) for key in ("year", "prefix_year", "trailing_year") if parts.get(key)}
        if len(supplied_years) > 1:
            return None, zone_name, "invalid_datetime"
        explicit_year = bool(supplied_years)
        years = list(supplied_years) if explicit_year else [reference.year - 1, reference.year, reference.year + 1]
        candidates = []
        failures = []
        for year in years:
            try:
                naive = datetime(year, month, day, hour, minute, second)
            except ValueError:
                continue
            value, failure = _wall_clock(naive, zone, hint)
            if value is not None:
                candidates.append(value)
            elif explicit_year or year == reference.year:
                failures.append(failure)
                if not explicit_year and abs((naive - reference.replace(tzinfo=None)).total_seconds()) <= YEARLESS_NEARBY_DAYS * 86400:
                    return None, zone_name, failure
        if not candidates:
            return None, zone_name, failures[0] if failures else "invalid_datetime"
        nearest = min(candidates, key=lambda value: (abs(value.timestamp() - reference_epoch), value.timestamp()))
        limit_days = MAX_EXPLICIT_DATE_DISTANCE_DAYS if explicit_year else YEARLESS_NEARBY_DAYS
        if abs(nearest.timestamp() - reference_epoch) > limit_days * 86400:
            return None, zone_name, "date_outside_window" if explicit_year else "year_unresolved"
        return nearest, zone_name, "explicit_provider_datetime" if explicit_year else "inferred_nearby_year"
    except (ValueError, KeyError, OverflowError, OSError):
        return None, zone_name, "invalid_datetime"


def _label(value: datetime) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year} {value.strftime('%I').lstrip('0')}:{value.strftime('%M %p %Z')}"


def _unknown_zone_after(tail: str) -> bool:
    tail = re.sub(r"^(?:\s*[)\]]){0,3}\s*", "", tail)
    if _UNSUPPORTED_ZONE.match(tail):
        return True
    token = re.match(r"^\s*([A-Za-z][A-Za-z0-9_:+/-]{1,31})(?=$|\s|[)\],;|])", tail)
    if token is None:
        return False
    value = token.group(1)
    if value.casefold() in {"hd", "sd", "uhd", "fhd", "hevc", "live", "vs", "at", "on", "to", "until", "day", "round", "game", "match"}:
        return False
    # A clock followed by an unknown abbreviation or zone/offset syntax is
    # explicit uncertainty. The North American default must not override it.
    return (
        any(character in value for character in ":/+_-")
        or (2 <= len(value) <= 6 and value.isalpha() and (value.isupper() or value.islower()))
    )


def parse_provider_event(
    value: object, *, reference_epoch: int, family: str = "", category: str = "",
) -> ProviderEventGuide:
    """Return source-derived name/time, failing closed on uncertain clocks.

    A yearless date must be within 31 days of the build. Legacy parenthesized
    ISO/numeric formats retain their documented Eastern clock assumption;
    other naive formats require a North American family/category/route.
    """
    if type(reference_epoch) is not int:
        raise ValueError("reference_epoch must be an integer")
    has_control = any(
        (unicodedata.category(character) in {"Cc", "Cs"} and not character.isspace())
        or character in "\ufffe\uffff\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"
        for character in str(value or "")
    )
    text = _normalize(value)
    if len(text) > MAX_EVENT_LABEL_CHARACTERS:
        return ProviderEventGuide(_clean_name(text[:MAX_EVENT_LABEL_CHARACTERS]), timing_status="invalid", timing_evidence="label_too_long")
    matches: list[tuple[re.Match[str], str]] = []
    for pattern, kind in ((_ISO, "iso"), (_SHORT, "short"), (_MONTH_NAME, "month_name")):
        for match in pattern.finditer(text):
            if not any(match.start() < prior.end() and prior.start() < match.end() for prior, _ in matches):
                matches.append((match, kind))
    matches.sort(key=lambda item: item[0].start())
    clock_end = None
    if len(matches) == 1:
        tail = text[matches[0][0].end():]
        clock_end = _END_CLOCK.match(tail) or _END_CLOCK_12.match(tail)
    is_range = len(matches) == 2 and _RANGE_SEPARATOR.fullmatch(text[matches[0][0].end():matches[1][0].start()]) is not None
    name_text = text
    if clock_end is not None:
        name_text = name_text[:matches[0][0].end()] + " " + name_text[matches[0][0].end() + clock_end.end():]
    if is_range:
        prefix = re.sub(r"\bstart\s*:\s*$", "", name_text[:matches[0][0].start()], flags=re.I)
        name_text = prefix + " " + name_text[matches[1][0].end():]
    else:
        for match, _kind in reversed(matches):
            prefix = re.sub(r"\bstart\s*:\s*$", "", name_text[:match.start()], flags=re.I)
            name_text = prefix + " " + name_text[match.end():]
    name = _clean_name(name_text, had_date=bool(matches))
    normalized_name = name.casefold()
    if re.search(r"\b(?:no events?(?: scheduled)?|no scheduled events?)\b", normalized_name):
        return ProviderEventGuide("No Event Scheduled", timing_status="no_event", timing_evidence="provider_no_event")
    if re.search(r"\b(?:off[ -]?air|offline)\b", normalized_name):
        return ProviderEventGuide("Off Air", timing_status="off_air", timing_evidence="provider_off_air")
    if not name or re.search(r"\b(?:coming soon|tba|to be announced)\b", normalized_name):
        return ProviderEventGuide("Event To Be Announced", timing_status="tba", timing_evidence="provider_tba")
    if has_control:
        return ProviderEventGuide(name, timing_status="invalid", timing_evidence="control_characters")
    if not matches:
        return ProviderEventGuide(name)
    if clock_end is not None and _unknown_zone_after(text[matches[0][0].end() + clock_end.end():]):
        return ProviderEventGuide(name, timing_evidence="unknown_timezone")
    if len(matches) > 4:
        return ProviderEventGuide(name, timing_status="ambiguous", timing_evidence="conflicting_datetimes")
    route_eastern = bool(re.match(r"^(?:(?:US|USA|CA|CAN|CANADA)\b|\(\s*(?:US|USA|CA|CAN|CANADA)\s*\))", text, re.I))
    australian_play = family == "au_espn_play" or bool(re.match(r"^\(\s*AU\s*\)\s+ESPN\s+PLAY\s*\d", text, re.I))
    category_eastern = bool(re.search(r"\b(?:US|USA|CANADA)\b", str(category), re.I))
    league_eastern = family == "league_event" and bool(re.match(r"^(?:NFL|NBA|WNBA|NHL|MLB|MLS|CFL|CHL)\b", name_text, re.I))
    decoded = []
    for match, kind in matches:
        if _unknown_zone_after(text[match.end():]):
            return ProviderEventGuide(name, timing_evidence="unknown_timezone")
        legacy_parenthesized = kind == "short" or (kind == "iso" and text[:match.start()].rstrip().endswith("(") and text[match.end():].lstrip().startswith(")"))
        allow_eastern = not australian_play and (legacy_parenthesized or route_eastern or category_eastern or league_eastern or family in _KNOWN_EASTERN_FAMILIES)
        inherited_zone = ""
        if is_range:
            inherited_zone = next((other.groupdict().get("zone") for other, _ in matches if other.groupdict().get("zone")), "")
        elif clock_end is not None:
            inherited_zone = clock_end.groupdict().get("zone") or ""
        start, zone_name, evidence = _decode(match, reference_epoch=reference_epoch, allow_eastern=allow_eastern, inherited_zone=inherited_zone)
        if start is None:
            status = "ambiguous" if evidence == "ambiguous_local_time" else "invalid" if evidence in {"invalid_datetime", "invalid_local_time", "invalid_timezone", "date_outside_window"} else "unscheduled"
            return ProviderEventGuide(name, timing_status=status, timing_evidence=evidence, timezone=zone_name)
        decoded.append((start, zone_name, evidence, match))
    start, zone_name, evidence, start_match = decoded[0]
    end_epoch = None
    if is_range:
        end = decoded[1][0]
        if not 0 < end.timestamp() - start.timestamp() <= MAX_EXPLICIT_EVENT_SECONDS:
            return ProviderEventGuide(name, timing_status="invalid", timing_evidence="invalid_event_range", timezone=zone_name)
        end_epoch = int(end.timestamp())
    elif len(decoded) > 1:
        # Compare at each representation's precision. A human minute label
        # corroborates an ISO stamp with seconds anywhere in that same minute.
        anchor = max(decoded, key=lambda item: (
            item[3].groupdict().get("second") is not None,
            any(item[3].groupdict().get(key) for key in ("year", "prefix_year", "trailing_year")),
        ))
        for candidate, _candidate_zone, _candidate_evidence, match in decoded:
            divisor = 1 if match.groupdict().get("second") is not None else 60
            if int(candidate.timestamp()) // divisor != int(anchor[0].timestamp()) // divisor:
                return ProviderEventGuide(name, timing_status="ambiguous", timing_evidence="conflicting_datetimes")
        start, zone_name, _old_evidence, start_match = anchor
        evidence = "corroborated_provider_datetime"
    elif clock_end is not None:
        parts = clock_end.groupdict()
        try:
            hour, minute = int(parts["hour"]), int(parts.get("minute") or 0)
            second = int(parts.get("second") or 0)
            if parts.get("meridiem"):
                if not 1 <= hour <= 12:
                    raise ValueError("invalid end hour")
                hour = hour % 12 + (12 if parts["meridiem"].upper() == "PM" else 0)
            end_zone, _zone_name, hint = _zone(parts.get("zone") or "", False)
            if end_zone is None:
                if parts.get("zone"):
                    raise ValueError("invalid end zone")
                end_zone, hint = start.tzinfo, ""
            local_start = start.astimezone(end_zone)
            end_wall = datetime(local_start.year, local_start.month, local_start.day, hour, minute, second)
            end, failure = _wall_clock(end_wall, end_zone, hint)
            if end is not None and end.timestamp() <= start.timestamp():
                end, failure = _wall_clock(end_wall + timedelta(days=1), end_zone, hint)
            if failure or end is None or not 0 < end.timestamp() - start.timestamp() <= MAX_EXPLICIT_EVENT_SECONDS:
                raise ValueError("invalid end range")
            end_epoch = int(end.timestamp())
        except (ValueError, KeyError, OverflowError):
            return ProviderEventGuide(name, timing_status="invalid", timing_evidence="invalid_event_range", timezone=zone_name)
    return ProviderEventGuide(name, int(start.timestamp()), _label(start), end_epoch, "scheduled", evidence, zone_name)


__all__ = ["ProviderEventGuide", "parse_provider_event"]
