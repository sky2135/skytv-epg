#!/usr/bin/env python3
"""Generate original name-and-motif fallbacks for named 24/7 channels.

The artwork deliberately contains no portrait or third-party image.  A verified
freely licensed portrait can replace any fallback later without changing the
exact stream-to-person mapping produced by this script.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

try:
    from scripts.named_person_subjects import canonical_name, normalized_key, slugify
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from named_person_subjects import canonical_name, normalized_key, slugify


CANVAS_SIZE = 1024
OUTPUT_SIZE = 512
DEFAULT_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

PALETTES = {
    "singer": (
        (42, 22, 88),
        (75, 38, 135),
        (56, 34, 100),
        (111, 43, 129),
        (31, 54, 105),
        (77, 49, 151),
    ),
    "actor": (
        (77, 24, 43),
        (144, 47, 65),
        (66, 31, 61),
        (137, 60, 49),
        (61, 37, 64),
        (122, 44, 82),
    ),
}


@dataclass(frozen=True)
class Requirement:
    server_id: str
    stream_id: str
    channel_name: str
    person_role: str
    subject_raw: str
    subject_canonical: str
    subject_key: str


def load_requirements(path: Path) -> list[Requirement]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "server_id",
        "stream_id",
        "channel_name",
        "person_role",
        "subject_candidate",
        "current_asset_id",
    }
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise SystemExit(f"Research CSV is missing columns: {', '.join(sorted(missing))}")

    requirements: list[Requirement] = []
    for row in rows:
        raw_name = " ".join(row["subject_candidate"].split())
        # Lata already has an approved real portrait.  The blank Bollywood song
        # row is intentionally a generic music channel, not a named person.
        if not raw_name or row["current_asset_id"] == "lata-mangeshkar":
            continue
        role = row["person_role"].strip().casefold()
        if role not in PALETTES:
            raise SystemExit(f"Unsupported person role {role!r} for stream {row['stream_id']}")
        display_name = canonical_name(raw_name)
        requirements.append(
            Requirement(
                server_id=row["server_id"].strip(),
                stream_id=row["stream_id"].strip(),
                channel_name=row["channel_name"].strip(),
                person_role=role,
                subject_raw=raw_name,
                subject_canonical=display_name,
                subject_key=normalized_key(display_name),
            )
        )
    return sorted(requirements, key=lambda row: (row.server_id, int(row.stream_id)))


def load_catalog_people(path: Path) -> dict[str, Requirement]:
    """Load only public subject names and roles for a normal regeneration."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    required = {"asset_id", "subject_name", "person_role"}
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise SystemExit(f"Asset catalog is missing columns: {', '.join(sorted(missing))}")
    people: dict[str, Requirement] = {}
    for row in rows:
        name = " ".join(row["subject_name"].split())
        role = row["person_role"].strip().casefold()
        if not name or role not in PALETTES:
            raise SystemExit(f"Invalid public subject catalog row: {row!r}")
        key = normalized_key(name)
        requirement = Requirement("", "", "", role, name, name, key)
        previous = people.setdefault(key, requirement)
        if previous.person_role != role:
            raise SystemExit(f"Conflicting roles for {name}")
        expected_id = f"person-fallback-{slugify(name)}"
        if row["asset_id"].strip() != expected_id:
            raise SystemExit(
                f"Unexpected asset ID for {name}: {row['asset_id']!r}; "
                f"expected {expected_id!r}"
            )
    return people


def require_private_path(path: Path, label: str) -> Path:
    """Reject accidental writes of lineup-derived data inside the repository."""
    if not path.is_absolute():
        raise SystemExit(f"{label} must use an explicit absolute private path.")
    resolved = path.resolve()
    project_root = Path(__file__).resolve().parents[1]
    private_root = (project_root / ".build" / "private-icon-audit").resolve()
    is_private_build_path = resolved == private_root or private_root in resolved.parents
    if (resolved == project_root or project_root in resolved.parents) and not is_private_build_path:
        raise SystemExit(
            f"{label} must be outside tracked repository paths or under "
            ".build/private-icon-audit/."
        )
    return resolved


def color_mix(start: tuple[int, int, int], end: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * amount) for a, b in zip(start, end))


def palette_for(name: str, role: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    choices = PALETTES[role]
    seed = hashlib.sha256(f"{role}:{normalized_key(name)}".encode()).digest()
    first = choices[seed[0] % len(choices)]
    second = choices[seed[1] % len(choices)]
    if second == first:
        second = choices[(seed[1] + 1) % len(choices)]
    return first, color_mix(second, (245, 170, 82), 0.22)


def gradient_background(start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for y in range(CANVAS_SIZE):
        amount = y / (CANVAS_SIZE - 1)
        color = color_mix(start, end, amount)
        draw.line((0, y, CANVAS_SIZE, y), fill=(*color, 255))
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, CANVAS_SIZE - 1, CANVAS_SIZE - 1), radius=138, fill=255)
    image.putalpha(mask)
    return image


def draw_microphone(draw: ImageDraw.ImageDraw) -> None:
    white = (255, 255, 255, 245)
    draw.rounded_rectangle((696, 138, 826, 394), radius=65, fill=white)
    draw.arc((627, 226, 895, 494), 0, 180, fill=white, width=29)
    draw.line((761, 493, 761, 556), fill=white, width=29)
    draw.line((686, 556, 836, 556), fill=white, width=29)
    draw.arc((614, 119, 908, 414), 307, 53, fill=(255, 255, 255, 105), width=18)


def draw_clapperboard(draw: ImageDraw.ImageDraw) -> None:
    white = (255, 255, 255, 245)
    draw.rounded_rectangle((620, 272, 904, 536), radius=30, outline=white, width=26)
    draw.polygon(((610, 260), (652, 139), (933, 139), (890, 260)), fill=white)
    dark = (76, 25, 48, 235)
    for x in (664, 760, 856):
        draw.polygon(((x, 145), (x + 47, 145), (x + 1, 256), (x - 46, 256)), fill=dark)
    draw.polygon(((733, 337), (733, 467), (843, 402)), fill=white)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def wrap_words(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    for word in text.split():
        candidate = " ".join((*current, word))
        if current and text_width(draw, candidate, font) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def fit_name(draw: ImageDraw.ImageDraw, name: str, font_path: Path) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    for size in range(108, 59, -2):
        font = ImageFont.truetype(str(font_path), size)
        lines = wrap_words(draw, name, font, 850)
        spacing = round(size * 0.18)
        line_height = draw.textbbox((0, 0), "Ag", font=font)[3]
        total_height = line_height * len(lines) + spacing * (len(lines) - 1)
        if len(lines) <= 3 and total_height <= 330:
            return font, lines, spacing
    raise SystemExit(f"Could not fit person name: {name}")


def render_icon(name: str, role: str, font_path: Path) -> Image.Image:
    start, end = palette_for(name, role)
    image = gradient_background(start, end)
    decoration = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(decoration)
    seed = hashlib.sha256(normalized_key(name).encode()).digest()
    for index in range(5):
        radius = 100 + seed[index] * 2
        x = -110 + index * 270
        y = 70 + (seed[index + 5] % 5) * 150
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=(255, 255, 255, 18), width=10)
    draw.rounded_rectangle((30, 30, 994, 994), radius=116, outline=(255, 255, 255, 120), width=7)

    label_font = ImageFont.truetype(str(font_path), 39)
    role_label = "24/7 MUSIC" if role == "singer" else "24/7 MOVIES"
    label_box = draw.textbbox((0, 0), role_label, font=label_font)
    label_width = label_box[2] - label_box[0]
    draw.rounded_rectangle((72, 78, 120 + label_width, 145), radius=34, fill=(8, 8, 18, 105))
    draw.text((96, 91), role_label, font=label_font, fill=(255, 255, 255, 235))
    if role == "singer":
        draw_microphone(draw)
    else:
        draw_clapperboard(draw)
    image = Image.alpha_composite(image, decoration)

    typography = Image.new("RGBA", image.size, (0, 0, 0, 0))
    type_draw = ImageDraw.Draw(typography)
    font, lines, spacing = fit_name(type_draw, name, font_path)
    line_boxes = [type_draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [box[3] - box[1] for box in line_boxes]
    block_height = sum(line_heights) + spacing * (len(lines) - 1)
    y = 626 + (292 - block_height) / 2
    for line, box, line_height in zip(lines, line_boxes, line_heights):
        width = box[2] - box[0]
        x = (CANVAS_SIZE - width) / 2
        type_draw.text((x + 5, y + 7), line, font=font, fill=(0, 0, 0, 100))
        type_draw.text((x, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=1)
        y += line_height + spacing
    image = Image.alpha_composite(image, typography)
    return image.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_png_atomic(image: Image.Image, destination: Path) -> None:
    """Replace a generated PNG only after its complete bytes are on disk."""
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary, format="PNG", optimize=True, compress_level=9)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        newline="",
        encoding="utf-8",
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_contact_sheet(paths: Sequence[Path], destination: Path, font_path: Path) -> None:
    columns = 12
    cell_width = 150
    cell_height = 182
    rows = (len(paths) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#10131a")
    label_font = ImageFont.truetype(str(font_path), 16)
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(paths):
        image = Image.open(path).convert("RGBA").resize((128, 128), Image.Resampling.LANCZOS)
        x = (index % columns) * cell_width + 11
        y = (index // columns) * cell_height + 8
        sheet.paste(image, (x, y), image)
        label = path.stem.removeprefix("person-fallback-").replace("-", " ")
        if len(label) > 19:
            label = label[:18] + "…"
        box = draw.textbbox((0, 0), label, font=label_font)
        draw.text((x + (128 - (box[2] - box[0])) / 2, y + 137), label, font=label_font, fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True, compress_level=9)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--research-csv",
        type=Path,
        help=(
            "Optional private lineup research CSV used to refresh the public "
            "subject catalog; use an absolute external path or the ignored "
            ".build/private-icon-audit/ directory."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("assets/logos/generated/people"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("assets/logos/named_person_fallback_catalog.csv"),
    )
    parser.add_argument(
        "--stream-map",
        type=Path,
        help=(
            "Optional exact-stream map; requires --research-csv and an absolute "
            "external path or the ignored .build/private-icon-audit/ directory."
        ),
    )
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--contact-sheet", type=Path)
    args = parser.parse_args()
    if not args.font.is_file():
        raise SystemExit(f"Font not found: {args.font}")

    if args.research_csv:
        research_path = require_private_path(args.research_csv, "--research-csv")
        requirements = load_requirements(research_path)
        people: dict[str, Requirement] = {}
        for row in requirements:
            previous = people.setdefault(row.subject_key, row)
            if previous.person_role != row.person_role:
                raise SystemExit(f"Conflicting roles for {row.subject_canonical}")
    else:
        requirements = []
        people = load_catalog_people(args.catalog)
    if args.stream_map and not args.research_csv:
        raise SystemExit("--stream-map requires --research-csv.")
    stream_map = (
        require_private_path(args.stream_map, "--stream-map")
        if args.stream_map
        else None
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    asset_rows: list[dict[str, str]] = []
    local_files: dict[str, str] = {}
    icon_paths: list[Path] = []
    used_slugs: dict[str, str] = {}
    for subject_key, row in sorted(people.items(), key=lambda item: item[1].subject_canonical.casefold()):
        slug = slugify(row.subject_canonical)
        if slug in used_slugs and used_slugs[slug] != subject_key:
            slug = f"{slug}-{row.person_role}"
        used_slugs[slug] = subject_key
        asset_id = f"person-fallback-{slug}"
        destination = args.output_dir / f"{asset_id}.png"
        save_png_atomic(
            render_icon(row.subject_canonical, row.person_role, args.font),
            destination,
        )
        icon_paths.append(destination)
        local_file = destination.relative_to(args.output_dir.parents[1]).as_posix()
        local_files[subject_key] = local_file
        asset_rows.append(
            {
                "asset_id": asset_id,
                "subject_type": "person",
                "subject_name": row.subject_canonical,
                "person_role": row.person_role,
                "local_file": local_file,
                "asset_kind": "generated_named_fallback",
                "creator": "SKY TV",
                "license_id": "CC0-1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "output_sha256": sha256(destination),
                "review_status": "fallback_ready",
                "rights_notes": (
                    "Original name-and-motif artwork released under CC0; "
                    "no portrait or third-party image."
                ),
            }
        )

    asset_by_key = {normalized_key(row["subject_name"]): row["asset_id"] for row in asset_rows}
    stream_rows = []
    for row in requirements:
        stream_rows.append(
            {
                "server_id": row.server_id,
                "stream_id": row.stream_id,
                "channel_name": row.channel_name,
                "person_role": row.person_role,
                "subject_raw": row.subject_raw,
                "subject_canonical": row.subject_canonical,
                "subject_key": row.subject_key,
                "asset_id": asset_by_key[row.subject_key],
                "local_file": local_files[row.subject_key],
                "priority": "300",
                "notes": "Exact-stream original fallback; replace with a verified reusable portrait when available.",
            }
        )

    write_csv(
        args.catalog,
        (
            "asset_id",
            "subject_type",
            "subject_name",
            "person_role",
            "local_file",
            "asset_kind",
            "creator",
            "license_id",
            "license_url",
            "output_sha256",
            "review_status",
            "rights_notes",
        ),
        asset_rows,
    )
    if stream_map:
        write_csv(
            stream_map,
            (
                "server_id",
                "stream_id",
                "channel_name",
                "person_role",
                "subject_raw",
                "subject_canonical",
                "subject_key",
                "asset_id",
                "local_file",
                "priority",
                "notes",
            ),
            stream_rows,
        )
    if args.contact_sheet:
        save_contact_sheet(icon_paths, args.contact_sheet, args.font)
    message = f"Generated {len(asset_rows)} named-person fallback icons"
    if requirements:
        message += f" from {len(requirements)} private lineup requirements"
    print(message + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
