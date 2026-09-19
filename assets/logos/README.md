# Optional locally hosted channel logos

Put only the logo files you are permitted to redistribute in this directory. Subdirectories are allowed.

The build copies supported files to `public/logos/`, and a row in `config/channel_icons.csv` can refer to one with the `local_file` column.

Supported extensions: PNG, JPG/JPEG, WebP, GIF, and SVG.

Do not copy an entire third-party logo repository here. Keep a small, reviewed set of exact logos needed by your lineup. Record the source, author, license or usage terms, and retrieval date in `ATTRIBUTION.md`.

Original background-free category artwork under `generated/` is released under
CC0 1.0. See `GENERATED_ART_LICENSE.md`. Third-party portrait cutouts under
`people/` keep the individual licenses listed in `ATTRIBUTION.md` and
`icon_catalog.csv`.

The generated v3 family is intentionally minimalist: every symbol has a solid
off-white (`#F7F8FA`) interior, a dark (`#1B2230`) rounded outline, and a
transparent outer canvas. It contains no gradients, shadows, text, or coloured
background cards. Open details use an off-white inner stroke with a dark outer
edge so they remain filled rather than hollow.

## Regeneration tools

The committed SVG files are the vector masters. Workflow 2 uses their
transparent PNG renders because IPTV clients vary in SVG support. Regenerating
both formats is an offline maintenance task; `generate_category_icons.py`
requires the `inkscape` command.
