#!/usr/bin/env bash
# Validate each independently built component and their final composition.
set -euo pipefail
component="${1:?expected live, vod, or all}"
[[ "$component" == live || "$component" == vod || "$component" == all ]]
if [[ "$component" != vod ]]; then
  test -s public/health.json
  test -s public/epg/index.json
  test -s public/EPG/index.json
  test -s public/EPG/taxonomy.json
  for server in server_1 server_2 server_3; do
    test -s "public/epg/${server}_tivimate.xml.gz"
    for kind in epg epg_v2 metadata; do
      test -s "public/EPG/${server}_${kind}.json.gz"
      test -s "public/EPG/${server}_${kind}_manifest.json"
      gzip -cd -- "public/EPG/${server}_${kind}.json.gz" | jq --stream empty
    done
  done
fi
if [[ "$component" != live ]]; then
  python scripts/build_vod_catalogs.py --validate-only \
    --expected-server server_1 --expected-server server_2 --expected-server server_3 \
    --output-dir public/VOD
  app_check=(python scripts/build_skytvplayer_catalog.py --validate-only --output-dir public/skytvplayer)
  if [[ "$component" == all ]]; then app_check+=(--epg-dir public/EPG); fi
  "${app_check[@]}"
fi
while IFS= read -r -d '' file; do gzip -t "$file"; done < <(find public -type f -name '*.gz' -print0)
PUBLICATION_COMPONENT="$component" python - <<'PY'
import json
import os
from pathlib import Path
component = os.environ['PUBLICATION_COMPONENT']
paths = []
if component != 'vod':
    paths += [Path('public') / p for p in ('health.json', 'epg/index.json', 'EPG/index.json', 'EPG/taxonomy.json')]
    for server in ('server_1', 'server_2', 'server_3'):
        paths += [Path(f'public/EPG/{server}_{kind}_manifest.json') for kind in ('epg', 'epg_v2', 'metadata')]
        paths += [Path(f'public/reports/{server}/{server}_{kind}.json') for kind in ('tivimate_manifest', 'validation')]
if component != 'live':
    paths += [Path('public/VOD/index.json'), Path('public/skytvplayer/index.json')]
for path in paths:
    if not isinstance(json.loads(path.read_text(encoding='utf-8')), dict):
        raise SystemExit(f'{path} is not a JSON object')
PY
if find public -type l -print -quit | grep -q .; then
  echo '::error::Pages output contains a symbolic link.'; exit 1
fi
if find public -type f -links +1 -print -quit | grep -q .; then
  echo '::error::Pages output contains a hard-linked file.'; exit 1
fi
public_kib="$(du -sk public | awk '{ print $1 }')"
if (( public_kib > 900 * 1024 )); then
  echo '::error::Pages payload exceeds the 900 MiB safety ceiling.'; exit 1
fi
