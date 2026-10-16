#!/usr/bin/env python3
"""Evidence-backed channel classification using the public iptv-org database.

Only exact channel IDs/names/aliases are joined. Languages come from the
channel's main broadcast feed, never from its country or guide-text language.
Provider market prefixes are separate evidence for the app's country facet.
The private mapping and its manual/locked fields remain authoritative.

Data documentation: https://github.com/iptv-org/api#channels
Database license: https://github.com/iptv-org/database/blob/master/LICENSE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests


API_BASE = "https://iptv-org.github.io/api/"
REGISTRY_FILES = ("channels.json", "feeds.json", "countries.json")
CATALOG_FILES = (*REGISTRY_FILES, "logos.json")
MAX_CATALOG_BYTES = 25 * 1024 * 1024
MAX_CATALOG_ROWS = 150_000
SCHEMA = "skytv-channel-registry-v1"

# ISO 639-3 broadcast-language codes -> the app's preferred BCP-47 base tags.
# Unmapped valid ISO 639-3 codes remain valid three-letter BCP-47 tags.
LANGUAGE_TAGS = dict(pair.split(":") for pair in (
    "aar:aa abk:ab afr:af aka:ak amh:am ara:ar arg:an asm:as ava:av ave:ae "
    "aym:ay aze:az bak:ba bam:bm bel:be ben:bn bis:bi bod:bo bos:bs bre:br "
    "bul:bg cat:ca ces:cs cha:ch che:ce chu:cu chv:cv cor:kw cos:co cre:cr "
    "cym:cy dan:da deu:de div:dv dzo:dz ell:el eng:en epo:eo est:et eus:eu "
    "ewe:ee fao:fo fas:fa fij:fj fin:fi fra:fr fry:fy ful:ff gla:gd gle:ga "
    "glg:gl glv:gv grn:gn guj:gu hat:ht hau:ha heb:he her:hz hin:hi hmo:ho "
    "hrv:hr hun:hu hye:hy ibo:ig ido:io iii:ii iku:iu ile:ie ina:ia ind:id "
    "ipk:ik isl:is ita:it jav:jv jpn:ja kal:kl kan:kn kas:ks kat:ka kau:kr "
    "kaz:kk khm:km kik:ki kin:rw kir:ky kom:kv kon:kg kor:ko kua:kj kur:ku "
    "lao:lo lat:la lav:lv lim:li lin:ln lit:lt ltz:lb lub:lu lug:lg mah:mh "
    "mal:ml mar:mr mkd:mk mlg:mg mlt:mt mon:mn mri:mi msa:ms mya:my nau:na "
    "nav:nv nbl:nr nde:nd ndo:ng nep:ne nld:nl nno:nn nob:nb nor:no nya:ny "
    "oci:oc oji:oj ori:or orm:om oss:os pan:pa pli:pi pol:pl por:pt pus:ps "
    "que:qu roh:rm ron:ro run:rn rus:ru sag:sg san:sa sin:si slk:sk slv:sl "
    "sme:se smo:sm sna:sn snd:sd som:so sot:st spa:es sqi:sq srd:sc srp:sr "
    "ssw:ss sun:su swa:sw swe:sv tah:ty tam:ta tat:tt tel:te tgk:tg tgl:tl "
    "tha:th tir:ti ton:to tsn:tn tso:ts tuk:tk tur:tr twi:tw uig:ug ukr:uk "
    "urd:ur uzb:uz ven:ve vie:vi vol:vo wln:wa wol:wo xho:xh yid:yi yor:yo "
    "zha:za zho:zh zul:zu"
).split())

CATEGORY_GENRES = {
    "animation": "kids", "auto": "lifestyle", "business": "news",
    "classic": "entertainment", "comedy": "entertainment", "cooking": "lifestyle",
    "culture": "documentary", "documentary": "documentary", "education": "education",
    "entertainment": "entertainment", "family": "general", "general": "general",
    "interactive": "entertainment", "kids": "kids", "legislative": "news",
    "lifestyle": "lifestyle", "movies": "movies", "music": "music", "news": "news",
    "outdoor": "lifestyle", "relax": "lifestyle", "religious": "religion",
    "science": "documentary", "series": "entertainment", "shop": "shopping",
    "sports": "sports", "travel": "lifestyle", "weather": "news", "xxx": "adult",
}
COUNTRY_ALIASES = {
    "UK": "GB", "USA": "US", "CAN": "CA", "PAK": "PK", "NPL": "NP",
    "RUS": "RU", "ALB": "AL", "CAFR": "CA", "GER": "DE", "GRE": "GR",
    "UAE": "AE", "KSA": "SA", "PHL": "PH", "ROM": "RO",
}
# AR means Arabic in one provider and Argentina in another. AF is frequently
# Africa, SV Swedish, LA Latin America and AM the radio format. Do not guess.
AMBIGUOUS_PREFIXES = {"AR", "AF", "SV", "LA", "AM", "NA", "SC", "TM", "MY", "TG", "KN", "AS", "ML", "PA", "GU", "MT", "TL"}
REGION_COUNTRIES = {
    "north_america": "US CA GL PM BM",
    "latin_america": "MX GT BZ HN SV NI CR PA CO VE EC PE BO BR PY CL AR UY GF FK",
    "caribbean": "AG AI AW BB BL BQ BS CU CW DM DO GD GP HT JM KN KY LC MF MQ MS PR SX TC TT VC VG VI",
    "europe": "AD AL AT AX BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GG GI GR HR HU IE IM IS IT JE LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SJ SK SM UA VA XK",
    "middle_east": "AE BH IL IQ IR JO KW LB OM PS QA SA SY TR YE",
    "south_asia": "AF BD BT IN LK MV NP PK",
    "southeast_asia": "BN ID KH LA MM MY PH SG TH TL VN",
    "east_asia": "CN HK JP KP KR MN MO TW",
    "oceania": "AS AU CC CK CX FJ FM GU KI MH MP NC NF NR NU NZ PF PG PN PW SB TK TO TV UM VU WF WS",
    "africa": "AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG EH ER ET GA GH GM GN GQ GW KE KM LR LS LY MA MG ML MR MU MW MZ NA NE NG RE RW SC SD SH SL SN SO SS ST SZ TD TG TN TZ UG YT ZA ZM ZW",
}
COUNTRY_REGIONS = {country: region for region, countries in REGION_COUNTRIES.items() for country in countries.split()}


class RegistryError(ValueError):
    """A public registry is unavailable or violates its bounded schema."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    return [item.strip() for item in re.split(r"[,;|]", _text(value)) if item.strip()]


def _known(value: Any) -> bool:
    return _text(value).casefold() not in {"", "und", "mul", "unknown"}


def normalize_name(value: Any) -> str:
    """Remove only presentation quality/punctuation; preserve +1/channel numbers."""
    value = unicodedata.normalize("NFKD", _text(value)).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = "".join(char if char.isalnum() or char.isspace() or char in "+&" else " " for char in value).strip()
    # Quality is presentation only at the edges. Removing internal UHD would
    # incorrectly turn the separate channel BBC UHD 2 into BBC 2.
    quality = r"(?:uhd|fhd|full\s*hd|hd|sd|4k|8k|h265|hevc|h264|1080[pi]?|720p|50fps|60fps)"
    value = re.sub(rf"^(?:{quality}\s+)+|(?:\s+{quality})+$", " ", value).strip()
    value = value.replace("&", " and ").replace("+", " plus ")
    return "".join(char for char in value if char.isalnum())


def _country(value: Any) -> str:
    value = _text(value).upper()
    return COUNTRY_ALIASES.get(value, value)


def _provider_prefix(value: Any, valid_countries: set[str]) -> tuple[str, str]:
    value = _text(value)
    subgroup = re.match(r"^([A-Z]{2})-[A-Z]{2,8}\s*[|:]\s*", value)
    if subgroup and _country(subgroup.group(1)) in valid_countries and subgroup.group(1) not in AMBIGUOUS_PREFIXES:
        return _country(subgroup.group(1)), value[subgroup.end():]
    # (US), |US|, .UK and US -/US| are actual provider presentation grammar.
    matched = re.match(r"^\s*(?:\(([A-Z]{2,4})\)|\|([A-Z]{2,4})\||\.([A-Z]{2,4})\b|([A-Z]{2,4})\s*(?:[|:/-]|\s+-))\s*", value)
    if not matched:
        return "", value
    token = next(part for part in matched.groups() if part)
    country = _country(token)
    if token in AMBIGUOUS_PREFIXES or country not in valid_countries:
        # Still strip known language/region presentation, without assigning a country.
        if token in AMBIGUOUS_PREFIXES | {"EN", "ENG", "LAT", "INT", "VIP", "XX"}:
            return "", value[matched.end():]
        return "", value
    return country, value[matched.end():]


def _provider_language(raw: Mapping[str, Any]) -> list[str]:
    # Explicit language labels, including the provider's Indian language
    # banks. A country label alone never establishes a broadcast language.
    names = {
        "EN": "en", "ENG": "en", "HIN": "hi", "HINDI": "hi",
        "PUNJ": "pa", "PUNJABI": "pa", "TM": "ta", "TAM": "ta",
        "TG": "te", "TEL": "te", "MY": "ml", "MAL": "ml",
        "KN": "kn", "KAN": "kn", "GUJ": "gu", "MAR": "mr",
        "BENG": "bn", "URD": "ur",
    }
    found = []
    for field in ("channel_name", "canonical_name", "channelName", "canonicalName"):
        match = re.match(r"^(?:IN-)?([A-Z]{2,7})\s*[|:/-]", _text(raw.get(field)))
        if match and match.group(1) in names:
            code = names[match.group(1)]
            # MY alone is Malaysia, KN alone is Saint Kitts. Restrict the
            # abbreviated Indian language bank tokens to IN- qualified names.
            if match.group(1) in {"MY", "KN", "TM", "TG"} and not _text(raw.get(field)).startswith("IN-"):
                continue
            if code not in found:
                found.append(code)
    return found


def _validate_catalog(filename: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_CATALOG_ROWS:
        raise RegistryError(f"{filename}: expected a bounded nonempty JSON list")
    for row in payload:
        if not isinstance(row, dict):
            raise RegistryError(f"{filename}: invalid record")
        required = {"channels.json": ("id", "name", "country"), "feeds.json": ("channel", "id"), "countries.json": ("code", "name"), "logos.json": ("channel", "url")}[filename]
        if any(not isinstance(row.get(key), str) or not 0 < len(row[key]) <= (2048 if key == "url" else 512) for key in required):
            raise RegistryError(f"{filename}: missing/invalid identity")
        fields = {"channels.json": ("alt_names", "categories"), "feeds.json": ("languages", "broadcast_area"), "countries.json": ("languages",), "logos.json": ("tags",)}[filename]
        for field in fields:
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > 250 or any(not isinstance(item, str) or len(item) > 512 for item in values):
                raise RegistryError(f"{filename}: invalid {field}")
        if filename in {"channels.json", "countries.json"} and not re.fullmatch(r"[A-Z]{2}", row.get("country", row.get("code", ""))):
            raise RegistryError(f"{filename}: invalid country code")
        if filename == "feeds.json" and (not isinstance(row.get("is_main", False), bool) or any(not re.fullmatch(r"[a-z]{3}", code) for code in row.get("languages", []))):
            raise RegistryError(f"{filename}: invalid broadcast language/feed flag")
        if filename == "logos.json" and (not isinstance(row.get("in_use"), bool) or row.get("feed") is not None and not isinstance(row["feed"], str)):
            raise RegistryError(f"{filename}: invalid current logo/feed flag")
    return payload


def _read_catalog(directory: Path, filename: str) -> list[dict[str, Any]]:
    path = directory / filename
    if path.stat().st_size > MAX_CATALOG_BYTES:
        raise RegistryError(f"{filename}: exceeds size limit")
    return _validate_catalog(filename, json.loads(path.read_text(encoding="utf-8-sig")))


@dataclass(frozen=True)
class ChannelRecord:
    channel_id: str
    name: str
    aliases: tuple[str, ...]
    country: str
    languages: tuple[str, ...]
    categories: tuple[str, ...]
    broadcast_countries: tuple[str, ...]
    is_nsfw: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class ChannelRegistry:
    def __init__(self, channels: Sequence[Mapping[str, Any]], feeds: Sequence[Mapping[str, Any]], countries: Sequence[Mapping[str, Any]]):
        self.country_names = {_country(row["code"]): _text(row["name"]) for row in countries}
        self.country_names["GB"] = self.country_names.get("GB", self.country_names.get("UK", "United Kingdom"))
        self.valid_countries = set(self.country_names)
        feed_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for feed in feeds:
            feed_rows[_text(feed["channel"])].append(feed)
        self.records: dict[str, ChannelRecord] = {}
        self.by_id: dict[str, ChannelRecord] = {}
        self.by_name: dict[str, set[str]] = defaultdict(set)
        for channel in channels:
            if channel.get("closed") or channel.get("replaced_by"):
                continue
            channel_id = _text(channel["id"])
            channel_feeds = feed_rows.get(channel_id, [])
            main_feeds = [feed for feed in channel_feeds if feed.get("is_main") is True]
            if main_feeds:
                languages = sorted({LANGUAGE_TAGS.get(code, code) for feed in main_feeds for code in feed.get("languages", []) if code not in {"und", "mul"}})
            else:
                # A channel with different regional feed languages has no defensible
                # language for an unidentified feed. Require unanimous feed evidence.
                variants = {tuple(sorted(feed.get("languages", []))) for feed in channel_feeds}
                languages = sorted({LANGUAGE_TAGS.get(code, code) for code in next(iter(variants), ()) if code not in {"und", "mul"}}) if len(variants) == 1 else []
            record = ChannelRecord(
                channel_id=channel_id, name=_text(channel["name"]),
                aliases=tuple(channel.get("alt_names", [])), country=_country(channel["country"]),
                languages=tuple(languages), categories=tuple(channel.get("categories", [])),
                broadcast_countries=tuple(sorted({_country(area[2:]) for feed in channel_feeds for area in feed.get("broadcast_area", []) if re.fullmatch(r"c/[A-Z]{2}", area)})),
                is_nsfw=channel.get("is_nsfw") is True,
            )
            self.records[channel_id] = record
            self.by_id[channel_id.casefold()] = record
            for name in (*record.names, channel_id.rsplit(".", 1)[0]):
                normalized = normalize_name(name)
                if len(normalized) >= 2:
                    self.by_name[normalized].add(channel_id)

    @classmethod
    def from_directory(cls, directory: Path | str) -> "ChannelRegistry":
        directory = Path(directory)
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema") != SCHEMA:
                raise RegistryError("Invalid registry manifest schema")
            for filename in CATALOG_FILES:
                path = directory / filename
                if path.stat().st_size > MAX_CATALOG_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("sha256", {}).get(filename):
                    raise RegistryError(f"Registry cache integrity failed: {filename}")
        catalogs = [_read_catalog(directory, filename) for filename in REGISTRY_FILES]
        return cls(*catalogs)

    def resolve_channel(self, name: str, countries: Iterable[str] = (), epg_id: str = "") -> ChannelRecord | None:
        """Resolve one exact name/alias/ID; never pick among ambiguous brands."""
        direct = self.by_id.get(_text(epg_id).casefold())
        if direct:
            return direct
        prefix_country, stripped = _provider_prefix(name, self.valid_countries)
        ids = self.by_name.get(normalize_name(stripped), set())
        if not ids:
            return None
        hints = {_country(value) for value in countries if value} | ({prefix_country} if prefix_country else set())
        if hints:
            origins = {channel_id for channel_id in ids if self.records[channel_id].country in hints}
            ids = origins or {channel_id for channel_id in ids if hints.intersection(self.records[channel_id].broadcast_countries)}
        return self.records[next(iter(ids))] if len(ids) == 1 else None

    def country_evidence(self, raw: Mapping[str, Any]) -> tuple[list[str], str]:
        for key in ("channel_name", "canonical_name", "channelName", "canonicalName", "category_name", "providerCategory"):
            country, _ = _provider_prefix(raw.get(key), self.valid_countries)
            if country:
                return [country], "provider_prefix"
        source = _text(raw.get("source", raw.get("requested_source"))).casefold()
        if source != "dummy":
            suffix = _text(raw.get("epg_id")).rsplit(".", 1)[-1].upper()
            suffix = re.sub(r"(?:_LOCALS|_SPORTS)?\d+$", "", suffix)
            country = _country(suffix)
            if country in self.valid_countries:
                return [country], "epg_id_suffix"
            feed = _text(raw.get("epg_feed")).upper()
            matched = re.fullmatch(r"([A-Z]{2})(?:_LOCALS|_SPORTS)?\d+", feed)
            if matched and _country(matched.group(1)) in self.valid_countries:
                return [_country(matched.group(1))], "epg_feed_market"
        return [], ""


def enrich_metadata(raw: Mapping[str, Any], metadata: Any, registry: ChannelRegistry) -> tuple[Any, dict[str, Any]]:
    """Return a replaced ChannelMetadata while respecting Sheet field authority.

    Call immediately after build_metadata(raw, row_number), with the original
    raw Sheet dictionary. Passing raw={} is safe but cannot distinguish an
    explicit per-field value from an older inferred value; enrich_rows fills
    only unknown fields for that reason.
    """
    if metadata.locked or (metadata.source == "manual" and (
        _text(raw.get("metadata_source")).casefold() == "manual" or metadata.status != "auto"
    )):
        return metadata, {}
    name = _text(raw.get("channel_name", raw.get("channelName", raw.get("canonical_name", raw.get("canonicalName")))))
    if not name or re.match(r"^\s*[#=_]{3,}", name) or metadata.channel_role in {"event", "ppv", "virtual"}:
        return metadata, {}
    market, market_method = registry.country_evidence(raw)
    country_hints = _list(raw.get("country_codes")) or market or list(metadata.countries)
    record = registry.resolve_channel(name, country_hints, _text(raw.get("epg_id")))
    if record is None:
        canonical = _text(raw.get("canonical_name", raw.get("canonicalName")))
        if canonical and canonical != name:
            record = registry.resolve_channel(canonical, country_hints)
    updates: dict[str, Any] = {}
    facets: dict[str, str] = {}
    if not _text(raw.get("country_codes")):
        countries = market or ([record.country] if record else [])
        if countries and countries != metadata.countries:
            updates["countries"] = countries
            facets["countries"] = market_method if market else "registry_channel_origin"
    countries = updates.get("countries", metadata.countries)
    if countries and not _known(raw.get("region_code")):
        regions = {COUNTRY_REGIONS[country] for country in countries if country in COUNTRY_REGIONS}
        if len(regions) == 1 and metadata.region != next(iter(regions)):
            updates["region"] = next(iter(regions))
            facets["region"] = "country_region"
    languages = _provider_language(raw) or (list(record.languages) if record else [])
    if not any(_known(code) for code in _list(raw.get("language_codes"))) and not any(_known(code) for code in metadata.languages) and not _known(raw.get("primary_language")) and languages:
        updates["languages"] = languages
        updates["primary_language"] = languages[0] if len(languages) == 1 else "mul"
        facets["languages"] = "provider_language_prefix" if _provider_language(raw) else "registry_broadcast_feed"
    if record:
        if record.is_nsfw:
            # Exact adult identity strengthens an automatic classification even
            # when a broad provider category already supplied a nonadult genre.
            # Manual/locked rows have returned above and retain their authority.
            adult = {"genre": "adult", "audiences": ["adults"], "content_rating": "adult",
                     "sports": [], "religions": []}
            changed = {key: value for key, value in adult.items() if getattr(metadata, key) != value}
            if changed:
                updates.update(changed)
                facets["genre"] = "registry_nsfw_identity"
        elif not _known(raw.get("genre")) and metadata.genre == "unknown":
            genres = [CATEGORY_GENRES[category] for category in record.categories if category in CATEGORY_GENRES]
            genre = genres[0] if genres else ""
            if genre:
                updates["genre"] = genre
                facets["genre"] = "registry_channel_category"
                if genre == "adult":
                    updates.update(audiences=["adults"], content_rating="adult", sports=[], religions=[])
                elif genre == "kids" and not _text(raw.get("audience_codes")):
                    updates["audiences"] = ["kids"]
    if not updates:
        return metadata, {}
    # These are still automatic assertions. Do not silently approve existing
    # review rows or weaken manual confidence controls.
    updates["source"] = "registry" if record else "provider_category"
    if not _text(raw.get("metadata_confidence")):
        updates["confidence"] = round(min(max(metadata.confidence, 0.82), 0.96), 3)
    evidence = {"facets": facets, "method": "exact_channel_registry" if record else market_method}
    if record:
        evidence.update(channelId=record.channel_id, source=API_BASE + "channels.json", languageSource=API_BASE + "feeds.json")
    return replace(metadata, **updates), evidence


def enrich_rows(rows: Sequence[Any], registry: ChannelRegistry) -> tuple[list[Any], dict[str, Any]]:
    """Conservative overlay for MappingRows after original Sheet cells are gone."""
    output, counts = [], Counter()
    for row in rows:
        metadata = row.metadata
        raw = {key: getattr(row, key, "") for key in ("channel_name", "canonical_name", "epg_id", "epg_feed", "requested_source", "category_name")}
        # Preserve already populated values: only the raw-aware entry point
        # may correct a weak previous inference with stronger market evidence.
        raw.update(country_codes="|".join(metadata.countries), region_code=metadata.region if metadata.region != "unknown" else "", genre=metadata.genre if metadata.genre != "unknown" else "", language_codes="|".join(code for code in metadata.languages if code != "und"), primary_language=metadata.primary_language if metadata.primary_language != "und" else "")
        changed, evidence = enrich_metadata(raw, metadata, registry)
        output.append(replace(row, metadata=changed) if evidence else row)
        counts.update(evidence.get("facets", {}).keys())
        counts["channels"] += bool(evidence)
    return output, dict(counts)


def fetch_registry(destination: Path | str, *, max_age_hours: float = 24, stale_max_days: float = 30, session: Any = None) -> dict[str, Any]:
    """Refresh a bounded validated cache; retain a verified recent stale cache."""
    destination = Path(destination)
    cached_manifest = None
    try:
        ChannelRegistry.from_directory(destination)
        cached_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        age = time.time() - float(cached_manifest["fetchedAtEpoch"])
        if 0 <= age < max_age_hours * 3600:
            return {**cached_manifest, "cacheState": "fresh"}
    except (OSError, ValueError, KeyError, TypeError):
        cached_manifest = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    deadline = time.monotonic() + 120
    try:
        with tempfile.TemporaryDirectory(prefix="channel-registry-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            digests = {}
            for filename in CATALOG_FILES:
                with client.get(API_BASE + filename, stream=True, timeout=(10, 35), allow_redirects=False, headers={"User-Agent": "SkyTV-EPG-Metadata/1.0", "Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        raise RegistryError(f"Public registry download failed: {filename} HTTP {response.status_code}")
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() > deadline:
                            raise RegistryError("Public registry download exceeded total deadline")
                        content.extend(chunk)
                        if len(content) > MAX_CATALOG_BYTES:
                            raise RegistryError(f"{filename}: download exceeds size limit")
                    _validate_catalog(filename, json.loads(content))
                    (stage / filename).write_bytes(content)
                    digests[filename] = hashlib.sha256(content).hexdigest()
            registry = ChannelRegistry.from_directory(stage)
            now = int(time.time())
            manifest = {"schema": SCHEMA, "fetchedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(), "fetchedAtEpoch": now, "sources": [API_BASE + filename for filename in CATALOG_FILES], "sha256": digests, "channelCount": len(registry.records)}
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            destination.mkdir(parents=True, exist_ok=True)
            for filename in (*CATALOG_FILES, "manifest.json"):
                os.replace(stage / filename, destination / filename)
            return {**manifest, "cacheState": "downloaded"}
    except (requests.RequestException, OSError, ValueError) as exc:
        if cached_manifest is not None and 0 <= time.time() - float(cached_manifest["fetchedAtEpoch"]) <= stale_max_days * 86400:
            return {**cached_manifest, "cacheState": "stale", "refreshError": type(exc).__name__}
        raise RegistryError("No validated recent public channel registry is available") from exc
    finally:
        if session is None:
            client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch"])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--stale-max-days", type=float, default=30)
    args = parser.parse_args(argv)
    if args.max_age_hours < 0 or args.stale_max_days < 0:
        parser.error("Cache ages must be nonnegative")
    report = fetch_registry(args.destination, max_age_hours=args.max_age_hours, stale_max_days=args.stale_max_days)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
