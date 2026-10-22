"""Encrypt private matching hints before placing them in an Actions cache."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_BYTES = 64 * 1024 * 1024
MAGIC = b"SKYTV-MATCH-CACHE-1\n"


def encryption_key() -> bytes | None:
    value = os.environ.get("SKYTV_MATCH_CACHE_KEY", "")
    if not value:
        try:
            raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
            if len(raw) > 128 * 1024:
                return None
            try:
                account = json.loads(raw)
            except ValueError:
                account = json.loads(base64.b64decode(raw, validate=True))
            value = account["private_key"]
        except (KeyError, TypeError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(b"skytv-private-matcher-encryption-v1\0" + value.encode()).digest()


def seal(directory: Path, destination: Path) -> bool:
    key = encryption_key()
    if key is None or not directory.is_dir() or directory.is_symlink():
        return False
    files = {}
    total = 0
    for path in sorted(directory.iterdir()):
        if path.name.endswith(".tmp.json"):
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ValueError("Unexpected private cache entry")
        total += path.stat().st_size
        if total > MAX_BYTES or len(files) >= 16:
            raise ValueError("Private cache exceeds its size bound")
        files[path.name] = base64.b64encode(path.read_bytes()).decode("ascii")
    if not files:
        return False
    raw = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    nonce = os.urandom(12)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, MAGIC))
    return True


def restore(source: Path, directory: Path) -> bool:
    key = encryption_key()
    if key is None or not source.is_file() or source.is_symlink():
        return False
    try:
        if source.stat().st_size > MAX_BYTES * 2:
            return False
        raw = source.read_bytes()
        if not raw.startswith(MAGIC):
            return False
        nonce = raw[len(MAGIC):len(MAGIC) + 12]
        files = json.loads(AESGCM(key).decrypt(nonce, raw[len(MAGIC) + 12:], MAGIC))
        if not isinstance(files, dict) or not 0 < len(files) <= 16:
            return False
        decoded = {}
        total = 0
        for name, value in files.items():
            if (not isinstance(name, str) or Path(name).name != name or "/" in name
                    or "\\" in name or not name.endswith(".json") or ":" in name):
                return False
            if not isinstance(value, str):
                return False
            content = base64.b64decode(value, validate=True)
            total += len(content)
            if total > MAX_BYTES:
                return False
            decoded[name] = content
        if directory.is_symlink():
            return False
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in decoded.items():
            path = directory / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                return False
        for name, content in decoded.items():
            (directory / name).write_bytes(content)
        return True
    except (InvalidTag, ValueError, TypeError, OSError, UnicodeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seal", "restore"))
    parser.add_argument("--directory", type=Path, default=Path(".build/matcher-cache"))
    parser.add_argument("--file", type=Path, default=Path(".build/matcher-cache.enc"))
    args = parser.parse_args()
    try:
        ok = seal(args.directory, args.file) if args.mode == "seal" else restore(args.file, args.directory)
    except (ValueError, OSError):
        ok = False
    print("Private matching cache " + ("ready." if ok else "unavailable; normal matching will run."))


if __name__ == "__main__":
    main()
