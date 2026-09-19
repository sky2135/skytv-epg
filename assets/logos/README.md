# Optional locally hosted channel logos

Put only the logo files you are permitted to redistribute in this directory. Subdirectories are allowed.

The build copies supported files to `public/logos/`, and a row in `config/channel_icons.csv` can refer to one with the `local_file` column.

Supported extensions: PNG, JPG/JPEG, WebP, GIF, and SVG.

Do not copy an entire third-party logo repository here. Keep a small, reviewed set of exact logos needed by your lineup. Record the source, author, license or usage terms, and retrieval date in `ATTRIBUTION.md`.

Original background-free category artwork under `generated/` is released under
CC0 1.0. See `GENERATED_ART_LICENSE.md`. Third-party portrait cutouts under
`people/` keep the individual licenses listed in `ATTRIBUTION.md` and
`icon_catalog.csv`.

## Regeneration tools

The committed SVG files are the vector masters. Workflow 2 uses their
transparent PNG renders because IPTV clients vary in SVG support. Regenerating
both formats is an offline maintenance task; `generate_category_icons.py`
requires the `inkscape` command.
