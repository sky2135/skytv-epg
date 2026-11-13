# Local series-resume publisher integration

This isolated patch is default-off and unpublished. It preserves the frozen v1
primitives/fixtures and excludes unrelated movie-credit edits. Native acceptance,
account-configured secret scanning and a hosted rollout remain separate gates.

## Candidate attachment and validation

`series_resume_publication.py attach --app-dir APP --index-sha256 PIN` preserves
bytes by default. Explicit `--enabled` generates all graphs against FINAL series
references, deeply validates every source/projection and collision before install,
then atomically switches the index last. A private, exclusively owned candidate is
required; the sibling lock serializes cooperating attaches, not unrelated writers.
Failures can leave harmless unadvertised immutable files; the old index stays
accepted. All ordinary source bytes/times remain unchanged.

`series_resume_publication.py validate --app-dir APP` strictly checks every present
optional graph and permits absence. Add `--require-lookup` to require all expected
servers. This supplements ordinary checks in `check_publication.sh vod` and `all`;
the permissive ordinary validator and existing publish lock/restorers are unchanged.

`app_catalog_closure.py` retains explicitly selected COMPLETE current and last-good
current graphs, including external episodes. A small catalogue-only retention record
may additionally hold ONE validated compensation closure through one following
distinct app generation. EPG-only composition and idempotent preparation do not
consume that hold. No historical-file glob or transitive generation inheritance is
used. After full graph/hash/collision/cap preflight, only an exclusively owned PRIVATE
candidate may discard unselected hashed files; original, last-good and hosted data
are never edited by preparation. Retention still runs with attachment off.

Caps cover the actual selected output and its retention record. Transfer checks
include all possible directory entries plus snapshot manifest/report wrappers under
the existing 20,000-member extractor cap (flat two-directory payload limit19,994).
Final publication also keeps its existing 900MiB safety cap. Older cached descriptors
outside the explicit retention window must refresh; indefinite old-generation
availability is not promised. Native error handling must preserve saved progress.

## Offline prepared bundle / compensation

Use complete candidate public VOD+app files and a separately verified last-good app.
Destinations must be NEW/separate; pins are exact SHA256 of local index bytes.

```text
python scripts/prepare_series_resume_component.py prepare --candidate-public-dir CANDIDATE_PUBLIC --last-good-app-dir LAST_GOOD_APP --output-dir NEW_BUNDLE --index-sha256 CANDIDATE_INDEX_SHA --last-good-index-sha256 LAST_GOOD_INDEX_SHA --enabled
```

Omit `--enabled` to preserve input refs without generating new ones. Use mutually
exclusive `--compensate` to remove optional references only. Bad optional references
can be withdrawn when the ordinary graph/stored bytes are valid; corrupt stored
bytes require a clean source. Last-good and final ordinary/strict checks stay on.
When reusing a prior prepared bundle, its record is validated against the original
pinned input before attachment/withdrawal. That authorized lookup-only transformation
explicitly transfers the record owner while preserving its bounded anchor; arbitrary
owner mismatches or ordinary-reference changes are rejected before mutations.
Inputs are never edited. The output is `snapshot/{snapshot.json,files/**}` plus
`report.json`; the report is informational, not the materialization trust root.

```text
python scripts/prepare_series_resume_component.py materialize --bundle-dir BUNDLE --output-dir NEW_PUBLIC --snapshot-sha256 REVIEWED_SNAPSHOT_MANIFEST_SHA
```

Materialization verifies the supplied snapshot seal and every file independently,
then validates the full ordinary and optional graphs. Both commands report
`readyForPublication:false` and `productionSecretScanPerformed:false`.

## Future workflow wiring in this patch

Normal VOD builds always retain the explicitly selected required closures. New graph generation requires
the explicit repository variable `SERIES_RESUME_LOOKUP_ENABLED=true`; unset/false
is off. The restore step passes the existing configured `EPG_PUBLIC_BASE_URL`, so
an empty/expired eligible-artifact pool can fall back to the hosted last-good graph.
Approved persistent rollout must intentionally enable generation for later daily
builds. A rollback turns it off before producers resume.

Optional manual inputs `prepared_component_artifact_id` and
`prepared_snapshot_sha256` use a separate provider-free job. Its reviewed private
artifact must be named `series-resume-preparation-<review-name>`, which is ineligible
for automatic production restoration. The job checks identity/name/pin, materializes,
runs existing configured secret and ordinary+strict checks, and seals a FRESH eligible
VOD component. It skips normal provider/TMDB/cache work. Existing trusted build-job
name and shared publisher are reused; cancellation prevents the publish job.

Every successful production upload is immediately eligible, even before deployment.
Future rollout must therefore pause triggers, drain all queued/running builders and
publishers, and review the exact component selections/hashes of both restorers.
Use a NEW validated compensating component, not an old rerun. Keep producers drained
through verification/compensation and disable unapproved attachment before resuming.
No epoch fence or two-phase redesign is included.

## Remaining limits

- Review/apply this isolated patch to the current approved source. No commit/push,
  production job pause/dispatch or activation occurred during preparation.
- Complete native/older-client acceptance, including bounded retries after transient
  Unknown. Server1 zero unchecked inventory does not disprove prior playback.
- Exercise GitHub job routing/configured secret scan/Pages deployment after separate
  approval; local tests execute the new inline Python boundaries, not GitHub itself.
- Validate final composed payload size and the explicit retention window. After eventual
  activation, perform bounded public full-closure/hash checks and re-read each
  server's series/Home/root ownership tuple. EPG-only composition can change index
  bytes without changing lookup ownership. No universal CDN atomicity is promised.
