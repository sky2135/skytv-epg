"""Install the already-built, validated VOD snapshot after EPG staging finishes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import build_vod_catalogs as vod


def _safe_directory(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise vod.CatalogBuildError("VOD installation paths cannot use symbolic links.")
    return path


def install(source, output, expected_servers=vod.DEFAULT_SERVERS):
    source, output = _safe_directory(source), _safe_directory(output)
    if source.resolve() == output.resolve() or source.resolve() in output.resolve().parents or output.resolve() in source.resolve().parents:
        raise vod.CatalogBuildError("VOD staging and installation paths must be separate.")
    index_path = source / "index.json"
    if index_path.is_symlink() or not index_path.is_file() or index_path.stat().st_size > vod.MAX_INDEX_BYTES:
        raise vod.CatalogBuildError("The prepared VOD index is unsafe or oversized.")
    raw_index = index_path.read_bytes()
    vod.validate_publication(source, expected_servers=expected_servers)
    if index_path.read_bytes() != raw_index:
        raise vod.CatalogBuildError("The prepared VOD release changed during validation.")
    index = json.loads(raw_index)
    files = {}
    for server in index["servers"]:
        name = server["manifestFile"]
        raw = (source / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != server["manifestSha256"]:
            raise vod.CatalogBuildError("The prepared VOD manifest changed during installation.")
        files[name] = (raw, server["manifestSha256"])
        for catalog in json.loads(raw)["catalogs"].values():
            files[catalog["file"]] = (None, catalog["sha256"])
    output.mkdir(parents=True, exist_ok=True)
    for name, (raw, digest) in files.items():
        origin, target = source / name, output / name
        if origin.is_symlink() or target.is_symlink() or not origin.is_file():
            raise vod.CatalogBuildError("VOD installation encountered an unsafe artifact.")
        raw = origin.read_bytes() if raw is None else raw
        if hashlib.sha256(raw).hexdigest() != digest:
            raise vod.CatalogBuildError("The prepared VOD data changed during installation.")
        temporary = vod._temporary_path(output, "vod-install")
        try:
            temporary.write_bytes(raw)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    if (source / "index.json").read_bytes() != raw_index:
        raise vod.CatalogBuildError("The prepared VOD release changed during installation.")
    vod.write_final_index(output, index)
    return {"serverCount": index["serverCount"], "artifactCount": len(files) + 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(install(args.source, args.output_dir), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, vod.CatalogBuildError):
        raise SystemExit("Prepared VOD installation failed validation.") from None
