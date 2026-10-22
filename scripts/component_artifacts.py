"""Read validated component artifacts, with a one-time legacy Pages bootstrap."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import zipfile

import requests

LIMIT = 1024 * 1024 * 1024
MAX_ARTIFACT_PAGES = 5
COMPONENT_BUILDS = {
    "live": ("main.yml", "Stream, validate, and package EPG outputs"),
    "vod": ("vod.yml", "Build movies, series, languages and release metadata"),
}


def safe_member(name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (not parts or name.startswith("/") or ".." in parts or "\\" in name
            or ":" in name or any(ord(c) < 32 for c in name)):
        raise ValueError("Unsafe artifact member")
    return Path(*parts)


def extract_zip(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("Artifact destination already exists")
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 20000 or sum(m.file_size for m in members) > LIMIT:
            raise ValueError("Artifact exceeds extraction limits")
        names = set()
        for member in members:
            path = safe_member(member.filename)
            if path in names or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Artifact contains duplicate paths or links")
            names.add(path)
        destination.mkdir(parents=True)
        for member in members:
            target = destination / safe_member(member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, target.open("xb") as output:
                    while block := incoming.read(1024 * 1024):
                        output.write(block)


def extract_pages(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("Pages destination already exists")
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > LIMIT:
            raise ValueError("Pages archive exceeds extraction limits")
        names = set()
        for member in members:
            if member.name in (".", "./") and member.isdir():
                continue
            path = safe_member(member.name)
            if path in names or not (member.isfile() or member.isdir()):
                raise ValueError("Pages archive contains duplicate paths or links")
            names.add(path)
        destination.mkdir(parents=True)
        for member in members:
            if member.name in (".", "./"):
                continue
            target = destination / safe_member(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, target.open("xb") as output:
                while block := incoming.read(1024 * 1024):
                    output.write(block)


class GitHubArtifacts:
    def __init__(self):
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise ValueError("GitHub artifact settings are unavailable")
        self.base = "https://api.github.com/repos/" + repository
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": "Bearer " + token,
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})

    def get(self, suffix: str):
        response = self.session.get(self.base + suffix, timeout=(20, 90), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
            raise ValueError("Cannot read GitHub artifact metadata")
        return response.json()

    def download(self, artifact: dict, destination: Path):
        if artifact.get("expired") or not 0 < artifact.get("size_in_bytes", 0) <= LIMIT:
            raise ValueError("Artifact is expired or oversized")
        artifact_id = int(artifact["id"])
        response = self.session.get(self.base + f"/actions/artifacts/{artifact_id}/zip",
                                    timeout=(20, 90), allow_redirects=False)
        location = response.headers.get("Location", "")
        if response.status_code != 302 or not location.startswith("https://"):
            raise ValueError("GitHub artifact download was unavailable")
        # The signed storage URL is fetched without forwarding GitHub credentials.
        public = requests.Session()
        public.trust_env = False
        with public.get(location, stream=True, timeout=(20, 180), allow_redirects=False) as incoming:
            if incoming.status_code != 200:
                raise ValueError("Artifact storage download failed")
            count = 0
            digest = hashlib.sha256()
            with destination.open("xb") as output:
                for block in incoming.iter_content(1024 * 1024):
                    count += len(block)
                    if count > LIMIT:
                        raise ValueError("Artifact download exceeds its size limit")
                    digest.update(block)
                    output.write(block)
        expected = artifact.get("digest")
        if expected and expected != "sha256:" + digest.hexdigest():
            raise ValueError("GitHub artifact digest mismatch")

    @staticmethod
    def _artifact_identity(component: str, artifact: dict):
        if component not in COMPONENT_BUILDS:
            raise ValueError("Unknown publication component")
        if (not isinstance(artifact, dict) or type(artifact.get("id")) is not int
                or artifact["id"] < 1 or artifact.get("expired") is not False
                or type(artifact.get("size_in_bytes")) is not int
                or not 0 < artifact["size_in_bytes"] <= LIMIT):
            return None
        match = re.fullmatch(rf"{component}-snapshot-([1-9][0-9]*)-([1-9][0-9]*)", str(artifact.get("name", "")))
        if not match:
            return None
        run_id, attempt = map(int, match.groups())
        workflow_run = artifact.get("workflow_run")
        if not isinstance(workflow_run, dict) or workflow_run.get("id") != run_id:
            return None
        return run_id, attempt

    def _validated(self, component: str, artifact: dict, runs: dict, attempts: dict) -> bool:
        identity = self._artifact_identity(component, artifact)
        if identity is None:
            return False
        run_id, attempt = identity
        if run_id not in runs:
            runs[run_id] = self.get(f"/actions/runs/{run_id}")
        run = runs[run_id]
        workflow, job_name = COMPONENT_BUILDS[component]
        if (run.get("id") != run_id or run.get("head_branch") != "main"
                or run.get("path") != f".github/workflows/{workflow}"):
            return False
        if identity not in attempts:
            # Publisher-only reruns increase the run's current attempt, while
            # the validated build/artifact may still belong to attempt one.
            attempts[identity] = self.get(
                f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")["jobs"]
        return any(job.get("name") == job_name and job.get("conclusion") == "success"
                   and job.get("run_attempt", attempt) == attempt for job in attempts[identity])

    def _latest_validated(self, component: str, *, newer_than: int = 0, runs=None, attempts=None):
        runs, attempts = ({} if runs is None else runs), ({} if attempts is None else attempts)
        candidates = []
        for page in range(1, MAX_ARTIFACT_PAGES + 1):
            artifacts = self.get(f"/actions/artifacts?per_page=100&page={page}")["artifacts"]
            if not isinstance(artifacts, list) or len(artifacts) > 100:
                raise ValueError("Invalid repository artifact page")
            candidates.extend(artifact for artifact in artifacts
                if self._artifact_identity(component, artifact) is not None and artifact["id"] > newer_than)
            if len(artifacts) < 100:
                break
        # An older workflow rerun can upload the newest validated snapshot.
        # Artifact IDs track uploads; workflow creation order cannot do so.
        for artifact in sorted(candidates, key=lambda value: value["id"], reverse=True):
            if self._validated(component, artifact, runs, attempts):
                return artifact
        return None

    def _restore_artifact(self, artifact: dict, destination: Path):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "snapshot.zip"
            self.download(artifact, archive)
            extract_zip(archive, destination)

    def current(self, component: str, destination: Path, artifact_id: int):
        run_id = int(os.environ["GITHUB_RUN_ID"])
        artifact = self.get(f"/actions/artifacts/{artifact_id}")
        identity = self._artifact_identity(component, artifact)
        runs, attempts = {}, {}
        if (artifact.get("id") != artifact_id or identity is None or identity[0] != run_id
                or not self._validated(component, artifact, runs, attempts)):
            raise ValueError("Current component artifact is not a validated build")
        latest = self._latest_validated(component, newer_than=artifact_id, runs=runs, attempts=attempts)
        # Retrying an old publisher must never restore its older component over
        # a newer validated build, even if the public CDN is still stale.
        self._restore_artifact(latest or artifact, destination)

    def latest_component(self, component: str, destination: Path) -> bool:
        artifact = self._latest_validated(component)
        if artifact is None:
            return False
        self._restore_artifact(artifact, destination)
        print("Reused the latest validated " + component + " component.")
        return True

    def legacy(self, component: str, destination: Path):
        from publication_snapshots import package_snapshot
        # Only a successful main-branch production publication may bootstrap.
        runs = self.get("/actions/workflows/main.yml/runs?branch=main&status=success&per_page=20")["workflow_runs"]
        for run in runs:
            if run.get("head_branch") != "main" or run.get("conclusion") != "success":
                continue
            artifacts = self.get(f'/actions/runs/{int(run["id"])}/artifacts?per_page=100')["artifacts"]
            matches = [a for a in artifacts if a["name"] == "github-pages" and not a["expired"]]
            if len(matches) != 1:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.download(matches[0], root / "pages.zip")
                extract_zip(root / "pages.zip", root / "archive")
                extract_pages(root / "archive/artifact.tar", root / "public")
                package_snapshot(component, root / "public", destination)
            print("Reused the last successful legacy Pages component.")
            return
        raise ValueError("No valid previous component is available; publication stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("current", "previous"))
    parser.add_argument("--component", choices=("live", "vod"), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--base-url", default=os.environ.get("EPG_PUBLIC_BASE_URL", ""))
    args = parser.parse_args()
    if args.mode == "current":
        if not args.artifact_id or args.artifact_id < 1:
            raise ValueError("Current validated artifact ID is required")
        GitHubArtifacts().current(args.component, args.destination, args.artifact_id)
    else:
        from publication_snapshots import fetch_snapshot, SnapshotNotFound
        github = GitHubArtifacts()
        if github.latest_component(args.component, args.destination):
            return
        try:
            fetch_snapshot(args.component, args.base_url, args.destination)
        except SnapshotNotFound:
            github.legacy(args.component, args.destination)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, requests.RequestException, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SystemExit("Component restore failed safely: " + type(exc).__name__) from None
