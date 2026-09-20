# Optional locally hosted channel logos

Put only the logo files you are permitted to redistribute in this directory. Subdirectories are allowed.

The build copies supported files to `public/logos/`, and a row in `config/channel_icons.csv` can refer to one with the `local_file` column.

Supported extensions: PNG, JPG/JPEG, WebP, GIF, and SVG.

Do not copy an entire third-party logo repository here. Keep a small, reviewed set of exact logos needed by your lineup. Record the source, author, license or usage terms, and retrieval date in `ATTRIBUTION.md`.

Original background-free category artwork under `generated/` is released under
CC0 1.0. See `GENERATED_ART_LICENSE.md`. Third-party portrait cutouts under
`people/` keep the individual licenses listed in `ATTRIBUTION.md` and
`icon_catalog.csv`.

Named-person source research is recorded in
`named_person_portrait_source_audit.csv` and summarized in
`docs/NAMED_PERSON_PORTRAIT_SOURCE_AUDIT.md`. Research approval does not add an
image to production; only a finished, reviewed cutout belongs in
`icon_catalog.csv`.

The 2026-09-19 production pass accepted 100 new portrait cutouts; 33 processed
candidates remain on the neutral role fallback after output QA.

The 2026-09-20 follow-up re-reviewed the 49 conditional source rows. Twenty-seven
sources are now approved and 22 remain conditional. Source approval means only
that the exact identity, provenance, reuse terms, and a viable crop were cleared;
it does not put a portrait in the TV guide. Every new cutout must separately pass
source-fidelity and visual QA and then be added to `icon_catalog.csv` and
`ATTRIBUTION.md`. Until that happens, the neutral role fallback remains active.

Six follow-up portraits passed that separate output review: Akhil, Baljit Malwa,
Boman Irani, Farhan Saeed, Guri, and Naseeruddin Shah. The other 43 members of
the original conditional set still use the neutral role fallback: 21 have a
cleared source but no faithful output, and 22 still have an unresolved source.
Production now contains 113 exact-name person portraits in total.

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
