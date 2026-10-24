"""Seal rolling VOD state and exchange validated positive metadata candidates."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from zipfile import BadZipFile, ZipFile

import requests

import component_artifacts
from component_artifacts import GitHubArtifacts, safe_member
import vod_tmdb as tmdb
from vod_provider_evidence import FACT_KEYS, normalize_facts

LIMIT = 4 * 1024 * 1024 * 1024
MAX_ENTRIES = 1_000_000
MAX_JSON = 256 * 1024 * 1024
WORKFLOW = '.github/workflows/enrich-vod.yml'
JOB = 'Resolve pending movies and series with durable progress'
SHA = re.compile(r'[0-9a-f]{64}\Z')
CONTENT = re.compile(r'server_[1-9][0-9]?:movie:[0-9]{1,10}\Z')
TREND_FILES = {hashlib.sha256(f'{kind}:trending:week'.encode()).hexdigest() + '.json'
               for kind in ('movie', 'series')}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def read_json(path, maximum=MAX_JSON):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ValueError('Invalid enrichment JSON file')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n'
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_root(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Enrichment paths cannot contain links')
    return path


def _files(root):
    root = _safe_root(root)
    result, total = {}, 0
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if path.is_symlink() or getattr(path.stat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Enrichment state contains a link')
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative == 'manifest.json':
                continue
            safe_member(relative)
            size = path.stat().st_size
            total += size
            if not path.is_file() or total > LIMIT or len(result) >= MAX_ENTRIES:
                raise ValueError('Enrichment state exceeds bounds')
            result[relative] = {'bytes': size, 'sha256': digest(path)}
    return result


def _positive(value):
    """Validate a full envelope; the publisher still binds it to current input."""
    if (not isinstance(value, dict) or set(value) != {'version', 'expiresAt', 'data'}
            or type(value['version']) is not int or value['version'] != 1 or type(value['expiresAt']) not in (int, float)
            or not math.isfinite(value['expiresAt']) or value['expiresAt'] < 0):
        return False
    data = value['data']
    if (not isinstance(data, dict) or data.get('identityPolicy') != tmdb.IDENTITY_POLICY
            or type(data.get('matchStrategy')) is not int or not 1 <= data['matchStrategy'] <= tmdb.MATCH_STRATEGY
            or set(data) - {'identityPolicy', 'matchStrategy', 'matchDiagnostic', 'identity', 'metadata'}):
        return False
    metadata = tmdb._cached_metadata(data.get('metadata'))
    proof = data.get('identity')
    if (metadata is None or metadata != data.get('metadata') or not metadata['matchStatus'].startswith('matched_')
            or not isinstance(proof, dict) or proof.get('tmdbId') != metadata['tmdbId']
            or proof.get('originalTitle') != tmdb._normalized_title(metadata['originalTitle'])
            or proof.get('year') != ((metadata['releaseDate'] or '')[:4] or None)
            or not tmdb._identity_matches(proof, metadata['originalTitle'] or '', proof.get('year'))):
        return False
    if ('matchDiagnostic' in data and data['matchDiagnostic'] != tmdb._match_diagnostic(data['matchDiagnostic'])
            or set(proof) - {'names', 'year', 'originalTitle', 'tmdbId', 'mediaKind', 'posterPaths',
                             'posterPath', 'posterPolicy', 'undatedPosterPolicy', 'sourceYear',
                             'contentId', 'evidenceFingerprint', 'evidenceDigest'}):
        return False
    for key in ('evidenceFingerprint', 'evidenceDigest'):
        if key in proof and (not isinstance(proof[key], str) or not SHA.fullmatch(proof[key])):
            return False
    if 'contentId' in proof and not re.fullmatch(r'server_[1-9][0-9]?:(?:movie|series):[0-9]{1,10}', str(proof['contentId'])):
        return False
    if metadata['matchStatus'] == 'matched_poster':
        if (proof.get('posterPolicy') != tmdb.POSTER_POLICY or proof.get('mediaKind') not in {'movie', 'series'}
                or not isinstance(proof.get('posterPath'), str) or not tmdb.POSTER_PATH.fullmatch(proof['posterPath'])):
            return False
    # Names/opaque image paths and bounded evidence hashes are the only proof
    # strings. Raw responses, request URLs and credentials are never exported.
    if len(json.dumps(proof, ensure_ascii=False)) > 64 * 1024:
        return False
    def strings_safe(item):
        if isinstance(item, str):
            return len(item) <= 1000 and '://' not in item and not any(ord(c) < 32 for c in item)
        if isinstance(item, list):
            return len(item) <= 1100 and all(strings_safe(child) for child in item)
        if isinstance(item, dict):
            return len(item) <= 30 and all(strings_safe(key) and strings_safe(child) for key, child in item.items())
        return item is None or type(item) in (int, float, bool)
    return strings_safe(proof)


def _evidence(value, name, now=None):
    now = time.time() if now is None else now
    return (isinstance(value, dict) and set(value) == {'version', 'contentId', 'sourceFingerprint', 'checkedAt', 'facts'}
        and type(value['version']) is int and value['version'] == 1 and isinstance(value['contentId'], str) and CONTENT.fullmatch(value['contentId'])
        and name == hashlib.sha256(value['contentId'].encode()).hexdigest() + '.json'
        and isinstance(value['sourceFingerprint'], str) and SHA.fullmatch(value['sourceFingerprint'])
        and type(value['checkedAt']) in (int, float) and math.isfinite(value['checkedAt'])
        and 0 <= value['checkedAt'] <= now + 300 and isinstance(value['facts'], dict)
        and set(value['facts']) == FACT_KEYS and normalize_facts({'info': value['facts']}) == value['facts']
        and len(json.dumps(value).encode()) <= 32 * 1024)


def _trending(value, name):
    return (name in TREND_FILES and isinstance(value, dict) and set(value) == {'version', 'expiresAt', 'data'}
        and type(value['version']) is int and value['version'] == 1
        and type(value['expiresAt']) in (int, float) and math.isfinite(value['expiresAt']) and value['expiresAt'] >= 0
        and isinstance(value['data'], dict) and len(value['data']) <= 500
        and all(isinstance(identifier, str) and tmdb._identifier(identifier) == identifier
                and type(rank) is int and 1 <= rank <= 500 for identifier, rank in value['data'].items()))


def _collisions(paths):
    if (not isinstance(paths, list) or len(paths) > 100000
            or any(not isinstance(path, str) or not tmdb.IMAGE_PATH.fullmatch(path) for path in paths)):
        raise ValueError('Invalid durable poster collision paths')
    return sorted(set(paths))


def _database_meta(state):
    database = Path(state) / 'queue.sqlite'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Rolling queue integrity check failed')
        return {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM meta')}


def _state_identity(state):
    control = read_json(Path(state) / 'control.json', 1024 * 1024)
    report = read_json(Path(state) / 'report.json', 8 * 1024 * 1024)
    metadata = _database_meta(state)
    if (control.get('schemaVersion') != 1 or type(control.get('isolated')) is not bool
            or type(control.get('sourceArtifactId')) is not int or control['sourceArtifactId'] < 1
            or report.get('mode') != 'rolling_vod_enrichment' or report.get('productionCatalogWritten') is not False
            or report.get('sourceArtifactId') != control['sourceArtifactId']
            or not isinstance(report.get('sourceIdentity'), dict)
            or not isinstance(report.get('policyIdentity'), str) or not SHA.fullmatch(report['policyIdentity'])
            or not isinstance(report.get('codeIdentity'), str) or not SHA.fullmatch(report['codeIdentity'])
            or metadata.get('sourceIdentity') != report.get('sourceIdentity')
            or metadata.get('policyIdentity') != report.get('policyIdentity')
            or metadata.get('batchNumber') != report.get('batchNumber')
            or type(report.get('batchNumber')) is not int or report['batchNumber'] < 1):
        raise ValueError('Rolling queue, report and control disagree')
    if 'fixedSourceArtifactId' in control and (control['isolated'] is not True
            or type(control['fixedSourceArtifactId']) is not int or control['fixedSourceArtifactId'] < 1
            or control['fixedSourceArtifactId'] != control['sourceArtifactId']):
        raise ValueError('Invalid isolated fixed source selection')
    return control, report, metadata


def seal(state):
    control, report, metadata = _state_identity(state)
    manifest = {'schemaVersion': 1, 'kind': 'vod-enrichment-state', 'isolated': control['isolated'],
        'sourceArtifactId': control['sourceArtifactId'], 'batchNumber': report['batchNumber'],
        'policyIdentity': report['policyIdentity'], 'codeIdentity': report['codeIdentity'],
        'collidedPosterPaths': _collisions(metadata.get('collidedPosterPaths', [])), 'files': _files(state)}
    atomic_json(Path(state) / 'manifest.json', manifest)
    return manifest


def _validate_files(root, manifest):
    expected = manifest.get('files')
    if not isinstance(expected, dict) or len(expected) > MAX_ENTRIES or expected != _files(root):
        raise ValueError('Enrichment artifact seal mismatch')


@contextmanager
def _large_downloads(maximum=LIMIT):
    previous = component_artifacts.LIMIT
    component_artifacts.LIMIT = maximum
    try:
        yield
    finally:
        component_artifacts.LIMIT = previous


def _extract(archive, destination, *, inner_name='state.tar'):
    target = _safe_root(destination)
    if target.exists():
        raise ValueError('Enrichment destination already exists')
    with ZipFile(archive) as outer:
        if (outer.namelist() != [inner_name] or outer.getinfo(inner_name).file_size > LIMIT
                or (outer.getinfo(inner_name).external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError('Unexpected enrichment archive')
        target.mkdir(parents=True)
        count, total, paths = 0, 0, set()
        with outer.open(inner_name) as incoming, tarfile.open(fileobj=incoming, mode='r|') as source:
            for member in source:
                count += 1
                if count > MAX_ENTRIES or member.size < 0:
                    raise ValueError('Enrichment archive exceeds entry bounds')
                if member.name in ('.', './') and member.isdir():
                    continue
                relative = safe_member(member.name)
                if relative in paths or not (member.isfile() or member.isdir()):
                    raise ValueError('Unsafe enrichment archive member')
                paths.add(relative)
                total += member.size
                if total > LIMIT:
                    raise ValueError('Enrichment archive exceeds byte bounds')
                output = target / relative
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as selected, output.open('xb') as stream:
                        shutil.copyfileobj(selected, stream, 1024 * 1024)


def _prefix(kind, isolated):
    return 'vod-enrichment-' + ('test-' if isolated else '') + kind + '-'


def _artifact_identity(artifact, kind, isolated):
    if (not isinstance(artifact, dict) or type(artifact.get('id')) is not int or artifact['id'] < 1
            or artifact.get('expired') is not False or type(artifact.get('size_in_bytes')) is not int
            or not 0 < artifact['size_in_bytes'] <= LIMIT
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(artifact.get('digest', '')))):
        return None
    match = re.fullmatch(re.escape(_prefix(kind, isolated)) + r'([1-9][0-9]*)-([1-9][0-9]*)', artifact.get('name', ''))
    if match is None or (artifact.get('workflow_run') or {}).get('id') != int(match[1]):
        return None
    return tuple(map(int, match.groups()))


def _trusted(github, artifact, kind, isolated):
    identity = _artifact_identity(artifact, kind, isolated)
    if identity is None:
        return False
    run_id, attempt = identity
    run = github.get(f'/actions/runs/{run_id}')
    if run.get('run_attempt', 1) != attempt:
        run = github.get(f'/actions/runs/{run_id}/attempts/{attempt}')
    if (run.get('id') != run_id or run.get('path') != WORKFLOW
            or run.get('head_branch') != os.environ.get('GITHUB_REF_NAME')
            or run.get('run_attempt', 1) != attempt):
        return False
    if run.get('conclusion') == 'success':
        return True
    if run.get('conclusion') != 'failure':
        return False
    jobs = github.get(f'/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')['jobs']
    required = {'Seal durable progress and export verified cache candidates',
                'Retain the sealed checkpoint before any continuation'}
    if kind == 'cache':
        required.add('Retain verified candidates for independent publication')
    if kind == 'report':
        required.add('Retain the aggregate progress report')
    return any(job.get('name') == JOB
        and job.get('run_attempt', attempt) == attempt
        and required <= {step.get('name') for step in job.get('steps', []) if step.get('conclusion') == 'success'}
        for job in jobs)


def _latest(github, kind, isolated):
    candidates = []
    truncated = False
    for page in range(1, component_artifacts.MAX_ARTIFACT_PAGES + 1):
        rows = github.get(f'/actions/artifacts?per_page=100&page={page}')['artifacts']
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Invalid enrichment artifact page')
        candidates.extend(row for row in rows if _artifact_identity(row, kind, isolated))
        if len(rows) < 100:
            break
        truncated = page == component_artifacts.MAX_ARTIFACT_PAGES
    for artifact in sorted(candidates, key=lambda row: row['id'], reverse=True):
        if _trusted(github, artifact, kind, isolated):
            return artifact
    if truncated:
        raise ValueError('Enrichment artifact history exceeds discovery bounds')
    return None


def _restore(github, artifact, destination, *, kind, isolated):
    with tempfile.TemporaryDirectory() as temporary, _large_downloads():
        temporary = Path(temporary)
        archive = temporary / 'artifact.zip'
        github.download(artifact, archive)
        unpacked = temporary / 'unpacked'
        _extract(archive, unpacked, inner_name='cache.tar' if kind == 'cache' else 'state.tar')
        manifest = read_json(unpacked / 'manifest.json')
        if manifest.get('schemaVersion') != 1 or manifest.get('kind') != 'vod-enrichment-' + kind or manifest.get('isolated') is not isolated:
            raise ValueError('Wrong enrichment artifact mode')
        _validate_files(unpacked, manifest)
        if kind == 'state':
            control, report, metadata = _state_identity(unpacked)
            if (manifest.get('sourceArtifactId') != control['sourceArtifactId']
                    or manifest.get('isolated') is not control['isolated']
                    or manifest.get('batchNumber') != report['batchNumber']
                    or manifest.get('policyIdentity') != report['policyIdentity']
                    or manifest.get('codeIdentity') != report['codeIdentity']
                    or manifest.get('collidedPosterPaths') != _collisions(metadata.get('collidedPosterPaths', []))):
                raise ValueError('Rolling state seal identity mismatch')
        elif (type(manifest.get('sourceArtifactId')) is not int or manifest['sourceArtifactId'] < 1
                or type(manifest.get('batchNumber')) is not int or manifest['batchNumber'] < 1
                or not isinstance(manifest.get('policyIdentity'), str) or not SHA.fullmatch(manifest['policyIdentity'])):
            raise ValueError('Candidate seal identity is invalid')
        destination = _safe_root(destination)
        if destination.exists():
            raise ValueError('Enrichment destination already exists')
        shutil.copytree(unpacked, destination)
    return manifest


def _small_report(github, state_artifact, isolated):
    """Avoid downloading the large state merely to learn that it is idle."""
    run_id, attempt = _artifact_identity(state_artifact, 'state', isolated)
    rows = github.get(f'/actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Invalid enrichment run artifact page')
    expected = _prefix('report', isolated) + f'{run_id}-{attempt}'
    matches = [row for row in rows if row.get('name') == expected
               and _artifact_identity(row, 'report', isolated) == (run_id, attempt)]
    if len(matches) != 1 or not _trusted(github, matches[0], 'report', isolated):
        return None
    artifact = matches[0]
    if artifact['size_in_bytes'] > 16 * 1024 * 1024:
        return None
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / 'report.zip'
        try:
            with _large_downloads(16 * 1024 * 1024):
                github.download(artifact, archive)
        except ValueError:
            print('WARNING: Enrichment report probe failed validation; restoring sealed state.', file=sys.stderr)
            return None
        try:
            with ZipFile(archive) as zipped:
                if (zipped.namelist() != ['report.json'] or zipped.getinfo('report.json').file_size > 8 * 1024 * 1024
                        or (zipped.getinfo('report.json').external_attr >> 16) & 0o170000 == 0o120000):
                    return None
                report = json.loads(zipped.read('report.json'))
            if (not isinstance(report, dict) or report.get('schemaVersion') != 1
                    or report.get('mode') != 'rolling_vod_enrichment' or report.get('productionCatalogWritten') is not False
                    or type(report.get('sourceArtifactId')) is not int or report['sourceArtifactId'] < 1
                    or type(report.get('batchNumber')) is not int or report['batchNumber'] < 1
                    or not isinstance(report.get('policyIdentity'), str) or not SHA.fullmatch(report['policyIdentity'])
                    or not isinstance(report.get('codeIdentity'), str) or not SHA.fullmatch(report['codeIdentity'])
                    or not isinstance(report.get('sourceIdentity'), dict)
                    or type(report.get('shouldContinue')) is not bool or not isinstance(report.get('totals'), dict)
                    or any(type(report['totals'].get(key)) is not int or report['totals'][key] < 0 for key in ('total', 'due', 'remaining'))
                    or any(type(value) is not int or value < 0 for value in report['totals'].values())
                    or report['totals']['due'] > report['totals']['total'] or report['totals']['remaining'] > report['totals']['total']
                    or report.get('fatalError') is not None and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', str(report['fatalError']))
                    or not isinstance(report.get('api') or {}, dict)):
                return None
            for value in (report.get('nextAttemptAt'), (report.get('api') or {}).get('cooldownUntil')):
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    return None
            _ready_due(report)
            return report
        except (ValueError, KeyError, TypeError, BadZipFile):
            return None


def _ready_due(report):
    trending = report.get('trending') or {}
    if not isinstance(trending, dict) or type(trending.get('due', 0)) is not int or not 0 <= trending.get('due', 0) <= 2:
        raise ValueError('Invalid trending due count')
    due = report.get('totals', {}).get('due', 0)
    if type(due) is not int or due < 0:
        raise ValueError('Invalid enrichment due count')
    return due + trending.get('due', 0)


def _idle(report, component_id, policy):
    cooldown = (report.get('api') or {}).get('cooldownUntil') or 0
    if cooldown > time.time():
        return {'reason': 'service_cooldown', 'waitingUntil': cooldown}
    changed = report['sourceArtifactId'] != component_id or report['policyIdentity'] != policy or bool(report.get('fatalError'))
    if not changed and _ready_due(report) == 0:
        due = report.get('nextAttemptAt')
        if not due or due > time.time():
            return {'reason': 'no_ready_work', 'waitingUntil': due}
    return None


def prepare(state_dir, component_dir, *, isolated=False, source_artifact_id=None, github=None):
    github = github or GitHubArtifacts()
    state_dir, component_dir = _safe_root(state_dir), _safe_root(component_dir)
    if state_dir == component_dir or state_dir in component_dir.parents or component_dir in state_dir.parents:
        raise ValueError('State and component paths must be separate')
    previous = _latest(github, 'state', isolated)
    if state_dir.exists():
        raise ValueError('Initial enrichment state already exists')
    restored = False
    # Isolated manual tests retain an explicitly frozen source on a fresh
    # dispatch even when the caller omits the original source input.
    if previous and isolated:
        _restore(github, previous, state_dir, kind='state', isolated=isolated)
        restored = True
        previous_control, _report, _metadata = _state_identity(state_dir)
        if source_artifact_id is None:
            source_artifact_id = previous_control.get('fixedSourceArtifactId')
    if source_artifact_id is not None:
        if not isolated or type(source_artifact_id) is not int or source_artifact_id < 1:
            raise ValueError('Explicit source selection requires isolated staging')
        component = github.get(f'/actions/artifacts/{int(source_artifact_id)}')
        if not github._validated('vod', component, {}, {}):
            raise ValueError('Source is not a validated VOD component')
    else:
        component = github._latest_validated('vod')
    result = {'should_run': False, 'seed_required': previous is None,
              'restored_state_artifact_id': previous['id'] if previous else None,
              'source_artifact_id': component['id'] if component else None}
    if component is None:
        return {**result, 'reason': 'no_validated_component'}
    from enrich_vod_catalog import policy_identity
    policy = policy_identity()
    if previous:
        small = None if restored else _small_report(github, previous, isolated)
        if small is not None:
            idle = _idle(small, component['id'], policy)
            if idle:
                return {**result, **idle}
        if not restored:
            _restore(github, previous, state_dir, kind='state', isolated=isolated)
        control, report, _metadata = _state_identity(state_dir)
        idle = _idle(report, component['id'], policy)
        if idle:
            return {**result, **idle}
    github._restore_artifact(component, component_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    control = {'schemaVersion': 1, 'isolated': isolated, 'sourceArtifactId': component['id']}
    if source_artifact_id is not None:
        control['fixedSourceArtifactId'] = source_artifact_id
    atomic_json(state_dir / 'control.json', control)
    return {**result, 'should_run': True}


def export_cache(state_dir, destination):
    control, report, metadata = _state_identity(state_dir)
    destination = _safe_root(destination)
    if destination.exists():
        raise ValueError('Candidate destination already exists')
    destination.mkdir(parents=True)
    counts = {'positive': 0, 'trending': 0, 'providerEvidence': 0, 'skipped': 0}
    collisions = _collisions(metadata.get('collidedPosterPaths', []))
    for directory, relative, maximum, valid, label in (
        (Path(state_dir) / 'cache/tmdb/tmdb-v1', 'cache/tmdb/tmdb-v1', tmdb.MAX_CACHE_BYTES, _positive, 'positive'),
        (Path(state_dir) / 'cache/provider-evidence', 'cache/provider-evidence', 32 * 1024, None, 'providerEvidence')):
        if not directory.exists():
            continue
        _safe_root(directory)
        for path in directory.iterdir():
            if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
                counts['skipped'] += 1
                continue
            try:
                value = read_json(path, maximum)
                selected_label = 'trending' if label == 'positive' and path.name in TREND_FILES else label
                accepted = (_trending(value, path.name) if selected_label == 'trending'
                            else valid(value) if valid else _evidence(value, path.name))
                if selected_label == 'positive' and value.get('data', {}).get('metadata', {}).get('matchStatus') == 'matched_poster':
                    accepted = accepted and value['data']['identity'].get('posterPath') not in collisions
                if not accepted:
                    counts['skipped'] += 1
                    continue
                atomic_json(destination / relative / path.name, value)
                counts[selected_label] += 1
            except (ValueError, TypeError, KeyError, OSError):
                counts['skipped'] += 1
    manifest = {'schemaVersion': 1, 'kind': 'vod-enrichment-cache', 'isolated': control['isolated'],
        'sourceArtifactId': control['sourceArtifactId'], 'batchNumber': report['batchNumber'],
        'policyIdentity': report['policyIdentity'], 'collidedPosterPaths': collisions,
        'counts': counts, 'files': _files(destination)}
    atomic_json(destination / 'manifest.json', manifest)
    return counts


def _import_cache(destination, *, isolated=False, github=None):
    github = github or GitHubArtifacts()
    artifact = _latest(github, 'cache', isolated)
    if artifact is None:
        return {'found': False, 'imported': 0, 'preserved': 0}
    destination = _safe_root(destination)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'candidate'
        manifest = _restore(github, artifact, root, kind='cache', isolated=isolated)
        selected = []
        for relative in manifest['files']:
            path = Path(relative)
            if not re.fullmatch('[0-9a-f]{64}\\.json', path.name):
                raise ValueError('Invalid candidate cache name')
            if path.parent.as_posix() == 'cache/tmdb/tmdb-v1':
                value = read_json(root / path, tmdb.MAX_CACHE_BYTES)
                kind = 'trending' if path.name in TREND_FILES else 'positive'
                if not (_trending(value, path.name) if kind == 'trending' else _positive(value)):
                    raise ValueError('Candidate contains unverified metadata')
                selected.append((path, value, kind))
            elif path.parent.as_posix() == 'cache/provider-evidence':
                value = read_json(root / path, 32 * 1024)
                if not _evidence(value, path.name):
                    raise ValueError('Candidate contains invalid provider evidence')
                selected.append((path, value, 'evidence'))
            else:
                raise ValueError('Candidate contains an unexpected cache file')
        collisions = _collisions(manifest.get('collidedPosterPaths', []))
        collision_file = destination / 'enrichment-collisions.json'
        if collision_file.exists():
            existing_collisions = read_json(collision_file, 16 * 1024 * 1024)
            if (not isinstance(existing_collisions, dict) or set(existing_collisions) != {'schemaVersion', 'collidedPosterPaths'}
                    or existing_collisions['schemaVersion'] != 1):
                raise ValueError('Invalid existing collision tombstones')
            collisions = _collisions(collisions + _collisions(existing_collisions['collidedPosterPaths']))
        plans, imported, preserved = [], 0, 0
        for relative, value, kind in selected:
            target = _safe_root(destination / relative.relative_to('cache'))
            keep = False
            if target.exists():
                try:
                    existing = read_json(target, tmdb.MAX_CACHE_BYTES)
                    if kind == 'positive' and _positive(existing):
                        keep = (existing['expiresAt'] >= value['expiresAt']
                            or existing['data']['metadata']['tmdbId'] != value['data']['metadata']['tmdbId'])
                    elif kind == 'positive' and _fresh_rejection(existing):
                        keep = True
                    elif kind == 'trending' and _trending(existing, target.name):
                        keep = existing['expiresAt'] >= value['expiresAt']
                    elif kind == 'evidence' and _evidence(existing, target.name):
                        keep = existing['checkedAt'] >= value['checkedAt']
                except (ValueError, OSError, KeyError, TypeError):
                    pass
            if keep:
                preserved += 1
            else:
                plans.append((target, value))
        # Every candidate, destination path, existing evidence and tombstone is
        # checked before the first cache write. A bad artifact cannot partly
        # replace the publisher's current valid cache.
        for target, value in plans:
            atomic_json(target, value)
            imported += 1
        atomic_json(collision_file, {'schemaVersion': 1, 'collidedPosterPaths': collisions})
    return {'found': True, 'artifactId': artifact['id'], 'imported': imported, 'preserved': preserved}


def _fresh_rejection(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        return False
    data, expires = value.get('data'), value.get('expiresAt')
    if (not isinstance(data, dict) or data.get('identityPolicy') != tmdb.IDENTITY_POLICY
            or data.get('matchStrategy') != tmdb.MATCH_STRATEGY
            or type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()):
        return False
    metadata = tmdb._cached_metadata(data.get('metadata'))
    return bool(metadata and metadata['matchStatus'] in {'no_match', 'unavailable'} and metadata['rejectedIdentity'])


def import_cache(destination, *, isolated=False, github=None, optional=False):
    try:
        return _import_cache(destination, isolated=isolated, github=github)
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, BadZipFile, requests.RequestException) as error:
        if not optional:
            raise
        category = 'candidate_unavailable' if isinstance(error, requests.RequestException) else 'candidate_validation_failed'
        print('WARNING: VOD enrichment import skipped: ' + category + '; existing cache remains available.', file=sys.stderr)
        return {'found': False, 'imported': 0, 'preserved': 0, 'warning': category}


def continue_work(state_dir, artifact_id, *, isolated=False, github=None):
    control, report, _metadata = _state_identity(state_dir)
    manifest = read_json(Path(state_dir) / 'manifest.json')
    _validate_files(state_dir, manifest)
    if control['isolated'] is not isolated:
        raise ValueError('Continuation mode differs from sealed state')
    if report.get('fatalError') or not report.get('shouldContinue'):
        return {'continued': False, 'reason': 'fatal_or_no_pending_work'}
    now = time.time()
    cooldown = (report.get('api') or {}).get('cooldownUntil') or 0
    ready = _ready_due(report)
    next_due = report.get('nextAttemptAt') if ready == 0 else 0
    if max(cooldown, next_due or 0) > now or ready == 0:
        return {'continued': False, 'reason': 'waiting', 'waitingUntil': max(cooldown, next_due or 0)}
    github = github or GitHubArtifacts()
    artifact = github.get(f'/actions/artifacts/{int(artifact_id)}')
    identity = _artifact_identity(artifact, 'state', isolated)
    if (identity != (int(os.environ['GITHUB_RUN_ID']), int(os.environ.get('GITHUB_RUN_ATTEMPT', '1')))
            or artifact.get('id') != int(artifact_id)):
        raise ValueError('Continuation artifact does not belong to this batch')
    inputs = {'isolated': str(isolated).lower()}
    if control.get('fixedSourceArtifactId') is not None:
        inputs['source-artifact-id'] = str(control['fixedSourceArtifactId'])
    response = github.session.post(github.base + '/actions/workflows/enrich-vod.yml/dispatches',
        json={'ref': os.environ['GITHUB_REF_NAME'], 'inputs': inputs},
        timeout=(20, 90), allow_redirects=False)
    if response.status_code != 204:
        raise ValueError('Sealed state saved; enrichment continuation dispatch failed')
    return {'continued': True, 'checkpointArtifactId': int(artifact_id)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'seal', 'export-cache', 'import-cache', 'continue'))
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--component-dir', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--artifact-id', type=int)
    parser.add_argument('--source-artifact-id', type=int)
    parser.add_argument('--isolated', action='store_true')
    parser.add_argument('--optional', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'prepare':
        result = prepare(args.state_dir, args.component_dir, isolated=args.isolated, source_artifact_id=args.source_artifact_id)
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
                for key in ('should_run', 'seed_required', 'source_artifact_id', 'restored_state_artifact_id'):
                    value = result.get(key)
                    output.write(f'{key}={str(value).lower() if isinstance(value, bool) else value or ""}\n')
    elif args.mode == 'seal':
        manifest = seal(args.state_dir)
        result = {key: value for key, value in manifest.items() if key not in {'files', 'collidedPosterPaths'}}
        result.update(fileCount=len(manifest['files']), collidedPosterPathCount=len(manifest['collidedPosterPaths']))
    elif args.mode == 'export-cache':
        result = export_cache(args.state_dir, args.destination)
    elif args.mode == 'import-cache':
        result = import_cache(args.destination, isolated=args.isolated, optional=args.optional)
    else:
        result = continue_work(args.state_dir, args.artifact_id, isolated=args.isolated)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, sqlite3.DatabaseError, tarfile.TarError, BadZipFile, requests.RequestException):
        raise SystemExit('VOD enrichment artifact validation failed safely.') from None
