from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.parse import urlencode


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_vod_catalogs as vod  # noqa: E402


class Response:
    def __init__(
        self,
        status: int = 200,
        *,
        body: bytes = b'[{"category_id":"1"}]',
        headers: dict[str, str] | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.is_redirect = status in {301, 302, 303, 307, 308}
        self.is_permanent_redirect = status in {301, 308}
        self.body = body
        self.stream_error = stream_error
        self.close_count = 0

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self.body
        if self.stream_error is not None:
            raise self.stream_error

    def close(self) -> None:
        self.close_count += 1


class VodTransportRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = vod.ServerConfig(
            "server_2", "https://provider.invalid", "private-account", "private-password"
        )
        self.sleep = mock.patch.object(vod.time, "sleep").start()
        self.stderr = io.StringIO()
        mock.patch.object(vod.sys, "stderr", self.stderr).start()
        self.addCleanup(mock.patch.stopall)

    def fetch(self, session, **kwargs):
        return vod.request_panel_json(
            session, self.config, "get_vod_categories", **kwargs
        )

    def assert_no_secret_output(self, error: Exception | None = None) -> None:
        output = self.stderr.getvalue() + (str(error) if error is not None else "")
        for secret in (self.config.base_url, self.config.username, self.config.password):
            self.assertNotIn(secret, output)

    def test_temporary_502_is_closed_before_bounded_retry_and_success(self) -> None:
        failed, successful = Response(502), Response()
        session = mock.Mock()
        session.get.side_effect = [failed, successful]
        self.sleep.side_effect = lambda _seconds: self.assertEqual(failed.close_count, 1)
        self.assertEqual(self.fetch(session), [{"category_id": "1"}])
        self.assertEqual(session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual((failed.close_count, successful.close_count), (1, 1))
        for call in session.get.call_args_list:
            self.assertEqual(call.args[0], self.config.base_url + "/player_api.php")
            self.assertEqual(call.kwargs["headers"], vod.PROVIDER_HEADERS)
            self.assertEqual(call.kwargs["params"], {
                "username": self.config.username,
                "password": self.config.password,
                "action": "get_vod_categories",
            })
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertIs(call.kwargs["stream"], True)
        self.assertEqual(
            self.stderr.getvalue(),
            "WARNING: server_2: get_vod_categories HTTP 502; retrying after attempt 1/3.\n",
        )
        self.assert_no_secret_output()

    def test_transient_http_failures_exhaust_exactly_three_attempts(self) -> None:
        responses = [Response(502), Response(503), Response(504)]
        session = mock.Mock()
        session.get.side_effect = responses
        with self.assertRaises(vod.CatalogBuildError) as caught:
            self.fetch(session)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual([response.close_count for response in responses], [1, 1, 1])
        self.assertEqual(str(caught.exception), "server_2: get_vod_categories provider returned HTTP 504.")
        self.assert_no_secret_output(caught.exception)

    def test_429_and_5xx_are_retryable_but_other_http_failures_are_terminal(self) -> None:
        for status in (429, 500, 501, 502, 503, 504, 599):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                failed, successful = Response(status), Response()
                session = mock.Mock()
                session.get.side_effect = [failed, successful]
                self.fetch(session)
                self.assertEqual(session.get.call_count, 2)
                self.sleep.assert_called_once_with(1)
                self.assertEqual((failed.close_count, successful.close_count), (1, 1))
        for status in (400, 401, 403, 404, 408, 425, 461, 600):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                response = Response(status)
                session = mock.Mock()
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError):
                    self.fetch(session)
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(response.close_count, 1)

    def test_actual_transient_network_exceptions_retry_without_echoing_url(self) -> None:
        for error_type in (vod.requests.Timeout, vod.requests.ConnectionError):
            with self.subTest(error_type=error_type):
                self.sleep.reset_mock()
                session = mock.Mock()
                response = Response()
                session.get.side_effect = [error_type(self.config.base_url + "?password=" + self.config.password), response]
                self.fetch(session)
                self.assertEqual(session.get.call_count, 2)
                self.sleep.assert_called_once_with(1)
                self.assertEqual(response.close_count, 1)
                self.assert_no_secret_output()

    def test_network_exhaustion_has_safe_terminal_message_and_suppressed_cause(self) -> None:
        session = mock.Mock()
        session.get.side_effect = vod.requests.ConnectionError(
            self.config.base_url + "?username=" + self.config.username + "&password=" + self.config.password
        )
        with self.assertRaises(vod.CatalogBuildError) as caught:
            self.fetch(session)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(str(caught.exception), "server_2: provider request failed.")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assert_no_secret_output(caught.exception)

    def test_tls_and_deterministic_request_errors_are_terminal(self) -> None:
        for error_type in (
            vod.requests.exceptions.SSLError,
            vod.requests.exceptions.InvalidURL,
            vod.requests.exceptions.TooManyRedirects,
            vod.requests.exceptions.ContentDecodingError,
            vod.requests.RequestException,
        ):
            with self.subTest(error_type=error_type):
                self.sleep.reset_mock()
                session = mock.Mock()
                session.get.side_effect = error_type(self.config.password)
                with self.assertRaises(vod.CatalogBuildError) as caught:
                    self.fetch(session)
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assert_no_secret_output(caught.exception)

    def test_invalid_content_encoding_during_streaming_is_terminal(self) -> None:
        response = Response(
            body=b"[",
            stream_error=vod.requests.exceptions.ContentDecodingError(self.config.password),
        )
        session = mock.Mock()
        session.get.return_value = response
        with self.assertRaises(vod.CatalogBuildError) as caught:
            self.fetch(session)
        session.get.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(response.close_count, 1)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assert_no_secret_output(caught.exception)

    def test_interrupted_body_retry_keeps_consumed_download_budget(self) -> None:
        partial = Response(body=b"[", stream_error=vod.requests.exceptions.ChunkedEncodingError(self.config.password))
        successful = Response()
        session = mock.Mock()
        session.get.side_effect = [partial, successful]
        budget = vod.DownloadBudget(limit=1000)
        self.sleep.side_effect = lambda _seconds: self.assertEqual(partial.close_count, 1)
        self.fetch(session, budget=budget)
        self.assertEqual(budget.consumed, len(partial.body) + len(successful.body))
        self.assertEqual((partial.close_count, successful.close_count), (1, 1))
        self.sleep.assert_called_once_with(1)
        self.assert_no_secret_output()

    def test_partial_body_budget_cannot_be_reset_by_retry(self) -> None:
        partial = Response(body=b"[", stream_error=vod.requests.exceptions.ChunkedEncodingError("interrupted"))
        response = Response(body=b"[]")
        session = mock.Mock()
        session.get.side_effect = [partial, response]
        budget = vod.DownloadBudget(limit=2)
        with self.assertRaisesRegex(vod.CatalogBuildError, "total size limit"):
            self.fetch(session, budget=budget)
        self.assertEqual(budget.consumed, 3)
        self.assertEqual(session.get.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual((partial.close_count, response.close_count), (1, 1))

    def test_retry_restarts_original_endpoint_and_redirect_budget(self) -> None:
        config = vod.ServerConfig("server_2", "http://provider.invalid", "private-account", "private-password")
        responses = [
            Response(302, headers={"Location": "https://provider.invalid/player_api.php"}),
            Response(502),
            Response(302, headers={"Location": "https://provider.invalid/player_api.php"}),
            Response(),
        ]
        session = mock.Mock()
        session.get.side_effect = responses
        vod.request_panel_json(session, config, "get_vod_categories")
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            "http://provider.invalid/player_api.php", "https://provider.invalid/player_api.php",
            "http://provider.invalid/player_api.php", "https://provider.invalid/player_api.php",
        ])
        self.assertEqual([response.close_count for response in responses], [1, 1, 1, 1])
        self.sleep.assert_called_once_with(1)

    def test_auth_payload_and_malformed_json_are_terminal(self) -> None:
        for body in (b'{"user_info":{"auth":0}}', b'{"user_info":{"status":"Expired"}}', b"not json", b"\xff"):
            with self.subTest(body=body):
                self.sleep.reset_mock()
                session = mock.Mock()
                response = Response(body=body)
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError):
                    self.fetch(session)
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(response.close_count, 1)

    def test_schema_and_secret_reflection_validation_stay_terminal(self) -> None:
        for body, validate in (
            (b'{"data":"invalid list"}', lambda payload: vod.normalize_rows(payload, "provider rows")),
            (b'{"title":"private-password"}', lambda payload: vod.ensure_no_secret_reflection(payload, self.config)),
        ):
            with self.subTest(body=body):
                self.sleep.reset_mock()
                session = mock.Mock()
                response = Response(body=body)
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError):
                    validate(self.fetch(session))
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(response.close_count, 1)

    def test_unsafe_or_credential_rewriting_redirects_are_terminal(self) -> None:
        params = {"username": self.config.username, "password": "rewritten", "action": "get_vod_categories"}
        for location in ("https://unapproved.invalid/player_api.php", "?" + urlencode(params)):
            with self.subTest(location=location):
                session = mock.Mock()
                response = Response(302, headers={"Location": location})
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError) as caught:
                    self.fetch(session)
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(response.close_count, 1)
                self.assert_no_secret_output(caught.exception)

    def test_response_and_total_byte_limits_are_terminal(self) -> None:
        for response, kwargs in (
            (Response(headers={"Content-Length": "100"}), {"maximum_bytes": 2}),
            (Response(body=b"[{}]"), {"maximum_bytes": 2}),
            (Response(body=b"[{}]"), {"budget": vod.DownloadBudget(limit=2)}),
        ):
            with self.subTest(kwargs=kwargs):
                session = mock.Mock()
                session.get.return_value = response
                with self.assertRaises(vod.CatalogBuildError):
                    self.fetch(session, **kwargs)
                session.get.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(response.close_count, 1)


if __name__ == "__main__":
    unittest.main()
