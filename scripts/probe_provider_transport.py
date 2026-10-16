"""Bounded, secret-safe transport diagnostic; never follows provider redirects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_vod_catalogs as vod


LIMIT = 256 * 1024
BROAD_ACCEPT = "application/json,text/plain,application/x-mpegURL,*/*"
PROFILES = (
    ("vod_default", {"Accept": "application/json"}),
    ("inventory", {"User-Agent": "SKYTV-Channel-Inventory/1.1", "Accept": BROAD_ACCEPT}),
    ("builder", {"User-Agent": "SKYTV-EPG-Builder/2.0", "Accept": BROAD_ACCEPT}),
)
ACTIONS = ("get_live_categories", "get_vod_categories", "get_series_categories", "")


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def mime_only(value: object) -> str:
    candidate = str(value or "").split(";", 1)[0].strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", candidate):
        return candidate[:120]
    return ""


def auth_rejected(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    account = payload.get("user_info")
    if not isinstance(account, dict):
        return False
    auth = account.get("auth")
    status = account.get("status")
    return auth is False or auth == 0 or auth == "0" or (
        isinstance(status, str) and status.strip().casefold() in {"disabled", "expired"}
    )


def probe(session: requests.Session, config: vod.ServerConfig, base: str, profile: str, action: str, candidate: int) -> None:
    parsed_base = urlsplit(base)
    endpoint = f"{base}/player_api.php"
    params = {"username": config.username, "password": config.password}
    if action:
        params["action"] = action
    record: dict[str, object] = {
        "profile": profile,
        "action": action or "account_metadata",
        "candidateNumber": candidate,
        "baseHasPath": bool(parsed_base.path.strip("/")),
        "explicitPort": parsed_base.port is not None,
        "redirect": False,
        "sameHost": False,
        "samePath": False,
        "exactQuery": False,
        "authRejected": False,
    }
    response = None
    try:
        response = session.get(endpoint, params=params, stream=True, allow_redirects=False, timeout=(10, 20))
        record["HTTPstatus"] = int(response.status_code)
        record["contentType"] = mime_only(response.headers.get("Content-Type", ""))
        redirect = response.is_redirect or response.is_permanent_redirect
        record["redirect"] = bool(redirect)
        if redirect:
            try:
                location = response.headers.get("Location", "")
                if isinstance(location, str) and location and len(location) <= 8192:
                    target = urlsplit(urljoin(endpoint, location))
                    original = urlsplit(endpoint)
                    record["sameHost"] = (target.hostname or "").casefold() == (original.hostname or "").casefold()
                    record["samePath"] = target.path == original.path
                    query = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
                    record["exactQuery"] = len(query) == len(params) and dict(query) == params
            except Exception:
                pass
        # Read at most LIMIT decompressed bytes and stop immediately. A body
        # exactly at the cap is conservatively classified as incomplete.
        body = response.raw.read(LIMIT, decode_content=True)
        incomplete = len(body) >= LIMIT
        if incomplete:
            record["responseShape"] = "incomplete"
        else:
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                record["responseShape"] = "nonJSON"
            else:
                if isinstance(payload, list):
                    record["responseShape"] = "list"
                    record["rowCount"] = len(payload)
                elif isinstance(payload, dict):
                    record["responseShape"] = "dict"
                else:
                    record["responseShape"] = "nonJSON"
                record["authRejected"] = auth_rejected(payload)
    except Exception as exc:
        record["exceptionClass"] = type(exc).__name__
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    emit(record)


def main() -> None:
    allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "").strip().casefold() in {"1", "true", "yes", "on"}
    try:
        config = vod.load_server_config("server_2", allow_insecure_http=allow_http)
    except Exception as exc:
        emit({"profile": "config", "exceptionClass": type(exc).__name__})
        return
    for profile, headers in PROFILES:
        with requests.Session() as session:
            session.trust_env = False
            session.headers.update(headers)
            for action in ACTIONS:
                probe(session, config, config.base_url, profile, action, 1)
            if profile in {"vod_default", "inventory"}:
                for action in ("get_vod_streams", "get_series"):
                    probe(session, config, config.base_url, profile, action, 1)
            parsed = urlsplit(config.base_url)
            if profile == "inventory" and parsed.path.strip("/"):
                origin = f"{parsed.scheme}://{parsed.netloc}"
                for action in ("get_live_categories", "get_vod_categories"):
                    probe(session, config, origin, profile, action, 2)


if __name__ == "__main__":
    main()
