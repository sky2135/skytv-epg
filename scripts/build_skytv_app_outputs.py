#!/usr/bin/env python3
"""Build compact SKY TV app EPG files for all approved server mappings."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skytv_app_export import (  # noqa: E402
    DEFAULT_FUTURE_HOURS,
    DEFAULT_PAST_HOURS,
    load_dummy_feeds,
    load_preserved_extensions_from_directory,
    load_preserved_extensions_from_url,
    write_app_epg_files,
)

SERVER_IDS = ("server_1", "server_2", "server_3")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--servers",
        nargs="+",
        choices=SERVER_IDS,
        default=list(SERVER_IDS),
        help="Servers to export (default: all three).",
    )
    parser.add_argument(
        "--mapping-dir", type=Path, default=REPO_ROOT / "mappings"
    )
    parser.add_argument(
        "--work-dir", type=Path, default=REPO_ROOT / ".build" / "work"
    )
    parser.add_argument(
        "--public-dir", type=Path, default=REPO_ROOT / "public"
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=REPO_ROOT / "config" / "epg_sources.json",
    )
    parser.add_argument("--past-hours", type=int, default=DEFAULT_PAST_HOURS)
    parser.add_argument("--future-hours", type=int, default=DEFAULT_FUTURE_HOURS)
    parser.add_argument(
        "--preserve-dir",
        type=Path,
        default=None,
        help="Optional existing app EPG directory whose non-core fields are retained.",
    )
    parser.add_argument(
        "--preserve-base-url",
        default="",
        help=(
            "Optional URL containing existing server_X_epg.json.gz and manifest files. "
            "Unknown top-level fields, such as an existing logo layer, are retained."
        ),
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8-sig")))


def main() -> int:
    args = parse_args()
    mapping_dir = args.mapping_dir.resolve()
    work_dir = args.work_dir.resolve()
    public_dir = args.public_dir.resolve()
    app_dir = public_dir / "EPG"
    app_dir.mkdir(parents=True, exist_ok=True)
    dummy_feeds = load_dummy_feeds(args.source_config.resolve())

    builds: list[dict[str, Any]] = []
    for server_id in args.servers:
        mapping_path = mapping_dir / f"{server_id}_final_mapping.csv"
        output_dir = work_dir / server_id / "output"
        xmltv_path = output_dir / f"{server_id}_tivimate.xml.gz"
        tivimate_manifest_path = output_dir / f"{server_id}_tivimate_manifest.json"
        missing = [
            path
            for path in (mapping_path, xmltv_path, tivimate_manifest_path)
            if not path.is_file()
        ]
        if missing:
            joined = "\n  - ".join(str(path) for path in missing)
            raise FileNotFoundError(
                f"Required input files for {server_id} are missing:\n  - {joined}"
            )

        mapping = pd.read_csv(
            mapping_path, dtype=str, keep_default_na=False, encoding="utf-8-sig"
        )
        source_manifest = read_json(tivimate_manifest_path)

        payload_extensions: dict[str, Any] = {}
        manifest_extensions: dict[str, Any] = {}
        preservation: dict[str, Any] = {
            "source": "",
            "payloadFound": False,
            "manifestFound": False,
            "error": "",
        }
        if args.preserve_dir is not None:
            payload_extensions, manifest_extensions, preservation = (
                load_preserved_extensions_from_directory(
                    args.preserve_dir.resolve(), server_id
                )
            )
        elif str(args.preserve_base_url).strip():
            payload_extensions, manifest_extensions, preservation = (
                load_preserved_extensions_from_url(
                    str(args.preserve_base_url), server_id
                )
            )

        result = write_app_epg_files(
            mapping=mapping,
            server_id=server_id,
            xmltv_path=xmltv_path,
            output_dir=app_dir,
            source_manifest=source_manifest,
            dummy_feeds=dummy_feeds,
            past_hours=args.past_hours,
            future_hours=args.future_hours,
            preserved_payload_extensions=payload_extensions,
            preserved_manifest_extensions=manifest_extensions,
            preservation_details=preservation,
        )

        report_dir = public_dir / "reports" / server_id
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{server_id}_app_export_validation.json"
        report_path.write_text(
            json.dumps(result["validation"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest = result["manifest"]
        builds.append(
            {
                "serverId": server_id,
                "dataFile": f"EPG/{Path(result['dataPath']).name}",
                "manifestFile": f"EPG/{Path(result['manifestPath']).name}",
                "generatedAtIso": manifest.get("generatedAtIso"),
                "mappedStreams": manifest.get("mappedStreams"),
                "streamsWithProgrammes": manifest.get("streamsWithProgrammes"),
                "coveragePercent": manifest.get("coveragePercent"),
                "programmeRows": manifest.get("programmeRows"),
            }
        )
        print(
            f"{server_id}: {manifest['mappedStreams']:,} streams, "
            f"{manifest['streamsWithProgrammes']:,} with programmes, "
            f"{manifest['coveragePercent']:.2f}% coverage -> EPG/",
            flush=True,
        )
        if preservation.get("error"):
            print(
                f"  Preservation warning: {preservation['error']}",
                flush=True,
            )

    index = {
        "schemaVersion": 1,
        "generatedAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mappingPolicy": "APPROVED_FINAL_MAPPING_CSV_ONLY_NO_AUTOMATIC_REMATCH",
        "builds": builds,
    }
    (app_dir / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print("Compact SKY TV app EPG publication staging complete.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
