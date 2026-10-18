#!/usr/bin/env python3
"""Bounded provider transport checks; never print bodies or credential URLs."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import time
import zlib
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from lxml import etree

import build_epg_streaming as epg
import build_vod_catalogs as vod
import sync_channel_inventory as inventory


MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_PUBLIC_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_EXPANDED_BYTES = 32 * 1024 * 1024


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


def main() -> int:
    allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "").casefold() == "true"
    configs = [vod.load_server_config("server_"+str(number),allow_insecure_http=allow_http) for number in (1,2)]
    report = {"schemaVersion":1,"inventory":[],"redirect":{}}
    for identity in (f"SKYTV-Channel-Inventory/{inventory.SYNC_VERSION}", vod.PROVIDER_HEADERS["User-Agent"]):
        report["inventory"].append(inventory_probe(configs[0],identity))
    try:
        report["redirect"] = redirect_probe(configs[1],configs)
    except requests.RequestException:
        report["redirect"] = {"serverId":configs[1].server_id,"issue":"request_failed"}
    for config in configs:
        vod.ensure_no_secret_reflection(report,config)
    print(json.dumps(report,sort_keys=True),flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Provider transport diagnostics could not complete safely; no raw response was logged.",file=sys.stderr)
        raise SystemExit(1) from None
