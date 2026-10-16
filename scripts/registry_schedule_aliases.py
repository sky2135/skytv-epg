"""Same-market exact registry aliases; every proposed schedule still needs gates.

This layer supplies spelling/brand aliases only. It never chooses a feed from
multiple EPG identities, imports a panel ID, or treats a logo as schedule proof.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

try:
    from .channel_metadata_enrichment import ChannelRegistry, normalize_name
except ImportError:
    from channel_metadata_enrichment import ChannelRegistry, normalize_name


def _country(market: str) -> str:
    return {"UK": "GB"}.get(market.upper(), market.upper())


def _edition(name: str) -> tuple:
    text = str(name).casefold()
    return (
        tuple(re.findall(r"\d+", re.sub(r"\b(?:4k|8k|1080p|720p|h264|h265)\b", "", text))),
        tuple(re.findall(r"\b(?:east|west|north|south|extra|alternate|plus)\b", text)),
        "+" in text,
    )


def registry_schedule_aliases(
    candidates: Sequence[Mapping[str, Any]], registry: ChannelRegistry,
    *, existing_aliases: Sequence[Mapping[str, Any]] = (),
    matcher_normalizer: Callable[[object], str] | None = None,
) -> list[dict[str, str]]:
    """Join exact registry identities only when one same-country EPG ID exists."""
    matcher_key = matcher_normalizer or normalize_name
    def matcher_keys(value: object) -> set[str]:
        strict = matcher_key(value)
        # Approved aliases are also compared with the frozen query's relaxed
        # identity. A different channel's "The Example TV" must veto "Example"
        # even when their strict keys retain different optional descriptors.
        relaxed = " ".join(word for word in strict.split()
                           if word not in {"tv", "television", "channel", "network", "the"})
        return {key for key in (strict, relaxed) if key}
    by_channel: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    source_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    registry_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in registry.records.values():
        for name in record.names:
            for key in matcher_keys(name):
                registry_keys[(record.country, key)].add(record.channel_id)
    for candidate in candidates:
        market = str(candidate.get("region") or "").strip().upper()
        country = _country(market)
        if country not in registry.valid_countries:
            continue
        name = str(candidate.get("display_name") or "")
        epg_id = str(candidate.get("epg_id") or "")
        if not name or not epg_id:
            continue
        for key in matcher_keys(name):
            # Even an unrecognized registry competitor must veto an alias
            # whose key the frozen resolver would consider an exact match.
            source_keys[(market, key)].add(epg_id)
        record = registry.resolve_channel(name, [country])
        if record is None or record.country != country or record.is_nsfw:
            continue
        by_channel[(record.channel_id, market)][epg_id].add(name)

    reserved = {key for item in existing_aliases for key in matcher_keys(item.get("alias", ""))}
    aliases: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for (channel_id, market), targets in sorted(by_channel.items()):
        if len(targets) != 1:
            continue
        epg_id, source_names = next(iter(targets.items()))
        editions = {_edition(name) for name in source_names}
        if len(editions) != 1:
            continue
        source_edition = next(iter(editions))
        current_source_keys = {matcher_key(name) for name in source_names}
        record = registry.records[channel_id]
        for alias in record.names:
            key = normalize_name(alias)
            canonical = matcher_key(alias)
            alias_keys = matcher_keys(alias)
            if (len(key) < 5 or not canonical or alias_keys.intersection(reserved)
                    or _edition(alias) != source_edition):
                continue
            if canonical in current_source_keys:
                # Exact source spellings already use the frozen structural
                # matcher. Register only aliases that contribute new evidence.
                continue
            if any(registry_keys.get((record.country, key), set()) != {channel_id}
                   for key in alias_keys):
                continue
            if any(source_keys.get((market, key), set()) - {epg_id} for key in alias_keys):
                continue
            # Re-resolve the spelling, including its competitors, rather than
            # trusting a spelling attached to only one iterated registry row.
            resolved = registry.resolve_channel(alias, [_country(market)])
            if resolved is None or resolved.channel_id != channel_id:
                continue
            aliases[(market, canonical)].append({
                "alias": alias, "regions": market, "epg_ids": epg_id,
                "relationship": "exact_public_registry_identity",
                "note": "Exact active registry identity " + channel_id + "; unique same-country EPG target; current programme gate required.",
            })
    return [
        min(values, key=lambda row: (row["alias"].casefold(), row["alias"]))
        for _, values in sorted(aliases.items())
        if len({row["epg_ids"] for row in values}) == 1
    ]
