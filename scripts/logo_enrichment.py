#!/usr/bin/env python3
"""Public channel-image normalization, honest provenance and bounded checks.

Image reachability is separate from brand identity. A successful PNG response
does not establish that a broadcaster owns, approves or still uses that image.
Only reviewed catalog entries are marked ``brandVerified``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from lxml import etree


_HTTPS_IMAGE_HOSTS = frozenset({
    "dtil.tmsimg.com", "tmsimg.com", "jiotv.catchup.cdn.jio.com",
    "upload.wikimedia.org", "assets.tv24.co",
})
_GENERIC_FILES = frozenset({
    "generic_sources_h3.png", "generic_sources_h3.jpg", "generic.png",
    "noimage.png", "no-image.png", "no_logo.png", "no-logo.png",
    "placeholder.png", "placeholder.jpg", "default-logo.png",
    "default_logo.png", "default-channel.png", "channel-default.png",
})
MAX_IMAGE_BYTES = 2 * 1024 * 1024
CACHE_SCHEMA_VERSION = 1


def normalize_logo_url(value: object, *, base_url: str = "") -> str:
    """Repair known public image formats without publishing arbitrary queries.

    TMS resize dimensions are presentation parameters on a static image path;
    removing only those numeric parameters recovers the original image while
    keeping signed/private URL query strings out of public XML and JSON.
    """
    raw = html.unescape(str(value or "").strip())
    if not raw or len(raw) > 2_000 or any(ord(char) < 32 for char in raw):
        return ""
    # Some upstream records accidentally concatenate two absolute schemes.
    raw = re.sub(r"^https?://(?=https?://)", "", raw, count=1, flags=re.IGNORECASE)
    # A known A1 catalog bug prepends the schedule page to an already absolute
    # static logo URL. Repair only that exact public origin/path combination.
    raw = re.sub(r"^https?://www\.a1\.hr/raspored(?=https://ssc\.a1\.hr/ITV/pictures/logos/)",
                 "", raw, count=1, flags=re.IGNORECASE)
    if raw.startswith("https://ssc.a1.hr/ITV/pictures/logos/"):
        raw = raw.replace(" ", "%20")
    if re.search(r"https?://", re.sub(r"^https?://", "", raw, count=1, flags=re.IGNORECASE),
                 flags=re.IGNORECASE):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif base_url:
        raw = urljoin(base_url, raw)
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").casefold()
        port = parts.port
    except ValueError:
        return ""
    if (parts.scheme.casefold() not in {"http", "https"} or not host
            or parts.username or parts.password or parts.fragment
            or port not in {None, 80, 443} or "\\" in raw
            or any(char.isspace() for char in raw)):
        return ""
    if port is not None and port != (443 if parts.scheme.casefold() == "https" else 80):
        return ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            return ""
    path = unquote(parts.path).casefold()
    if ("/sources/generic/" in path
            or path.rsplit("/", 1)[-1] in _GENERIC_FILES
            or (host == "focus.telerama.fr" and path.endswith("/clear-192.png"))):
        return ""
    if parts.query:
        resize = parse_qsl(parts.query, keep_blank_values=True)
        if (host != "tmsimg.com" and not host.endswith(".tmsimg.com")) or not resize:
            return ""
        if any(key not in {"w", "h"} or not re.fullmatch(r"[0-9]{1,4}", val)
               or not 1 <= int(val) <= 4096 for key, val in resize):
            return ""
        if len({key for key, _ in resize}) != len(resize):
            return ""
    scheme = parts.scheme.casefold()
    if host in _HTTPS_IMAGE_HOSTS or host.endswith(".tmsimg.com"):
        scheme = "https"
    # The normalized output has no credentials, query or fragment.
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit((scheme, netloc, parts.path, "", ""))


def load_logo_verification(path: Path | str | None) -> dict[str, dict]:
    if path is None or not Path(path).is_file():
        return {}
    if Path(path).stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Logo verification cache exceeds its size limit.")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != CACHE_SCHEMA_VERSION:
        raise ValueError("Logo verification cache has an unsupported schema.")
    records = data.get("images")
    if not isinstance(records, dict) or len(records) > 100_000:
        raise ValueError("Logo verification cache has an invalid image map.")
    return {
        url: dict(record) for url, record in records.items()
        if isinstance(url, str) and normalize_logo_url(url) == url
        and isinstance(record, dict)
        and record.get("status") in {
            "valid_image", "http_error", "network_error", "invalid_image", "unchecked"
        }
        and type(record.get("checkedAt", 0)) is int
    }


class RegistryLogoIndex:
    """Current channel-wide registry logos, joined by one exact channel identity."""

    def __init__(self, registry, records: list[dict]):
        if not isinstance(records, list) or len(records) > 100_000:
            raise ValueError("Registry logo catalog has an invalid record count.")
        self.registry = registry
        candidates: dict[str, list[dict]] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("channel"), str):
                raise ValueError("Registry logo catalog has an invalid channel identity.")
            if record.get("in_use") is not True:
                continue
            url = normalize_logo_url(record.get("url", ""))
            if not url or record["channel"] not in registry.records:
                continue
            feed = record.get("feed")
            tags = record.get("tags", [])
            if (feed is not None and (not isinstance(feed, str) or len(feed) > 512)
                    or not isinstance(tags, list) or len(tags) > 40
                    or any(not isinstance(tag, str) or len(tag) > 80 for tag in tags)):
                raise ValueError("Registry logo catalog has invalid variant metadata.")
            candidates.setdefault(record["channel"], []).append({**record, "url": url})
        self.by_channel = {}
        for channel, options in candidates.items():
            channel_wide = [record for record in options if record.get("feed") is None]
            if not channel_wide:
                # Regional-feed artwork must not be applied to an unidentified
                # feed unless every current catalog variant agrees exactly.
                if len({record["url"] for record in options}) != 1:
                    continue
                channel_wide = options
            def rank(record):
                tags = {tag.casefold() for tag in record.get("tags", [])}
                raster = str(record.get("format", "")).upper() in {"PNG", "APNG", "WEBP", "JPEG"}
                width = record.get("width", 0)
                width = width if type(width) is int and 0 <= width <= 100_000 else 0
                return (bool(tags.intersection({"white", "black", "monochrome"})),
                        not raster, not 160 <= width <= 2048, abs(width - 512), record["url"])
            self.by_channel[channel] = min(channel_wide, key=rank)["url"]

    @classmethod
    def from_directory(cls, directory: Path | str):
        from channel_metadata_enrichment import ChannelRegistry
        directory = Path(directory)
        path = directory / "logos.json"
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Registry logo catalog exceeds its size limit.")
        return cls(ChannelRegistry.from_directory(directory),
                   json.loads(path.read_text(encoding="utf-8-sig")))

    def lookup(self, row: Mapping[str, object]) -> tuple[str, str] | None:
        if any(re.search(r"[#*_]{2,}", str(row.get(key) or ""))
               for key in ("channel_name", "channelName")):
            return None
        explicit = row.get("countries", ())
        if isinstance(explicit, str):
            try:
                explicit = json.loads(explicit) if explicit.strip().startswith("[") else re.split(r"[,;|]", explicit)
            except ValueError:
                explicit = ()
        hints = [str(value).strip().upper() for value in explicit
                 if str(value).strip().upper() in self.registry.valid_countries] if isinstance(explicit, (list, tuple)) else []
        if not hints:
            hints, _method = self.registry.country_evidence(row)
        records = {}
        for key in ("channel_name", "canonical_name", "channelName", "canonicalName"):
            name = str(row.get(key) or "")
            if not name:
                continue
            record = self.registry.resolve_channel(name, countries=hints,
                                                    epg_id=str(row.get("epg_id") or ""))
            if record is not None:
                if hints and record.country not in hints and not set(hints).intersection(record.broadcast_countries):
                    continue
                records[record.channel_id] = record
        if len(records) != 1:
            return None
        channel_id = next(iter(records))
        url = self.by_channel.get(channel_id)
        return (channel_id, url) if url else None


def logo_provenance(
    url: object, *, mapping_url: object = "", override_notes: object = "",
    source_icon: object = "", verification: Mapping[str, Mapping] | None = None,
) -> dict:
    """Describe identity evidence independently from successful image checks."""
    clean = normalize_logo_url(url)
    notes = str(override_notes or "")
    kind, source, match, brand_verified = "missing", "none", "none", False
    if clean:
        path = urlsplit(clean).path
        if "/logos/generated/" in path:
            kind, source, match = "generated_artwork", "project_artwork", "content_category"
        elif "/logos/people/" in path or notes.startswith("NAMED_PORTRAIT:"):
            kind, source, match = "person_portrait", "reviewed_portrait", "exact_person"
        elif clean == normalize_logo_url(mapping_url):
            kind, source, match = "provider_logo", "mapping", "exact_stream"
        elif notes.startswith("BRAND_LOGO:"):
            kind, source, match, brand_verified = "brand_logo", "reviewed_brand_catalog", "exact_brand_family", True
        elif notes.startswith("SOURCE_NAME_LOGO:"):
            kind, source, match = "source_channel_logo", "epgshare", "unique_exact_name"
        elif notes.startswith("REGISTRY_LOGO:"):
            kind, source, match = "source_channel_logo", "iptv-org", "exact_registry_channel"
        elif clean == normalize_logo_url(source_icon):
            kind, source, match = "source_channel_logo", "epgshare", "exact_epg_id"
        else:
            kind, source, match = "approved_override", "configured_override", "exact_stream"
    record = (verification or {}).get(clean, {})
    image_status = record.get("status", "unchecked") if clean else "missing"
    result = {"kind": kind, "source": source, "match": match,
              "imageStatus": image_status, "brandVerified": brand_verified}
    if record.get("checkedAt"):
        result["imageCheckedAt"] = record["checkedAt"]
    return result


def _public_network_target(url: str) -> bool | None:
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return all(ipaddress.ip_address(item[4][0]).is_global for item in addresses) if addresses else None
    except OSError:
        # DNS failure is a transient inability to check an image, not evidence
        # that the public image is invalid and should disappear from the app.
        return None
    except ValueError:
        return False


class _PublicImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        normalized = normalize_logo_url(newurl)
        if not normalized or not _public_network_target(normalized):
            raise urllib.error.URLError("Unsafe image redirect")
        return super().redirect_request(req, fp, code, msg, headers, normalized)


def _image_kind(data: bytes, content_type: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        return "png" if width and height and width <= 8192 and height <= 8192 else ""
    if data.startswith(b"\xff\xd8\xff") and data.rstrip().endswith(b"\xff\xd9"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if "svg" in content_type or data.lstrip().startswith((b"<svg", b"<?xml")):
        try:
            root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
            if etree.QName(root).localname == "svg" and not root.getroottree().docinfo.doctype:
                return "svg"
        except (etree.XMLSyntaxError, ValueError):
            pass
    return ""


def check_logo_image(url: str, *, timeout: float = 6, now_epoch: int | None = None) -> dict:
    checked_at = int(now_epoch if now_epoch is not None else time.time())
    result = {"status": "network_error", "checkedAt": checked_at}
    clean = normalize_logo_url(url)
    if not clean:
        return {**result, "status": "invalid_image", "reason": "unsafe_url"}
    target = _public_network_target(clean)
    if target is None:
        return result
    if not target:
        return {**result, "status": "invalid_image", "reason": "unsafe_url"}
    request = urllib.request.Request(clean, headers={
        "User-Agent": "SkyTVPlayer-Channel-Image-Check/1.0",
        "Accept": "image/png,image/jpeg,image/webp,image/gif,image/svg+xml,*/*;q=0.1",
    })
    try:
        opener = urllib.request.build_opener(_PublicImageRedirect())
        started = time.monotonic()
        with opener.open(request, timeout=max(1, min(timeout, 10))) as response:
            mime = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
            chunks, size = [], 0
            while size <= MAX_IMAGE_BYTES:
                if time.monotonic() - started > max(1, min(timeout, 10)):
                    return result
                chunk = response.read1(min(64 * 1024, MAX_IMAGE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            image_kind = _image_kind(data, mime) if len(data) <= MAX_IMAGE_BYTES else ""
            return {**result, "status": "valid_image" if image_kind else "invalid_image",
                    "httpStatus": response.status, "contentType": mime,
                    "format": image_kind, "bytes": len(data)}
    except urllib.error.HTTPError as exc:
        # Throttling and temporary server outages must not remove otherwise
        # valid logos merely because the scheduled validator was rate limited.
        temporary = exc.code in {408, 425, 429} or 500 <= exc.code <= 599
        return {**result, "status": "network_error" if temporary else "http_error", "httpStatus": exc.code}
    except (urllib.error.URLError, OSError, ValueError):
        return result


def verify_logo_urls(
    urls: Iterable[str], *, cache_path: Path | str | None = None,
    max_checks: int = 2_000, workers: int = 12, timeout: float = 6,
    deadline_seconds: float = 180, now_epoch: int | None = None,
) -> dict[str, dict]:
    """Check each distinct public image once, reusing bounded positive/negative TTLs."""
    now = int(now_epoch if now_epoch is not None else time.time())
    wanted = list(dict.fromkeys(clean for url in urls if (clean := normalize_logo_url(url))))
    cached = load_logo_verification(cache_path)
    results, pending = {}, []
    for url in wanted:
        old = cached.get(url, {})
        ttl = (14 * 86400 if old.get("status") == "valid_image"
               else 3600 if old.get("status") == "network_error" else 86400)
        age = now - old.get("checkedAt", 0)
        if old and old.get("status") != "unchecked" and 0 <= age < ttl:
            results[url] = old
        else:
            results[url] = {"status": "unchecked", "checkedAt": 0}
            pending.append(url)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 16)))
    futures = {executor.submit(check_logo_image, url, timeout=timeout, now_epoch=now): url
               for url in pending[:max(0, min(max_checks, 10_000))]}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=max(1, deadline_seconds)):
            results[futures[future]] = future.result()
    except concurrent.futures.TimeoutError:
        pass
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    if cache_path is not None:
        destination = Path(cache_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps({"schemaVersion": CACHE_SCHEMA_VERSION,
                                        "generatedAt": now, "images": results},
                                       sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temporary.replace(destination)
    return results


def collect_logo_verification_urls(
    source_icon_catalog: Path, *, icon_config: Path | None = None,
    mapping_csv: Path | None = None,
) -> list[str]:
    """Prioritize selected external overrides and omit unused source channels."""
    import csv
    from collections import defaultdict
    def enabled(row):
        return str(row.get("enabled", "")).strip().casefold() in {"", "1", "true", "yes", "y", "on", "enabled"}
    def source(row):
        value = str(row.get("source", "")).strip().casefold()
        feed = str(row.get("epg_feed", "")).strip().casefold()
        return value or ("panel" if feed in {"panel", "server xmltv.php"}
                         else "dummy" if feed == "dummy_channels" else "epgshare01")
    selected = None
    by_stream, by_epg, by_name = defaultdict(list), defaultdict(list), defaultdict(list)
    source_ids: set[str] = set()
    mapping_urls = []
    if mapping_csv is not None:
        with mapping_csv.open(encoding="utf-8-sig", newline="") as handle:
            selected = [row for row in csv.DictReader(handle) if enabled(row)
                        and str(row.get("action", "")).strip().upper() not in {
                            "REVIEW", "UNMATCHED", "NO_EPG", "UNRESOLVED", "SKIP", "IGNORE", "REJECTED"}
                        and str(row.get("epg_id", "")).strip()
                        and not (str(row.get("server_id", "")).strip() == "server_1" and source(row) == "panel")]
        for row in selected:
            by_stream[str(row.get("stream_id", "")).strip()].append(row)
            by_epg[str(row.get("epg_id", "")).strip().casefold()].append(row)
            by_name[str(row.get("channel_name", "")).strip().casefold()].append(row)
            mapping_urls.append(row.get("logo_url") or row.get("stream_icon") or "")
            if source(row) in {"epgshare01", "epgshare"}:
                source_ids.add(str(row["epg_id"]).strip())
    urls = []
    if icon_config is not None:
        with icon_config.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not enabled(row) or not row.get("icon_url", "").strip():
                    # Local-file artwork is staged by this build and must not
                    # be rejected because its public URL is not deployed yet.
                    continue
                if selected is not None:
                    stream = str(row.get("stream_id", "")).strip()
                    epg = str(row.get("epg_id", "")).strip().casefold()
                    name = str(row.get("channel_name", "")).strip().casefold()
                    server = str(row.get("server_id", "*")).strip() or "*"
                    candidates = by_stream[stream] if stream else by_epg[epg] if epg else by_name[name]
                    if not any(
                        (server == "*" or server == candidate.get("server_id", "").strip())
                        and (not stream or stream == candidate.get("stream_id", "").strip())
                        and (not epg or epg == candidate.get("epg_id", "").strip().casefold())
                        and (not name or name == candidate.get("channel_name", "").strip().casefold())
                        for candidate in candidates
                    ):
                        continue
                urls.append(row["icon_url"])
    urls.extend(mapping_urls)
    catalog = json.loads(source_icon_catalog.read_text(encoding="utf-8"))
    urls.extend(record.get("icon_url", "") for record in catalog.get("channel_records", [])
                if selected is None or record.get("epg_id") in source_ids)
    return list(dict.fromkeys(clean for url in urls if (clean := normalize_logo_url(url))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-icon-catalog", type=Path, required=True)
    parser.add_argument("--icon-config", type=Path)
    parser.add_argument("--mapping-csv", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--max-checks", type=int, default=2_000)
    parser.add_argument("--deadline-seconds", type=float, default=180)
    args = parser.parse_args()
    urls = collect_logo_verification_urls(args.source_icon_catalog,
        icon_config=args.icon_config, mapping_csv=args.mapping_csv)
    records = verify_logo_urls(urls, cache_path=args.cache, max_checks=args.max_checks,
                               deadline_seconds=args.deadline_seconds)
    from collections import Counter
    print(json.dumps({"images": len(records), "statuses": dict(Counter(
        record["status"] for record in records.values()))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
