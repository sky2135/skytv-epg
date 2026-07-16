from __future__ import annotations

import csv
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def xmltv_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


class ProductionRunnerTests(unittest.TestCase):
    def test_three_servers_share_source_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            webroot = root / "web"
            mapping_dir = root / "mappings"
            work_dir = root / "work"
            cache_dir = root / "cache"
            public_dir = root / "public"
            webroot.mkdir()
            mapping_dir.mkdir()

            now = datetime.now(timezone.utc).replace(microsecond=0)
            source_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Channel.One.test"><display-name>Channel One</display-name></channel>
  <programme start="{xmltv_time(now - timedelta(hours=1))}" stop="{xmltv_time(now + timedelta(hours=1))}" channel="Channel.One.test"><title>Current Programme</title></programme>
  <programme start="{xmltv_time(now + timedelta(hours=1))}" stop="{xmltv_time(now + timedelta(hours=5))}" channel="Channel.One.test"><title>Future Programme</title></programme>
</tv>
'''
            with gzip.open(webroot / "test1.xml.gz", "wb") as handle:
                handle.write(source_xml.encode("utf-8"))

            panel_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="Panel.Three"><display-name>Panel Three</display-name></channel>
  <programme start="{xmltv_time(now - timedelta(hours=1))}" stop="{xmltv_time(now + timedelta(hours=4))}" channel="Panel.Three"><title>Panel Programme</title></programme>
</tv>
'''
            (webroot / "xmltv.php").write_text(panel_xml, encoding="utf-8")

            request_counts: Counter[str] = Counter()

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(webroot), **kwargs)

                def do_GET(self):  # noqa: N802 - standard-library API name
                    request_counts[self.path.split("?", 1)[0]] += 1
                    return super().do_GET()

                def log_message(self, format, *args):
                    return None

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = int(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                source_config = {
                    "schemaVersion": 1,
                    "sources": [
                        {
                            "enabled": True,
                            "name": "TEST1",
                            "region": "US",
                            "kind": "real",
                            "txt_url": f"http://127.0.0.1:{port}/test1.txt",
                            "xml_url": f"http://127.0.0.1:{port}/test1.xml.gz",
                            "match_keywords": [],
                            "use_for_matching": False,
                        }
                    ],
                }
                config_path = root / "epg_sources.json"
                config_path.write_text(json.dumps(source_config), encoding="utf-8")

                columns = [
                    "server_id",
                    "stream_id",
                    "category_id",
                    "category_name",
                    "channel_name",
                    "action",
                    "source",
                    "epg_id",
                    "epg_feed",
                    "reason",
                ]
                rows = {
                    "server_1": [
                        "server_1", "1", "1", "General", "Server One Channel",
                        "APPROVED", "epgshare", "Channel.One.test", "TEST1", "",
                    ],
                    "server_2": [
                        "server_2", "2", "1", "General", "Server Two Channel",
                        "APPROVED", "epgshare", "Channel.One.test", "TEST1", "",
                    ],
                    "server_3": [
                        "server_3", "3", "1", "General", "Server Three Channel",
                        "APPROVED", "panel", "Panel.Three", "server xmltv.php", "",
                    ],
                }
                for server_id, row in rows.items():
                    path = mapping_dir / f"{server_id}_final_mapping.csv"
                    with path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(columns)
                        writer.writerow(row)

                environment = os.environ.copy()
                environment.update(
                    {
                        "SERVER_3_BASE_URL": f"http://127.0.0.1:{port}",
                        "SERVER_3_USERNAME": "catalog",
                        "SERVER_3_PASSWORD": "test-secret",
                    }
                )
                command = [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "build_all_servers.py"),
                    "--mapping-dir",
                    str(mapping_dir),
                    "--source-config",
                    str(config_path),
                    "--work-dir",
                    str(work_dir),
                    "--cache-dir",
                    str(cache_dir),
                    "--public-dir",
                    str(public_dir),
                    "--download-workers",
                    "3",
                ]
                completed = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}",
                )
                self.assertEqual(request_counts["/test1.xml.gz"], 1)
                self.assertEqual(request_counts["/xmltv.php"], 1)

                index = json.loads((public_dir / "epg" / "index.json").read_text())
                self.assertEqual(len(index["builds"]), 3)
                for server_id in rows:
                    self.assertTrue(
                        (public_dir / "epg" / f"{server_id}_tivimate.xml.gz").is_file()
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
