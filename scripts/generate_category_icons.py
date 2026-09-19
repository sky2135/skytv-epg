#!/usr/bin/env python3
"""Generate the original, background-free SKY TV fallback icon set.

The committed SVG files are the editable vector masters. Transparent 512px
PNG renders are generated alongside them because many IPTV clients do not
display SVG channel artwork reliably.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


WHITE = "#f8fafc"
INK = "#182033"
RED = "#e84a5f"
GOLD = "#f4b942"
PURPLE = "#7b61d1"
BLUE = "#3787e5"
CYAN = "#35b7c8"
GREEN = "#38a66b"
PINK = "#e45896"
ORANGE = "#ed7b3a"


# Original project drawings, deliberately readable at TV-guide thumbnail size.
# No full-canvas rectangle is used: every SVG is genuinely background-free.
ICONS = {
    "adult": (
        "Adult",
        f'<path d="M256 66 410 124v116c0 99-61 171-154 212-93-41-154-113-154-212V124z" fill="{PINK}" stroke="{WHITE}" stroke-width="20"/>'
        f'<text x="256" y="298" text-anchor="middle" fill="{WHITE}" stroke="{INK}" stroke-width="5" paint-order="stroke" font-size="122" font-family="sans-serif" font-weight="800">18+</text>',
    ),
    "documentary": (
        "Documentary",
        f'<circle cx="256" cy="256" r="172" fill="{CYAN}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M84 256h344M256 84c61 56 88 115 88 172s-27 116-88 172c-61-56-88-115-88-172s27-116 88-172z" fill="none" stroke="{INK}" stroke-width="22"/>',
    ),
    "education": (
        "Education",
        f'<path d="m62 200 194-99 194 99-194 99z" fill="{BLUE}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M135 270v91c72 55 170 55 242 0v-91" fill="none" stroke="{INK}" stroke-width="26"/>'
        f'<path d="M450 202v130" stroke="{GOLD}" stroke-width="24"/>'
        f'<circle cx="450" cy="353" r="18" fill="{GOLD}" stroke="{WHITE}" stroke-width="10"/>',
    ),
    "entertainment": (
        "Entertainment",
        f'<path d="m256 63 51 118 128 11-97 85 29 124-111-65-111 65 29-124-97-85 128-11z" fill="{PURPLE}" stroke="{WHITE}" stroke-width="21"/>'
        f'<path d="m256 149 27 62 68 6-52 45 16 66-59-35-59 35 16-66-52-45 68-6z" fill="{GOLD}"/>',
    ),
    "events": (
        "Events",
        f'<rect x="73" y="126" width="366" height="316" rx="43" fill="{ORANGE}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M73 211h366M164 76v99M348 76v99" stroke="{INK}" stroke-width="26"/>'
        f'<path d="m256 249 23 52 56 5-42 37 12 54-49-29-49 29 12-54-42-37 56-5z" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>',
    ),
    "general": (
        "General TV",
        f'<rect x="62" y="104" width="388" height="291" rx="48" fill="{BLUE}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m220 171 128 77-128 77z" fill="{WHITE}" stroke="{INK}" stroke-width="14"/>'
        f'<path d="M171 447h170" stroke="{INK}" stroke-width="28"/>'
        f'<path d="m203 394-32 53m138-53 32 53" stroke="{INK}" stroke-width="22"/>',
    ),
    "kids": (
        "Kids",
        f'<circle cx="164" cy="188" r="78" fill="{GOLD}" stroke="{WHITE}" stroke-width="18"/>'
        f'<rect x="271" y="112" width="154" height="154" rx="34" fill="{PINK}" stroke="{WHITE}" stroke-width="18"/>'
        f'<path d="m256 268 93 158H163z" fill="{GREEN}" stroke="{WHITE}" stroke-width="18"/>'
        f'<path d="M133 361c59 57 187 57 246 0" fill="none" stroke="{INK}" stroke-width="24"/>',
    ),
    "lifestyle": (
        "Lifestyle",
        f'<path d="M69 282 256 116l187 166" fill="none" stroke="{GREEN}" stroke-width="42"/>'
        f'<path d="M123 274v166h266V274" fill="{WHITE}" stroke="{INK}" stroke-width="24"/>'
        f'<path d="M268 250c88-106 165-63 153-159-114 1-173 55-153 159z" fill="{GREEN}" stroke="{WHITE}" stroke-width="16"/>'
        f'<path d="M270 249c35-49 72-82 112-111" stroke="{INK}" stroke-width="15"/>',
    ),
    "movie-clapperboard": (
        "Movies — Clapperboard",
        f'<rect x="65" y="188" width="382" height="253" rx="37" fill="{RED}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M72 188 116 78h365l-44 110z" fill="{GOLD}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m136 83 49 101m54-101 49 101m54-101 49 101m54-101 30 62" stroke="{INK}" stroke-width="22"/>'
        f'<path d="m220 254 118 68-118 68z" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>',
    ),
    "movie-reel": (
        "Movies — Film Reel",
        f'<circle cx="220" cy="235" r="164" fill="{RED}" stroke="{WHITE}" stroke-width="22"/>'
        f'<circle cx="220" cy="235" r="35" fill="{GOLD}" stroke="{INK}" stroke-width="15"/>'
        f'<circle cx="220" cy="129" r="39" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>'
        f'<circle cx="321" cy="202" r="39" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>'
        f'<circle cx="282" cy="319" r="39" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>'
        f'<circle cx="158" cy="319" r="39" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>'
        f'<circle cx="119" cy="202" r="39" fill="{WHITE}" stroke="{INK}" stroke-width="12"/>'
        f'<path d="M364 314c49 25 68 67 43 124M372 331c32 21 44 51 25 96" fill="none" stroke="{GOLD}" stroke-width="25"/>',
    ),
    "movie-projector": (
        "Movies — Projector",
        f'<circle cx="162" cy="148" r="82" fill="{GOLD}" stroke="{WHITE}" stroke-width="20"/>'
        f'<circle cx="322" cy="148" r="82" fill="{RED}" stroke="{WHITE}" stroke-width="20"/>'
        f'<circle cx="162" cy="148" r="27" fill="{INK}"/>'
        f'<circle cx="322" cy="148" r="27" fill="{INK}"/>'
        f'<rect x="89" y="227" width="294" height="174" rx="33" fill="{RED}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m383 267 88-48v190l-88-48z" fill="{GOLD}" stroke="{WHITE}" stroke-width="18"/>'
        f'<path d="m193 401-51 64m147-64 51 64" stroke="{INK}" stroke-width="25"/>',
    ),
    "movie-filmstrip": (
        "Movies — Filmstrip",
        f'<rect x="48" y="105" width="416" height="302" rx="34" fill="{RED}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M111 105v302m290-302v302" stroke="{INK}" stroke-width="20"/>'
        f'<path d="M70 153h20m-20 68h20m-20 68h20m-20 68h20m332-204h20m-20 68h20m-20 68h20m-20 68h20" stroke="{GOLD}" stroke-width="24"/>'
        f'<path d="m207 178 113 78-113 78z" fill="{WHITE}" stroke="{INK}" stroke-width="13"/>',
    ),
    "movie-ticket": (
        "Movies — Ticket",
        f'<path d="M76 145h360v62c-36 0-57 20-57 49s21 49 57 49v62H76v-62c36 0 57-20 57-49s-21-49-57-49z" fill="{GOLD}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M180 145v222" stroke="{INK}" stroke-width="18" stroke-dasharray="20 18"/>'
        f'<path d="m299 179 23 48 53 7-39 36 10 52-47-25-47 25 10-52-39-36 53-7z" fill="{RED}" stroke="{INK}" stroke-width="12"/>',
    ),
    "music-notes": (
        "Music — Notes",
        f'<path d="M205 129v231c0 49-42 83-92 83s-84-28-84-68 35-70 85-70c21 0 40 6 56 15V102l284-56v209c0 49-42 83-92 83s-84-28-84-68 35-70 85-70c21 0 40 6 56 15V79z" fill="{PURPLE}" stroke="{WHITE}" stroke-width="18"/>'
        f'<path d="M205 190 419 148" stroke="{GOLD}" stroke-width="24"/>',
    ),
    "music-microphone": (
        "Music — Microphone",
        f'<rect x="177" y="56" width="158" height="276" rx="79" fill="{PINK}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M138 238v17c0 66 53 119 118 119s118-53 118-119v-17" fill="none" stroke="{INK}" stroke-width="28"/>'
        f'<path d="M256 374v78m-79 0h158" stroke="{INK}" stroke-width="28"/>'
        f'<path d="M201 133h110m-110 61h110m-110 61h110" stroke="{WHITE}" stroke-width="16"/>',
    ),
    "music-headphones": (
        "Music — Headphones",
        f'<path d="M88 280v-38c0-109 75-190 168-190s168 81 168 190v38" fill="none" stroke="{PURPLE}" stroke-width="46"/>'
        f'<rect x="62" y="253" width="103" height="180" rx="39" fill="{PINK}" stroke="{WHITE}" stroke-width="20"/>'
        f'<rect x="347" y="253" width="103" height="180" rx="39" fill="{PINK}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M165 397c43 48 139 48 182 0" fill="none" stroke="{INK}" stroke-width="22"/>',
    ),
    "music-guitar": (
        "Music — Guitar",
        f'<path d="M169 260c-71 12-111 70-87 129 25 61 94 76 145 36 29-23 38-58 29-92l45-45c34 9 69 0 92-29 40-51 25-120-36-145-59-24-117 16-129 87z" fill="{ORANGE}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m261 249 142-142 44 44-142 142" fill="{GOLD}" stroke="{INK}" stroke-width="18"/>'
        f'<circle cx="181" cy="345" r="37" fill="{INK}" stroke="{WHITE}" stroke-width="14"/>'
        f'<path d="m391 119 43-43m-19 67 43-43" stroke="{INK}" stroke-width="18"/>',
    ),
    "music-dhol": (
        "Music — Dhol",
        f'<path d="M111 167c84-46 206-46 290 0l-30 198c-68 37-162 37-230 0z" fill="{ORANGE}" stroke="{WHITE}" stroke-width="21"/>'
        f'<ellipse cx="256" cy="167" rx="145" ry="55" fill="{GOLD}" stroke="{INK}" stroke-width="18"/>'
        f'<ellipse cx="256" cy="365" rx="115" ry="43" fill="{GOLD}" stroke="{INK}" stroke-width="18"/>'
        f'<path d="m144 191 102 152m122-152L266 343M190 179l82 177m50-177-82 177" stroke="{PINK}" stroke-width="15"/>'
        f'<path d="m78 73 111 105m245-105L323 178" stroke="{INK}" stroke-width="20"/>',
    ),
    "music-sitar": (
        "Music — Sitar",
        f'<path d="M185 353c-42 39-42 92-4 119 38 27 91 8 106-44 17-57-20-98-20-98z" fill="{ORANGE}" stroke="{WHITE}" stroke-width="19"/>'
        f'<path d="m232 366 91-282 45 15-86 285" fill="{GOLD}" stroke="{INK}" stroke-width="18"/>'
        f'<path d="m321 87 65-43m-78 83 81-16m-95 55 82 13" stroke="{INK}" stroke-width="17"/>'
        f'<circle cx="232" cy="409" r="35" fill="{PURPLE}" stroke="{INK}" stroke-width="12"/>'
        f'<path d="M246 377 345 86" stroke="{WHITE}" stroke-width="8"/>',
    ),
    "news": (
        "News",
        f'<rect x="64" y="92" width="384" height="329" rx="37" fill="{BLUE}" stroke="{WHITE}" stroke-width="20"/>'
        f'<rect x="105" y="138" width="133" height="116" rx="14" fill="{GOLD}" stroke="{INK}" stroke-width="14"/>'
        f'<path d="M270 151h132m-132 54h132M105 303h297M105 358h297" stroke="{WHITE}" stroke-width="23"/>',
    ),
    "radio": (
        "Radio",
        f'<rect x="70" y="174" width="372" height="252" rx="43" fill="{CYAN}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m115 163 282-107" stroke="{INK}" stroke-width="22"/>'
        f'<circle cx="320" cy="296" r="76" fill="{GOLD}" stroke="{INK}" stroke-width="18"/>'
        f'<path d="M119 237h94m-94 53h94m-94 53h65" stroke="{WHITE}" stroke-width="22"/>',
    ),
    "religion": (
        "Religion",
        f'<path d="M65 158c76-22 141-7 191 39 50-46 115-61 191-39v245c-76-22-141-7-191 39-50-46-115-61-191-39z" fill="{GOLD}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="M256 197v245M256 72v71M161 99l45 58m145-58-45 58" stroke="{INK}" stroke-width="22"/>',
    ),
    "shopping": (
        "Shopping",
        f'<path d="M86 176h340l-27 271H113z" fill="{PINK}" stroke="{WHITE}" stroke-width="21"/>'
        f'<path d="M174 194v-42c0-58 33-100 82-100s82 42 82 100v42" fill="none" stroke="{INK}" stroke-width="26"/>'
        f'<path d="m256 243 25 52 58 8-42 39 10 57-51-27-51 27 10-57-42-39 58-8z" fill="{GOLD}" stroke="{INK}" stroke-width="12"/>',
    ),
    "sports": (
        "Sports",
        f'<circle cx="256" cy="256" r="188" fill="{GREEN}" stroke="{WHITE}" stroke-width="20"/>'
        f'<path d="m256 166 88 64-34 103H202l-34-103zM256 68v98M77 198l91 32m267-32-91 32M143 415l59-82m167 82-59-82" fill="none" stroke="{INK}" stroke-width="22"/>',
    ),
}


def render_icon(name: str, title: str, body: str) -> str:
    description = title.replace(" — ", " ").casefold()
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-labelledby="title desc">
  <title id="title">{title} channel icon</title>
  <desc id="desc">Original background-free SKY TV fallback artwork for {description} channels.</desc>
  <g stroke-linecap="round" stroke-linejoin="round">
    {body}
  </g>
</svg>
'''


def render_png(source: Path, destination: Path) -> None:
    """Rasterize one transparent SVG for IPTV clients without SVG support."""
    inkscape = shutil.which("inkscape")
    if not inkscape:  # pragma: no cover - depends on developer workstation
        raise SystemExit(
            "PNG generation needs Inkscape. "
            "Use --svg-only when only the vector sources are needed."
        )
    subprocess.run(
        [
            inkscape,
            str(source),
            "--export-type=png",
            f"--export-filename={destination}",
            "--export-width=512",
            "--export-height=512",
            "--export-background-opacity=0",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", type=Path, default=Path("assets/logos/generated")
    )
    parser.add_argument(
        "--svg-only",
        action="store_true",
        help="Skip the broadly compatible transparent PNG copies.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, (title, body) in sorted(ICONS.items()):
        svg = render_icon(name, title, body)
        svg_destination = args.output_dir / f"category-{name}-v2.svg"
        svg_destination.write_text(svg, encoding="utf-8")
        if not args.svg_only:
            render_png(
                svg_destination,
                args.output_dir / f"category-{name}-v2.png",
            )
    formats = "SVG" if args.svg_only else "SVG and transparent PNG"
    print(f"Generated {len(ICONS)} category icons ({formats}) in {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
