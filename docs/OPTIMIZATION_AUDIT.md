# SKY TV EPG Version 1 — optimization audit

This is the final Version 1 decision record. The setup steps are in
[`START_HERE_VERSION_1.md`](START_HERE_VERSION_1.md).

## Decisions

| Proposal | Version 1 decision | Reason |
|---|---|---|
| Check only EPGShare `ETag`/`Last-Modified` and skip the run | Not used | The private Google Sheet and Server 2/3 panel guides are independent inputs. A safe skip would need persistent validators and a reusable validated output for every input, not one `HEAD` request. |
| Replace `lxml.etree.iterparse` with `pyexpat` | Not used without a representative benchmark | Both parsers are C-backed and streaming. Generic claims of 2–4× speed or a fixed memory reduction are not evidence for this workload. The tested `iterparse` implementation clears records immediately and enforces structural limits. |
| Put SQLite in `/dev/shm` with sync disabled | Not used | A RAM disk consumes the same finite runner memory needed for parsing and compression. Disk-backed SQLite keeps memory bounded, survives multiple streaming output passes, and uses safe temporary staging. |
| Split gzip/XML text at `</programme>` and parse chunks in multiple processes | Rejected | A gzip stream is sequential, and raw delimiter splitting can break valid XML namespaces, comments, CDATA, ordering, and malformed-input detection. Copying chunks also adds memory and process overhead. |
| Replace every JSON write with `orjson` | Not used without profiling | The large app files are already streamed directly into deterministic gzip writers. A faster serializer cannot be assumed to improve end-to-end time when download, XML parsing, SQLite, and compression are also involved. |
| Cache the Google Sheet as Parquet/SQLite | Not used | The Sheet is the live authority and is only about 25,000 starter rows. Reading it once per run ensures edits are honored and avoids stale-cache recovery rules. |
| Keep generated XML/JSON out of Git history | Implemented | GitHub Pages receives an Actions artifact directly. No generated guide is committed to `main` or a `gh-pages` branch. |

## Implemented efficiency and safety controls

- The combined EPGShare gzip is downloaded once and decompressed as a stream.
- XML is parsed once with `lxml.etree.iterparse`; completed elements are cleared.
- Only selected channels and programmes are staged in disk-backed SQLite.
- XMLTV, app JSON, and personalization metadata are written as streams.
- Gzip output is deterministic, so identical inputs produce identical bytes.
- Download sizes, expanded sizes, XML records, Sheet rows, SQLite batches,
  output sizes, wall time, and process memory are bounded.
- The workflow applies a 6 GiB process-memory ceiling to catch regressions.
- Version 1 dependency versions and third-party GitHub Actions are pinned.
- Outputs are published only after syntax, gzip, mapping, coverage, path,
  symlink, hard-link, size, and configured-secret checks succeed.

## GitHub cost and capacity conclusion

The repository can use GitHub's standard hosted runner while it remains public
and within GitHub's current Actions and Pages limits. This is not a promise that
every private-repository plan, Pages configuration, traffic level, or future
GitHub policy is free. The workflow records actual runner memory and disk at
the start of each run and fails early if safe capacity is unavailable.

GitHub Pages in this setup is for personal, noncommercial distribution. Move
the generated files to object storage plus a CDN before using them for a paid
customer service, sustained high traffic, or payloads that approach Pages
limits.

## What to measure before any later optimization

Use the workflow's `resource-usage.txt`, elapsed time, maximum resident memory,
download sizes, SQLite size, and final compressed sizes from several real runs.
Change one component at a time and retain it only when end-to-end measurements
improve without weakening deterministic output, input validation, or the
Server 1 EPGShare-only boundary.
