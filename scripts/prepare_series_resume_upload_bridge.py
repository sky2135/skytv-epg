#!/usr/bin/env python3
"""PROPOSED CI-only bridge: pinned existing VOD artifacts to a private bundle.

No provider/TMDB clients, ordinary rebuild, deployment or Git mutation. Network
calls, when explicitly run in Actions, are confined to the existing GitHub
artifact reader. This proposal has not been dispatched or executed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
ARTIFACT_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
# Accepted BFD runtime additions; approved Git SHA additionally binds all imports.
BFD_RUNTIME = {
    "scripts/series_resume_publication.py": "5992a21219df3b7d2e469e8f34f5e61a9be4187cd4c604a20483a29b8009f7b6",
    "scripts/app_catalog_closure.py": "316d8cf26a3467b19511a2d81535bcf9919cf5852fe3be673e7040c403be363a",
    "scripts/prepare_series_resume_component.py": "dbc0beb5d53281285388b1ae794828f06141f4c2ce36cd9022fc65ed50486a7f",
}
INPUT_KEYS = {"artifactId", "artifactSha256", "snapshotManifestSha256", "indexSha256"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path, maximum):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum,
            "A pinned file is missing, linked or oversized.")
    result, count = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            require(count <= maximum, "A pinned file exceeded its byte ceiling.")
            result.update(chunk)
    return result.hexdigest()


def pins_from_json(raw):
    require(isinstance(raw, str) and len(raw.encode("utf-8")) <= 16_384, "Pins must be bounded JSON.")

    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "Duplicate pin key.")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    require(isinstance(value, dict) and set(value) == {"current", "lastGood", "preparedSnapshotSha256"},
            "Supply current, lastGood and preparedSnapshotSha256 pins exactly.")
    for role in ("current", "lastGood"):
        item = value[role]
        require(isinstance(item, dict) and set(item) == INPUT_KEYS, "Each input needs all four exact pins.")
        require(isinstance(item["artifactId"], str) and ARTIFACT_ID.fullmatch(item["artifactId"]),
                "Artifact IDs must be explicit positive decimal strings.")
        for key in INPUT_KEYS - {"artifactId"}:
            require(isinstance(item[key], str) and SHA256.fullmatch(item[key]), "All digests must be lowercase SHA256.")
    require(isinstance(value["preparedSnapshotSha256"], str) and SHA256.fullmatch(value["preparedSnapshotSha256"]),
            "The reviewed expected prepared snapshot SHA256 is required.")
    if value["current"]["artifactId"] == value["lastGood"]["artifactId"]:
        require(value["current"] == value["lastGood"], "One artifact ID cannot have conflicting role pins.")
    return value


def run(approved_dir, approved_code_sha, pins, work_dir):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "This proposal is CI-only; do not run it as a desktop uploader.")
    require(isinstance(approved_code_sha, str) and GIT_SHA.fullmatch(approved_code_sha),
            "An exact reviewed source commit SHA is required.")
    approved = Path(approved_dir).resolve(strict=True)
    actual = subprocess.check_output(["git", "-C", str(approved), "rev-parse", "HEAD"], text=True).strip()
    require(actual == approved_code_sha, "Checked-out source differs from its approved commit.")
    for name, expected in BFD_RUNTIME.items():
        require(digest(approved / name, 1024 * 1024) == expected, "An accepted BFD runtime file differs.")
    work = Path(work_dir).absolute()
    require(not work.exists() and approved != work and approved not in work.parents and work not in approved.parents,
            "Use a NEW private work directory outside the approved checkout.")
    require(not (approved / "public").exists(), "The fresh approved checkout must have no preexisting public tree.")
    sys.path.insert(0, str(approved / "scripts"))
    import component_artifacts as transport
    import publication_snapshots as snapshots
    import prepare_series_resume_component as prepared
    import app_catalog_closure as closure

    work.mkdir(parents=True)
    client = transport.GitHubArtifacts()
    inputs = {}
    restored = {}
    for role in ("current", "lastGood"):
        pin = pins[role]
        identifier = int(pin["artifactId"])
        if identifier not in restored:
            artifact = client.get(f"/actions/artifacts/{identifier}")
            require(artifact.get("id") == identifier
                    and transport.GitHubArtifacts._artifact_identity("vod", artifact) is not None
                    and client._validated("vod", artifact, {}, {}),
                    "A pinned input is not a successful main-branch VOD component.")
            archive = work / (pin["artifactId"] + ".zip")
            client.download(artifact, archive)
            require(digest(archive, transport.LIMIT) == pin["artifactSha256"], "Input artifact ZIP SHA256 differs.")
            destination = work / ("input-" + pin["artifactId"])
            transport.extract_zip(archive, destination)
            require(digest(destination / "snapshot.json", snapshots.MAX_MANIFEST_BYTES)
                    == pin["snapshotManifestSha256"], "Input snapshot manifest SHA256 differs.")
            snapshots._snapshot(destination, "vod")
            require(digest(destination / "files/skytvplayer/index.json", snapshots.MAX_INDEX_BYTES)
                    == pin["indexSha256"], "Input app index SHA256 differs.")
            restored[identifier] = destination
        inputs[role] = restored[identifier]

    bundle = work / "bundle"
    report = prepared.prepare(inputs["current"] / "files", inputs["lastGood"] / "files/skytvplayer", bundle,
                              expected_index_sha256=pins["current"]["indexSha256"],
                              expected_last_good_index_sha256=pins["lastGood"]["indexSha256"], enabled=True)
    snapshot_sha = digest(bundle / "snapshot/snapshot.json", snapshots.MAX_MANIFEST_BYTES)
    require(snapshot_sha == pins["preparedSnapshotSha256"] == report["snapshotManifestSha256"],
            "Generated snapshot differs from the independently reviewed output; stop for reconciliation.")
    _, manifest = snapshots._snapshot(bundle / "snapshot", "vod")
    transfer = closure.check_transfer_members(entry["path"] for entry in manifest["files"])
    require(report.get("mode") == "attach" and report.get("publicReferenceActivated") is False,
            "Expected a non-activating enabled preparation result.")
    # Approved code validates original retention ownership and carries bounded
    # current/last-good/compensation-anchor closures. No input filtering, record
    # editing, synthetic anchor or provider reconstruction is performed here.
    prepared.materialize(bundle, approved / "public", expected_snapshot_sha256=snapshot_sha)
    # Include the otherwise out-of-tree report in the existing scanner's roots.
    reports = approved / ".build/reports/series-resume-bridge"
    reports.mkdir(parents=True)
    (reports / "report.json").write_bytes((bundle / "report.json").read_bytes())
    (reports / "snapshot-manifest.json").write_bytes((bundle / "snapshot/snapshot.json").read_bytes())
    receipt = {"schema": "SERIES_RESUME_PRIVATE_UPLOAD_BRIDGE_V1", "approvedCodeSha": approved_code_sha,
               "pins": pins, "transfer": transfer, "publicReferenceActivated": False,
               "configuredSecretScanPending": True}
    (reports / "input-pins.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT")
    require(bool(output), "Actions output channel is missing.")
    values = {"bundle-dir": str(bundle), "snapshot-sha256": snapshot_sha, "approved-dir": str(approved)}
    require(all("\n" not in value and "\r" not in value for value in values.values()), "Unsafe output path.")
    with Path(output).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(key + "=" + value + "\n")
    print(json.dumps({"preparedSnapshotSha256": snapshot_sha, "transfer": transfer,
                      "currentArtifactId": pins["current"]["artifactId"],
                      "lastGoodArtifactId": pins["lastGood"]["artifactId"],
                      "uploaded": False, "published": False}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-dir", type=Path, required=True)
    parser.add_argument("--approved-code-sha", required=True)
    parser.add_argument("--pins-json", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        pins = pins_from_json(args.pins_json)
        run(args.approved_dir, args.approved_code_sha, pins, args.work_dir)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.CalledProcessError):
        # Do not echo authenticated request objects or raw pin JSON on failure.
        parser.exit(2, "Private preparation bridge stopped; no upload/publication step is authorized by this result.\n")


if __name__ == "__main__":
    main()
