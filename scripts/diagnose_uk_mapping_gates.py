#!/usr/bin/env python3
"""Read one mapping snapshot; emit only aggregate gates for five fixed UK labels."""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import sync_channel_inventory as inventory
import verified_guide_connections as reviewed
import diagnose_cricket_mapping_gates as cricket


GROUPS = ("BBC1", "BBCTwoScotland", "ITVPlus1", "BBC3", "TalkTV")
DEFAULT_OUTPUT = Path(".build/uk-mapping-gates/report.json")
REVIEW_ACTIONS = cricket.REVIEW_ACTIONS
IGNORE_ACTIONS = cricket.IGNORE_ACTIONS
KNOWN_COUNTRIES = frozenset((
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
    "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP "
    "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT "
    "MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW "
    "SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG "
    "UK UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW"
).split())
KNOWN_LANGUAGES = cricket.KNOWN_LANGUAGES | {"cy", "gd", "ga", "sco"}
_QUALITY_SUFFIX = re.compile(r"(?:\(?(?:fhd|uhd|hd|sd|4k|8k|2160p|1080p|720p|hevc)\)?)+$", re.I)
_FOREIGN_CATEGORY = re.compile(
    r"(?:\A|[^a-z])(?:de|germany|german|it|italy|italian|ie|ireland|irish|us|usa|united states|ca|canada|"
    r"au|australia|nz|new zealand|in|india|pk|pakistan|sg|singapore|fr|france|es|spain|pt|portugal|"
    r"nl|netherlands|be|belgium|za|south africa|my|malaysia|hk|hong kong|br|brazil|mx|mexico|"
    r"at|austria|ch|switzerland|pl|poland|ro|romania|tr|turkey)(?:\Z|[^a-z])", re.I,
)
_OTHER_UK_REGION = re.compile(r"\b(?:england|wales|northern\s+ireland)\b", re.I)
PROTECTED_TERMS = cricket.PROTECTED_TERMS
# Exact public identifiers seen in the retained UK1 source catalogue. These
# enums describe a row's existing ID, not an approved match or current schedule.
# In particular BBC Two generic is not proof of the Scotland feed, and BBC One
# London is not an automatic choice for an unspecified BBC 1 channel.
KNOWN_EPG_IDS = {
    "BBC.One.EastHD.uk": "bbc_one_east",
    "BBC.One.Lon.HD.uk": "bbc_one_london",
    "BBC.One.ScotHD.uk": "bbc_one_scotland",
    "BBC.One.Wal.HD.uk": "bbc_one_wales",
    "BBC.One.NI.HD.uk": "bbc_one_northern_ireland",
    "BBC.Two.HD.uk": "bbc_two_generic",
    "BBC.Two.NI.HD.uk": "bbc_two_northern_ireland",
    "BBC.Two.Wal.HD.uk": "bbc_two_wales",
    "BBC.Three.HD.uk": "bbc_three",
    "ITV1.HD.uk": "itv1_main",
    "ITV1+1.uk": "itv1_plus_one",
    "TalkRadio.(TalkTV).uk": "talk_radio_talk_tv",
    "TalkTV.HD.uk": "legacy_talk_tv_hd",
}


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[|,;\s]+", str(value).strip()) if token]


def classify_row(row: Mapping[str, str]) -> tuple[str | None, tuple[str, ...]]:
    """Preserve +1, regional qualifiers and market evidence while grouping."""
    name = re.sub(r"\s+", "", _text(row.get("channel_name", "")))
    name = _QUALITY_SUFFIX.sub("", name)
    prefix = re.fullmatch(r"([a-z]{2,3})(:|\|+|-)(.+)", name)
    market, separator, base = prefix.groups() if prefix else ("", "", name)
    group = {
        "bbc1": "BBC1", "bbctwo(scotland)": "BBCTwoScotland",
        "itv+1": "ITVPlus1", "bbc3": "BBC3", "talktv": "TalkTV",
    }.get(base)
    if group is None:
        return None, ()
    # BBC/ITV selection requires the screenshot's explicit UK: label. Talk TV
    # is unprefixed in the photo; an explicit UK: label remains the same market.
    if not prefix and group != "TalkTV":
        return None, ()
    vetoes = []
    if prefix and (market != "uk" or separator != ":"):
        vetoes.append("different_label_market_or_prefix")
    country_tokens = [token.upper() for token in _tokens(row.get("country_codes", ""))]
    if any(token not in KNOWN_COUNTRIES for token in country_tokens):
        vetoes.append("unrecognized_country_metadata")
    if any(token in KNOWN_COUNTRIES - {"UK", "GB"} for token in country_tokens):
        vetoes.append("foreign_country_metadata")
    category = _text(row.get("category_name", ""))
    if _FOREIGN_CATEGORY.search(category):
        vetoes.append("foreign_category_metadata")
    if group == "BBCTwoScotland" and _OTHER_UK_REGION.search(category):
        vetoes.append("different_uk_region_metadata")
    return group, tuple(vetoes)


def group_for_row(row: Mapping[str, str]) -> str | None:
    group, vetoes = classify_row(row)
    return group if not vetoes else None


def epg_id_provenance(
    row: Mapping[str, str], source: str, *, known_epg_ids: Mapping[str, str] = KNOWN_EPG_IDS,
) -> str:
    identifier = str(row.get("epg_id", ""))
    if not identifier.strip():
        return "blank"
    if identifier in known_epg_ids:
        return known_epg_ids[identifier]
    if source == "panel":
        return "other_panel_id"
    if source == "dummy":
        return "other_dummy_id"
    return "other_nonempty"


def summarize(
    table: inventory.MappingTable, *, groups: Sequence[str] = GROUPS,
    classifier: Callable[[Mapping[str, str]], tuple[str | None, tuple[str, ...]]] = classify_row,
    known_epg_ids: Mapping[str, str] = KNOWN_EPG_IDS,
) -> dict[str, Any]:
    groups = {
        group: {
            "group": group, "rowCount": 0, "vetoedLookalikeRows": 0,
            "eligibleRows": 0, "enabledRows": 0, "disabledRows": 0,
            "sourcePolicyBlockedRows": 0, "reviewRows": 0,
            "explicitIgnoreRows": 0, "manualDecisionRows": 0,
            "manualDisabledRows": 0, "protectedNoteRows": 0,
            "metadataLockedRows": 0, "invalidGateRows": 0,
            "unrecognizedLanguageRows": 0,
            "countryCodes": set(), "languageCodes": set(),
            **{field: Counter() for field in (
                "actionCounts", "sourceCounts", "protectedTermCounts", "epgIdProvenanceCounts", "vetoReasonCounts",
            )},
        } for group in groups
    }
    for row in table.rows:
        if row.get("server_id") != "server_1":
            continue
        group, vetoes = classifier(row)
        if group is None:
            continue
        item = groups[group]
        if vetoes:
            item["vetoedLookalikeRows"] += 1
            item["vetoReasonCounts"].update(vetoes)
            continue
        item["rowCount"] += 1
        action = str(row.get("action", "APPROVED")).strip().upper() or "APPROVED"
        valid_action = action in inventory.streaming.ALLOWED_ACTIONS
        item["actionCounts"][action if valid_action else "other"] += 1
        item["reviewRows"] += action in REVIEW_ACTIONS
        item["explicitIgnoreRows"] += action in IGNORE_ACTIONS
        item["manualDecisionRows"] += action == "MANUAL"
        note = f"{row.get('reason', '')} {row.get('notes', '')}"
        item["protectedNoteRows"] += bool(reviewed._PROTECTED_REVIEW_NOTE.search(note))
        for term, pattern in PROTECTED_TERMS.items():
            item["protectedTermCounts"][term] += bool(re.search(pattern, note, re.I))
        item["countryCodes"].update(token.upper() for token in _tokens(row.get("country_codes", "")) if token.upper() in KNOWN_COUNTRIES)
        language_tokens = [token.casefold() for token in _tokens(f"{row.get('language_codes', '')} {row.get('primary_language', '')}")]
        item["languageCodes"].update(token for token in language_tokens if token in KNOWN_LANGUAGES)
        item["unrecognizedLanguageRows"] += any(token not in KNOWN_LANGUAGES for token in language_tokens)
        invalid = not valid_action
        enabled = locked = None
        try:
            enabled = inventory.streaming.parse_bool(row.get("enabled", ""), default=action not in IGNORE_ACTIONS, field_name="diagnostic flag")
        except inventory.streaming.BuildError:
            invalid = True
        try:
            locked = inventory.streaming.parse_bool(row.get("metadata_locked", ""), default=False, field_name="diagnostic flag")
        except inventory.streaming.BuildError:
            invalid = True
        try:
            source = inventory.streaming.normalize_requested_source(row.get("source", ""), row.get("epg_feed", ""), row_number=0)
        except inventory.streaming.BuildError:
            source, invalid = "invalid", True
        item["sourceCounts"][source] += 1
        item["epgIdProvenanceCounts"][epg_id_provenance(row, source, known_epg_ids=known_epg_ids)] += 1
        item["enabledRows"] += enabled is True
        item["disabledRows"] += enabled is False
        item["metadataLockedRows"] += locked is True
        item["manualDisabledRows"] += enabled is False and action not in REVIEW_ACTIONS
        item["invalidGateRows"] += invalid
        blocked = source == "panel"
        item["sourcePolicyBlockedRows"] += blocked
        item["eligibleRows"] += bool(not invalid and enabled and action in inventory.streaming.ACTIVE_ACTIONS and row.get("epg_id", "").strip() and not blocked)
    for item in groups.values():
        for field in ("countryCodes", "languageCodes"):
            item[field] = sorted(item[field])
        for field in ("actionCounts", "sourceCounts", "protectedTermCounts", "epgIdProvenanceCounts", "vetoReasonCounts"):
            item[field] = dict(sorted(item[field].items()))
    return {
        "schemaVersion": 1, "serverId": "server_1", "status": "available",
        "selectedRows": sum(item["rowCount"] for item in groups.values()),
        "vetoedLookalikeRows": sum(item["vetoedLookalikeRows"] for item in groups.values()),
        "countsCanOverlap": True, "eligibilityIsNotScheduleCoverage": True,
        "idProvenanceIsNotMatchApproval": True, "groups": list(groups.values()),
    }


def diagnose(*, summarizer: Callable[[inventory.MappingTable], dict[str, Any]] = summarize) -> dict[str, Any]:
    session = None
    try:
        sheet_id = inventory.validate_sheet_id(os.environ.get("GOOGLE_SHEET_ID", ""))
        session = inventory.authorized_google_session(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
        values = inventory.google_sheet_values(session, sheet_id, os.environ.get("GOOGLE_SHEET_TAB", "Mappings"))
        return summarizer(inventory.parse_table_values(values, maximum_rows=inventory.MAX_GOOGLE_MAPPING_ROWS))
    except (inventory.SyncError, inventory.streaming.BuildError):
        report = summarizer(inventory.MappingTable([], [], []))
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
    print(json.dumps({
        "selectedRows": report["selectedRows"], "vetoedLookalikeRows": report["vetoedLookalikeRows"],
        "groupsWithRows": sum(item["rowCount"] > 0 for item in report["groups"]),
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
