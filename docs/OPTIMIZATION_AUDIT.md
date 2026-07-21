# Runtime and architecture audit

## Unchanged compatibility/builder engine

`src/skytv_epg_engine.py` is still the byte-for-byte assembly of the two engine
cells from the supplied v7.1 notebook. Its SHA-256 is recorded in
`MATCHER_INTEGRITY.json`. Builder v7.1 and all legacy-v7 regression cases remain
unchanged.

## New algorithm boundary

Smart Rules v8 is implemented in `src/skytv_epg_contextual_v8.py`. It owns the
new parser, evidence routing, structural indexes, compatibility gates, reusable
knowledge, schedule fingerprints, diagnostics and fuzzy-review policy.

This is preferable to editing many historical v4–v7 functions in place: old
behavior remains testable as a subsystem, while the active algorithm has one
coherent decision flow.

## Efficiency changes

1. Independent EPGShare text catalogs download concurrently.
2. Results are processed in original registry order, preserving deterministic
   candidate order and existing tie-breaking.
3. The legacy v7 index is cached by a full catalog fingerprint.
4. v8 builds structural indexes once per full catalog fingerprint.
5. The self-test and real server scan reuse the same v8 index when catalogs are
   identical.
6. The three-server production builder downloads each distinct XMLTV source
   once and shares it across server builds.
7. GitHub invokes Python with `-u` and `PYTHONUNBUFFERED=1`, so long downloads
   show live progress.
8. Programme-schedule comparison is targeted/opt-in rather than downloading all
   full XML feeds during every ordinary match.

## Determinism and safety

- Fuzzy-only results cannot be automatic.
- Explicit diaspora routing cannot fall back to India.
- Meaningful edition fields are compatibility constraints.
- Alias memory is catalog-revalidated.
- Scheduled builds do not perform matching.
- Integrity hashes and regression tests run before every GitHub build.
