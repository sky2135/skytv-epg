from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import requests


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_channel_inventory as sync  # noqa: E402


class Response:
    def __init__(
        self, status: int, payload: object = None, *, raw: bytes | None = None,
        location: str = "", declared_bytes: int | None = None,
        stream_error: Exception | None = None,
    ):
        self.status_code = status
        self.body = json.dumps(payload).encode("utf-8") if raw is None else raw
        self.headers: dict[str, str] = {}
        if location:
            self.headers["Location"] = location
        if declared_bytes is not None:
            self.headers["Content-Length"] = str(declared_bytes)
        self.stream_error = stream_error
        self.closed = False

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.body
        if self.stream_error is not None:
            raise self.stream_error

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *events):
        self.events = list(events)
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[Response] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.events:
            raise AssertionError("Unexpected extra provider request")
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        self.responses.append(event)
        return event


class InventoryTransportRetryTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "https://panel.private.test/player_api.php"
        self.username = "private-reader"
        self.password = "do-not-print-password"
        self.payload = [{"stream_id": "17", "name": "News"}]
        output = mock.patch.object(sync, "print", create=True)
        self.output = output.start()
        self.addCleanup(output.stop)

    def probe(self, session):
        return sync.request_panel_json(
            session, self.endpoint, self.username, self.password, "get_live_streams",
            allow_insecure_http=False,
        )

    def assert_secret_safe(self, issue):
        for secret in (self.endpoint, self.username, self.password):
            self.assertNotIn(secret, issue)

    def test_http_502_then_success_closes_before_bounded_backoff(self):
        session = Session(Response(502, {"error": self.password}), Response(200, self.payload))

        def sleep(seconds):
            self.assertEqual(seconds, 1)
            self.assertTrue(all(response.closed for response in session.responses))

        with mock.patch.object(sync.time, "sleep", side_effect=sleep) as backoff:
            result = self.probe(session)
        self.assertEqual(result.payload, self.payload)
        self.assertEqual(result.issue, "")
        self.assertEqual(len(session.calls), 2)
        backoff.assert_called_once_with(1)
        self.assertTrue(all(response.closed for response in session.responses))
        self.output.assert_called_once_with(
            "WARNING: Provider inventory JSON request returned HTTP 502; retrying (attempt 2/3).",
            flush=True,
        )

    def test_all_5xx_and_429_failures_stop_after_three_attempts(self):
        for status in (429, 500, 501, 502, 503, 504, 522, 599):
            with self.subTest(status=status):
                session = Session(*(Response(status, {"error": self.password}) for _ in range(3)))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.issue, f"get_live_streams: HTTP {status}")
                self.assertEqual(len(session.calls), 3)
                self.assertEqual(backoff.call_args_list, [mock.call(1), mock.call(2)])
                self.assertTrue(all(response.closed for response in session.responses))
                self.assert_secret_safe(result.issue)

    def test_429_then_success_preserves_credentials_only_in_original_request_params(self):
        session = Session(Response(429), Response(200, self.payload))
        with mock.patch.object(sync.time, "sleep"):
            result = self.probe(session)
        self.assertEqual(result.payload, self.payload)
        for url, kwargs in session.calls:
            self.assertEqual(url, self.endpoint)
            self.assertEqual(kwargs["params"], {
                "username": self.username, "password": self.password, "action": "get_live_streams",
            })
            self.assertFalse(kwargs["allow_redirects"])

    def test_actual_network_failures_retry_without_echoing_exception_urls(self):
        errors = (
            requests.ConnectionError, requests.Timeout, requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout, requests.exceptions.ProxyError,
        )
        for error_type in errors:
            with self.subTest(error_type=error_type):
                session = Session(error_type(self.endpoint + "?password=" + self.password), Response(200, self.payload))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertEqual(result.payload, self.payload)
                self.assertEqual(len(session.calls), 2)
                backoff.assert_called_once_with(1)

    def test_network_failure_exhaustion_is_bounded_and_secret_safe(self):
        session = Session(*(requests.ConnectionError(self.password) for _ in range(3)))
        with mock.patch.object(sync.time, "sleep") as backoff:
            result = self.probe(session)
        self.assertEqual(result.issue, "get_live_streams: A provider network request failed.")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(backoff.call_args_list, [mock.call(1), mock.call(2)])
        self.assert_secret_safe(result.issue)
        self.assertEqual(self.output.call_count, 2)
        for call in self.output.call_args_list:
            self.assert_secret_safe(call.args[0])
            self.assertIn("transport failure", call.args[0])

    def test_interrupted_stream_is_closed_and_retried(self):
        broken = Response(200, raw=b"[", stream_error=requests.exceptions.ChunkedEncodingError(self.password))
        session = Session(broken, Response(200, self.payload))
        with mock.patch.object(sync.time, "sleep", side_effect=lambda _: self.assertTrue(broken.closed)):
            result = self.probe(session)
        self.assertEqual(result.payload, self.payload)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_retry_restarts_original_endpoint_after_same_host_redirect(self):
        session = Session(
            Response(302, location="/temporary/player_api.php"), Response(502),
            Response(200, self.payload),
        )
        with mock.patch.object(sync.time, "sleep") as backoff:
            result = self.probe(session)
        self.assertEqual(result.payload, self.payload)
        self.assertEqual([url for url, _ in session.calls], [
            self.endpoint, "https://panel.private.test/temporary/player_api.php", self.endpoint,
        ])
        backoff.assert_called_once_with(1)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_auth_and_other_nontransient_http_statuses_are_terminal(self):
        for status in (400, 401, 403, 404, 408, 425, 461):
            with self.subTest(status=status):
                session = Session(Response(status, {"error": self.password}))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertEqual(result.status_code, status)
                self.assertEqual(len(session.calls), 1)
                backoff.assert_not_called()
                self.assertTrue(session.responses[0].closed)
                self.assert_secret_safe(result.issue)

    def test_malformed_json_and_utf8_are_terminal(self):
        for raw in (b"<html>upstream error</html>", b"\xff"):
            with self.subTest(raw=raw):
                session = Session(Response(200, raw=raw))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertIn("non-JSON", result.issue)
                self.assertEqual(len(session.calls), 1)
                backoff.assert_not_called()
                self.assertTrue(session.responses[0].closed)

    def test_unsafe_redirect_cannot_receive_credentials_or_trigger_retries(self):
        for target in (
            "https://hostile.test/player_api.php", "https://panel.private.test/player_api.php?auth=1",
            "http://panel.private.test/player_api.php",
        ):
            with self.subTest(target=target):
                session = Session(Response(302, location=target))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertIn("unsafe credential redirect", result.issue)
                self.assertEqual(len(session.calls), 1)
                backoff.assert_not_called()
                self.assertTrue(session.responses[0].closed)

    def test_oversize_response_is_terminal_even_when_http_status_is_retryable(self):
        for status in (200, 502):
            for declared in (True, False):
                with self.subTest(status=status, declared=declared):
                    response = Response(status, raw=b"12345", declared_bytes=5 if declared else None)
                    session = Session(response)
                    with mock.patch.object(sync, "MAX_PANEL_JSON_BYTES", 4), mock.patch.object(sync.time, "sleep") as backoff:
                        result = self.probe(session)
                    self.assertIn("size limit", result.issue)
                    self.assertEqual(len(session.calls), 1)
                    backoff.assert_not_called()
                    self.assertTrue(response.closed)

    def test_tls_invalid_url_and_arbitrary_exception_types_are_terminal(self):
        for error_type in (
            requests.exceptions.SSLError, requests.exceptions.InvalidURL,
            requests.exceptions.InvalidSchema, requests.exceptions.TooManyRedirects,
            requests.exceptions.ContentDecodingError, RuntimeError, ValueError,
        ):
            with self.subTest(error_type=error_type):
                session = Session(error_type(self.endpoint + self.password))
                with mock.patch.object(sync.time, "sleep") as backoff:
                    result = self.probe(session)
                self.assertTrue(result.issue)
                self.assertEqual(len(session.calls), 1)
                backoff.assert_not_called()
                self.assert_secret_safe(result.issue)

    def test_valid_json_auth_schema_and_reflection_failures_do_not_retry_actions(self):
        config = sync.ServerConfig(
            "server_1", "Server 1", "https://panel.private.test", self.username, self.password
        )
        cases = (
            (Response(200, {"user_info": {"auth": "0"}}),),
            (Response(200, []), Response(200, [{"name": "Missing ID"}])),
            (Response(200, []), Response(200, [{"stream_id": "17", "name": self.password}])),
        )
        for responses in cases:
            with self.subTest(request_count=len(responses)):
                session = Session(*responses)
                with mock.patch.object(sync.time, "sleep") as backoff, \
                        mock.patch.object(sync, "fetch_m3u_inventory", return_value=None):
                    with self.assertRaises(sync.SyncError) as caught:
                        sync.fetch_panel_inventory(session, config, allow_insecure_http=False)
                self.assertEqual(len(session.calls), len(responses))
                backoff.assert_not_called()
                self.assertTrue(all(response.closed for response in session.responses))
                self.assert_secret_safe(str(caught.exception))


if __name__ == "__main__":
    unittest.main()
