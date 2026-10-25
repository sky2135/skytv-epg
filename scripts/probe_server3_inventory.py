"""Read two Server3 inventory actions; retain counts and four safe samples only."""
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

import build_vod_catalogs as vod
from trace_vod_source_provenance import trace

ACTIONS = ('get_vod_streams', 'get_vod_categories')
IDS = ('1194029', '1194031', '954024', '955380')
DESTINATION = Path('.build/server3-inventory/report.json')


class CountingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.counts = {action: {'wireAttempts': 0, 'statusCounts': Counter(), 'transportFailures': Counter()} for action in ACTIONS}

    def get(self, url, **kwargs):
        action = kwargs.get('params', {}).get('action')
        if action not in ACTIONS:
            raise ValueError('Unexpected inventory probe action')
        record = self.counts[action]
        record['wireAttempts'] += 1
        try:
            response = super().get(url, **kwargs)
        except requests.RequestException as error:
            kind = ('timeout' if isinstance(error, requests.Timeout) else 'tls' if isinstance(error, requests.exceptions.SSLError)
                    else 'connection' if isinstance(error, requests.ConnectionError) else 'other_network')
            record['transportFailures'][kind] += 1
            raise
        record['statusCounts'][str(response.status_code) if 100 <= response.status_code <= 599 else 'other'] += 1
        return response


def error_code(error):
    if isinstance(error, vod.TransientPanelRequestError):
        return 'http_' + str(error.http_status) if error.http_status else 'transport_failure'
    if isinstance(error, vod.CatalogBuildError):
        message = str(error)
        if message == 'server_3: provider credentials were rejected.':
            return 'auth_rejected'
        if message == 'server_3: unsafe provider redirect refused.' or message == 'server_3: too many provider redirects.':
            return 'redirect_refused'
        if message == 'server_3 provider response is not valid UTF-8 JSON.':
            return 'invalid_json'
        if message in ('server_3: provider request failed.', 'server_3: provider response download failed.'):
            return 'transport_failure'
        if 'response is too large' in message or 'downloads exceed the total size' in message:
            return 'response_limit'
        return 'provider_validation_failure'
    return 'internal_validation_failure'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    os.replace(temporary, path)


def main():
    started = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    report = {'schemaVersion': 1, 'runId': os.environ.get('GITHUB_RUN_ID'), 'codeSha': os.environ.get('GITHUB_SHA'),
        'serverId': 'server_3', 'checkedAt': started, 'actions': {}, 'trace': None,
        'productionCatalogWritten': False, 'productionCachesWritten': False, 'tmdbRequests': 0,
        'providerDetailRequests': 0, 'semanticEmptyRetries': 0}
    config = vod.load_server_config('server_3', allow_insecure_http=os.environ.get('ALLOW_INSECURE_PANEL_HTTP', '').lower() == 'true')
    source = {'get_series': [], 'get_series_categories': [], 'vod_details': {}, 'series_details': {}}
    with CountingSession() as session:
        budget = vod.DownloadBudget()
        for action in ACTIONS:
            info = {'outcome': None, 'rowCount': None, 'explicitEmptyList': None}
            try:
                payload = vod.request_panel_json(session, config, action, budget=budget)
                rows = vod.normalize_rows(payload, 'server_3 ' + action)
                info.update(outcome='ok_nonempty' if rows else 'ok_empty', rowCount=len(rows),
                    explicitEmptyList=vod._explicit_empty_inventory_list(payload),
                    payloadType='array' if isinstance(payload, list) else 'object' if isinstance(payload, dict) else 'other',
                    rawArrayCount=len(payload) if isinstance(payload, list) else None)
                if action == 'get_vod_streams':
                    info['validNumericStreamIds'] = sum(bool(vod.normalize_item_id(row.get('stream_id', row.get('id')))) for row in rows)
                source[action] = payload
            except Exception as error:
                info['outcome'] = error_code(error)
            info.update(session.counts[action])
            report['actions'][action] = info
        report['providerDownloadedBytes'] = budget.consumed
    if all(action in source for action in ACTIONS):
        report['trace'] = trace(source, 'server_3', IDS, config=config,
            run_id=os.environ.get('GITHUB_RUN_ID', ''), code_sha=os.environ.get('GITHUB_SHA', ''))
    report['inventoryNonemptyNow'] = report['actions']['get_vod_streams']['outcome'] == 'ok_nonempty'
    report['currentInventoryRows'] = report['actions']['get_vod_streams']['rowCount']
    report['limitations'] = 'One observation, with bounded transport retries only. No detail lookups, catalog publication, cache writes, or empty-list guard bypass.'
    vod.ensure_no_secret_reflection(report, config)
    atomic_json(DESTINATION, report)
    print(json.dumps({'diagnosticCompleted': True, 'inventoryNonemptyNow': report['inventoryNonemptyNow'],
        'currentInventoryRows': report['currentInventoryRows'], 'actions': report['actions'],
        'productionCatalogWritten': False, 'productionCachesWritten': False, 'tmdbRequests': 0}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        atomic_json(DESTINATION, {'schemaVersion': 1, 'runId': os.environ.get('GITHUB_RUN_ID'),
            'codeSha': os.environ.get('GITHUB_SHA'), 'serverId': 'server_3', 'diagnosticCompleted': False,
            'outcome': 'configuration_or_diagnostic_failure', 'errorType': type(error).__name__,
            'productionCatalogWritten': False, 'productionCachesWritten': False, 'tmdbRequests': 0})
        print('Server3 inventory diagnostic failed safely; only a fixed-code report was retained.')
        raise SystemExit(1) from None
