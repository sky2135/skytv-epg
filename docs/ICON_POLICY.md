# XMLTV icon policy

Channel logos are metadata only. They never influence Smart Rules matching.

## Exactness

The system accepts an icon only through an exact provider stream ID, exact EPG
ID, exact provider channel name, exact mapping-row URL, or the exact
`<channel id>` in a source XMLTV file. When an override supplies more than one
identity field, every field must match. It does not infer a logo from a similar
filename or fuzzy channel name. Person images and other 24/7 artwork must use
an exact server and stream ID; a shared dummy EPG ID is never sufficient.

## Hosting

Effective order:

1. safe URL supplied in the private mapping row;
2. exact reviewed manual override;
3. approved exact-subject portrait, or a transparent movie/music symbol when
   that named person has no approved portrait;
4. safe source XMLTV icon;
5. transparent category fallback when no usable source icon exists.

Movie fallbacks use five vector motifs. Numbered members of the same normalized
name pattern share one motif. Music fallbacks use notes, microphone,
headphones, guitar, dhol, or sitar according to the channel/category wording.
The committed SVGs are the editable vector masters; transparent PNG renders are
published for broader IPTV-player support.

Exact per-stream rows are derived from the private mapping only during the
workflow run. They stay under `.build/` and are never committed or uploaded.
The checked-in catalogs contain public subjects and assets, not provider stream
bindings.

Do not mirror an entire third-party logo repository. Keep only the subset needed by the approved lineup and record attribution and permission.

## Security

The icon layer accepts only absolute HTTP(S) URLs, rejects URLs containing embedded credentials, rejects parent-directory local paths, and copies only supported image extensions.
