"""Deterministic scheduling for isolated VOD rule evaluations.

Baseline outcomes select controls and strata only. This module supplies content
IDs, never metadata, candidate IDs, title corrections, or identity proof to a
matcher. Every copy of a selected original logical identity remains in scope.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


SAMPLING_POLICY = 2
MAX_RECORDS = 1_000_000
SMOKE_CORE_IDENTITIES = 1200
SMOKE_EXTRA_FORMAT_IDENTITIES = 300
POSITIVE_CONTROLS = 200
MAX_SMOKE_ID_BYTES = 960 * 1024
CONTENT_ID = re.compile(r"(server_[1-9][0-9]?):(movie|series):[0-9]{1,10}\Z")

# These are selection screens, not instructions to strip true title words.
_PREFIX = re.compile(
    r"^(?:AR|BN|DE|EN|ENG|ES|FR|HI|IN|IT|PA|PB|PL|PT|RU|TA|TE|TR|"
    r"EX|NF|LAT|GR|AL|BG|DK|TG|TM|MA|AF|STH|SOM|KD|SC|TOP|PK|IR|IS|SPT|IND)\s+-\s+\S"
)
_PIPE_YEAR = re.compile(r"\s+-\s+(?:19|20)\d{2}\s*\|\s*[A-Za-z][A-Za-z +&/\d.-]{1,45}\s*$", re.I)
_YEAR_PERIOD = re.compile(r"\((?:19|20)\d{2}\)\.\s*$")
_YEAR_WRAPPER = re.compile(r"\((?:19|20)\d{2}\)\s*\([^()]{1,45}\)\s*$")
_DUPLICATE_YEAR = re.compile(r"\(((?:19|20)\d{2})\)\s*\(\1\)\s*$")
_QUALITY = re.compile(r"\b(?:WEB[- .]?DL|WEBRip|BluRay|BDRip|HDRip|DVDRip|HEVC|1080p|720p|2160p|VOSTFR|MULTISUBS)\s*$", re.I)
_NUMERIC = re.compile(r"\d{1,4}(?:\s*\((?:19|20)\d{2}\))?(?:\s*\([A-Za-z ]+\))?\s*\Z")
_SEMANTIC_GUARD = re.compile(r"(?:\b(?:Class of 1984|English Vinglish|Blade Runner 2049|2001:\s*A Space Odyssey|IT:\s*Chapter)\b|\bHIT:)", re.I)
FORMAT_FEATURES = frozenset({"providerTag", "yearPipeLanguage", "yearPeriod", "yearWrapper", "duplicateYear", "qualityTail"})


@dataclass(slots=True)
class _Entry:
    content_id: str
    identity: str
    server: str
    kind: str
    language: str
    reason: str
    status: str
    verified: bool
    features: tuple[str, ...]


@dataclass(slots=True)
class _Group:
    identity: str
    entries: list[_Entry] = field(default_factory=list)

    @property
    def representative(self) -> _Entry:
        return self.entries[0]


def _features(item: Mapping[str, Any]) -> tuple[str, ...]:
    value = item.get("title") or item.get("name")
    title = value[:1000] if isinstance(value, str) else ""
    flags = []
    for name, pattern in (("providerTag", _PREFIX), ("yearPipeLanguage", _PIPE_YEAR),
                          ("yearPeriod", _YEAR_PERIOD), ("yearWrapper", _YEAR_WRAPPER),
                          ("duplicateYear", _DUPLICATE_YEAR), ("qualityTail", _QUALITY)):
        if pattern.search(title):
            flags.append(name)
    unprefixed = re.sub(r"^[A-Z]{2,3}\s+-\s+", "", title)
    if _NUMERIC.fullmatch(unprefixed.strip()):
        flags.append("numericTitle")
    if _SEMANTIC_GUARD.search(title):
        flags.append("semanticTitleGuard")
    if any(ord(character) > 127 for character in title):
        flags.append("unicodeTitle")
    return tuple(flags)


def _language(item: Mapping[str, Any]) -> str:
    values = item.get("languages")
    value = values[0] if isinstance(values, list) and values else item.get("language")
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", value) else "und"


def _reviewed_scopes() -> tuple[frozenset[str], frozenset[str]]:
    path = Path(__file__).resolve().parents[1] / "data/vod_reviewed_corrections.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError("Reviewed control scopes are unavailable or invalid")
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("corrections") if isinstance(document, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Invalid reviewed control scopes")
    result, rejections = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid reviewed control scope")
        value = f"{row.get('serverId')}:{row.get('kind')}:{row.get('recordId')}"
        if not CONTENT_ID.fullmatch(value):
            raise ValueError("Invalid reviewed control identity")
        result.add(value)
        if row.get("rejectIdentity") is True:
            rejections.add(value)
    return frozenset(result), frozenset(rejections)


def _reviewed_ids() -> frozenset[str]:
    return _reviewed_scopes()[0]


def _reviewed_rejection_ids() -> frozenset[str]:
    # A rejected provider ID may now have a valid replacement. These scopes
    # are mandatory regression work, not instructions to reject the record.
    return _reviewed_scopes()[1]


def _balanced(groups: Iterable[_Group], limit: int, rank) -> list[_Group]:
    """Rotate reason, then server/kind, then language and feature stratum."""
    buckets = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for group in groups:
        row = group.representative
        buckets[row.reason][(row.server, row.kind)][(row.language, row.features, row.status)].append(group)
    for server_buckets in buckets.values():
        for strata in server_buckets.values():
            for values in strata.values():
                values.sort(key=lambda group: rank(group.identity), reverse=True)
    reasons = sorted(buckets)
    servers = {reason: sorted(buckets[reason]) for reason in reasons}
    strata = {(reason, server): sorted(buckets[reason][server], key=lambda key: rank(repr(key)))
              for reason in reasons for server in servers[reason]}
    # Rotate the starting server across reasons so each reason's remainder
    # does not systematically favor the first server/kind pair.
    server_offsets = Counter({reason: index for index, reason in enumerate(reasons)})
    stratum_offsets = Counter()
    result = []
    while len(result) < limit:
        progress = False
        for reason in reasons:
            server_keys = servers[reason]
            found = False
            for _ in server_keys:
                server = server_keys[server_offsets[reason] % len(server_keys)]
                server_offsets[reason] += 1
                stratum_keys = strata[(reason, server)]
                for _ in stratum_keys:
                    key = stratum_keys[stratum_offsets[(reason, server)] % len(stratum_keys)]
                    stratum_offsets[(reason, server)] += 1
                    values = buckets[reason][server][key]
                    if values:
                        result.append(values.pop())
                        progress = found = True
                        break
                if found:
                    break
            if len(result) >= limit:
                break
        if not progress:
            break
    return result


def _distribution(entries: Iterable[_Entry], key) -> dict[str, int]:
    return dict(sorted(Counter(key(row) for row in entries).items()))


def select_evaluation(records: Iterable[Mapping[str, Any]], scope: str = "smoke") -> dict[str, Any]:
    if scope not in {"smoke", "full"}:
        raise ValueError("Rule evaluation scope must be smoke or full")
    entries, groups, seen = [], {}, set()
    for record in records:
        if len(entries) >= MAX_RECORDS or not isinstance(record, Mapping):
            raise ValueError("Invalid or oversized evaluation records")
        content_id, identity = record.get("contentId"), record.get("identityKey")
        matched = CONTENT_ID.fullmatch(content_id) if isinstance(content_id, str) else None
        if (matched is None or content_id in seen or record.get("kind") != matched[2]
                or not isinstance(identity, str) or not identity or len(identity) > 1024
                or type(record.get("baselineVerified")) is not bool or not isinstance(record.get("item"), Mapping)):
            raise ValueError("Invalid or duplicated evaluation record identity")
        server, kind = matched.groups()
        if record.get("serverId", server) != server:
            raise ValueError("Evaluation server does not match its content identity")
        reason = record.get("baselineReason")
        reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason) else "unspecified"
        status = record.get("baselineStatus")
        status = status if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,80}", status) else "unspecified"
        row = _Entry(content_id, identity, server, kind, _language(record["item"]), reason, status,
                     record["baselineVerified"], _features(record["item"]))
        group = groups.setdefault(identity, _Group(identity))
        if group.entries and group.entries[0].kind != kind:
            raise ValueError("A logical evaluation identity spans incompatible media kinds")
        group.entries.append(row)
        entries.append(row)
        seen.add(content_id)
    reviewed = _reviewed_ids()
    reviewed_rejections = _reviewed_rejection_ids()
    fingerprint = hashlib.sha256()
    fingerprint.update(f"vod-rule-sampling-v{SAMPLING_POLICY}\n".encode())
    for row in sorted(entries, key=lambda row: row.content_id):
        fingerprint.update(json.dumps((row.content_id, row.identity, row.verified, row.reason, row.status,
                                       row.language, row.features), separators=(",", ":")).encode())
        fingerprint.update(b"\n")
    fingerprint.update(json.dumps(sorted(reviewed), separators=(",", ":")).encode())
    fingerprint.update(json.dumps(sorted(reviewed_rejections), separators=(",", ":")).encode())
    seed = fingerprint.hexdigest()
    rank = lambda value: hashlib.sha256((seed + "\0" + value).encode()).hexdigest()
    for group in groups.values():
        group.entries.sort(key=lambda row: (row.verified, rank(row.content_id)))
    unresolved = [group for group in groups.values() if not all(row.verified for row in group.entries)]
    positives = [group for group in groups.values() if all(row.verified for row in group.entries)]

    controls, controlled = [], set()
    def add_controls(values, maximum):
        if len(controls) >= maximum:
            return
        for group in sorted(values, key=lambda group: rank(group.identity)):
            if group.identity in controlled:
                continue
            controls.append(group)
            controlled.add(group.identity)
            if len(controls) >= maximum:
                break
    add_controls((group for group in positives if any(row.content_id in reviewed for row in group.entries)), POSITIVE_CONTROLS)
    add_controls((group for group in positives if "numericTitle" in group.representative.features), min(POSITIVE_CONTROLS, len(controls) + 30))
    add_controls((group for group in positives if "semanticTitleGuard" in group.representative.features), min(POSITIVE_CONTROLS, len(controls) + 15))
    controls += _balanced((group for group in positives if group.identity not in controlled), POSITIVE_CONTROLS - len(controls), rank)
    controlled = {group.identity for group in controls}
    force_ids = sorted(next((row.content_id for row in group.entries if row.content_id in reviewed),
                            group.representative.content_id) for group in controls)

    core, extra, mandatory = [], [], []
    if scope == "full":
        selected_groups = list(groups.values())
        selected_ids = None
    else:
        core = _balanced(unresolved, SMOKE_CORE_IDENTITIES, rank)
        selected = {group.identity for group in core}
        extra = _balanced((group for group in unresolved if group.identity not in selected
                           and any(FORMAT_FEATURES.intersection(row.features) for row in group.entries)),
                          SMOKE_EXTRA_FORMAT_IDENTITIES, rank)
        selected_groups = core + extra + controls
        selected = {group.identity for group in selected_groups}
        mandatory = sorted((group for group in groups.values() if group.identity not in selected
                            and any(row.content_id in reviewed_rejections for row in group.entries)),
                           key=lambda group: rank(group.identity))
        selected_groups += mandatory
        selected_ids = sorted(row.content_id for group in selected_groups for row in group.entries)
        if len(json.dumps(selected_ids, separators=(",", ":")).encode()) > MAX_SMOKE_ID_BYTES:
            raise ValueError("Smoke group copies exceed the evaluation ID-list bound; use full scope")
    selected_rows = [row for group in selected_groups for row in group.entries]
    control_rows = [next(row for row in group.entries if row.content_id in force_ids) for group in controls]
    summary = {
        "samplingPolicyVersion": SAMPLING_POLICY, "scope": scope, "sourceFingerprint": seed,
        "sourceRecords": len(entries), "sourceLogicalIdentities": len(groups),
        "baselineVerifiedRecords": sum(row.verified for row in entries),
        "baselineUnverifiedRecords": sum(not row.verified for row in entries),
        "unverifiedLogicalIdentities": len(unresolved), "selectedLogicalIdentities": len(selected_groups),
        "selectedContentRecords": len(selected_rows), "coreUnverifiedIdentities": len(core),
        "extraFormatIdentities": len(extra), "forcedPositiveControls": len(force_ids),
        "extraReviewedRejectionIdentities": len(mandatory),
        "availableReviewedRejectionControls": len(reviewed_rejections.intersection(seen)),
        "selectedReviewedRejectionControls": sum(row.content_id in reviewed_rejections for row in selected_rows),
        "selectedByReason": _distribution(selected_rows, lambda row: row.reason),
        "selectedByServerKind": _distribution(selected_rows, lambda row: row.server + ":" + row.kind),
        "selectedByLanguage": _distribution(selected_rows, lambda row: row.language),
        "coreByReason": _distribution((group.representative for group in core), lambda row: row.reason),
        "coreByServerKind": _distribution((group.representative for group in core), lambda row: row.server + ":" + row.kind),
        "controlsByServerKind": _distribution(control_rows, lambda row: row.server + ":" + row.kind),
        "controlsByLanguage": _distribution(control_rows, lambda row: row.language),
        "reviewedPositiveControls": sum(row.content_id in reviewed for row in control_rows),
        "numericPositiveControls": sum("numericTitle" in row.features for row in control_rows),
        "qualification": "IDs select work only. Baseline outcomes are comparison evidence, never matcher hints or proof. Smoke strata are not a population match-rate estimate.",
    }
    return {"selectedContentIds": selected_ids, "forceRecheckContentIds": force_ids, "summary": summary}
