from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_vod_catalogs as vod  # noqa: E402


EXPECTED_HEADERS = {
    "User-Agent": "SKYTV-EPG-Builder/2.0",
    "Accept": "application/json,text/plain,application/x-mpegURL,*/*",
}
ACTION_CASES = (
    ("get_vod_categories", "", [{"category_id": "10"}]),
    ("get_vod_streams", "", [{"stream_id": 101}]),
    ("get_series_categories", "", [{"category_id": "20"}]),
    ("get_series", "", [{"series_id": 201}]),
    ("get_vod_info", "101", {"movie_data": {"stream_id": 101}}),
    ("get_series_info", "201", {"info": {"series_id": 201}}),
)


class ProviderResponse:
    def __init__(self, status: int, payload: object, *, redirect: bool = False) -> None:
        self.status_code = status
        self.is_redirect = redirect
        self.is_permanent_redirect = False
        self.headers = {"Location": "/player_api.php"} if redirect else {}
        self.body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int) -> object:
        del chunk_size
        yield self.body

    def close(self) -> None:
        self.closed = True


class HeaderSensitiveProvider:
    """Model the observed provider: default Python request headers get HTTP 461.

    Deliberately has no ``headers`` attribute. Correct behavior must come from
    the central request helper, including callers that supply a fresh session.
    """

    def __init__(self, payload: object, *, redirect: bool = False, status: int = 200) -> None:
        self.payload = payload
        self.redirect = redirect
        self.status = status
        self.calls: list[dict[str, object]] = []
        self.responses: list[ProviderResponse] = []

    def get(self, url: str, **kwargs: object) -> ProviderResponse:
        del url
        headers = dict(kwargs.get("headers", {}))
        self.calls.append({"headers": headers, "params": dict(kwargs.get("params", {}))})
        if headers != EXPECTED_HEADERS:
            response = ProviderResponse(461, {"error": "unsupported client"})
        elif self.redirect and len(self.calls) == 1:
            response = ProviderResponse(302, {}, redirect=True)
        else:
            response = ProviderResponse(self.status, self.payload)
        self.responses.append(response)
        return response


class VodProviderHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = vod.ServerConfig(
            server_id="server_2",
            base_url="https://private-provider.invalid",
            username="private-account",
            password="private-password",
        )

    def test_all_catalog_and_detail_actions_use_accepted_provider_headers(self) -> None:
        for action, item_id, payload in ACTION_CASES:
            with self.subTest(action=action):
                provider = HeaderSensitiveProvider(payload)
                self.assertEqual(
                    vod.request_panel_json(provider, self.config, action, item_id=item_id),
                    payload,
                )
                self.assertEqual(provider.calls[0]["headers"], EXPECTED_HEADERS)
                expected_params = {
                    "username": self.config.username,
                    "password": self.config.password,
                    "action": action,
                }
                if item_id:
                    expected_params["vod_id" if action == "get_vod_info" else "series_id"] = item_id
                self.assertEqual(provider.calls[0]["params"], expected_params)
                self.assertTrue(all(response.closed for response in provider.responses))

    def test_safe_redirect_preserves_headers_and_parameters_on_every_hop(self) -> None:
        for action, item_id, payload in ACTION_CASES:
            with self.subTest(action=action):
                provider = HeaderSensitiveProvider(payload, redirect=True)
                self.assertEqual(
                    vod.request_panel_json(provider, self.config, action, item_id=item_id),
                    payload,
                )
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[0], provider.calls[1])
                self.assertEqual(provider.calls[1]["headers"], EXPECTED_HEADERS)
                self.assertTrue(all(response.closed for response in provider.responses))

    def test_http_failure_names_action_without_disclosing_provider_credentials(self) -> None:
        for action, item_id, _payload in ACTION_CASES:
            with self.subTest(action=action):
                provider = HeaderSensitiveProvider(
                    {"error": "/".join((
                        self.config.base_url, self.config.username, self.config.password,
                    ))},
                    status=461,
                )
                with self.assertRaises(vod.CatalogBuildError) as caught:
                    vod.request_panel_json(provider, self.config, action, item_id=item_id)
                self.assertEqual(
                    str(caught.exception),
                    f"server_2: {action} provider returned HTTP 461.",
                )
                for secret in (self.config.base_url, self.config.username, self.config.password):
                    self.assertNotIn(secret, str(caught.exception))
                self.assertTrue(all(response.closed for response in provider.responses))


if __name__ == "__main__":
    unittest.main()
