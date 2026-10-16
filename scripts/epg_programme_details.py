"""Bounded XMLTV programme facts shared by both source ingestion paths.

Values describe what the source supplied. No episode, rating, artwork, or
broadcast status is inferred from a title or channel classification.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse


MAX_DETAILS_BYTES = 32 * 1024
MAX_DETAIL_ITEMS = 32
TEXT_FIELDS = frozenset({"date", "language", "originalLanguage", "icon"})
LIST_FIELDS = frozenset({"episode", "ratings", "starRatings", "credits", "subtitles"})
DETAIL_FIELDS = TEXT_FIELDS | LIST_FIELDS | frozenset(
    {"countries", "broadcastType", "previouslyShown", "video", "audio", "length", "stopInferred"}
)
RECORD_FIELDS = {
    "episode": {"system", "value"}, "ratings": {"system", "value"},
    "starRatings": {"system", "value"}, "credits": {"role", "name", "character", "guest"},
    "subtitles": {"type", "language"}, "previouslyShown": {"start", "channel"},
    "video": {"present", "colour", "aspect", "quality"}, "audio": {"present", "stereo"},
    "length": {"value", "units"},
}


def _text(value: object, maximum: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()[:maximum]


def _name(element: Any) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _value(element: Any, maximum: int = 300) -> str:
    return _text("".join(element.itertext()), maximum)


def _public_icon(value: object, base_url: str) -> str:
    if len(str(value or "")) > 2_000:
        return ""
    text = _text(value, 2_000)
    if not text:
        return ""
    if base_url:
        text = urljoin(base_url, text)
    try:
        parsed = urlparse(text)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return ""
    except ValueError:
        return ""
    return text


def details_json(details: dict[str, Any]) -> str:
    """Validate the private spool's bounded JSON representation."""
    if not isinstance(details, dict) or set(details) - DETAIL_FIELDS:
        raise ValueError("Programme details contain unsupported fields.")

    def valid_record(key: str, value: object) -> bool:
        return (isinstance(value, dict) and not set(value) - RECORD_FIELDS[key]
                and all(isinstance(v, str) and len(v) <= 300 and "\x00" not in v
                        for v in value.values()))

    for key, value in details.items():
        if key in TEXT_FIELDS:
            if (not isinstance(value, str) or len(value) > (2_000 if key == "icon" else 300)
                    or "\x00" in value):
                raise ValueError("Programme details contain invalid text.")
            if key == "icon" and _public_icon(value, "") != value:
                raise ValueError("Programme details contain an unsafe artwork URL.")
        elif key == "stopInferred":
            if value is not True:
                raise ValueError("Programme details contain invalid timing provenance.")
        elif key in LIST_FIELDS | {"countries", "broadcastType"}:
            if not isinstance(value, list) or len(value) > MAX_DETAIL_ITEMS:
                raise ValueError("Programme details contain an invalid list.")
            if key in {"countries", "broadcastType"}:
                if any(not isinstance(item, str) or len(item) > 300 or "\x00" in item for item in value):
                    raise ValueError("Programme details contain invalid list text.")
                if key == "broadcastType" and set(value) - {"live", "new", "premiere", "last-chance", "previously-shown"}:
                    raise ValueError("Programme details contain an invalid broadcast marker.")
            elif any(not valid_record(key, item) for item in value):
                raise ValueError("Programme details contain an invalid record list.")
        elif not valid_record(key, value):
            raise ValueError("Programme details contain an invalid record.")
    encoded = json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("Programme details are oversized or invalid.")
    return encoded


def parse_details_json(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("Programme details are oversized or invalid.")
    try:
        details = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Programme details are malformed.") from exc
    details_json(details)
    return details


def xmltv_programme_details(element: Any, *, allow_icons: bool = True,
                            source_base_url: str = "", stop_inferred: bool = False) -> dict[str, Any]:
    details: dict[str, Any] = {}

    def set_fact(key: str, value: Any) -> None:
        previous = details.get(key)
        details[key] = value
        # Reserve enough room to record inferred timing even after filling the
        # optional-field budget. Bound every assignment in UTF-8 bytes, not
        # merely list additions or character counts.
        if len(json.dumps(details, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_DETAILS_BYTES - 64:
            if previous is None:
                del details[key]
            else:
                details[key] = previous

    def append(key: str, value: Any) -> None:
        values = details.get(key, [])
        if value and value not in values and len(values) < MAX_DETAIL_ITEMS:
            set_fact(key, values + [value])

    for child in element:
        name = _name(child)
        value = _value(child)
        if name in {"date", "language", "orig-language"} and value:
            key = {"orig-language": "originalLanguage"}.get(name, name)
            if key not in details:
                set_fact(key, value)
        elif name == "icon" and allow_icons and "icon" not in details:
            icon = _public_icon(child.get("src"), source_base_url)
            if icon:
                set_fact("icon", icon)
        elif name == "episode-num" and value:
            append("episode", {"system": _text(child.get("system"), 100), "value": value})
        elif name in {"rating", "star-rating"}:
            rating_value = next((_value(item) for item in child if _name(item) == "value"), "")
            if rating_value:
                append("ratings" if name == "rating" else "starRatings",
                       {"system": _text(child.get("system"), 100), "value": rating_value})
        elif name == "credits":
            for person in child:
                person_name = _value(person)
                if person_name:
                    credit = {"role": _text(_name(person), 100), "name": person_name}
                    if person.get("role"):
                        credit["character"] = _text(person.get("role"))
                    if person.get("guest"):
                        credit["guest"] = _text(person.get("guest"))
                    append("credits", credit)
        elif name == "country" and value:
            append("countries", value)
        elif name in {"live", "new", "premiere", "last-chance", "previously-shown"}:
            append("broadcastType", name)
            if name == "previously-shown":
                set_fact("previouslyShown", {
                    key: _text(child.get(key)) for key in ("start", "channel") if child.get(key)
                })
        elif name in {"video", "audio"}:
            allowed = {"present", "colour", "aspect", "quality"} if name == "video" else {"present", "stereo"}
            facts = {_name(item): _value(item) for item in child
                     if _name(item) in allowed and _value(item)}
            if facts:
                set_fact(name, facts)
        elif name == "subtitles":
            subtitle = {"type": _text(child.get("type"), 100)}
            language = next((_value(item) for item in child if _name(item) == "language"), "")
            if language:
                subtitle["language"] = language
            if any(subtitle.values()):
                append("subtitles", subtitle)
        elif name == "length" and value:
            set_fact("length", {"value": value, "units": _text(child.get("units"), 100)})
    if stop_inferred:
        details["stopInferred"] = True
    details_json(details)
    return details
