#!/usr/bin/env python3
"""Read private mapping gates; report only fixed cricket groups and aggregates."""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import sync_channel_inventory as inventory
import verified_guide_connections as reviewed


GROUPS = ("Sony1", "Sony2", "Sony3", "Sony4Tamil", "Sony4Telugu", "Sony5", "SonyESPN", "StarFirst", "Hub4", "SuperSportsGeneric", "SkySport1Generic")
REVIEW_ACTIONS = frozenset({"REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED"})
IGNORE_ACTIONS = frozenset({"IGNORE", "SKIP", "REJECTED"})
KNOWN_LANGUAGES = frozenset({"en", "hi", "pa", "ta", "te", "ur", "und", "mul", "bn", "kn", "ml", "mr", "gu", "or", "ne", "si", "ar", "ms", "zh", "fr", "de", "it", "es", "pt", "pl", "ru", "uk", "nl", "ro", "tr", "ko", "ja", "th", "vi", "id"})
PROTECTED_TERMS = {
    "manual_review": r"\b(?:manual|human|operator)\s+review\b",
    "manual_decision": r"\b(?:manual|human|operator)\s+(?:decision|mapping|selection|override|reuse)\b",
    "do_not_remap": r"\b(?:do\s+not|never)\s+(?:auto(?:matically)?[- ]?(?:match|map|change|replace|update)|remap|replace|change|override)\b",
    "stream_id_reuse": r"\b(?:stream[- ]?id\s+reuse|reused\s+stream[- ]?id|possible\s+(?:stream[- ]?id\s+)?reuse)\b",
    "identity_review": r"\b(?:provider\s+)?identity\s+(?:mismatch|review|change(?:d)?)\b",
    "schema_review": r"\bschema\s+review\b",
    "open_alert": r"\bopen\s+(?:sync\s+)?alerts?\b",
    "locked_mapping": r"\b(?:mapping|guide|schedule)\s+(?:is\s+)?locked\b|\blocked\s+(?:mapping|guide|schedule)\b",
}
DEFAULT_OUTPUT = Path(".build/cricket-mapping-gates/report.json")


def _key(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def group_for_row(row: Mapping[str, str]) -> str | None:
    name = str(row.get("channel_name", ""))
    category = _key(str(row.get("category_name", "")))
    prefix = bool(re.match(r"\s*(?:cric|cri)\s*[:|\-]", name, re.I))
    text = name.casefold()
    for _ in range(2):
        text = re.sub(r"^\s*(?:cric|cri|in|india|sg|sgp|singapore|uk)\s*[:|\-]+\s*", "", text)
    text = _key(text)
    suffix = r"(?:\s*(?:sd|hd|fhd|uhd|4k|2160p|1080p|720p|hevc))*"
    hub = bool(re.fullmatch(r"hub\s+sports\s*4" + suffix, text))
    if category != "sports cricket" and not prefix and not hub:
        return None
    if hub:
        return "Hub4"
    sony = re.fullmatch(r"sony\s+(?:sports?(?:\s+ten)?|ten)\s*([1-5])\s*(.*)", text)
    if sony:
        number, qualifiers = sony.groups()
        tokens = set(qualifiers.split())
        technical = {"sd", "hd", "fhd", "uhd", "4k", "2160p", "1080p", "720p", "hevc"}
        if number == "4":
            if tokens - technical in ({"tamil"}, {"tam"}, {"ta"}):
                return "Sony4Tamil"
            if tokens - technical in ({"telugu"}, {"tel"}, {"te"}):
                return "Sony4Telugu"
            return None
        if tokens <= technical | {"english", "hindi", "en", "hi"}:
            return "Sony" + number
    if re.fullmatch(r"sony\s+espn" + suffix, text):
        return "SonyESPN"
    if re.fullmatch(r"star\s+sports\s+first" + suffix, text):
        return "StarFirst"
    if re.fullmatch(r"super\s*sports?" + suffix, text):
        return "SuperSportsGeneric"
    if re.fullmatch(r"sky\s+sports?\s*1" + suffix, text):
        return "SkySport1Generic"
    return None


def _bool(value: str, *, default: bool = False) -> bool:
    return inventory.streaming.parse_bool(value, default=default, field_name="diagnostic flag")


def summarize(table: inventory.MappingTable) -> dict[str, Any]:
    groups = {
        group: {
            "group": group, "rowCount": 0, "eligibleRows": 0,
            "sourcePolicyBlockedRows": 0, "reviewRows": 0,
            "explicitIgnoreRows": 0, "manualDecisionRows": 0,
            "manualDisabledRows": 0, "protectedNoteRows": 0,
            "metadataLockedRows": 0, "invalidGateRows": 0,
            "reviewedRuleIdentityRows": 0, "reviewedRuleAllowedRows": 0,
            "countryCodes": set(), "languageCodes": set(),
            "actionCounts": Counter(), "sourceCounts": Counter(),
            "protectedTermCounts": Counter(),
        } for group in GROUPS
    }
    for row in table.rows:
        if row.get("server_id") != "server_1":
            continue
        group = group_for_row(row)
        if group is None:
            continue
        item = groups[group]
        item["rowCount"] += 1
        action = str(row.get("action", "APPROVED")).strip().upper() or "APPROVED"
        item["actionCounts"][action if action in inventory.streaming.ALLOWED_ACTIONS else "other"] += 1
        item["reviewRows"] += action in REVIEW_ACTIONS
        item["explicitIgnoreRows"] += action in IGNORE_ACTIONS
        item["manualDecisionRows"] += action == "MANUAL"
        note = f"{row.get('reason', '')} {row.get('notes', '')}"
        item["protectedNoteRows"] += bool(reviewed._PROTECTED_REVIEW_NOTE.search(note))
        for term, pattern in PROTECTED_TERMS.items():
            item["protectedTermCounts"][term] += bool(re.search(pattern, note, re.I))
        countries = {value.upper() for value in re.split(r"[|,;\s]+", row.get("country_codes", "")) if re.fullmatch(r"[A-Za-z]{2}", value)}
        languages = {value.casefold() for value in re.split(r"[|,;\s]+", f"{row.get('language_codes', '')} {row.get('primary_language', '')}") if value.casefold() in KNOWN_LANGUAGES}
        item["countryCodes"].update(countries)
        item["languageCodes"].update(languages)
        item["reviewedRuleIdentityRows"] += reviewed.reviewed_connection(row.get("channel_name", ""), row.get("category_name", ""), countries) is not None
        item["reviewedRuleAllowedRows"] += reviewed.connection_for_row(row, allow_review=True) is not None
        try:
            enabled = _bool(row.get("enabled", ""), default=action not in IGNORE_ACTIONS)
            locked = _bool(row.get("metadata_locked", ""))
            source = inventory.streaming.normalize_requested_source(row.get("source", ""), row.get("epg_feed", ""), row_number=0)
        except inventory.streaming.BuildError:
            item["invalidGateRows"] += 1
            item["sourceCounts"]["invalid"] += 1
            continue
        item["sourceCounts"][source] += 1
        item["metadataLockedRows"] += locked
        item["manualDisabledRows"] += not enabled and action not in REVIEW_ACTIONS
        blocked = source == "panel"
        item["sourcePolicyBlockedRows"] += blocked
        item["eligibleRows"] += bool(enabled and action in inventory.streaming.ACTIVE_ACTIONS and row.get("epg_id") and not blocked)
    for item in groups.values():
        item["countryCodes"] = sorted(item["countryCodes"])
        item["languageCodes"] = sorted(item["languageCodes"])
        for field in ("actionCounts", "sourceCounts", "protectedTermCounts"):
            item[field] = dict(sorted(item[field].items()))
    return {
        "schemaVersion": 1, "serverId": "server_1", "status": "available",
        "selectedRows": sum(item["rowCount"] for item in groups.values()),
        "countsCanOverlap": True, "ruleAllowanceIsNotApproval": True,
        "groups": list(groups.values()),
    }


def diagnose() -> dict[str, Any]:
    session = None
    try:
        sheet_id = inventory.validate_sheet_id(os.environ.get("GOOGLE_SHEET_ID", ""))
        session = inventory.authorized_google_session(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
        values = inventory.google_sheet_values(session, sheet_id, os.environ.get("GOOGLE_SHEET_TAB", "Mappings"))
        return summarize(inventory.parse_table_values(values, maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS))
    except (inventory.SyncError, inventory.streaming.BuildError):
        report = summarize(inventory.MappingTable([], [], []))
        report["status"] = "source_unavailable"
        return report
    finally:
        if session is not None:
            inventory.streaming.close_panel_transport(session)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = diagnose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    inventory.atomic_write_bytes(args.output, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"selectedRows": report["selectedRows"], "groupsWithRows": sum(item["rowCount"] > 0 for item in report["groups"])}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
