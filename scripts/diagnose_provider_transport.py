#!/usr/bin/env python3
"""Bounded provider transport checks; never print bodies or credential URLs."""
from __future__ import annotations

import argparse
import gzip
import ipaddress
import json
import os
import re
import socket
import sys
import time
import zlib
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from lxml import etree

import build_epg_streaming as epg
import build_vod_catalogs as vod
import sync_channel_inventory as inventory


MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_PUBLIC_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_NATIVE_EXPANDED_BYTES = 1024 * 1024 * 1024
NATIVE_SERVERS = ("server_2", "server_3")
ALL_SERVERS = ("server_1", *NATIVE_SERVERS)
NEAR_FUTURE_HOURS = 48


def inventory_error_code(error: Exception) -> str:
    text = str(error).casefold()
    for fragment, code in (
        ("persistence was blocked", "reflection_guard"),
        ("password is too short", "short_password_guard"),
        ("malformed percent", "malformed_transport_text"),
        ("malformed html", "malformed_transport_text"),
        ("transport-decoding safety limit", "text_scan_limit"),
        ("account is not active", "inactive_account"),
        ("rejected its configured credentials", "auth_rejected"),
        ("unsafe credential redirect", "redirect_guard"),
        ("network request failed", "network_error"),
        ("duplicate", "duplicate_identity"),
        ("conflicting", "conflicting_identity"),
        ("id or name but not both", "partial_identity"),
        ("non-channel row", "non_channel_row"),
        ("non-object item", "non_object_row"),
        ("empty item", "empty_row"),
        ("unparseable item", "unparseable_row"),
        ("no valid live inventory", "no_valid_inventory"),
    ):
        if fragment in text:
            return code
    return "sync_error"


def raw_stream_schema_summary(payload: list) -> dict:
    issues = {}
    valid_count = 0
    for raw in payload:
        if not isinstance(raw, dict):
            code = "non_object_row"
        elif not raw:
            code = "empty_row"
        else:
            try:
                row = inventory.normalize_stream_row(raw, allow_generic_id=True)
                if row is not None:
                    valid_count += 1
                    continue
                code = "unparseable_row"
            except inventory.SyncError as error:
                code = inventory_error_code(error)
        issues[code] = issues.get(code, 0) + 1
    return {"validRowCount":valid_count, "invalidRowCounts":issues}


def actual_inventory_probe(config, allow_http: bool) -> dict:
    sync_config = inventory.ServerConfig(config.server_id, config.server_id.replace("_", " ").title(), config.base_url,
                                         config.username, config.password)
    result = {"serverId": config.server_id}
    with clean_session() as session:
        session.headers.update({"User-Agent": f"SKYTV-Channel-Inventory/{inventory.SYNC_VERSION}",
                                "Accept": vod.PROVIDER_HEADERS["Accept"]})
        try:
            value = inventory.fetch_panel_inventory(session, sync_config, allow_insecure_http=allow_http)
            result.update(ok=True, source=value.source, categoryCount=len(value.categories),
                          channelCount=len(value.channels),
                          quarantinedRowCount=len(value.quarantined_stream_ids))
        except inventory.SyncError as error:
            result.update(ok=False, errorCode=inventory_error_code(error))
            result["httpStatusesInSafeDiagnostics"] = sorted({int(value) for value in re.findall(r"HTTP ([1-5][0-9]{2})\b", str(error))})
            if result["errorCode"] in {"reflection_guard", "malformed_transport_text", "text_scan_limit"}:
                try:
                    bases = inventory.panel_base_candidates(sync_config.base_url, allow_insecure_http=allow_http)
                    policy = inventory.provider_reflection_needles(sync_config, bases)
                    counts = {}
                    allowed_fields = {"stream_id","name","category_id","category_name",
                                      "epg_channel_id","num","stream_icon"}
                    for action in ("get_live_categories", "get_live_streams"):
                        probe = inventory.request_panel_json(session, config.base_url+"/player_api.php",
                            config.username, config.password, action, allow_insecure_http=allow_http)
                        rows = inventory.normalize_panel_action_rows(probe.payload, action)
                        rejected = 0
                        for row in rows:
                            try:
                                inventory.validate_provider_inventory_secret_safe([row],[],policy)
                            except inventory.SyncError:
                                rejected += 1
                                for field, value in row.items():
                                    try:
                                        inventory.validate_provider_inventory_secret_safe([{field:value}],[],policy)
                                    except inventory.SyncError as field_error:
                                        safe_field = field if field in allowed_fields else "other"
                                        key = action+"."+safe_field+"."+inventory_error_code(field_error)
                                        counts[key] = counts.get(key,0)+1
                                if rejected >= 10:
                                    break
                    result["rejectedFieldCountsUpToTenRowsPerAction"] = counts
                except inventory.SyncError as scan_error:
                    result["fieldScanIssue"] = inventory_error_code(scan_error)
    return result


def production_session_inventory_probe(configs: list, allow_http: bool) -> list[dict]:
    """Repeat production's ordered inventory fetches using its default session."""
    results = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"SKYTV-Channel-Inventory/{inventory.SYNC_VERSION}",
        "Accept": vod.PROVIDER_HEADERS["Accept"],
    })
    try:
        for config in configs:
            result = {"serverId": config.server_id}
            try:
                value = inventory.fetch_panel_inventory(session, config, allow_insecure_http=allow_http)
                result.update(ok=True, source=value.source, categoryCount=len(value.categories),
                              channelCount=len(value.channels),
                              quarantinedRowCount=len(value.quarantined_stream_ids))
            except inventory.SyncError as error:
                result.update(ok=False, errorCode=inventory_error_code(error))
                result["httpStatusesInSafeDiagnostics"] = sorted({
                    int(value) for value in re.findall(r"HTTP ([1-5][0-9]{2})\b", str(error))
                })
            results.append(result)
    finally:
        session.close()
    return results


def clean_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.auth = epg.PanelCredentiallessAuth()
    return session


def public_target(location: str, current_url: str, configs: list) -> str | None:
    if not str(location or "").strip():
        return None
    try:
        parsed = urlsplit(urljoin(current_url, location))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            return None
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parsed.hostname):
            return None
        clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        decoded = clean
        for _ in range(5):
            for config in configs:
                vod.ensure_no_secret_reflection({"target": decoded}, config)
                # Hostnames are case-insensitive; protect even short account
                # identifiers here rather than treating the URL as a title.
                secrets = (config.username, config.password, config.base_url,
                           urlsplit(config.base_url).hostname or "")
                if any(variant.lower() in decoded.encode("utf-8").lower()
                       for secret in secrets if secret
                       for variant in vod._secret_variants(secret)):
                    return None
            decoded = unquote(decoded)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            return None
        return clean
    except (ValueError, OSError, vod.CatalogBuildError):
        return None


def inventory_probe(config, identity: str) -> dict:
    results = []
    with clean_session() as session:
        for action in ("get_live_categories", "get_live_streams"):
            result = {"action": action, "identity": identity}
            try:
                with session.get(
                    config.base_url + "/player_api.php",
                    params={"username":config.username, "password":config.password, "action":action},
                    headers={"User-Agent":identity, "Accept":vod.PROVIDER_HEADERS["Accept"]},
                    timeout=(10, 40), allow_redirects=False, stream=True,
                ) as response:
                    result["httpStatus"] = response.status_code
                    if response.status_code != 200:
                        results.append(result)
                        continue
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > MAX_JSON_BYTES:
                            raise ValueError()
                    payload = json.loads(raw)
                    result["payloadKind"] = "list" if isinstance(payload,list) else "object" if isinstance(payload,dict) else "other"
                    result["rowCount"] = len(payload) if isinstance(payload,list) else 0
                    result["authenticationRejected"] = vod._auth_rejected(payload)
                    try:
                        result["normalizedRowCount"] = len(inventory.normalize_panel_action_rows(payload, action))
                    except inventory.SyncError as error:
                        result["normalizationIssue"] = inventory_error_code(error)
                        if action == "get_live_streams" and isinstance(payload, list):
                            result["schemaSummary"] = raw_stream_schema_summary(payload)
            except (requests.RequestException, ValueError):
                result["issue"] = "request_or_payload_failed"
            results.append(result)
    return {"serverId":config.server_id, "checks":results}


def anonymous_xmltv(target: str) -> dict:
    result = {"publicHostname":urlsplit(target).hostname, "credentialsSent":False}
    parser = etree.XMLPullParser(events=("start",), resolve_entities=False, load_dtd=False, no_network=True)
    expanded = compressed = 0
    decoder = None
    first = True
    started = time.monotonic()
    with clean_session() as session:
        with session.get(target, headers=vod.PROVIDER_HEADERS, timeout=(10,40), allow_redirects=False, stream=True) as response:
            result["httpStatus"] = response.status_code
            if response.status_code != 200:
                return result
            for chunk in response.iter_content(65536):
                if time.monotonic() - started > 60:
                    break
                compressed += len(chunk)
                if compressed > MAX_PUBLIC_COMPRESSED_BYTES:
                    break
                if first:
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if chunk.startswith(b"\x1f\x8b") else None
                    first = False
                body = decoder.decompress(chunk, MAX_PUBLIC_EXPANDED_BYTES - expanded + 1) if decoder else chunk
                expanded += len(body)
                if expanded > MAX_PUBLIC_EXPANDED_BYTES:
                    break
                parser.feed(body)
                for _event, element in parser.read_events():
                    tag = etree.QName(element).localname
                    if "xmlRoot" not in result:
                        result["xmlRoot"] = "tv" if tag == "tv" else "other"
                        if tag != "tv":
                            return result
                    if tag == "channel":
                        result["hasChannel"] = True
                    if tag == "programme":
                        result["hasProgramme"] = True
                        result["expandedBytesInspected"] = expanded
                        return result
                    element.clear()
    result["expandedBytesInspected"] = expanded
    return result


def redirect_probe(config, configs: list) -> dict:
    result = {"serverId":config.server_id}
    endpoint = config.base_url + "/xmltv.php"
    with clean_session() as session:
        with session.get(endpoint, params={"username":config.username,"password":config.password},
                         headers=vod.PROVIDER_HEADERS, timeout=(10,40), allow_redirects=False, stream=True) as response:
            result["httpStatus"] = response.status_code
            if response.status_code not in epg.PANEL_REDIRECT_STATUSES:
                return result
            target = public_target(response.headers.get("Location", ""), endpoint, configs)
            result["safeAnonymousTarget"] = target is not None
    if target:
        try:
            result["anonymousTarget"] = anonymous_xmltv(target)
        except (requests.RequestException, ValueError, etree.XMLSyntaxError, zlib.error):
            result["anonymousTarget"] = {"issue":"request_or_xml_failed", "publicHostname":urlsplit(target).hostname}
    return result


def declared_native_channel_ids(path: Path, server_id: str) -> set[str]:
    """Collect only declared identities using the production XML safety rules."""
    source_key = "panel:" + server_id
    epg.reject_unsafe_xml_prefix(
        path, allow_inert_xmltv_doctype=True, source_label=source_key
    )
    channel_ids = set()
    root = None
    total = 0
    active_top_level = None
    child_elements = 0
    with epg.open_limited_xml(path, MAX_NATIVE_EXPANDED_BYTES) as source:
        context = etree.iterparse(
            source, events=("start", "end"), recover=False, huge_tree=False,
            load_dtd=False, no_network=True, resolve_entities=False,
            remove_comments=True, remove_pis=True,
        )
        for event, element in context:
            if event == "start":
                if root is None:
                    root = element
                    epg.reject_parsed_doctype(
                        root, allow_inert_xmltv_doctype=True, source_label=source_key
                    )
                    if epg.local_name(root.tag) != "tv":
                        raise ValueError("Not an XMLTV guide")
                elif element.getparent() is root:
                    active_top_level = element
                    child_elements = 0
                elif active_top_level is not None:
                    child_elements += 1
                    if child_elements > epg.MAX_RECORD_CHILD_ELEMENTS:
                        raise ValueError("Native guide record exceeds the child limit")
                continue
            if element.getparent() is not root:
                continue
            total += 1
            if total > epg.MAX_SOURCE_ELEMENTS:
                raise ValueError("Native guide exceeds the record limit")
            name = epg.local_name(element.tag)
            if name not in {"channel", "programme"}:
                raise ValueError("Unsupported native guide record")
            if name == "channel":
                channel_id = epg.clean_identifier(element.get("id") or "", 300)
                if channel_id:
                    channel_ids.add(channel_id)
            epg.release_top_level(element)
            active_top_level = None
            child_elements = 0
    if root is None:
        raise ValueError("Empty native guide")
    return channel_ids


def native_epg_probe(server_id: str, *, now: int | None = None) -> dict:
    """Read and validate one native source; never return a title, ID or URL."""
    if server_id not in NATIVE_SERVERS:
        raise ValueError("Native guide diagnostics require Server 2 or Server 3")
    result = {"serverId": server_id, "status": "unavailable", "usable": False}
    started = time.monotonic()
    try:
        with TemporaryDirectory(prefix="skytv-native-probe-") as private:
            private_root = Path(private)
            source_path, details = epg.download_panel_xmltv(
                server_id, private_root / "source.xmltv", maximum_attempts=1
            )
            checked_at = int(time.time()) if now is None else int(now)
            channel_ids = declared_native_channel_ids(source_path, server_id)
            with closing(epg.create_database(private_root / "selected.sqlite")) as connection:
                epg.ingest_xmltv_source(
                    connection=connection, path=source_path,
                    source_key="panel:" + server_id, wanted_ids=channel_ids,
                    window_start=checked_at - 86400, source_base_url="",
                    allow_source_icons=False, allow_inert_xmltv_doctype=True,
                    maximum_expanded_bytes=MAX_NATIVE_EXPANDED_BYTES,
                )
                # Inferred stops cannot prove that the provider has recovered.
                # Far-future rows also cannot stand in for this day's schedules.
                counts = connection.execute(
                    """WITH coverage AS (
                    SELECT p.channel_key, COUNT(*) AS programme_rows,
                        MAX(CASE WHEN p.start_epoch <= ? AND p.stop_epoch > ?
                            AND p.stop_epoch <= ? THEN 1 ELSE 0 END) AS has_current,
                        MAX(CASE WHEN p.start_epoch > ? AND p.start_epoch <= ?
                            AND p.stop_epoch <= ? THEN 1 ELSE 0 END) AS has_future
                    FROM programmes p JOIN channels c
                        ON p.source_key = c.source_key AND p.channel_key = c.channel_key
                    WHERE p.source_key = ?
                        AND coalesce(json_extract(p.details_json, '$.stopInferred'), 0) = 0
                        AND p.stop_epoch > p.start_epoch
                        AND p.stop_epoch - p.start_epoch <= 86400
                    GROUP BY p.channel_key)
                    SELECT coalesce(SUM(programme_rows), 0), coalesce(SUM(has_current), 0),
                        coalesce(SUM(has_future), 0), coalesce(SUM(has_current * has_future), 0)
                    FROM coverage""",
                    (checked_at, checked_at, checked_at + 86400,
                     checked_at, checked_at + NEAR_FUTURE_HOURS * 3600,
                     checked_at + (NEAR_FUTURE_HOURS + 24) * 3600,
                     "panel:" + server_id),
                ).fetchone()
            programme_rows, current_channels, future_channels, current_and_future = map(int, counts)
            usable = bool(channel_ids and current_and_future)
            result.update(
                httpStatus=200, status="ok" if usable else "stale_or_empty",
                usable=usable, channels=len(channel_ids), programmeRows=programme_rows,
                currentChannels=current_channels, futureChannels=future_channels,
                currentAndFutureChannels=current_and_future,
                futureWindowHours=NEAR_FUTURE_HOURS, checkedAt=checked_at,
                sha256=details["sha256"], bytes=details["bytes"],
            )
            cache_directory = os.environ.get("SKYTV_PANEL_CACHE_DIR", "").strip()
            if usable and cache_directory:
                # Cache plaintext stays in the runner's private directory; a
                # separate workflow step seals it before durable storage.
                try:
                    import private_panel_cache
                    result["cacheSaved"] = private_panel_cache.save_source(
                        server_id, source_path, Path(cache_directory),
                        credentials=epg.panel_credentials(server_id), captured_at=checked_at,
                    )
                except Exception:
                    # Optional cache trouble must not relabel a valid source.
                    result["cacheSaved"] = False
    except epg.PanelSourceUnavailable as error:
        result["issue"] = "source_unavailable"
        status = re.fullmatch(r"HTTP ([1-5][0-9]{2})", error.last_result)
        if status:
            result["httpStatus"] = int(status.group(1))
        next_attempt_at = getattr(error, "next_attempt_at", None)
        if type(next_attempt_at) is int and 0 < next_attempt_at < 2**53:
            result["nextAttemptAt"] = next_attempt_at
    except epg.BuildError as error:
        result.update(status="invalid_or_rejected", issue="validation_or_transport_rejected")
        status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(error))
        if status:
            result["httpStatus"] = int(status.group(1))
    except (etree.XMLSyntaxError, gzip.BadGzipFile, EOFError,
            OSError, ValueError, TypeError, KeyError):
        # Third-party exception text may contain a credential-bearing URL or
        # source text. Only this fixed diagnostic code is emitted.
        result.update(status="invalid_or_rejected", issue="validation_or_transport_rejected")
    result["elapsedSeconds"] = round(time.monotonic() - started, 3)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("live", "native_epg"), default="live")
    parser.add_argument("--servers", nargs="+", choices=ALL_SERVERS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    servers = tuple(dict.fromkeys(args.servers or (
        NATIVE_SERVERS if args.mode == "native_epg" else ALL_SERVERS
    )))
    if args.mode == "native_epg" and any(server not in NATIVE_SERVERS for server in servers):
        parser.error("Native guide diagnostics support only server_2 and server_3")
    allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "").casefold() == "true"
    if args.mode == "native_epg":
        configs = []
        native = []
        for server in servers:
            try:
                config = vod.load_server_config(server, allow_insecure_http=allow_http)
            except (vod.CatalogBuildError, ValueError):
                native.append({"serverId": server, "status": "configuration_rejected",
                               "usable": False, "issue": "invalid_configuration"})
                continue
            configs.append(config)
            native.append(native_epg_probe(server))
        report = {"schemaVersion": 1, "mode": "native_epg", "nativeEpg": native,
                  "recovered": all(value["usable"] for value in native)}
        for config in configs:
            vod.ensure_no_secret_reflection(report, config)
        rendered = json.dumps(report, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered, flush=True)
        return 0
    configs = [vod.load_server_config(server, allow_insecure_http=allow_http) for server in servers]
    report = {"schemaVersion":1,"inventory":[],"redirect":{}}
    production_configs = inventory.load_server_configs([config.server_id for config in configs])
    report["productionSessionInventory"] = production_session_inventory_probe(production_configs, allow_http)
    for config in configs:
        for identity in (f"SKYTV-Channel-Inventory/{inventory.SYNC_VERSION}", vod.PROVIDER_HEADERS["User-Agent"]):
            report["inventory"].append(inventory_probe(config,identity))
    report["actualInventory"] = actual_inventory_probe(configs[0], allow_http)
    report["additionalInventory"] = [actual_inventory_probe(config, allow_http) for config in configs[1:]]
    report["redirects"] = []
    for config in configs:
        if config.server_id not in NATIVE_SERVERS:
            continue
        try:
            result = redirect_probe(config, configs)
        except requests.RequestException:
            result = {"serverId": config.server_id, "issue": "request_failed"}
        report["redirects"].append(result)
        if not report["redirect"]:
            # Preserve the original first-native redirect summary for readers
            # that predate the full per-server list.
            report["redirect"] = result
    for config in configs:
        vod.ensure_no_secret_reflection(report,config)
    rendered = json.dumps(report, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Provider transport diagnostics could not complete safely; no raw response was logged.",file=sys.stderr)
        raise SystemExit(1) from None
