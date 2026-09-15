#!/usr/bin/env python3
"""Merge legacy per-server mappings into the Google Sheets Version 1 schema.

The generated CSV is a migration seed, not an automatic approval of inferred
metadata.  It deliberately delegates normalization and classification to
``build_epg_streaming`` so the spreadsheet and production build use the same
controlled vocabulary and validation rules.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO

import build_epg_streaming as streaming


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path("skytv_google_sheet_seed.csv")
ALLOWED_SOURCE_VALUES = frozenset({"epgshare01", "epgshare", "dummy", "panel"})


class SeedExportError(RuntimeError):
    """An actionable migration error safe to print in CI or a terminal."""


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a UTF-8 CSV and normalize headers exactly as production does."""
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        raise SeedExportError(f"Cannot inspect mapping file: {path}") from exc
    if size > streaming.MAX_MAPPING_BYTES:
        raise SeedExportError(
            f"Mapping exceeds the {streaming.MAX_MAPPING_BYTES:,}-byte limit: {path}"
        )
    try:
        handle = Path(path).open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise SeedExportError(f"Cannot open mapping file: {path}") from exc

    with handle:
        reader = csv.reader(handle)
        try:
            raw_headers = next(reader)
        except StopIteration as exc:
            raise SeedExportError(f"Mapping file is empty: {path}") from exc

        headers = [streaming.normalized_header(value) for value in raw_headers]
        if not headers or any(not header for header in headers):
            raise SeedExportError(f"Mapping has a blank header: {path}")
        duplicates = sorted(
            header for header, count in Counter(headers).items() if count > 1
        )
        if duplicates:
            raise SeedExportError(
                f"Mapping has duplicate normalized headers ({path}): "
                + ", ".join(duplicates)
            )

        required = {"server_id", "stream_id", "channel_name"}
        missing = sorted(required - set(headers))
        if missing:
            raise SeedExportError(
                f"Mapping is missing required headers ({path}): "
                + ", ".join(missing)
            )

        rows: list[dict[str, str]] = []
        for row_number, values in enumerate(reader, start=2):
            if row_number > streaming.MAX_MAPPING_ROWS + 1:
                raise SeedExportError(
                    f"Mapping exceeds {streaming.MAX_MAPPING_ROWS:,} rows: {path}"
                )
            if not values or not any(str(value).strip() for value in values):
                continue
            if len(values) > len(headers):
                raise SeedExportError(
                    f"{path}:{row_number}: more values than column headers."
                )
            values.extend([""] * (len(headers) - len(values)))
            rows.append(
                {headers[index]: str(value) for index, value in enumerate(values)}
            )
        return headers, rows


def pipe(values: Iterable[str]) -> str:
    return "|".join(str(value) for value in values)


def append_note(existing: object, addition: str) -> str:
    text = streaming.clean_text(existing, 500)
    if not addition:
        return text
    if not text:
        return addition
    if addition.casefold() in text.casefold():
        return text
    return streaming.clean_text(f"{text}; {addition}", 500)


def normalized_seed_row(
    raw: Mapping[str, str],
    *,
    path: Path,
    row_number: int,
    selected_servers: set[str],
) -> dict[str, str] | None:
    """Convert one legacy/current row into the canonical Sheet representation."""
    try:
        server_id = streaming.normalize_server_id(raw.get("server_id", ""))
    except streaming.BuildError as exc:
        raise SeedExportError(f"{path}:{row_number}: {exc}") from exc
    if server_id not in selected_servers:
        return None

    stream_id = streaming.clean_identifier(raw.get("stream_id", ""), 120)
    channel_name = streaming.clean_identifier(raw.get("channel_name", ""), 300)
    if not stream_id or not channel_name:
        raise SeedExportError(
            f"{path}:{row_number}: stream_id and channel_name are required."
        )

    original_action = (
        streaming.clean_text(raw.get("action", "APPROVED"), 40).upper()
        or "APPROVED"
    )
    try:
        enabled = streaming.parse_bool(
            raw.get("enabled", ""),
            default=original_action not in {"SKIP", "IGNORE", "REJECTED"},
            field_name=f"{path}:{row_number} enabled",
        )
        metadata = streaming.build_metadata(raw, row_number)
    except streaming.BuildError as exc:
        raise SeedExportError(f"{path}:{row_number}: {exc}") from exc

    sort_text = streaming.clean_text(raw.get("sort_priority", ""), 20)
    try:
        sort_priority = int(sort_text or 1000)
    except ValueError as exc:
        raise SeedExportError(
            f"{path}:{row_number}: sort_priority must be an integer."
        ) from exc

    epg_feed = streaming.clean_text(raw.get("epg_feed", ""), 100)
    source = streaming.clean_text(raw.get("source", ""), 40).casefold()
    if not source:
        source = (
            "panel"
            if epg_feed.casefold() in {"panel", "server xmltv.php"}
            else "epgshare01"
        )
    if source not in ALLOWED_SOURCE_VALUES:
        raise SeedExportError(
            f"{path}:{row_number}: unsupported source {source!r}; use "
            "epgshare01, epgshare, dummy, or panel."
        )
    # Version 1 writes the single canonical Sheet value. The builder continues
    # accepting the historical ``epgshare`` alias when reading older data, but
    # a newly generated seed should never violate its own dropdown validation.
    output_source = "epgshare01" if source == "epgshare" else source

    notes = streaming.clean_text(raw.get("notes", ""), 500)
    legacy_server1_panel = (
        server_id == "server_1"
        and streaming.normalize_requested_source(
            source, epg_feed, row_number=row_number
        )
        == "panel"
    )
    action = "REVIEW" if legacy_server1_panel else original_action
    # Version 1 uses one simple publication rule everywhere: a row awaiting
    # review is disabled.  This also keeps historical migration rows out of
    # public channel metadata and personalized groups until the user verifies
    # the exact mapping and explicitly enables the row last.
    if action == "REVIEW":
        enabled = False
    reason = streaming.clean_text(raw.get("reason", ""), 500)
    if legacy_server1_panel:
        notes = append_note(notes, f"Legacy reason: {reason}")
        notes = append_note(
            notes,
            "Quarantined during the Version 1 migration; replace with a reviewed exact "
            "EPGShare ALL ID before changing action from REVIEW",
        )
        reason = "Legacy Server 1 native ID requires reviewed EPGShare replacement"

    canonical_name = (
        streaming.clean_text(raw.get("canonical_name", ""), 300) or channel_name
    )
    result = {column: "" for column in streaming.SHEET_COLUMNS}
    result.update(
        {
            "server_id": server_id,
            "server_label": streaming.clean_text(raw.get("server_label", ""), 80)
            or server_id.replace("_", " ").title(),
            "region_code": metadata.region,
            "genre": metadata.genre,
            "primary_language": metadata.primary_language,
            "stream_id": stream_id,
            "enabled": "TRUE" if enabled else "FALSE",
            "channel_name": channel_name,
            "canonical_name": canonical_name,
            "category_id": streaming.clean_text(raw.get("category_id", ""), 120),
            "category_name": streaming.clean_text(
                raw.get("category_name", ""), 200
            ),
            "channel_number": streaming.clean_text(
                raw.get("channel_number", ""), 40
            ),
            "country_codes": pipe(metadata.countries),
            "language_codes": pipe(metadata.languages),
            "subgenres": pipe(metadata.subgenres),
            "sport_codes": pipe(metadata.sports),
            "religion_codes": pipe(metadata.religions),
            "audience_codes": pipe(metadata.audiences),
            "content_rating": metadata.content_rating,
            "channel_role": metadata.channel_role,
            "tags": pipe(metadata.tags),
            "sort_priority": str(sort_priority),
            "action": action,
            # Preserve panel/dummy distinctions while canonicalizing the old
            # EPGShare spelling for a coherent Version 1 Sheet.
            "source": output_source,
            "epg_feed": epg_feed,
            "epg_id": streaming.clean_identifier(raw.get("epg_id", ""), 300),
            "logo_url": streaming.valid_http_url(raw.get("logo_url", "")),
            "metadata_status": metadata.status,
            "metadata_source": metadata.source,
            "metadata_confidence": f"{metadata.confidence:.3f}".rstrip("0").rstrip("."),
            "metadata_locked": "TRUE" if metadata.locked else "FALSE",
            "reason": reason,
            "notes": notes,
        }
    )
    return result


def server_sort_key(server_id: str) -> tuple[int, object]:
    match = streaming.SERVER_RE.fullmatch(server_id)
    if match:
        return (0, int(match.group(1)))
    return (1, server_id.casefold())


def seed_sort_key(row: Mapping[str, str]) -> tuple[object, ...]:
    return (
        server_sort_key(row["server_id"]),
        row["region_code"].casefold(),
        row["genre"].casefold(),
        row["primary_language"].casefold(),
        row["canonical_name"].casefold(),
        streaming.stream_sort_key(row["stream_id"]),
    )


def collect_seed_rows(
    mapping_dir: Path, selected_servers: Sequence[str]
) -> tuple[list[dict[str, str]], dict[str, int]]:
    wanted = {streaming.normalize_server_id(value) for value in selected_servers}
    rows: list[dict[str, str]] = []
    seen: dict[tuple[str, str], tuple[Path, int]] = {}
    counts: Counter[str] = Counter()

    for server_id in sorted(wanted, key=server_sort_key):
        path = Path(mapping_dir) / f"{server_id}_final_mapping.csv"
        if not path.is_file():
            raise SeedExportError(f"Mapping file does not exist: {path}")
        _headers, raw_rows = read_csv_rows(path)
        for offset, raw in enumerate(raw_rows, start=2):
            result = normalized_seed_row(
                raw,
                path=path,
                row_number=offset,
                selected_servers=wanted,
            )
            if result is None:
                continue
            key = (result["server_id"], result["stream_id"])
            if key in seen:
                previous_path, previous_line = seen[key]
                raise SeedExportError(
                    f"Duplicate ({key[0]}, {key[1]}) at {previous_path}:"
                    f"{previous_line} and {path}:{offset}."
                )
            seen[key] = (path, offset)
            rows.append(result)
            counts[result["server_id"]] += 1

    missing = sorted(wanted - set(counts), key=server_sort_key)
    if missing:
        raise SeedExportError(
            "No mapping rows found for: " + ", ".join(missing)
        )
    if len(rows) > streaming.MAX_MAPPING_ROWS:
        raise SeedExportError(
            f"Merged seed exceeds the production {streaming.MAX_MAPPING_ROWS:,}-row limit."
        )
    rows.sort(key=seed_sort_key)
    return rows, dict(counts)


def write_seed(handle: TextIO, rows: Iterable[Mapping[str, str]]) -> None:
    writer = csv.DictWriter(
        handle,
        fieldnames=list(streaming.SHEET_COLUMNS),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(
        {
            key: streaming.spreadsheet_safe_cell(value)
            for key, value in row.items()
        }
        for row in rows
    )


def write_seed_atomically(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            write_seed(handle, rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Merge mappings/server_X_final_mapping.csv into a normalized, "
            "Google-Sheets-ready Version 1 CSV."
        )
    )
    parser.add_argument(
        "--mapping-dir",
        type=Path,
        default=REPO_ROOT / "mappings",
        help="Directory containing server_X_final_mapping.csv files.",
    )
    parser.add_argument(
        "--servers",
        nargs="+",
        default=list(streaming.DEFAULT_SERVERS),
        help="Servers to merge (default: server_1 server_2 server_3).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write CSV to stdout instead of --output.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        selected = [streaming.normalize_server_id(value) for value in args.servers]
        if not args.stdout:
            destination = args.output.resolve()
            source_paths = {
                (args.mapping_dir.resolve() / f"{server_id}_final_mapping.csv").resolve()
                for server_id in selected
            }
            if destination in source_paths:
                raise SeedExportError(
                    "Refusing to overwrite an input mapping; choose a separate "
                    "--output path."
                )
        rows, counts = collect_seed_rows(args.mapping_dir.resolve(), selected)
        if args.stdout:
            write_seed(sys.stdout, rows)
        else:
            write_seed_atomically(args.output, rows)

        legacy_server1_panel = [
            row
            for row in rows
            if row["server_id"] == "server_1"
            and streaming.normalize_requested_source(
                row["source"], row["epg_feed"], row_number=0
            )
            == "panel"
        ]
        destination = "stdout" if args.stdout else str(args.output.resolve())
        print(
            "Exported "
            f"{len(rows):,} rows to {destination}; "
            + ", ".join(
                f"{server_id}={counts.get(server_id, 0):,}"
                for server_id in sorted(counts, key=server_sort_key)
            )
            + ".",
            file=sys.stderr,
        )
        if legacy_server1_panel:
            print(
                "Review required: the seed quarantined "
                f"{len(legacy_server1_panel):,} Server 1 rows reference "
                f"{len({row['epg_id'] for row in legacy_server1_panel}):,} "
                "unique legacy native IDs. They are disabled with "
                "action=REVIEW until remapped; "
                "Server 1 never downloads the native panel guide.",
                file=sys.stderr,
            )
        return 0
    except (
        SeedExportError,
        streaming.BuildError,
        UnicodeError,
        csv.Error,
        OSError,
    ) as exc:
        print(f"Seed export failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
