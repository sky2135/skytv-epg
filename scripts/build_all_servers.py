#!/usr/bin/env python3
"""Build all approved XMLTV EPG files with Builder v7.1.

Smart Rules v8 is used interactively to create/review mappings.  This production
runner deliberately performs no automatic rematching: it consumes only approved
mapping CSV files and uses the unchanged build_tivimate_package function.
Independent source files are downloaded once per workflow run and reused by all
three server builds.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skytv_epg_engine as engine  # noqa: E402
import skytv_epg_icons as icon_layer  # noqa: E402

SERVER_IDS = ("server_1", "server_2", "server_3")
PUBLIC_REPORT_SUFFIXES = (
    "_tivimate_manifest.json",
    "_validation.json",
    "_refresh_plan.json",
    "_next_refresh_utc.txt",
)


@dataclass(frozen=True)
class ServerInput:
    server_id: str
    mapping: pd.DataFrame
    prepared: pd.DataFrame
    channel_report: pd.DataFrame | None
    mapping_path: Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def load_server_inputs(mapping_dir: Path, server_ids: Iterable[str]) -> list[ServerInput]:
    result: list[ServerInput] = []
    missing: list[str] = []
    for server_id in server_ids:
        mapping_path = mapping_dir / f"{server_id}_final_mapping.csv"
        if not mapping_path.is_file():
            missing.append(str(mapping_path.relative_to(REPO_ROOT)))
            continue
        mapping = read_csv_text(mapping_path)
        prepared = engine.prepare_mapping(mapping, server_id)

        report_path = mapping_dir / f"{server_id}_channel_report.csv"
        channel_report = read_csv_text(report_path) if report_path.is_file() else None
        result.append(
            ServerInput(
                server_id=server_id,
                mapping=mapping,
                prepared=prepared,
                channel_report=channel_report,
                mapping_path=mapping_path,
            )
        )

    if missing:
        joined = "\n  - ".join(missing)
        raise FileNotFoundError(
            "Approved mapping files are required before an automatic build:\n"
            f"  - {joined}\n"
            "Run Stage 2 once for each server and copy each server_X_final_mapping.csv "
            "from its output ZIP into mappings/."
        )
    return result


def load_source_registry(config_path: Path) -> None:
    if not config_path.is_file():
        raise FileNotFoundError(f"EPG source configuration is missing: {config_path}")
    engine.reset_epgshare_sources()
    engine.load_epg_source_config(config_path, overwrite=True)
    print(f"Loaded {len(engine.EPGSHARE_FEEDS):,} configured EPG sources.", flush=True)


def secret_name(server_id: str, field: str) -> str:
    return f"{server_id.upper()}_{field}"


def panel_credentials(server_id: str) -> tuple[str, str, str]:
    base_name = secret_name(server_id, "BASE_URL")
    user_name = secret_name(server_id, "USERNAME")
    password_name = secret_name(server_id, "PASSWORD")
    base_url = os.environ.get(base_name, "").strip()
    username = os.environ.get(user_name, "").strip()
    password = os.environ.get(password_name, "")
    missing = [
        name
        for name, value in (
            (base_name, base_url),
            (user_name, username),
            (password_name, password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"{server_id} has PANEL mapping rows, but these GitHub secrets are missing: "
            + ", ".join(missing)
        )
    return engine.safe_base_url(base_url), username, password


def download_epg_source(feed: str, destination: Path) -> Path:
    session = engine.make_session()
    try:
        engine.download_streamed(
            session,
            str(engine.EPGSHARE_FEEDS[feed]["xml_url"]),
            destination,
        )
        engine.validate_xmlish(destination)
        return destination
    finally:
        session.close()


def download_panel_source(
    server_id: str,
    base_url: str,
    username: str,
    password: str,
    destination: Path,
) -> Path:
    session = engine.make_session()
    try:
        engine.download_streamed(
            session,
            f"{base_url}/xmltv.php",
            destination,
            params={"username": username, "password": password},
        )
        engine.validate_xmlish(destination)
        return destination
    finally:
        session.close()


def prefetch_sources(
    server_inputs: list[ServerInput],
    cache_dir: Path,
    max_workers: int,
) -> tuple[dict[str, Path], dict[str, Path]]:
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    epg_dir = cache_dir / "epgshare"
    panel_dir = cache_dir / "panel"
    epg_dir.mkdir(parents=True, exist_ok=True)
    panel_dir.mkdir(parents=True, exist_ok=True)

    epg_feeds = sorted(
        {
            str(feed)
            for item in server_inputs
            for feed in item.prepared["feed_key"].astype(str)
            if str(feed) != "PANEL"
        }
    )
    panel_servers = sorted(
        item.server_id
        for item in server_inputs
        if item.prepared["feed_key"].eq("PANEL").any()
    )

    epg_paths = {feed: epg_dir / f"{feed}.xml.gz" for feed in epg_feeds}
    panel_paths = {server_id: panel_dir / f"{server_id}.xmltv" for server_id in panel_servers}

    jobs: list[tuple[str, concurrent.futures.Future[Path]]] = []
    workers = max(1, min(int(max_workers), len(epg_feeds) + len(panel_servers) or 1))
    print(
        f"Downloading {len(epg_feeds)} distinct EPGShare file(s) and "
        f"{len(panel_servers)} panel file(s) with up to {workers} workers.",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="epg-source",
    ) as pool:
        for feed, path in epg_paths.items():
            jobs.append((f"EPGShare {feed}", pool.submit(download_epg_source, feed, path)))
        for server_id, path in panel_paths.items():
            base_url, username, password = panel_credentials(server_id)
            jobs.append(
                (
                    f"panel {server_id}",
                    pool.submit(
                        download_panel_source,
                        server_id,
                        base_url,
                        username,
                        password,
                        path,
                    ),
                )
            )

        # Resolve in stable submission order. This keeps logs deterministic while
        # the network work itself happens concurrently.
        for label, future in jobs:
            downloaded = future.result()
            print(f"Ready: {label} ({downloaded.stat().st_size:,} bytes)", flush=True)

    return epg_paths, panel_paths


def install_shared_download_cache(epg_paths: dict[str, Path]) -> Any:
    """Make the original builder copy prefetched EPGShare files.

    The original build algorithm remains untouched. Only its network transport is
    replaced during this process, and only for exact configured source URLs.
    """
    by_url = {
        str(engine.EPGSHARE_FEEDS[feed]["xml_url"]): source
        for feed, source in epg_paths.items()
    }
    original = engine.download_streamed

    def cached_download(
        session: Any,
        url: str,
        destination: Path,
        *,
        params: dict[str, str] | None = None,
        attempts: int = 3,
    ) -> Path:
        source = by_url.get(str(url)) if not params else None
        if source is None:
            return original(
                session,
                url,
                destination,
                params=params,
                attempts=attempts,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    engine.download_streamed = cached_download
    return original


def derive_public_base_url(explicit: str = "") -> str:
    """Return the Pages base URL used for locally hosted logos."""
    configured = str(explicit or os.environ.get("EPG_PUBLIC_BASE_URL", "")).strip().rstrip("/")
    if configured:
        return configured
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository or "/" not in repository:
        return ""
    owner, repo = repository.split("/", 1)
    if repo.casefold() == f"{owner.casefold()}.github.io":
        return f"https://{owner}.github.io"
    return f"https://{owner}.github.io/{repo}"


def source_base_urls_for_icons(
    epg_paths: dict[str, Path],
    panel_paths: dict[str, Path],
    server_id: str,
) -> dict[str, str]:
    urls = {
        feed: str(engine.EPGSHARE_FEEDS.get(feed, {}).get("xml_url", ""))
        for feed in epg_paths
    }
    if server_id in panel_paths:
        try:
            base_url, _username, _password = panel_credentials(server_id)
            urls["PANEL"] = base_url + "/"
        except Exception:
            urls["PANEL"] = ""
    return urls


def prefetch_source_icon_catalogs(
    inputs: list[ServerInput],
    epg_paths: dict[str, Path],
    panel_paths: dict[str, Path],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Parse each downloaded source at most once for all mapped icon IDs."""
    wanted_shared: dict[str, set[str]] = {}
    for item in inputs:
        for feed, group in item.prepared.groupby("feed_key", sort=True):
            feed = str(feed)
            if feed == "PANEL":
                continue
            wanted_shared.setdefault(feed, set()).update(group["epg_id"].astype(str))
    shared = icon_layer.extract_icons_by_feed(
        wanted_shared,
        epg_paths,
        {
            feed: str(engine.EPGSHARE_FEEDS.get(feed, {}).get("xml_url", ""))
            for feed in epg_paths
        },
    )

    panels: dict[str, dict[str, str]] = {}
    for item in inputs:
        if item.server_id not in panel_paths:
            continue
        wanted = set(
            item.prepared.loc[item.prepared["feed_key"].eq("PANEL"), "epg_id"].astype(str)
        )
        base_url, _username, _password = panel_credentials(item.server_id)
        panels[item.server_id] = icon_layer.extract_source_icons(
            panel_paths[item.server_id], wanted, base_url=base_url + "/"
        )
    print(
        "Source icon catalog: "
        f"{sum(len(value) for value in shared.values()):,} EPGShare icon(s), "
        f"{sum(len(value) for value in panels.values()):,} panel icon(s).",
        flush=True,
    )
    return shared, panels


def enrich_server_icons(
    *,
    item: ServerInput,
    output_dir: Path,
    manifest: dict[str, Any],
    shared_source_icons: dict[str, dict[str, str]],
    panel_source_icons: dict[str, dict[str, str]],
    icon_overrides: icon_layer.IconOverrides,
) -> dict[str, Any]:
    """Add exact XMLTV icon metadata without changing any mapping decision."""
    source_icons = dict(shared_source_icons)
    if item.server_id in panel_source_icons:
        source_icons["PANEL"] = panel_source_icons[item.server_id]
    assignments, report_rows = icon_layer.resolve_icon_assignments(
        item.prepared,
        server_id=item.server_id,
        source_icons_by_feed=source_icons,
        overrides=icon_overrides,
    )
    data_file = output_dir / str(manifest["dataFile"])
    icon_stats = icon_layer.inject_icons_into_xmltv(data_file, assignments)
    validation = engine.validate_tivimate_xmltv(data_file)
    validation.update(icon_stats)
    coverage = round(
        icon_stats["channels_with_icons"] * 100.0 / max(1, icon_stats["xmltv_channels"]),
        2,
    )
    manifest.update({
        "dataSha256": sha256_file(data_file),
        "compressedBytes": data_file.stat().st_size,
        "channelsWithIcons": icon_stats["channels_with_icons"],
        "channelsWithoutIcons": icon_stats["channels_without_icons"],
        "iconCoveragePercent": coverage,
        "iconAssignments": len(assignments),
        "iconPolicy": "mapping_exact_then_config_exact_then_source_xmltv_exact_no_fuzzy",
        "validation": validation,
    })
    manifest_path = output_dir / f"{item.server_id}_tivimate_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / f"{item.server_id}_validation.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    pd.DataFrame(
        report_rows,
        columns=[
            "server_id", "channel_name", "epg_id", "feed",
            "icon_url", "icon_source", "matched_by",
        ],
    ).to_csv(
        output_dir / f"{item.server_id}_icon_report.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(
        f"  Icons: {icon_stats['channels_with_icons']:,}/{icon_stats['xmltv_channels']:,} "
        f"XMLTV channel entries ({coverage:.2f}%).",
        flush=True,
    )
    return manifest


def clear_public_dir(public_dir: Path) -> None:
    if public_dir.exists():
        shutil.rmtree(public_dir)
    (public_dir / "epg").mkdir(parents=True, exist_ok=True)
    (public_dir / "reports").mkdir(parents=True, exist_ok=True)
    (public_dir / ".nojekyll").write_text("", encoding="utf-8")


def publish_server_output(
    server_id: str,
    output_dir: Path,
    public_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    data_name = str(manifest["dataFile"])
    source_data = output_dir / data_name
    if not source_data.is_file():
        raise FileNotFoundError(f"Builder did not create expected file: {source_data}")

    public_data = public_dir / "epg" / data_name
    shutil.copy2(source_data, public_data)

    report_dir = public_dir / "reports" / server_id
    report_dir.mkdir(parents=True, exist_ok=True)
    for suffix in PUBLIC_REPORT_SUFFIXES:
        candidate = output_dir / f"{server_id}{suffix}"
        if candidate.is_file():
            shutil.copy2(candidate, report_dir / candidate.name)

    return {
        "serverId": server_id,
        "file": f"epg/{data_name}",
        "sha256": sha256_file(public_data),
        "compressedBytes": public_data.stat().st_size,
        "generatedAtIso": manifest.get("generatedAtIso"),
        "mappedStreams": manifest.get("mappedStreams"),
        "mappedStreamsWithProgrammes": manifest.get("mappedStreamsWithProgrammes"),
        "programmeRows": manifest.get("programmeRows"),
        "coveragePercent": manifest.get("mappingProgrammeCoveragePercent"),
        "recommendedRefreshAt": (manifest.get("refreshPlan") or {}).get(
            "recommendedRefreshAt"
        ),
        "builderVersion": manifest.get("builderVersion"),
        "builderBuildId": manifest.get("builderBuildId"),
        "channelsWithIcons": manifest.get("channelsWithIcons", 0),
        "iconCoveragePercent": manifest.get("iconCoveragePercent", 0),
    }


def write_public_index(public_dir: Path, builds: list[dict[str, Any]]) -> None:
    payload = {
        "schemaVersion": 1,
        "generatedAtUtc": utc_now_iso(),
        "targetApp": "TVMeta / TiviMate",
        "matcherPolicy": "APPROVED_MAPPINGS_ONLY_NO_AUTOMATIC_REMATCH",
        "builds": builds,
    }
    (public_dir / "epg" / "index.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (public_dir / "health.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "generatedAtUtc": payload["generatedAtUtc"],
                "servers": len(builds),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    rows = []
    for item in builds:
        label = html.escape(str(item["serverId"]))
        raw_href = str(item["file"])
        href = html.escape(raw_href, quote=True)
        filename = html.escape(Path(raw_href).name)
        generated = html.escape(str(item.get("generatedAtIso") or ""))
        coverage_value = item.get("coveragePercent")
        coverage = html.escape("" if coverage_value is None else str(coverage_value))
        rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td><a href=\"{href}\">{filename}</a></td>"
            f"<td>{generated}</td>"
            f"<td>{coverage}%</td>"
            f"<td>{html.escape(str(item.get('iconCoveragePercent', 0)))}%</td>"
            "</tr>"
        )
    document = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SKY TV XMLTV EPG</title>
  <style>
    body { font: 16px/1.5 system-ui, sans-serif; max-width: 980px; margin: 3rem auto; padding: 0 1rem; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ccc; padding: .65rem; text-align: left; }
    th { background: #f3f3f3; }
    code { overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <h1>SKY TV XMLTV EPG files</h1>
  <p>Generated from reviewed mappings. Automatic fuzzy rematching is disabled in scheduled builds.</p>
  <table>
    <thead><tr><th>Server</th><th>EPG download</th><th>Generated UTC</th><th>Programme coverage</th><th>Icon coverage</th></tr></thead>
    <tbody>
""" + "\n".join(rows) + """
    </tbody>
  </table>
  <p>Machine-readable status: <a href="epg/index.json"><code>epg/index.json</code></a></p>
</body>
</html>
"""
    (public_dir / "index.html").write_text(document, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--servers",
        nargs="+",
        choices=SERVER_IDS,
        default=list(SERVER_IDS),
        help="Servers to build (default: all three).",
    )
    parser.add_argument(
        "--mapping-dir",
        type=Path,
        default=REPO_ROOT / "mappings",
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=REPO_ROOT / "config" / "epg_sources.json",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=REPO_ROOT / ".build" / "work",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPO_ROOT / ".build" / "cache",
    )
    parser.add_argument(
        "--public-dir",
        type=Path,
        default=REPO_ROOT / "public",
    )
    parser.add_argument(
        "--past-days",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--refresh-safety-hours",
        type=int,
        default=12,
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=int(os.environ.get("EPG_DOWNLOAD_WORKERS", "4")),
    )
    parser.add_argument(
        "--icon-config",
        type=Path,
        default=REPO_ROOT / "config" / "channel_icons.csv",
        help="Exact optional icon overrides; source XMLTV icons are used automatically.",
    )
    parser.add_argument(
        "--logo-assets-dir",
        type=Path,
        default=REPO_ROOT / "assets" / "logos",
        help="Locally hosted logo files copied to GitHub Pages.",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("EPG_PUBLIC_BASE_URL", ""),
        help="Pages/custom-domain base URL for local logo files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_source_registry(args.source_config.resolve())
    inputs = load_server_inputs(args.mapping_dir.resolve(), args.servers)

    for item in inputs:
        print(
            f"{item.server_id}: {len(item.prepared):,} approved mapping rows "
            f"from {item.mapping_path.name}",
            flush=True,
        )

    epg_paths, panel_paths = prefetch_sources(
        inputs,
        args.cache_dir.resolve(),
        args.download_workers,
    )
    original_download = install_shared_download_cache(epg_paths)
    public_base_url = derive_public_base_url(args.public_base_url)
    icon_overrides = icon_layer.load_icon_overrides(
        args.icon_config.resolve(),
        public_base_url=public_base_url,
    )
    print(
        f"Icon overrides: {icon_overrides.rows_loaded:,} loaded, "
        f"{icon_overrides.rows_rejected:,} rejected. "
        f"Local-logo base URL: {public_base_url or 'not configured'}",
        flush=True,
    )
    shared_source_icons, panel_source_icons = prefetch_source_icon_catalogs(
        inputs, epg_paths, panel_paths
    )
    clear_public_dir(args.public_dir.resolve())
    asset_stats = icon_layer.stage_logo_assets(
        args.logo_assets_dir.resolve(),
        args.public_dir.resolve() / "logos",
    )
    print(
        f"Staged {asset_stats['files']:,} local logo/attribution file(s) "
        f"({asset_stats['bytes']:,} bytes).",
        flush=True,
    )

    if args.work_dir.exists():
        shutil.rmtree(args.work_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    published: list[dict[str, Any]] = []
    try:
        for item in inputs:
            print(f"\nBuilding {item.server_id} with Builder v7.1 + XMLTV icon layer...", flush=True)
            server_work = args.work_dir / item.server_id
            _zip_path, manifest = engine.build_tivimate_package(
                mapping=item.mapping,
                server_id=item.server_id,
                work_dir=server_work,
                panel_xmltv_path=panel_paths.get(item.server_id, ""),
                channel_report=item.channel_report,
                past_days=args.past_days,
                refresh_safety_hours=args.refresh_safety_hours,
            )
            manifest = enrich_server_icons(
                item=item,
                output_dir=server_work / "output",
                manifest=manifest,
                shared_source_icons=shared_source_icons,
                panel_source_icons=panel_source_icons,
                icon_overrides=icon_overrides,
            )
            published.append(
                publish_server_output(
                    item.server_id,
                    server_work / "output",
                    args.public_dir.resolve(),
                    manifest,
                )
            )
    finally:
        engine.download_streamed = original_download

    write_public_index(args.public_dir.resolve(), published)
    print("\nBuild and publication staging complete.", flush=True)
    for item in published:
        print(
            f"  {item['serverId']}: {item['file']} "
            f"({item['compressedBytes']:,} bytes, SHA-256 {item['sha256'][:12]}...)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        if os.environ.get("SKYTV_DEBUG", "").strip() == "1":
            raise
        raise SystemExit(1) from None
