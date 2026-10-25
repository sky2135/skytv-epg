"""Bounded shared candidate index derived only from verified TMDB metadata.

Image paths are opaque identifiers, never encoded movie IDs. Index hits remain
candidates: callers must recheck current official details and all identity gates.
The verified detail caches persist the evidence; this module writes no files.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import weakref
from urllib.parse import urlsplit

POSTER_POLICY = 1
MAX_PATHS = 100_000
MAX_IMAGES = 1_000
IMAGE_PATH = re.compile(r"/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)\Z", re.I)
URL_PATH = re.compile(r"/t/p/(?:original|w[1-9][0-9]{0,3}(?:_and_h[1-9][0-9]{0,3})?(?:_bestv2|_face)?)(/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp))\Z", re.I)
_instances = weakref.WeakValueDictionary()
_instances_lock = threading.Lock()


def seed_collision_file(index, path):
    """Restore rejection-only collision evidence before reading imported proofs."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return 0
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024
            or any(parent.is_symlink() for parent in path.parents)):
        raise ValueError("Invalid persisted poster collision evidence")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError):
        raise ValueError("Invalid persisted poster collision evidence") from None
    paths = document.get("collidedPosterPaths") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1
            or not isinstance(paths, list) or len(paths) > MAX_PATHS
            or any(not isinstance(value, str) or not IMAGE_PATH.fullmatch(value) for value in paths)):
        raise ValueError("Invalid persisted poster collision evidence")
    proof = {"names": ["persisted poster collision"], "year": None}
    for value in set(paths):
        index.add("movie", None, [value], proof)
        index.add("series", None, [value], proof)
    return len(set(paths))


def poster_identity(item):
    artwork = item.get("artwork")
    value = artwork.get("posterUrl") if isinstance(artwork, dict) else None
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in {"image.tmdb.org", "media.themoviedb.org"}
                or url.username is not None or url.password is not None or url.port not in (None, 443)
                or url.query or url.fragment):
            return None
        matched = URL_PATH.fullmatch(url.path)
        return matched.group(1) if matched else None
    except ValueError:
        return None


def official_paths(details):
    """Return exact official poster paths and whether alternate images are complete."""
    default = details.get("poster_path")
    paths = {default} if isinstance(default, str) and IMAGE_PATH.fullmatch(default) else set()
    images = details.get("images")
    posters = images.get("posters") if isinstance(images, dict) else None
    complete = isinstance(posters, list) and len(posters) <= MAX_IMAGES
    if complete:
        for image in posters:
            path = image.get("file_path") if isinstance(image, dict) else None
            if not isinstance(path, str) or not IMAGE_PATH.fullmatch(path):
                complete = False
                break
            paths.add(path)
    return paths, complete


class VerifiedPosterIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._paths = {}

    def add(self, kind, identifier, paths, proof):
        names, year = proof.get("names"), proof.get("year")
        if kind not in {"movie", "series"} or not isinstance(names, list) or not names:
            return
        with self._lock:
            for path in paths:
                if not isinstance(path, str) or not IMAGE_PATH.fullmatch(path):
                    continue
                if path not in self._paths and len(self._paths) >= MAX_PATHS:
                    continue
                entries = self._paths.setdefault(path, {})
                # Retain bounded collision evidence; never let truncation turn
                # a shared movie/TV image into an apparently unique candidate.
                if len(entries) >= 4 and (kind, identifier) not in entries:
                    entries[None] = None
                    continue
                previous = entries.get((kind, identifier))
                combined = set(names)
                if previous:
                    combined.update(previous["names"])
                    if previous["year"] != year:
                        year = None
                entries[(kind, identifier)] = {"names": tuple(sorted(combined)[:102]), "year": year}

    def lookup(self, path, kind, normalized_title, year):
        with self._lock:
            entries = self._paths.get(path, {})
            if len(entries) != 1:
                return None
            (identity, proof), = entries.items()
            if identity is None or identity[0] != kind or not year:
                return None
            return identity[1] if normalized_title in proof["names"] and proof["year"] == year else None

    def collided(self, path):
        with self._lock:
            return len(self._paths.get(path, {})) > 1


def shared_index(directory):
    key = str(directory.resolve())
    with _instances_lock:
        index = _instances.get(key)
        if index is None:
            index = VerifiedPosterIndex()
            _instances[key] = index
        return index
