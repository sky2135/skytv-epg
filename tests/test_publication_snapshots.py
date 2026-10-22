from __future__ import annotations

import copy
from contextlib import nullcontext
import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import publication_snapshots as snapshots

BASE_URL = "https://sky2135.github.io/skytv-epg/"
LIVE_TIME = "2026-10-22T10:00:00Z"
VOD_TIME = "2026-10-20T12:00:00Z"


class Response:
    def __init__(self, url, raw=b"", status=200, headers=None):
        self.url, self.raw, self.status_code = url, raw, status
        self.headers = headers if headers is not None else {"Content-Length": str(len(raw))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self.raw[:3]
        yield self.raw[3:]


class SessionFactory:
    def __init__(self, responses):
        self.responses, self.calls, self.sessions = responses, [], []

    def __call__(self):
        owner = self

        class Session:
            trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                owner.calls.append((url, kwargs, self.trust_env))
                response = owner.responses[url]
                if isinstance(response, Exception):
                    raise response
                return response

        session = Session()
        self.sessions.append(session)
        return session


class PublicationSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.public = self.root / "input-public"
        self.public.mkdir()
        self.live = self.root / "live-snapshot"
        self.vod = self.root / "vod-snapshot"
        self.output = self.root / "composed"
        self.write(".nojekyll", b"")
        self.write("health.json", {"generatedAt": LIVE_TIME, "healthy": True})
        self.write("EPG/index.json", {"generatedAt": LIVE_TIME, "servers": []})
        for server in ("server_1", "server_2", "server_3"):
            for kind in ("epg", "epg_v2", "metadata"):
                self.write(f"EPG/{server}_{kind}.json.gz", gzip.compress(
                    json.dumps({"serverId": server, "kind": kind, "generatedAt": LIVE_TIME}).encode(), mtime=0))
        self.write("VOD/index.json", {"generatedAt": VOD_TIME, "servers": []})
        self.write("VOD/unchanged.json.gz", b"raw catalog bytes")
        self.write("skytvplayer/home.1234567890abcdef.json.gz", b"raw Home bytes")
        self.index = {"schemaVersion": 1, "appName": "SkyTVPlayer", "generatedAt": VOD_TIME,
                      "serverCount": 3, "servers": [
                          {"serverId": f"server_{number}", "epg": {"old": "reference"},
                           "movies": {"file": "unchanged-movies.json"}}
                          for number in (1, 2, 3)]}
        self.write("skytvplayer/index.json", self.index)

    def write(self, name, value):
        path = self.public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value, indent=2).encode() if isinstance(value, dict) else value)

    def package(self):
        return (snapshots.package("live", self.public, self.live),
                snapshots.package("vod", self.public, self.vod))

    def old_output(self):
        self.output.mkdir()
        (self.output / "previous-release").write_bytes(b"keep until validation succeeds")

    def assert_old_output(self):
        self.assertEqual(list(self.output.iterdir()), [self.output / "previous-release"])
        self.assertEqual((self.output / "previous-release").read_bytes(), b"keep until validation succeeds")

    def assert_manifest_bytes(self, manifest, root):
        self.assertEqual(manifest["fileCount"], len(manifest["files"]))
        self.assertEqual(manifest["totalBytes"], sum(entry["bytes"] for entry in manifest["files"]))
        for entry in manifest["files"]:
            raw = (root / entry["path"]).read_bytes()
            self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry["bytes"], len(raw))

    def responses(self, component):
        manifest_path = self.output / "component-manifests" / f"{component}.json"
        raw = manifest_path.read_bytes()
        values = {BASE_URL + f"component-manifests/{component}.json": Response(
            BASE_URL + f"component-manifests/{component}.json", raw)}
        for entry in json.loads(raw)["files"]:
            url = BASE_URL + entry["path"]
            values[url] = Response(url, (self.output / entry["path"]).read_bytes())
        return values

    def test_composition_preserves_components_and_their_distinct_dates(self):
        live_manifest, vod_manifest = self.package()
        self.assert_manifest_bytes(live_manifest, self.live / "files")
        self.assert_manifest_bytes(vod_manifest, self.vod / "files")
        self.old_output()
        result = snapshots.compose(self.live, self.vod, self.output)
        self.assertTrue(result["epgReferencesUpdated"])
        self.assertFalse((self.output / "previous-release").exists())
        for manifest in (live_manifest, vod_manifest):
            for entry in manifest["files"]:
                if entry["path"] != "skytvplayer/index.json":
                    self.assertEqual((self.public / entry["path"]).read_bytes(),
                                     (self.output / entry["path"]).read_bytes())
        composed_index = json.loads((self.output / "skytvplayer/index.json").read_bytes())
        self.assertEqual(composed_index["generatedAt"], VOD_TIME)
        self.assertEqual(json.loads((self.output / "health.json").read_bytes())["generatedAt"], LIVE_TIME)
        self.assertEqual(composed_index["servers"][0]["movies"], self.index["servers"][0]["movies"])
        for server in composed_index["servers"]:
            self.assertEqual(set(server["epg"]), {"programme", "programmeRich", "metadata"})
            for entry in server["epg"].values():
                path = self.output / "skytvplayer" / entry["file"]
                self.assertEqual(entry["compressedBytes"], path.stat().st_size)
                self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        for component in ("live", "vod"):
            manifest = json.loads((self.output / "component-manifests" / f"{component}.json").read_bytes())
            self.assert_manifest_bytes(manifest, self.output)

    def test_second_composition_keeps_unchanged_index_exactly_and_does_not_package_manifests(self):
        self.package()
        snapshots.compose(self.live, self.vod, self.output)
        original_index = (self.output / "skytvplayer/index.json").read_bytes()
        # Formatting is part of the immutable VOD bytes when EPG refs already match.
        index = json.loads(original_index)
        original_index = json.dumps(index, indent=4).encode()
        (self.output / "skytvplayer/index.json").write_bytes(original_index)
        later_live, later_vod = self.root / "later-live", self.root / "later-vod"
        for component, destination in (("live", later_live), ("vod", later_vod)):
            manifest = snapshots.package(component, self.output, destination)
            self.assertFalse(any(entry["path"].startswith("component-manifests/") for entry in manifest["files"]))
        result = snapshots.compose(later_live, later_vod, self.output)
        self.assertFalse(result["epgReferencesUpdated"])
        self.assertEqual((self.output / "skytvplayer/index.json").read_bytes(), original_index)

    def test_missing_or_corrupt_counterpart_preserves_existing_publication(self):
        self.package()
        self.old_output()
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.root / "missing", self.output)
        self.assert_old_output()
        (self.vod / "files/VOD/unchanged.json.gz").write_bytes(b"altered")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.vod, self.output)
        self.assert_old_output()

    def test_missing_app_live_artifact_cannot_switch_release(self):
        (self.public / "EPG/server_3_metadata.json.gz").unlink()
        self.package()
        self.old_output()
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.vod, self.output)
        self.assert_old_output()

    def test_optional_rich_guide_is_removed_from_reused_index_if_unavailable(self):
        self.package()
        snapshots.compose(self.live, self.vod, self.output)
        (self.public / "EPG/server_1_epg_v2.json.gz").unlink()
        snapshots.package("live", self.public, self.live)
        snapshots.compose(self.live, self.vod, self.output)
        index = json.loads((self.output / "skytvplayer/index.json").read_bytes())
        self.assertNotIn("programmeRich", index["servers"][0]["epg"])
        self.assertEqual(index["generatedAt"], VOD_TIME)

    def test_manifest_rejects_traversal_cross_component_wrong_totals_and_collisions(self):
        _, original = self.package()
        for name in ("../outside", "/root", "VOD/../secret", "VOD//file", "VOD\\file", "VOD/x?token=1",
                     "VOD/CON.json", "VOD/trailing.", "EPG/private.json", "component-manifests/vod.json"):
            with self.subTest(name=name):
                manifest = copy.deepcopy(original)
                manifest["files"][0]["path"] = name
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots._validate_manifest(manifest, "vod")
        for mutate in (
                lambda value: value.update(totalBytes=0),
                lambda value: value.update(fileCount=True),
                lambda value: value.update(component="live"),
                lambda value: value["files"][0].update(bytes=True),
                lambda value: value["files"][0].update(sha256="X" * 64),
                lambda value: value["files"][0].update(extra="untrusted"),
                lambda value: value["files"].reverse()):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            with self.assertRaises(snapshots.SnapshotError):
                snapshots._validate_manifest(manifest, "vod")
        for name in (original["files"][0]["path"].upper(), original["files"][0]["path"] + "/child"):
            manifest = copy.deepcopy(original)
            entry = dict(manifest["files"][0], path=name)
            manifest["files"].append(entry)
            manifest["files"].sort(key=lambda row: row["path"])
            manifest["fileCount"] += 1
            manifest["totalBytes"] += entry["bytes"]
            with self.assertRaises(snapshots.SnapshotError):
                snapshots._validate_manifest(manifest, "vod")

    def test_extra_payload_files_and_overlapping_directories_are_rejected(self):
        self.package()
        (self.live / "files/unlisted.txt").write_text("not approved", encoding="utf-8")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.vod, self.output)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.package("live", self.public, self.public / "nested")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.package("live", self.public, self.root)
        (self.live / "files/unlisted.txt").unlink()
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.vod, self.live / "nested")

    def test_symlink_in_public_or_snapshot_is_rejected(self):
        try:
            os.symlink(self.public / "health.json", self.public / "linked.json")
        except OSError:
            self.skipTest("The local platform does not allow symbolic links.")
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.package("live", self.public, self.live)
        (self.public / "linked.json").unlink()
        self.package()
        target = self.vod / "files/VOD/unchanged.json.gz"
        target.unlink()
        os.symlink(self.public / "VOD/unchanged.json.gz", target)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.compose(self.live, self.vod, self.output)

    def test_file_total_depth_and_tree_entry_limits_are_enforced(self):
        for setting, limit in (("MAX_FILE_BYTES", 2), ("MAX_TOTAL_BYTES", 2),
                               ("MAX_TREE_ENTRIES", 2), ("MAX_DEPTH", 1), ("MAX_FILES", 1)):
            with self.subTest(setting=setting), mock.patch.object(snapshots, setting, limit):
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.package("live", self.public, self.live)
                self.assertFalse(self.live.exists())

    def test_live_namespaces_allow_both_exact_epg_spellings(self):
        entry = lambda path: {"path": path, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        manifest = snapshots._make_manifest("live", [entry("EPG/index.json"), entry("epg/index.json"),
                                                     entry("health.json")])
        self.assertEqual(manifest["fileCount"], 3)
        with self.assertRaises(snapshots.SnapshotError):
            snapshots._make_manifest("live", [entry("EPG/index.json"), entry("EpG/index.json"), entry("health.json")])

    def test_snapshot_wrapper_preserves_both_epg_namespaces_and_payload_depth(self):
        # Exercise Linux's intentional case-sensitive public namespaces even on
        # the Windows development filesystem, which cannot hold both indexes.
        paths = {"files/EPG/index.json", "files/epg/index.json", "snapshot.json",
                 "files/" + "/".join(["nested"] * (snapshots.MAX_DEPTH - 1) + ["data.json"])}
        children = {}
        virtual_root = self.root.as_posix()
        for name in paths:
            path = virtual_root + "/" + name
            while path != virtual_root:
                parent = path.rsplit("/", 1)[0]
                children.setdefault(parent, set()).add(path)
                path = parent

        def scan(directory):
            entries = []
            for path in children.get(str(directory).replace("\\", "/"), []):
                entries.append(SimpleNamespace(
                    name=path.rsplit("/", 1)[1], path=path, is_symlink=lambda: False,
                    is_dir=lambda follow_symlinks, path=path: path in children,
                    is_file=lambda follow_symlinks, path=path: path not in children))
            return nullcontext(iter(entries))

        with mock.patch.object(snapshots.os, "scandir", side_effect=scan):
            self.assertEqual(set(snapshots._tree(self.root, wrapped=True)), paths)

    def test_fetch_exact_deployed_component_then_reuse_its_bytes(self):
        self.package()
        snapshots.compose(self.live, self.vod, self.output)
        for component in ("live", "vod"):
            with self.subTest(component=component):
                factory = SessionFactory(self.responses(component))
                target = self.root / f"downloaded-{component}"
                executor = snapshots.ThreadPoolExecutor
                with mock.patch.object(snapshots.requests, "Session", factory), mock.patch.object(
                        snapshots, "ThreadPoolExecutor", wraps=executor) as pool:
                    manifest = snapshots.fetch(component, BASE_URL, target)
                pool.assert_called_once_with(max_workers=4)
                self.assert_manifest_bytes(manifest, target / "files")
                for _url, kwargs, trust_env in factory.calls:
                    self.assertFalse(trust_env)
                    self.assertFalse(kwargs["allow_redirects"])
                    self.assertEqual(kwargs["headers"], {"Accept-Encoding": "identity"})
                self.assertEqual((target / "snapshot.json").read_bytes(),
                                 (self.output / "component-manifests" / f"{component}.json").read_bytes())

    def test_fetch_corruption_redirects_and_http_errors_preserve_old_snapshot(self):
        self.package()
        snapshots.compose(self.live, self.vod, self.output)
        target = self.root / "fetched"
        target.mkdir()
        (target / "old").write_bytes(b"previous valid")
        for failure in ("corrupt", "redirect", "different-response-url", "gzip", "oversize", "bad-length", "network"):
            with self.subTest(failure=failure):
                values = self.responses("vod")
                url = BASE_URL + "VOD/unchanged.json.gz"
                if failure == "corrupt":
                    values[url] = Response(url, b"x" * len(values[url].raw))
                elif failure == "redirect":
                    values[url] = Response(url, status=302, headers={"Location": "https://elsewhere.invalid/file"})
                elif failure == "different-response-url":
                    values[url].url = "https://elsewhere.invalid/file"
                elif failure == "gzip":
                    values[url].headers = {"Content-Encoding": "gzip"}
                elif failure == "oversize":
                    values[url] = Response(url, values[url].raw + b"more", headers={})
                elif failure == "bad-length":
                    values[url].headers = {"Content-Length": "one"}
                elif failure == "network":
                    values[url] = snapshots.requests.ConnectionError("must not reach logs")
                with mock.patch.object(snapshots.requests, "Session", SessionFactory(values)):
                    with self.assertRaises(snapshots.SnapshotError):
                        snapshots.fetch("vod", BASE_URL, target)
                self.assertEqual((target / "old").read_bytes(), b"previous valid")
                self.assertEqual(len(list(target.iterdir())), 1)

    def test_only_manifest_404_is_bootstrap_signal(self):
        target = self.root / "fetched"
        url = BASE_URL + "component-manifests/vod.json"
        for status, exception in ((404, snapshots.SnapshotNotFound), (403, snapshots.SnapshotError),
                                  (500, snapshots.SnapshotError), (302, snapshots.SnapshotError)):
            with self.subTest(status=status), mock.patch.object(snapshots.requests, "Session", SessionFactory({
                    url: Response(url, status=status)})):
                with self.assertRaises(exception) as result:
                    snapshots.fetch("vod", BASE_URL, target)
                if status != 404:
                    self.assertNotIsInstance(result.exception, snapshots.SnapshotNotFound)
                self.assertFalse(target.exists())

    def test_invalid_or_incomplete_remote_manifest_never_downloads_files(self):
        self.package()
        snapshots.compose(self.live, self.vod, self.output)
        url = BASE_URL + "component-manifests/vod.json"
        for raw in (b"{}", b"not JSON", b"[]"):
            factory = SessionFactory({url: Response(url, raw)})
            with mock.patch.object(snapshots.requests, "Session", factory):
                with self.assertRaises(ValueError):
                    snapshots.fetch("vod", BASE_URL, self.root / "fetched")
            self.assertEqual(len(factory.calls), 1)

    def test_base_urls_reject_credentials_queries_ip_addresses_and_unsafe_paths(self):
        for url in ("http://example.com/", "https://user:password@example.com/", "https://example.com/?token=x",
                    "https://example.com/#fragment", "https://127.0.0.1/", "https://[::1]/", "https://localhost/",
                    "https://test.localhost/", "https://example.com:8443/", "https://example.com:bad/",
                    "https://example.com/a/../b/", "https://example.com/a/%2e%2e/b/"):
            with self.subTest(url=url), mock.patch.object(snapshots.requests, "Session") as session:
                with self.assertRaises(snapshots.SnapshotError):
                    snapshots.fetch("vod", url, self.root / "fetched")
                session.assert_not_called()

    def test_cli_uses_the_same_package_and_composition_contract(self):
        with mock.patch("builtins.print"):
            self.assertEqual(snapshots.main(["package", "--component", "live", "--source-dir", str(self.public),
                                             "--output-dir", str(self.live)]), 0)
            self.assertEqual(snapshots.main(["package", "--component", "vod", "--source-dir", str(self.public),
                                             "--output-dir", str(self.vod)]), 0)
            self.assertEqual(snapshots.main(["compose", "--live-dir", str(self.live), "--vod-dir", str(self.vod),
                                             "--output-dir", str(self.output)]), 0)


if __name__ == "__main__":
    unittest.main()
