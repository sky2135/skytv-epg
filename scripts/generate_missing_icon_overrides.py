#!/usr/bin/env python3
"""Create exact, production-accurate fallback icon overrides.

Only enabled mapping rows that would otherwise have no usable icon receive a
generated category fallback.  The production builder can use source XMLTV
icons for EPGShare rows, but it intentionally does not publish native panel
icons and synthetic/dummy guides have no source icon.  Those two cases
therefore need local overrides even when the same ID happens to exist in the
EPGShare catalog.

The exact rows contain private provider identities. They are written only to a
required ephemeral output path for the current workflow run. The small public
base config is read but never overwritten. Public subject-only catalogs select
reviewed portraits with no fuzzy person matching. A named person without a
reviewed portrait receives a reusable transparent music or movie symbol.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urljoin, urlparse

from lxml import etree

from named_person_subjects import (
    canonical_name,
    classify_person_subject,
    normalized_key,
)
from icon_variant_rules import (
    allocate_movie_variants,
    icon_variant_scope,
    select_icon_variant,
    series_pattern_key,
)


CONFIG_COLUMNS = (
    "enabled",
    "server_id",
    "stream_id",
    "epg_id",
    "channel_name",
    "icon_url",
    "local_file",
    "priority",
    "notes",
)
SUPPORTED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})
CATEGORY_NAMES = frozenset(
    {
        "adult",
        "documentary",
        "education",
        "entertainment",
        "events",
        "general",
        "kids",
        "lifestyle",
        "movies",
        "music",
        "news",
        "radio",
        "religion",
        "shopping",
        "sports",
    }
)
GENERATED_NOTE_PREFIX = "AUTO-GENERATED category fallback:"
NAMED_FALLBACK_NOTE_PREFIX = "NAMED_FALLBACK:"
NAMED_PORTRAIT_NOTE_PREFIX = "NAMED_PORTRAIT:"
NAMED_SYMBOL_NOTE_PREFIX = "NAMED_SYMBOL:"
EPHEMERAL_NOTE_PREFIXES = (
    GENERATED_NOTE_PREFIX,
    NAMED_FALLBACK_NOTE_PREFIX,
    NAMED_PORTRAIT_NOTE_PREFIX,
    NAMED_SYMBOL_NOTE_PREFIX,
)
TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on", "enabled"})
FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", "disabled"})


def normalized(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def identifier(value: object) -> str:
    """Normalize XML attribute whitespace without collapsing normal spaces."""
    return (
        str(value or "")
        .replace("\t", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def folded_identifier(value: object) -> str:
    return identifier(value).casefold()


def is_enabled(value: object) -> bool:
    text = normalized(value)
    if not text or text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid enabled value: {value!r}")


def safe_http_url(value: object, *, base_url: str = "") -> str:
    """Match the production builder's public icon URL safety boundary."""
    text = " ".join(str(value or "").split())[:2_000]
    if not text:
        return ""
    if base_url:
        text = urljoin(base_url, text)
    parsed = urlparse(text)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return text


def requested_source(row: Mapping[str, str]) -> str:
    source = normalized(row.get("source", ""))
    aliases = {"epgshare": "epgshare01", "epgshare01": "epgshare01"}
    if source:
        return aliases.get(source, source)
    feed = normalized(row.get("epg_feed", ""))
    if feed in {"panel", "server xmltv.php"}:
        return "panel"
    if feed == "dummy_channels":
        return "dummy"
    return "epgshare01"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or ())
        return headers, [dict(row) for row in reader]


def usable_local_file(value: object, logo_root: Path) -> str:
    raw = str(value or "").strip()
    if not raw or "\\" in raw:
        return ""
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return ""
    if relative.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        return ""
    source = (logo_root / relative).resolve()
    root = logo_root.resolve()
    return raw if root in source.parents and source.is_file() else ""


def local_override_is_usable(row: Mapping[str, str], logo_root: Path) -> bool:
    if safe_http_url(row.get("icon_url", "")):
        return True
    return bool(usable_local_file(row.get("local_file", ""), logo_root))


def portrait_asset_index(
    *,
    asset_catalog: Path,
    logo_root: Path,
) -> dict[str, dict[str, str]]:
    """Load only approved, exact-subject portrait assets.

    Name-and-text fallback cards were retired in favor of reusable transparent
    movie and music symbols.  A named channel therefore receives a real,
    reviewed portrait or a content symbol; it never receives an invented
    likeness or an opaque name card.
    """
    _real_headers, real_rows = read_csv(asset_catalog)
    portraits: dict[str, dict[str, str]] = {}

    for row in real_rows:
        if normalized(row.get("subject_type", "")) != "person":
            continue
        if normalized(row.get("asset_kind", "")) != "person_photo":
            continue
        if normalized(row.get("review_status", "")) != "approved":
            continue
        if normalized(row.get("license_id", "")) in {"", "unknown", "unrecorded"}:
            continue
        local_file = usable_local_file(row.get("local_file", ""), logo_root)
        subject_key = normalized_key(str(row.get("subject_name", "") or ""))
        if not local_file or not subject_key:
            continue
        item = dict(row)
        item["local_file"] = local_file
        previous = portraits.setdefault(subject_key, item)
        if previous.get("asset_id") != item.get("asset_id"):
            raise ValueError(
                "More than one approved portrait exists for exact subject key "
                f"{subject_key!r}."
            )

    return portraits


def override_matches(row: Mapping[str, str], override: Mapping[str, str]) -> bool:
    if not is_enabled(override.get("enabled", "")):
        return False
    scope = normalized(override.get("server_id", "")) or "*"
    if scope not in {"*", normalized(row.get("server_id", ""))}:
        return False
    stream_id = identifier(override.get("stream_id", ""))
    epg_id = folded_identifier(override.get("epg_id", ""))
    channel_name = folded_identifier(override.get("channel_name", ""))
    if not any((stream_id, epg_id, channel_name)):
        return False
    return (
        (not stream_id or stream_id == identifier(row.get("stream_id", "")))
        and (not epg_id or epg_id == folded_identifier(row.get("epg_id", "")))
        and (
            not channel_name
            or channel_name == folded_identifier(row.get("channel_name", ""))
        )
    )


def open_maybe_gzip(path: Path):
    with path.open("rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if compressed else path.open("rb")


def local_name(tag: object) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def extract_source_icons(
    path: Path, wanted_ids: Iterable[str], *, base_url: str = ""
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Read exact channel icons without retaining the large schedule in RAM.

    Some aggregate XMLTV catalogs interleave each upstream feed's channels and
    programmes, so later channel records can appear after the first programme.
    The full document must be streamed to avoid silently missing those icons.
    """
    wanted_folds = {
        folded_identifier(value) for value in wanted_ids if folded_identifier(value)
    }
    icons: dict[str, str] = {}
    variants_by_fold: dict[str, set[str]] = defaultdict(set)
    if not wanted_folds:
        return icons, variants_by_fold

    with open_maybe_gzip(path) as source:
        context = etree.iterparse(
            source,
            events=("end",),
            recover=False,
            huge_tree=True,
            load_dtd=False,
            no_network=True,
            resolve_entities=False,
            remove_comments=True,
            remove_pis=True,
        )
        try:
            for _event, element in context:
                name = local_name(element.tag)
                if name not in {"channel", "programme"}:
                    continue
                if name == "channel":
                    source_id = identifier(element.get("id") or "")
                    folded = folded_identifier(source_id)
                    if source_id and folded in wanted_folds:
                        variants_by_fold[folded].add(source_id)
                        for child in element:
                            if local_name(child.tag) != "icon":
                                continue
                            icon_url = safe_http_url(
                                child.get("src") or "", base_url=base_url
                            )
                            if icon_url:
                                icons.setdefault(source_id, icon_url)
                                break
                element.clear()
                parent = element.getparent()
                while parent is not None and element.getprevious() is not None:
                    del parent[0]
        finally:
            del context
    return icons, variants_by_fold


def usable_source_icon(
    epg_id: str,
    icons: Mapping[str, str],
    variants_by_fold: Mapping[str, set[str]],
) -> str:
    if epg_id in icons:
        return icons[epg_id]
    variants = variants_by_fold.get(folded_identifier(epg_id), set())
    if len(variants) != 1:
        return ""
    return icons.get(next(iter(variants)), "")


def category_for(row: Mapping[str, str]) -> str:
    if normalized(row.get("channel_role", "")) == "radio":
        return "radio"
    genre = normalized(row.get("genre", ""))
    return genre if genre in CATEGORY_NAMES else "general"


def generated_asset_for(
    row: Mapping[str, str],
    *,
    person_role: str = "",
    named_person: bool = False,
    extension: str = "png",
    movie_variant: str = "",
) -> tuple[str, str]:
    """Return ``(asset_name, local_file)`` for one generated fallback.

    Movie and music artwork comes from small transparent icon families.  The
    pattern selector strips display-quality labels and normalizes numbered
    series, so ``Movie 1`` and ``Movie 2`` share an image while a different
    channel-name pattern selects another stable variant.
    """
    category_name = row.get("category_name", "")
    channel_name = row.get("channel_name", "")
    role = normalized(person_role)
    category = category_for(row)
    if role == "singer":
        asset_name = select_icon_variant(
            "music",
            category_name,
            channel_name,
            named_singer=named_person,
        )
    elif role == "actor":
        asset_name = movie_variant or select_icon_variant(
            "movies", category_name, channel_name
        )
    elif category == "movies":
        asset_name = movie_variant or select_icon_variant(
            "movies", category_name, channel_name
        )
    elif category == "music":
        asset_name = select_icon_variant("music", category_name, channel_name)
    else:
        asset_name = category
    return asset_name, f"generated/category-{asset_name}-v3.{extension}"


def movie_scope(row: Mapping[str, str]) -> str:
    """Return an internal-only scope for adjacent provider name patterns."""
    return icon_variant_scope(
        row.get("server_id", ""), row.get("category_name", "")
    )


def movie_variant_key(row: Mapping[str, str]) -> tuple[str, str]:
    return (
        movie_scope(row),
        series_pattern_key(
            row.get("category_name", ""), row.get("channel_name", "")
        ),
    )


def stream_sort_key(value: str) -> tuple[int, int | str, str]:
    text = str(value or "").strip()
    if text.isdigit():
        return (0, int(text), text)
    return (1, text.casefold(), text)


def write_config(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CONFIG_COLUMNS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CONFIG_COLUMNS})
    temporary.replace(path)


def generate(
    *,
    mapping_csv: Path,
    source_xmltv: Path,
    base_config: Path,
    output_config: Path,
    logo_root: Path,
    asset_catalog: Path,
    source_base_url: str = "",
    category_extension: str = "png",
) -> dict[str, object]:
    if base_config.resolve() == output_config.resolve():
        raise ValueError(
            "The private generated output must not overwrite the checked-in "
            "base icon config."
        )
    repository_root = Path(__file__).resolve().parents[1]
    try:
        repository_relative = output_config.resolve().relative_to(repository_root)
    except ValueError:
        repository_relative = None
    if repository_relative is not None and (
        not repository_relative.parts or repository_relative.parts[0] != ".build"
    ):
        raise ValueError(
            "A private generated output inside the repository must be under "
            "the ignored .build directory."
        )
    mapping_headers, mapping_rows = read_csv(mapping_csv)
    required = {"server_id", "stream_id", "enabled", "channel_name", "epg_id"}
    missing = sorted(required - set(mapping_headers))
    if missing:
        raise ValueError("Mapping CSV is missing: " + ", ".join(missing))

    enabled_rows = [row for row in mapping_rows if is_enabled(row.get("enabled", ""))]
    config_headers, base_rows = read_csv(base_config)
    if config_headers and tuple(config_headers) != CONFIG_COLUMNS:
        missing_config = sorted(set(CONFIG_COLUMNS) - set(config_headers))
        if missing_config:
            raise ValueError("Icon config is missing: " + ", ".join(missing_config))
    if any(
        str(row.get("notes", "") or "").startswith(EPHEMERAL_NOTE_PREFIXES)
        for row in base_rows
    ):
        raise ValueError(
            "The checked-in base icon config contains private generated rows."
        )
    portraits = portrait_asset_index(
        asset_catalog=asset_catalog,
        logo_root=logo_root,
    )

    wanted_source_ids = {
        identifier(row.get("epg_id", ""))
        for row in enabled_rows
        if requested_source(row) == "epgshare01"
        and identifier(row.get("epg_id", ""))
    }
    source_icons, source_variants = extract_source_icons(
        source_xmltv, wanted_source_ids, base_url=source_base_url
    )

    manual_matches: set[tuple[str, str]] = set()
    for row in enabled_rows:
        identity = (
            str(row.get("server_id", "") or "").strip(),
            identifier(row.get("stream_id", "")),
        )
        if any(
            override_matches(row, override)
            and local_override_is_usable(override, logo_root)
            for override in base_rows
        ):
            manual_matches.add(identity)

    ephemeral_rows: list[dict[str, str]] = []
    covered_by = Counter()
    category_counts = Counter()
    named_counts = Counter()
    ordered_movie_rows = []
    for candidate in sorted(
        enabled_rows,
        key=lambda item: (
            str(item.get("server_id", "") or "").strip(),
            stream_sort_key(identifier(item.get("stream_id", ""))),
        ),
    ):
        candidate_role, _candidate_subject = classify_person_subject(
            candidate.get("category_name", ""), candidate.get("channel_name", "")
        )
        if candidate_role == "actor" or (
            candidate_role != "singer" and category_for(candidate) == "movies"
        ):
            ordered_movie_rows.append(
                (
                    movie_scope(candidate),
                    candidate.get("category_name", ""),
                    candidate.get("channel_name", ""),
                )
            )
    movie_variants = allocate_movie_variants(ordered_movie_rows)

    for row in enabled_rows:
        server_id = str(row.get("server_id", "") or "").strip()
        stream_id = identifier(row.get("stream_id", ""))
        identity = (server_id, stream_id)
        if safe_http_url(row.get("logo_url", "")):
            covered_by["mapping_logo"] += 1
            continue
        if identity in manual_matches:
            covered_by["manual_override"] += 1
            continue

        role, raw_subject = classify_person_subject(
            row.get("category_name", ""), row.get("channel_name", "")
        )
        if raw_subject:
            subject = canonical_name(raw_subject)
            subject_key = normalized_key(subject)
            asset = portraits.get(subject_key)
            if asset is not None:
                ephemeral_rows.append(
                    {
                        "enabled": "true",
                        "server_id": server_id,
                        "stream_id": stream_id,
                        "epg_id": "",
                        "channel_name": identifier(row.get("channel_name", "")),
                        "icon_url": "",
                        "local_file": str(asset["local_file"]),
                        "priority": "400",
                        "notes": (
                            f"{NAMED_PORTRAIT_NOTE_PREFIX} {subject}; "
                            "exact public subject asset "
                            f"{asset.get('asset_id', '')}"
                        ),
                    }
                )
                covered_by["named_portrait"] += 1
                named_counts[f"{role}_portrait"] += 1
                continue

            asset_name, local_file = generated_asset_for(
                row,
                person_role=role,
                named_person=True,
                extension=category_extension,
                movie_variant=movie_variants.get(movie_variant_key(row), ""),
            )
            if not (logo_root / local_file).is_file():
                raise FileNotFoundError(f"Missing generated icon: {logo_root / local_file}")
            ephemeral_rows.append(
                {
                    "enabled": "true",
                    "server_id": server_id,
                    "stream_id": stream_id,
                    "epg_id": "",
                    "channel_name": identifier(row.get("channel_name", "")),
                    "icon_url": "",
                    "local_file": local_file,
                    "priority": "200",
                    "notes": (
                        f"{NAMED_SYMBOL_NOTE_PREFIX} {role} {subject}; "
                        f"transparent {asset_name} fallback; replace with a "
                        "reviewed exact portrait when available"
                    ),
                }
            )
            covered_by["named_symbol"] += 1
            named_counts[f"{role}_symbol"] += 1
            category_counts[asset_name] += 1
            continue

        source = requested_source(row)
        epg_id = identifier(row.get("epg_id", ""))
        if source == "epgshare01" and usable_source_icon(
            epg_id, source_icons, source_variants
        ):
            covered_by["source_xmltv"] += 1
            continue

        asset_name, local_file = generated_asset_for(
            row,
            person_role=role,
            extension=category_extension,
            movie_variant=movie_variants.get(movie_variant_key(row), ""),
        )
        if not (logo_root / local_file).is_file():
            raise FileNotFoundError(f"Missing generated icon: {logo_root / local_file}")
        ephemeral_rows.append(
            {
                "enabled": "true",
                "server_id": server_id,
                "stream_id": stream_id,
                "epg_id": "",
                "channel_name": identifier(row.get("channel_name", "")),
                "icon_url": "",
                "local_file": local_file,
                "priority": "10",
                "notes": (
                    f"{GENERATED_NOTE_PREFIX} {asset_name}; replace with reviewed "
                    "exact artwork when available"
                ),
            }
        )
        category_counts[asset_name] += 1
        covered_by["generated_fallback"] += 1

    ephemeral_rows.sort(
        key=lambda row: (
            row["server_id"],
            stream_sort_key(row["stream_id"]),
            row["channel_name"].casefold(),
            row["channel_name"],
        )
    )
    exact_keys = [(row["server_id"], row["stream_id"]) for row in ephemeral_rows]
    if len(exact_keys) != len(set(exact_keys)):
        raise ValueError("Generated icon rows contain duplicate server/stream keys.")

    if sum(covered_by.values()) != len(enabled_rows):
        raise AssertionError("Icon coverage accounting does not match enabled rows.")
    write_config(output_config, [*base_rows, *ephemeral_rows])
    return {
        "enabled_mapping_rows": len(enabled_rows),
        "preserved_base_config_rows": len(base_rows),
        "generated_fallback_rows": covered_by["generated_fallback"],
        "named_portrait_rows": covered_by["named_portrait"],
        "named_symbol_rows": covered_by["named_symbol"],
        "named_fallback_rows": 0,
        "final_private_config_rows": len(base_rows) + len(ephemeral_rows),
        "coverage": dict(sorted(covered_by.items())),
        "generated_categories": dict(sorted(category_counts.items())),
        "named_assets": dict(sorted(named_counts.items())),
        "source_catalog_ids_with_icons": len(source_icons),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-csv", type=Path, required=True)
    parser.add_argument("--source-xmltv", type=Path, required=True)
    parser.add_argument("--source-base-url", default="")
    parser.add_argument(
        "--base-config", type=Path, default=Path("config/channel_icons.csv")
    )
    parser.add_argument("--output-config", type=Path, required=True)
    parser.add_argument("--logo-root", type=Path, default=Path("assets/logos"))
    parser.add_argument(
        "--asset-catalog",
        type=Path,
        default=Path("assets/logos/icon_catalog.csv"),
    )
    parser.add_argument(
        "--category-extension", choices=("png", "svg"), default="png"
    )
    args = parser.parse_args()
    summary = generate(
        mapping_csv=args.mapping_csv,
        source_xmltv=args.source_xmltv,
        base_config=args.base_config,
        output_config=args.output_config,
        logo_root=args.logo_root,
        asset_catalog=args.asset_catalog,
        source_base_url=args.source_base_url,
        category_extension=args.category_extension,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
