"""Stable file access and deterministic private artifact output."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ContractError, canonical_json_bytes, sha256_bytes, sha256_json


MAX_PROPOSAL_BYTES = 256 * 1024 * 1024
MAX_PROPOSAL_LINE_BYTES = 512 * 1024


def read_stable_regular_file(path: Path, *, maximum_bytes: int) -> tuple[bytes, str]:
    target = Path(path)
    try:
        before_path = target.lstat()
    except OSError as exc:
        raise ContractError("A required Matching Lab input is unavailable.") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ContractError("A Matching Lab input must be a regular non-symlink file.")
    if before_path.st_size > int(maximum_bytes):
        raise ContractError("A Matching Lab input exceeds its size limit.")
    try:
        with target.open("rb") as handle:
            before_handle = os.fstat(handle.fileno())
            if (
                before_handle.st_dev != before_path.st_dev
                or before_handle.st_ino != before_path.st_ino
                or not stat.S_ISREG(before_handle.st_mode)
            ):
                raise ContractError("A Matching Lab input changed while opening.")
            content = handle.read(int(maximum_bytes) + 1)
            after_handle = os.fstat(handle.fileno())
        after_path = target.lstat()
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("A Matching Lab input could not be read safely.") from exc
    if len(content) > int(maximum_bytes):
        raise ContractError("A Matching Lab input exceeds its size limit.")
    stable = (
        before_handle.st_dev,
        before_handle.st_ino,
        before_handle.st_size,
        before_handle.st_mtime_ns,
        before_handle.st_ctime_ns,
    )
    if stable != (
        after_handle.st_dev,
        after_handle.st_ino,
        after_handle.st_size,
        after_handle.st_mtime_ns,
        after_handle.st_ctime_ns,
    ) or stable != (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_size,
        after_path.st_mtime_ns,
        after_path.st_ctime_ns,
    ):
        raise ContractError("A Matching Lab input changed while it was read.")
    return content, sha256_bytes(content)


def _atomic_write(path: Path, content: bytes) -> None:
    target = Path(path)
    if target.exists():
        raise ContractError("A Matching Lab output artifact already exists.")
    temporary = target.with_suffix(target.suffix + ".part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except FileExistsError as exc:
        raise ContractError("A Matching Lab temporary output already exists.") from exc
    except OSError as exc:
        raise ContractError("A Matching Lab artifact could not be written safely.") from exc


def jsonl_bytes(records: Iterable[Mapping[str, object]]) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for record in records:
        line = canonical_json_bytes(record) + b"\n"
        if len(line) > MAX_PROPOSAL_LINE_BYTES:
            raise ContractError("One proposal record exceeds its line-size limit.")
        total += len(line)
        if total > MAX_PROPOSAL_BYTES:
            raise ContractError("The proposal artifact exceeds its size limit.")
        chunks.append(line)
    return b"".join(chunks)


def write_private_bundle(
    output_dir: Path,
    *,
    proposals: Iterable[Mapping[str, object]],
    summary: Mapping[str, object],
    manifest_factory: Any,
) -> tuple[bytes, bytes, bytes]:
    target = Path(output_dir)
    try:
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ContractError(
                    "The Matching Lab output directory must be a real directory."
                )
            if any(target.iterdir()):
                raise ContractError(
                    "The Matching Lab output directory must be new and empty."
                )
        else:
            target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError(
            "The Matching Lab output directory could not be prepared safely."
        ) from exc
    proposal_content = jsonl_bytes(proposals)
    summary_content = canonical_json_bytes(summary) + b"\n"
    manifest = manifest_factory(
        proposals_sha256=sha256_bytes(proposal_content),
        summary_sha256=sha256_bytes(summary_content),
    )
    manifest_content = canonical_json_bytes(manifest) + b"\n"
    _atomic_write(target / "proposals.jsonl", proposal_content)
    _atomic_write(target / "summary.json", summary_content)
    _atomic_write(target / "manifest.json", manifest_content)
    return proposal_content, summary_content, manifest_content


def package_code_sha256(root: Path) -> str:
    base = Path(root)
    repository = base.parent
    files: set[Path] = set()
    files.update(
        path
        for path in (repository / "matching_lab").glob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    for name in (
        "scripts/ai_review_gemini.py",
        "scripts/ai_review_policy.py",
        "scripts/auto_match_inventory.py",
        "scripts/build_epg_streaming.py",
        "scripts/epg_catalog_stream.py",
        "scripts/epg_selection_spool.py",
        "scripts/native_epg_review.py",
        "scripts/sync_channel_inventory.py",
        "src/skytv_epg_auto_match_v1.py",
        "src/skytv_epg_contextual_v8.py",
        "src/skytv_epg_engine.py",
        "knowledge/approved_channel_aliases.csv",
        "knowledge/schedule_equivalence_groups.json",
        "config/channel_icons.csv",
        "config/epg_sources.json",
        "requirements.txt",
    ):
        path = repository / name
        if path.is_file() and not path.is_symlink():
            files.add(path)
    return sha256_json(
        {
            "schema": "skytv-matching-lab-code-v1",
            "files": [
                [path.relative_to(repository).as_posix(), sha256_bytes(path.read_bytes())]
                for path in sorted(files)
            ],
        }
    )


class _DuplicateKey(ContractError):
    pass


def strict_json_loads(content: bytes) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("A JSON artifact is not valid UTF-8.") from exc
    if text.startswith("\ufeff"):
        raise ContractError("A JSON artifact must not contain a BOM.")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise _DuplicateKey("A JSON artifact contains duplicate keys.")
            result[key] = value
        return result

    def reject_float(_value: str) -> None:
        raise ContractError("Proposal artifacts must not contain floating-point values.")

    def reject_constant(_value: str) -> None:
        raise ContractError("Proposal artifacts must not contain NaN or infinity.")

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError("A JSON artifact is malformed.") from exc


__all__ = (
    "MAX_PROPOSAL_BYTES",
    "MAX_PROPOSAL_LINE_BYTES",
    "jsonl_bytes",
    "package_code_sha256",
    "read_stable_regular_file",
    "strict_json_loads",
    "write_private_bundle",
)
