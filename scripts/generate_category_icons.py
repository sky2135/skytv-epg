#!/usr/bin/env python3
"""Generate the original SKY TV fallback channel-icon set."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


ICONS = {
    "adult": (
        "#56142f",
        "#d73a74",
        '<path d="M256 90 390 140v98c0 86-53 148-134 184-81-36-134-98-134-184v-98z" fill="none" stroke="white" stroke-width="24"/>'
        '<text x="256" y="285" text-anchor="middle" fill="white" font-size="112" font-family="sans-serif" font-weight="700">18+</text>',
    ),
    "documentary": (
        "#12344d",
        "#1887a8",
        '<circle cx="256" cy="256" r="150" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="M106 256h300M256 106c54 50 78 101 78 150s-24 100-78 150c-54-50-78-101-78-150s24-100 78-150z" fill="none" stroke="white" stroke-width="20"/>',
    ),
    "education": (
        "#18304f",
        "#456eb3",
        '<path d="m82 210 174-88 174 88-174 88z" fill="white"/>'
        '<path d="M151 271v77c66 51 144 51 210 0v-77" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="M430 216v111" stroke="white" stroke-width="20" stroke-linecap="round"/>',
    ),
    "entertainment": (
        "#35174f",
        "#8a3fb0",
        '<path d="m256 91 45 103 112 10-85 74 25 109-97-57-97 57 25-109-85-74 112-10z" fill="white"/>',
    ),
    "events": (
        "#533019",
        "#dc7837",
        '<rect x="102" y="128" width="308" height="278" rx="34" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="M102 202h308M174 94v68M338 94v68" stroke="white" stroke-width="24" stroke-linecap="round"/>'
        '<path d="m256 238 20 45 49 5-37 32 11 48-43-25-43 25 11-48-37-32 49-5z" fill="white"/>',
    ),
    "general": (
        "#202839",
        "#52647c",
        '<rect x="77" y="112" width="358" height="266" rx="44" fill="none" stroke="white" stroke-width="26"/>'
        '<path d="m224 184 111 61-111 61z" fill="white"/>'
        '<path d="M180 422h152" stroke="white" stroke-width="24" stroke-linecap="round"/>',
    ),
    "kids": (
        "#274571",
        "#38a6dd",
        '<circle cx="177" cy="202" r="69" fill="#ffd35a"/>'
        '<rect x="269" y="136" width="137" height="137" rx="28" fill="#ff6f91"/>'
        '<path d="m258 286 78 126H180z" fill="#71e19b"/>'
        '<path d="M134 357c58 51 186 51 244 0" fill="none" stroke="white" stroke-width="22" stroke-linecap="round"/>',
    ),
    "lifestyle": (
        "#21462e",
        "#4da765",
        '<path d="M98 296 256 157l158 139" fill="none" stroke="white" stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M145 282v122h222V282" fill="none" stroke="white" stroke-width="24" stroke-linejoin="round"/>'
        '<path d="M266 251c78-89 139-55 131-136-96 1-146 46-131 136z" fill="#b9f0bd"/>',
    ),
    "movies": (
        "#3c1a27",
        "#a83b57",
        '<rect x="90" y="190" width="332" height="226" rx="28" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="M93 190 130 96h318l-37 94zM139 101l46 84M225 101l46 84M311 101l46 84M397 101l35 66" fill="none" stroke="white" stroke-width="22"/>'
        '<path d="m229 251 102 58-102 58z" fill="white"/>',
    ),
    "music": (
        "#2d1854",
        "#7047b8",
        '<path d="M221 142v221c0 43-37 72-81 72s-74-24-74-58 31-61 75-61c18 0 35 5 49 13V117l244-48v198c0 43-37 72-81 72s-74-24-74-58 31-61 75-61c18 0 35 5 49 13V97z" fill="white"/>',
    ),
    "news": (
        "#17375c",
        "#2d70ad",
        '<rect x="82" y="104" width="348" height="304" rx="28" fill="none" stroke="white" stroke-width="24"/>'
        '<rect x="119" y="149" width="121" height="104" rx="12" fill="white"/>'
        '<path d="M270 157h120M270 204h120M119 294h271M119 343h271" stroke="white" stroke-width="22" stroke-linecap="round"/>',
    ),
    "radio": (
        "#173f46",
        "#2f9096",
        '<rect x="91" y="174" width="330" height="236" rx="38" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="m133 164 242-92" stroke="white" stroke-width="20" stroke-linecap="round"/>'
        '<circle cx="306" cy="293" r="65" fill="none" stroke="white" stroke-width="22"/>'
        '<path d="M136 235h85M136 280h85M136 325h58" stroke="white" stroke-width="20" stroke-linecap="round"/>',
    ),
    "religion": (
        "#4b3517",
        "#b07d32",
        '<path d="M82 170c67-19 126-6 174 35 48-41 107-54 174-35v214c-67-19-126-6-174 35-48-41-107-54-174-35z" fill="none" stroke="white" stroke-width="22" stroke-linejoin="round"/>'
        '<path d="M256 205v214M256 99v58M174 122l35 47M338 122l-35 47" stroke="white" stroke-width="20" stroke-linecap="round"/>',
    ),
    "shopping": (
        "#4e243c",
        "#bd4d7b",
        '<path d="M112 190h288l-22 235H134z" fill="none" stroke="white" stroke-width="26" stroke-linejoin="round"/>'
        '<path d="M186 207v-36c0-48 28-83 70-83s70 35 70 83v36" fill="none" stroke="white" stroke-width="24" stroke-linecap="round"/>'
        '<path d="m256 252 20 42 47 6-35 32 9 46-41-22-41 22 9-46-35-32 47-6z" fill="white"/>',
    ),
    "sports": (
        "#173f31",
        "#2f9b6b",
        '<circle cx="256" cy="256" r="163" fill="none" stroke="white" stroke-width="24"/>'
        '<path d="m256 176 76 55-29 89h-94l-29-89zM256 93v83M101 206l79 25M411 206l-79 25M158 388l51-68M354 388l-51-68" fill="none" stroke="white" stroke-width="20" stroke-linejoin="round"/>',
    ),
}


def render_icon(name: str, start: str, end: str, body: str) -> str:
    title = name.replace("-", " ").title()
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-labelledby="title desc">
  <title id="title">{title} channel icon</title>
  <desc id="desc">Original SKY TV fallback artwork for {title.lower()} channels.</desc>
  <defs><linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="{start}"/><stop offset="100%" stop-color="{end}"/></linearGradient></defs>
  <rect width="512" height="512" rx="72" fill="url(#bg)"/>
  {body}
</svg>
'''


def render_png(source: Path, destination: Path) -> None:
    """Rasterize one SVG for IPTV players that do not display SVG artwork."""
    inkscape = shutil.which("inkscape")
    if not inkscape:  # pragma: no cover - depends on the developer workstation
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
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("assets/logos/generated"),
    )
    parser.add_argument(
        "--svg-only",
        action="store_true",
        help="Skip the broadly compatible PNG copies.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, (start, end, body) in sorted(ICONS.items()):
        svg = render_icon(name, start, end, body)
        svg_destination = args.output_dir / f"category-{name}.svg"
        svg_destination.write_text(svg, encoding="utf-8")
        if not args.svg_only:
            render_png(svg_destination, args.output_dir / f"category-{name}.png")
    formats = "SVG" if args.svg_only else "SVG and PNG"
    print(f"Generated {len(ICONS)} category icons ({formats}) in {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
