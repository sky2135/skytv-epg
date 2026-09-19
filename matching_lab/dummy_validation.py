"""Strict, read-only validation for private dummy-shadow bundles.

The dummy sidecar is evidence for review, not an apply artifact.  This module
therefore accepts only the exact canonical v1 bundle emitted by
``matching_lab.dummy_shadow`` and rejects any record that claims write or apply
authority.  It has no network client and no mutation path.

Dummy manifest v1 predates an explicit ``expires_at`` field.  Validation gives
it the same conservative six-hour lifetime as the main Matching Lab policy,
derived from its content-bound ``generated_at`` timestamp.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .artifacts import (
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSAL_LINE_BYTES,
    package_code_sha256,
    read_stable_regular_file,
    strict_json_loads,
)
from .compat import streaming, sync
from .dummy_shadow import (
    DUMMY_CLASSIFICATION_SCHEMA,
    DUMMY_MANIFEST_SCHEMA,
    DUMMY_SUMMARY_SCHEMA,
    MAX_CLASSIFICATIONS,
    SUPPORTED_SERVERS,
)
from .models import (
    ContractError,
    canonical_json_bytes,
    require_opaque_identifier,
    require_sha256,
    safe_display_text,
    safe_text,
    sha256_bytes,
    sha256_json,
)
from .policy import DEFAULT_POLICY
from .retrieval import mapping_row_guard, provider_identity_guard
from skytv_epg_auto_match_v1 import (
    SAFE_DUMMY_METHODS,
    _has_exact_numbered_event_bank_evidence,
    _safe_dummy_classification,
)


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SUMMARY_BYTES = 1024 * 1024
_MAX_TEXT_CATALOG_AGE_SECONDS = 48 * 60 * 60
_MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS = 6 * 60 * 60

_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "run_id",
        "generated_at",
        "servers",
        "input_sha256",
        "lab_code_sha256",
        "classification_count",
        "counts",
        "private_artifact",
        "write_authority",
        "classifications_sha256",
        "summary_sha256",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "run_id",
        "generated_at",
        "private_details_emitted",
        "write_authority",
        "counts",
    }
)
_CLASSIFICATION_FIELDS = frozenset(
    {
        "classification_id",
        "schema",
        "run_id",
        "created_at",
        "identity",
        "decision",
        "evidence",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "server_id",
        "stream_id",
        "channel_name",
        "category_name",
        "row_guard_sha256",
        "provider_identity_sha256",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "state",
        "action",
        "source",
        "epg_feed",
        "epg_id",
        "method",
        "matcher_reason",
        "reason_codes",
        "apply_eligible",
        "write_authority",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "source_sha256",
        "xml_catalog_sha256",
        "text_file_sha256",
        "text_catalog_sha256",
        "text_generated",
        "mapping_table_sha256",
        "alerts_file_sha256",
        "open_alerts_sha256",
        "lab_code_sha256",
        "matcher_version",
        "matcher_build_id",
        "matcher_sha256",
        "matcher_engine_sha256",
        "approved_aliases_sha256",
        "schedule_equivalences_sha256",
    }
)
_INPUT_HASH_FIELDS = frozenset(
    {
        "MAPPING_FILE",
        "MAPPING_TABLE",
        "ALERTS_FILE",
        "OPEN_ALERT_KEYS",
        "EPG_XML",
        "EPG_XML_CATALOG",
        "EPG_TEXT",
        "EPG_TEXT_CATALOG",
    }
)
_COUNT_FIELDS = frozenset(
    {
        "REVIEW_ROWS",
        "DUMMY_CLASSIFIED",
        "DUMMY_UNBLOCKED",
        "DUMMY_BLOCKED_ALERT",
        "DUMMY_REJECTED",
    }
)
_BASE_REASON_CODES = frozenset(
    {
        "EXACT_XML_TEXT_DUMMY_ID",
        "SHADOW_ONLY_NO_WRITE_AUTHORITY",
        "VERIFIED_DUMMY_CLASSIFICATION",
    }
)
_EXACT_NUMBERED_EVENT_BANK_METHOD = "exact_numbered_event_bank"
_EXACT_NUMBERED_EVENT_BANK_IDS = frozenset(
    {"espn+.dummy.us", "flo.events.dummy.us"}
)
_ALLOWED_METHODS = frozenset(SAFE_DUMMY_METHODS) | {
    _EXACT_NUMBERED_EVENT_BANK_METHOD
}


@dataclass(frozen=True, slots=True)
class DummyClassificationRecord:
    """Immutable validated view of one dummy-shadow classification."""

    classification_id: str
    run_id: str
    created_at: str
    server_id: str
    stream_id: str
    channel_name: str
    category_name: str
    row_guard_sha256: str
    provider_identity_sha256: str
    state: str
    epg_id: str
    method: str
    matcher_reason: str
    reason_codes: tuple[str, ...]
    evidence: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            MappingProxyType(dict(sorted(dict(self.evidence).items()))),
        )


@dataclass(frozen=True, slots=True)
class DummyValidationResult:
    """Verified, non-mutating view of one private dummy-shadow bundle."""

    bundle_dir: Path
    run_id: str
    generated_at: str
    expires_at: str
    validated_as_of: str
    classification_count: int
    counts: Mapping[str, int]
    classifications: tuple[DummyClassificationRecord, ...]
    mappings_checked: bool
    alerts_checked: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "counts", MappingProxyType(dict(sorted(dict(self.counts).items())))
        )
        object.__setattr__(self, "classifications", tuple(self.classifications))


@dataclass(frozen=True, slots=True)
class _ParsedManifest:
    run_id: str
    generated_at: str
    servers: tuple[str, ...]
    input_sha256: Mapping[str, str]
    lab_code_sha256: str
    classification_count: int
    counts: Mapping[str, int]
    classifications_sha256: str
    summary_sha256: str


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{label} must be a JSON object.")
    return value


def _list(value: object, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise ContractError(f"{label} must be a JSON array.")
    return value


def _string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ContractError(f"{label} must be a string.")
    return value


def _integer(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractError(f"{label} must be a non-negative integer.")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{label} must be exactly boolean.")
    return value


def _exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    details: list[str] = []
    missing = sorted(expected.difference(actual))
    unknown = sorted(actual.difference(expected))
    if missing:
        details.append("missing=" + ",".join(missing))
    if unknown:
        details.append("unknown=" + ",".join(unknown))
    raise ContractError(f"{label} has invalid fields ({'; '.join(details)}).")


def _digest(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    normalized = require_sha256(text, label=label)
    if text != normalized:
        raise ContractError(f"{label} is not canonical lowercase SHA-256.")
    return normalized


def _normalized_text(
    value: object, *, label: str, maximum: int, blank: bool = False
) -> str:
    text = _string(value, label=label)
    normalized = safe_text(text, maximum=maximum)
    if text != normalized or (not blank and not text):
        raise ContractError(f"{label} is blank or not normalized.")
    return text


def _display_text(
    value: object, *, label: str, maximum: int, blank: bool = False
) -> str:
    text = _string(value, label=label)
    normalized = safe_display_text(text, maximum=maximum)
    if text != normalized or (not blank and not text):
        raise ContractError(f"{label} is blank or not normalized.")
    return text


def _matcher_reason(value: object, *, label: str) -> str:
    text = _normalized_text(value, label=label, maximum=500)
    if text != re.sub(r"\s+", " ", text.strip()):
        raise ContractError(f"{label} is not in canonical bounded form.")
    return text


def _parse_utc(value: object, *, label: str) -> datetime:
    text = _string(value, label=label)
    if not text.endswith("Z"):
        raise ContractError(f"{label} must be canonical UTC ending in Z.")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is not an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError(f"{label} must use UTC.")
    parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if parsed.microsecond or text != canonical:
        raise ContractError(f"{label} must use canonical whole-second UTC form.")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_as_of(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ContractError("Validation as-of must include the UTC timezone.")
        if value.microsecond:
            raise ContractError("Validation as-of must use whole seconds.")
        return value.astimezone(timezone.utc)
    return _parse_utc(value, label="validation as-of")


def _parse_counts(value: object, *, label: str) -> dict[str, int]:
    raw = _mapping(value, label=label)
    _exact_fields(raw, _COUNT_FIELDS, label=label)
    return {
        name: _integer(raw[name], label=f"{label} {name}")
        for name in sorted(_COUNT_FIELDS)
    }


def _canonical_document(
    path: Path, *, maximum_bytes: int, label: str
) -> tuple[dict[str, Any], bytes, str]:
    content, digest = read_stable_regular_file(path, maximum_bytes=maximum_bytes)
    parsed = strict_json_loads(content)
    value = _mapping(parsed, label=label)
    if content != canonical_json_bytes(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON with one final newline.")
    return value, content, digest


def _parse_servers(value: object) -> tuple[str, ...]:
    raw = _list(value, label="manifest servers")
    servers = tuple(
        _normalized_text(item, label="manifest server", maximum=40)
        for item in raw
    )
    if (
        not servers
        or tuple(sorted(set(servers))) != servers
        or not set(servers).issubset(SUPPORTED_SERVERS)
    ):
        raise ContractError("The dummy manifest has an invalid server scope.")
    return servers


def _parse_manifest(raw: Mapping[str, Any]) -> _ParsedManifest:
    _exact_fields(raw, _MANIFEST_FIELDS, label="manifest")
    if _string(raw["schema"], label="manifest schema") != DUMMY_MANIFEST_SCHEMA:
        raise ContractError("The dummy manifest schema is unsupported.")
    if _string(raw["mode"], label="manifest mode") != "dummy-shadow":
        raise ContractError("Only read-only dummy-shadow bundles are supported.")
    if not _boolean(raw["private_artifact"], label="manifest private_artifact"):
        raise ContractError("A dummy bundle must remain a private artifact.")
    if _boolean(raw["write_authority"], label="manifest write_authority"):
        raise ContractError("A dummy bundle cannot claim write authority.")

    generated_at = _string(raw["generated_at"], label="manifest generated_at")
    _parse_utc(generated_at, label="manifest generated_at")
    servers = _parse_servers(raw["servers"])
    inputs_raw = _mapping(raw["input_sha256"], label="manifest input hashes")
    _exact_fields(inputs_raw, _INPUT_HASH_FIELDS, label="manifest input hashes")
    inputs = {
        name: _digest(inputs_raw[name], label=f"manifest input {name}")
        for name in sorted(_INPUT_HASH_FIELDS)
    }
    lab_code_sha256 = _digest(
        raw["lab_code_sha256"], label="manifest lab-code hash"
    )
    current_code = package_code_sha256(Path(__file__).resolve().parent)
    if lab_code_sha256 != current_code:
        raise ContractError("The dummy bundle was produced by different Matching Lab code.")
    classification_count = _integer(
        raw["classification_count"], label="manifest classification_count"
    )
    if classification_count > MAX_CLASSIFICATIONS:
        raise ContractError("The dummy classification count exceeds its safety limit.")
    counts = _parse_counts(raw["counts"], label="manifest counts")
    if counts["REVIEW_ROWS"] > MAX_CLASSIFICATIONS:
        raise ContractError("The dummy REVIEW count exceeds its safety limit.")
    classifications_sha256 = _digest(
        raw["classifications_sha256"], label="manifest classifications hash"
    )
    summary_sha256 = _digest(
        raw["summary_sha256"], label="manifest summary hash"
    )
    run_id = _digest(raw["run_id"], label="manifest run_id")
    expected_run_id = sha256_json(
        {
            "schema": DUMMY_MANIFEST_SCHEMA,
            "mode": "dummy-shadow",
            "generated_at": generated_at,
            "servers": list(servers),
            "input_sha256": inputs,
            "lab_code_sha256": lab_code_sha256,
        }
    )
    if run_id != expected_run_id:
        raise ContractError("The dummy manifest run_id does not bind its inputs.")
    return _ParsedManifest(
        run_id=run_id,
        generated_at=generated_at,
        servers=servers,
        input_sha256=MappingProxyType(inputs),
        lab_code_sha256=lab_code_sha256,
        classification_count=classification_count,
        counts=MappingProxyType(counts),
        classifications_sha256=classifications_sha256,
        summary_sha256=summary_sha256,
    )


def _parse_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    _exact_fields(raw, _SUMMARY_FIELDS, label="summary")
    if _string(raw["schema"], label="summary schema") != DUMMY_SUMMARY_SCHEMA:
        raise ContractError("The dummy summary schema is unsupported.")
    if _string(raw["mode"], label="summary mode") != "dummy-shadow":
        raise ContractError("The dummy summary is not in shadow mode.")
    if _boolean(
        raw["private_details_emitted"], label="summary private_details_emitted"
    ):
        raise ContractError("The aggregate dummy summary claims private details.")
    if _boolean(raw["write_authority"], label="summary write_authority"):
        raise ContractError("The dummy summary cannot claim write authority.")
    generated_at = _string(raw["generated_at"], label="summary generated_at")
    _parse_utc(generated_at, label="summary generated_at")
    return {
        "run_id": _digest(raw["run_id"], label="summary run_id"),
        "generated_at": generated_at,
        "counts": _parse_counts(raw["counts"], label="summary counts"),
    }


def _parse_text_generation(value: object, *, label: str) -> tuple[str, datetime]:
    token = _string(value, label=label)
    formats = {12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    format_string = formats.get(len(token))
    if format_string is None or not token.isascii() or not token.isdigit():
        raise ContractError(f"{label} is not a valid catalog generation token.")
    try:
        parsed = datetime.strptime(token, format_string).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ContractError(f"{label} is not a valid catalog generation token.") from exc
    return token, parsed


def _parse_classification(
    raw: object, *, line_number: int
) -> DummyClassificationRecord:
    label = f"classification line {line_number}"
    value = _mapping(raw, label=label)
    _exact_fields(value, _CLASSIFICATION_FIELDS, label=label)
    if _string(value["schema"], label=f"{label} schema") != DUMMY_CLASSIFICATION_SCHEMA:
        raise ContractError(f"{label} has an unsupported schema.")

    identity = _mapping(value["identity"], label=f"{label} identity")
    decision = _mapping(value["decision"], label=f"{label} decision")
    evidence = _mapping(value["evidence"], label=f"{label} evidence")
    _exact_fields(identity, _IDENTITY_FIELDS, label=f"{label} identity")
    _exact_fields(decision, _DECISION_FIELDS, label=f"{label} decision")
    _exact_fields(evidence, _EVIDENCE_FIELDS, label=f"{label} evidence")

    classification_id = _digest(
        value["classification_id"], label=f"{label} classification_id"
    )
    unsigned = dict(value)
    del unsigned["classification_id"]
    if classification_id != sha256_json(unsigned):
        raise ContractError(f"{label} content does not match classification_id.")

    run_id = _digest(value["run_id"], label=f"{label} run_id")
    created_at = _string(value["created_at"], label=f"{label} created_at")
    _parse_utc(created_at, label=f"{label} created_at")
    server_id = _normalized_text(
        identity["server_id"], label=f"{label} server_id", maximum=40
    )
    if server_id not in SUPPORTED_SERVERS:
        raise ContractError(f"{label} has an unsupported server ID.")
    stream_id = require_opaque_identifier(
        _string(identity["stream_id"], label=f"{label} stream_id"),
        label=f"{label} stream_id",
        maximum=120,
    )
    channel_name = _display_text(
        identity["channel_name"], label=f"{label} channel_name", maximum=300
    )
    category_name = _display_text(
        identity["category_name"],
        label=f"{label} category_name",
        maximum=200,
        blank=True,
    )
    row_guard = _digest(
        identity["row_guard_sha256"], label=f"{label} row guard"
    )
    provider_guard = _digest(
        identity["provider_identity_sha256"],
        label=f"{label} provider identity",
    )

    state = _string(decision["state"], label=f"{label} state")
    if state not in {"CLASSIFIED", "BLOCKED_ALERT"}:
        raise ContractError(f"{label} has an unsupported state.")
    fixed_values = {
        "action": "AUTO_DUMMY",
        "source": "dummy",
        "epg_feed": "DUMMY_CHANNELS",
    }
    for field, expected in fixed_values.items():
        if _string(decision[field], label=f"{label} {field}") != expected:
            raise ContractError(f"{label} has an unsafe {field}.")
    epg_id = require_opaque_identifier(
        _string(decision["epg_id"], label=f"{label} EPG ID"),
        label=f"{label} EPG ID",
        maximum=300,
    )
    method = _normalized_text(
        decision["method"], label=f"{label} method", maximum=80
    )
    if method not in _ALLOWED_METHODS:
        raise ContractError(f"{label} has an unsupported dummy method.")
    if (
        epg_id.casefold() in _EXACT_NUMBERED_EVENT_BANK_IDS
        and method != _EXACT_NUMBERED_EVENT_BANK_METHOD
    ):
        raise ContractError(f"{label} uses a numbered-bank ID outside its exact rule.")
    matcher_reason = _matcher_reason(
        decision["matcher_reason"],
        label=f"{label} matcher_reason",
    )
    safe_classification = (
        _has_exact_numbered_event_bank_evidence(
            epg_id=epg_id,
            channel_name=channel_name,
            category_name=category_name,
        )
        if method == _EXACT_NUMBERED_EVENT_BANK_METHOD
        else _safe_dummy_classification(
            method=method,
            epg_id=epg_id,
            matcher_reason=matcher_reason,
            channel_name=channel_name,
            category_name=category_name,
        )
    )
    if not safe_classification:
        raise ContractError(f"{label} lacks independently verified dummy evidence.")
    reasons_raw = _list(decision["reason_codes"], label=f"{label} reason codes")
    reasons = tuple(
        _normalized_text(item, label=f"{label} reason code", maximum=80)
        for item in reasons_raw
    )
    expected_reasons = set(_BASE_REASON_CODES)
    if state == "BLOCKED_ALERT":
        expected_reasons.add("OPEN_SYNC_ALERT")
    if reasons != tuple(sorted(expected_reasons)):
        raise ContractError(f"{label} has inconsistent reason codes.")
    if _boolean(decision["apply_eligible"], label=f"{label} apply_eligible"):
        raise ContractError(f"{label} cannot be apply-eligible.")
    if _boolean(decision["write_authority"], label=f"{label} write_authority"):
        raise ContractError(f"{label} cannot claim write authority.")

    evidence_hashes = {
        name: _digest(evidence[name], label=f"{label} evidence {name}")
        for name in (
            "source_sha256",
            "xml_catalog_sha256",
            "text_file_sha256",
            "text_catalog_sha256",
            "mapping_table_sha256",
            "alerts_file_sha256",
            "open_alerts_sha256",
            "lab_code_sha256",
            "matcher_sha256",
            "matcher_engine_sha256",
            "approved_aliases_sha256",
            "schedule_equivalences_sha256",
        )
    }
    text_generated, _generated = _parse_text_generation(
        evidence["text_generated"], label=f"{label} text_generated"
    )
    evidence_values = {
        **evidence_hashes,
        "text_generated": text_generated,
        "matcher_version": _normalized_text(
            evidence["matcher_version"],
            label=f"{label} matcher_version",
            maximum=120,
        ),
        "matcher_build_id": _normalized_text(
            evidence["matcher_build_id"],
            label=f"{label} matcher_build_id",
            maximum=200,
        ),
    }
    return DummyClassificationRecord(
        classification_id=classification_id,
        run_id=run_id,
        created_at=created_at,
        server_id=server_id,
        stream_id=stream_id,
        channel_name=channel_name,
        category_name=category_name,
        row_guard_sha256=row_guard,
        provider_identity_sha256=provider_guard,
        state=state,
        epg_id=epg_id,
        method=method,
        matcher_reason=matcher_reason,
        reason_codes=reasons,
        evidence=evidence_values,
    )


def _stream_sort_key(value: object) -> tuple[int, int | str, str]:
    text = str(value or "")
    if text.isdigit():
        return 0, int(text), text
    return 1, text.casefold(), text


def _read_classifications(
    path: Path,
) -> tuple[tuple[DummyClassificationRecord, ...], bytes, str]:
    content, digest = read_stable_regular_file(path, maximum_bytes=MAX_PROPOSAL_BYTES)
    if not content:
        return (), content, digest
    if not content.endswith(b"\n"):
        raise ContractError("classifications.jsonl must end with one canonical newline.")
    records: list[DummyClassificationRecord] = []
    for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
        if len(line) > MAX_PROPOSAL_LINE_BYTES:
            raise ContractError("One dummy classification exceeds its line-size limit.")
        if line == b"\n" or not line.endswith(b"\n"):
            raise ContractError(
                "classifications.jsonl contains a blank or unterminated line."
            )
        raw_content = line[:-1]
        raw = strict_json_loads(raw_content)
        if raw_content != canonical_json_bytes(raw):
            raise ContractError(
                f"Classification line {line_number} is not canonical JSON."
            )
        records.append(_parse_classification(raw, line_number=line_number))
        if len(records) > MAX_CLASSIFICATIONS:
            raise ContractError("The dummy artifact exceeds its record limit.")
    ids = [record.classification_id for record in records]
    if len(ids) != len(set(ids)):
        raise ContractError("The dummy artifact repeats a classification_id.")
    identities = [(record.server_id, record.stream_id) for record in records]
    if len(identities) != len(set(identities)):
        raise ContractError("The dummy artifact repeats a stream identity.")
    expected_order = sorted(
        identities, key=lambda key: (key[0], _stream_sort_key(key[1]))
    )
    if identities != expected_order:
        raise ContractError("Dummy classifications are not in deterministic order.")
    return tuple(records), content, digest


def _validate_cross_artifact_contract(
    manifest: _ParsedManifest,
    summary: Mapping[str, Any],
    records: Sequence[DummyClassificationRecord],
    *,
    as_of: datetime,
) -> datetime:
    generated = _parse_utc(manifest.generated_at, label="manifest generated_at")
    expires = generated + timedelta(seconds=DEFAULT_POLICY.expiry_seconds)
    if as_of < generated:
        raise ContractError("The dummy bundle was generated after validation as-of.")
    if as_of >= expires:
        raise ContractError("The dummy bundle has expired.")
    if summary["run_id"] != manifest.run_id:
        raise ContractError("The dummy summary and manifest run IDs differ.")
    if summary["generated_at"] != manifest.generated_at:
        raise ContractError("The dummy summary and manifest generation times differ.")
    if dict(summary["counts"]) != dict(manifest.counts):
        raise ContractError("The dummy summary and manifest counts differ.")
    if manifest.classification_count != len(records):
        raise ContractError("The dummy manifest classification count is incorrect.")
    if manifest.counts["DUMMY_CLASSIFIED"] != len(records):
        raise ContractError("DUMMY_CLASSIFIED does not match the artifact.")
    state_counts = Counter(record.state for record in records)
    if manifest.counts["DUMMY_UNBLOCKED"] != state_counts["CLASSIFIED"]:
        raise ContractError("DUMMY_UNBLOCKED does not match record states.")
    if manifest.counts["DUMMY_BLOCKED_ALERT"] != state_counts["BLOCKED_ALERT"]:
        raise ContractError("DUMMY_BLOCKED_ALERT does not match record states.")
    if (
        manifest.counts["DUMMY_UNBLOCKED"]
        + manifest.counts["DUMMY_BLOCKED_ALERT"]
        != manifest.counts["DUMMY_CLASSIFIED"]
    ):
        raise ContractError("The dummy classification counts do not reconcile.")
    if (
        manifest.counts["DUMMY_CLASSIFIED"] > manifest.counts["REVIEW_ROWS"]
        or manifest.counts["DUMMY_REJECTED"] > manifest.counts["REVIEW_ROWS"]
        or manifest.counts["DUMMY_CLASSIFIED"]
        + manifest.counts["DUMMY_REJECTED"]
        > manifest.counts["REVIEW_ROWS"]
    ):
        raise ContractError("The dummy counts exceed the REVIEW backlog.")

    runtime_evidence: set[tuple[str, ...]] = set()
    generation_tokens: set[str] = set()
    for record in records:
        if record.run_id != manifest.run_id:
            raise ContractError("A dummy classification belongs to another run_id.")
        if record.created_at != manifest.generated_at:
            raise ContractError("A dummy classification has a different creation time.")
        if record.server_id not in manifest.servers:
            raise ContractError("A dummy classification is outside the server scope.")
        expected_evidence = {
            "source_sha256": manifest.input_sha256["EPG_XML"],
            "xml_catalog_sha256": manifest.input_sha256["EPG_XML_CATALOG"],
            "text_file_sha256": manifest.input_sha256["EPG_TEXT"],
            "text_catalog_sha256": manifest.input_sha256["EPG_TEXT_CATALOG"],
            "mapping_table_sha256": manifest.input_sha256["MAPPING_TABLE"],
            "alerts_file_sha256": manifest.input_sha256["ALERTS_FILE"],
            "open_alerts_sha256": manifest.input_sha256["OPEN_ALERT_KEYS"],
            "lab_code_sha256": manifest.lab_code_sha256,
        }
        for name, expected in expected_evidence.items():
            if record.evidence[name] != expected:
                raise ContractError(
                    f"A dummy classification has different {name} evidence."
                )
        generation_tokens.add(record.evidence["text_generated"])
        runtime_evidence.add(
            tuple(
                record.evidence[name]
                for name in (
                    "matcher_version",
                    "matcher_build_id",
                    "matcher_sha256",
                    "matcher_engine_sha256",
                    "approved_aliases_sha256",
                    "schedule_equivalences_sha256",
                )
            )
        )
    if len(generation_tokens) > 1:
        raise ContractError("Dummy classifications contain multiple catalog generations.")
    if len(runtime_evidence) > 1:
        raise ContractError("Dummy classifications contain different matcher runtimes.")
    if generation_tokens:
        token = next(iter(generation_tokens))
        _token, token_time = _parse_text_generation(
            token, label="dummy catalog generation"
        )
        catalog_age = int((generated - token_time).total_seconds())
        if (
            catalog_age > _MAX_TEXT_CATALOG_AGE_SECONDS
            or catalog_age < -_MAX_TEXT_CATALOG_FUTURE_SKEW_SECONDS
        ):
            raise ContractError("The dummy catalog generation is not current.")
    return expires


def _read_mappings(path: Path) -> tuple[Any, str]:
    content, digest = read_stable_regular_file(
        path, maximum_bytes=streaming.MAX_MAPPING_BYTES
    )
    try:
        table = sync.parse_mapping_csv(content)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    return table, digest


def _disabled_review(row: Mapping[str, Any]) -> bool:
    if streaming.clean_text(row.get("action", ""), 40).upper() != "REVIEW":
        return False
    try:
        enabled = streaming.parse_bool(
            row.get("enabled", ""), default=False, field_name="enabled"
        )
    except streaming.BuildError as exc:
        raise ContractError("A current Mapping row has invalid enabled state.") from exc
    return not enabled


def _validate_current_mappings(
    path: Path,
    manifest: _ParsedManifest,
    records: Sequence[DummyClassificationRecord],
) -> None:
    table, digest = _read_mappings(path)
    if digest != manifest.input_sha256["MAPPING_FILE"]:
        raise ContractError("The current Mappings file differs from the dummy input.")
    if sync.mapping_table_fingerprint(table) != manifest.input_sha256["MAPPING_TABLE"]:
        raise ContractError("The current Mappings fingerprint differs from the dummy input.")
    rows_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    scoped_review: list[tuple[str, str]] = []
    for row in table.rows:
        key = (
            streaming.normalize_server_id(row.get("server_id", "")),
            streaming.clean_identifier(row.get("stream_id", ""), 120),
        )
        if key in rows_by_key:
            raise ContractError("Current Mappings contains a duplicate stream identity.")
        rows_by_key[key] = row
        if key[0] in manifest.servers and _disabled_review(row):
            scoped_review.append(key)
    if len(scoped_review) != len(set(scoped_review)) or len(scoped_review) != manifest.counts[
        "REVIEW_ROWS"
    ]:
        raise ContractError("The current disabled REVIEW backlog count differs.")
    scoped_set = set(scoped_review)
    for record in records:
        key = (record.server_id, record.stream_id)
        row = rows_by_key.get(key)
        if row is None or key not in scoped_set:
            raise ContractError("A dummy target is not a current disabled REVIEW row.")
        current_channel = safe_display_text(
            streaming.clean_text(row.get("channel_name", ""), 300), maximum=300
        )
        current_category = safe_display_text(
            streaming.clean_text(row.get("category_name", ""), 200), maximum=200
        )
        if (
            record.channel_name != current_channel
            or record.category_name != current_category
        ):
            raise ContractError("A dummy target identity differs from current Mappings.")
        if mapping_row_guard(row) != record.row_guard_sha256:
            raise ContractError("A dummy row guard differs from current Mappings.")
        if provider_identity_guard(row) != record.provider_identity_sha256:
            raise ContractError("A dummy provider-identity guard is stale.")


def _read_alerts(path: Path) -> tuple[list[dict[str, str]], str]:
    content, digest = read_stable_regular_file(
        path, maximum_bytes=sync.MAX_SYNC_ALERT_BYTES
    )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("The Sync Alerts CSV is not UTF-8.") from exc
    try:
        alerts = sync.parse_sync_alert_values(
            list(csv.reader(io.StringIO(text, newline="")))
        )
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    return alerts, digest


def _validate_current_alerts(
    path: Path,
    manifest: _ParsedManifest,
    records: Sequence[DummyClassificationRecord],
) -> None:
    alerts, digest = _read_alerts(path)
    if digest != manifest.input_sha256["ALERTS_FILE"]:
        raise ContractError("The current Sync Alerts file differs from the dummy input.")
    try:
        open_keys = sync.open_alert_quarantine_keys(alerts)
    except sync.SyncError as exc:
        raise ContractError(str(exc)) from exc
    open_digest = sha256_json(
        [[server_id, stream_id] for server_id, stream_id in sorted(open_keys)]
    )
    if open_digest != manifest.input_sha256["OPEN_ALERT_KEYS"]:
        raise ContractError("The current OPEN-alert set differs from the dummy input.")
    for record in records:
        is_open = (record.server_id, record.stream_id) in open_keys
        is_blocked = record.state == "BLOCKED_ALERT"
        if is_open != is_blocked:
            raise ContractError("A dummy classification has stale OPEN-alert state.")


def validate_dummy_bundle(
    bundle_dir: Path,
    *,
    mappings_csv: Path | None = None,
    alerts_csv: Path | None = None,
    as_of: str | datetime | None = None,
) -> DummyValidationResult:
    """Validate one private dummy-shadow bundle without performing any write.

    Optional Mapping and Sync Alert snapshots must be byte-identical to the
    inputs bound into the bundle.  When supplied, all row guards and emitted
    alert decisions are rechecked against their current parsed state.
    """

    target = Path(bundle_dir)
    try:
        target.lstat()
    except OSError as exc:
        raise ContractError("The dummy bundle directory is unavailable.") from exc
    if target.is_symlink() or not target.is_dir():
        raise ContractError("The dummy bundle path must be a real directory.")
    expected_names = {"manifest.json", "classifications.jsonl", "summary.json"}
    try:
        initial_names = {entry.name for entry in target.iterdir()}
    except OSError as exc:
        raise ContractError("The dummy bundle directory cannot be listed.") from exc
    if initial_names != expected_names:
        raise ContractError("The dummy bundle must contain exactly three artifacts.")

    manifest_raw, manifest_content, _manifest_digest = _canonical_document(
        target / "manifest.json",
        maximum_bytes=MAX_MANIFEST_BYTES,
        label="manifest.json",
    )
    summary_raw, summary_content, summary_digest = _canonical_document(
        target / "summary.json",
        maximum_bytes=MAX_SUMMARY_BYTES,
        label="summary.json",
    )
    records, classifications_content, classifications_digest = _read_classifications(
        target / "classifications.jsonl"
    )
    manifest = _parse_manifest(manifest_raw)
    if sha256_bytes(classifications_content) != classifications_digest:
        raise ContractError("The classifications file changed during validation.")
    if classifications_digest != manifest.classifications_sha256:
        raise ContractError("The classifications hash differs from the manifest.")
    if sha256_bytes(summary_content) != summary_digest:
        raise ContractError("The dummy summary changed during validation.")
    if summary_digest != manifest.summary_sha256:
        raise ContractError("The summary hash differs from the manifest.")
    summary = _parse_summary(summary_raw)
    validated_at = _parse_as_of(as_of)
    expires = _validate_cross_artifact_contract(
        manifest, summary, records, as_of=validated_at
    )

    if mappings_csv is not None:
        _validate_current_mappings(Path(mappings_csv), manifest, records)
    if alerts_csv is not None:
        _validate_current_alerts(Path(alerts_csv), manifest, records)

    terminal_manifest, _ = read_stable_regular_file(
        target / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES
    )
    terminal_summary, _ = read_stable_regular_file(
        target / "summary.json", maximum_bytes=MAX_SUMMARY_BYTES
    )
    terminal_classifications, _ = read_stable_regular_file(
        target / "classifications.jsonl", maximum_bytes=MAX_PROPOSAL_BYTES
    )
    if (
        terminal_manifest != manifest_content
        or terminal_summary != summary_content
        or terminal_classifications != classifications_content
    ):
        raise ContractError("The dummy bundle changed during validation.")
    try:
        terminal_names = {entry.name for entry in target.iterdir()}
    except OSError as exc:
        raise ContractError("The dummy bundle directory cannot be relisted.") from exc
    if terminal_names != expected_names:
        raise ContractError("The dummy bundle changed during validation.")

    return DummyValidationResult(
        bundle_dir=target.resolve(),
        run_id=manifest.run_id,
        generated_at=manifest.generated_at,
        expires_at=_utc_text(expires),
        validated_as_of=_utc_text(validated_at),
        classification_count=len(records),
        counts=manifest.counts,
        classifications=records,
        mappings_checked=mappings_csv is not None,
        alerts_checked=alerts_csv is not None,
    )


__all__ = (
    "DummyClassificationRecord",
    "DummyValidationResult",
    "validate_dummy_bundle",
)
