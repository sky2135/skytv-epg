from __future__ import annotations
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import build_vod_catalogs as vod
import install_vod_catalogs as installer
from test_vod_catalogs import sample_source


class PreparedVODInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "prepared"
        self.output = self.root / "published"
        self.index = vod.publish_catalogs(self.source, [("server_1", sample_source(), None)],
                                         generated_at="2026-10-21T12:00:00Z")

    def test_install_preserves_every_referenced_byte_without_refetching(self):
        result = installer.install(self.source, self.output, expected_servers=["server_1"])
        self.assertEqual(result, {"serverCount": 1, "artifactCount": 4})
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()},
                         {p.name: p.read_bytes() for p in self.output.iterdir()})
        self.assertEqual(vod.validate_publication(self.source), vod.validate_publication(self.output))

    def test_unreferenced_private_files_are_never_copied(self):
        (self.source / "private.txt").write_text("private credentials", encoding="utf-8")
        installer.install(self.source, self.output, expected_servers=["server_1"])
        self.assertFalse((self.output / "private.txt").exists())

    def test_corrupt_prepared_data_cannot_switch_existing_release(self):
        self.output.mkdir()
        old = b'{"old":"release"}'
        (self.output / "index.json").write_bytes(old)
        entry = self.index["servers"][0]
        manifest = json.loads((self.source / entry["manifestFile"]).read_text(encoding="utf-8"))
        (self.source / manifest["catalogs"]["movies"]["file"]).write_bytes(b"corrupt")
        with self.assertRaises(vod.CatalogBuildError):
            installer.install(self.source, self.output, expected_servers=["server_1"])
        self.assertEqual((self.output / "index.json").read_bytes(), old)

    def test_expected_servers_and_separate_paths_are_required(self):
        with self.assertRaises(vod.CatalogBuildError):
            installer.install(self.source, self.output)
        with self.assertRaises(vod.CatalogBuildError):
            installer.install(self.source, self.source, expected_servers=["server_1"])

    def test_index_change_during_validation_cannot_switch_existing_release(self):
        self.output.mkdir()
        old = b'{"old":"release"}'
        (self.output / "index.json").write_bytes(old)
        validate = vod.validate_publication

        def swap_after_validation(*args, **kwargs):
            result = validate(*args, **kwargs)
            (self.source / "index.json").write_text(json.dumps(dict(self.index, servers=[], serverCount=0)), encoding="utf-8")
            return result

        with mock.patch.object(vod, "validate_publication", side_effect=swap_after_validation):
            with self.assertRaises(vod.CatalogBuildError):
                installer.install(self.source, self.output, expected_servers=["server_1"])
        self.assertEqual((self.output / "index.json").read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
