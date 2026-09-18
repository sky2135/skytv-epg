# Matching Lab phase-one operations

## Purpose and safety boundary

The Matching Lab is a private, deterministic shadow runner for unresolved
EPGShare mappings. It reads four frozen local files and writes a proposal
bundle. By default it does not fetch a Google Sheet, contact a provider, call
Gemini, change a mapping, publish EPG output, or invoke Workflow 2. An optional
local `--use-ai` mode can attach advisory evidence through the existing
hardened Gemini client; the Cloud Run phase-one deployment below leaves that
mode disabled and supplies no AI secret.

The container always starts this command:

```text
python -m matching_lab shadow
```

The phase-one outputs are evidence for later review. Even a proposal whose
state is `AUTO_ELIGIBLE` has `auto_apply_eligible=false`; no file produced by
this job is permission to update the production Sheet. The separate guarded
human-approval command described below is downstream of the Lab and is not
available inside the shadow container.

Enforce the boundary twice:

1. Do not share the production Google Sheet with the Cloud Run runtime service
   account and do not give the job a Google credential, provider credential,
   Gemini key, GitHub token, or deployment credential.
2. Give the runtime identity read-only access to a private input bucket and
   object read/write access only to a separate private output bucket.

Workflows 1 and 2 (`.github/workflows/channel_inventory_sync.yml` and
`.github/workflows/main.yml`) are outside this deployment and remain unchanged.

## Inputs and outputs

Each run needs one immutable snapshot directory containing:

| Argument | Required file | Content |
|---|---|---|
| `--mappings-csv` | `mappings.csv` | Private `Mappings` tab export in the existing schema |
| `--alerts-csv` | `sync_alerts.csv` | Private `Sync Alerts` tab export |
| `--epg-xml` | `epgshare.xml.gz` | Exact EPGShare `ALL_SOURCES1` XML or XML.GZ snapshot |
| `--epg-text` | `epgshare.txt` | Official text catalog captured with the XML snapshot |
| `--as-of` | UTC timestamp | Evidence time associated with that frozen snapshot |

`--alerts-csv` is technically optional, but normal runs must provide it.
Omitting it adds `ALERT_SNAPSHOT_MISSING` and prevents a proposal from reaching
the strongest evidence tier.

Use the same `--as-of` value when replaying the same input bytes. It must be an
ISO-8601 UTC value such as `2026-09-17T03:00:00Z`; do not use the current time
for a historical snapshot.

The output directory receives exactly these private artifacts:

- `proposals.jsonl`: one bounded, explainable decision per eligible REVIEW row;
- `summary.json`: aggregate counts without private row details;
- `manifest.json`: run identity plus hashes binding the inputs, policy, code,
  proposals, and summary.

Channel names, stream IDs, candidates, and mapping evidence in
`proposals.jsonl` are private. Never upload the bundle as a public GitHub
artifact or commit it to the repository.

An optional SQLite observation ledger and AI replay cache must be stored
outside the bundle directory. They are private operational state, not bundle
artifacts.

Capture all four inputs as one named snapshot: after a successful Workflow 1
run, export the complete `Mappings` and `Sync Alerts` tabs, download the
official `ALL_SOURCES1` XML.GZ and TXT consecutively with the repository's
bounded download helper, record the UTC capture time as `--as-of`, and hash the
files before upload. Keep these comparison artifacts with the run record (they
are not runner inputs): Workflow 1's
`skytv-channel-sync-summary-<run>-<attempt>/summary.json`, Workflow 2's
`skytv-epg-diagnostics-<run>-<attempt>` artifact, and Workflow 3's
`skytv-backlog-analysis-<run>-<attempt>/summary.json`.

## Values you must supply

Code and image creation do not need credentials. A Cloud Run deployment needs
the operator to choose these values:

| Variable used below | Required value |
|---|---|
| `PROJECT_ID` | Google Cloud project with billing enabled |
| `REGION` | Cloud Run and Artifact Registry region |
| `AR_REPOSITORY` | Existing private Docker Artifact Registry repository |
| `IMAGE_TAG` | Immutable revision tag, preferably the Git commit SHA |
| `JOB_NAME` | New dedicated Cloud Run Job name; never reuse an existing job |
| `RUNTIME_SA` | Dedicated runtime service-account email |
| `INPUT_BUCKET` | Private bucket containing frozen snapshots |
| `OUTPUT_BUCKET` | Different private bucket for proposal bundles |
| `SNAPSHOT_PREFIX` | Exact immutable input prefix for this run |
| `RUN_PREFIX` | New, unused output prefix for this run |
| `AS_OF_UTC` | Frozen snapshot timestamp in UTC |

Optional local AI remains off unless the owner explicitly approves sending
bounded provider channel/category names and candidate display names to Gemini.
Real EPG IDs, stream IDs, credentials, and free-form model prose are not sent.

For the Cloud Run phase-one job, no secret value belongs in a command-line
argument, environment variable, container image, build substitution, log, or
this repository. It does not require Secret Manager because AI is disabled and
the job consumes no application secret. Optional local AI uses the Gemini key
only in the transient process environment, never as a CLI argument or artifact.

## Local Python check

Install the repository's pinned dependencies into a disposable virtual
environment, then inspect the CLI:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: \
  -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m matching_lab --help
```

Run the shadow command only against private local files:

```bash
umask 077
mkdir -p .build/matching-lab/local-output
.venv/bin/python -m matching_lab shadow \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --epg-xml /absolute/private/snapshot/epgshare.xml.gz \
  --epg-text /absolute/private/snapshot/epgshare.txt \
  --as-of 2026-09-17T03:00:00Z \
  --output-dir .build/matching-lab/local-output \
  --ledger .build/matching-lab/history.sqlite3
```

Repeat the non-AI command into a second empty output directory with the same
inputs and `--as-of`. The three output files should be byte-identical. A
changed input, changed policy/code/dependency, or changed evidence time must
produce a different run identity.

Validate the canonical bundle, hashes, expiry, decisions, and current private
row/alert guards without writing anything:

```bash
.venv/bin/python -m matching_lab validate \
  --bundle-dir .build/matching-lab/local-output \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --as-of 2026-09-17T03:01:00Z
```

## Record an exact human approval offline

Human approval is stored in a separate, private, content-addressed JSON file.
It is not a spreadsheet import and it is not permission to approve a future
run. The approval is bound to the exact Lab run, manifest, proposal file,
policy/code hashes, proposal IDs, Mapping row/provider guards, Mapping snapshot,
and Sync Alerts snapshot.

Record approval only while the validated bundle is unexpired. The approval time
must be the canonical UTC time at which the owner made the decision:

```bash
umask 077
mkdir -p .build/matching-lab/private-approvals

.venv/bin/python scripts/matching_lab_approvals.py approve-strong \
  --bundle-dir .build/matching-lab/local-output \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --output-file .build/matching-lab/private-approvals/approval.json \
  --approved-at 2026-09-17T03:05:00Z
```

`approve-strong` includes only this run's `MULTI_SIGNAL_STRONG_PROPOSAL`
records. Omitting `--reviewer-notes` records an intentionally blank reviewer
note. It does not erase any existing automated note in Mappings: canary
preparation prepends exact system provenance and retains the old note byte for
byte after it.

Validate the approval again against the intact bundle and exact current
Mapping/alert snapshots before using it:

```bash
.venv/bin/python scripts/matching_lab_approvals.py validate \
  --approval-file .build/matching-lab/private-approvals/approval.json \
  --bundle-dir .build/matching-lab/local-output \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --as-of 2026-09-17T03:06:00Z
```

The approval file contains private stream IDs, targets, and row guards. Keep it
outside source control and public GitHub artifacts, with the same controls as
`proposals.jsonl`.

## Prepare a 25-row canary without writing

The standalone canary command validates both sides of preparation, selects the
highest-score/highest-margin uncommitted approvals deterministically, and emits
at most 25 full desired Mapping rows:

```bash
.venv/bin/python scripts/apply_matching_lab_approvals.py prepare \
  --approval-file .build/matching-lab/private-approvals/approval.json \
  --bundle-dir .build/matching-lab/local-output \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --output-file .build/matching-lab/private-approvals/canary-01.json \
  --as-of 2026-09-17T03:06:00Z \
  --limit 25
```

The `prepare` subcommand performs no Google Sheets or provider access. Its
batch declares `write_authority=false` and all four live revalidation
requirements; do not paste or upload its rows directly into Mappings. Any
different `APPROVED` row fails closed.

## Apply one guarded live canary

`scripts/apply_matching_lab_approvals.py apply` is a separate, narrow writer;
the Matching Lab package itself remains proposal-only. Each invocation requires
the exact original Mapping, Sync Alerts, XML.GZ, and TXT files used by the
approved run, the intact bundle and approval, and an explicit confirmation of
the approval's complete 64-character lowercase ID. It accepts no abbreviated
ID and applies at most 25 rows.

Run it only on a secure runner after an operator has independently reviewed the
full approval ID:

```bash
# Set this manually from the validated private approval, not from a shortened log.
CONFIRMED_APPROVAL_ID="replace-with-reviewed-64-character-lowercase-id"

.venv/bin/python scripts/apply_matching_lab_approvals.py apply \
  --approval-file .build/matching-lab/private-approvals/approval.json \
  --bundle-dir .build/matching-lab/local-output \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --all-source-file /absolute/private/snapshot/epgshare.xml.gz \
  --all-source-catalog-file /absolute/private/snapshot/epgshare.txt \
  --sheet-id "$GOOGLE_SHEET_ID" \
  --sheet-tab Mappings \
  --alerts-tab "Sync Alerts" \
  --confirm-approval-id "$CONFIRMED_APPROVAL_ID" \
  --limit 25
```

The secure runner must inject `GOOGLE_SERVICE_ACCOUNT_JSON` and the selected
servers' `SERVER_N_BASE_URL`, `SERVER_N_USERNAME`, and `SERVER_N_PASSWORD`
values from its secret manager. Never put these values in a command argument,
repository, artifact, log, workbook, or chat. Do not run live apply from the
credential-free shadow Cloud Run Job.

The live path fails closed unless all of these checks succeed:

1. The full confirmation ID equals the canonical approval ID, and the approval
   and bundle are unchanged and unexpired at the actual write time.
2. The four supplied local inputs match the exact approved-run hashes. The
   XML/TXT catalogs are reparsed, exact target membership and case safety are
   recomputed, and every selected ID passes the programme gate at current time.
   This check must finish within the enforced ten-minute freshness window.
3. The live provider contains the same stream name, category name, and category
   ID. Provider identity is checked initially and freshly revalidated again
   before the Sheet mutation.
4. The live Mapping table is either the original snapshot or that snapshot plus
   exact prior commits from this same approval. Unrelated edits, row movement,
   partial provenance, or target drift stop the entire invocation.
5. Immediately before writing, one authoritative `batchGet` rereads the full
   Mappings and Sync Alerts tabs. The selected preimages must be unchanged and
   none may have a current OPEN alert.
6. All selected changes are sent in one atomic Google Sheets `batchUpdate`.
   The writer then authoritatively rereads the full Mapping table and reconciles
   every intended row and every untouched row. An uncertain response is never
   treated as permission for a blind retry.

Resumption is allowed only with the same exact approval, bundle, four run
inputs, and still-unexpired run. A later invocation verifies and skips rows with
the exact approval/run/proposal provenance, then selects the next score/margin
prefix of at most 25. After expiry, or after any unrelated Mapping drift, create
a new shadow run and approval instead of bypassing a guard.

The CLI does not acquire GitHub Actions concurrency by itself. Before any live
deployment, wrap it in a manual secure job that holds the same non-cancelling
`${{ github.repository }}-skytv-epg-v1` concurrency group as Workflows 1 and 2
for the entire validation, write, and postread interval. An equivalent external
lock is safe only if those workflows also honor it. Never run this command while
either production workflow or another approval invocation can access the Sheet.

No live `apply` command was executed during this implementation and review; no
Google Sheet row was changed here.

These standalone commands do not dispatch or modify
`.github/workflows/channel_inventory_sync.yml` or
`.github/workflows/main.yml`; the original production workflows remain the
authority and remain unchanged.

Optional AI review is local/manual in phase one. It requires a durable cache so
the same request is not sent or charged twice. The cache namespace is bound to
the run identity, so use the same intact, unchanged cache for byte-identical
replay. Missing or changed rows fail its local checkpoint; deleting the cache
file is an explicit new AI attempt and produces a different run identity. The
model receives only bounded channel context and opaque candidate keys; it cannot
promote a decision or grant apply authority. Keep both files outside the bundle
directory:

```bash
read -rsp "Gemini API key: " GEMINI_API_KEY
export GEMINI_API_KEY
.venv/bin/python -m matching_lab shadow \
  --mappings-csv /absolute/private/snapshot/mappings.csv \
  --alerts-csv /absolute/private/snapshot/sync_alerts.csv \
  --epg-xml /absolute/private/snapshot/epgshare.xml.gz \
  --epg-text /absolute/private/snapshot/epgshare.txt \
  --as-of 2026-09-17T03:00:00Z \
  --output-dir .build/matching-lab/ai-output \
  --use-ai \
  --ai-cache .build/matching-lab/ai-replay.sqlite3 \
  --ledger .build/matching-lab/history.sqlite3
unset GEMINI_API_KEY
```

## Build and test the container locally

`Dockerfile.dockerignore` is specific to this Dockerfile. It keeps private
exports, generated output, repository history, tests, and local credentials
out of Docker's build context. `gcloudignore` separately restricts the source
archive uploaded by `gcloud builds submit`; always pass it explicitly.

```bash
docker build \
  --file deploy/matching-lab/Dockerfile \
  --tag skytv-matching-lab:local \
  .

docker run --rm skytv-matching-lab:local --help
```

For an offline shadow run, use absolute host paths. The root filesystem and
input mount remain read-only, the container has no network, and only the output
mount is writable:

```bash
umask 077
mkdir -p .build/matching-lab/container-output

docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=2g \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src=/absolute/private/snapshot,dst=/input,readonly \
  --mount type=bind,src="$PWD/.build/matching-lab/container-output",dst=/output \
  skytv-matching-lab:local \
  --mappings-csv /input/mappings.csv \
  --alerts-csv /input/sync_alerts.csv \
  --epg-xml /input/epgshare.xml.gz \
  --epg-text /input/epgshare.txt \
  --as-of 2026-09-17T03:00:00Z \
  --output-dir /output
```

Inspect `manifest.json` and `summary.json` first. Keep `proposals.jsonl`
private.

## Build the image with Cloud Build

Enable Cloud Build, Artifact Registry, Cloud Run, and Cloud Storage APIs, and
create the private Artifact Registry repository before the first build. The
operator running the commands needs permission to submit builds, deploy a
Cloud Run Job, and act as the selected runtime service account.

Confirm the identities separately before building:

- the active deployer can submit Cloud Builds, deploy Cloud Run Jobs, act as
  the chosen build/runtime service accounts, read Artifact Registry metadata,
  and use the project services;
- the Cloud Build service account has Artifact Registry Writer on the one
  target repository (plus the logging/storage access selected for that build);
- the Cloud Run service agent can pull the image; and
- the runtime service account has only the storage roles described below.

The `gcloud artifacts docker images describe` command may additionally require
Artifact Registry Reader, Container Analysis metadata access, and Service Usage
Viewer for the active deployer. Grant roles at the narrowest repository/project
scope your organization supports; do not give the runtime identity any of
these build/deploy roles.

Set shell variables without putting any credential in them:

```bash
PROJECT_ID="your-project-id"
REGION="us-central1"
AR_REPOSITORY="skytv"
IMAGE_TAG="replace-with-immutable-revision"

# Review this list before uploading source. It must not contain a snapshot,
# generated proposal, credential, mapping export, or unrelated repository file.
gcloud meta list-files-for-upload . \
  --ignore-file deploy/matching-lab/gcloudignore

gcloud builds submit . \
  --project "$PROJECT_ID" \
  --config deploy/matching-lab/cloudbuild.yaml \
  --ignore-file deploy/matching-lab/gcloudignore \
  --substitutions "_LOCATION=$REGION,_REPOSITORY=$AR_REPOSITORY,_IMAGE_NAME=matching-lab,_TAG=$IMAGE_TAG"

IMAGE_REPOSITORY="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPOSITORY/matching-lab"
IMAGE="$IMAGE_REPOSITORY:$IMAGE_TAG"
IMAGE_DIGEST="$(gcloud artifacts docker images describe "$IMAGE" \
  --project "$PROJECT_ID" \
  --format='value(image_summary.digest)')"
if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Could not resolve an immutable image digest." >&2
  exit 1
fi
DEPLOY_IMAGE="$IMAGE_REPOSITORY@$IMAGE_DIGEST"
```

The Dockerfile performs an import/CLI smoke check while building. The build
contains no private snapshot or credential. Dependency versions are pinned,
but the base-image and wheel hashes are not yet lockfile-pinned, so a later
rebuild is not guaranteed to be byte-identical. Retain Cloud Build provenance
and deploy the resolved `DEPLOY_IMAGE` digest, not the mutable tag.

## Runtime IAM and storage

Use two buckets with public-access prevention and uniform bucket-level access:

- input bucket: grant the runtime identity
  `roles/storage.objectViewer` only;
- output bucket: grant the runtime identity `roles/storage.objectUser` so the
  container can create the temporary `.part` files and rename them into the
  three final artifacts;
- Artifact Registry: ensure the Cloud Run service identity can read the image.

Do not grant the runtime identity any Google Sheets access or project-level
Editor/Owner role. Do not attach Secret Manager secrets. Do not reuse the
service account used by Workflow 1 or Workflow 2.

Use immutable object prefixes, for example:

```text
gs://INPUT_BUCKET/snapshots/2026-09-17T030000Z/
gs://OUTPUT_BUCKET/runs/2026-09-17T030000Z-first-shadow/
```

Upload the four private inputs from a trusted workstation or private capture
process. Verify their local hashes before and after upload. Apply a lifecycle
policy appropriate to the data: a common starting point is 30 days for raw
snapshots and 365 days for manifests/proposals, subject to your privacy policy.

## Create a new manual Cloud Run Job

Cloud Run mounts the input bucket read-only and the output bucket writable.
Cloud Storage volume mounts use Cloud Storage FUSE; they are not fully POSIX
and do not provide concurrency control for competing writes. Therefore:

- use one task, parallelism one, and zero automatic retries;
- use a new output prefix for every execution;
- never start two executions with the same output prefix;
- treat a failed prefix as incomplete and choose a new prefix for the rerun;
- trust a bundle only when all three files exist and the manifest hashes
  validate.

Set the user-supplied deployment values:

```bash
# Use a new, dedicated name for each deployment; do not update an older job.
JOB_NAME="skytv-matching-lab-shadow-20260917-v1"
RUNTIME_SA="matching-lab-runtime@$PROJECT_ID.iam.gserviceaccount.com"
INPUT_BUCKET="your-private-input-bucket"
OUTPUT_BUCKET="your-private-output-bucket"
SNAPSHOT_PREFIX="snapshots/2026-09-17T030000Z"
RUN_PREFIX="runs/2026-09-17T030000Z-first-shadow"
AS_OF_UTC="2026-09-17T03:00:00Z"
```

Fail if that name already exists. This prevents an older job's command,
environment, secret, or volume configuration from carrying into phase one:

```bash
if gcloud run jobs describe "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" >/dev/null 2>&1; then
  echo "Refusing to update existing Cloud Run Job: $JOB_NAME" >&2
  exit 1
fi
```

Deploy the new job without executing it. The clear/reset flags also document
and enforce the no-secret, image-entrypoint boundary:

```bash
gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$DEPLOY_IMAGE" \
  --service-account "$RUNTIME_SA" \
  --clear-env-vars \
  --clear-secrets \
  --command="" \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 2h \
  --cpu 4 \
  --memory 16Gi \
  --add-volume "mount-path=/mnt/input,type=cloud-storage,bucket=$INPUT_BUCKET,readonly=true,mount-options=uid=10001;gid=10001" \
  --add-volume "mount-path=/mnt/output,type=cloud-storage,bucket=$OUTPUT_BUCKET,readonly=false,mount-options=uid=10001;gid=10001" \
  --args="--mappings-csv,/mnt/input/$SNAPSHOT_PREFIX/mappings.csv,--alerts-csv,/mnt/input/$SNAPSHOT_PREFIX/sync_alerts.csv,--epg-xml,/mnt/input/$SNAPSHOT_PREFIX/epgshare.xml.gz,--epg-text,/mnt/input/$SNAPSHOT_PREFIX/epgshare.txt,--as-of,$AS_OF_UTC,--output-dir,/mnt/output/$RUN_PREFIX"
```

Inspect the deployed resource before its first execution. This check rejects
any non-empty command override, environment variables, Secret Manager wiring,
or image other than the resolved digest:

```bash
JOB_SPEC_JSON="$(mktemp)"
gcloud run jobs describe "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format=json > "$JOB_SPEC_JSON"

JOB_SPEC_JSON="$JOB_SPEC_JSON" DEPLOY_IMAGE="$DEPLOY_IMAGE" \
  .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

document = json.loads(Path(os.environ["JOB_SPEC_JSON"]).read_text(encoding="utf-8"))
images = []
problems = []

def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            folded = key.casefold()
            if folded == "image" and isinstance(child, str):
                images.append(child)
            if folded == "command" and child not in (None, "", []):
                problems.append("non-empty command override")
            if folded == "env" and child not in (None, [], {}):
                problems.append("environment variables")
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)

walk(document)
serialized = json.dumps(document, sort_keys=True).casefold()
for marker in ("secretkeyref", "secretmanager", "run.googleapis.com/secrets"):
    if marker in serialized:
        problems.append(f"secret configuration ({marker})")
if images != [os.environ["DEPLOY_IMAGE"]]:
    problems.append(f"unexpected image list: {images!r}")
if problems:
    raise SystemExit("Unsafe deployed job: " + "; ".join(sorted(set(problems))))
print("Deployed job has the expected image and no command/env/secret override.")
PY
```

The temporary job-spec file is not private input data, but remove it afterward
using your normal temporary-file cleanup procedure.

These resources are intentionally conservative for the first full backlog:
the repository permits an EPG input up to 1 GiB compressed and 4 GiB expanded.
Measure the first run before reducing CPU, memory, or timeout.

## Execute and verify manually

Start phase one manually; do not create a scheduler yet:

```bash
gcloud run jobs execute "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --wait

gcloud storage ls "gs://$OUTPUT_BUCKET/$RUN_PREFIX/"
gcloud storage cp "gs://$OUTPUT_BUCKET/$RUN_PREFIX/manifest.json" -
gcloud storage cp "gs://$OUTPUT_BUCKET/$RUN_PREFIX/summary.json" -
```

Download and validate the exact private objects before reviewing proposals.
This also checks the four source-file SHA-256 values recorded in the manifest:

```bash
VERIFY_DIR="$(mktemp -d)"
mkdir -p "$VERIFY_DIR/bundle" "$VERIFY_DIR/input"
gcloud storage cp "gs://$OUTPUT_BUCKET/$RUN_PREFIX/manifest.json" "$VERIFY_DIR/bundle/"
gcloud storage cp "gs://$OUTPUT_BUCKET/$RUN_PREFIX/summary.json" "$VERIFY_DIR/bundle/"
gcloud storage cp "gs://$OUTPUT_BUCKET/$RUN_PREFIX/proposals.jsonl" "$VERIFY_DIR/bundle/"
gcloud storage cp "gs://$INPUT_BUCKET/$SNAPSHOT_PREFIX/mappings.csv" "$VERIFY_DIR/input/"
gcloud storage cp "gs://$INPUT_BUCKET/$SNAPSHOT_PREFIX/sync_alerts.csv" "$VERIFY_DIR/input/"
gcloud storage cp "gs://$INPUT_BUCKET/$SNAPSHOT_PREFIX/epgshare.xml.gz" "$VERIFY_DIR/input/"
gcloud storage cp "gs://$INPUT_BUCKET/$SNAPSHOT_PREFIX/epgshare.txt" "$VERIFY_DIR/input/"

.venv/bin/python -m matching_lab validate \
  --bundle-dir "$VERIFY_DIR/bundle" \
  --mappings-csv "$VERIFY_DIR/input/mappings.csv" \
  --alerts-csv "$VERIFY_DIR/input/sync_alerts.csv" \
  --as-of "$AS_OF_UTC"

VERIFY_DIR="$VERIFY_DIR" .venv/bin/python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["VERIFY_DIR"])
manifest = json.loads((root / "bundle/manifest.json").read_text(encoding="utf-8"))
expected = manifest["input_sha256"]
for filename, key in (
    ("mappings.csv", "MAPPING_FILE"),
    ("sync_alerts.csv", "ALERTS_FILE"),
    ("epgshare.xml.gz", "EPG_XML"),
    ("epgshare.txt", "EPG_TEXT"),
):
    with (root / "input" / filename).open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != expected[key]:
        raise SystemExit(f"Input hash mismatch: {filename}")
print("All exact input-file hashes match the manifest.")
PY
```

The verification directory contains private data; remove it afterward using
your approved secure-retention procedure.

Acceptance requires:

1. The execution succeeds once with no retry.
2. The output prefix contains only `proposals.jsonl`, `summary.json`, and
   `manifest.json` (temporary `.part` objects must not remain).
3. Manifest hashes match the exact input and output objects.
4. A non-AI replay—or AI replay with the same intact, unchanged cache—produces
   byte-identical output for the same snapshot and timestamp.
5. The production Sheet and Workflow 2 output are unchanged.
6. Logs may contain fixed matcher-version banners and aggregate progress, but
   never proposal rows, channel names, credentials, private URLs, or file
   contents.

Keep the job manual through multiple reviewed shadow runs. Scheduling and any
guarded proposal application are separate future changes with their own review
and rollback plan.

## Failure and rollback

The runner exits with status `2` for a controlled contract failure. Do not
weaken a catalog, integrity, ambiguity, programme, or input-size gate to make a
run pass. Correct the snapshot or configuration and rerun into a new prefix.

To stop further work, do not execute the job. If necessary, delete only the
Cloud Run Job; this does not delete either bucket or any prior evidence:

```bash
gcloud run jobs delete "$JOB_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION"
```

Deleting a run prefix is a separate data-retention decision and should follow
the project's privacy and audit policy.

## References

- [Cloud Run Job creation](https://cloud.google.com/run/docs/create-jobs)
- [Cloud Storage volume mounts for Cloud Run Jobs](https://cloud.google.com/run/docs/configuring/jobs/cloud-storage-volume-mounts)
- [Building container images with Cloud Build](https://cloud.google.com/build/docs/building/build-containers)
