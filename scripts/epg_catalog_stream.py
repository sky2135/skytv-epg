#!/usr/bin/env python3
"""One-pass EPGShare ALL_SOURCES1 catalog and programme-gate reader.

The XMLTV DTD orders all ``channel`` records before all ``programme`` records.
This module uses that boundary to support safe automatic mapping without a
second decompression pass:

1. stream and bound the complete channel catalog;
2. freeze a deterministic catalog when the first programme starts;
3. let a caller select provisional exact XMLTV IDs from that frozen catalog;
4. continue the same parse and send only fixed/provisional schedules to a sink;
5. return conservative current/future programme-usability gates.

It deliberately does not make a matching decision or write Google Sheets.
Those policy/side-effect layers remain separate, which makes this parser useful
to both the production builder and a dry-run/manual inventory workflow.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import stat
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence

from lxml import etree


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_epg_streaming as streaming  # noqa: E402


CATALOG_STREAM_VERSION = "1.0"
MAX_XMLTV_ID_CHARACTERS = 300
DEFAULT_MINIMUM_UNIQUE_CATALOG_CHANNELS = 20_000
MAX_CATALOG_CHANNEL_ELEMENTS = 100_000
MAX_CATALOG_TEXT_BYTES = 16 * 1024 * 1024
MAX_CATALOG_TEXT_LINES = 250_000
MAX_CATALOG_SECTIONS = 1_024
MAX_DUPLICATE_CHANNEL_RECORDS_PER_ID = 64
MAX_CHANNEL_VALUE_VARIANTS = 16
MAX_GATE_SIGNATURES_PER_ID = 8
MAX_GATE_PROGRAMME_DURATION_SECONDS = 24 * 60 * 60
DEFAULT_GATE_HORIZON_SECONDS = 72 * 60 * 60
DEFAULT_GATE_MINIMUM_PROGRAMMES = 2
DEFAULT_GATE_MINIMUM_FUTURE_SECONDS = 6 * 60 * 60
DEFAULT_GATE_MAXIMUM_INITIAL_GAP_SECONDS = 6 * 60 * 60

PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:"
    r"no\s+(?:program(?:me)?|events?|epg|schedule|information|info|data)"
    r"(?:\s+(?:information|info))?"
    r"(?:\s+(?:not\s+)?available|\s+unavailable)?(?:\s+\d+)?|"
    r"(?:program(?:me)?|events?|epg|schedule)"
    r"(?:\s+(?:information|info))?\s+(?:not\s+available|unavailable)|"
    r"to\s+be\s+(?:announced|advised)|tba|tbd|"
    r"off\s*air|sign(?:ed)?\s*off|nothing\s+scheduled|"
    r"no\s+event(?:s)?(?:\s+scheduled)?"
    r")$",
    flags=re.IGNORECASE,
)

# Kept aligned with the legacy guide validator. Matching exact normalized
# values avoids rejecting legitimate titles merely containing words such as
# "unknown" or "unavailable".
PLACEHOLDER_TITLE_KEYS = frozenset(
    {
        "n a",
        "na",
        "none",
        "unknown",
        "tba",
        "tbd",
        "to be announced",
        "to be advised",
        "no information",
        "no information available",
        "no programme information",
        "no program information",
        "no programme information available",
        "no program information available",
        "no epg",
        "no epg information",
        "no event",
        "no event information",
        "not available",
        "unavailable",
        "schedule unavailable",
        "programme unavailable",
        "program unavailable",
        "no data",
        "no schedule",
        "epg not available",
        "programme information not available",
        "program information not available",
        "off air",
        "sign off",
        "signed off",
        "nothing scheduled",
        "no events scheduled",
    }
)

DUMMY_ID_COMPONENT_RE = re.compile(r"(?:^|\.)dummy(?:\.|$)", re.IGNORECASE)
KNOWN_DUMMY_XMLTV_IDS = frozenset(
    value.casefold()
    for value in {
        "24.7.Dummy.us",
        "Adult.Programming.Dummy.us",
        "Adult.Section.Dummy.us",
        "Blank.Dummy.us",
        "ESPN+.Dummy.us",
        "FITE.TV.Dummy.us",
        "Flo.Events.Dummy.us",
        "Movie.Dummy.us",
        "Music.Choice.Dummy.us",
        "NEWS.Dummy.us",
        "PPV.EVENTS.Dummy.us",
        "Religious.Dummy.us",
        "Shopping.Dummy.us",
        "Sports.Dummy.us",
        "TrillerTV.Dummy.us",
    }
)

TEXT_SECTION_RE = re.compile(
    r"^--\s*epg_ripper_([A-Za-z0-9_+.-]+)\s*--$", re.IGNORECASE
)
TEXT_GENERATED_RE = re.compile(r"^\d{12,14}$")
TEXT_COUNTRY_SECTION_RE = re.compile(r"^([A-Z]{2})\d+$")


class CatalogStreamError(streaming.BuildError):
    """A controlled catalog/parser failure safe to expose in build logs."""


def _stable_file_state(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return identity/content metadata which ordinary reads cannot change."""
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


class _BoundXmlSource:
    """One immutable-path view whose descriptor supplies hash, scan, and XML.

    Reopening a pathname for each operation lets another process atomically
    replace it after its hash is recorded.  This object opens exactly once,
    keeps that regular-file descriptor alive through parsing, and checks both
    the descriptor and its pathname before and after every trust boundary.
    """

    def __init__(
        self,
        path: Path,
        handle: BinaryIO,
        initial_stat: os.stat_result,
        source_sha256: str,
    ) -> None:
        self.path = path
        self.handle = handle
        self.initial_stat = initial_stat
        self.source_sha256 = source_sha256

    @classmethod
    def open(cls, path: Path) -> "_BoundXmlSource":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.fspath(path), flags)
        except OSError as exc:
            raise CatalogStreamError(
                "The ALL_SOURCES1 input file cannot be opened safely."
            ) from exc
        handle = os.fdopen(descriptor, "rb")
        try:
            initial_stat = os.fstat(handle.fileno())
            path_stat = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(initial_stat.st_mode) or not stat.S_ISREG(
                path_stat.st_mode
            ):
                raise CatalogStreamError(
                    "The ALL_SOURCES1 input must be a regular file."
                )
            if (initial_stat.st_dev, initial_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise CatalogStreamError(
                    "The ALL_SOURCES1 path changed while it was being opened."
                )
            if initial_stat.st_size > streaming.MAX_SOURCE_COMPRESSED_BYTES:
                raise CatalogStreamError(
                    "The ALL_SOURCES1 input exceeds its compressed limit."
                )
            source_sha256 = streaming.sha256_open_file(handle)
            current_stat = os.fstat(handle.fileno())
            if _stable_file_state(current_stat) != _stable_file_state(initial_stat):
                raise CatalogStreamError(
                    "The ALL_SOURCES1 file changed while it was being hashed."
                )
            bound = cls(path, handle, initial_stat, source_sha256)
            bound.verify_path_and_descriptor()
            return bound
        except Exception:
            handle.close()
            raise

    @property
    def source_bytes(self) -> int:
        return int(self.initial_stat.st_size)

    def verify_path_and_descriptor(self, *, verify_hash: bool = False) -> None:
        try:
            descriptor_stat = os.fstat(self.handle.fileno())
            path_stat = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise CatalogStreamError(
                "The ALL_SOURCES1 path changed during verification."
            ) from exc
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or _stable_file_state(descriptor_stat)
            != _stable_file_state(self.initial_stat)
            or (path_stat.st_dev, path_stat.st_ino)
            != (self.initial_stat.st_dev, self.initial_stat.st_ino)
        ):
            raise CatalogStreamError(
                "The ALL_SOURCES1 file changed during verification."
            )
        if verify_hash:
            try:
                current_sha256 = streaming.sha256_open_file(self.handle)
            except OSError as exc:
                raise CatalogStreamError(
                    "The ALL_SOURCES1 file cannot be re-verified."
                ) from exc
            if current_sha256 != self.source_sha256:
                raise CatalogStreamError(
                    "The ALL_SOURCES1 bytes changed during verification."
                )

    def close(self) -> None:
        self.handle.close()


@dataclass(frozen=True)
class CatalogRoute:
    """Deterministic internal matcher route inferred from an XMLTV ID."""

    feed: str
    region: str


@dataclass(frozen=True)
class CatalogChannel:
    """One exact, case-preserving XMLTV channel identity."""

    epg_id: str
    display_name: str
    icon_url: str
    occurrences: int
    route: CatalogRoute


@dataclass(frozen=True)
class TextCatalogEntry:
    """One ID from the lightweight official ALL_SOURCES1 text catalog."""

    epg_id: str
    sections: tuple[str, ...]
    kind: str
    route: CatalogRoute


def _text_catalog_match_route(entry: TextCatalogEntry) -> CatalogRoute | None:
    """Corroborate an ID-suffix market with authoritative country sections.

    Most EPGShare sections are country feeds such as ``US2`` or ``IN1``;
    others are provider families such as ``AUDACY1``.  A recognized country
    section may supply the market for a dotless ID, but it must never silently
    contradict an informative ID suffix. Multiple distinct country sections
    are likewise ambiguous for unattended matching.
    """

    feeds_by_region: dict[str, set[str]] = defaultdict(set)
    for section in entry.sections:
        if re.fullmatch(r"US_[A-Z0-9_]+\d+", section):
            feeds_by_region["US"].add(section)
            continue
        match = TEXT_COUNTRY_SECTION_RE.fullmatch(section)
        if match is None:
            continue
        region = {"GB": "UK"}.get(match.group(1), match.group(1))
        feeds_by_region[region].add(section)
    if not feeds_by_region:
        return entry.route
    if len(feeds_by_region) != 1:
        return None
    section_region = next(iter(feeds_by_region))
    if entry.route.region not in {"ALL", section_region}:
        return None
    if entry.route.region == section_region:
        return entry.route
    return CatalogRoute(
        min(feeds_by_region[section_region]),
        section_region,
    )


@dataclass(frozen=True)
class TextCatalogSnapshot:
    """Bounded manual-workflow catalog parsed without loading the XML guide."""

    generated_token: str
    entries: tuple[TextCatalogEntry, ...]
    fingerprint_sha256: str
    duplicate_id_lines: int
    casefold_collision_keys: frozenset[str]
    ambiguous_kind_ids: frozenset[str]

    def validate_for_unattended_matching(self) -> None:
        """Fail closed when official TXT metadata contradicts itself.

        Silently omitting a contradictory entry can make a different, similar
        station appear unique to the frozen matcher.  Ambiguous real/dummy
        membership and conflicting country evidence are therefore catalog-wide
        preflight failures, not merely ineligible approval targets.
        """

        if self.ambiguous_kind_ids:
            raise CatalogStreamError(
                "The ALL_SOURCES1 text catalog assigns an ID to both real and "
                "dummy sections."
            )
        if any(
            entry.kind == "real" and _text_catalog_match_route(entry) is None
            for entry in self.entries
        ):
            raise CatalogStreamError(
                "The ALL_SOURCES1 text catalog contains conflicting country "
                "evidence for an ID."
            )
        routes_by_engine_safe_fold: dict[str, set[tuple[str, str]]] = defaultdict(set)
        kinds_by_engine_safe_fold: dict[str, set[str]] = defaultdict(set)
        for entry in self.entries:
            route = _text_catalog_match_route(entry)
            if route is None:
                # The contradiction above already fails; keep this loop total
                # for static analyzers and direct method reuse.
                continue
            engine_safe_fold = "".join(
                " " if character.isspace() else character
                for character in entry.epg_id
            ).casefold()
            routes_by_engine_safe_fold[engine_safe_fold].add(
                (route.feed, route.region)
            )
            kinds_by_engine_safe_fold[engine_safe_fold].add(entry.kind)
        if any(len(kinds) > 1 for kinds in kinds_by_engine_safe_fold.values()):
            raise CatalogStreamError(
                "The ALL_SOURCES1 text catalog has normalization-confusable "
                "IDs split between real and dummy sections."
            )
        if any(
            len(routes) > 1 for routes in routes_by_engine_safe_fold.values()
        ):
            raise CatalogStreamError(
                "The ALL_SOURCES1 text catalog has normalization-confusable "
                "IDs in multiple feed/market routes."
            )

    def matcher_inputs(
        self, *, explicit_dummy_ids: Iterable[str] = ()
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        """Return deterministic real candidates and explicit dummy IDs."""
        real: list[dict[str, str]] = []
        dummy: dict[str, str] = {}
        explicit_dummy_folds = _explicit_dummy_folds(explicit_dummy_ids)
        normalized_unsafe_twins = frozenset(
            "".join(
                " " if character.isspace() else character
                for character in entry.epg_id
            ).casefold()
            for entry in self.entries
            if any(
                character.isspace() and character != " "
                for character in entry.epg_id
            )
        )
        for entry in self.entries:
            if (
                entry.epg_id.casefold() in self.casefold_collision_keys
                or entry.epg_id in self.ambiguous_kind_ids
            ):
                continue
            # The frozen legacy matcher repairs old mojibake and, as part of
            # that compatibility path, converts non-ASCII Unicode whitespace
            # such as NBSP to a normal space. XMLTV IDs are opaque, so those
            # entries remain in this authoritative catalog and can still serve
            # an existing exact mapping, but they must never enter automatic
            # matching under a manufactured identity.
            if any(
                character.isspace() and character != " "
                for character in entry.epg_id
            ) or entry.epg_id.casefold() in normalized_unsafe_twins:
                continue
            if entry.kind == "dummy" or _is_dummy_xmltv_id(
                entry.epg_id, explicit_dummy_folds
            ):
                dummy[entry.epg_id.casefold()] = entry.epg_id
                continue
            match_route = _text_catalog_match_route(entry)
            if match_route is None:
                # A contradictory or multi-market official section is useful
                # evidence for review, never for unattended approval.
                continue
            display_name = _epg_id_match_name(entry.epg_id)
            real.append(
                {
                    "epg_id": entry.epg_id,
                    "feed": match_route.feed,
                    "region": match_route.region,
                    "display_name": display_name,
                    "normalized": _simple_match_key(display_name),
                }
            )
        return real, dummy


@dataclass(frozen=True)
class CatalogSnapshot:
    """Immutable catalog presented to the caller's matching policy."""

    channels: tuple[CatalogChannel, ...]
    source_sha256: str
    fingerprint_sha256: str
    duplicate_channel_elements: int
    casefold_collision_keys: frozenset[str]
    _by_exact: Mapping[str, CatalogChannel] = field(repr=False, compare=False)
    _ids_by_fold: Mapping[str, tuple[str, ...]] = field(repr=False, compare=False)

    def exact(self, epg_id: object) -> CatalogChannel | None:
        """Return only an exact, case-sensitive catalog identity."""
        return self._by_exact.get(str(epg_id or ""))

    def unique_casefold(self, epg_id: object) -> CatalogChannel | None:
        """Resolve case-insensitively only when the source has one exact form."""
        variants = self._ids_by_fold.get(str(epg_id or "").casefold(), ())
        if len(variants) != 1:
            return None
        return self._by_exact[variants[0]]

    def is_case_ambiguous(self, epg_id: object) -> bool:
        return str(epg_id or "").casefold() in self.casefold_collision_keys

    def matcher_candidates(
        self, *, explicit_dummy_ids: Iterable[str] = ()
    ) -> list[dict[str, str]]:
        """Return stable Smart-Rules-shaped candidates.

        Matching text is derived from the exact ID in both XML and TXT catalog
        modes. XML display-name changes therefore cannot silently change a
        matching decision between a manual sync and the production build.
        """
        result: list[dict[str, str]] = []
        explicit_dummy_folds = _explicit_dummy_folds(explicit_dummy_ids)
        for channel in self.channels:
            if self.is_case_ambiguous(channel.epg_id) or _is_dummy_xmltv_id(
                channel.epg_id, explicit_dummy_folds
            ):
                # The frozen legacy matcher uses case-folded ID lookup. Keep
                # colliding identities out of unattended matching entirely.
                continue
            display_name = _epg_id_match_name(channel.epg_id)
            result.append(
                {
                    "epg_id": channel.epg_id,
                    "feed": channel.route.feed,
                    "region": channel.route.region,
                    "display_name": display_name,
                    "normalized": _simple_match_key(display_name),
                }
            )
        return result


@dataclass(frozen=True)
class ProgrammeRecord:
    """Selected programme ready for the builder's SQLite spool."""

    channel_key: str
    source_channel_id: str
    start_epoch: int
    stop_epoch: int
    title: str
    subtitle: str
    description: str
    categories: tuple[str, ...]
    quality: int


@dataclass(frozen=True)
class ProgrammeGate:
    """Bounded evidence that an exact candidate has a useful near-term guide."""

    channel_key: str
    distinct_informative_programmes: int
    first_start_epoch: int | None
    latest_stop_epoch: int | None
    passed: bool
    reason: str


@dataclass
class CatalogStreamStats:
    source_bytes: int = 0
    expanded_bytes: int = 0
    total_elements: int = 0
    channel_elements: int = 0
    unique_channel_ids: int = 0
    duplicate_channel_ids: int = 0
    programme_elements: int = 0
    selected_programmes: int = 0
    invalid_start: int = 0
    blank_title: int = 0
    synthesized_stop: int = 0
    outside_window: int = 0
    implausible_gate_programmes: int = 0


@dataclass(frozen=True)
class OnePassCatalogResult:
    source_sha256: str
    catalog: CatalogSnapshot
    programme_gates: Mapping[str, ProgrammeGate]
    requested_fixed_ids: tuple[str, ...]
    requested_provisional_ids: tuple[str, ...]
    resolved_source_ids: Mapping[str, str]
    unresolved_requested_ids: tuple[str, ...]
    stats: CatalogStreamStats


@dataclass
class _ChannelAggregate:
    epg_id: str
    display_names: set[str] = field(default_factory=set)
    icon_urls: set[str] = field(default_factory=set)
    occurrences: int = 0


@dataclass
class _GateAccumulator:
    # XMLTV feeds can repeat one time slot with alternate-language or otherwise
    # different titles.  Those are variants of one programme, not independent
    # schedule evidence, so gate distinctness is based only on the interval.
    signatures: set[tuple[int, int]] = field(default_factory=set)
    first_start: int | None = None
    latest_stop: int | None = None


CatalogSelector = Callable[[CatalogSnapshot], Iterable[str]]
ChannelSink = Callable[[str, CatalogChannel], None]
ProgrammeSink = Callable[[ProgrammeRecord], None]


def _strict_xmltv_id(value: object, *, label: str) -> str:
    """Return an opaque XMLTV ID unchanged, rejecting unsafe normalization."""
    text = str(value or "")
    normalized = streaming.INVALID_XML_RE.sub(
        "", text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    ).strip()
    if normalized != text:
        raise CatalogStreamError(
            f"{label} contains leading/trailing whitespace or control characters."
        )
    if len(text) > MAX_XMLTV_ID_CHARACTERS:
        raise CatalogStreamError(
            f"{label} exceeds the {MAX_XMLTV_ID_CHARACTERS}-character XMLTV ID limit."
        )
    return text


def _explicit_dummy_folds(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        epg_id = _strict_xmltv_id(value, label="explicit_dummy_ids")
        if epg_id:
            result.add(epg_id.casefold())
    return frozenset(result)


def _is_dummy_xmltv_id(
    epg_id: object, explicit_dummy_folds: frozenset[str] = frozenset()
) -> bool:
    value = str(epg_id or "").strip()
    folded = value.casefold()
    return bool(
        folded in KNOWN_DUMMY_XMLTV_IDS
        or folded in explicit_dummy_folds
        or DUMMY_ID_COMPONENT_RE.search(value)
    )


def _simple_match_key(value: object) -> str:
    text = str(value or "").casefold().replace("&", " and ").replace("+", " plus ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _epg_id_match_name(epg_id: object) -> str:
    """Create the same stable human identity from either catalog format."""
    text = str(epg_id or "").strip()
    text = re.sub(
        r"\.(?:us(?:\d+|_locals\d+)?|uk\d*|gb\d*|in\d*|ie\d*|ca\d*|"
        r"au\d*|nz\d*|ch\d*|es\d*|fr\d*|de\d*|pt\d*|za\d*|"
        r"pk\d*|bd\d*|lk\d*|np\d*|ae\d*|bein)$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = text.replace("+", " plus ")
    text = re.sub(r"[._/\\-]+", " ", text)
    return " ".join(text.split())


def infer_catalog_route(epg_id: object) -> CatalogRoute:
    """Infer a stable virtual feed/region without needing per-feed downloads."""
    value = streaming.clean_identifier(epg_id, 300)
    folded = value.casefold()
    if folded.endswith(".us_locals1"):
        return CatalogRoute("US_LOCALS1", "US")
    if folded.endswith(".us2"):
        return CatalogRoute("US2", "US")
    if folded.endswith(".ca2"):
        return CatalogRoute("CA2", "CA")
    if folded.endswith(".uk"):
        return CatalogRoute("UK1", "UK")
    if folded.endswith((".in", ".in2")):
        return CatalogRoute("IN4", "IN")
    if folded.endswith(".bein"):
        return CatalogRoute("BEIN1", "BEIN")

    match = re.search(r"\.([a-z]{2,3})(?:\d+)?$", folded)
    if match is None:
        return CatalogRoute("ALL_UNROUTED", "ALL")
    suffix = match.group(1).upper()
    region = {"GB": "UK"}.get(suffix, suffix)
    return CatalogRoute(f"ALL_{region}", region)


def _catalog_fingerprint(channels: Sequence[CatalogChannel]) -> str:
    digest = hashlib.sha256()
    for channel in channels:
        record = (
            channel.epg_id,
            channel.display_name,
            channel.icon_url,
            channel.occurrences,
            channel.route.feed,
            channel.route.region,
        )
        digest.update(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _freeze_catalog(
    aggregates: Mapping[str, _ChannelAggregate],
    duplicate_elements: int,
    source_sha256: str,
) -> CatalogSnapshot:
    channels: list[CatalogChannel] = []
    ids_by_fold: dict[str, list[str]] = defaultdict(list)
    for epg_id in sorted(aggregates, key=lambda item: (item.casefold(), item)):
        aggregate = aggregates[epg_id]
        display_name = min(
            aggregate.display_names,
            key=lambda item: (item.casefold(), item),
            default="",
        )
        icon_url = min(
            aggregate.icon_urls,
            key=lambda item: (item.casefold(), item),
            default="",
        )
        channel = CatalogChannel(
            epg_id=epg_id,
            display_name=display_name,
            icon_url=icon_url,
            occurrences=aggregate.occurrences,
            route=infer_catalog_route(epg_id),
        )
        channels.append(channel)
        ids_by_fold[epg_id.casefold()].append(epg_id)
    by_exact = {channel.epg_id: channel for channel in channels}
    folded = {
        key: tuple(sorted(values, key=lambda item: (item.casefold(), item)))
        for key, values in ids_by_fold.items()
    }
    collisions = frozenset(key for key, values in folded.items() if len(values) > 1)
    frozen_channels = tuple(channels)
    return CatalogSnapshot(
        channels=frozen_channels,
        source_sha256=source_sha256,
        fingerprint_sha256=_catalog_fingerprint(frozen_channels),
        duplicate_channel_elements=int(duplicate_elements),
        casefold_collision_keys=collisions,
        _by_exact=MappingProxyType(by_exact),
        _ids_by_fold=MappingProxyType(folded),
    )


def parse_all_sources_text(
    content: bytes | str,
    *,
    maximum_bytes: int = MAX_CATALOG_TEXT_BYTES,
    maximum_lines: int = MAX_CATALOG_TEXT_LINES,
    maximum_ids: int = MAX_CATALOG_CHANNEL_ELEMENTS,
    minimum_ids: int = DEFAULT_MINIMUM_UNIQUE_CATALOG_CHANNELS,
) -> TextCatalogSnapshot:
    """Parse EPGShare's sectioned ALL_SOURCES1 ``.txt`` catalog strictly.

    This is the manual-sync companion to :func:`stream_catalog_and_programmes_once`.
    The official file consists of a UTC-like generation token, section headers,
    and one complete XMLTV ID per line. Whole lines are retained so IDs containing
    spaces are not damaged by whitespace tokenization. XML entities are decoded
    once because the corresponding XML attribute is decoded by ``lxml``.
    """
    if minimum_ids < 1 or minimum_ids > maximum_ids:
        raise CatalogStreamError("The text-catalog completeness floor is invalid.")
    if isinstance(content, bytes):
        raw = bytes(content)
        if len(raw) > int(maximum_bytes):
            raise CatalogStreamError("The ALL_SOURCES1 text catalog is too large.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CatalogStreamError(
                "The ALL_SOURCES1 text catalog is not valid UTF-8."
            ) from exc
    else:
        text = str(content or "")
        if len(text.encode("utf-8")) > int(maximum_bytes):
            raise CatalogStreamError("The ALL_SOURCES1 text catalog is too large.")
        text = text.lstrip("\ufeff")
    if text.lstrip()[:100].casefold().startswith(("<!doctype html", "<html")):
        raise CatalogStreamError(
            "The ALL_SOURCES1 text catalog contains HTML instead of channel IDs."
        )

    lines = text.splitlines()
    if len(lines) > int(maximum_lines):
        raise CatalogStreamError("The ALL_SOURCES1 text catalog has too many lines.")
    generated_token = ""
    current_section = ""
    sections_seen: set[str] = set()
    sections_by_id: dict[str, set[str]] = defaultdict(set)
    duplicate_lines = 0
    saw_nonempty = False
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not saw_nonempty:
            saw_nonempty = True
            if not TEXT_GENERATED_RE.fullmatch(line):
                raise CatalogStreamError(
                    "The ALL_SOURCES1 text catalog has no valid generation token."
                )
            generated_token = line
            continue
        section_match = TEXT_SECTION_RE.fullmatch(line)
        if section_match is not None:
            current_section = section_match.group(1).upper()
            sections_seen.add(current_section)
            if len(sections_seen) > MAX_CATALOG_SECTIONS:
                raise CatalogStreamError(
                    "The ALL_SOURCES1 text catalog has too many sections."
                )
            continue
        if not current_section:
            raise CatalogStreamError(
                f"The ALL_SOURCES1 text catalog has an ID before a section at line {line_number}."
            )
        if raw_line != line:
            raise CatalogStreamError(
                f"Text-catalog ID at line {line_number} contains "
                "leading/trailing whitespace or control characters."
            )
        epg_id = _strict_xmltv_id(
            html.unescape(line), label=f"Text-catalog ID at line {line_number}"
        )
        # XMLTV channel IDs are opaque strings.  EPGShare normally uses dotted
        # IDs, but its official catalog also contains legitimate dotless IDs
        # (including IDs with internal spaces), so punctuation is not a valid
        # structural requirement here.
        if not epg_id:
            raise CatalogStreamError(
                f"The ALL_SOURCES1 text catalog has an invalid ID at line {line_number}."
            )
        target_sections = sections_by_id[epg_id]
        before = len(target_sections)
        target_sections.add(current_section)
        if before:
            duplicate_lines += 1
        if len(target_sections) > MAX_CHANNEL_VALUE_VARIANTS:
            raise CatalogStreamError(
                "One XMLTV ID occurs in too many text-catalog sections."
            )
        if len(sections_by_id) > int(maximum_ids):
            raise CatalogStreamError("The ALL_SOURCES1 text catalog has too many IDs.")
    if not generated_token or not sections_seen or not sections_by_id:
        raise CatalogStreamError("The ALL_SOURCES1 text catalog is incomplete.")
    if len(sections_by_id) < int(minimum_ids):
        raise CatalogStreamError(
            "The ALL_SOURCES1 text catalog is below its completeness floor."
        )

    ids_by_fold: dict[str, list[str]] = defaultdict(list)
    entries: list[TextCatalogEntry] = []
    ambiguous_kinds: set[str] = set()
    for epg_id in sorted(sections_by_id, key=lambda item: (item.casefold(), item)):
        sections = tuple(sorted(sections_by_id[epg_id]))
        kinds = {
            "dummy" if section == "DUMMY_CHANNELS" else "real"
            for section in sections
        }
        kind = next(iter(kinds)) if len(kinds) == 1 else "ambiguous"
        if kind == "ambiguous":
            ambiguous_kinds.add(epg_id)
        entries.append(
            TextCatalogEntry(
                epg_id=epg_id,
                sections=sections,
                kind=kind,
                route=infer_catalog_route(epg_id),
            )
        )
        ids_by_fold[epg_id.casefold()].append(epg_id)
    collisions = frozenset(
        key for key, values in ids_by_fold.items() if len(values) > 1
    )
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            json.dumps(
                (entry.epg_id, entry.sections, entry.kind, entry.route.feed, entry.route.region),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return TextCatalogSnapshot(
        generated_token=generated_token,
        entries=tuple(entries),
        fingerprint_sha256=digest.hexdigest(),
        duplicate_id_lines=duplicate_lines,
        casefold_collision_keys=collisions,
        ambiguous_kind_ids=frozenset(ambiguous_kinds),
    )


def informative_programme_title(value: object) -> bool:
    title = streaming.clean_text(value, 2_000)
    normalized = _simple_match_key(title)
    return bool(
        normalized
        and normalized not in PLACEHOLDER_TITLE_KEYS
        and not re.fullmatch(r"(?:n a|na)(?: (?:n a|na))+", normalized)
        and not PLACEHOLDER_TITLE_RE.fullmatch(title)
    )


def _normalize_requested_ids(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        epg_id = _strict_xmltv_id(raw, label=label)
        if not epg_id:
            raise CatalogStreamError(f"{label} contains a blank XMLTV ID.")
        if epg_id in seen:
            continue
        seen.add(epg_id)
        result.append(epg_id)
        if len(result) > streaming.MAX_MAPPING_ROWS:
            raise CatalogStreamError(f"{label} contains too many XMLTV IDs.")
    return tuple(sorted(result, key=lambda item: (item.casefold(), item)))


def _resolve_requested_ids(
    catalog: CatalogSnapshot,
    fixed_ids: Sequence[str],
    provisional_ids: Sequence[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Map exact source IDs to requested builder keys using current safe rules."""
    actual_to_target: dict[str, str] = {}
    unresolved: list[str] = []
    for requested in (*fixed_ids, *provisional_ids):
        exact = catalog.exact(requested)
        if exact is not None:
            actual = exact.epg_id
        else:
            unique = catalog.unique_casefold(requested)
            if unique is None:
                unresolved.append(requested)
                continue
            actual = unique.epg_id
        previous = actual_to_target.get(actual)
        if previous is not None and previous != requested:
            # Two mapping rows must never silently claim the same source identity
            # under differently-cased target IDs.
            raise CatalogStreamError(
                "More than one requested mapping targets the same XMLTV identity."
            )
        actual_to_target[actual] = requested
    return actual_to_target, tuple(unresolved)


def _gate_results(
    selected_keys: Sequence[str],
    accumulators: Mapping[str, _GateAccumulator],
    *,
    now_epoch: int,
    minimum_programmes: int,
    minimum_future_seconds: int,
    maximum_initial_gap_seconds: int,
) -> Mapping[str, ProgrammeGate]:
    results: dict[str, ProgrammeGate] = {}
    required_stop = int(now_epoch) + int(minimum_future_seconds)
    latest_near_term_start = int(now_epoch) + int(maximum_initial_gap_seconds)
    for channel_key in sorted(selected_keys, key=lambda item: (item.casefold(), item)):
        accumulator = accumulators.get(channel_key, _GateAccumulator())
        count = len(accumulator.signatures)
        first = accumulator.first_start
        latest = accumulator.latest_stop
        if first is None or first > latest_near_term_start:
            passed = False
            reason = "No informative programme begins within the near-term safety window."
        elif count < minimum_programmes:
            passed = False
            reason = (
                f"Only {count} distinct informative current/future programme(s); "
                f"{minimum_programmes} required."
            )
        elif latest is None or latest < required_stop:
            passed = False
            reason = "The current/future guide does not extend through the safety horizon."
        else:
            passed = True
            reason = "Exact ID has a non-placeholder current/future programme guide."
        results[channel_key] = ProgrammeGate(
            channel_key=channel_key,
            distinct_informative_programmes=count,
            first_start_epoch=first,
            latest_stop_epoch=latest,
            passed=passed,
            reason=reason,
        )
    return MappingProxyType(results)


def stream_catalog_and_programmes_once(
    *,
    path: Path,
    fixed_wanted_ids: Iterable[str],
    select_provisional_ids: CatalogSelector,
    window_start: int,
    now_epoch: int,
    channel_sink: ChannelSink | None = None,
    programme_sink: ProgrammeSink | None = None,
    source_base_url: str = "",
    maximum_expanded_bytes: int = streaming.MAX_SOURCE_EXPANDED_BYTES,
    maximum_channel_elements: int = MAX_CATALOG_CHANNEL_ELEMENTS,
    minimum_unique_channels: int = DEFAULT_MINIMUM_UNIQUE_CATALOG_CHANNELS,
    gate_horizon_seconds: int = DEFAULT_GATE_HORIZON_SECONDS,
    gate_minimum_programmes: int = DEFAULT_GATE_MINIMUM_PROGRAMMES,
    gate_minimum_future_seconds: int = DEFAULT_GATE_MINIMUM_FUTURE_SECONDS,
    gate_maximum_initial_gap_seconds: int = DEFAULT_GATE_MAXIMUM_INITIAL_GAP_SECONDS,
) -> OnePassCatalogResult:
    """Parse one ALL_SOURCES1 file exactly once and return catalog/gate evidence.

    ``select_provisional_ids`` is invoked once, after all channels and before the
    first programme body is parsed. It must be pure with respect to the XML
    source: Google Sheet writes belong after this function returns successfully.
    The caller may nevertheless retain its own provisional decision objects in
    the callback closure.
    """
    source_path = Path(path)
    if maximum_channel_elements < 1 or maximum_channel_elements > 1_000_000:
        raise CatalogStreamError("The catalog channel-element limit is invalid.")
    if minimum_unique_channels < 1 or minimum_unique_channels > maximum_channel_elements:
        raise CatalogStreamError("The catalog completeness floor is invalid.")
    if gate_horizon_seconds <= 0 or gate_minimum_programmes <= 0:
        raise CatalogStreamError("The programme-gate settings are invalid.")
    if gate_minimum_programmes > MAX_GATE_SIGNATURES_PER_ID:
        raise CatalogStreamError("The programme-gate minimum exceeds its bounded tracker.")
    if gate_minimum_future_seconds < 0:
        raise CatalogStreamError("The programme-gate future horizon is invalid.")
    if gate_maximum_initial_gap_seconds <= 0:
        raise CatalogStreamError("The programme-gate initial-gap window is invalid.")

    fixed_ids = _normalize_requested_ids(fixed_wanted_ids, label="fixed_wanted_ids")
    bound_source = _BoundXmlSource.open(source_path)
    source_sha256 = bound_source.source_sha256
    stats = CatalogStreamStats(source_bytes=bound_source.source_bytes)
    aggregates: dict[str, _ChannelAggregate] = {}
    catalog: CatalogSnapshot | None = None
    provisional_ids: tuple[str, ...] = ()
    actual_to_target: dict[str, str] = {}
    unresolved: tuple[str, ...] = ()
    gates: dict[str, _GateAccumulator] = {}
    selector_called = False
    root_element: etree._Element | None = None
    active_top_level: etree._Element | None = None
    active_child_elements = 0
    programme_phase_started = False
    started = time.monotonic()

    def freeze_and_select() -> None:
        nonlocal catalog, provisional_ids, actual_to_target, unresolved, selector_called
        if selector_called:
            return
        catalog = _freeze_catalog(
            aggregates,
            stats.duplicate_channel_ids,
            source_sha256,
        )
        if len(catalog.channels) < int(minimum_unique_channels):
            raise CatalogStreamError(
                "ALL_SOURCES1 is below its catalog completeness floor."
            )
        try:
            selected = select_provisional_ids(catalog)
        except CatalogStreamError:
            raise
        except Exception as exc:
            raise CatalogStreamError("The catalog selector failed.") from exc
        provisional_ids = _normalize_requested_ids(
            selected, label="select_provisional_ids result"
        )
        missing_provisional = [
            epg_id for epg_id in provisional_ids if catalog.exact(epg_id) is None
        ]
        if missing_provisional:
            raise CatalogStreamError(
                "The catalog selector returned an ID not present exactly in the catalog."
            )
        actual_to_target, unresolved = _resolve_requested_ids(
            catalog, fixed_ids, provisional_ids
        )
        selected_keys = set(actual_to_target.values())
        gates.update({channel_key: _GateAccumulator() for channel_key in selected_keys})
        if channel_sink is not None:
            for actual_id, channel_key in sorted(
                actual_to_target.items(), key=lambda item: (item[1].casefold(), item[1])
            ):
                channel_sink(channel_key, catalog._by_exact[actual_id])
        selector_called = True

    try:
        streaming.reject_unsafe_xml_prefix_handle(bound_source.handle)
        bound_source.verify_path_and_descriptor()
        with streaming.open_limited_xml_handle(
            bound_source.handle,
            maximum_expanded_bytes,
        ) as source:
            context = etree.iterparse(
                source,
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
                        streaming.reject_parsed_doctype(element)
                        if streaming.local_name(element.tag) != "tv":
                            raise CatalogStreamError(
                                "ALL_SOURCES1 is not an XMLTV <tv> document."
                            )
                    elif element.getparent() is root_element:
                        name = streaming.local_name(element.tag)
                        if name not in {"channel", "programme"}:
                            raise CatalogStreamError(
                                f"ALL_SOURCES1 contains unsupported top-level <{name}>."
                            )
                        if name == "channel" and programme_phase_started:
                            raise CatalogStreamError(
                                "ALL_SOURCES1 contains a channel after programmes began."
                            )
                        if name == "programme" and not programme_phase_started:
                            freeze_and_select()
                            programme_phase_started = True
                        active_top_level = element
                        active_child_elements = 0
                    elif active_top_level is not None:
                        active_child_elements += 1
                        if active_child_elements > streaming.MAX_RECORD_CHILD_ELEMENTS:
                            raise CatalogStreamError(
                                "An ALL_SOURCES1 XMLTV record exceeds the child limit."
                            )
                    continue

                if root_element is None or element.getparent() is not root_element:
                    continue
                name = streaming.local_name(element.tag)
                stats.total_elements += 1
                if stats.total_elements > streaming.MAX_SOURCE_ELEMENTS:
                    raise CatalogStreamError(
                        "ALL_SOURCES1 exceeds its top-level element limit."
                    )
                if name == "channel":
                    stats.channel_elements += 1
                    if stats.channel_elements > int(maximum_channel_elements):
                        raise CatalogStreamError(
                            "ALL_SOURCES1 exceeds its channel-element limit."
                        )
                    epg_id = _strict_xmltv_id(
                        element.get("id") or "", label="Channel XMLTV ID"
                    )
                    if epg_id:
                        aggregate = aggregates.get(epg_id)
                        if aggregate is None:
                            aggregate = _ChannelAggregate(epg_id=epg_id)
                            aggregates[epg_id] = aggregate
                            stats.unique_channel_ids += 1
                        else:
                            stats.duplicate_channel_ids += 1
                        aggregate.occurrences += 1
                        if aggregate.occurrences > MAX_DUPLICATE_CHANNEL_RECORDS_PER_ID:
                            raise CatalogStreamError(
                                "One XMLTV channel ID occurs too many times in ALL_SOURCES1."
                            )
                        display_name = streaming.preferred_child_text(
                            element, "display-name"
                        )
                        if display_name:
                            aggregate.display_names.add(display_name)
                            if len(aggregate.display_names) > MAX_CHANNEL_VALUE_VARIANTS:
                                raise CatalogStreamError(
                                    "One XMLTV channel has too many display-name variants."
                                )
                        icon_url = streaming.first_channel_icon(
                            element, source_base_url
                        )
                        if icon_url:
                            aggregate.icon_urls.add(icon_url)
                            if len(aggregate.icon_urls) > MAX_CHANNEL_VALUE_VARIANTS:
                                raise CatalogStreamError(
                                    "One XMLTV channel has too many icon variants."
                                )
                    streaming.release_top_level(element)
                else:
                    stats.programme_elements += 1
                    source_channel_id = _strict_xmltv_id(
                        element.get("channel") or "", label="Programme XMLTV ID"
                    )
                    channel_key = actual_to_target.get(source_channel_id, "")
                    if channel_key:
                        start_epoch = streaming.parse_xmltv_time(element.get("start"))
                        stop_epoch = streaming.parse_xmltv_time(element.get("stop"))
                        original_stop_is_valid = bool(
                            start_epoch is not None
                            and stop_epoch is not None
                            and stop_epoch > start_epoch
                        )
                        title = streaming.preferred_child_text(element, "title")
                        if start_epoch is None:
                            stats.invalid_start += 1
                        elif not title:
                            stats.blank_title += 1
                        else:
                            if stop_epoch is None or stop_epoch <= start_epoch:
                                stop_epoch = start_epoch + 3600
                                stats.synthesized_stop += 1
                            if stop_epoch <= int(window_start):
                                stats.outside_window += 1
                            else:
                                subtitle = streaming.preferred_child_text(
                                    element, "sub-title"
                                )
                                description = streaming.preferred_child_text(
                                    element, "desc"
                                )
                                categories = tuple(
                                    streaming.child_texts(element, "category")
                                )
                                record = ProgrammeRecord(
                                    channel_key=channel_key,
                                    source_channel_id=source_channel_id,
                                    start_epoch=int(start_epoch),
                                    stop_epoch=int(stop_epoch),
                                    title=title,
                                    subtitle=subtitle,
                                    description=description,
                                    categories=categories,
                                    quality=streaming.programme_quality(
                                        title, subtitle, description, categories
                                    ),
                                )
                                if programme_sink is not None:
                                    programme_sink(record)
                                stats.selected_programmes += 1

                                gate_end = int(now_epoch) + int(
                                    gate_horizon_seconds
                                )
                                if (
                                    original_stop_is_valid
                                    and
                                    stop_epoch > int(now_epoch)
                                    and start_epoch < gate_end
                                    and informative_programme_title(title)
                                ):
                                    duration = int(stop_epoch) - int(start_epoch)
                                    maximum_gate_stop = (
                                        gate_end + MAX_GATE_PROGRAMME_DURATION_SECONDS
                                    )
                                    if (
                                        duration <= 0
                                        or duration > MAX_GATE_PROGRAMME_DURATION_SECONDS
                                        or stop_epoch > maximum_gate_stop
                                    ):
                                        stats.implausible_gate_programmes += 1
                                    else:
                                        accumulator = gates[channel_key]
                                        signature = (
                                            int(start_epoch),
                                            int(stop_epoch),
                                        )
                                        if (
                                            signature in accumulator.signatures
                                            or len(accumulator.signatures)
                                            < MAX_GATE_SIGNATURES_PER_ID
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
                if stats.total_elements % streaming.PROGRESS_EVERY_ELEMENTS == 0:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(
                        "ALL_SOURCES1 one-pass: "
                        f"{stats.total_elements:,} records, "
                        f"{stats.selected_programmes:,} selected "
                        f"({stats.total_elements / elapsed:,.0f} records/s)",
                        flush=True,
                    )
            del context
            stats.expanded_bytes = source.count
        bound_source.verify_path_and_descriptor(verify_hash=True)
    except CatalogStreamError:
        raise
    except streaming.BuildError as exc:
        raise CatalogStreamError(str(exc)) from exc
    except (etree.XMLSyntaxError, gzip.BadGzipFile, EOFError, OSError) as exc:
        raise CatalogStreamError(
            "ALL_SOURCES1 XMLTV input is malformed or truncated."
        ) from exc
    finally:
        bound_source.close()

    if root_element is None:
        raise CatalogStreamError("ALL_SOURCES1 XMLTV input is empty.")
    if not selector_called:
        freeze_and_select()
    assert catalog is not None
    selected_keys = tuple(sorted(set(actual_to_target.values())))
    return OnePassCatalogResult(
        source_sha256=source_sha256,
        catalog=catalog,
        programme_gates=_gate_results(
            selected_keys,
            gates,
            now_epoch=int(now_epoch),
            minimum_programmes=int(gate_minimum_programmes),
            minimum_future_seconds=int(gate_minimum_future_seconds),
            maximum_initial_gap_seconds=int(gate_maximum_initial_gap_seconds),
        ),
        requested_fixed_ids=fixed_ids,
        requested_provisional_ids=provisional_ids,
        resolved_source_ids=MappingProxyType(dict(actual_to_target)),
        unresolved_requested_ids=unresolved,
        stats=stats,
    )


__all__ = (
    "CATALOG_STREAM_VERSION",
    "DEFAULT_GATE_MAXIMUM_INITIAL_GAP_SECONDS",
    "DEFAULT_MINIMUM_UNIQUE_CATALOG_CHANNELS",
    "KNOWN_DUMMY_XMLTV_IDS",
    "MAX_GATE_PROGRAMME_DURATION_SECONDS",
    "MAX_XMLTV_ID_CHARACTERS",
    "CatalogChannel",
    "CatalogRoute",
    "CatalogSnapshot",
    "CatalogStreamError",
    "CatalogStreamStats",
    "OnePassCatalogResult",
    "ProgrammeGate",
    "ProgrammeRecord",
    "TextCatalogEntry",
    "TextCatalogSnapshot",
    "infer_catalog_route",
    "informative_programme_title",
    "parse_all_sources_text",
    "stream_catalog_and_programmes_once",
)
