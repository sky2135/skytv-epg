#!/usr/bin/env python3
"""Generate the minimalist, background-free SKY TV fallback icon set.

The committed SVG files are the editable vector masters. Transparent 512px
PNG renders are generated alongside them because many IPTV clients do not
display SVG channel artwork reliably.

The fallback family intentionally uses one neutral two-colour treatment:
off-white filled symbols with a dark rounded outline. The canvas itself stays
transparent, so the artwork works in TV guides without looking like a card.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


FILL = "#F7F8FA"
INK = "#1B2230"


def dual_line(
    path_data: str,
    *,
    outer_width: int = 24,
    inner_width: int = 10,
    dash_array: str = "",
) -> str:
    """Draw an open stroke as off-white ink with a dark rounded edge."""

    dash = f' stroke-dasharray="{dash_array}"' if dash_array else ""
    return (
        f'<path d="{path_data}" fill="none" stroke="{INK}" '
        f'stroke-width="{outer_width}"{dash}/>'
        f'<path d="{path_data}" fill="none" stroke="{FILL}" '
        f'stroke-width="{inner_width}"{dash}/>'
    )


# Original project drawings, deliberately readable at TV-guide thumbnail size.
# Every icon has a solid off-white interior and a dark outline. No full-canvas
# rectangle is used: the area outside each symbol is genuinely transparent.
ICONS = {
    "adult": (
        "Adult",
        '<path d="M256 58 414 119v122c0 102-61 176-158 217C159 417 98 343 98 241V119z"/>'
        + '<rect x="172" y="230" width="168" height="132" rx="28"/>'
        + dual_line("M202 230v-34c0-33 24-58 54-58s54 25 54 58v34")
        + f'<circle cx="256" cy="296" r="16" fill="{INK}" stroke="none"/>'
        + dual_line("M256 309v25", outer_width=18, inner_width=7),
    ),
    "documentary": (
        "Documentary",
        '<circle cx="256" cy="256" r="178"/>'
        + dual_line(
            "M78 256h356M256 78c60 52 92 113 92 178s-32 126-92 178c-60-52-92-113-92-178s32-126 92-178z"
        )
        + dual_line("M105 164h302M105 348h302"),
    ),
    "education": (
        "Education",
        '<path d="m58 194 198-101 198 101-198 101z"/>'
        + '<path d="M128 273v91c74 55 182 55 256 0v-91"/>'
        + dual_line("M454 196v130")
        + f'<circle cx="454" cy="354" r="20" fill="{INK}" stroke="none"/>',
    ),
    "entertainment": (
        "Entertainment",
        '<path d="m256 61 54 122 133 12-101 88 30 130-116-68-116 68 30-130-101-88 133-12z"/>'
        + f'<path d="m256 155 25 56 61 6-46 40 14 60-54-32-54 32 14-60-46-40 61-6z" fill="{INK}" stroke="none"/>',
    ),
    "events": (
        "Events",
        '<rect x="72" y="118" width="368" height="322" rx="44"/>'
        + dual_line("M72 205h368M164 69v99M348 69v99")
        + f'<path d="m256 249 24 52 57 6-43 38 12 56-50-29-50 29 12-56-43-38 57-6z" fill="{INK}" stroke="none"/>',
    ),
    "general": (
        "General TV",
        '<rect x="58" y="96" width="396" height="300" rx="48"/>'
        + f'<path d="m219 167 131 79-131 79z" fill="{INK}" stroke="none"/>'
        + dual_line("M172 449h168m-135-53-33 53m135-53 33 53"),
    ),
    "kids": (
        "Kids",
        '<circle cx="160" cy="184" r="76"/>'
        + '<rect x="278" y="108" width="150" height="150" rx="36"/>'
        + '<path d="m256 266 97 166H159z"/>'
        + dual_line("M139 354c55 53 179 53 234 0"),
    ),
    "lifestyle": (
        "Lifestyle",
        '<path d="M78 276 256 116l178 160v166H78z"/>'
        + dual_line("M198 442V319h116v123")
        + '<path d="M273 242c77-107 157-73 150-166-110 2-167 57-150 166z"/>'
        + dual_line("M274 241c36-49 73-84 111-112"),
    ),
    "movie-clapperboard": (
        "Movies — Clapperboard",
        '<rect x="63" y="187" width="386" height="256" rx="38"/>'
        + '<path d="M71 187 116 74h366l-45 113z"/>'
        + dual_line(
            "m137 80 49 103m55-103 49 103m55-103 49 103m55-103 28 59"
        )
        + f'<path d="m219 254 121 70-121 70z" fill="{INK}" stroke="none"/>',
    ),
    "movie-reel": (
        "Movies — Film Reel",
        '<circle cx="219" cy="235" r="166"/>'
        + f'<circle cx="219" cy="235" r="31" fill="{INK}" stroke="none"/>'
        + f'<circle cx="219" cy="127" r="37" fill="{INK}" stroke="none"/>'
        + f'<circle cx="322" cy="201" r="37" fill="{INK}" stroke="none"/>'
        + f'<circle cx="283" cy="322" r="37" fill="{INK}" stroke="none"/>'
        + f'<circle cx="155" cy="322" r="37" fill="{INK}" stroke="none"/>'
        + f'<circle cx="116" cy="201" r="37" fill="{INK}" stroke="none"/>'
        + dual_line("M364 314c50 24 70 67 44 126"),
    ),
    "movie-projector": (
        "Movies — Projector",
        '<circle cx="162" cy="148" r="82"/>'
        + '<circle cx="322" cy="148" r="82"/>'
        + f'<circle cx="162" cy="148" r="25" fill="{INK}" stroke="none"/>'
        + f'<circle cx="322" cy="148" r="25" fill="{INK}" stroke="none"/>'
        + '<rect x="86" y="226" width="302" height="176" rx="34"/>'
        + '<path d="m388 268 89-49v190l-89-49z"/>'
        + dual_line("m194 402-52 64m148-64 52 64"),
    ),
    "movie-filmstrip": (
        "Movies — Filmstrip",
        '<rect x="47" y="104" width="418" height="304" rx="36"/>'
        + f'<path d="M87 128h30v38H87zm0 73h30v38H87zm0 73h30v38H87zm0 73h30v38H87zm308-219h30v38h-30zm0 73h30v38h-30zm0 73h30v38h-30zm0 73h30v38h-30z" fill="{INK}" stroke="none"/>'
        + f'<path d="m202 175 119 81-119 81z" fill="{INK}" stroke="none"/>',
    ),
    "movie-ticket": (
        "Movies — Ticket",
        '<path d="M73 142h366v66c-36 0-59 20-59 48s23 48 59 48v66H73v-66c36 0 59-20 59-48s-23-48-59-48z"/>'
        + dual_line("M181 142v228", dash_array="18 20")
        + f'<path d="m301 181 23 49 55 7-40 37 10 54-48-26-48 26 10-54-40-37 55-7z" fill="{INK}" stroke="none"/>',
    ),
    "music-notes": (
        "Music — Notes",
        '<path d="M198 128v232c0 48-39 82-89 82-47 0-81-27-81-66s34-68 82-68c18 0 35 4 48 11V101l296-56v211c0 48-39 82-89 82-47 0-81-27-81-66s34-68 82-68c18 0 35 4 48 11V78z"/>'
        + dual_line("M198 191 414 150"),
    ),
    "music-microphone": (
        "Music — Microphone",
        '<rect x="178" y="53" width="156" height="280" rx="78"/>'
        + dual_line(
            "M137 239v17c0 66 53 119 119 119s119-53 119-119v-17M256 375v79m-80 0h160"
        )
        + dual_line(
            "M202 135h108m-108 62h108m-108 62h108",
            outer_width=18,
            inner_width=7,
        ),
    ),
    "music-headphones": (
        "Music — Headphones",
        dual_line(
            "M88 277v-37c0-109 75-188 168-188s168 79 168 188v37",
            outer_width=44,
            inner_width=26,
        )
        + '<rect x="60" y="249" width="106" height="186" rx="41"/>'
        + '<rect x="346" y="249" width="106" height="186" rx="41"/>'
        + dual_line("M166 397c44 47 136 47 180 0"),
    ),
    "music-guitar": (
        "Music — Guitar",
        '<path d="M169 261c-71 12-111 70-87 129 25 61 94 76 145 36 29-23 38-58 29-92l46-46c34 9 69 0 92-29 40-51 25-120-36-145-59-24-117 16-129 87z"/>'
        + '<path d="m260 250 144-144 45 45-144 144z"/>'
        + f'<circle cx="181" cy="346" r="34" fill="{INK}" stroke="none"/>'
        + dual_line(
            "m393 118 42-42m-18 67 42-42", outer_width=18, inner_width=7
        ),
    ),
    "music-dhol": (
        "Music — Dhol",
        '<path d="M109 166c85-47 209-47 294 0l-31 201c-68 38-164 38-232 0z"/>'
        + '<ellipse cx="256" cy="166" rx="147" ry="56"/>'
        + '<ellipse cx="256" cy="367" rx="116" ry="44"/>'
        + dual_line(
            "m144 192 103 153m121-153L265 345m-75-166 83 179m49-179-83 179M77 72l112 106M435 72 323 178",
            outer_width=18,
            inner_width=7,
        ),
    ),
    "music-sitar": (
        "Music — Sitar",
        '<path d="M183 352c-43 39-43 93-4 121 39 27 93 8 108-45 17-58-21-100-21-100z"/>'
        + '<path d="m231 366 91-284 47 15-87 288z"/>'
        + dual_line(
            "m320 85 67-43m-80 84 83-16m-97 56 85 13M246 377 346 85",
            outer_width=17,
            inner_width=7,
        )
        + f'<circle cx="232" cy="410" r="32" fill="{INK}" stroke="none"/>',
    ),
    "news": (
        "News",
        '<rect x="62" y="89" width="388" height="334" rx="39"/>'
        + f'<rect x="105" y="137" width="132" height="116" rx="14" fill="{INK}" stroke="none"/>'
        + dual_line(
            "M270 150h132m-132 56h132M105 303h297M105 360h297",
            outer_width=21,
            inner_width=9,
        ),
    ),
    "radio": (
        "Radio",
        '<rect x="68" y="171" width="376" height="258" rx="45"/>'
        + dual_line("m113 160 286-108")
        + '<circle cx="321" cy="298" r="78"/>'
        + f'<circle cx="321" cy="298" r="24" fill="{INK}" stroke="none"/>'
        + dual_line(
            "M117 236h96m-96 55h96m-96 55h68", outer_width=20, inner_width=8
        ),
    ),
    "religion": (
        "Religion",
        '<path d="M63 155c78-22 143-7 193 40 50-47 115-62 193-40v250c-78-22-143-7-193 40-50-47-115-62-193-40z"/>'
        + dual_line("M256 195v250M256 69v72M160 96l46 59m146-59-46 59"),
    ),
    "shopping": (
        "Shopping",
        '<path d="M84 173h344l-28 276H112z"/>'
        + dual_line("M173 191v-41c0-58 34-99 83-99s83 41 83 99v41")
        + f'<path d="m256 243 26 53 59 8-43 40 10 58-52-28-52 28 10-58-43-40 59-8z" fill="{INK}" stroke="none"/>',
    ),
    "sports": (
        "Sports",
        '<circle cx="256" cy="256" r="190"/>'
        + f'<path d="m256 163 91 66-35 107H200l-35-107z" fill="{INK}" stroke="none"/>'
        + dual_line(
            "M256 66v97M75 196l90 33m272-33-90 33M142 417l58-81m170 81-58-81"
        ),
    ),
}


def render_icon(name: str, title: str, body: str) -> str:
    description = title.replace(" — ", " ").casefold()
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-labelledby="title desc">
  <title id="title">{title} channel icon</title>
  <desc id="desc">Original minimalist filled SKY TV artwork for {description} channels, with a transparent outer background.</desc>
  <g fill="{FILL}" stroke="{INK}" stroke-width="24" stroke-linecap="round" stroke-linejoin="round">
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
        svg_destination = args.output_dir / f"category-{name}-v3.svg"
        svg_destination.write_text(svg, encoding="utf-8")
        if not args.svg_only:
            render_png(
                svg_destination,
                args.output_dir / f"category-{name}-v3.png",
            )
    formats = "SVG" if args.svg_only else "SVG and transparent PNG"
    print(f"Generated {len(ICONS)} category icons ({formats}) in {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
