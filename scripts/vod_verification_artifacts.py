"""Durable artifacts and bounded continuation for one non-publishing VOD test."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import component_artifacts
from component_artifacts import GitHubArtifacts, extract_zip


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def seal(state, test_id):
    state = Path(state)
    report = json.loads((state / 'report.json').read_text(encoding='utf-8'))
    if report.get('testId') != test_id or report.get('productionCatalogWritten') is not False:
        raise ValueError('Verification report has the wrong identity')
    database = state / 'checkpoint.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Checkpoint integrity check failed')
        metadata = {row[0]: json.loads(row[1]) for row in connection.execute(
            "SELECT key,value FROM meta WHERE key IN ('verificationConfig','sourceIdentity','batchNumber')")}
    if (metadata.get('verificationConfig') != report.get('sourceIdentity', {}).get('verificationConfig')
            or metadata.get('sourceIdentity') != report.get('sourceIdentity')
            or metadata.get('batchNumber') != report.get('batchNumber')
            or metadata.get('verificationConfig', {}).get('testId') != test_id):
        raise ValueError('Verification report differs from the durable checkpoint identity')
    manifest = {'schemaVersion': 1, 'testId': test_id,
                'sourceArtifactId': report['sourceIdentity']['verificationConfig']['sourceArtifactId'],
                'codeIdentity': report['sourceIdentity']['verificationConfig']['codeIdentity'],
                'checkpointSha256': digest(database), 'checkpointBytes': database.stat().st_size,
                'reportSha256': digest(state / 'report.json'),
                'batchNumber': report['batchNumber']}
    (state / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n', encoding='utf-8')
    return manifest


def restore_component(artifact_id, destination):
    github = GitHubArtifacts()
    artifact = github.get(f'/actions/artifacts/{int(artifact_id)}')
    if not github._validated('vod', artifact, {}, {}):
        raise ValueError('Verification source is not a validated production VOD component')
    github._restore_artifact(artifact, Path(destination))
    return {'sourceArtifactId': int(artifact_id)}


def restore_state(artifact_id, destination, test_id):
    github = GitHubArtifacts()
    artifact = github.get(f'/actions/artifacts/{int(artifact_id)}')
    match = re.fullmatch(r'vod-verification-' + re.escape(test_id) + r'-([1-9][0-9]*)-([1-9][0-9]*)', artifact.get('name', ''))
    if not match or (artifact.get('workflow_run') or {}).get('id') != int(match[1]):
        raise ValueError('Checkpoint artifact has the wrong test identity')
    run = github.get('/actions/runs/' + match[1])
    if (run.get('path') != '.github/workflows/verify-vod.yml'
            or run.get('head_sha') != os.environ.get('GITHUB_SHA')
            or run.get('head_branch') != os.environ.get('GITHUB_REF_NAME')
            or run.get('conclusion') != 'success'):
        raise ValueError('Checkpoint did not come from this successful verification revision')
    # This full-catalog checkpoint can contain hundreds of thousands of small
    # identity files. The normal production component's lower limit is retained.
    previous_limit = component_artifacts.LIMIT
    component_artifacts.LIMIT = 4 * 1024 * 1024 * 1024
    try:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'checkpoint.zip'
            github.download(artifact, archive)
            # Verification states can contain more files than a publication.
            # They are packed as a single bounded tar file by the workflow.
            import tarfile
            from component_artifacts import safe_member
            from zipfile import ZipFile
            with ZipFile(archive) as outer:
                if outer.namelist() != ['state.tar'] or outer.getinfo('state.tar').file_size > component_artifacts.LIMIT:
                    raise ValueError('Unexpected checkpoint archive')
                with outer.open('state.tar') as incoming, (Path(temporary) / 'state.tar').open('xb') as output:
                    while block := incoming.read(1024 * 1024):
                        output.write(block)
            target = Path(destination)
            if target.exists():
                raise ValueError('Checkpoint destination already exists')
            with tarfile.open(Path(temporary) / 'state.tar') as source:
                entries = source.getmembers()
                if len(entries) > 1000000 or sum(entry.size for entry in entries) > component_artifacts.LIMIT:
                    raise ValueError('Checkpoint exceeds extraction bounds')
                paths = set()
                for entry in entries:
                    if entry.name in ('.', './') and entry.isdir():
                        continue
                    path = safe_member(entry.name)
                    if path in paths or not (entry.isfile() or entry.isdir()):
                        raise ValueError('Unsafe checkpoint member')
                    paths.add(path)
                target.mkdir(parents=True)
                for entry in entries:
                    if entry.name in ('.', './') and entry.isdir():
                        continue
                    path = target / safe_member(entry.name)
                    if entry.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with source.extractfile(entry) as incoming, path.open('xb') as output:
                            while block := incoming.read(1024 * 1024):
                                output.write(block)
    finally:
        component_artifacts.LIMIT = previous_limit
    manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('schemaVersion') != 1 or manifest.get('testId') != test_id
            or manifest.get('checkpointSha256') != digest(target / 'checkpoint.sqlite')
            or manifest.get('checkpointBytes') != (target / 'checkpoint.sqlite').stat().st_size
            or manifest.get('reportSha256') != digest(target / 'report.json')):
        raise ValueError('Checkpoint seal mismatch')
    expected = seal(target, test_id)
    if manifest != expected:
        raise ValueError('Checkpoint manifest differs from its frozen database/report')
    return {'restoredArtifactId': int(artifact_id), 'batchNumber': manifest['batchNumber']}


def continue_test(report_path, artifact_id, inputs):
    report = json.loads(Path(report_path).read_text(encoding='utf-8'))
    if not report['shouldContinue']:
        return {'continued': False, 'complete': report['complete'], 'fatalError': report['fatalError']}
    cooldown = (report.get('api') or {}).get('cooldownUntil') or 0
    next_due = report.get('nextAttemptAt') if report.get('totals', {}).get('due', 0) == 0 else 0
    not_before = max(cooldown, next_due or 0)
    if not_before - time.time() > 1800:
        return {'continued': False, 'complete': False, 'waitingUntil': not_before,
                'checkpointArtifactId': int(artifact_id), 'reason': 'long_service_cooldown'}
    while not_before > time.time():
        time.sleep(min(60, not_before - time.time()))
    github = GitHubArtifacts()
    payload = {**inputs, 'resume-artifact-id': str(int(artifact_id))}
    response = github.session.post(github.base + '/actions/workflows/verify-vod.yml/dispatches',
                                   json={'ref': os.environ['GITHUB_REF_NAME'], 'inputs': payload},
                                   timeout=(20, 90), allow_redirects=False)
    if response.status_code != 204:
        raise ValueError('Checkpoint saved; automatic continuation dispatch failed')
    return {'continued': True, 'checkpointArtifactId': int(artifact_id), 'batchNumber': report['batchNumber']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('restore-component', 'restore-state', 'seal', 'continue'))
    parser.add_argument('--artifact-id', type=int)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--test-id')
    parser.add_argument('--inputs-json')
    args = parser.parse_args()
    if args.mode == 'restore-component':
        result = restore_component(args.artifact_id, args.destination)
    elif args.mode == 'restore-state':
        result = restore_state(args.artifact_id, args.destination, args.test_id)
    elif args.mode == 'seal':
        result = seal(args.state_dir, args.test_id)
    else:
        result = continue_test(args.state_dir / 'report.json', args.artifact_id, json.loads(args.inputs_json))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
