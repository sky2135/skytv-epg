from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_epg_streaming as epg


class NativePanelRetryTests(unittest.TestCase):
    def response(self, status: int, retry_after=None):
        response = mock.MagicMock()
        response.status_code = status
        response.headers = {} if retry_after is None else {"Retry-After": retry_after}
        response.iter_content.return_value = [b"<tv/>"]
        return response

    def download(self, outcomes, *, maximum_attempts=3, now=1_800_000_000, cache_dir=""):
        session = mock.MagicMock()
        session.headers = {}
        session.get.side_effect = outcomes
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "panel.xmltv"
            with mock.patch.dict(epg.os.environ, {"SKYTV_PANEL_CACHE_DIR": str(cache_dir)}), \
                    mock.patch.object(epg, "panel_credentials", return_value=(
                "https://private-panel.example", "fixture-user", "fixture-password",
            )), mock.patch.object(epg.requests, "Session", return_value=session), \
                    mock.patch.object(epg.time, "sleep") as sleep, \
                    mock.patch.object(epg.time, "time", return_value=now), \
                    mock.patch.object(epg.random, "uniform", return_value=0.0):
                def verify_closed_before_pause(_seconds):
                    for outcome in outcomes[:session.get.call_count]:
                        if not isinstance(outcome, Exception):
                            outcome.close.assert_called_once()
                sleep.side_effect = verify_closed_before_pause
                try:
                    result = epg.download_panel_xmltv(
                        "server_3", destination, maximum_attempts=maximum_attempts,
                    )
                    return session, sleep, result, destination.read_bytes()
                except epg.BuildError as error:
                    self.assertFalse(destination.exists())
                    self.assertFalse(destination.with_suffix(".xmltv.part").exists())
                    return session, sleep, error, None

    def test_transient_502_uses_longer_backoff_and_closes_before_sleep(self):
        first, second, final = [self.response(status) for status in (502, 502, 200)]
        session, sleep, result, content = self.download([first, second, final])
        self.assertEqual(content, b"<tv/>")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(15.0), mock.call(45.0)])
        for response in (first, second, final):
            response.close.assert_called_once()
        session.close.assert_called_once()
        self.assertEqual(result[1]["bytes"], 5)

    def test_retry_after_delta_and_http_date_are_minimum_delays(self):
        now = 1_800_000_000
        for status, header in (
            (429, "75"),
            (503, format_datetime(datetime.fromtimestamp(now + 75, timezone.utc), usegmt=True)),
        ):
            with self.subTest(status=status):
                first, final = self.response(status, header), self.response(200)
                session, sleep, _result, content = self.download([first, final], now=now)
                self.assertEqual(content, b"<tv/>")
                sleep.assert_called_once_with(75.0)
                self.assertEqual(session.get.call_count, 2)

    def test_long_retry_after_defers_without_requesting_early(self):
        first = self.response(503, "86400")
        session, sleep, error, _ = self.download([first])
        self.assertIsInstance(error, epg.PanelSourceUnavailable)
        self.assertEqual(error.attempts, 1)
        self.assertEqual(error.next_attempt_at, 1_800_086_400)
        self.assertEqual(error.last_result, "HTTP 503")
        session.get.assert_called_once()
        first.close.assert_called_once()
        session.close.assert_called_once()
        sleep.assert_not_called()
        for secret in ("private-panel.example", "fixture-user", "fixture-password", "86400"):
            self.assertNotIn(secret, str(error))

    def test_one_attempt_probe_preserves_final_retry_after_deadline(self):
        first = self.response(429, "90")
        session, sleep, error, _ = self.download([first], maximum_attempts=1)
        self.assertIsInstance(error, epg.PanelSourceUnavailable)
        self.assertEqual(error.attempts, 1)
        self.assertEqual(error.next_attempt_at, 1_800_000_090)
        session.get.assert_called_once()
        sleep.assert_not_called()

    def test_absurd_numeric_retry_after_defers_instead_of_being_ignored(self):
        seconds = int("9" * 100)
        session, sleep, error, _ = self.download([self.response(429, str(seconds))])
        self.assertIsInstance(error, epg.PanelSourceUnavailable)
        self.assertEqual(error.next_attempt_at, 1_800_000_000 + seconds)
        session.get.assert_called_once()
        sleep.assert_not_called()

    def test_cooldown_survives_calls_and_expiry_allows_recovery(self):
        with tempfile.TemporaryDirectory() as cache:
            first = self.response(503, "900")
            session, sleep, error, _ = self.download([first], cache_dir=cache)
            self.assertEqual(error.next_attempt_at, 1_800_000_900)
            session.get.assert_called_once()
            sleep.assert_not_called()
            session, sleep, deferred, _ = self.download([], cache_dir=cache, now=1_800_000_899)
            self.assertIsInstance(deferred, epg.PanelSourceUnavailable)
            self.assertEqual(deferred.attempts, 0)
            self.assertEqual(deferred.next_attempt_at, error.next_attempt_at)
            session.get.assert_not_called()
            session.close.assert_not_called()
            sleep.assert_not_called()
            _session, _sleep, result, content = self.download(
                [self.response(200)], cache_dir=cache, now=1_800_000_900,
            )
            self.assertEqual(content, b"<tv/>")
            self.assertIsInstance(result, tuple)
            self.assertFalse((Path(cache) / "server_3.cooldown.json").exists())

    def test_permanent_http_errors_stop_without_retry_or_synthetic_eligibility(self):
        for status in (400, 401, 403, 404, 410):
            with self.subTest(status=status):
                first = self.response(status)
                session, sleep, error, _ = self.download([first])
                self.assertIsInstance(error, epg.BuildError)
                self.assertNotIsInstance(error, epg.PanelSourceUnavailable)
                self.assertIn(f"HTTP {status}", str(error))
                session.get.assert_called_once()
                first.close.assert_called_once()
                session.close.assert_called_once()
                sleep.assert_not_called()

    def test_retry_after_invalid_or_expired_uses_normal_delay(self):
        for header in ("private reflected text", "-1", "1.5", "x" * 201,
                       "Wed, 01 Jan 2020 00:00:00 GMT"):
            with self.subTest(header=header[:40]):
                _session, sleep, _result, content = self.download([
                    self.response(503, header), self.response(200),
                ])
                self.assertEqual(content, b"<tv/>")
                sleep.assert_called_once_with(15.0)

    def test_network_errors_retry_but_tls_failures_remain_terminal(self):
        secret = "https://private-panel.example/?password=fixture-password"
        session, sleep, error, _ = self.download([
            epg.requests.ConnectionError(secret), epg.requests.Timeout(secret),
            epg.requests.ConnectionError(secret),
        ])
        self.assertIsInstance(error, epg.PanelSourceUnavailable)
        self.assertEqual(error.attempts, 3)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn(secret, str(error))
        session, sleep, error, _ = self.download([epg.requests.exceptions.SSLError(secret)])
        self.assertNotIsInstance(error, epg.PanelSourceUnavailable)
        self.assertIn("TLS validation failed", str(error))
        session.get.assert_called_once()
        sleep.assert_not_called()

    def test_attempt_limit_cannot_raise_production_bound(self):
        for attempts in (0, 4, -1, True, 1.5):
            with self.subTest(attempts=attempts), mock.patch.object(epg.requests, "Session") as session:
                with self.assertRaisesRegex(epg.BuildError, "attempt limit"):
                    epg.download_panel_xmltv("server_3", Path("unused.xmltv"), maximum_attempts=attempts)
                session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
