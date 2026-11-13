"""Exercise real local composition and a later ordinary rebuild, offline."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT / "scripts", ROOT / "tests"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))
import app_catalog_closure as closure
import build_skytvplayer_catalog as catalog
import build_vod_catalogs as vod
import publication_snapshots as snapshots
import series_resume_publication as resume
from test_skytvplayer_catalog import FakeTmdbClient, NOW, app_source

SERVERS = ("server_1",)
LATER = "2026-10-18T16:30:00Z"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SeriesResumeEndToEndTests(unittest.TestCase):
    def test_live_only_composition_then_vod_rebuild_preserves_and_rebinds_lookup(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            def ordinary(name, when, title):
                source = app_source()
                source["get_series"][1]["name"] = title
                out = root / name
                vod.publish_catalogs(out / "VOD", [("server_1", source, None)], generated_at=when)
                catalog.build(out / "skytvplayer", root / (name + "-cache"),
                              [("server_1", source, None)], FakeTmdbClient(), when)
                resume.attach(out / "skytvplayer", enabled=True,
                              expected_index_sha256=sha(out / "skytvplayer/index.json"), expected_servers=SERVERS)
                return out
            def live(name, marker):
                out = root / (name + "-source")
                (out / "EPG").mkdir(parents=True)
                (out / "EPG/index.json").write_text("{}")
                (out / "health.json").write_text("{}")
                for kind in ("epg", "epg_v2", "metadata"):
                    (out / ("EPG/server_1_" + kind + ".json.gz")).write_bytes(
                        gzip.compress(json.dumps({"marker": marker}).encode(), mtime=0))
                packaged = root / name
                snapshots.package("live", out, packaged)
                return packaged
            first = ordinary("first", NOW, "First reviewed title")
            old = json.loads((first / "skytvplayer/index.json").read_bytes())["servers"][0]
            old_files = closure.collect_app_closure(first / "skytvplayer", expected_servers=SERVERS)["files"]
            first_snapshot = root / "vod-first"
            snapshots.package("vod", first, first_snapshot)
            live1 = live("live-first", 1)
            snapshots.compose(live1, first_snapshot, root / "composed-first")
            live2 = live("live-second", 2)
            out = root / "composed-live-update"
            snapshots.compose(live2, first_snapshot, out)
            current = json.loads((out / "skytvplayer/index.json").read_bytes())["servers"][0]
            for field in ("series", "home", "seriesResumeLookup"):
                self.assertEqual(old[field], current[field])
            self.assertNotEqual(sha(root / "composed-first/skytvplayer/index.json"), sha(out / "skytvplayer/index.json"))
            catalog.validate(out / "skytvplayer", expected_servers=SERVERS, epg_dir=out / "EPG")
            resume.validate_optional_graphs(out / "skytvplayer", expected_servers=SERVERS, require_lookup=True)
            second = ordinary("second", LATER, "Later reviewed title")
            retention = closure.retain_app_closures(second / "skytvplayer", out / "skytvplayer", expected_servers=SERVERS)
            self.assertGreater(retention["copiedFiles"], 0)
            second_snapshot = root / "vod-second"
            snapshots.package("vod", second, second_snapshot)
            final = root / "composed-later-vod"
            snapshots.compose(live2, second_snapshot, final)
            updated = json.loads((final / "skytvplayer/index.json").read_bytes())["servers"][0]
            self.assertNotEqual(updated["series"]["sha256"], old["series"]["sha256"])
            self.assertNotEqual(updated["seriesResumeLookup"]["sha256"], old["seriesResumeLookup"]["sha256"])
            catalog.validate(final / "skytvplayer", expected_servers=SERVERS, epg_dir=final / "EPG")
            resume.validate_optional_graphs(final / "skytvplayer", expected_servers=SERVERS, require_lookup=True)
            for name, metadata in old_files.items():
                if name != "index.json":
                    self.assertEqual(sha(final / "skytvplayer" / name), metadata["sha256"])
            manifest = json.loads((final / "component-manifests/vod.json").read_bytes())
            row = next(x for x in manifest["files"] if x["path"] == "skytvplayer/index.json")
            self.assertEqual(row["sha256"], sha(final / "skytvplayer/index.json"))


if __name__ == "__main__":
    unittest.main()
