"""Describe per-channel XMLTV links using the publisher's existing build data.

This module never fetches a provider, parses XML, or inspects programme rows.
The caller supplies the same selected entries and aggregate statistics used by
the XMLTV writer, so a successful file-wide freshness check cannot hide an
individual missing/expired schedule or an alias lost to another channel.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "skytv-tivimate-guide-health-v1"
CSV_HEADERS = (
    "server_id", "stream_id", "channel_name", "action", "enabled",
    "requested_source", "effective_source", "epg_feed", "epg_id",
    "runtime_eligible", "guide_kind", "outcome", "omitted_reason",
    "programme_rows", "current_programme_rows",
    "current_or_upcoming_programme_rows", "upcoming_programme_rows",
    "latest_stop", "latest_current_or_upcoming_stop", "schedule_status",
    "exported_id_count", "conflicting_id_count", "channel_name_binding",
    "provider_epg_id", "provider_link_evidence", "provider_epg_id_status",
    "requires_attention", "issues",
)

_REVIEW_ACTIONS = frozenset({"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED"})
_EXCLUDED_ACTIONS = frozenset({"SKIP", "IGNORE", "REJECTED"})
_URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:password|passwd|username|access_token|api_key|token)\s*[=:]\s*\S+",
    re.IGNORECASE,
)


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, Mapping) else getattr(item, key, default)


def _public_text(value: Any, limit: int = 500) -> str:
    """Keep diagnostic identifiers exact while omitting transport/secret text."""
    text = str(value or "")
    text = _URL.sub("[redacted-url]", text)
    text = _SENSITIVE_ASSIGNMENT.sub("[redacted-credential]", text)
    return "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)[:limit]


def _number(item: Any, key: str, default: int = 0) -> int:
    return max(0, int(_value(item, key, default) or 0))


def _provider_evidence(row: Any, link: Mapping[str, Any] | None) -> tuple[str, str]:
    if link is not None:
        return str(link.get("provider_epg_id") or ""), str(link.get("status") or "unverified")
    aliases = tuple(_value(row, "provider_epg_id_aliases", ()) or ())
    if aliases:
        # An alias attached by the authenticated overlay is already verified.
        # Its exact first ID is sufficient for the per-stream report; all
        # attached IDs are checked below as export candidates.
        return str(aliases[0]), "verified"
    return "", "not_observed"


def _binding(entries: Mapping[str, Any], identifier: str, key: tuple[str, str]) -> str:
    entry = entries.get(identifier)
    if entry is None:
        return "absent"
    actual = (str(_value(entry, "source_key", "")), str(_value(entry, "channel_key", "")))
    return "bound" if actual == key else "other_schedule"


def _omission(row: Any) -> str:
    if bool(_value(row, "runtime_eligible", False)):
        return ""
    action = str(_value(row, "action", "")).upper()
    reason = str(_value(row, "reason", ""))
    if reason.startswith("Effective snapshot quarantine:"):
        return "identity_quarantine"
    if bool(_value(row, "source_policy_blocked", False)):
        return "source_policy_blocked"
    if action in _EXCLUDED_ACTIONS:
        return "explicit_" + action.lower()
    if action in _REVIEW_ACTIONS:
        return "awaiting_mapping_review"
    if not bool(_value(row, "enabled", False)):
        return "disabled"
    if not _value(row, "epg_id", ""):
        return "missing_epg_id"
    return "not_runtime_eligible"


def build_guide_link_health(
    *,
    rows: Sequence[Any],
    entries: Mapping[str, Any],
    schedule_stats: Mapping[tuple[str, str], Any],
    generated_at: int,
    provider_links: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a compact summary plus CSV-ready records for every mapping row.

    ``entries`` must be the final collision-resolved XML entries, not proposed
    aliases. ``provider_links`` is optional authenticated inventory evidence,
    keyed by the exact (server, stream) pair; a missing value means the test was
    not observed, not proof that the provider omitted an ID. Current coverage
    is reported only when ScheduleStats supplies ``current_rows``. Older stats
    remain supported and are explicitly labelled ``current_or_upcoming``.
    """
    generated_at = int(generated_at)
    provider_links = provider_links or {}
    seen: set[tuple[str, str]] = set()
    provider_targets: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    evidence: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        identity = (str(_value(row, "server_id", "")), str(_value(row, "stream_id", "")))
        if identity in seen:
            raise ValueError("Guide-link health received a duplicate server/stream identity.")
        seen.add(identity)
        provider_id, status = _provider_evidence(row, provider_links.get(identity))
        evidence[identity] = (provider_id, status)
        if provider_id and bool(_value(row, "runtime_eligible", False)):
            provider_targets[(identity[0], provider_id)].add((
                str(_value(row, "source_key", "")),
                str(_value(row, "schedule_channel_key", _value(row, "epg_id", ""))),
            ))

    records: list[dict[str, Any]] = []
    outcomes: Counter[str] = Counter()
    provider_statuses: Counter[str] = Counter()
    omissions: Counter[str] = Counter()
    totals: Counter[str] = Counter({key: 0 for key in (
        "eligibleRows", "exportedRows", "realScheduleRows", "realCurrentRows",
        "realCurrentOrUpcomingRows", "placeholderRows", "missingScheduleRows",
        "expiredScheduleRows", "requiresAttentionRows", "currentStatusMeasuredRows",
    )})
    for row in rows:
        server = str(_value(row, "server_id", ""))
        stream = str(_value(row, "stream_id", ""))
        key = (
            str(_value(row, "source_key", "")),
            str(_value(row, "schedule_channel_key", _value(row, "epg_id", ""))),
        )
        eligible = bool(_value(row, "runtime_eligible", False))
        synthetic = bool(_value(row, "uses_local_synthetic", False))
        stats = schedule_stats.get(key)
        programme_rows = _number(stats, "programme_rows")
        available_rows = _number(stats, "future_rows")
        current_raw = _value(stats, "current_rows")
        current_rows = None if current_raw is None else max(0, int(current_raw))
        latest_stop = _number(stats, "latest_stop")
        latest_available_stop = _number(stats, "latest_future_stop")
        if not programme_rows:
            schedule_status = "missing"
        elif not available_rows or latest_stop <= generated_at:
            schedule_status = "expired"
        elif current_rows is None:
            schedule_status = "current_or_upcoming"
        elif current_rows:
            schedule_status = "current"
        else:
            schedule_status = "upcoming"

        channel_name = str(_value(row, "channel_name", ""))
        aliases = [channel_name, *(_value(row, "provider_name_aliases", ()) or ())]
        aliases.extend(_value(row, "provider_epg_id_aliases", ()) or ())
        if synthetic:
            aliases.append(str(_value(row, "schedule_key", "")))
        else:
            aliases.append(str(_value(row, "epg_id", "")))
        provider_id, provider_evidence = evidence[(server, stream)]
        if provider_id:
            aliases.append(provider_id)
        candidates = dict.fromkeys(identifier for identifier in aliases if identifier)
        bindings = {identifier: _binding(entries, identifier, key) for identifier in candidates}
        bound = sum(value == "bound" for value in bindings.values()) if eligible else 0
        conflicting = sum(value == "other_schedule" for value in bindings.values())
        name_binding = _binding(entries, channel_name, key) if eligible else "omitted"
        omission = _omission(row)

        if provider_evidence in {"shared_id", "conflicting_id", "ambiguous"}:
            provider_status = "ambiguous"
        elif provider_evidence == "invalid_id":
            provider_status = "invalid"
        elif provider_evidence in {"name_only", "missing"}:
            provider_status = "missing"
        elif provider_evidence in {"not_observed", "unavailable"}:
            provider_status = "not_observed"
        elif provider_evidence != "verified":
            provider_status = "unverified"
        elif not provider_id:
            provider_status = "missing"
        elif len(provider_targets[(server, provider_id)]) > 1:
            provider_status = "ambiguous"
        elif not eligible:
            provider_status = "omitted"
        else:
            provider_binding = _binding(entries, provider_id, key)
            provider_status = {
                "bound": "bound", "absent": "not_exported", "other_schedule": "wrong_schedule",
            }[provider_binding]

        issues: list[str] = []
        if not eligible:
            outcome = "omitted"
        elif not programme_rows:
            outcome = "missing_schedule"
            issues.append("no_programmes")
        elif not bound:
            outcome = "unbound_export"
            issues.append("no_exported_identity")
        elif synthetic:
            outcome = "placeholder"
        else:
            outcome = "real_" + schedule_status
        if eligible and programme_rows and schedule_status == "expired":
            issues.append("expired_schedule")
        if eligible and not synthetic and schedule_status == "upcoming":
            issues.append("no_current_programme")
        if eligible and name_binding == "other_schedule":
            issues.append("channel_name_points_elsewhere")
        if eligible and provider_status in {"ambiguous", "invalid", "not_exported", "wrong_schedule"}:
            issues.append("provider_id_" + provider_status)
        requires_attention = bool(eligible and issues)
        record = {
            "server_id": _public_text(server, 40),
            "stream_id": _public_text(stream, 120),
            "channel_name": _public_text(channel_name, 300),
            "action": _public_text(_value(row, "action", ""), 40),
            "enabled": bool(_value(row, "enabled", False)),
            "requested_source": _public_text(_value(row, "requested_source", ""), 40),
            "effective_source": _public_text(_value(row, "effective_source", ""), 40),
            "epg_feed": _public_text(_value(row, "epg_feed", ""), 100),
            "epg_id": _public_text(_value(row, "epg_id", ""), 300),
            "runtime_eligible": eligible,
            "guide_kind": "placeholder" if synthetic else "real",
            "outcome": outcome,
            "omitted_reason": omission,
            "programme_rows": programme_rows,
            "current_programme_rows": current_rows,
            "current_or_upcoming_programme_rows": available_rows,
            "upcoming_programme_rows": None if current_rows is None else max(0, available_rows - current_rows),
            "latest_stop": latest_stop,
            "latest_current_or_upcoming_stop": latest_available_stop,
            "schedule_status": schedule_status,
            "exported_id_count": bound,
            "conflicting_id_count": conflicting,
            "channel_name_binding": name_binding,
            "provider_epg_id": _public_text(provider_id, 300),
            "provider_link_evidence": _public_text(provider_evidence, 40),
            "provider_epg_id_status": provider_status,
            "requires_attention": requires_attention,
            "issues": "|".join(issues),
        }
        records.append(record)
        outcomes[outcome] += 1
        provider_statuses[provider_status] += 1
        if omission:
            omissions[omission] += 1
        totals["eligibleRows"] += eligible
        totals["exportedRows"] += eligible and bound > 0
        totals["realScheduleRows"] += eligible and not synthetic and programme_rows > 0
        totals["realCurrentRows"] += eligible and not synthetic and programme_rows > 0 and bool(current_rows)
        totals["realCurrentOrUpcomingRows"] += eligible and not synthetic and programme_rows > 0 and available_rows > 0 and latest_stop > generated_at
        totals["placeholderRows"] += eligible and synthetic and programme_rows > 0
        totals["missingScheduleRows"] += eligible and not programme_rows
        totals["expiredScheduleRows"] += eligible and programme_rows > 0 and schedule_status == "expired"
        totals["requiresAttentionRows"] += requires_attention
        totals["currentStatusMeasuredRows"] += stats is not None and current_rows is not None

    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "servers": sorted({str(_value(row, "server_id", "")) for row in rows}),
        "mappingRows": len(records),
        "omittedRows": outcomes["omitted"],
        **dict(totals),
        "outcomes": dict(sorted(outcomes.items())),
        "omittedReasons": dict(sorted(omissions.items())),
        "providerIdStatuses": dict(sorted(provider_statuses.items())),
        "semantics": {
            "realScheduleRows": "An eligible external schedule with retained programmes; expired rows are separately identified.",
            "placeholderRows": "Generated programme labels, not verified real programme schedules.",
            "currentOrUpcoming": "Programme stop is after generatedAt; future_rows alone does not prove coverage at the current instant.",
            "providerIdBound": "The exact verified provider ID survived XMLTV conflict resolution and selects this stream's schedule.",
            "notObserved": "Provider ID evidence was unavailable; not proof that the playlist has no ID.",
        },
    }
    return summary, records


def csv_rows(records: Iterable[Mapping[str, Any]]) -> Iterable[tuple[Any, ...]]:
    """Use the publisher's existing spreadsheet-safe atomic CSV writer."""
    for record in records:
        yield tuple(record.get(header, "") for header in CSV_HEADERS)
