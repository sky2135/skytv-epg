"""Compact SKY TV app EPG exporter.

This module converts the approved final mapping CSV plus the XMLTV file already
built for TiviMate into the compact JSON schema consumed by the SKY TV app.
It never rematches channels.  Every stream-to-EPG decision comes from the
approved ``server_X_final_mapping.csv`` file.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import pandas as pd
import requests
from lxml import etree

APP_SCHEMA_VERSION = 1
APP_EXPORTER_VERSION = "1.1"
APP_EXPORTER_BUILD_ID = "SKYTV-APP-EXPORT-1.1-2026-08-11"
DEFAULT_PAST_HOURS = 6
DEFAULT_FUTURE_HOURS = 72

CORE_PAYLOAD_KEYS = frozenset(
    {
        "schemaVersion",
        "serverId",
        "generatedAt",
        "windowStart",
        "windowEnd",
        "streamToEpg",
        "programmes",
    }
)
CORE_MANIFEST_KEYS = frozenset(
    {
        "schemaVersion",
        "serverId",
        "generatedAt",
        "generatedAtIso",
        "windowStart",
        "windowEnd",
        "dataFile",
        "dataSha256",
        "mappingSha256",
        "compressedBytes",
        "mappedStreams",
        "streamsWithProgrammes",
        "coveragePercent",
        "uniqueSchedulesWithProgrammes",
        "programmeRows",
    }
)
REJECTED_ACTIONS = frozenset(
    {
        "REVIEW",
        "UNMATCHED",
        "NO_EPG",
        "UNRESOLVED",
        "SKIP",
        "IGNORE",
        "REJECTED",
    }
)


class AppExportError(RuntimeError):
    """Raised when an app EPG package cannot be built safely."""


def _stream_sort_key(value: object) -> tuple[int, object]:
    text = str(value or "").strip()
    try:
        return (0, int(text))
    except (TypeError, ValueError):
        return (1, text.casefold())


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_maybe_gzip(path: Path):
    path = Path(path)
    with path.open("rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return path.open("rb")


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _child_text(element: etree._Element, wanted_name: str) -> str:
    fallback = ""
    for child in element:
        if _local_name(child.tag) != wanted_name:
            continue
        text = " ".join("".join(child.itertext()).split())
        if not text:
            continue
        language = (child.get("lang") or "").lower()
        if language in {"en", "eng", "en-us", "en-gb"}:
            return text
        if not fallback:
            fallback = text
    return fallback


def parse_xmltv_time(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.strip().split()
    if not parts:
        return None
    stamp = parts[0]
    if not stamp.isdigit() or len(stamp) < 12:
        return None
    stamp = stamp[:14]
    fmt = "%Y%m%d%H%M%S" if len(stamp) >= 14 else "%Y%m%d%H%M"
    try:
        parsed = datetime.strptime(stamp, fmt)
    except ValueError:
        return None

    token = parts[1] if len(parts) > 1 else "+0000"
    if token.upper() in {"Z", "UTC", "GMT"}:
        tz = timezone.utc
    else:
        match = re.fullmatch(r"([+-])(\d{2})(\d{2})", token)
        if match is None:
            tz = timezone.utc
        else:
            sign = 1 if match.group(1) == "+" else -1
            offset = timedelta(
                hours=int(match.group(2)), minutes=int(match.group(3))
            )
            tz = timezone(sign * offset)
    return int(parsed.replace(tzinfo=tz).timestamp())


def load_dummy_feeds(source_config: Path | None) -> set[str]:
    if source_config is None:
        return {"DUMMY_CHANNELS"}
    path = Path(source_config)
    if not path.is_file():
        raise FileNotFoundError(f"EPG source configuration is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    feeds = {
        str(item.get("name") or item.get("source_code") or "").strip()
        for item in payload.get("sources", [])
        if str(item.get("kind") or "").strip().casefold() == "dummy"
        and bool(item.get("enabled", True))
    }
    return {value for value in feeds if value} or {"DUMMY_CHANNELS"}


def _mapping_feed_key(row: pd.Series) -> str:
    source = str(row.get("source", "")).strip().casefold()
    feed = str(row.get("epg_feed", "")).strip()
    if source == "panel" or feed.casefold() == "server xmltv.php":
        return "PANEL"
    return feed


def prepare_app_export_mapping(
    mapping: pd.DataFrame,
    server_id: str,
    *,
    dummy_feeds: Iterable[str] = ("DUMMY_CHANNELS",),
) -> pd.DataFrame:
    """Normalize an approved final mapping for the compact app schema."""
    if not isinstance(mapping, pd.DataFrame):
        mapping = pd.DataFrame(mapping)
    out = mapping.copy()

    aliases = {
        "selected_epg_id": "epg_id",
        "chosen_epg_id": "epg_id",
        "selected_feed": "epg_feed",
        "chosen_feed": "epg_feed",
    }
    for old, new in aliases.items():
        if new not in out.columns and old in out.columns:
            out[new] = out[old]

    if "server_id" not in out.columns:
        out["server_id"] = server_id
    out["server_id"] = (
        out["server_id"].fillna("").astype(str).str.strip().replace("", server_id)
    )
    out = out[out["server_id"].eq(server_id)].copy()

    text_columns = [
        "stream_id",
        "channel_name",
        "action",
        "source",
        "epg_id",
        "epg_feed",
        "feed_key",
        "effective_feed_key",
        "effective_epg_id",
        "has_programmes",
    ]
    for column in text_columns:
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].fillna("").astype(str).str.strip()

    if "action" in out.columns:
        out = out[~out["action"].str.upper().isin(REJECTED_ACTIONS)].copy()

    missing_feed = out["feed_key"].eq("")
    if missing_feed.any():
        out.loc[missing_feed, "feed_key"] = out.loc[missing_feed].apply(
            _mapping_feed_key, axis=1
        )
    out["effective_feed_key"] = out["effective_feed_key"].where(
        out["effective_feed_key"].ne(""), out["feed_key"]
    )
    out["effective_epg_id"] = out["effective_epg_id"].where(
        out["effective_epg_id"].ne(""), out["epg_id"]
    )

    out = out[
        out["stream_id"].ne("")
        & out["channel_name"].ne("")
        & out["effective_feed_key"].ne("")
        & out["effective_epg_id"].ne("")
    ].copy()
    out = out.drop_duplicates(["server_id", "stream_id"], keep="last")
    if out.empty:
        raise AppExportError(f"No usable approved mapping rows were found for {server_id}.")

    out["app_schedule_key"] = (
        out["effective_feed_key"] + "::" + out["effective_epg_id"]
    )
    dummy_lookup = {str(value).strip().casefold() for value in dummy_feeds if str(value).strip()}

    def candidates(row: pd.Series) -> list[str]:
        feed = str(row.get("effective_feed_key", ""))
        epg_id = str(row.get("effective_epg_id", ""))
        original_epg_id = str(row.get("epg_id", ""))
        channel_name = str(row.get("channel_name", ""))
        ordered: list[str]
        if feed.casefold() in dummy_lookup:
            ordered = [channel_name, epg_id, original_epg_id]
        else:
            ordered = [epg_id, original_epg_id, channel_name]
        return [value for value in dict.fromkeys(ordered) if value]

    out["xmltv_channel_candidates"] = out.apply(candidates, axis=1)
    return out


def _scan_xmltv_channel_ids(xmltv_path: Path) -> dict[str, str]:
    available: dict[str, str] = {}
    with _open_maybe_gzip(Path(xmltv_path)) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            name = _local_name(element.tag)
            if name == "channel":
                channel_id = (element.get("id") or "").strip()
                if channel_id:
                    available.setdefault(channel_id.casefold(), channel_id)
            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
        del context
    return available


def parse_tivimate_xmltv_for_app(
    xmltv_path: Path,
    prepared: pd.DataFrame,
    *,
    window_start: int,
    window_end: int,
) -> tuple[dict[str, list[list[object]]], dict[str, Any]]:
    """Extract only the schedules referenced by the approved mapping."""
    available = _scan_xmltv_channel_ids(Path(xmltv_path))
    channel_to_schedules: dict[str, set[str]] = defaultdict(set)
    missing_schedule_keys: set[str] = set()

    for row in prepared.itertuples(index=False):
        selected = ""
        for candidate in row.xmltv_channel_candidates:
            actual = available.get(str(candidate).casefold())
            if actual:
                selected = actual
                break
        if selected:
            channel_to_schedules[selected.casefold()].add(str(row.app_schedule_key))
        else:
            missing_schedule_keys.add(str(row.app_schedule_key))

    programme_sets: dict[str, set[tuple[int, int, str]]] = defaultdict(set)
    with _open_maybe_gzip(Path(xmltv_path)) as source:
        context = etree.iterparse(source, events=("end",), recover=True, huge_tree=True)
        for _event, element in context:
            element_name = _local_name(element.tag)
            if element_name != "programme":
                # Do not clear <title> or other programme children before the
                # enclosing <programme> element is processed.  Channel elements
                # can be released immediately because only programme data is
                # needed in this pass.
                if element_name == "channel":
                    element.clear()
                    parent = element.getparent()
                    if parent is not None:
                        while element.getprevious() is not None:
                            del parent[0]
                continue

            source_channel = (element.get("channel") or "").strip().casefold()
            schedule_keys = channel_to_schedules.get(source_channel)
            if schedule_keys:
                start = parse_xmltv_time(element.get("start"))
                stop = parse_xmltv_time(element.get("stop"))
                title = _child_text(element, "title")
                if start is not None and title.strip():
                    if stop is None or stop <= start:
                        stop = start + 3600
                    if int(stop) > int(window_start) and int(start) < int(window_end):
                        programme = (int(start), int(stop), str(title))
                        for schedule_key in schedule_keys:
                            programme_sets[schedule_key].add(programme)

            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
        del context

    programmes: dict[str, list[list[object]]] = {}
    for schedule_key in sorted(programme_sets, key=str.casefold):
        rows = sorted(
            programme_sets[schedule_key],
            key=lambda item: (int(item[0]), int(item[1]), str(item[2])),
        )
        if rows:
            programmes[schedule_key] = [list(row) for row in rows]

    diagnostics = {
        "xmltvChannelIds": len(available),
        "selectedXmltvChannelIds": len(channel_to_schedules),
        "mappedSchedulesWithoutXmltvChannel": len(missing_schedule_keys),
        "mappedSchedulesWithoutXmltvChannelExamples": sorted(missing_schedule_keys)[:25],
    }
    return programmes, diagnostics


def validate_app_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = sorted(CORE_PAYLOAD_KEYS - set(payload))
    if missing:
        raise AppExportError(
            "The app payload is missing required fields: " + ", ".join(missing)
        )
    stream_to_epg = payload.get("streamToEpg")
    programmes = payload.get("programmes")
    if not isinstance(stream_to_epg, dict) or not isinstance(programmes, dict):
        raise AppExportError("streamToEpg and programmes must both be JSON objects.")

    malformed = 0
    out_of_order = 0
    for rows in programmes.values():
        if not isinstance(rows, list):
            malformed += 1
            continue
        previous: tuple[int, int, str] | None = None
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                malformed += 1
                continue
            try:
                current = (int(row[0]), int(row[1]), str(row[2]))
            except (TypeError, ValueError):
                malformed += 1
                continue
            if current[1] <= current[0] or not current[2].strip():
                malformed += 1
            if previous is not None and current < previous:
                out_of_order += 1
            previous = current
    if malformed:
        raise AppExportError(f"The app payload contains {malformed:,} malformed programme rows.")
    if out_of_order:
        raise AppExportError(f"The app payload contains {out_of_order:,} out-of-order rows.")

    schedule_values = {str(value) for value in stream_to_epg.values()}
    schedule_keys = {str(value) for value in programmes}
    streams_with_programmes = sum(
        1 for value in stream_to_epg.values() if str(value) in schedule_keys
    )
    return {
        "schemaVersion": int(payload["schemaVersion"]),
        "mappedStreams": len(stream_to_epg),
        "streamsWithProgrammes": streams_with_programmes,
        "mappedSchedules": len(schedule_values),
        "schedulesWithProgrammes": len(schedule_keys),
        "mappedSchedulesWithoutProgrammes": len(schedule_values - schedule_keys),
        "programmeRows": sum(len(rows) for rows in programmes.values()),
        "malformedProgrammeRows": 0,
        "outOfOrderProgrammeRows": 0,
    }


def _mapping_sha256(prepared: pd.DataFrame) -> str:
    stable = prepared[["stream_id", "app_schedule_key"]].copy()
    stable["stream_id"] = stable["stream_id"].astype(str)
    stable["app_schedule_key"] = stable["app_schedule_key"].astype(str)
    stable = stable.sort_values(
        "stream_id", key=lambda values: values.map(_stream_sort_key)
    )
    content = stable.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return _sha256_bytes(content)


def _read_gzip_json_bytes(content: bytes) -> dict[str, Any]:
    return dict(json.loads(gzip.decompress(content).decode("utf-8-sig")))


def _preserved_extensions(
    payload: Mapping[str, Any] | None,
    core_keys: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): value for key, value in payload.items() if str(key) not in core_keys}


def load_preserved_extensions_from_directory(
    directory: Path,
    server_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    directory = Path(directory)
    data_path = directory / f"{server_id}_epg.json.gz"
    manifest_path = directory / f"{server_id}_epg_manifest.json"
    details: dict[str, Any] = {
        "source": str(directory),
        "payloadFound": data_path.is_file(),
        "manifestFound": manifest_path.is_file(),
        "error": "",
    }
    payload: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    try:
        if data_path.is_file():
            payload = _read_gzip_json_bytes(data_path.read_bytes())
        if manifest_path.is_file():
            manifest = dict(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
    except Exception as exc:  # preservation is best-effort, not a build dependency
        details["error"] = f"{type(exc).__name__}: {exc}"
        payload = {}
        manifest = {}
    return (
        _preserved_extensions(payload, CORE_PAYLOAD_KEYS),
        _preserved_extensions(manifest, CORE_MANIFEST_KEYS),
        details,
    )


def _url_with_cache_buster(url: str) -> str:
    separator = "&" if "?" in url else "?"
    stamp = int(datetime.now(timezone.utc).timestamp())
    return f"{url}{separator}skytv_refresh={stamp}"


def load_preserved_extensions_from_url(
    base_url: str,
    server_id: str,
    *,
    timeout: int = 30,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return {}, {}, {"source": "", "payloadFound": False, "manifestFound": False, "error": ""}

    data_url = f"{base}/{quote(server_id)}_epg.json.gz"
    manifest_url = f"{base}/{quote(server_id)}_epg_manifest.json"
    details: dict[str, Any] = {
        "source": base,
        "payloadFound": False,
        "manifestFound": False,
        "error": "",
    }
    payload: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "SKYTV-App-EPG-Exporter/1.1"})
    errors: list[str] = []
    try:
        data_response = session.get(_url_with_cache_buster(data_url), timeout=timeout)
        if data_response.status_code == 200:
            payload = _read_gzip_json_bytes(data_response.content)
            details["payloadFound"] = True
        elif data_response.status_code != 404:
            errors.append(f"data HTTP {data_response.status_code}")

        manifest_response = session.get(
            _url_with_cache_buster(manifest_url), timeout=timeout
        )
        if manifest_response.status_code == 200:
            manifest = dict(manifest_response.json())
            details["manifestFound"] = True
        elif manifest_response.status_code != 404:
            errors.append(f"manifest HTTP {manifest_response.status_code}")
    except Exception as exc:  # preservation must not prevent fresh EPG generation
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        session.close()

    details["error"] = "; ".join(errors)
    return (
        _preserved_extensions(payload, CORE_PAYLOAD_KEYS),
        _preserved_extensions(manifest, CORE_MANIFEST_KEYS),
        details,
    )


def write_app_epg_files(
    *,
    mapping: pd.DataFrame,
    server_id: str,
    xmltv_path: Path,
    output_dir: Path,
    source_manifest: Mapping[str, Any] | None = None,
    dummy_feeds: Iterable[str] = ("DUMMY_CHANNELS",),
    past_hours: int = DEFAULT_PAST_HOURS,
    future_hours: int = DEFAULT_FUTURE_HOURS,
    preserved_payload_extensions: Mapping[str, Any] | None = None,
    preserved_manifest_extensions: Mapping[str, Any] | None = None,
    preservation_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = prepare_app_export_mapping(
        mapping, server_id, dummy_feeds=dummy_feeds
    )
    source_manifest = dict(source_manifest or {})
    generated_at = int(
        source_manifest.get("generatedAt")
        or datetime.now(timezone.utc).timestamp()
    )
    generated_iso = str(source_manifest.get("generatedAtIso") or "").strip()
    if not generated_iso:
        generated_iso = (
            datetime.fromtimestamp(generated_at, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    window_start = generated_at - int(past_hours) * 3600
    window_end = generated_at + int(future_hours) * 3600

    programmes, xml_diagnostics = parse_tivimate_xmltv_for_app(
        Path(xmltv_path),
        prepared,
        window_start=window_start,
        window_end=window_end,
    )

    ordered = prepared.sort_values(
        "stream_id", key=lambda values: values.map(_stream_sort_key)
    )
    stream_to_epg = {
        str(row.stream_id): str(row.app_schedule_key)
        for row in ordered.itertuples(index=False)
    }

    core_payload: dict[str, Any] = {
        "schemaVersion": APP_SCHEMA_VERSION,
        "serverId": server_id,
        "generatedAt": generated_at,
        "windowStart": window_start,
        "windowEnd": window_end,
        "streamToEpg": stream_to_epg,
        "programmes": programmes,
    }
    payload = dict(preserved_payload_extensions or {})
    payload.update(core_payload)  # approved fresh EPG fields always win
    validation = validate_app_payload(payload)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / f"{server_id}_epg.json.gz"
    json_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    with data_path.open("wb") as raw:
        with gzip.GzipFile(
            filename=data_path.name[:-3],
            fileobj=raw,
            mode="wb",
            compresslevel=9,
            mtime=0,
        ) as compressed:
            compressed.write(json_bytes)

    mapping_sha = str(source_manifest.get("mappingSha256") or "").strip()
    if not mapping_sha:
        mapping_sha = _mapping_sha256(prepared)

    mapped_streams = len(stream_to_epg)
    streams_with_programmes = int(validation["streamsWithProgrammes"])
    core_manifest: dict[str, Any] = {
        "schemaVersion": APP_SCHEMA_VERSION,
        "serverId": server_id,
        "generatedAt": generated_at,
        "generatedAtIso": generated_iso,
        "windowStart": window_start,
        "windowEnd": window_end,
        "dataFile": data_path.name,
        "dataSha256": sha256_file(data_path),
        "mappingSha256": mapping_sha,
        "compressedBytes": data_path.stat().st_size,
        "mappedStreams": mapped_streams,
        "streamsWithProgrammes": streams_with_programmes,
        "coveragePercent": round(
            streams_with_programmes * 100.0 / max(1, mapped_streams), 2
        ),
        "uniqueSchedulesWithProgrammes": len(programmes),
        "programmeRows": int(validation["programmeRows"]),
    }
    manifest = dict(preserved_manifest_extensions or {})
    manifest.update(core_manifest)
    manifest_path = output_dir / f"{server_id}_epg_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    report = {
        "exporterVersion": APP_EXPORTER_VERSION,
        "exporterBuildId": APP_EXPORTER_BUILD_ID,
        "serverId": server_id,
        "sourceFormat": "approved final mapping + generated TiviMate XMLTV",
        "pastHoursRetained": int(past_hours),
        "futureHoursRetained": int(future_hours),
        "preservedPayloadExtensionKeys": sorted(
            str(key) for key in (preserved_payload_extensions or {})
        ),
        "preservedManifestExtensionKeys": sorted(
            str(key) for key in (preserved_manifest_extensions or {})
        ),
        "preservation": dict(preservation_details or {}),
        "xmltv": xml_diagnostics,
        "validation": validation,
    }
    return {
        "dataPath": data_path,
        "manifestPath": manifest_path,
        "manifest": manifest,
        "validation": report,
        "preparedMappingRows": len(prepared),
    }
