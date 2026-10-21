from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_vod_catalogs as vod


class Response:
    def __init__(self, payload, status=200):
        self.body = json.dumps(payload).encode("utf-8")
        self.status_code = status
        self.headers = {}
        self.is_redirect = self.is_permanent_redirect = False
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class InventorySession:
    def __init__(self, overrides=None):
        self.payloads = {
            "get_vod_categories": [[]],
            "get_vod_streams": [[{"stream_id": 1, "name": "Movie"}]],
            "get_series_categories": [[]],
            "get_series": [[{"series_id": 2, "name": "Series"}]],
        }
        self.payloads.update(overrides or {})
        self.calls, self.responses = [], []

    def get(self, url, **kwargs):
        action = kwargs["params"]["action"]
        self.calls.append(action)
        item = self.payloads[action].pop(0)
        response = item if isinstance(item, Response) else Response(item)
        self.responses.append(response)
        return response


class EmptyInventoryRetryTests(unittest.TestCase):
    def setUp(self):
        self.config = vod.ServerConfig("server_2", "https://provider.invalid", "private-account", "private-password")
        self.stderr = io.StringIO()
        self.sleep = self.enterContext(patch.object(vod.time, "sleep"))
        self.enterContext(patch.object(vod.sys, "stderr", self.stderr))

    def fetch(self, session):
        return vod.fetch_live_inputs(session, self.config, fetch_details=False)

    def assert_safe_output(self):
        for secret in (self.config.base_url, self.config.username, self.config.password):
            self.assertNotIn(secret, self.stderr.getvalue())

    def test_explicit_empty_movie_or_series_list_is_confirmed_once(self):
        for action in ("get_vod_streams", "get_series"):
            for empty in ([], {"data": []}, {"results": []}, {"response": []}, {"items": []}):
                with self.subTest(action=action, empty=empty):
                    self.sleep.reset_mock()
                    valid = [{"stream_id": 1, "name": "Movie"}] if action == "get_vod_streams" else [{"series_id": 2, "name": "Series"}]
                    session = InventorySession({action: [empty, valid]})
                    budget = vod.DownloadBudget()
                    with patch.object(vod, "DownloadBudget", return_value=budget):
                        source = self.fetch(session)
                    self.assertEqual(source[action], valid)
                    self.assertEqual(session.calls.count(action), 2)
                    self.assertEqual(len(session.calls), 5)
                    self.sleep.assert_called_once_with(1)
                    self.assertEqual(budget.consumed, sum(len(r.body) for r in session.responses))
                    self.assertTrue(all(r.closed for r in session.responses))
                    self.assert_safe_output()

    def test_persistent_empty_inventory_still_refuses_publication(self):
        for action in ("get_vod_streams", "get_series"):
            with self.subTest(action=action):
                self.sleep.reset_mock()
                session = InventorySession({action: [[], []]})
                with self.assertRaisesRegex(vod.CatalogBuildError, "refusing to replace the existing VOD release"):
                    self.fetch(session)
                self.assertEqual(session.calls.count(action), 2)
                self.sleep.assert_called_once_with(1)

    def test_malformed_or_unsupported_payloads_are_not_semantically_retried(self):
        for payload in (None, False, {}, 3, "not a list", {"unknown": []}, ["invalid scalar row"]):
            with self.subTest(payload=payload):
                self.sleep.reset_mock()
                session = InventorySession({"get_vod_streams": [payload]})
                with self.assertRaises(vod.CatalogBuildError):
                    self.fetch(session)
                self.assertEqual(session.calls.count("get_vod_streams"), 1)
                self.sleep.assert_not_called()

    def test_authentication_failures_are_not_retried_as_empty_inventory(self):
        for response in (Response([], 401), Response([], 403), Response({"user_info": {"auth": 0}, "data": []})):
            with self.subTest(status=response.status_code):
                self.sleep.reset_mock()
                session = InventorySession({"get_vod_streams": [response]})
                with self.assertRaisesRegex(vod.CatalogBuildError, "credentials were rejected"):
                    self.fetch(session)
                self.assertEqual(session.calls.count("get_vod_streams"), 1)
                self.sleep.assert_not_called()
                self.assertTrue(response.closed)
                self.assert_safe_output()

    def test_empty_categories_do_not_request_confirmation(self):
        session = InventorySession()
        self.fetch(session)
        self.assertEqual(len(session.calls), 4)
        self.sleep.assert_not_called()

    def test_confirmation_cannot_reset_the_cumulative_download_limit(self):
        valid = [{"stream_id": 1, "name": "Movie"}]
        session = InventorySession({"get_vod_streams": [[], valid]})
        initial_bytes = len(json.dumps([]).encode()) * 3 + len(json.dumps([{"series_id": 2, "name": "Series"}]).encode())
        budget = vod.DownloadBudget(limit=initial_bytes + 1)
        with patch.object(vod, "DownloadBudget", return_value=budget):
            with self.assertRaisesRegex(vod.CatalogBuildError, "downloads exceed the total size limit"):
                self.fetch(session)
        self.assertEqual(session.calls.count("get_vod_streams"), 2)
        self.assertGreater(budget.consumed, budget.limit)
        self.sleep.assert_called_once_with(1)

    def test_offline_inputs_never_enter_the_network_retry_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "server_2_vod_bundle.json").write_text(json.dumps({"get_vod_streams": [], "get_series": []}), encoding="utf-8")
            with patch.object(vod, "request_panel_json", side_effect=AssertionError("network called")):
                source = vod.load_offline_inputs(root, "server_2")
            self.assertEqual(source["get_vod_streams"], [])
            self.assertEqual(source["get_series"], [])
            self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
