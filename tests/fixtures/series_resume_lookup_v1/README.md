# Shared synthetic resume lookup fixtures

These are offline wire fixtures for the additive optional publisher contract.
They do not represent hosted data, real viewing history or successful provider
playback. The ordinary series manifest is synthetic and exists to bind and
validate the lookup's complete source projection.

- `accepted_descriptor.json`: accepted server, ordinary series reference,
  lookup reference and publication cutoff. All referenced files are in
  `artifacts/`. The descriptor is a test harness envelope, not a new wire schema.
- `schema.json`: exact JSON Schema 2020-12 definitions for roots, both children,
  references and records. The `root.schema.json`, `parent.schema.json` and
  `episode.schema.json` wrappers select one definition. Resolve their local
  relative reference; no internet access is needed.
- `source_records.json`: logical final source parents with all inline episode
  objects. The equivalent ordinary publication graph externalizes one parent's
  episode into a checksum-linked episode artifact, exercising input traversal.
- `hash_vectors.json`: exact UTF-8 bytes, full hashes and slots, plus invalid-ID
  examples. `(1,23)` and `(12,3)` protect tuple boundaries; case, leading zeroes
  and server scope are separate identities.
- `resolution_cases.json`: current membership/status/time decisions before
  local account, adult, language, hidden and parental guards. Positive inventory
  evidence is not permission to bypass those guards. Unknown adult is explicit.
- `negative_cases.json`: complete checksum-linked malformed graphs. All use the
  accepted descriptor's server/ordinary manifest. Their listed rejection layer
  distinguishes consumer checks from publisher full-source parity/coverage.
- `root_only_change.json`: valid alternate lookup graph with identical source
  series SHA and facts but a different checksum-linked child filename and root
  SHA. The app must retire former lookup evidence.
- `unsupported_descriptors.json`: optional contract absence/removal.
- `generation_summary.json`: counts, root reference and reviewed proposal SHA.
- `checksums.json`: every fixture file's exact bytes and SHA-256, excluding
  itself. This is an interoperability inventory, not a publication signature.

`generate.py` is a standard-library fixture preparation helper. It writes only
within this folder. Consumers should use the checked-in bytes and golden values;
they should not regenerate expected hashes with the same implementation they
are testing. Re-run the helper after intentional edits to refresh its checksum
inventory. It does not download data, call APIs, run workflows or edit app code.

The normative text is `docs/SERIES_RESUME_LOOKUP_V1.md`. JSON Schema alone does
not implement sorting, exact tuple hashing, graph binding, root/child counts,
full published-source parity, inventory coverage, accepted-cutoff comparison
or current app policy. Those independent checks remain mandatory.
