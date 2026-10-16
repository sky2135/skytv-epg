from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_vod_catalogs as vod


NOW = "2026-10-16T12:00:00Z"
PRIVATE_URL = "https://private-provider.invalid/movie/private-user/private-password/1.mp4"


def sample_source() -> dict:
    return {
        "get_vod_categories": [], "get_series_categories": [],
        "get_vod_streams": [{"stream_id": "1", "name": "Example film", "language": "en", "adult": False, "direct_source": PRIVATE_URL}],
        "get_series": [{"series_id": "2", "name": "Example series", "language": "en", "adult": False}],
    }


class SnapshotTests(unittest.TestCase):
    def prepare(self, root: Path) -> Path:
        source = root / "input"
        source.mkdir()
        (source / "server_1_vod_bundle.json").write_text(json.dumps(sample_source()), encoding="utf-8")
        return source

    def publish(self, source: Path, output: Path, snapshot: Path | None = None) -> int:
        args = ["--server", "server_1", "--input-dir", str(source), "--output-dir", str(output), "--now-utc", NOW]
        if snapshot is not None:
            args.extend(["--snapshot-dir", str(snapshot)])
        with contextlib.redirect_stdout(io.StringIO()):
            return vod.main(args)

    def test_private_snapshot_retains_raw_input_but_public_catalog_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.prepare(root)
            snapshot = root / "private" / "snapshots"
            output = root / "public" / "VOD"
            self.assertEqual(self.publish(source, output, snapshot), 0)
            raw = (snapshot / "server_1_vod_bundle.json").read_text(encoding="utf-8")
            self.assertIn(PRIVATE_URL, raw)
            for path in output.iterdir():
                data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
                self.assertNotIn(b"private-provider.invalid", data)
                self.assertNotIn(b"private-password", data)
                self.assertNotIn(b"direct_source", data)
            summary = vod.validate_publication(output, expected_servers=["server_1"])
            self.assertEqual(summary["movieCount"], 1)
            self.assertEqual(summary["seriesCount"], 1)

    def test_snapshot_replays_deterministically_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vod, "request_panel_json") as request:
            root = Path(folder)
            source = self.prepare(root)
            snapshot = root / "private" / "snapshots"
            first, second = root / "public" / "VOD", root / "replay" / "VOD"
            self.publish(source, first, snapshot)
            self.publish(snapshot, second)
            self.assertEqual({path.name: path.read_bytes() for path in first.iterdir()}, {path.name: path.read_bytes() for path in second.iterdir()})
            request.assert_not_called()

    def test_snapshot_directory_inside_publication_tree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.prepare(root)
            output = root / "public" / "VOD"
            for snapshot in (root / "public", root / "public" / "private", output, output / "private"):
                with self.subTest(snapshot=snapshot), self.assertRaises(vod.CatalogBuildError):
                    self.publish(source, output, snapshot)
                self.assertFalse((snapshot / "server_1_vod_bundle.json").exists())

    def test_atomic_snapshot_does_not_overwrite_public_hardlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.prepare(root)
            output, snapshot = root / "public" / "VOD", root / "private"
            output.mkdir(parents=True)
            snapshot.mkdir()
            public_file = output.parent / "keep.json"
            public_file.write_text('{"keep":true}', encoding="utf-8")
            try:
                os.link(public_file, snapshot / "server_1_vod_bundle.json")
            except OSError:
                self.skipTest("This filesystem does not permit hardlinks.")
            self.publish(source, output, snapshot)
            self.assertEqual(public_file.read_text(encoding="utf-8"), '{"keep":true}')
            self.assertIn(PRIVATE_URL, (snapshot / "server_1_vod_bundle.json").read_text(encoding="utf-8"))

    def test_validate_only_rejects_snapshot_publication_option(self) -> None:
        with self.assertRaises(vod.CatalogBuildError):
            vod.main(["--validate-only", "--snapshot-dir", "private"])


class Response:
    def __init__(self, location: str | None = None) -> None:
        self.headers = {} if location is None else {"Location": location}
        self.is_redirect = location is not None
        self.is_permanent_redirect = self.is_redirect
        self.status_code = 302 if self.is_redirect else 200
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int):
        yield b'[{"stream_id":"1"}]'


class RedirectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = vod.ServerConfig("server_1", "https://provider.invalid", "private-user", "private-password")
        self.params = {"username": self.config.username, "password": self.config.password, "action": "get_vod_streams"}

    def test_same_endpoint_redirect_preserves_original_parameters(self) -> None:
        for location in ("/player_api.php", "https://provider.invalid/player_api.php", "?" + urlencode(self.params)):
            with self.subTest(location=location):
                redirect, final = Response(location), Response()
                session = mock.Mock()
                session.get.side_effect = [redirect, final]
                self.assertEqual(vod.request_panel_json(session, self.config, "get_vod_streams"), [{"stream_id": "1"}])
                self.assertTrue(redirect.closed)
                self.assertTrue(final.closed)
                for call in session.get.call_args_list:
                    self.assertEqual(call.kwargs["params"], self.params)
                    self.assertIs(call.kwargs["allow_redirects"], False)
                    self.assertNotIn("?", call.args[0])

    def test_same_host_http_to_https_upgrade_is_allowed(self) -> None:
        config = vod.ServerConfig("server_1", "http://provider.invalid", "private-user", "private-password")
        session = mock.Mock()
        session.get.side_effect = [Response("https://provider.invalid/player_api.php"), Response()]
        vod.request_panel_json(session, config, "get_vod_streams")
        self.assertEqual(session.get.call_args.args[0], "https://provider.invalid/player_api.php")

    def test_unsafe_or_rewritten_redirects_are_rejected_before_second_call(self) -> None:
        locations = [
            "https://elsewhere.invalid/player_api.php", "http://provider.invalid/player_api.php",
            "https://provider.invalid:444/player_api.php", "/another_api.php",
            "https://provider.invalid:0/player_api.php",
            "https://user:pass@provider.invalid/player_api.php", "/player_api.php#fragment",
            "?" + urlencode(dict(self.params, action="get_series")),
            "?" + urlencode(dict(self.params, password="rewritten")),
            "?" + urlencode(self.params) + "&password=private-password",
            "?" + urlencode(self.params) + "&series_id=2",
        ]
        for location in locations:
            with self.subTest(location=location):
                response = Response(location)
                session = mock.Mock()
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError) as caught:
                    vod.request_panel_json(session, self.config, "get_vod_streams")
                self.assertEqual(session.get.call_count, 1)
                self.assertTrue(response.closed)
                self.assertNotIn(self.config.password, str(caught.exception))
                self.assertNotIn(location, str(caught.exception))

    def test_detail_item_id_cannot_change_and_redirect_loop_is_bounded(self) -> None:
        session = mock.Mock()
        params = dict(self.params, action="get_series_info", series_id="changed")
        session.get.return_value = Response("?" + urlencode(params))
        with self.assertRaises(vod.CatalogBuildError):
            vod.request_panel_json(session, self.config, "get_series_info", item_id="123")
        self.assertEqual(session.get.call_count, 1)
        responses = [Response("/player_api.php") for _ in range(5)]
        session.get.side_effect = responses
        session.get.reset_mock()
        with self.assertRaises(vod.CatalogBuildError):
            vod.request_panel_json(session, self.config, "get_vod_streams")
        self.assertEqual(session.get.call_count, 5)
        self.assertTrue(all(response.closed for response in responses))

    def test_unknown_action_is_rejected_without_network_access(self) -> None:
        session = mock.Mock()
        with self.assertRaises(vod.CatalogBuildError):
            vod.request_panel_json(session, self.config, "unsupported")
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
