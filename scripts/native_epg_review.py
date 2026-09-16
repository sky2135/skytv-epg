#!/usr/bin/env python3
"""Validate exact native-panel XMLTV IDs for REVIEW-only decisions.

This module deliberately performs no matching, network access, persistence, or
Google Sheets work.  A caller supplies exact candidate IDs from current
provider evidence; this validator accepts only IDs that are case-unique in the
complete native catalog and have a useful current/future programme window.

Server 1 native EPG is forbidden by policy.  The same validator can therefore
be shared by the read-only backlog analyzer and a later controlled writer
without either caller being able to weaken that boundary.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Iterable, Mapping

from lxml import etree


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_epg_streaming as streaming  # noqa: E402
import epg_catalog_stream as catalog_stream  # noqa: E402


MAX_NATIVE_REQUEST_IDS = streaming.MAX_MAPPING_ROWS
MIN_NATIVE_CATALOG_IDS = 100
MAX_NATIVE_DISPLAY_NAMES_PER_ID = 16
MAX_NATIVE_CATALOG_DISPLAY_NAME_KEYS = streaming.MAX_MAPPING_ROWS * 4
MAX_NATIVE_GATE_SIGNATURES = catalog_stream.MAX_GATE_SIGNATURES_PER_ID
NATIVE_GATE_HORIZON_SECONDS = catalog_stream.DEFAULT_GATE_HORIZON_SECONDS
NATIVE_GATE_MINIMUM_PROGRAMMES = catalog_stream.DEFAULT_GATE_MINIMUM_PROGRAMMES
NATIVE_GATE_MINIMUM_FUTURE_SECONDS = (
    catalog_stream.DEFAULT_GATE_MINIMUM_FUTURE_SECONDS
)
NATIVE_GATE_MAXIMUM_INITIAL_GAP_SECONDS = (
    catalog_stream.DEFAULT_GATE_MAXIMUM_INITIAL_GAP_SECONDS
)

_COUNTRY_WRAPPER = (
    r"US|USA|CA|CANADA|UK|GB|IN|INDIA|PK|PT|AU|NZ|ZA|IE|FR|DE|ES|IT|"
    r"NL|BE|CH|AT|SE|NO|DK|FI|PL|CZ|SK|HU|RO|BG|GR|TR|RU|UA|AE|SA|"
    r"QA|EG|MA|MX|BR|AR|CL|CO|PE|VE|UY|PY|BO|EC|DO|PR|JM|TT|BZ|CR|"
    r"PA|GT|HN|SV|NI|CN|HK|TW|JP|KR|TH|VN|ID|MY|SG|PH|BD|LK|NP|AF|"
    r"IR|IQ|IL|PS|JO|LB|KW|BH|OM|YE|SY|RS|HR|SI|BA|ME|MK|AL|EE|LV|"
    r"LT|IS|LU|MT|CY|GE|AM|AZ|KZ|UZ"
)
_BRACKETED_COUNTRY_PREFIX_RE = re.compile(
    rf"^\s*[\[(]\s*(?:{_COUNTRY_WRAPPER})\s*[\])]\s*",
    re.IGNORECASE,
)
_DELIMITED_COUNTRY_PREFIX_RE = re.compile(
    rf"^\s*(?:{_COUNTRY_WRAPPER})\s*(?:[|:/\\-]+)\s*",
    re.IGNORECASE,
)
_TRAILING_QUALITY_RE = re.compile(r"(?:\s+(?:sd|hd|fhd|uhd|4k))+$")
_GENERIC_NATIVE_NAME_RE = re.compile(
    r"(?:channel|tv|live|stream|events?|ppv|sports?|movies?|news|"
    r"entertainment|radio|cinema|kids|music|documentary|series|football|"
    r"soccer|cricket|hockey|nba|nfl|nhl|mlb|test|unknown|unnamed)"
    r"(?:\s+\d+)?"
)


class NativeReviewError(RuntimeError):
    """A controlled native-review failure that contains no private XMLTV ID."""


@dataclass(frozen=True)
class NativeValidation:
    verified_ids: frozenset[str]
    requested_ids: int
    display_names_by_id: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    ambiguous_display_name_keys: frozenset[str] = frozenset()


@dataclass
class _GateAccumulator:
    signatures: set[tuple[int, int]] = field(default_factory=set)
    first_start: int | None = None
    latest_stop: int | None = None


def _strict_xmltv_id(value: object, *, source_value: bool) -> str:
    text = str(value or "")
    normalized = streaming.INVALID_XML_RE.sub(
        "", text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    ).strip()
    if normalized != text or len(text) > 300:
        label = "source" if source_value else "candidate set"
        raise NativeReviewError(f"The native EPG {label} contains an invalid ID.")
    return text


def native_display_name_key(value: object) -> str:
    """Return a conservative exact-comparison key for native display names."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    if not text:
        return ""
    text = _BRACKETED_COUNTRY_PREFIX_RE.sub("", text, count=1)
    text = _DELIMITED_COUNTRY_PREFIX_RE.sub("", text, count=1)
    text = "".join(character if character.isalnum() else " " for character in text)
    text = " ".join(text.split())
    text = _TRAILING_QUALITY_RE.sub("", text).strip()
    if (
        not text
        or not any(character.isalpha() for character in text)
        or _GENERIC_NATIVE_NAME_RE.fullmatch(text)
    ):
        return ""
    return text


def native_names_compatible(
    provider_name: object,
    display_names: Iterable[str],
) -> bool:
    """Require one non-generic exact normalized native display-name match."""

    provider_key = native_display_name_key(provider_name)
    if not provider_key:
        return False
    return any(
        display_key == provider_key
        for display_name in display_names
        for display_key in (native_display_name_key(display_name),)
        if display_key
    )


def _stable_file_state(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _open_regular_file(path: Path) -> tuple[BinaryIO, os.stat_result]:
    try:
        path_state = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise NativeReviewError("A native XMLTV source is unavailable.") from exc
    if not stat.S_ISREG(path_state.st_mode):
        raise NativeReviewError("A native XMLTV source is not a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise NativeReviewError("A native XMLTV source is unavailable.") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise NativeReviewError(
                "A native XMLTV source is not a regular file."
            )
        if _stable_file_state(path_state) != _stable_file_state(initial):
            raise NativeReviewError(
                "The native XMLTV source changed before validation."
            )
        return os.fdopen(descriptor, "rb"), initial
    except Exception:
        os.close(descriptor)
        raise


def _assert_file_unchanged(
    path: Path,
    handle: BinaryIO,
    initial: os.stat_result,
) -> None:
    try:
        final = os.fstat(handle.fileno())
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise NativeReviewError(
            "The native XMLTV source changed during validation."
        ) from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _stable_file_state(initial) != _stable_file_state(final)
        or _stable_file_state(initial) != _stable_file_state(current)
    ):
        raise NativeReviewError(
            "The native XMLTV source changed during validation."
        )


def _freeze_case_unique_ids(
    requested_ids: frozenset[str], source_ids: set[str]
) -> frozenset[str]:
    variants: dict[str, set[str]] = defaultdict(set)
    for source_id in source_ids:
        variants[source_id.casefold()].add(source_id)
    return frozenset(
        requested
        for requested in requested_ids
        if requested in source_ids and variants.get(requested.casefold()) == {requested}
    )


def _normalize_requested_ids(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        if not value:
            continue
        result.add(_strict_xmltv_id(value, source_value=False))
        if len(result) > MAX_NATIVE_REQUEST_IDS:
            raise NativeReviewError("The native EPG candidate set is too large.")
    return frozenset(result)


def validate_native_xmltv(
    path: Path,
    *,
    server_id: str,
    requested_ids: Iterable[str],
    now_epoch: int,
) -> NativeValidation:
    """Return exact native IDs with a useful six-hour programme window.

    Candidate IDs are never inferred here.  Each verified ID must occur with
    exactly the requested case, have no differently-cased catalog twin, start
    useful programming within six hours, contain at least two distinct bounded
    programme intervals, and cover at least six hours into the future.
    """

    try:
        normalized_server = streaming.normalize_server_id(server_id)
    except streaming.BuildError as exc:
        raise NativeReviewError(
            "Native EPG validation received an unsupported server."
        ) from exc
    if normalized_server == "server_1":
        raise NativeReviewError("Server 1 native EPG validation is forbidden.")
    if normalized_server not in {"server_2", "server_3"}:
        raise NativeReviewError(
            "Native EPG validation received an unsupported server."
        )

    requested = _normalize_requested_ids(requested_ids)
    if "" in requested:
        raise NativeReviewError("The native EPG candidate set is invalid.")
    if not requested:
        return NativeValidation(frozenset(), 0)
    if not isinstance(now_epoch, int) or isinstance(now_epoch, bool) or now_epoch < 0:
        raise NativeReviewError("The native EPG validation time is invalid.")

    source = Path(path)
    handle, initial_state = _open_regular_file(source)
    source_ids: set[str] = set()
    duplicate_source_ids: set[str] = set()
    safe_ids: frozenset[str] | None = None
    display_names: dict[str, set[str]] = {
        value: set() for value in requested
    }
    display_name_owner_by_key: dict[str, str | None] = {}
    display_name_overflow: set[str] = set()
    accumulators = {value: _GateAccumulator() for value in requested}
    root_element: etree._Element | None = None
    active_top_level: etree._Element | None = None
    active_child_elements = 0
    total_elements = 0
    programme_phase_started = False
    gate_end = int(now_epoch) + NATIVE_GATE_HORIZON_SECONDS
    maximum_gate_stop = (
        gate_end + catalog_stream.MAX_GATE_PROGRAMME_DURATION_SECONDS
    )

    try:
        try:
            streaming.reject_unsafe_xml_prefix_handle(
                handle,
                allow_inert_xmltv_doctype=True,
                source_label=f"panel:{normalized_server}",
            )
        except streaming.BuildError as exc:
            raise NativeReviewError(
                "A native XMLTV source failed its security preflight."
            ) from exc

        try:
            with streaming.open_limited_xml_handle(
                handle, streaming.MAX_SOURCE_EXPANDED_BYTES
            ) as bounded_source:
                context = etree.iterparse(
                    bounded_source,
                    events=("start", "end"),
                    recover=False,
                    huge_tree=False,
                    load_dtd=False,
                    no_network=True,
                    resolve_entities=False,
                    remove_comments=True,
                    remove_pis=True,
                )
                for event, element in context:
                    if event == "start":
                        if root_element is None:
                            root_element = element
                            streaming.reject_parsed_doctype(
                                element,
                                allow_inert_xmltv_doctype=True,
                                source_label=f"panel:{normalized_server}",
                            )
                            if streaming.local_name(element.tag) != "tv":
                                raise NativeReviewError(
                                    "A native EPG source is not an XMLTV document."
                                )
                        elif element.getparent() is root_element:
                            child_name = streaming.local_name(element.tag)
                            if child_name not in {"channel", "programme"}:
                                raise NativeReviewError(
                                    "A native XMLTV source has an unsupported "
                                    "top-level record."
                                )
                            active_top_level = element
                            active_child_elements = 0
                        elif active_top_level is not None:
                            active_child_elements += 1
                            if (
                                active_child_elements
                                > streaming.MAX_RECORD_CHILD_ELEMENTS
                            ):
                                raise NativeReviewError(
                                    "A native XMLTV record exceeds its configured limit."
                                )
                        continue

                    if root_element is None or element.getparent() is not root_element:
                        continue

                    record_name = streaming.local_name(element.tag)
                    total_elements += 1
                    if total_elements > streaming.MAX_SOURCE_ELEMENTS:
                        raise NativeReviewError(
                            "A native XMLTV source exceeds its configured record limit."
                        )
                    if record_name == "channel":
                        if programme_phase_started:
                            raise NativeReviewError(
                                "A native XMLTV source declares channels after programmes."
                            )
                        source_id = _strict_xmltv_id(
                            element.get("id") or "",
                            source_value=True,
                        )
                        if source_id:
                            if source_id in source_ids:
                                duplicate_source_ids.add(source_id)
                            source_ids.add(source_id)
                            if len(source_ids) > streaming.MAX_MAPPING_ROWS:
                                raise NativeReviewError(
                                    "A native XMLTV source has too many channel identities."
                                )
                            for child in element:
                                if streaming.local_name(child.tag) != "display-name":
                                    continue
                                display_name = streaming.clean_text(
                                    "".join(child.itertext()), 300
                                )
                                if not display_name:
                                    continue
                                display_key = native_display_name_key(display_name)
                                if display_key:
                                    if display_key not in display_name_owner_by_key:
                                        if (
                                            len(display_name_owner_by_key)
                                            >= MAX_NATIVE_CATALOG_DISPLAY_NAME_KEYS
                                        ):
                                            raise NativeReviewError(
                                                "A native XMLTV source has too many "
                                                "display-name identities."
                                            )
                                        display_name_owner_by_key[display_key] = source_id
                                    elif (
                                        display_name_owner_by_key[display_key]
                                        != source_id
                                    ):
                                        display_name_owner_by_key[display_key] = None
                                if source_id in requested:
                                    if (
                                        display_name in display_names[source_id]
                                    ):
                                        continue
                                    if (
                                        len(display_names[source_id])
                                        >= MAX_NATIVE_DISPLAY_NAMES_PER_ID
                                    ):
                                        display_name_overflow.add(source_id)
                                        continue
                                    display_names[source_id].add(display_name)
                    else:
                        if not programme_phase_started:
                            programme_phase_started = True
                            safe_ids = _freeze_case_unique_ids(
                                requested, source_ids
                            ).difference(display_name_overflow).difference(
                                duplicate_source_ids
                            )
                        source_id = _strict_xmltv_id(
                            element.get("channel") or "",
                            source_value=True,
                        )
                        if safe_ids is not None and source_id in safe_ids:
                            start_epoch = streaming.parse_xmltv_time(
                                element.get("start")
                            )
                            stop_epoch = streaming.parse_xmltv_time(
                                element.get("stop")
                            )
                            title = streaming.preferred_child_text(element, "title")
                            if (
                                start_epoch is not None
                                and stop_epoch is not None
                                and stop_epoch > now_epoch
                                and start_epoch < gate_end
                                and catalog_stream.informative_programme_title(title)
                            ):
                                duration = int(stop_epoch) - int(start_epoch)
                                if (
                                    0 < duration
                                    <= catalog_stream.MAX_GATE_PROGRAMME_DURATION_SECONDS
                                    and stop_epoch <= maximum_gate_stop
                                ):
                                    accumulator = accumulators[source_id]
                                    signature = (int(start_epoch), int(stop_epoch))
                                    if (
                                        signature in accumulator.signatures
                                        or len(accumulator.signatures)
                                        < MAX_NATIVE_GATE_SIGNATURES
                                    ):
                                        accumulator.signatures.add(signature)
                                    accumulator.first_start = (
                                        int(start_epoch)
                                        if accumulator.first_start is None
                                        else min(
                                            accumulator.first_start,
                                            int(start_epoch),
                                        )
                                    )
                                    accumulator.latest_stop = (
                                        int(stop_epoch)
                                        if accumulator.latest_stop is None
                                        else max(
                                            accumulator.latest_stop,
                                            int(stop_epoch),
                                        )
                                    )
                    streaming.release_top_level(element)
                    active_top_level = None
                    active_child_elements = 0
                del context
        except NativeReviewError:
            raise
        except (streaming.BuildError, etree.XMLSyntaxError, OSError, EOFError) as exc:
            raise NativeReviewError(
                "A native XMLTV source is malformed or unavailable."
            ) from exc

        _assert_file_unchanged(source, handle, initial_state)
    finally:
        handle.close()

    if root_element is None:
        raise NativeReviewError("A native XMLTV source is empty.")
    if len(source_ids) < MIN_NATIVE_CATALOG_IDS:
        raise NativeReviewError(
            "A native XMLTV source is below its conservative completeness floor."
        )
    if safe_ids is None:
        safe_ids = _freeze_case_unique_ids(requested, source_ids).difference(
            display_name_overflow
        ).difference(duplicate_source_ids)

    requested_name_keys_by_id = {
        source_id: frozenset(
            key
            for display_name in display_names[source_id]
            for key in (native_display_name_key(display_name),)
            if key
        )
        for source_id in safe_ids
    }
    ambiguous_display_name_keys = frozenset(
        key
        for keys in requested_name_keys_by_id.values()
        for key in keys
        if display_name_owner_by_key.get(key) is None
    )
    identity_safe_ids = frozenset(
        source_id
        for source_id, name_keys in requested_name_keys_by_id.items()
        if name_keys and name_keys.isdisjoint(ambiguous_display_name_keys)
    )

    required_stop = int(now_epoch) + NATIVE_GATE_MINIMUM_FUTURE_SECONDS
    latest_near_start = int(now_epoch) + NATIVE_GATE_MAXIMUM_INITIAL_GAP_SECONDS
    verified = frozenset(
        source_id
        for source_id in identity_safe_ids
        for accumulator in (accumulators[source_id],)
        if accumulator.first_start is not None
        and accumulator.first_start <= latest_near_start
        and len(accumulator.signatures) >= NATIVE_GATE_MINIMUM_PROGRAMMES
        and accumulator.latest_stop is not None
        and accumulator.latest_stop >= required_stop
    )
    frozen_display_names = MappingProxyType(
        {
            source_id: tuple(
                sorted(
                    display_names[source_id],
                    key=lambda value: (value.casefold(), value),
                )
            )
            for source_id in sorted(
                identity_safe_ids, key=lambda value: (value.casefold(), value)
            )
        }
    )
    return NativeValidation(
        verified_ids=verified,
        requested_ids=len(requested),
        display_names_by_id=frozen_display_names,
        ambiguous_display_name_keys=ambiguous_display_name_keys,
    )


__all__ = [
    "MAX_NATIVE_CATALOG_DISPLAY_NAME_KEYS",
    "MAX_NATIVE_DISPLAY_NAMES_PER_ID",
    "MIN_NATIVE_CATALOG_IDS",
    "NativeReviewError",
    "NativeValidation",
    "native_display_name_key",
    "native_names_compatible",
    "validate_native_xmltv",
]
