"""Exercise the real ZIP-member boundary of a transferable prepared snapshot."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import app_catalog_closure as closure
import component_artifacts as transport
import publication_snapshots as snapshots


class SeriesResumeTransferLimitTests(unittest.TestCase):
    def test_flat_payload_boundary_round_trips_through_real_package_and_extractor(self):
        """19,994 payload files plus all wrappers/directories fit exactly 20,000."""
        with tempfile.TemporaryDirectory(prefix="resume-transfer-boundary-") as temporary:
            root = Path(temporary)
            public = root / "public"
            (public / "VOD").mkdir(parents=True)
            (public / "skytvplayer").mkdir()
            payloads = ["VOD/index.json", "skytvplayer/index.json"]
            payloads.extend(f"skytvplayer/item_{number:05d}.json" for number in range(19_992))
            budget = closure.check_transfer_members(payloads)
            self.assertEqual(budget, {"payloadFiles": 19_994, "directoryMembers": 4,
                                      "wrapperFiles": 2, "totalMembers": 20_000, "memberLimit": 20_000})

            # Real source tree and existing package helper; no fake ZipInfo list
            # or patched extraction limit. Empty additional files keep byte cost
            # tiny while preserving the actual file/member-count boundary.
            for name in payloads:
                (public / name).write_bytes(b"{}" if name.endswith("/index.json") else b"")
            snapshot = root / "sealed"
            manifest = snapshots.package("vod", public, snapshot)
            self.assertEqual(manifest["fileCount"], 19_994)

            archive = root / "accepted.zip"
            directory_members = {"snapshot/", "snapshot/files/"}
            for name in payloads:
                parent = Path(name).parent
                while parent != Path("."):
                    directory_members.add("snapshot/files/" + parent.as_posix() + "/")
                    parent = parent.parent
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
                for name in sorted(directory_members):
                    zipped.writestr(name, b"")
                for entry in manifest["files"]:
                    zipped.write(snapshot / "files" / entry["path"], "snapshot/files/" + entry["path"])
                zipped.write(snapshot / "snapshot.json", "snapshot/snapshot.json")
                zipped.writestr("report.json", b'{"mode":"synthetic_transfer_boundary"}')
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(len(zipped.infolist()), budget["totalMembers"])
                self.assertEqual(sum(member.is_dir() for member in zipped.infolist()), budget["directoryMembers"])

            extracted = root / "accepted"
            transport.extract_zip(archive, extracted)
            _, received = snapshots._snapshot(extracted / "snapshot", "vod")
            self.assertEqual(received, manifest)
            self.assertEqual(json.loads((extracted / "report.json").read_bytes())["mode"],
                             "synthetic_transfer_boundary")
            self.assertEqual(hashlib.sha256((extracted / "snapshot/snapshot.json").read_bytes()).hexdigest(),
                             hashlib.sha256((snapshot / "snapshot.json").read_bytes()).hexdigest())

            # A single additional payload exceeds the transport limit, even
            # though the publication package's own 20,000-file limit permits it.
            extra = "skytvplayer/one_too_many.json"
            with self.assertRaises(closure.ClosureError):
                closure.check_transfer_members([*payloads, extra])
            overflow = root / "overflow.zip"
            shutil.copyfile(archive, overflow)
            with zipfile.ZipFile(overflow, "a", compression=zipfile.ZIP_DEFLATED) as zipped:
                zipped.writestr("snapshot/files/" + extra, b"")
            with zipfile.ZipFile(overflow) as zipped:
                self.assertEqual(len(zipped.infolist()), 20_001)
            rejected = root / "rejected"
            with self.assertRaisesRegex(ValueError, "extraction limits"):
                transport.extract_zip(overflow, rejected)
            self.assertFalse(rejected.exists())

    def test_nested_payload_ancestors_are_counted_once_in_addition_to_wrappers(self):
        payloads = ["VOD/index.json", "skytvplayer/index.json",
                    "skytvplayer/nested/a/first.json", "skytvplayer/nested/a/second.json"]
        budget = closure.check_transfer_members(payloads)
        self.assertEqual(budget["payloadFiles"], 4)
        self.assertEqual(budget["directoryMembers"], 6)
        self.assertEqual(budget["wrapperFiles"], 2)
        self.assertEqual(budget["totalMembers"], 12)
        # Existing ancestors are shared, while a distinct branch costs one
        # additional payload member and one additional directory member.
        other_branch = closure.check_transfer_members([*payloads, "skytvplayer/nested/b/third.json"])
        self.assertEqual(other_branch["totalMembers"], budget["totalMembers"] + 2)


if __name__ == "__main__":
    unittest.main()
