"""Keep validated native guides private, bounded, and tied to their provider.

The builder calls ``save_source`` only after XML validation and programme
freshness checks. A cache hit is still re-ingested and checked by the builder;
this module establishes the bytes, account binding and original capture time.
Only the seal/restore commands require the service-account encryption secret.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if __package__:
    from .private_matcher_cache import encryption_key as matching_encryption_key
else:
    from private_matcher_cache import encryption_key as matching_encryption_key


SCHEMA_VERSION = 1
SERVER_IDS = frozenset({"server_2", "server_3"})
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * MAX_SOURCE_BYTES + 64 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 64 * 1024
MAX_METADATA_BYTES = 8192
MAX_AGE_SECONDS = 36 * 3600
MAGIC = b"SKYTV-NATIVE-GUIDE-CACHE-1\n"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_NAMES = frozenset(
    server + suffix
    for server in SERVER_IDS
    for suffix in (".xmltv", ".json", ".cooldown.json")
)


def _server(server_id: str) -> str:
    if server_id not in SERVER_IDS:
        raise ValueError("Unsupported native-guide cache server")
    return server_id


def _safe_path(path: Path) -> bool:
    """Do not follow symlinks in a private cache or its ancestor path."""
    return not any(part.is_symlink() for part in (path, *path.parents))


def _read_regular(path: Path, maximum_bytes: int) -> bytes:
    if not _safe_path(path):
        raise ValueError("Unsafe private guide cache path")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= maximum_bytes:
            raise ValueError("Invalid private guide cache file")
        content = handle.read(maximum_bytes + 1)
        if len(content) > maximum_bytes:
            raise ValueError("Private guide cache exceeds its size bound")
        return content


def _write_atomic(path: Path, content: bytes) -> None:
    if not _safe_path(path) or (path.exists() and not path.is_file()):
        raise ValueError("Unsafe private guide cache destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _safe_path(path.parent):
        raise ValueError("Unsafe private guide cache directory")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".panel-cache-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fingerprint(server_id: str, credentials: tuple[str, str, str]) -> str:
    if not isinstance(credentials, (tuple, list)) or len(credentials) != 3:
        raise ValueError("Invalid native-guide account binding")
    if any(not isinstance(value, str) or not value for value in credentials):
        raise ValueError("Invalid native-guide account binding")
    # The password-derived HMAC key prevents metadata from exposing a useful
    # unkeyed hash of a private origin or username. The account secret itself
    # appears only inside the HMAC input, never in persisted metadata.
    key = hashlib.sha256(b"skytv-native-guide-binding-key-v1\0" + credentials[2].encode()).digest()
    payload = json.dumps([server_id, *credentials], ensure_ascii=True, separators=(",", ":")).encode()
    return hmac.new(key, b"skytv-native-guide-account-v1\0" + payload, hashlib.sha256).hexdigest()


def _metadata(raw: bytes, server_id: str, *, cooldown: bool = False) -> dict[str, Any]:
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("Oversized native-guide cache metadata")
    value = json.loads(raw)
    expected = {"schemaVersion", "serverId", "originFingerprint"}
    expected |= {"nextAttemptAt"} if cooldown else {"capturedAt", "sha256", "bytes"}
    if (not isinstance(value, dict) or set(value) != expected
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != SCHEMA_VERSION
            or value.get("serverId") != server_id
            or not isinstance(value.get("originFingerprint"), str)
            or not _HEX.fullmatch(value["originFingerprint"])):
        raise ValueError("Invalid native-guide cache metadata")
    timestamp = value.get("nextAttemptAt" if cooldown else "capturedAt")
    if (type(timestamp) is not int or timestamp <= 0
            or (not cooldown and timestamp > 32503680000)):
        raise ValueError("Invalid native-guide cache timestamp")
    if not cooldown and (
        type(value.get("bytes")) is not int or not 0 < value["bytes"] <= MAX_SOURCE_BYTES
        or not isinstance(value.get("sha256"), str) or not _HEX.fullmatch(value["sha256"])
    ):
        raise ValueError("Invalid native-guide cache source identity")
    return value


def _render(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def save_source(
    server_id: str, source_path: Path, cache_dir: Path, *,
    credentials: tuple[str, str, str], captured_at: int,
) -> bool:
    """Save builder-validated bytes without extending their capture time."""
    try:
        _server(server_id)
        if type(captured_at) is not int:
            return False
        body = _read_regular(source_path, MAX_SOURCE_BYTES)
        value = {"schemaVersion": SCHEMA_VERSION, "serverId": server_id,
                 "originFingerprint": _fingerprint(server_id, credentials),
                 "capturedAt": captured_at, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        raw = _render(value)
        _metadata(raw, server_id)
        if not _safe_path(cache_dir):
            return False
        # A torn pair is rejected by its digest on load. Neither a malformed
        # metadata file nor partial copy can be accepted as a valid guide.
        _write_atomic(cache_dir / (server_id + ".xmltv"), body)
        _write_atomic(cache_dir / (server_id + ".json"), raw)
        clear_cooldown(server_id, cache_dir)
        return True
    except (OSError, TypeError, ValueError, UnicodeError):
        return False


def load_source(
    server_id: str, cache_dir: Path, *, credentials: tuple[str, str, str],
    now_epoch: int, max_age_seconds: int = MAX_AGE_SECONDS,
    snapshot_path: Path | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    """Return authenticated-cache bytes only for the original account and age."""
    try:
        _server(server_id)
        if (type(now_epoch) is not int or type(max_age_seconds) is not int
                or not 0 < max_age_seconds <= MAX_AGE_SECONDS):
            return None
        value = _metadata(_read_regular(cache_dir / (server_id + ".json"), MAX_METADATA_BYTES), server_id)
        if not hmac.compare_digest(value["originFingerprint"], _fingerprint(server_id, credentials)):
            return None
        age = now_epoch - value["capturedAt"]
        if not 0 <= age <= max_age_seconds:
            return None
        source = cache_dir / (server_id + ".xmltv")
        body = _read_regular(source, MAX_SOURCE_BYTES)
        if len(body) != value["bytes"] or hashlib.sha256(body).hexdigest() != value["sha256"]:
            return None
        if snapshot_path is not None:
            # Give the parser the exact checked bytes in its own private work
            # directory, rather than reopening a mutable restored-cache path.
            if snapshot_path.resolve() == cache_dir.resolve() or snapshot_path.resolve().is_relative_to(cache_dir.resolve()):
                return None
            _write_atomic(snapshot_path, body)
            source = snapshot_path
        return source, {"sha256": value["sha256"], "bytes": value["bytes"],
                        "cacheCapturedAt": value["capturedAt"], "cacheAgeSeconds": age,
                        "inputMode": "private-last-good-cache", "cacheReused": True}
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def save_cooldown(
    server_id: str, cache_dir: Path, *, credentials: tuple[str, str, str], next_attempt_at: int,
) -> bool:
    try:
        _server(server_id)
        value = {"schemaVersion": SCHEMA_VERSION, "serverId": server_id,
                 "originFingerprint": _fingerprint(server_id, credentials), "nextAttemptAt": next_attempt_at}
        raw = _render(value)
        _metadata(raw, server_id, cooldown=True)
        try:
            previous = _metadata(_read_regular(cache_dir / (server_id + ".cooldown.json"), MAX_METADATA_BYTES),
                                 server_id, cooldown=True)
        except (OSError, TypeError, ValueError, UnicodeError):
            previous = None
        if previous is not None and hmac.compare_digest(previous["originFingerprint"], value["originFingerprint"]):
            value["nextAttemptAt"] = max(previous["nextAttemptAt"], next_attempt_at)
            raw = _render(value)
        _write_atomic(cache_dir / (server_id + ".cooldown.json"), raw)
        return True
    except (OSError, TypeError, ValueError, UnicodeError):
        return False


def load_cooldown(
    server_id: str, cache_dir: Path, *, credentials: tuple[str, str, str], now_epoch: int,
) -> int | None:
    try:
        _server(server_id)
        value = _metadata(_read_regular(cache_dir / (server_id + ".cooldown.json"), MAX_METADATA_BYTES),
                          server_id, cooldown=True)
        if (type(now_epoch) is not int or value["nextAttemptAt"] <= now_epoch
                or not hmac.compare_digest(value["originFingerprint"], _fingerprint(server_id, credentials))):
            return None
        return value["nextAttemptAt"]
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def clear_cooldown(server_id: str, cache_dir: Path) -> bool:
    try:
        _server(server_id)
        path = cache_dir / (server_id + ".cooldown.json")
        if not _safe_path(path):
            return False
        path.unlink(missing_ok=True)
        return True
    except (OSError, ValueError):
        return False


def encryption_key() -> bytes | None:
    key = matching_encryption_key()
    return None if key is None else hmac.new(key, MAGIC, hashlib.sha256).digest()


def _validate_files(files: dict[str, bytes]) -> bool:
    if not files or set(files) - _ALLOWED_NAMES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        return False
    for server_id in SERVER_IDS:
        source_name, meta_name = server_id + ".xmltv", server_id + ".json"
        if (source_name in files) != (meta_name in files):
            return False
        if meta_name in files:
            meta = _metadata(files[meta_name], server_id)
            if len(files[source_name]) != meta["bytes"] or hashlib.sha256(files[source_name]).hexdigest() != meta["sha256"]:
                return False
        cooldown_name = server_id + ".cooldown.json"
        if cooldown_name in files:
            _metadata(files[cooldown_name], server_id, cooldown=True)
    return True


def seal(directory: Path, destination: Path) -> bool:
    """Persist only authenticated ciphertext to the shared Actions cache."""
    try:
        key = encryption_key()
        if key is None or not _safe_path(directory) or not directory.is_dir():
            return False
        names = [path.name for path in directory.iterdir()]
        if len(names) > len(_ALLOWED_NAMES) or set(names) - _ALLOWED_NAMES:
            return False
        files = {name: _read_regular(directory / name, MAX_SOURCE_BYTES if name.endswith(".xmltv") else MAX_METADATA_BYTES)
                 for name in names}
        if not _validate_files(files):
            return False
        archive = io.BytesIO()
        with ZipFile(archive, "w", compression=ZIP_STORED) as zipped:
            for name, body in sorted(files.items()):
                zipped.writestr(name, body)
        raw = archive.getvalue()
        if len(raw) > MAX_ARCHIVE_BYTES:
            return False
        nonce = os.urandom(12)
        _write_atomic(destination, MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, MAGIC))
        return True
    except (OSError, TypeError, ValueError, UnicodeError, BadZipFile):
        return False


def restore(source: Path, directory: Path) -> bool:
    """Validate the complete sealed bundle before writing any restored source."""
    try:
        key = encryption_key()
        if key is None or not _safe_path(directory):
            return False
        raw = _read_regular(source, MAX_ARCHIVE_BYTES + len(MAGIC) + 28)
        if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 28:
            return False
        nonce = raw[len(MAGIC):len(MAGIC) + 12]
        body = AESGCM(key).decrypt(nonce, raw[len(MAGIC) + 12:], MAGIC)
        with ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if not 0 < len(entries) <= len(_ALLOWED_NAMES):
                return False
            files = {}
            total = 0
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.filename not in _ALLOWED_NAMES or entry.filename in files
                        or entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1
                        or entry.compress_type != ZIP_STORED):
                    return False
                maximum = MAX_SOURCE_BYTES if entry.filename.endswith(".xmltv") else MAX_METADATA_BYTES
                total += entry.file_size
                if not 0 <= entry.file_size <= maximum or total > MAX_TOTAL_BYTES:
                    return False
                files[entry.filename] = archive.read(entry)
        if not _validate_files(files):
            return False
        for name in files:
            path = directory / name
            if not _safe_path(path) or (path.exists() and not path.is_file()):
                return False
        for name, content in files.items():
            _write_atomic(directory / name, content)
        return True
    except (InvalidTag, OSError, TypeError, ValueError, UnicodeError, BadZipFile):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seal", "restore"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    ok = seal(args.directory, args.file) if args.mode == "seal" else restore(args.file, args.directory)
    print("Private native-guide cache " + ("ready." if ok else "unavailable; current provider checks still apply."))


if __name__ == "__main__":
    main()
