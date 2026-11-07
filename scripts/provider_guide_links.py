"""Bind same-run authenticated playlist attributes to stable mapping identities.

These are XMLTV compatibility aliases, never evidence for choosing a schedule.
The mapping or a separately reviewed, programme-gated rule chooses that schedule.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from typing import Any, Sequence

try:
    from . import verified_guide_connections as reviewed
except ImportError:
    import verified_guide_connections as reviewed


STATUSES = frozenset({
    "verified", "name_only", "shared_id", "invalid_id", "conflicting_id",
    "name_mismatch", "unavailable",
})
SOURCES = frozenset({
    "player_api", "authenticated_m3u", "player_api+authenticated_m3u", "",
})


def _safe_text(value: str, maximum: int = 300) -> bool:
    return bool(
        value and len(value) <= maximum and value == value.strip()
        and not any(unicodedata.category(c).startswith("C") for c in value)
        and not re.search(r"https?://|(?:password|username)=", value, re.I)
    )


def _name_key(value: str) -> str:
    # '+' and word/digit boundaries identify different feeds, e.g. ITV +1
    # versus ITV1. Only case and redundant whitespace are presentation here.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def same_channel_name(left: str, right: str, category: str = "", countries=()) -> bool:
    if not left or not right:
        return False
    if _name_key(left) == _name_key(right):
        return True
    # Only the reviewed UK families may ignore presentation/quality suffixes.
    a = reviewed.reviewed_connection(left, category, countries)
    b = reviewed.reviewed_connection(right, category, countries)
    return a is not None and b is not None and a == b


def apply_provider_playlist_aliases(
    rows: Sequence[Any], inventory_content: bytes, selected_servers: set[str],
    *, quarantine_reason: str,
) -> tuple[list[Any], dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    """Require exact server/stream binding and unchanged proven channel identity.

    Old inventory CSVs simply contain no playlist-link evidence. Invalid new
    evidence fails closed rather than silently granting an alias.
    """
    reader = csv.DictReader(io.StringIO(inventory_content.decode("utf-8-sig"), newline=""))
    if "provider_playlist_link_status" not in (reader.fieldnames or ()):
        return list(rows), {}, {"observedRows": 0, "identityVerifiedRows": 0, "idAliasRows": 0}
    inventory: dict[tuple[str, str], dict[str, Any]] = {}
    id_counts: Counter[tuple[str, str]] = Counter()
    for raw in reader:
        server = str(raw.get("server_id") or "")
        sid = str(raw.get("stream_id") or "")
        if server not in selected_servers or not sid or (server, sid) in inventory:
            raise ValueError("Provider playlist links contain an invalid or duplicate identity.")
        status = str(raw.get("provider_playlist_link_status") or "unavailable")
        source = str(raw.get("provider_playlist_identity_source") or "")
        if status not in STATUSES or source not in SOURCES:
            raise ValueError("Provider playlist links contain an invalid evidence status.")
        names = json.loads(raw.get("provider_playlist_name_aliases") or "[]")
        if (not isinstance(names, list) or len(names) > 3
                or any(not isinstance(n, str) for n in names)):
            raise ValueError("Provider playlist links contain invalid name aliases.")
        names = [name for name in names if _safe_text(name)]
        epg_id = str(raw.get("provider_playlist_epg_id_alias") or "")
        if epg_id and not _safe_text(epg_id):
            epg_id, status = "", "invalid_id"
        name = str(raw.get("channel_name") or "")
        if not _safe_text(name):
            status = "unavailable"
        shared_id = str(raw.get("provider_playlist_shared_epg_id") or "")
        raw_shared_count = str(raw.get("provider_playlist_shared_stream_count") or "0")
        if not re.fullmatch(r"[0-9]{1,6}", raw_shared_count):
            raise ValueError("Provider playlist links contain an invalid shared-ID count.")
        shared_count = int(raw_shared_count)
        if shared_id and (not _safe_text(shared_id) or status != "shared_id" or shared_count < 2):
            shared_id, shared_count = "", 0
        inventory[(server, sid)] = {
            "current_name": name, "names": names, "provider_epg_id": epg_id,
            "status": status, "source": source,
            "shared_epg_id": shared_id, "shared_stream_count": shared_count,
        }
        if epg_id:
            id_counts[(server, epg_id)] += 1
    links: dict[tuple[str, str], dict[str, Any]] = {}
    output = []
    summary = {"observedRows": 0, "identityVerifiedRows": 0, "idAliasRows": 0}
    for row in rows:
        key = row.server_id, row.stream_id
        evidence = inventory.get(key)
        if evidence is None:
            output.append(row)
            continue
        summary["observedRows"] += 1
        link = dict(evidence)
        countries = getattr(row.metadata, "countries", ())
        identity_verified = bool(
            row.reason != quarantine_reason
            and not row.reason.casefold().startswith("effective snapshot quarantine:")
            and evidence["source"]
            and evidence["status"] not in {"name_mismatch", "unavailable"}
            and same_channel_name(row.channel_name, evidence["current_name"], row.category_name, countries)
        )
        link["identity_verified"] = identity_verified
        if not identity_verified:
            link["status"] = "unverified"
            links[key] = link
            output.append(row)
            continue
        summary["identityVerifiedRows"] += 1
        epg_id = evidence["provider_epg_id"]
        verified_id = bool(
            evidence["status"] == "verified" and epg_id
            and id_counts[(row.server_id, epg_id)] == 1
        )
        if epg_id and id_counts[(row.server_id, epg_id)] != 1:
            link["status"] = "shared_id"
        names = {
            name for name in [evidence["current_name"], *evidence["names"]]
            if _safe_text(name)
            and same_channel_name(row.channel_name, name, row.category_name, countries)
            and name != row.channel_name
        }
        updated = replace(
            row,
            provider_name_aliases=tuple(sorted(set(row.provider_name_aliases) | names)),
            provider_epg_id_aliases=tuple(sorted(
                set(row.provider_epg_id_aliases) | ({epg_id} if verified_id else set())
            )),
        )
        summary["idAliasRows"] += int(verified_id)
        links[key] = link
        output.append(updated)
    return output, links, summary


def apply_shared_playlist_aliases(
    rows: Sequence[Any], links: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[Any], int]:
    """Share an ID only when every authenticated member selects one schedule.

    The producer's count covers *all* raw inventory members, including streams
    absent from the mapping. A missing, quarantined or different member vetoes
    the alias. This permits verified HD/FHD feeds without arbitrary tie-breaking.
    """
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        proof = links.get((row.server_id, row.stream_id), {})
        identifier = proof.get("shared_epg_id", "")
        if identifier:
            groups.setdefault((row.server_id, identifier), []).append(row)
    approved: dict[tuple[str, str], str] = {}
    for (server, identifier), members in groups.items():
        proofs = [links[(server, row.stream_id)] for row in members]
        counts = {proof["shared_stream_count"] for proof in proofs}
        if (counts != {len(members)} or len(members) < 2
                or any(proof.get("identity_verified") is not True or proof.get("status") != "shared_id" for proof in proofs)
                or any(not row.runtime_eligible for row in members)
                or len({(row.source_key, row.schedule_channel_key) for row in members}) != 1):
            continue
        for row, proof in zip(members, proofs):
            approved[(server, row.stream_id)] = identifier
            proof.update(provider_epg_id=identifier, status="verified")
    return [
        replace(row, provider_epg_id_aliases=tuple(sorted(
            set(row.provider_epg_id_aliases) | {approved[(row.server_id, row.stream_id)]}
        ))) if (row.server_id, row.stream_id) in approved else row
        for row in rows
    ], len(approved)
