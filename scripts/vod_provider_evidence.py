"""Small, private, record-bound provider facts for unresolved movie identities.

The normal catalogue never downloads the whole detail library. This collector
is optional, bounded, and uses the existing credential/redirect/size checks.
It never stores a raw provider response or a playback/artwork URL.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Mapping

import requests
import build_vod_catalogs as vod

MAX_CACHE_BYTES = 32 * 1024
EVIDENCE_TTL = 7 * 86400
FACT_KEYS = {"imdbId", "directors", "cast", "runtimeMinutes"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def source_fingerprint(item):
    return _digest({key: item.get(key) for key in
                    ("contentId", "title", "providerLastModified", "tmdbId", "releaseDate")})


def _names(value):
    if isinstance(value, str):
        value = re.split(r"[,;|]", value)
    if not isinstance(value, list):
        return []
    result = []
    for entry in value[:40]:
        if isinstance(entry, Mapping):
            entry = entry.get("name")
        if not isinstance(entry, str):
            continue
        name = vod.clean_text(entry, 100)
        if name and "://" not in name and "@" not in name and name not in result:
            result.append(name)
    return result


def normalize_facts(detail):
    if not isinstance(detail, Mapping):
        return {"imdbId": None, "directors": [], "cast": [], "runtimeMinutes": None}
    sources = vod._nested_sources({}, detail)
    imdb = vod._first_value(sources, ("imdb_id", "imdbId", "imdb"))
    imdb = imdb if isinstance(imdb, str) and re.fullmatch(r"tt[0-9]{7,10}", imdb) else None
    minutes = vod._first_value(sources, ("runtimeMinutes", "runtime", "duration_minutes"))
    seconds = vod._first_value(sources, ("duration_secs", "duration_seconds"))
    try:
        if isinstance(minutes, bool) or isinstance(seconds, bool):
            raise ValueError("Invalid runtime")
        runtime = float(minutes) if minutes is not None else float(seconds) / 60 if seconds is not None else None
        runtime = runtime if runtime is not None and math.isfinite(runtime) and 1 <= runtime <= 1440 else None
    except (ValueError, TypeError, OverflowError):
        runtime = None
    return {"imdbId": imdb,
            "directors": _names(vod._first_value(sources, ("director", "directors"))),
            "cast": _names(vod._first_value(sources, ("cast", "actors"))),
            "runtimeMinutes": round(runtime, 2) if runtime is not None else None}


class DeadlineSession(requests.Session):
    def __init__(self, deadline, stats, max_requests):
        super().__init__()
        self.trust_env = False
        self.deadline = deadline
        self.stats = stats
        self.max_requests = max_requests
        self.initial_reserved = False

    def get(self, url, **kwargs):
        if self.initial_reserved:
            self.initial_reserved = False
        else:
            if self.stats["requests"] >= self.max_requests:
                raise requests.Timeout()
            self.stats["requests"] += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout()
        kwargs["timeout"] = (min(5, remaining), min(10, remaining))
        response = super().get(url, **kwargs)
        original = response.iter_content

        def bounded_chunks(*args, **options):
            for chunk in original(*args, **options):
                if time.monotonic() >= self.deadline:
                    response.close()
                    raise requests.Timeout()
                yield chunk
        response.iter_content = bounded_chunks
        return response


class MovieEvidenceCollector:
    def __init__(self, cache_dir, *, configs=None, max_requests=40, max_seconds=60,
                 now=None, secret_values=()):
        if (type(max_requests) is not int or not 0 <= max_requests <= 200
                or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds)
                or not 0 <= max_seconds <= 180):
            raise ValueError("Invalid movie evidence limits")
        self.cache_dir = Path(cache_dir)
        self.configs = dict(configs or {})
        self.max_requests = max_requests
        self.now = time.time() if now is None else now
        if type(self.now) not in (int, float) or not math.isfinite(self.now) or self.now < 0:
            raise ValueError("Invalid movie evidence timestamp")
        self.deadline = time.monotonic() + max_seconds
        self.session = None
        for directory in (self.cache_dir, *self.cache_dir.parents):
            if directory.is_symlink():
                raise ValueError("Movie evidence cache cannot use symbolic links")
        self._known_names = {path.name for path in self.cache_dir.glob("*.json")
                             if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)}
        self.secrets = [x for x in secret_values if x]
        self.stats = {"requests": 0, "lookups": 0, "cacheHits": 0, "errors": 0, "budgetDeferred": 0}
        self.budget = vod.DownloadBudget(limit=16 * 1024 * 1024)

    def close(self):
        if self.session is not None:
            self.session.close()

    def _path(self, item):
        identity = item.get("contentId", "")
        if not isinstance(identity, str) or not re.fullmatch(r"server_[1-9][0-9]?:movie:[0-9]{1,10}", identity):
            raise ValueError("Invalid movie evidence identity")
        for directory in (self.cache_dir, *self.cache_dir.parents):
            if directory.is_symlink():
                raise ValueError("Movie evidence cache cannot use symbolic links")
        path = self.cache_dir / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if path.is_symlink():
            raise ValueError("Movie evidence cache cannot use symbolic links")
        return path

    def _read(self, item):
        identity = item.get("contentId", "")
        if not isinstance(identity, str):
            return None
        # Most catalogue rows have never needed a provider detail. Avoid an
        # extra filesystem walk for every one of the tens of thousands of rows.
        name = hashlib.sha256(identity.encode()).hexdigest() + ".json"
        if name not in self._known_names:
            return None
        path = self._path(item)
        try:
            if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if (set(value) != {"version", "contentId", "sourceFingerprint", "checkedAt", "facts"}
                    or value["version"] != 1 or value["contentId"] != item["contentId"]
                    or value["sourceFingerprint"] != source_fingerprint(item)
                    or type(value["checkedAt"]) not in (int, float) or not math.isfinite(value["checkedAt"])
                    or value["checkedAt"] > self.now + 300 or not isinstance(value["facts"], dict)
                    or set(value["facts"]) != FACT_KEYS or normalize_facts({"info": value["facts"]}) != value["facts"]):
                return None
            self._check_secrets(value, self.configs.get(item["contentId"].split(":")[0]))
            return value
        except (ValueError, TypeError, KeyError, OSError, vod.CatalogBuildError):
            return None

    def _check_secrets(self, value, config):
        vod.ensure_no_secret_reflection(value, config)
        raw = json.dumps(value, ensure_ascii=False).encode()
        if any(token in raw for secret in self.secrets for token in vod._secret_variants(secret)):
            raise ValueError("Movie evidence failed secret validation")

    def load(self, item):
        """Validated stale facts can reuse a positive identity for the same asset."""
        value = self._read(item)
        if value is None or not any(value["facts"].values()):
            return None
        facts = value["facts"]
        return {"contentId": item["contentId"], "fingerprint": _digest({
            "source": value["sourceFingerprint"], "facts": facts}), **facts}

    def collect(self, server_id, item):
        if (not isinstance(item.get("contentId"), str)
                or str(item.get("id")) != item["contentId"].rsplit(":", 1)[-1]):
            raise ValueError("Movie evidence provider ID does not match identity")
        value = self._read(item)
        if value is not None and 0 <= self.now - value["checkedAt"] < EVIDENCE_TTL:
            self.stats["cacheHits"] += 1
            return self.load(item)
        config = self.configs.get(server_id)
        if config is None:
            return self.load(item)
        if config.server_id != server_id or item["contentId"].split(":")[0] != server_id:
            raise ValueError("Movie evidence provider does not match identity")
        if self.stats["requests"] >= self.max_requests or time.monotonic() >= self.deadline:
            self.stats["budgetDeferred"] += 1
            return self.load(item)
        if self.session is None:
            self.session = DeadlineSession(self.deadline, self.stats, self.max_requests)
        self.stats["requests"] += 1
        self.stats["lookups"] += 1
        self.session.initial_reserved = True
        try:
            # Optional disambiguation makes one attempt. The next due queue run
            # handles retries; list ingestion retains its normal retry policy.
            detail = vod._request_panel_json_once(self.session, config, "get_vod_info",
                item_id=item["id"], maximum_bytes=512 * 1024, budget=self.budget)
            facts = normalize_facts(detail)
            if value is not None and any(value["facts"].values()) and not any(facts.values()):
                # An empty refresh cannot erase an unchanged asset's existing
                # facts. Keep their original checkedAt so this failed refresh
                # does not silently renew the evidence freshness period.
                raise ValueError("Movie evidence refresh returned no facts")
            value = {"version": 1, "contentId": item["contentId"],
                     "sourceFingerprint": source_fingerprint(item), "checkedAt": self.now, "facts": facts}
            self._check_secrets(value, config)
            raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
            if len(raw) > MAX_CACHE_BYTES:
                raise ValueError("Movie evidence exceeds size limit")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._path(item)
            temporary = vod._temporary_path(self.cache_dir, "movie-evidence")
            try:
                temporary.write_bytes(raw)
                os.replace(temporary, path)
                self._known_names.add(path.name)
            finally:
                temporary.unlink(missing_ok=True)
        except (vod.CatalogBuildError, requests.RequestException, OSError, ValueError, TypeError):
            self.stats["errors"] += 1
        return self.load(item)
