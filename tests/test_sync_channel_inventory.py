from __future__ import annotations

import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_epg_streaming as streaming  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402


def mapping_row(
    server_id: str,
    stream_id: str,
    channel_name: str,
    *,
    category_name: str = "General",
    action: str = "APPROVED",
    source: str = "epgshare01",
    epg_feed: str = "ALL_SOURCES1",
    epg_id: str = "Fixture.test",
) -> dict[str, str]:
    row = {column: "" for column in streaming.SHEET_COLUMNS}
    row.update(
        {
            "server_id": server_id,
            "server_label": server_id.replace("_", " ").title(),
            "stream_id": stream_id,
            "enabled": "TRUE",
            "channel_name": channel_name,
            "canonical_name": channel_name,
            "category_id": "cat",
            "category_name": category_name,
            "sort_priority": "1000",
            "action": action,
            "source": source,
            "epg_feed": epg_feed,
            "epg_id": epg_id,
            "metadata_status": "approved",
            "metadata_locked": "TRUE",
        }
    )
    return row


def table(rows: list[dict[str, str]]) -> sync.MappingTable:
    columns = list(streaming.SHEET_COLUMNS)
    return sync.MappingTable(columns, columns, rows)


def inventory(
    server_id: str,
    channels: list[dict[str, str]],
    *,
    source: str = "player_api",
) -> sync.PanelInventory:
    return sync.PanelInventory(
        server_id=server_id,
        server_label=server_id.replace("_", " ").title(),
        categories=[],
        channels=channels,
        source=source,
    )


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        return None


def mapping_values(rows: list[dict[str, str]]) -> list[list[str]]:
    return [list(streaming.SHEET_COLUMNS)] + [
        [str(row.get(column, "")) for column in streaming.SHEET_COLUMNS]
        for row in rows
    ]


class ReviewUpdateSession:
    """Stateful Google Sheets double for exact REVIEW-row patch tests."""

    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        status_code: int = 200,
        valid_payload: bool = True,
        commit: bool = True,
        raise_after_commit: bool = False,
        mutate_non_target_after_commit: bool = False,
    ) -> None:
        self.values = mapping_values(rows)
        self.status_code = status_code
        self.valid_payload = valid_payload
        self.commit = commit
        self.raise_after_commit = raise_after_commit
        self.mutate_non_target_after_commit = mutate_non_target_after_commit
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.values_reads = 0

    def get(self, url, **_kwargs):
        if "/values/" in url:
            self.values_reads += 1
            return FakeResponse(200, {"values": self.values})
        return FakeResponse(
            200,
            {
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 42,
                            "title": "Mappings",
                            "gridProperties": {
                                "rowCount": 1000,
                                "columnCount": len(streaming.SHEET_COLUMNS),
                            },
                        }
                    }
                ]
            },
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        requests = kwargs["json"]["requests"]
        if self.commit:
            for request in requests:
                update = request["updateCells"]
                cell_range = update["range"]
                row_index = cell_range["startRowIndex"]
                column_index = cell_range["startColumnIndex"]
                cells = update["rows"][0]["values"]
                for offset, cell in enumerate(cells):
                    self.values[row_index][column_index + offset] = cell[
                        "userEnteredValue"
                    ]["stringValue"]
            if self.mutate_non_target_after_commit and len(self.values) > 2:
                notes_index = list(streaming.SHEET_COLUMNS).index("notes")
                self.values[2][notes_index] = "concurrent unrelated edit"
        if self.raise_after_commit:
            raise ConnectionError("connection ended after request was sent")
        payload = (
            {
                "spreadsheetId": "a" * 30,
                "replies": [{} for _request in requests],
            }
            if self.valid_payload
            else {"spreadsheetId": "a" * 30, "replies": []}
        )
        return FakeResponse(self.status_code, payload)


class PayloadValidationTests(unittest.TestCase):
    def test_account_metadata_is_not_accepted_as_channel_rows(self) -> None:
        payload = {
            "user_info": {"auth": 1, "status": "Active"},
            "server_info": {"url": "panel.example", "port": "8080"},
        }
        self.assertEqual(
            sync.normalize_panel_action_rows(payload, "get_live_streams"), []
        )

    def test_duplicate_stream_ids_fail_instead_of_disappearing(self) -> None:
        payload = [
            {"stream_id": "7", "name": "One"},
            {"stream_id": "7", "name": "Two"},
        ]
        with self.assertRaisesRegex(sync.SyncError, "duplicate stream_id"):
            sync.normalize_panel_action_rows(payload, "get_live_streams")

    def test_partial_stream_identity_fails_instead_of_dropping_row(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "ID or name but not both"):
            sync.normalize_panel_action_rows(
                [{"stream_id": "7", "name": ""}], "get_live_streams"
            )

    def test_non_channel_object_inside_stream_list_fails(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "non-channel row"):
            sync.normalize_panel_action_rows(
                [{"unexpected": "metadata"}], "get_live_streams"
            )

    def test_every_malformed_explicit_stream_item_rejects_api_collection(self) -> None:
        malformed_items = (
            None,
            "not-an-object",
            {},
            {"unexpected": "metadata"},
            {"stream_id": "", "name": ""},
            {"stream_id": "9"},
            {"name": "Missing ID"},
        )
        for malformed in malformed_items:
            with self.subTest(malformed=malformed):
                with self.assertRaises(sync.SyncError):
                    sync.normalize_panel_action_rows(
                        {
                            "streams": [
                                {"stream_id": "1", "name": "Valid"},
                                malformed,
                            ]
                        },
                        "get_live_streams",
                    )

    def test_identical_stream_rows_across_wrapper_aliases_are_deduplicated(self) -> None:
        stream = {
            "stream_id": "1",
            "name": "Channel A",
            "category_id": "news",
        }
        rows = sync.normalize_panel_action_rows(
            {"streams": [dict(stream)], "data": [dict(stream)]},
            "get_live_streams",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stream_id"], "1")
        self.assertEqual(rows[0]["name"], "Channel A")

    def test_conflicting_stream_rows_across_wrapper_aliases_fail(self) -> None:
        payload = {
            "streams": [{"stream_id": "1", "name": "Channel A"}],
            "data": [{"stream_id": "1", "name": "Channel B"}],
        }
        with self.assertRaisesRegex(sync.SyncError, "conflicting stream_id"):
            sync.normalize_panel_action_rows(payload, "get_live_streams")

    def test_m3u_fallback_keeps_live_rows_and_drops_vod(self) -> None:
        content = """#EXTM3U
#EXTINF:-1 tvg-id="Live.test" tvg-name="Live" group-title="Sports" tvg-logo="http://u:p@example.test/logo.png",=Formula Channel
http://panel.test/live/user/secret/12345.ts
#EXTINF:-1 tvg-id="Movie.test" group-title="Movies",Movie
http://panel.test/movie/user/secret/999.mp4
"""
        categories, channels = sync.parse_xtream_m3u(
            content, username="user", password="secret"
        )
        self.assertEqual(len(categories), 1)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["stream_id"], "12345")
        self.assertEqual(channels[0]["name"], "=Formula Channel")
        self.assertEqual(channels[0]["stream_icon"], "")
        self.assertNotIn("secret", json.dumps(channels))

    def test_m3u_duplicate_live_ids_fail(self) -> None:
        content = """#EXTM3U
#EXTINF:-1,One
http://panel.test/live/u/p/123.ts
#EXTINF:-1,Two
http://panel.test/live/u/p/123.m3u8
"""
        with self.assertRaisesRegex(sync.SyncError, "duplicate live stream IDs"):
            sync.parse_xtream_m3u(content, username="u", password="p")

    def test_m3u_live_entry_without_name_fails(self) -> None:
        content = """#EXTM3U
#EXTINF:-1
http://panel.test/live/u/p/123.ts
"""
        with self.assertRaisesRegex(sync.SyncError, "no channel name"):
            sync.parse_xtream_m3u(content, username="u", password="p")

    def test_m3u_dangling_extinf_fails(self) -> None:
        with self.assertRaisesRegex(sync.SyncError, "ended after EXTINF"):
            sync.parse_xtream_m3u(
                "#EXTM3U\n#EXTINF:-1,Channel\n", username="u", password="p"
            )

    def test_synthetic_m3u_id_redacts_credentials_and_survives_rotation(self) -> None:
        captured: list[bytes] = []

        class Digest:
            def hexdigest(self):
                return "a" * 64

        def capture_digest(value):
            captured.append(value)
            return Digest()

        with mock.patch.object(sync.hashlib, "sha256", side_effect=capture_digest):
            first = sync.m3u_stream_id(
                "https://panel.test/live/alice/old%2Fsecret/news-main.ts",
                "News Main",
                "",
                username="alice",
                password="old/secret",
            )
        self.assertEqual(first, "m3u_" + "a" * 20)
        self.assertNotIn(b"alice", captured[0])
        self.assertNotIn(b"old", captured[0])
        self.assertNotIn(b"secret", captured[0])

        old_id = sync.m3u_stream_id(
            "https://panel.test/live/alice/old%2Fsecret/news-main.ts",
            "News Main",
            "",
            username="alice",
            password="old/secret",
        )
        rotated_id = sync.m3u_stream_id(
            "https://panel.test/live/alice/new%2Fsecret/news-main.ts",
            "News Main",
            "",
            username="alice",
            password="new/secret",
        )
        self.assertEqual(old_id, rotated_id)

        overlapping_old = sync.m3u_stream_id(
            "https://panel.test/live/alice/alice-secret/news-main.ts",
            "News Main",
            "",
            username="alice",
            password="alice-secret",
        )
        overlapping_rotated = sync.m3u_stream_id(
            "https://panel.test/live/bob/bob-secret/news-main.ts",
            "News Main",
            "",
            username="bob",
            password="bob-secret",
        )
        self.assertEqual(overlapping_old, overlapping_rotated)

    def test_embedded_encoded_m3u_credentials_are_rejected_before_hashing(self) -> None:
        unsafe_url = (
            "https://panel.test/live/user-alice/token-old%2Fsecret-live/"
            "news-main.ts"
        )
        with mock.patch.object(sync.hashlib, "sha256") as digest:
            with self.assertRaisesRegex(sync.SyncError, "unsafe segment") as caught:
                sync.m3u_stream_id(
                    unsafe_url,
                    "News Main",
                    "",
                    username="alice",
                    password="old/secret",
                )
        digest.assert_not_called()
        self.assertNotIn("alice", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_numeric_terminal_credential_is_never_returned_as_stream_id(self) -> None:
        captured: list[bytes] = []
        real_sha256 = sync.hashlib.sha256

        def capture_digest(value):
            captured.append(value)
            return real_sha256(value)

        with mock.patch.object(sync.hashlib, "sha256", side_effect=capture_digest):
            stream_id = sync.m3u_stream_id(
                "https://panel.test/live/alice/1234/1234.ts",
                "Fallback",
                "",
                username="alice",
                password="1234",
            )
        self.assertTrue(stream_id.startswith("m3u_"))
        self.assertNotEqual(stream_id, "1234")
        self.assertNotIn(b"alice", captured[0])
        self.assertNotIn(b"1234", captured[0])

    def test_short_common_m3u_username_does_not_match_ordinary_path_text(self) -> None:
        synthetic = sync.m3u_stream_id(
            "https://panel.test/live/s/pass/news.ts",
            "World News",
            "",
            username="s",
            password="pass",
        )
        self.assertTrue(synthetic.startswith("m3u_"))
        self.assertEqual(
            sync.m3u_stream_id(
                "https://panel.test/live/s/pass/77.ts",
                "World News",
                "",
                username="s",
                password="pass",
            ),
            "77",
        )

    def test_network_exception_never_echoes_secret(self) -> None:
        class FailingSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("https://panel/?password=do-not-print")

        probe = sync.request_panel_json(
            FailingSession(),
            "https://panel.test/player_api.php",
            "user",
            "do-not-print",
            "get_live_streams",
            allow_insecure_http=False,
        )
        self.assertTrue(probe.issue)
        self.assertNotIn("do-not-print", probe.issue)

    def test_malformed_live_api_collection_uses_authenticated_m3u_fallback(self) -> None:
        class RawResponse:
            def __init__(self, status_code: int, content: bytes):
                self.status_code = status_code
                self.content = content
                self.headers: dict[str, str] = {}

            def close(self) -> None:
                return None

        class PanelSession:
            def get(self, url, **kwargs):
                params = kwargs.get("params", {})
                action = params.get("action", "")
                if action == "get_live_categories":
                    return FakeResponse(
                        200, [{"category_id": "sports", "category_name": "Sports"}]
                    )
                if action == "get_live_streams":
                    return FakeResponse(
                        200,
                        {
                            "streams": [
                                {"stream_id": "1", "name": "Valid"},
                                None,
                            ]
                        },
                    )
                self.assert_m3u_request(url, params)
                return RawResponse(
                    200,
                    b'#EXTM3U\n#EXTINF:-1 group-title="Sports",Fallback\n'
                    b'https://panel.test/live/user/secret/77.ts\n',
                )

            @staticmethod
            def assert_m3u_request(url, params):
                if not url.endswith("/get.php") or params.get("type") not in {
                    "m3u_plus", "m3u"
                }:
                    raise AssertionError("Expected authenticated M3U fallback")

        result = sync.fetch_panel_inventory(
            PanelSession(),
            sync.ServerConfig(
                "server_1", "Server 1", "https://panel.test", "user", "secret"
            ),
            allow_insecure_http=False,
        )
        self.assertEqual(result.source, "authenticated_m3u")
        self.assertEqual([channel["stream_id"] for channel in result.channels], ["77"])
        self.assertNotIn("secret", json.dumps(result.channels))

    def test_api_reflected_secret_is_rejected_before_inventory_returns(self) -> None:
        class ReflectionSession:
            def get(self, _url, **kwargs):
                action = kwargs.get("params", {}).get("action", "")
                if action == "get_live_categories":
                    return FakeResponse(
                        200,
                        [{"category_id": "1", "category_name": "user-reflect"}],
                    )
                return FakeResponse(
                    200,
                    [
                        {
                            "stream_id": "77",
                            "name": "News pa%2Fssword",
                            "category_id": "1",
                            "epg_channel_id": "https://panel.private.test/guide",
                        }
                    ],
                )

        config = sync.ServerConfig(
            "server_1",
            "Server 1",
            "https://panel.private.test",
            "user-reflect",
            "pa/ssword",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "persistence was blocked") as caught:
                sync.fetch_panel_inventory(
                    ReflectionSession(), config, allow_insecure_http=False
                )
            self.assertEqual(list(root.iterdir()), [])
        error = str(caught.exception)
        self.assertNotIn("user-reflect", error)
        self.assertNotIn("ssword", error)
        self.assertNotIn("panel.private.test", error)

    def test_m3u_reflected_encoded_secret_is_rejected_before_return(self) -> None:
        class RawResponse:
            def __init__(self, content):
                self.status_code = 200
                self.content = content
                self.headers = {}

            def close(self):
                return None

        class ReflectionM3USession:
            def get(self, _url, **kwargs):
                if kwargs.get("params", {}).get("action"):
                    return FakeResponse(200, [])
                return RawResponse(
                    b'#EXTM3U\n#EXTINF:-1 group-title="pa%2Fssword",News\n'
                    b'https://panel.private.test/live/user-reflect/pa%2Fssword/77.ts\n'
                )

        config = sync.ServerConfig(
            "server_1",
            "Server 1",
            "https://panel.private.test",
            "user-reflect",
            "pa/ssword",
        )
        with self.assertRaisesRegex(sync.SyncError, "persistence was blocked") as caught:
            sync.fetch_panel_inventory(
                ReflectionM3USession(), config, allow_insecure_http=False
            )
        self.assertNotIn("ssword", str(caught.exception))

    def test_short_provider_password_fails_before_network_access(self) -> None:
        class NoNetworkSession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("short password must fail before network access")

        with self.assertRaisesRegex(sync.SyncError, "password is too short"):
            sync.fetch_panel_inventory(
                NoNetworkSession(),
                sync.ServerConfig(
                    "server_1", "Server 1", "https://panel.test", "u", "abc"
                ),
                allow_insecure_http=False,
            )

    def test_common_username_exact_provider_values_do_not_false_match(self) -> None:
        cases = (
            ("news", "77", "News", "news", "News"),
            ("1", "1", "Channel 1", "general", "General"),
        )
        for username, stream_id, channel_name, category_id, category_name in cases:
            with self.subTest(username=username):
                policy = sync.provider_reflection_needles(
                    sync.ServerConfig(
                        "server_1",
                        "Server 1",
                        "https://panel.private.test",
                        username,
                        "1234",
                    ),
                    ["https://panel.private.test"],
                )
                sync.validate_provider_inventory_secret_safe(
                    [
                        {
                            "category_id": category_id,
                            "category_name": category_name,
                        }
                    ],
                    [
                        {
                            "stream_id": stream_id,
                            "name": channel_name,
                            "category_id": category_id,
                            "category_name": category_name,
                        }
                    ],
                    policy,
                )

    def test_structured_username_reflection_is_blocked(self) -> None:
        config = sync.ServerConfig(
            "server_1",
            "Server 1",
            "https://panel.private.test",
            "news",
            "1234",
        )
        policy = sync.provider_reflection_needles(
            config, ["https://panel.private.test"]
        )
        for reflected in ("username=news", "/live/news/redacted/77"):
            with self.subTest(reflected=reflected):
                with self.assertRaisesRegex(
                    sync.SyncError, "persistence was blocked"
                ) as caught:
                    sync.validate_provider_inventory_secret_safe(
                        [],
                        [{"stream_id": "77", "name": reflected}],
                        policy,
                )
                self.assertNotIn("news", str(caught.exception).casefold())

        one_policy = sync.provider_reflection_needles(
            sync.ServerConfig(
                "server_1",
                "Server 1",
                "https://panel.private.test",
                "1",
                "1234",
            ),
            ["https://panel.private.test"],
        )
        with self.assertRaisesRegex(sync.SyncError, "persistence was blocked"):
            sync.validate_provider_inventory_secret_safe(
                [], [{"stream_id": "77", "name": "username: 1"}], one_policy
            )

    def test_password_substring_reflection_is_always_blocked(self) -> None:
        policy = sync.provider_reflection_needles(
            sync.ServerConfig(
                "server_1",
                "Server 1",
                "https://panel.private.test",
                "news",
                "1234",
            ),
            ["https://panel.private.test"],
        )
        with self.assertRaisesRegex(sync.SyncError, "persistence was blocked") as caught:
            sync.validate_provider_inventory_secret_safe(
                [],
                [{"stream_id": "77", "name": "Premium 12345 Sports"}],
                policy,
            )
        self.assertNotIn("1234", str(caught.exception))

    def test_distinctive_username_substring_reflection_is_blocked(self) -> None:
        username = "A9x$Q2p!Lm7Z"
        self.assertTrue(sync.distinctive_provider_value(username))
        policy = sync.provider_reflection_needles(
            sync.ServerConfig(
                "server_1",
                "Server 1",
                "https://panel.private.test",
                username,
                "1234",
            ),
            ["https://panel.private.test"],
        )
        with self.assertRaisesRegex(sync.SyncError, "persistence was blocked"):
            sync.validate_provider_inventory_secret_safe(
                [],
                [{"stream_id": "77", "name": f"Premium {username} Sports"}],
                policy,
            )

    def test_conflicting_wrapper_streams_use_authenticated_m3u_fallback(self) -> None:
        class RawResponse:
            def __init__(self, status_code: int, content: bytes):
                self.status_code = status_code
                self.content = content
                self.headers: dict[str, str] = {}

            def close(self) -> None:
                return None

        class PanelSession:
            def get(self, url, **kwargs):
                params = kwargs.get("params", {})
                action = params.get("action", "")
                if action == "get_live_categories":
                    return FakeResponse(200, [])
                if action == "get_live_streams":
                    return FakeResponse(
                        200,
                        {
                            "streams": [{"stream_id": "1", "name": "Channel A"}],
                            "data": [{"stream_id": "1", "name": "Channel B"}],
                        },
                    )
                if not url.endswith("/get.php"):
                    raise AssertionError("Expected authenticated M3U fallback")
                return RawResponse(
                    200,
                    b'#EXTM3U\n#EXTINF:-1 group-title="Sports",Fallback\n'
                    b'https://panel.test/live/user/secret/77.ts\n',
                )

        result = sync.fetch_panel_inventory(
            PanelSession(),
            sync.ServerConfig(
                "server_1", "Server 1", "https://panel.test", "user", "secret"
            ),
            allow_insecure_http=False,
        )
        self.assertEqual(result.source, "authenticated_m3u")
        self.assertEqual([channel["stream_id"] for channel in result.channels], ["77"])
        self.assertNotIn("secret", json.dumps(result.channels))


class MappingAndComparisonTests(unittest.TestCase):
    def test_false_and_zero_sheet_values_are_preserved(self) -> None:
        values: list[object] = [""] * len(streaming.SHEET_COLUMNS)
        index = {name: position for position, name in enumerate(streaming.SHEET_COLUMNS)}
        values[index["server_id"]] = "server_1"
        values[index["stream_id"]] = 0
        values[index["enabled"]] = False
        values[index["channel_name"]] = "Zero"
        parsed = sync.parse_table_values([list(streaming.SHEET_COLUMNS), values])
        self.assertEqual(parsed.rows[0]["stream_id"], "0")
        self.assertEqual(parsed.rows[0]["enabled"], "False")

    def test_sheet_headers_must_remain_in_version_one_order(self) -> None:
        headers = list(streaming.SHEET_COLUMNS)
        headers[0], headers[1] = headers[1], headers[0]
        with self.assertRaisesRegex(sync.SyncError, "Version 1 order"):
            sync.parse_table_values([headers])

    def test_server_one_never_copies_native_epg_or_provider_icon(self) -> None:
        inv = inventory(
            "server_1",
            [
                {
                    "stream_id": "88",
                    "name": "Gurbani Live",
                    "category_id": "religion",
                    "category_name": "Punjabi Religious",
                    "epg_channel_id": "native-secret-id",
                    "stream_icon": "https://panel.test/live/user/password/logo.png",
                    "num": "8",
                }
            ],
        )
        row = sync.new_mapping_row(
            inv, inv.channels[0], discovered_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual(row["action"], "REVIEW")
        self.assertEqual(row["enabled"], "FALSE")
        self.assertEqual(row["metadata_status"], "review")
        self.assertEqual(row["source"], "epgshare01")
        self.assertEqual(row["epg_id"], "")
        self.assertEqual(row["logo_url"], "")

    def test_server_two_native_id_is_candidate_only(self) -> None:
        inv = inventory(
            "server_2",
            [
                {
                    "stream_id": "99",
                    "name": "Channel",
                    "category_id": "general",
                    "category_name": "General",
                    "epg_channel_id": "Panel.Channel",
                    "stream_icon": "",
                    "num": "",
                }
            ],
        )
        row = sync.new_mapping_row(
            inv, inv.channels[0], discovered_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual(row["action"], "REVIEW")
        self.assertEqual(row["enabled"], "FALSE")
        self.assertEqual(row["source"], "panel")
        self.assertEqual(row["epg_feed"], "panel")
        self.assertEqual(row["epg_id"], "Panel.Channel")

    def test_quality_suffix_and_case_do_not_create_false_name_drift(self) -> None:
        current = table([mapping_row("server_1", "1", "News HD")])
        inv = inventory(
            "server_1",
            [
                {
                    "stream_id": "1",
                    "name": "news fhd",
                    "category_id": "cat",
                    "category_name": " general ",
                }
            ],
        )
        new, changed, missing = sync.compare_inventory(
            current, [inv], discovered_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual((new, changed, missing), ([], [], []))

    def test_possible_id_reuse_is_reported_and_snapshot_only_is_quarantined(self) -> None:
        original = mapping_row(
            "server_1", "1", "Sports One", category_name="Sports"
        )
        current = table([original])
        inv = inventory(
            "server_1",
            [
                {
                    "stream_id": "1",
                    "name": "Kids Planet",
                    "category_id": "kids",
                    "category_name": "Children",
                }
            ],
        )
        _new, changed, _missing = sync.compare_inventory(
            current, [inv], discovered_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual(changed[0]["risk"], "POSSIBLE_STREAM_ID_REUSE_REVIEW_REQUIRED")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "effective.csv"
            sync.write_mapping_snapshot(path, current, changed_rows=changed)
            effective = sync.parse_mapping_csv(path.read_bytes())
        self.assertEqual(effective.rows[0]["action"], "REVIEW")
        self.assertEqual(effective.rows[0]["enabled"], "FALSE")
        self.assertEqual(effective.rows[0]["epg_id"], "Fixture.test")
        self.assertEqual(original["action"], "APPROVED")
        self.assertEqual(original["enabled"], "TRUE")

    def test_unrelated_names_in_same_category_are_possible_id_reuse(self) -> None:
        examples = (
            ("ESPN HD", "Fox Sports 1 FHD"),
            ("BBC One", "BBC Two"),
            ("Fox Sports 1", "Fox Sports 2"),
            ("Sky Sports Football", "Sky Sports Cricket"),
            ("ESPN", "ESPN Deportes"),
            ("HBO", "HBO Family"),
            ("CNN", "CNN International"),
            ("TV", "CNN"),
            ("HBO East", "HBO West"),
            ("CNN US", "CNN Canada"),
            ("ESPN UK", "ESPN India"),
            ("Sports North", "Sports South"),
        )
        for old_name, new_name in examples:
            with self.subTest(old_name=old_name, new_name=new_name):
                self.assertTrue(
                    sync.possible_id_reuse(
                        old_name, new_name, "Sports", "Sports"
                    )
                )
        self.assertFalse(
            sync.possible_id_reuse("BBC One HD", "bbc one fhd", "News", "News")
        )
        self.assertFalse(
            sync.possible_id_reuse("CNN TV", "CNN Channel", "News", "News")
        )

    def test_adult_category_boundary_quarantines_even_when_name_is_unchanged(self) -> None:
        self.assertTrue(
            sync.possible_id_reuse(
                "Channel 100", "Channel 100", "General", "FOR ADULTS"
            )
        )
        self.assertTrue(
            sync.possible_id_reuse(
                "Channel 100", "Channel 100", "XXX", "General"
            )
        )
        self.assertFalse(
            sync.possible_id_reuse(
                "Adult Swim", "Adult Swim", "Adult Swim", "Entertainment"
            )
        )

    def test_generic_numbered_name_category_change_is_conservative(self) -> None:
        self.assertTrue(
            sync.possible_id_reuse(
                "Channel 100", "Channel 100", "Sports", "Kids"
            )
        )
        self.assertTrue(
            sync.possible_id_reuse("Feed 1", "Feed 1", "East", "West")
        )
        self.assertFalse(
            sync.possible_id_reuse(
                "BBC One", "BBC One", "UK Entertainment", "UK General"
            )
        )

    def test_low_stream_id_overlap_blocks_mass_append(self) -> None:
        rows = [mapping_row("server_1", str(number), f"Old {number}") for number in range(100)]
        channels = [
            {"stream_id": str(1000 + number), "name": f"New {number}"}
            for number in range(100)
        ]
        issues = sync.inventory_overlap_issues(table(rows), [inventory("server_1", channels)])
        self.assertEqual(len(issues), 1)
        self.assertIn("mass append was blocked", issues[0])


class ReportsAndSheetsTests(unittest.TestCase):
    def test_private_snapshot_bundle_deduplicates_and_accounts_quarantine_keys(self) -> None:
        runnable_quarantine = mapping_row(
            "server_3", "7001", "Old Sports Name", category_name="Sports"
        )
        unaffected = mapping_row(
            "server_3", "7002", "Unaffected News", category_name="News"
        )
        already_disabled = mapping_row(
            "server_3", "7003", "Already Under Review", action="REVIEW"
        )
        already_disabled["enabled"] = "FALSE"
        current_risk = {
            "server_id": "server_3",
            "stream_id": "7001",
            "risk": "POSSIBLE_STREAM_ID_REUSE_REVIEW_REQUIRED",
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            effective_path = root / "effective.csv"
            authoritative_path = root / "authoritative.csv"
            manifest_path = root / "manifest.json"
            validation = sync.write_private_mapping_snapshot_bundle(
                effective_path=effective_path,
                authoritative_path=authoritative_path,
                manifest_path=manifest_path,
                table=table([runnable_quarantine, unaffected, already_disabled]),
                changed_rows=[current_risk],
                persistent_quarantine_keys=[
                    ("server_3", "7001"),
                    ("server_3", "7001"),
                    ("server_3", "7003"),
                ],
            )

            stats = validation["servers"]["server_3"]
            self.assertEqual(stats["authoritative_rows"], 3)
            self.assertEqual(stats["authoritative_runnable_rows"], 2)
            self.assertEqual(stats["effective_rows"], 3)
            self.assertEqual(stats["effective_runnable_rows"], 1)
            self.assertEqual(stats["quarantined_rows"], 2)
            self.assertEqual(
                stats["quarantined_authoritative_runnable_rows"], 1
            )
            self.assertEqual(stats["quarantined_already_ineligible_rows"], 1)

            effective = streaming.parse_mapping_csv(
                effective_path.read_bytes(), {"server_3"}
            )
            by_stream = {row.stream_id: row for row in effective}
            self.assertFalse(by_stream["7001"].runtime_eligible)
            self.assertTrue(by_stream["7002"].runtime_eligible)
            self.assertFalse(by_stream["7003"].runtime_eligible)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["authoritative_sha256"],
                streaming.sha256_file(authoritative_path),
            )
            self.assertEqual(
                manifest["effective_sha256"],
                streaming.sha256_file(effective_path),
            )
            serialized_manifest = json.dumps(manifest, sort_keys=True)
            self.assertNotIn("7001", serialized_manifest)
            self.assertNotIn("7003", serialized_manifest)

    def test_orphan_open_alert_blocks_every_snapshot_bundle_file(self) -> None:
        current = table(
            [mapping_row("server_3", "7051", "Present Mapping Channel")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            effective_path = root / "effective.csv"
            authoritative_path = root / "authoritative.csv"
            manifest_path = root / "manifest.json"
            with self.assertRaisesRegex(
                sync.SyncError,
                "quarantine identities are missing.*server_3=1",
            ):
                sync.write_private_mapping_snapshot_bundle(
                    effective_path=effective_path,
                    authoritative_path=authoritative_path,
                    manifest_path=manifest_path,
                    table=current,
                    persistent_quarantine_keys=[
                        ("server_3", "orphan-not-in-mappings")
                    ],
                )
            self.assertFalse(effective_path.exists())
            self.assertFalse(authoritative_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_reserved_snapshot_reason_cannot_be_copied_into_google_sheet(self) -> None:
        for quarantined in (False, True):
            with self.subTest(quarantined=quarantined):
                copied = mapping_row(
                    "server_3", "7061", "Copied System Reason Channel"
                )
                copied["reason"] = streaming.EFFECTIVE_QUARANTINE_REASON
                persistent = [("server_3", "7061")] if quarantined else []
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    effective_path = root / "effective.csv"
                    authoritative_path = root / "authoritative.csv"
                    manifest_path = root / "manifest.json"
                    with self.assertRaisesRegex(
                        sync.SyncError, "reserved effective-snapshot"
                    ):
                        sync.write_private_mapping_snapshot_bundle(
                            effective_path=effective_path,
                            authoritative_path=authoritative_path,
                            manifest_path=manifest_path,
                            table=table([copied]),
                            persistent_quarantine_keys=persistent,
                        )
                    self.assertFalse(effective_path.exists())
                    self.assertFalse(authoritative_path.exists())
                    self.assertFalse(manifest_path.exists())

    def test_private_snapshot_manifest_binds_both_snapshot_files(self) -> None:
        current = table(
            [mapping_row("server_3", "7101", "Hash Bound Channel")]
        )
        risk = {
            "server_id": "server_3",
            "stream_id": "7101",
            "risk": "POSSIBLE_STREAM_ID_REUSE_REVIEW_REQUIRED",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            effective_path = root / "effective.csv"
            authoritative_path = root / "authoritative.csv"
            manifest_path = root / "manifest.json"
            sync.write_private_mapping_snapshot_bundle(
                effective_path=effective_path,
                authoritative_path=authoritative_path,
                manifest_path=manifest_path,
                table=current,
                changed_rows=[risk],
            )
            authoritative = authoritative_path.read_bytes()
            effective = effective_path.read_bytes()
            manifest = manifest_path.read_bytes()

            for changed_authoritative, changed_effective in (
                (authoritative + b"\n", effective),
                (authoritative, effective + b"\n"),
            ):
                with self.subTest(
                    changed=(
                        "authoritative"
                        if changed_authoritative != authoritative
                        else "effective"
                    )
                ):
                    validation = streaming.validate_private_mapping_snapshots(
                        changed_authoritative,
                        changed_effective,
                        {"server_3"},
                    )
                    with self.assertRaisesRegex(
                        streaming.BuildError, "manifest does not match"
                    ):
                        streaming.parse_and_validate_mapping_snapshot_manifest(
                            manifest, validation
                        )

    def test_sync_summary_reports_only_per_server_quarantine_accounting(self) -> None:
        current = table(
            [
                mapping_row(
                    "server_3",
                    "private-stream-7201",
                    "Old Private Sports Name",
                    category_name="Sports",
                ),
                mapping_row(
                    "server_3",
                    "private-stream-7202",
                    "Stable Private News Name",
                    category_name="News",
                ),
            ]
        )
        provider = inventory(
            "server_3",
            [
                {
                    "stream_id": "private-stream-7201",
                    "name": "Completely Different Kids Name",
                    "category_name": "Children",
                },
                {
                    "stream_id": "private-stream-7202",
                    "name": "Stable Private News Name",
                    "category_name": "News",
                },
            ],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reports = root / "reports"
            effective_path = root / "private" / "effective.csv"
            summary = sync.run_sync(
                table=current,
                inventories=[provider],
                output_dir=reports,
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=effective_path,
            )

            stats = summary["snapshot_integrity_servers"]["server_3"]
            self.assertEqual(stats["authoritative_runnable_rows"], 2)
            self.assertEqual(stats["effective_runnable_rows"], 1)
            self.assertEqual(
                stats["quarantined_authoritative_runnable_rows"], 1
            )
            self.assertEqual(stats["quarantined_already_ineligible_rows"], 0)

            summary_payload = (reports / "summary.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("private-stream-7201", summary_payload)
            self.assertNotIn("Old Private Sports Name", summary_payload)
            self.assertNotIn("Completely Different Kids Name", summary_payload)
            self.assertIn("snapshot_integrity_servers", summary_payload)
            self.assertNotIn("snapshot_authoritative_sha256", summary_payload)
            self.assertNotIn("snapshot_effective_sha256", summary_payload)
            self.assertNotIn("snapshot_quarantine_keys_sha256", summary_payload)

            effective = streaming.parse_mapping_csv(
                effective_path.read_bytes(), {"server_3"}
            )
            by_stream = {row.stream_id: row for row in effective}
            self.assertFalse(by_stream["private-stream-7201"].runtime_eligible)
            self.assertTrue(by_stream["private-stream-7202"].runtime_eligible)

    def test_reflected_provider_secret_blocks_all_sheet_and_report_writes(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        reflected = inventory(
            "server_1",
            [
                {
                    "stream_id": "2",
                    "name": "News pa%2Fssword",
                    "category_name": "General",
                    "epg_channel_id": "",
                }
            ],
        )
        config = sync.ServerConfig(
            "server_1",
            "Server 1",
            "https://panel.private.test",
            "user-reflect",
            "pa/ssword",
        )

        class NoGoogleCalls:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("secret scan must run before Google reads")

            def post(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("secret scan must run before Google writes")

        google = NoGoogleCalls()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "persistence was blocked"):
                sync.run_sync(
                    table=current,
                    inventories=[reflected],
                    output_dir=root / "reports",
                    generated_at="2026-09-15T00:00:00Z",
                    snapshot_out=root / "effective.csv",
                    write_to_sheet=True,
                    google_session=google,
                    sheet_id="a" * 30,
                    server_configs=[config],
                )
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(google.calls, 0)

    def test_sync_alerts_deduplicate_open_but_reopen_after_resolution(self) -> None:
        changed = {
            "server_id": "server_1",
            "stream_id": "7",
            "risk": "POSSIBLE_STREAM_ID_REUSE_REVIEW_REQUIRED",
            "sheet_channel_name": "BBC One",
            "provider_channel_name": "BBC Two",
            "sheet_category_name": "UK",
            "provider_category_name": "UK",
        }
        first = sync.pending_sync_alert_rows(
            [changed], [], detected_at="2026-09-15T00:00:00Z"
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "OPEN")
        self.assertEqual(
            sync.pending_sync_alert_rows(
                [changed], first, detected_at="2026-09-16T00:00:00Z"
            ),
            [],
        )
        resolved = [dict(first[0], status="RESOLVED")]
        reopened = sync.pending_sync_alert_rows(
            [changed], resolved, detected_at="2026-09-17T00:00:00Z"
        )
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]["status"], "OPEN")

    def test_open_alert_persists_quarantine_during_next_provider_outage(self) -> None:
        original = mapping_row("server_1", "7", "BBC One")
        current = table([original])
        alert = {
            "detected_at": "2026-09-15T00:00:00Z",
            "server_id": "server_1",
            "stream_id": "7",
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": "BBC One",
            "provider_channel_name": "BBC Two",
            "sheet_category_name": "UK",
            "provider_category_name": "UK",
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
            "review_notes": "",
        }
        # Day 2 has no provider inventory, so there are no fresh changed rows.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "day2.csv"
            sync.write_mapping_snapshot(
                path,
                current,
                persistent_quarantine_keys=sync.open_alert_quarantine_keys([alert]),
            )
            day2 = sync.parse_mapping_csv(path.read_bytes())
        self.assertEqual(day2.rows[0]["action"], "REVIEW")
        self.assertEqual(day2.rows[0]["enabled"], "FALSE")
        self.assertEqual(original["action"], "APPROVED")
        self.assertEqual(original["enabled"], "TRUE")

        # After the user fixes Mappings and marks the private alert resolved,
        # an aligned provider identity produces neither a current nor persistent quarantine.
        fixed = mapping_row("server_1", "7", "BBC Two")
        resolved = dict(alert, status="RESOLVED")
        aligned = inventory(
            "server_1",
            [{"stream_id": "7", "name": "BBC Two", "category_name": "General"}],
        )
        _new, changed, _missing = sync.compare_inventory(
            table([fixed]), [aligned], discovered_at="2026-09-16T00:00:00Z"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "resolved.csv"
            sync.write_mapping_snapshot(
                path,
                table([fixed]),
                changed_rows=changed,
                persistent_quarantine_keys=sync.open_alert_quarantine_keys([resolved]),
            )
            effective = sync.parse_mapping_csv(path.read_bytes())
        self.assertEqual(effective.rows[0]["action"], "APPROVED")
        self.assertEqual(effective.rows[0]["enabled"], "TRUE")

    def test_alert_append_is_verified_before_snapshot_and_survives_outage(self) -> None:
        original = mapping_row("server_1", "7", "BBC One")
        current = table([original])
        changed_inventory = inventory(
            "server_1",
            [
                {
                    "stream_id": "7",
                    "name": "BBC Two",
                    "category_name": "General",
                }
            ],
        )

        class AlertTableSession:
            def __init__(self, *, commit_alert: bool, valid_response: bool = True):
                self.commit_alert = commit_alert
                self.valid_response = valid_response
                self.mapping_values = [
                    list(streaming.SHEET_COLUMNS),
                    [original.get(column, "") for column in streaming.SHEET_COLUMNS],
                ]
                self.alert_values = [list(sync.ALERT_COLUMNS)]
                self.alert_table_end = 1
                self.posts = []

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    values = (
                        self.alert_values
                        if "Sync%20Alerts" in url
                        else self.mapping_values
                    )
                    return FakeResponse(200, {"values": values})
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {"sheetId": 42, "title": "Mappings"}
                            },
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                    "gridProperties": {
                                        "rowCount": 10_000,
                                        "columnCount": 11,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "name": "SyncAlertsTable",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": self.alert_table_end,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 11,
                                        },
                                    }
                                ],
                            },
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if not url.endswith(":batchUpdate"):
                    rows = kwargs["json"]["values"]
                    first_row = len(self.alert_values) + 1
                    last_row = first_row + len(rows) - 1
                    if self.commit_alert:
                        self.alert_values.extend([list(row) for row in rows])
                    # Simulate Google's native table growing even when the
                    # values read is deliberately stale in the negative case.
                    self.alert_table_end = max(self.alert_table_end, last_row)
                    payload = (
                        {
                            "spreadsheetId": "a" * 30,
                            "tableRange": (
                                f"'Sync Alerts'!A1:K{first_row - 1}"
                            ),
                            "updates": {
                                "spreadsheetId": "a" * 30,
                                "updatedRange": (
                                    f"'Sync Alerts'!A{first_row}:K{last_row}"
                                ),
                                "updatedRows": len(rows),
                                "updatedColumns": 11,
                                "updatedCells": len(rows) * 11,
                            },
                        }
                        if self.valid_response
                        else {"updates": {}}
                    )
                    return FakeResponse(200, payload)
                requests = kwargs["json"]["requests"]
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "replies": [{} for _request in requests],
                    },
                )

        # A syntactically valid values.append response is not enough: if the
        # authoritative re-read cannot see the OPEN row, stop before Mappings
        # or an effective snapshot can proceed.
        silent = AlertTableSession(commit_alert=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "effective.csv"
            with self.assertRaisesRegex(sync.SyncError, "did not durably store"):
                sync.run_sync(
                    table=current,
                    inventories=[changed_inventory],
                    output_dir=root / "reports",
                    generated_at="2026-09-15T00:00:00Z",
                    snapshot_out=snapshot,
                    write_to_sheet=True,
                    google_session=silent,
                    sheet_id="a" * 30,
                )
            self.assertFalse(snapshot.exists())
        self.assertEqual(len(silent.posts), 1)

        committed = AlertTableSession(commit_alert=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            day1_snapshot = root / "day1.csv"
            summary = sync.run_sync(
                table=current,
                inventories=[changed_inventory],
                output_dir=root / "day1-reports",
                generated_at="2026-09-15T00:00:00Z",
                snapshot_out=day1_snapshot,
                write_to_sheet=True,
                google_session=committed,
                sheet_id="a" * 30,
            )
            day1 = sync.parse_mapping_csv(day1_snapshot.read_bytes())
            self.assertEqual(day1.rows[0]["enabled"], "FALSE")
            self.assertEqual(summary["open_sync_alerts"], 1)

            # Day 2 provider outage has no fresh changed row. The now-durable
            # OPEN alert still disables the stale mapping in the snapshot.
            day2_snapshot = root / "day2.csv"
            sync.run_sync(
                table=current,
                inventories=[],
                output_dir=root / "day2-reports",
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=day2_snapshot,
                write_to_sheet=False,
                google_session=committed,
                sheet_id="a" * 30,
            )
            day2 = sync.parse_mapping_csv(day2_snapshot.read_bytes())
        self.assertEqual(day2.rows[0]["enabled"], "FALSE")

        # If a proxy damages the 2xx response after Google committed the row,
        # the authoritative alert re-read safely recovers without re-appending.
        ambiguous = AlertTableSession(commit_alert=True, valid_response=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambiguous_snapshot = root / "ambiguous.csv"
            summary = sync.run_sync(
                table=current,
                inventories=[changed_inventory],
                output_dir=root / "reports",
                generated_at="2026-09-15T00:00:00Z",
                snapshot_out=ambiguous_snapshot,
                write_to_sheet=True,
                google_session=ambiguous,
                sheet_id="a" * 30,
            )
            effective = sync.parse_mapping_csv(ambiguous_snapshot.read_bytes())
        self.assertTrue(summary["sync_alert_write_recovered_by_reread"])
        self.assertEqual(len(ambiguous.posts), 1)
        self.assertEqual(effective.rows[0]["enabled"], "FALSE")

    def test_classic_alert_append_is_authoritatively_verified(self) -> None:
        original = mapping_row("server_1", "7", "BBC One")
        current = table([original])
        changed_inventory = inventory(
            "server_1", [{"stream_id": "7", "name": "BBC Two"}]
        )

        class ClassicAlertSession:
            def __init__(self):
                self.mapping_values = [
                    list(streaming.SHEET_COLUMNS),
                    [original.get(column, "") for column in streaming.SHEET_COLUMNS],
                ]
                self.alert_values = [list(sync.ALERT_COLUMNS)]

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {
                            "values": (
                                self.alert_values
                                if "Sync%20Alerts" in url
                                else self.mapping_values
                            )
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {"properties": {"sheetId": 42, "title": "Mappings"}},
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                }
                            },
                        ]
                    },
                )

            def post(self, url, **kwargs):
                if url.endswith(":batchUpdate"):
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                values = kwargs["json"]["values"]
                self.alert_values.extend(values)
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Sync Alerts'!A1:K1",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Sync Alerts'!A2:K2",
                            "updatedRows": 1,
                            "updatedColumns": 11,
                            "updatedCells": 11,
                        },
                    },
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "effective.csv"
            summary = sync.run_sync(
                table=current,
                inventories=[changed_inventory],
                output_dir=root / "reports",
                generated_at="2026-09-15T00:00:00Z",
                snapshot_out=snapshot,
                write_to_sheet=True,
                google_session=ClassicAlertSession(),
                sheet_id="a" * 30,
            )
            effective = sync.parse_mapping_csv(snapshot.read_bytes())
        self.assertEqual(summary["sync_alerts_appended"], 1)
        self.assertEqual(summary["open_sync_alerts"], 1)
        self.assertEqual(effective.rows[0]["enabled"], "FALSE")

    def test_quarantined_snapshot_is_absent_from_metadata_and_schedule_output(self) -> None:
        safe = mapping_row("server_1", "1", "Safe News", epg_id="Safe.test")
        suspect = mapping_row("server_1", "7", "BBC One", epg_id="BBC.One.test")
        discovered_inventory = inventory(
            "server_1",
            [{"stream_id": "9", "name": "New Unreviewed", "category_name": "News"}],
        )
        discovered = sync.new_mapping_row(
            discovered_inventory,
            discovered_inventory.channels[0],
            discovered_at="2026-09-15T00:00:00Z",
        )
        current = table([safe, suspect, discovered])
        alert = {
            "detected_at": "2026-09-15T00:00:00Z",
            "server_id": "server_1",
            "stream_id": "7",
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": "BBC One",
            "provider_channel_name": "BBC Two",
            "sheet_category_name": "UK",
            "provider_category_name": "UK",
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
            "review_notes": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "effective.csv"
            sync.write_mapping_snapshot(
                snapshot,
                current,
                persistent_quarantine_keys=sync.open_alert_quarantine_keys([alert]),
            )
            parsed = streaming.parse_mapping_csv(
                snapshot.read_bytes(), {"server_1"}
            )
            by_stream = {row.stream_id: row for row in parsed}
            self.assertTrue(by_stream["1"].runtime_eligible)
            self.assertFalse(by_stream["7"].enabled)
            self.assertFalse(by_stream["7"].runtime_eligible)
            self.assertFalse(by_stream["9"].enabled)
            self.assertFalse(by_stream["9"].runtime_eligible)

            connection = streaming.create_database(root / "stage.db")
            try:
                metadata_path = root / "metadata.json.gz"
                streaming.write_metadata_json(
                    destination=metadata_path,
                    rows=parsed,
                    connection=connection,
                    generated_at=1_789_430_400,
                    mapping_sha256="fixture",
                    icon_overrides=[],
                )
                schedule_path = root / "epg.json.gz"
                streaming.write_app_epg(
                    destination=schedule_path,
                    rows=parsed,
                    connection=connection,
                    generated_at=1_789_430_400,
                    window_start=1_789_430_400,
                    window_end=1_789_516_800,
                    icon_overrides=[],
                )
            finally:
                connection.close()

            with gzip.open(metadata_path, "rt", encoding="utf-8") as handle:
                metadata = json.load(handle)
            with gzip.open(schedule_path, "rt", encoding="utf-8") as handle:
                schedule = json.load(handle)
        self.assertIn("1", metadata["streamMetadata"])
        self.assertNotIn("7", metadata["streamMetadata"])
        self.assertNotIn("9", metadata["streamMetadata"])
        self.assertIn("1", schedule["streamToEpg"])
        self.assertNotIn("7", schedule["streamToEpg"])
        self.assertNotIn("9", schedule["streamToEpg"])

    def test_read_only_private_sheet_run_still_applies_open_alert_quarantine(self) -> None:
        original = mapping_row("server_1", "7", "BBC One")
        current = table([original])
        alert = {
            "detected_at": "2026-09-15T00:00:00Z",
            "server_id": "server_1",
            "stream_id": "7",
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": "BBC One",
            "provider_channel_name": "BBC Two",
            "sheet_category_name": "UK",
            "provider_category_name": "UK",
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
            "review_notes": "",
        }

        class AlertReadSession:
            def get(self, url, **kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {
                            "values": [
                                list(sync.ALERT_COLUMNS),
                                [alert[column] for column in sync.ALERT_COLUMNS],
                            ]
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {"properties": {"sheetId": 43, "title": "Sync Alerts"}}
                        ]
                    },
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "effective.csv"
            summary = sync.run_sync(
                table=current,
                inventories=[],
                output_dir=Path(temporary) / "reports",
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=path,
                write_to_sheet=False,
                google_session=AlertReadSession(),
                sheet_id="a" * 30,
            )
            effective = sync.parse_mapping_csv(path.read_bytes())
        self.assertEqual(effective.rows[0]["action"], "REVIEW")
        self.assertEqual(summary["open_sync_alerts"], 1)

    def test_csv_reports_escape_formula_like_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.csv"
            sync.write_csv_report(path, [{"name": "=SUM(A1:A2)"}], ["name"])
            rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
        self.assertEqual(rows[1][0], "'=SUM(A1:A2)")

    def test_sheet_append_uses_raw_values_and_copies_only_format_validation(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "=Formula", action="REVIEW", epg_id="")

        class FakeSession:
            def __init__(self):
                self.posts: list[tuple[str, dict[str, object]]] = []
                self.get_kwargs = None

            def get(self, url, **kwargs):
                self.get_kwargs = kwargs
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "basicFilter": {
                                    "range": {
                                        "sheetId": 42,
                                        "startRowIndex": 0,
                                        "endRowIndex": 2,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 33,
                                    },
                                    "sortSpecs": [
                                        {"dimensionIndex": 0, "sortOrder": "ASCENDING"}
                                    ],
                                    "filterSpecs": {
                                        "6": {"filterCriteria": {"hiddenValues": ["FALSE"]}}
                                    },
                                },
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if url.endswith(":batchUpdate"):
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:AG2",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A3:AG3",
                            "updatedRows": 1,
                            "updatedColumns": 33,
                            "updatedCells": 33,
                        }
                    },
                )

        session = FakeSession()
        appended = sync.append_google_sheet_rows(
            session, "a" * 30, "Mappings", current, [new]
        )
        self.assertEqual(appended, 1)
        self.assertIn(
            "tables(tableId,name,range,rowsProperties(footerColorStyle))",
            session.get_kwargs["params"]["fields"],
        )
        self.assertIn("basicFilter", session.get_kwargs["params"]["fields"])
        append_kwargs = session.posts[0][1]
        self.assertEqual(append_kwargs["params"]["valueInputOption"], "RAW")
        self.assertIn("!A:AG:append", session.posts[0][0])
        name_index = list(streaming.SHEET_COLUMNS).index("channel_name")
        self.assertEqual(append_kwargs["json"]["values"][0][name_index], "=Formula")
        all_requests = session.posts[1][1]["json"]["requests"]
        copy_requests = [request for request in all_requests if "copyPaste" in request]
        self.assertEqual(
            {request["copyPaste"]["pasteType"] for request in copy_requests},
            {"PASTE_FORMAT", "PASTE_DATA_VALIDATION"},
        )
        self.assertTrue(
            all("values" not in request["copyPaste"] for request in copy_requests)
        )
        filter_request = next(
            request["setBasicFilter"]
            for request in all_requests
            if "setBasicFilter" in request
        )
        self.assertEqual(filter_request["filter"]["range"]["endRowIndex"], 3)
        self.assertEqual(
            filter_request["filter"]["sortSpecs"],
            [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
        )
        self.assertEqual(
            filter_request["filter"]["filterSpecs"],
            {"6": {"filterCriteria": {"hiddenValues": ["FALSE"]}}},
        )

    def test_modern_mapping_table_expands_then_writes_literal_strings(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "=Formula", action="REVIEW", epg_id="")

        class ModernSession:
            def __init__(self):
                self.posts = []
                self.table_end = 2

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["server_id"], ["server_1"], ["server_1"]]},
                    )
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "name": "MappingsTable",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": self.table_end,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 33,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if not url.endswith(":batchUpdate"):
                    rows = kwargs["json"]["values"]
                    self.table_end += len(rows)
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "tableRange": "'Mappings'!A1:AG2",
                            "updates": {
                                "spreadsheetId": "a" * 30,
                                "updatedRange": "'Mappings'!A3:AG3",
                                "updatedRows": 1,
                                "updatedColumns": 33,
                                "updatedCells": 33,
                            },
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "replies": [{} for _request in kwargs["json"]["requests"]],
                    },
                )

        session = ModernSession()
        self.assertEqual(
            sync.append_google_sheet_rows(
                session, "a" * 30, "Mappings", current, [new]
            ),
            1,
        )
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertIn("!A:AG:append", url)
        self.assertEqual(kwargs["params"]["valueInputOption"], "RAW")
        self.assertEqual(kwargs["params"]["insertDataOption"], "INSERT_ROWS")
        name_index = list(streaming.SHEET_COLUMNS).index("channel_name")
        self.assertEqual(kwargs["json"]["values"][0][name_index], "=Formula")

    def test_modern_alert_table_uses_its_own_table_id_and_eleven_cells(self) -> None:
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "detected_at": "2026-09-15T00:00:00Z",
                "server_id": "server_1",
                "stream_id": "7",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "sheet_channel_name": "+Literal",
                "status": "OPEN",
            }
        )

        class ModernAlertSession:
            def __init__(self):
                self.posts = []
                self.metadata_reads = 0
                self.table_end = 1
                self.grid_rows = 1

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["detected_at"], ["2026-09-15T00:00:00Z"]]},
                    )
                self.metadata_reads += 1
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                    "gridProperties": {
                                        "rowCount": self.grid_rows,
                                        "columnCount": 11,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "name": "SyncAlertsTable",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": self.table_end,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 11,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if not url.endswith(":batchUpdate"):
                    rows = kwargs["json"]["values"]
                    self.grid_rows += len(rows)
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "tableRange": "'Sync Alerts'!A1:K1",
                            "updates": {
                                "spreadsheetId": "a" * 30,
                                "updatedRange": "'Sync Alerts'!A2:K2",
                                "updatedRows": 1,
                                "updatedColumns": 11,
                                "updatedCells": 11,
                            },
                        },
                    )
                update = kwargs["json"]["requests"][0]["updateTable"]
                self.table_end = update["table"]["range"]["endRowIndex"]
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "replies": [{} for _request in kwargs["json"]["requests"]],
                    },
                )

        session = ModernAlertSession()
        self.assertEqual(
            sync.append_sync_alert_rows(
                session, "a" * 30, "Sync Alerts", [], [alert]
            ),
            1,
        )
        self.assertEqual(len(session.posts), 2)
        append_url, append_kwargs = session.posts[0]
        self.assertIn("!A:K:append", append_url)
        self.assertEqual(append_kwargs["params"]["valueInputOption"], "RAW")
        self.assertEqual(append_kwargs["params"]["insertDataOption"], "INSERT_ROWS")
        self.assertEqual(len(append_kwargs["json"]["values"][0]), 11)
        self.assertEqual(append_kwargs["json"]["values"][0][4], "+Literal")
        requests = session.posts[1][1]["json"]["requests"]
        self.assertEqual(
            requests[0]["updateTable"]["table"]["tableId"],
            "table-alerts-v1",
        )
        self.assertEqual(
            requests[0]["updateTable"]["table"]["range"]["endRowIndex"],
            2,
        )

    def test_modern_table_chunking_uses_non_overlapping_atomic_appends(self) -> None:
        class ChunkSession:
            def __init__(self):
                self.posts = []
                self.append_calls = 0

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["detected_at"], ["one"], ["two"], ["three"]]},
                    )
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                    "gridProperties": {
                                        "rowCount": 4,
                                        "columnCount": 2,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": 4,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 2,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                self.append_calls += 1
                first, last = ((2, 3) if self.append_calls == 1 else (4, 4))
                row_count = last - first + 1
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": f"'Sync Alerts'!A1:B{first - 1}",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": f"'Sync Alerts'!A{first}:B{last}",
                            "updatedRows": row_count,
                            "updatedColumns": 2,
                            "updatedCells": row_count * 2,
                        },
                    },
                )

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=43,
            title="Sync Alerts",
            table_id="table-alerts-v1",
            table_end_row=1,
            grid_row_count=3,
            grid_column_count=2,
            table_has_footer=False,
            basic_filter=None,
        )
        session = ChunkSession()
        appended = sync.append_modern_table_rows(
            session,
            "a" * 30,
            "Sync Alerts",
            layout,
            [["one", "1"], ["two", "2"], ["three", "3"]],
            existing_data_rows=0,
            chunk_size=2,
            failure_label="Sync Alerts",
        )
        self.assertEqual(appended, 3)
        self.assertEqual(len(session.posts), 2)

        self.assertTrue(all("/values/" in url for url, _kwargs in session.posts))
        self.assertEqual(len(session.posts[0][1]["json"]["values"]), 2)
        self.assertEqual(len(session.posts[1][1]["json"]["values"]), 1)

    def test_raw_append_accepts_a_concurrent_row_before_our_rows(self) -> None:
        class ConcurrentSession:
            def post(self, _url, **kwargs):
                self.params = kwargs["params"]
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:B3",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            # Row 3 was expected. Another editor atomically
                            # claimed it, so Google placed this write at row 4.
                            "updatedRange": "'Mappings'!A4:B4",
                            "updatedRows": 1,
                            "updatedColumns": 2,
                            "updatedCells": 2,
                        },
                    },
                )

        session = ConcurrentSession()
        appended, ranges = sync.append_raw_sheet_rows(
            session,
            "a" * 30,
            "Mappings",
            [["server_1", "2"]],
            existing_data_rows=1,
            column_count=2,
            chunk_size=500,
            failure_label="Mappings",
        )
        self.assertEqual(appended, 1)
        self.assertEqual(ranges, [(4, 4)])
        self.assertEqual(session.params["valueInputOption"], "RAW")
        self.assertEqual(session.params["insertDataOption"], "INSERT_ROWS")

    def test_raw_append_wrong_typed_updates_is_reconcilable_write_error(self) -> None:
        class NullUpdatesSession:
            def post(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:B1",
                        "updates": None,
                    },
                )

        with self.assertRaises(sync.SheetWriteError) as caught:
            sync.append_raw_sheet_rows(
                NullUpdatesSession(),
                "a" * 30,
                "Mappings",
                [["server_1", "2"]],
                existing_data_rows=0,
                column_count=2,
                chunk_size=500,
                failure_label="Mappings",
            )
        self.assertEqual(caught.exception.appended_count, 1)

    def test_raw_append_rejects_wrong_logical_table_range(self) -> None:
        class WrongLogicalTableSession:
            def post(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A10:B10",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A11:B11",
                            "updatedRows": 1,
                            "updatedColumns": 2,
                            "updatedCells": 2,
                        },
                    },
                )

        with self.assertRaisesRegex(sync.SheetWriteError, "unexpected or partial"):
            sync.append_raw_sheet_rows(
                WrongLogicalTableSession(),
                "a" * 30,
                "Mappings",
                [["server_1", "2"]],
                existing_data_rows=0,
                column_count=2,
                chunk_size=500,
                failure_label="Mappings",
            )

    def test_modern_range_finalizer_repairs_a_concurrent_high_water_mark(self) -> None:
        class RacingSession:
            def __init__(self):
                self.table_end = 1
                self.used_end = 2
                self.targets = []
                self.append_done = False

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["server_id"]] + [["server_1"]] * (self.used_end - 1)},
                    )
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 2,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": self.table_end,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 2,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                if not url.endswith(":batchUpdate"):
                    self.append_done = True
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "tableRange": "'Mappings'!A1:B1",
                            "updates": {
                                "spreadsheetId": "a" * 30,
                                "updatedRange": "'Mappings'!A2:B2",
                                "updatedRows": 1,
                                "updatedColumns": 2,
                                "updatedCells": 2,
                            },
                        },
                    )
                target = kwargs["json"]["requests"][0]["updateTable"]["table"][
                    "range"
                ]["endRowIndex"]
                self.targets.append(target)
                self.table_end = target
                if len(self.targets) == 1:
                    # A second writer committed a third row immediately after
                    # this stale range request; the next read must repair it.
                    self.used_end = 3
                return FakeResponse(
                    200,
                    {"spreadsheetId": "a" * 30, "replies": [{}]},
                )

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=42,
            title="Mappings",
            table_id="table-mappings-v1",
            table_end_row=1,
            grid_row_count=100,
            grid_column_count=2,
            table_has_footer=False,
            basic_filter=None,
        )
        session = RacingSession()
        self.assertEqual(
            sync.append_modern_table_rows(
                session,
                "a" * 30,
                "Mappings",
                layout,
                [["server_1", "2"]],
                existing_data_rows=0,
                chunk_size=500,
                failure_label="Mappings",
            ),
            1,
        )
        self.assertEqual(session.targets, [2, 3])

    def test_modern_range_finalizer_verifies_a_successful_third_update(self) -> None:
        class DelayedThirdUpdateSession:
            def __init__(self):
                self.update_posts = 0
                self.metadata_reads = 0

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["server_id"], ["server_1"]]},
                    )
                self.metadata_reads += 1
                visible_end_row = 2 if self.update_posts == 3 else 1
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 2,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": visible_end_row,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 2,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **_kwargs):
                self.assert_batch_update(url)
                self.update_posts += 1
                if self.update_posts > 3:
                    raise AssertionError("a fourth table-range write is forbidden")
                return FakeResponse(
                    200,
                    {"spreadsheetId": "a" * 30, "replies": [{}]},
                )

            @staticmethod
            def assert_batch_update(url):
                if not url.endswith(":batchUpdate"):
                    raise AssertionError("only table-range writes are expected")

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=42,
            title="Mappings",
            table_id="table-mappings-v1",
            table_end_row=1,
            grid_row_count=100,
            grid_column_count=2,
            table_has_footer=False,
            basic_filter=None,
        )
        session = DelayedThirdUpdateSession()
        sync.finalize_modern_table_range(
            session,
            "a" * 30,
            "Mappings",
            layout,
            required_end_row=2,
            column_count=2,
            maximum_data_rows=100,
            failure_label="Mappings",
            appended_count=1,
        )
        self.assertEqual(session.update_posts, 3)
        self.assertEqual(session.metadata_reads, 4)

    def test_modern_range_finalizer_fails_closed_after_three_unseen_updates(self) -> None:
        class NeverVisibleSession:
            def __init__(self):
                self.update_posts = 0
                self.metadata_reads = 0

            def get(self, url, **_kwargs):
                if "/values/" in url:
                    return FakeResponse(
                        200,
                        {"values": [["server_id"], ["server_1"]]},
                    )
                self.metadata_reads += 1
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 2,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": 1,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 2,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, url, **_kwargs):
                if not url.endswith(":batchUpdate"):
                    raise AssertionError("only table-range writes are expected")
                self.update_posts += 1
                if self.update_posts > 3:
                    raise AssertionError("a fourth table-range write is forbidden")
                return FakeResponse(
                    200,
                    {"spreadsheetId": "a" * 30, "replies": [{}]},
                )

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=42,
            title="Mappings",
            table_id="table-mappings-v1",
            table_end_row=1,
            grid_row_count=100,
            grid_column_count=2,
            table_has_footer=False,
            basic_filter=None,
        )
        session = NeverVisibleSession()
        with self.assertRaises(sync.SheetWriteError) as caught:
            sync.finalize_modern_table_range(
                session,
                "a" * 30,
                "Mappings",
                layout,
                required_end_row=2,
                column_count=2,
                maximum_data_rows=100,
                failure_label="Mappings",
                appended_count=1,
            )
        self.assertEqual(caught.exception.appended_count, 1)
        self.assertEqual(session.update_posts, 3)
        self.assertEqual(session.metadata_reads, 4)

    def test_modern_footer_is_rejected_before_any_write(self) -> None:
        class NoCallsSession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("footer must fail before a read")

            def post(self, *_args, **_kwargs):
                raise AssertionError("footer must fail before a write")

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=42,
            title="Mappings",
            table_id="table-mappings-v1",
            table_end_row=2,
            grid_row_count=100,
            grid_column_count=2,
            table_has_footer=True,
            basic_filter=None,
        )
        with self.assertRaisesRegex(sync.SyncError, "has a footer"):
            sync.append_modern_table_rows(
                NoCallsSession(),
                "a" * 30,
                "Mappings",
                layout,
                [["server_1", "2"]],
                existing_data_rows=1,
                chunk_size=500,
                failure_label="Mappings",
            )

    def test_post_append_metadata_failure_keeps_committed_row_accounting(self) -> None:
        class MetadataFailureSession:
            def post(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:B1",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A2:B2",
                            "updatedRows": 1,
                            "updatedColumns": 2,
                            "updatedCells": 2,
                        },
                    },
                )

            def get(self, _url, **_kwargs):
                return FakeResponse(503, {"error": {"status": "UNAVAILABLE"}})

        layout = sync.GoogleSheetLayout(
            numeric_sheet_id=42,
            title="Mappings",
            table_id="table-mappings-v1",
            table_end_row=1,
            grid_row_count=100,
            grid_column_count=2,
            table_has_footer=False,
            basic_filter=None,
        )
        with mock.patch.object(sync.time, "sleep"):
            with self.assertRaises(sync.SheetWriteError) as caught:
                sync.append_modern_table_rows(
                    MetadataFailureSession(),
                    "a" * 30,
                    "Mappings",
                    layout,
                    [["server_1", "2"]],
                    existing_data_rows=0,
                    chunk_size=500,
                    failure_label="Mappings",
                )
        self.assertEqual(caught.exception.appended_count, 1)

    def test_malformed_sheet_metadata_is_a_controlled_error(self) -> None:
        for payload in ({"sheets": None}, {"sheets": [{"properties": []}]}):
            with self.subTest(payload=payload):
                class MalformedMetadataSession:
                    def get(self, _url, **_kwargs):
                        return FakeResponse(200, payload)

                with self.assertRaises(sync.SyncError):
                    sync.google_sheet_layout(
                        MalformedMetadataSession(),
                        "a" * 30,
                        "Mappings",
                        column_count=33,
                    )

    def test_google_write_error_does_not_reflect_untrusted_response_text(self) -> None:
        response = json.dumps(
            {
                "error": {
                    "status": "PRIVATE_VALUE_MUST_NOT_ESCAPE",
                    "message": "private-value-must-not-escape",
                }
            }
        ).encode("utf-8")
        message = sync.google_write_rejection_message(
            "Sync Alerts",
            status=403,
            response_content=response,
            operation="table write",
        )
        self.assertIn("HTTP 403", message)
        self.assertIn("lacks edit access", message)
        self.assertNotIn("PRIVATE_VALUE_MUST_NOT_ESCAPE", message)
        self.assertNotIn("private-value-must-not-escape", message)

    def test_incompatible_modern_table_fails_before_any_append(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class WrongTableSession:
            def __init__(self):
                self.posts = 0

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "wrong",
                                        "range": {
                                            "sheetId": 42,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 5,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, *_args, **_kwargs):
                self.posts += 1
                raise AssertionError("No append may occur")

        session = WrongTableSession()
        with self.assertRaisesRegex(sync.SyncError, "does not uniquely cover"):
            sync.append_google_sheet_rows(
                session, "a" * 30, "Mappings", current, [new]
            )
        self.assertEqual(session.posts, 0)

    def test_modern_table_may_lag_authoritative_rows_for_safe_repair(self) -> None:
        class LaggingTableSession:
            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": 1,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 33,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

        layout = sync.google_sheet_layout(
            LaggingTableSession(),
            "a" * 30,
            "Mappings",
            column_count=33,
            expected_used_rows=2,
        )
        self.assertEqual(layout.table_end_row, 1)

    def test_modern_table_may_extend_beyond_authoritative_used_rows(self) -> None:
        class TallerTableSession:
            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": 3,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 33,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

        layout = sync.google_sheet_layout(
            TallerTableSession(),
            "a" * 30,
            "Mappings",
            column_count=33,
            expected_used_rows=2,
        )
        self.assertEqual(layout.table_end_row, 3)

    def test_classic_append_rejects_unexpected_row_two_for_nonempty_sheet(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class RowTwoSession:
            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {"sheets": [{"properties": {"sheetId": 42, "title": "Mappings"}}]},
                )

            def post(self, url, **_kwargs):
                if url.endswith(":batchUpdate"):
                    raise AssertionError("Unsafe append range must stop before formatting")
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:AG1",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A2:AG2",
                            "updatedRows": 1,
                            "updatedColumns": 33,
                            "updatedCells": 33,
                        }
                    },
                )

        with self.assertRaisesRegex(sync.SheetWriteError, "unexpected or partial") as caught:
            sync.append_google_sheet_rows(
                RowTwoSession(), "a" * 30, "Mappings", current, [new]
            )
        self.assertEqual(caught.exception.appended_count, 1)

    def test_modern_table_http_failure_is_reported_before_counting_chunk(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class FailingModernSession:
            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "startRowIndex": 0,
                                            "endRowIndex": 2,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 33,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, _url, **_kwargs):
                return FakeResponse(
                    500,
                    {
                        "error": {
                            "status": "INTERNAL",
                            "message": "sensitive response details",
                        }
                    },
                )

        with self.assertRaises(sync.SheetWriteError) as caught:
            sync.append_google_sheet_rows(
                FailingModernSession(), "a" * 30, "Mappings", current, [new]
            )
        self.assertEqual(caught.exception.appended_count, 0)
        self.assertIn("HTTP 500 INTERNAL", str(caught.exception))
        self.assertIn("temporary server failure", str(caught.exception))
        self.assertNotIn("sensitive response details", str(caught.exception))

    def test_modern_table_malformed_success_is_possibly_committed_and_not_retried(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class MalformedSuccessSession:
            def __init__(self):
                self.posts = 0

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 42,
                                    "title": "Mappings",
                                    "gridProperties": {
                                        "rowCount": 100,
                                        "columnCount": 33,
                                    },
                                },
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "range": {
                                            "sheetId": 42,
                                            "endRowIndex": 2,
                                            "endColumnIndex": 33,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, _url, **_kwargs):
                self.posts += 1
                return FakeResponse(200, {"replies": [{}]})

        session = MalformedSuccessSession()
        with self.assertRaisesRegex(
            sync.SheetWriteError, "unexpected or partial"
        ) as caught:
            sync.append_google_sheet_rows(
                session, "a" * 30, "Mappings", current, [new]
            )
        self.assertEqual(session.posts, 1)
        self.assertEqual(caught.exception.appended_count, 1)

    def test_empty_append_is_a_true_noop_for_both_tabs(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])

        class NoCallsSession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("empty append must not read metadata")

            def post(self, *_args, **_kwargs):
                raise AssertionError("empty append must not write")

        session = NoCallsSession()
        self.assertEqual(
            sync.append_google_sheet_rows(
                session, "a" * 30, "Mappings", current, []
            ),
            0,
        )
        self.assertEqual(
            sync.append_sync_alert_rows(
                session, "a" * 30, "Sync Alerts", [], []
            ),
            0,
        )

    def test_classic_filter_never_shrinks_when_already_preexpanded(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class PreexpandedFilterSession:
            def __init__(self):
                self.posts = []

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {"sheetId": 42, "title": "Mappings"},
                                "basicFilter": {
                                    "range": {
                                        "sheetId": 42,
                                        "startRowIndex": 0,
                                        "endRowIndex": 150001,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 33,
                                    },
                                    "sortSpecs": [
                                        {"dimensionIndex": 0, "sortOrder": "ASCENDING"}
                                    ],
                                },
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if url.endswith(":batchUpdate"):
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:AG2",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A3:AG3",
                            "updatedRows": 1,
                            "updatedColumns": 33,
                            "updatedCells": 33,
                        },
                    },
                )

        session = PreexpandedFilterSession()
        self.assertEqual(
            sync.append_google_sheet_rows(
                session, "a" * 30, "Mappings", current, [new]
            ),
            1,
        )
        finalize_requests = session.posts[1][1]["json"]["requests"]
        self.assertFalse(any("setBasicFilter" in item for item in finalize_requests))
        self.assertEqual(
            {item["copyPaste"]["pasteType"] for item in finalize_requests},
            {"PASTE_FORMAT", "PASTE_DATA_VALIDATION"},
        )

    def test_classic_filter_finalize_retries_without_reappending(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        class TransientFinalizeSession:
            def __init__(self):
                self.append_posts = 0
                self.batch_posts = 0

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {"sheets": [{"properties": {"sheetId": 42, "title": "Mappings"}}]},
                )

            def post(self, url, **kwargs):
                if url.endswith(":batchUpdate"):
                    self.batch_posts += 1
                    if self.batch_posts == 1:
                        return FakeResponse(503, {"error": {}})
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                self.append_posts += 1
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Mappings'!A1:AG2",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Mappings'!A3:AG3",
                            "updatedRows": 1,
                            "updatedColumns": 33,
                            "updatedCells": 33,
                        },
                    },
                )

        session = TransientFinalizeSession()
        with mock.patch.object(sync.time, "sleep") as sleep:
            self.assertEqual(
                sync.append_google_sheet_rows(
                    session, "a" * 30, "Mappings", current, [new]
                ),
                1,
            )
        self.assertEqual(session.append_posts, 1)
        self.assertEqual(session.batch_posts, 2)
        sleep.assert_called_once_with(1)

    def test_sync_alert_append_is_raw_and_exactly_eleven_columns(self) -> None:
        alert = {
            "detected_at": "2026-09-15T00:00:00Z",
            "server_id": "server_1",
            "stream_id": "7",
            "alert_type": "POSSIBLE_STREAM_ID_REUSE",
            "sheet_channel_name": "=Old",
            "provider_channel_name": "+New",
            "sheet_category_name": "Old",
            "provider_category_name": "New",
            "action_taken": "QUARANTINED_IN_EFFECTIVE_SNAPSHOT",
            "status": "OPEN",
            "review_notes": "",
        }

        class FakeSession:
            def __init__(self):
                self.posts = []

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                }
                            }
                        ]
                    },
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if url.endswith(":batchUpdate"):
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": "'Sync Alerts'!A1:K1",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": "'Sync Alerts'!A2:K2",
                            "updatedRows": 1,
                            "updatedColumns": 11,
                            "updatedCells": 11,
                        }
                    },
                )

        session = FakeSession()
        appended = sync.append_sync_alert_rows(
            session, "a" * 30, "Sync Alerts", [], [alert]
        )
        self.assertEqual(appended, 1)
        append_url, append_kwargs = session.posts[0]
        self.assertIn("!A:K:append", append_url)
        self.assertEqual(append_kwargs["params"]["valueInputOption"], "RAW")
        self.assertEqual(len(append_kwargs["json"]["values"][0]), 11)
        self.assertEqual(append_kwargs["json"]["values"][0][4], "=Old")
        requests = session.posts[1][1]["json"]["requests"]
        copy_requests = [request for request in requests if "copyPaste" in request]
        self.assertEqual(
            {request["copyPaste"]["pasteType"] for request in copy_requests},
            {"PASTE_FORMAT", "PASTE_DATA_VALIDATION"},
        )
        self.assertTrue(
            all(
                request["copyPaste"]["source"]["endColumnIndex"] == 11
                and request["copyPaste"]["destination"]["endColumnIndex"] == 11
                for request in copy_requests
            )
        )
        filter_request = next(
            request["setBasicFilter"]
            for request in requests
            if "setBasicFilter" in request
        )
        self.assertEqual(
            filter_request["filter"]["range"],
            {
                "sheetId": 43,
                "startRowIndex": 0,
                "endRowIndex": 2,
                "startColumnIndex": 0,
                "endColumnIndex": 11,
            },
        )

    def test_missing_sync_alerts_tab_fails_before_any_sheet_append(self) -> None:
        current_row = mapping_row("server_1", "1", "One")
        current = table([current_row])
        inv = inventory(
            "server_1",
            [{"stream_id": "1", "name": "One", "category_name": "General"}],
        )

        class MissingAlertsSession:
            def __init__(self):
                self.posts = []

            def get(self, url, **kwargs):
                if "/values/" in url:
                    values = [
                        list(streaming.SHEET_COLUMNS),
                        [
                            current_row.get(column, "")
                            for column in streaming.SHEET_COLUMNS
                        ],
                    ]
                    return FakeResponse(200, {"values": values})
                return FakeResponse(
                    200,
                    {"sheets": [{"properties": {"sheetId": 42, "title": "Mappings"}}]},
                )

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse(200, {})

        session = MissingAlertsSession()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(sync.SyncError, "Sync Alerts.*not found"):
                sync.run_sync(
                    table=current,
                    inventories=[inv],
                    output_dir=Path(temporary) / "reports",
                    generated_at="2026-09-15T00:00:00Z",
                    snapshot_out=Path(temporary) / "effective.csv",
                    write_to_sheet=True,
                    google_session=session,
                    sheet_id="a" * 30,
                )
        self.assertEqual(session.posts, [])

    def test_private_sheet_read_is_bounded_and_unformatted(self) -> None:
        values = [list(streaming.SHEET_COLUMNS)]

        class FakeSession:
            def __init__(self):
                self.kwargs = None

            def get(self, _url, **kwargs):
                self.kwargs = kwargs
                return FakeResponse(200, {"values": values})

        session = FakeSession()
        self.assertEqual(sync.google_sheet_values(session, "a" * 30, "Mappings"), values)
        self.assertTrue(session.kwargs["stream"])
        self.assertEqual(
            session.kwargs["params"]["valueRenderOption"], "UNFORMATTED_VALUE"
        )

    def test_projected_sheet_limits_are_checked_before_append(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        additional = mapping_row("server_1", "2", "Two")
        with mock.patch.object(sync, "MAX_GOOGLE_MAPPING_ROWS", 1):
            with self.assertRaisesRegex(sync.SyncError, "Google mapping limit"):
                sync.validate_projected_sheet_size(current, [additional])

        class NoWriteSession:
            def __init__(self):
                self.calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Google must not be called")

            def post(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Google must not be called")

        session = NoWriteSession()
        with mock.patch.object(sync, "MAX_GOOGLE_MAPPING_ROWS", 1):
            with self.assertRaisesRegex(sync.SyncError, "Google mapping limit"):
                sync.append_google_sheet_rows(
                    session, "a" * 30, "Mappings", current, [additional]
                )
        self.assertEqual(session.calls, 0)

    def test_alert_row_cap_fails_before_google_post(self) -> None:
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "server_id": "server_1",
                "stream_id": "7",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )

        class NoPostSession:
            def __init__(self):
                self.posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                raise AssertionError("post must not be called")

        session = NoPostSession()
        with mock.patch.object(sync, "MAX_SYNC_ALERT_ROWS", 0):
            with self.assertRaisesRegex(sync.SyncError, "Sync Alerts limit"):
                sync.append_sync_alert_rows(
                    session, "a" * 30, "Sync Alerts", [], [alert]
                )
        self.assertEqual(session.posts, 0)

    def test_alert_byte_cap_fails_before_google_post(self) -> None:
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "server_id": "server_1",
                "stream_id": "7",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "sheet_channel_name": "X" * 500,
                "status": "OPEN",
            }
        )

        class NoPostSession:
            def __init__(self):
                self.posts = 0

            def post(self, *args, **kwargs):
                self.posts += 1
                raise AssertionError("post must not be called")

        session = NoPostSession()
        with mock.patch.object(sync, "MAX_SYNC_ALERT_BYTES", 64):
            with self.assertRaisesRegex(sync.SyncError, "8 MiB"):
                sync.append_sync_alert_rows(
                    session, "a" * 30, "Sync Alerts", [], [alert]
                )
        self.assertEqual(session.posts, 0)

    def test_dry_run_writes_all_reports_and_effective_snapshot(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        inv = inventory(
            "server_1",
            [
                {"stream_id": "1", "name": "One", "category_id": "cat", "category_name": "General"},
                {"stream_id": "2", "name": "Two", "category_id": "cat", "category_name": "General"},
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = sync.run_sync(
                table=current,
                inventories=[inv],
                output_dir=root / "reports",
                generated_at="2026-09-15T00:00:00Z",
                snapshot_out=root / "effective.csv",
            )
            expected = {
                "inventory.csv",
                "new_channels.csv",
                "changed_channels.csv",
                "possible_id_reuse.csv",
                "missing_channels.csv",
                "mapping_before_append.csv",
                "summary.json",
            }
            self.assertTrue(expected.issubset({path.name for path in (root / "reports").iterdir()}))
            self.assertTrue((root / "effective.csv").is_file())
        self.assertEqual(summary["inventory_rows"], 2)
        self.assertEqual(summary["new_rows"], 1)
        self.assertEqual(summary["appended_rows"], 0)

    def test_write_mode_rereads_sheet_and_snapshots_authoritative_rows(self) -> None:
        first = mapping_row("server_1", "1", "One")
        current = table([first])
        inv = inventory(
            "server_1",
            [
                {"stream_id": "1", "name": "One", "category_id": "cat", "category_name": "General"},
                {"stream_id": "2", "name": "Two", "category_id": "cat", "category_name": "General"},
            ],
        )

        class StatefulSheetSession:
            def __init__(self):
                self.values = [
                    list(streaming.SHEET_COLUMNS),
                    [first.get(column, "") for column in streaming.SHEET_COLUMNS],
                ]
                self.alert_values = [list(sync.ALERT_COLUMNS)]

            def get(self, url, **kwargs):
                if "/values/" in url:
                    if "Sync%20Alerts" in url:
                        return FakeResponse(200, {"values": self.alert_values})
                    return FakeResponse(200, {"values": self.values})
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {"properties": {"sheetId": 42, "title": "Mappings"}},
                            {"properties": {"sheetId": 43, "title": "Sync Alerts"}},
                        ]
                    },
                )

            def post(self, url, **kwargs):
                if url.endswith(":batchUpdate"):
                    return FakeResponse(
                        200,
                        {
                            "spreadsheetId": "a" * 30,
                            "replies": [{} for _request in kwargs["json"]["requests"]],
                        },
                    )
                appended = kwargs["json"]["values"]
                first_row = len(self.values) + 1
                self.values.extend(appended)
                last_row = len(self.values)
                return FakeResponse(
                    200,
                    {
                        "spreadsheetId": "a" * 30,
                        "tableRange": f"'Mappings'!A1:AG{first_row - 1}",
                        "updates": {
                            "spreadsheetId": "a" * 30,
                            "updatedRange": f"'Mappings'!A{first_row}:AG{last_row}",
                            "updatedRows": len(appended),
                            "updatedColumns": 33,
                            "updatedCells": len(appended) * 33,
                        }
                    },
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = sync.run_sync(
                table=current,
                inventories=[inv],
                output_dir=root / "reports",
                generated_at="2026-09-15T00:00:00Z",
                snapshot_out=root / "effective.csv",
                write_to_sheet=True,
                google_session=StatefulSheetSession(),
                sheet_id="a" * 30,
                sheet_tab="Mappings",
            )
            effective = sync.parse_mapping_csv((root / "effective.csv").read_bytes())
        self.assertEqual(summary["appended_rows"], 1)
        self.assertEqual(len(effective.rows), 2)
        self.assertEqual(effective.rows[1]["action"], "REVIEW")

    def test_appended_mapping_verification_requires_all_thirty_three_cells(self) -> None:
        expected = mapping_row(
            "server_1", "22", "=Literal Name", action="REVIEW", epg_id=""
        )
        sync.verify_appended_mapping_rows([expected], table([dict(expected)]))

        tampered = dict(expected)
        tampered["reason"] = "A different value returned by the Sheet"
        with self.assertRaisesRegex(sync.SyncError, "every cell"):
            sync.verify_appended_mapping_rows([expected], table([tampered]))

    def test_appended_mapping_verification_rejects_missing_identity(self) -> None:
        expected = mapping_row(
            "server_2", "22", "New Channel", action="REVIEW", epg_id=""
        )
        with self.assertRaisesRegex(sync.SyncError, "every cell"):
            sync.verify_appended_mapping_rows([expected], table([]))

    def test_append_transition_rejects_unrelated_existing_row_edit(self) -> None:
        existing = mapping_row("server_1", "11", "Existing")
        added = mapping_row("server_1", "22", "New", action="REVIEW", epg_id="")
        added["enabled"] = "FALSE"
        edited = dict(existing)
        edited["notes"] = "concurrent operator edit"

        with self.assertRaisesRegex(sync.SyncError, "unrelated Mapping row"):
            sync.verify_mapping_append_transition(
                table([existing]), [added], table([edited, added])
            )

    def test_cli_contract_matches_workflows(self) -> None:
        args = sync.parse_args(
            [
                "--mode",
                "bootstrap",
                "--sheet-id",
                "a" * 30,
                "--snapshot-out",
                "effective.csv",
                "--minimum-server-channels",
                "server_1=4000",
                "server_2=10000",
                "server_3=9000",
                "--write-to-sheet",
            ]
        )
        self.assertEqual(args.mode, "bootstrap")
        self.assertEqual(len(sync.parse_server_minimums(args.minimum_server_channels)), 3)
        self.assertTrue(args.write_to_sheet)
        self.assertEqual(args.alerts_tab, "Sync Alerts")

    def test_public_repo_workflow_uploads_summary_only(self) -> None:
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "channel_inventory_sync.yml"
        ).read_text(encoding="utf-8")
        upload_marker = "uses: actions/upload-artifact@"
        self.assertIn(upload_marker, workflow)
        upload_section = workflow.rsplit(upload_marker, 1)[-1]
        self.assertIn("path: .build/channel-sync/summary.json", upload_section)
        self.assertNotIn("path: .build/channel-sync\n", upload_section)
        self.assertNotIn("path: .build/channel-sync/\n", upload_section)
        self.assertIn("retention-days: 7", upload_section)

    def test_workflow_two_requires_the_hash_bound_snapshot_bundle(self) -> None:
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "main.yml"
        ).read_text(encoding="utf-8")
        for required in (
            "--authoritative-snapshot-out",
            "--snapshot-manifest-out",
            "--mapping-authoritative-file",
            "--mapping-snapshot-manifest",
            "test -s .build/channel-sync/authoritative_mapping.csv",
            "test -s .build/channel-sync/mapping_snapshot_manifest.json",
            "--minimum-server-rows server_3=9400",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)


class ReviewRecheckBoundaryTests(unittest.TestCase):
    @staticmethod
    def disabled_review(
        stream_id: str,
        *,
        source: str = "epgshare01",
        epg_id: str = "",
        notes: str = "",
    ) -> dict[str, str]:
        row = mapping_row(
            "server_1",
            stream_id,
            f"Channel {stream_id}",
            action="REVIEW",
            source=source,
            epg_id=epg_id,
        )
        row["enabled"] = "FALSE"
        row["notes"] = notes
        return row

    def test_review_selector_applies_every_eligibility_exclusion(self) -> None:
        eligible = self.disabled_review("eligible")
        known_auto = self.disabled_review(
            "known-auto",
            epg_id="Old.Auto.us2",
            notes="auto-map-v1 prior provisional result",
        )
        legacy_panel = self.disabled_review(
            "legacy-panel", source="panel", epg_id="native.panel.id"
        )
        not_review = mapping_row("server_1", "not-review", "Approved")
        enabled_review = self.disabled_review("enabled-review")
        enabled_review["enabled"] = "TRUE"
        missing = self.disabled_review("missing")
        open_alert = self.disabled_review("open-alert")
        changed = self.disabled_review("changed")
        manual_candidate = self.disabled_review(
            "manual-candidate", epg_id="Operator.Choice.us2", notes="chosen by operator"
        )
        another_server = mapping_row(
            "server_2", "outside-scope", "Outside Scope", action="REVIEW", epg_id=""
        )
        another_server["enabled"] = "FALSE"
        rows = [
            eligible,
            known_auto,
            legacy_panel,
            not_review,
            enabled_review,
            missing,
            open_alert,
            changed,
            manual_candidate,
            another_server,
        ]
        present_ids = [
            row["stream_id"]
            for row in rows
            if row["server_id"] == "server_1" and row["stream_id"] != "missing"
        ]
        provider = inventory(
            "server_1",
            [
                {
                    "stream_id": stream_id,
                    "name": f"Channel {stream_id}",
                    "category_name": "General",
                }
                for stream_id in present_ids
            ],
        )

        selected, stats = sync.select_review_recheck_rows(
            table(rows),
            [provider],
            selected_servers={"server_1"},
            quarantined_keys={("server_1", "open-alert")},
            changed_rows=[{"server_id": "server_1", "stream_id": "changed"}],
        )

        self.assertEqual(
            [row["stream_id"] for row in selected],
            ["eligible", "known-auto", "legacy-panel"],
        )
        self.assertEqual(stats["review_recheck_selected_server_rows"], 9)
        self.assertEqual(stats["review_recheck_eligible_rows"], 3)
        self.assertEqual(stats["review_recheck_skipped_rows"], 5)
        for field in (
            "review_recheck_excluded_not_review",
            "review_recheck_excluded_enabled",
            "review_recheck_excluded_missing_provider",
            "review_recheck_excluded_open_alert",
            "review_recheck_excluded_changed_identity",
            "review_recheck_excluded_manual_candidate",
        ):
            with self.subTest(field=field):
                self.assertEqual(stats[field], 1)

    def test_review_update_is_one_atomic_seven_column_targeted_write(self) -> None:
        review = self.disabled_review("review-1")
        unaffected = mapping_row("server_1", "stable-2", "Stable")
        desired = dict(review)
        desired.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Verified exact Smart Rules match",
                "notes": "auto-map-v1 verified",
            }
        )
        session = ReviewUpdateSession([review, unaffected])
        pre_write_post_counts: list[int] = []

        count, final_table = sync.update_google_sheet_review_rows(
            session,
            "a" * 30,
            "Mappings",
            table([review, unaffected]),
            [desired],
            pre_write_check=lambda: pre_write_post_counts.append(len(session.posts)),
        )

        self.assertEqual(count, 1)
        self.assertEqual(pre_write_post_counts, [0])
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(session.posts[0][0].endswith(":batchUpdate"))
        requests = session.posts[0][1]["json"]["requests"]
        self.assertEqual(len(requests), 3)
        touched_columns: set[int] = set()
        for request in requests:
            update = request["updateCells"]
            self.assertEqual(update["fields"], "userEnteredValue")
            self.assertEqual(update["range"]["startRowIndex"], 1)
            self.assertEqual(update["range"]["endRowIndex"], 2)
            touched_columns.update(
                range(
                    update["range"]["startColumnIndex"],
                    update["range"]["endColumnIndex"],
                )
            )
        column_indexes = {
            name: index for index, name in enumerate(streaming.SHEET_COLUMNS)
        }
        self.assertEqual(
            touched_columns,
            {column_indexes[name] for name in sync.RECHECK_PATCH_COLUMNS},
        )
        self.assertEqual(len(touched_columns), 7)
        self.assertEqual(final_table.rows[0]["epg_id"], "Good.Channel.us2")
        self.assertEqual(final_table.rows[1], unaffected)
        self.assertEqual(session.values_reads, 2)

    def test_review_update_preimage_race_sends_no_write(self) -> None:
        review = self.disabled_review("review-1")
        desired = dict(review)
        desired.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "epg_id": "Good.Channel.us2",
                "reason": "Verified",
            }
        )
        concurrently_edited = dict(review)
        concurrently_edited["notes"] = "operator edited while proposals ran"
        session = ReviewUpdateSession([concurrently_edited])

        with self.assertRaisesRegex(sync.SyncError, "changed before REVIEW updates"):
            sync.update_google_sheet_review_rows(
                session, "a" * 30, "Mappings", table([review]), [desired]
            )

        self.assertEqual(session.posts, [])
        self.assertEqual(session.values_reads, 1)

    def test_review_update_rejects_any_unrelated_concurrent_edit(self) -> None:
        review = self.disabled_review("review-1")
        unaffected = mapping_row("server_1", "stable-2", "Stable")
        desired = dict(review)
        desired.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "epg_id": "Good.Channel.us2",
                "reason": "Verified",
            }
        )
        session = ReviewUpdateSession(
            [review, unaffected], mutate_non_target_after_commit=True
        )

        with self.assertRaisesRegex(sync.SyncError, "unrelated Mapping row changed"):
            sync.update_google_sheet_review_rows(
                session,
                "a" * 30,
                "Mappings",
                table([review, unaffected]),
                [desired],
            )

    def test_review_update_recovers_ambiguous_success_by_authoritative_reread(self) -> None:
        review = self.disabled_review("review-1")
        desired = dict(review)
        desired.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "epg_id": "Good.Channel.us2",
                "reason": "Verified",
            }
        )
        for kwargs in (
            {"valid_payload": False},
            {"raise_after_commit": True},
        ):
            with self.subTest(kwargs=kwargs):
                session = ReviewUpdateSession([review], **kwargs)
                count, final_table = sync.update_google_sheet_review_rows(
                    session, "a" * 30, "Mappings", table([review]), [desired]
                )
                self.assertEqual(count, 1)
                self.assertEqual(final_table.rows[0]["epg_id"], "Good.Channel.us2")
                self.assertEqual(session.values_reads, 2)

    def test_review_update_recovers_committed_retryable_http_failure(self) -> None:
        review = self.disabled_review("review-1")
        desired = dict(review)
        desired.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "epg_id": "Good.Channel.us2",
                "reason": "Verified",
            }
        )
        session = ReviewUpdateSession([review], status_code=503, commit=True)

        count, final_table = sync.update_google_sheet_review_rows(
            session, "a" * 30, "Mappings", table([review]), [desired]
        )

        self.assertEqual(count, 1)
        self.assertEqual(final_table.rows[0]["epg_id"], "Good.Channel.us2")
        self.assertEqual(session.values_reads, 2)

    @staticmethod
    def ai_shortlist(stream_id: str = "review-1"):
        return sync.automatch.AiReviewShortlist(
            server_id="server_1",
            stream_id=stream_id,
            channel_name=f"Channel {stream_id}",
            category_name="US | General",
            market="US",
            candidates=(
                sync.automatch.AiReviewCandidate(
                    candidate_key="c01",
                    epg_id="Candidate.One.us2",
                    display_name="Candidate One",
                    feed="US2",
                    region="US",
                    local_score=91,
                ),
                sync.automatch.AiReviewCandidate(
                    candidate_key="c02",
                    epg_id="Candidate.Two.us2",
                    display_name="Candidate Two",
                    feed="US2",
                    region="US",
                    local_score=88,
                ),
            ),
        )

    @staticmethod
    def fake_recheck_outcome(
        rows: list[dict[str, str]], *, shortlists=()
    ) -> SimpleNamespace:
        approved = sum(
            1
            for row in rows
            if row.get("action") == "AUTO_EPGSHARE"
            and row.get("enabled") == "TRUE"
        )
        fields = {
            "auto_match_considered_rows": 0,
            "auto_match_provisional_rows": approved,
            "auto_matched_rows": 0,
            "auto_match_review_rows": 0,
            "review_recheck_considered_rows": len(rows),
            "review_recheck_safe_matches": approved,
            "review_recheck_still_review_rows": len(rows) - approved,
            "auto_match_rejected_programme_gates": 0,
            "ai_review_shortlisted_rows": len(shortlists),
            "ai_review_shortlisted_candidates": sum(
                len(item.candidates) for item in shortlists
            ),
        }
        return SimpleNamespace(
            rows=tuple(rows),
            ai_review_shortlists=tuple(shortlists),
            summary_fields=lambda: dict(fields),
        )

    @staticmethod
    def gemini_batch(result) -> object:
        return sync.gemini_review.ReviewBatchResult(
            results=(result,),
            prompt_tokens=12,
            candidate_tokens=3,
            total_tokens=15,
            batches_attempted=1,
            batches_succeeded=1,
        )

    def test_gemini_high_suggestion_creates_disabled_review_patch_only(self) -> None:
        review = self.disabled_review("review-1")
        outcome = SimpleNamespace(ai_review_shortlists=(self.ai_shortlist(),))
        result = sync.gemini_review.ReviewResult(
            review_id="review-0001",
            decision=sync.gemini_review.ReviewDecision.SUGGEST,
            candidate_key="c02",
            confidence=sync.gemini_review.ReviewConfidence.HIGH,
        )

        with mock.patch.object(
            sync.gemini_review,
            "review_flagged_channels",
            return_value=self.gemini_batch(result),
        ) as call:
            updates, summary = sync._gemini_review_updates(
                outcome=outcome,
                authoritative_table=table([review]),
                api_key="test-key",
                limit=25,
            )

        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update["enabled"], "FALSE")
        self.assertEqual(update["action"], "REVIEW")
        self.assertEqual(update["source"], "epgshare01")
        self.assertEqual(update["epg_feed"], "ALL_SOURCES1")
        self.assertEqual(update["epg_id"], "Candidate.Two.us2")
        self.assertIn("manual approval is required", update["reason"])
        self.assertIn("ai-review-v1", update["notes"])
        for column in set(streaming.SHEET_COLUMNS) - set(sync.RECHECK_PATCH_COLUMNS):
            with self.subTest(column=column):
                self.assertEqual(update[column], review[column])
        request = call.call_args.args[0][0]
        self.assertEqual(request.review_id, "review-0001")
        self.assertEqual(
            [candidate.candidate_key for candidate in request.candidates],
            ["c01", "c02"],
        )
        self.assertEqual(summary["ai_review_suggestion_rows"], 1)
        self.assertEqual(summary["ai_review_high_suggestions_found"], 1)
        self.assertEqual(summary["ai_review_high_suggestion_updates"], 1)
        self.assertEqual(summary["ai_review_high_suggestions_persisted"], 0)
        self.assertEqual(summary["ai_review_abstained_rows"], 0)
        self.assertEqual(summary["ai_review_error_rows"], 0)
        self.assertEqual(summary["ai_review_total_tokens"], 15)

    def test_gemini_non_high_marks_reviewed_but_error_creates_no_patch(self) -> None:
        review = self.disabled_review("review-1")
        outcome = SimpleNamespace(ai_review_shortlists=(self.ai_shortlist(),))
        cases = (
            (
                sync.gemini_review.ReviewDecision.SUGGEST,
                sync.gemini_review.ReviewConfidence.MEDIUM,
                "c01",
                "marked",
            ),
            (
                sync.gemini_review.ReviewDecision.SUGGEST,
                sync.gemini_review.ReviewConfidence.LOW,
                "c01",
                "marked",
            ),
            (
                sync.gemini_review.ReviewDecision.ABSTAIN,
                sync.gemini_review.ReviewConfidence.NONE,
                None,
                "marked",
            ),
            (
                sync.gemini_review.ReviewDecision.ERROR,
                sync.gemini_review.ReviewConfidence.NONE,
                None,
                "error",
            ),
        )
        for decision, confidence, candidate_key, expected in cases:
            with self.subTest(decision=decision, confidence=confidence):
                result = sync.gemini_review.ReviewResult(
                    review_id="review-0001",
                    decision=decision,
                    candidate_key=candidate_key,
                    confidence=confidence,
                    error_code="fixture" if decision.value == "ERROR" else None,
                )
                with mock.patch.object(
                    sync.gemini_review,
                    "review_flagged_channels",
                    return_value=self.gemini_batch(result),
                ):
                    updates, summary = sync._gemini_review_updates(
                        outcome=outcome,
                        authoritative_table=table([review]),
                        api_key="test-key",
                        limit=25,
                    )
                if expected == "error":
                    self.assertEqual(updates, [])
                    self.assertEqual(summary["ai_review_error_rows"], 1)
                    self.assertEqual(summary["ai_review_abstained_rows"], 0)
                else:
                    self.assertEqual(len(updates), 1)
                    update = updates[0]
                    self.assertEqual(update["enabled"], "FALSE")
                    self.assertEqual(update["action"], "REVIEW")
                    for column in ("source", "epg_feed", "epg_id"):
                        self.assertEqual(update[column], review[column])
                    changed = {
                        column
                        for column in streaming.SHEET_COLUMNS
                        if update[column] != review[column]
                    }
                    self.assertEqual(changed, {"reason", "notes"})
                    self.assertIn("ai-review-v1", update["notes"])
                    self.assertEqual(summary["ai_review_error_rows"], 0)
                    self.assertEqual(summary["ai_review_abstained_rows"], 1)
                    self.assertEqual(summary["ai_review_abstain_marked_rows"], 1)

    def test_gemini_repeated_suggestion_is_idempotent(self) -> None:
        review = self.disabled_review("review-1")
        outcome = SimpleNamespace(ai_review_shortlists=(self.ai_shortlist(),))
        result = sync.gemini_review.ReviewResult(
            review_id="review-0001",
            decision=sync.gemini_review.ReviewDecision.SUGGEST,
            candidate_key="c01",
            confidence=sync.gemini_review.ReviewConfidence.HIGH,
        )
        batch = self.gemini_batch(result)
        with mock.patch.object(
            sync.gemini_review, "review_flagged_channels", return_value=batch
        ):
            first, _summary = sync._gemini_review_updates(
                outcome=outcome,
                authoritative_table=table([review]),
                api_key="test-key",
                limit=25,
            )
            second, repeat_summary = sync._gemini_review_updates(
                outcome=outcome,
                authoritative_table=table([first[0]]),
                api_key="test-key",
                limit=25,
            )

        self.assertEqual(second, [])
        self.assertEqual(repeat_summary["ai_review_suggestion_rows"], 1)
        self.assertEqual(repeat_summary["ai_review_repeat_suggestions"], 1)

    def test_gemini_api_exception_fails_closed_without_blocking(self) -> None:
        review = self.disabled_review("review-1")
        outcome = SimpleNamespace(ai_review_shortlists=(self.ai_shortlist(),))
        with mock.patch.object(
            sync.gemini_review,
            "review_flagged_channels",
            side_effect=TimeoutError("Gemini unavailable"),
        ):
            updates, summary = sync._gemini_review_updates(
                outcome=outcome,
                authoritative_table=table([review]),
                api_key="test-key",
                limit=25,
            )

        self.assertEqual(updates, [])
        self.assertEqual(summary["ai_review_error_rows"], 1)
        self.assertEqual(summary["ai_review_status"], "failed_closed")

    def test_recheck_dry_run_never_writes_mapping_rows(self) -> None:
        review = self.disabled_review("review-1")
        approved = dict(review)
        approved.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Smart Rules verified",
            }
        )
        provider = inventory(
            "server_1",
            [{"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"}],
        )
        outcome = self.fake_recheck_outcome([approved])

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ) as matcher, mock.patch.object(
            sync, "update_google_sheet_review_rows"
        ) as update_rows, mock.patch.object(
            sync, "append_google_sheet_rows"
        ) as append_rows, mock.patch.object(
            sync, "append_sync_alert_rows"
        ) as append_alerts:
            root = Path(temporary)
            summary = sync.run_sync(
                table=table([review]),
                inventories=[provider],
                output_dir=root / "reports",
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=root / "effective.csv",
                all_source_file=root / "all.xml.gz",
                all_source_catalog_file=root / "all.txt",
                epgshare_spool_out=root / "selected.sqlite3",
                review_recheck_mode="dry-run",
                review_recheck_servers=("server_1",),
            )
            effective = streaming.parse_mapping_csv(
                (root / "effective.csv").read_bytes(),
                {"server_1"},
                require_enabled_servers=False,
            )

        update_rows.assert_not_called()
        append_rows.assert_not_called()
        append_alerts.assert_not_called()
        self.assertEqual(summary["review_recheck_safe_matches"], 1)
        self.assertEqual(summary["review_recheck_rows_updated"], 0)
        self.assertIs(matcher.call_args.kwargs["enable_ai_review"], False)
        self.assertFalse(effective[0].runtime_eligible)

    def test_recheck_dry_run_reports_ai_high_without_claiming_it_was_saved(self) -> None:
        review = self.disabled_review("review-1")
        shortlist = self.ai_shortlist("review-1")
        outcome = self.fake_recheck_outcome([review], shortlists=(shortlist,))
        provider = inventory(
            "server_1",
            [{"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"}],
        )
        result = sync.gemini_review.ReviewResult(
            review_id="review-0001",
            decision=sync.gemini_review.ReviewDecision.SUGGEST,
            candidate_key="c01",
            confidence=sync.gemini_review.ReviewConfidence.HIGH,
        )

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ) as ai_matcher, mock.patch.object(
            sync.gemini_review,
            "review_flagged_channels",
            return_value=self.gemini_batch(result),
        ):
            root = Path(temporary)
            summary = sync.run_sync(
                table=table([review]),
                inventories=[provider],
                output_dir=root / "reports",
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=root / "effective.csv",
                all_source_file=root / "all.xml.gz",
                all_source_catalog_file=root / "all.txt",
                epgshare_spool_out=root / "selected.sqlite3",
                review_recheck_mode="dry-run",
                review_recheck_servers=("server_1",),
                use_gemini_ai=True,
                gemini_api_key="test-key",
            )
            with (root / "reports" / "ai_review_results.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                report_rows = list(csv.DictReader(handle))

        self.assertEqual(summary["ai_review_high_suggestions_found"], 1)
        self.assertEqual(summary["ai_review_high_suggestions_persisted"], 0)
        self.assertIs(ai_matcher.call_args.kwargs["enable_ai_review"], True)
        self.assertEqual(len(report_rows), 1)
        self.assertEqual(report_rows[0]["epg_id"], "Candidate.One.us2")
        self.assertEqual(report_rows[0]["enabled"], "FALSE")

    def test_recheck_apply_rereads_alerts_and_blocks_new_quarantine_race(self) -> None:
        review = self.disabled_review("review-1")
        approved = dict(review)
        approved.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Smart Rules verified",
            }
        )
        provider = inventory(
            "server_1",
            [{"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"}],
        )
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "detected_at": "2026-09-16T00:00:01Z",
                "server_id": "server_1",
                "stream_id": "review-1",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )
        empty_alerts = [list(sync.ALERT_COLUMNS)]
        opened_alerts = [
            list(sync.ALERT_COLUMNS),
            [alert[column] for column in sync.ALERT_COLUMNS],
        ]

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync, "google_sheet_values", return_value=mapping_values([review])
        ), mock.patch.object(
            sync,
            "google_sync_alert_values",
            side_effect=[empty_alerts, empty_alerts, opened_alerts],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch,
            "auto_match_and_spool",
            return_value=self.fake_recheck_outcome([approved]),
        ), mock.patch.object(
            sync, "update_google_sheet_review_rows"
        ) as update_rows:
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "quarantine set changed"):
                sync.run_sync(
                    table=table([]),
                    inventories=[provider],
                    output_dir=root / "reports",
                    generated_at="2026-09-16T00:00:00Z",
                    snapshot_out=root / "effective.csv",
                    google_session=object(),
                    sheet_id="a" * 30,
                    all_source_file=root / "all.xml.gz",
                    all_source_catalog_file=root / "all.txt",
                    epgshare_spool_out=root / "selected.sqlite3",
                    review_recheck_mode="apply",
                    review_recheck_servers=("server_1",),
                )
            self.assertFalse((root / "effective.csv").exists())

        update_rows.assert_not_called()

    def test_recheck_apply_blocks_unrelated_new_open_evidence_quarantine(self) -> None:
        review = self.disabled_review("review-1")
        evidence = mapping_row(
            "server_1", "evidence-2", "Human Approved Evidence", action="APPROVED"
        )
        approved = dict(review)
        approved.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Smart Rules verified",
            }
        )
        provider = inventory(
            "server_1",
            [
                {"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"},
                {"stream_id": "evidence-2", "name": "Human Approved Evidence", "category_name": "General"},
            ],
        )
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "detected_at": "2026-09-16T00:00:01Z",
                "server_id": "server_1",
                "stream_id": "evidence-2",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )
        empty_alerts = [list(sync.ALERT_COLUMNS)]
        opened_alerts = [
            list(sync.ALERT_COLUMNS),
            [alert[column] for column in sync.ALERT_COLUMNS],
        ]

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync,
            "google_sheet_values",
            return_value=mapping_values([review, evidence]),
        ), mock.patch.object(
            sync,
            "google_sync_alert_values",
            side_effect=[empty_alerts, empty_alerts, opened_alerts],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch,
            "auto_match_and_spool",
            return_value=self.fake_recheck_outcome([approved]),
        ), mock.patch.object(
            sync, "update_google_sheet_review_rows"
        ) as update_rows:
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "quarantine set changed"):
                sync.run_sync(
                    table=table([]),
                    inventories=[provider],
                    output_dir=root / "reports",
                    generated_at="2026-09-16T00:00:00Z",
                    snapshot_out=root / "effective.csv",
                    google_session=object(),
                    sheet_id="a" * 30,
                    all_source_file=root / "all.xml.gz",
                    all_source_catalog_file=root / "all.txt",
                    epgshare_spool_out=root / "selected.sqlite3",
                    review_recheck_mode="apply",
                    review_recheck_servers=("server_1",),
                )
            self.assertFalse((root / "effective.csv").exists())

        update_rows.assert_not_called()

    def test_recheck_apply_reverifies_this_runs_alert_at_last_write_boundary(self) -> None:
        review = self.disabled_review("review-1")
        stale = mapping_row("server_1", "stale-2", "Old Station")
        approved = dict(review)
        approved.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Smart Rules verified",
            }
        )
        provider = inventory(
            "server_1",
            [
                {"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"},
                {"stream_id": "stale-2", "name": "Different Station", "category_name": "General"},
            ],
        )
        generated_at = "2026-09-16T00:00:00Z"
        _new, changed, _missing = sync.compare_inventory(
            table([review, stale]), [provider], discovered_at=generated_at
        )
        pending = sync.pending_sync_alert_rows(changed, [], detected_at=generated_at)
        self.assertEqual(len(pending), 1)
        empty_alerts = [list(sync.ALERT_COLUMNS)]
        committed_alerts = [
            list(sync.ALERT_COLUMNS),
            [pending[0][column] for column in sync.ALERT_COLUMNS],
        ]

        def reach_last_boundary(
            _session,
            _sheet_id,
            _tab_name,
            _base_table,
            _rows,
            *,
            pre_write_check=None,
        ):
            self.assertIsNotNone(pre_write_check)
            pre_write_check()
            raise AssertionError("Mappings write must not be reached")

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync,
            "google_sheet_values",
            return_value=mapping_values([review, stale]),
        ), mock.patch.object(
            sync,
            "google_sync_alert_values",
            side_effect=[
                empty_alerts,
                committed_alerts,
                committed_alerts,
                empty_alerts,
            ],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=1
        ), mock.patch.object(
            sync.automatch,
            "auto_match_and_spool",
            return_value=self.fake_recheck_outcome([approved]),
        ), mock.patch.object(
            sync,
            "update_google_sheet_review_rows",
            side_effect=reach_last_boundary,
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "did not durably store"):
                sync.run_sync(
                    table=table([]),
                    inventories=[provider],
                    output_dir=root / "reports",
                    generated_at=generated_at,
                    snapshot_out=root / "effective.csv",
                    google_session=object(),
                    sheet_id="a" * 30,
                    all_source_file=root / "all.xml.gz",
                    all_source_catalog_file=root / "all.txt",
                    epgshare_spool_out=root / "selected.sqlite3",
                    review_recheck_mode="apply",
                    review_recheck_servers=("server_1",),
                )
            self.assertFalse((root / "effective.csv").exists())

    def test_recheck_apply_no_updates_blocks_stale_terminal_snapshot(self) -> None:
        review = self.disabled_review("review-1")
        concurrent = dict(review)
        concurrent["notes"] = "operator edited after proposals"
        provider = inventory("server_1", [])
        outcome = self.fake_recheck_outcome([])

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync,
            "google_sheet_values",
            side_effect=[
                mapping_values([review]),
                mapping_values([concurrent]),
                mapping_values([concurrent]),
            ],
        ), mock.patch.object(
            sync,
            "google_sync_alert_values",
            return_value=[list(sync.ALERT_COLUMNS)],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(sync.SyncError, "Terminal.*snapshot"):
                sync.run_sync(
                    table=table([]),
                    inventories=[provider],
                    output_dir=root / "reports",
                    generated_at="2026-09-16T00:00:00Z",
                    snapshot_out=root / "effective.csv",
                    write_to_sheet=True,
                    google_session=object(),
                    sheet_id="a" * 30,
                    all_source_file=root / "all.xml.gz",
                    all_source_catalog_file=root / "all.txt",
                    epgshare_spool_out=root / "selected.sqlite3",
                    review_recheck_mode="apply",
                    review_recheck_servers=("server_1",),
                )
            self.assertFalse((root / "effective.csv").exists())

    def test_recheck_apply_blocks_new_open_alert_at_snapshot_boundary(self) -> None:
        review = self.disabled_review("review-1")
        provider = inventory("server_1", [])
        outcome = self.fake_recheck_outcome([])
        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "detected_at": "2026-09-16T00:00:01Z",
                "server_id": "server_1",
                "stream_id": "review-1",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )
        empty_alerts = [list(sync.ALERT_COLUMNS)]
        opened_alerts = [
            list(sync.ALERT_COLUMNS),
            [alert[column] for column in sync.ALERT_COLUMNS],
        ]

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync, "google_sheet_values", return_value=mapping_values([review])
        ), mock.patch.object(
            sync,
            "google_sync_alert_values",
            side_effect=[empty_alerts, empty_alerts, opened_alerts],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(
                sync.SyncError, "changed immediately before.*snapshot"
            ):
                sync.run_sync(
                    table=table([]),
                    inventories=[provider],
                    output_dir=root / "reports",
                    generated_at="2026-09-16T00:00:00Z",
                    snapshot_out=root / "effective.csv",
                    authoritative_snapshot_out=root / "authoritative.csv",
                    snapshot_manifest_out=root / "manifest.json",
                    google_session=object(),
                    sheet_id="a" * 30,
                    all_source_file=root / "all.xml.gz",
                    all_source_catalog_file=root / "all.txt",
                    epgshare_spool_out=root / "selected.sqlite3",
                    review_recheck_mode="apply",
                    review_recheck_servers=("server_1",),
                )
            for path in (
                root / "effective.csv",
                root / "authoritative.csv",
                root / "manifest.json",
            ):
                self.assertFalse(path.exists())

    def test_recheck_apply_combines_new_append_review_update_and_final_snapshot(self) -> None:
        review = self.disabled_review("review-1")
        provider = inventory(
            "server_1",
            [
                {"stream_id": "review-1", "name": "Channel review-1", "category_name": "General"},
                {"stream_id": "new-2", "name": "Brand New", "category_name": "General"},
            ],
        )
        generated_at = "2026-09-16T00:00:00Z"
        approved = dict(review)
        approved.update(
            {
                "enabled": "TRUE",
                "action": "AUTO_EPGSHARE",
                "source": "epgshare01",
                "epg_feed": "ALL_SOURCES1",
                "epg_id": "Good.Channel.us2",
                "reason": "Smart Rules verified",
            }
        )
        new_row = sync.new_mapping_row(
            provider, provider.channels[1], discovered_at=generated_at
        )
        outcome = self.fake_recheck_outcome([approved, new_row])
        current_rows = [dict(review)]

        def read_mapping(*_args, **_kwargs):
            return mapping_values(current_rows)

        def append_rows(_session, _sheet_id, _tab_name, base_table, rows):
            self.assertEqual(len(base_table.rows), 1)
            self.assertEqual([row["stream_id"] for row in rows], ["new-2"])
            current_rows.extend(dict(row) for row in rows)
            return len(rows)

        def apply_updates(
            _session,
            _sheet_id,
            _tab_name,
            base_table,
            rows,
            *,
            pre_write_check=None,
        ):
            self.assertEqual(len(base_table.rows), 2)
            self.assertEqual([row["stream_id"] for row in rows], ["review-1"])
            self.assertIsNotNone(pre_write_check)
            pre_write_check()
            replacements = {_row["stream_id"]: dict(_row) for _row in rows}
            current_rows[:] = [
                replacements.get(row["stream_id"], dict(row))
                for row in current_rows
            ]
            return len(rows), table([dict(row) for row in current_rows])

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync, "google_sheet_values", side_effect=read_mapping
        ) as mapping_reads, mock.patch.object(
            sync,
            "google_sync_alert_values",
            return_value=[list(sync.ALERT_COLUMNS)],
        ) as alert_reads, mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ), mock.patch.object(
            sync, "append_google_sheet_rows", side_effect=append_rows
        ) as append_call, mock.patch.object(
            sync, "update_google_sheet_review_rows", side_effect=apply_updates
        ) as update_call:
            root = Path(temporary)
            summary = sync.run_sync(
                table=table([]),
                inventories=[provider],
                output_dir=root / "reports",
                generated_at=generated_at,
                snapshot_out=root / "effective.csv",
                authoritative_snapshot_out=root / "authoritative.csv",
                snapshot_manifest_out=root / "manifest.json",
                write_to_sheet=True,
                google_session=object(),
                sheet_id="a" * 30,
                all_source_file=root / "all.xml.gz",
                all_source_catalog_file=root / "all.txt",
                epgshare_spool_out=root / "selected.sqlite3",
                review_recheck_mode="apply",
                review_recheck_servers=("server_1",),
            )
            authoritative = streaming.parse_mapping_csv(
                (root / "authoritative.csv").read_bytes(),
                {"server_1"},
                require_enabled_servers=False,
            )
            effective = streaming.parse_mapping_csv(
                (root / "effective.csv").read_bytes(),
                {"server_1"},
                require_enabled_servers=False,
            )

        self.assertEqual(summary["appended_rows"], 1)
        self.assertEqual(summary["review_recheck_rows_updated"], 1)
        self.assertEqual(mapping_reads.call_count, 3)
        self.assertEqual(alert_reads.call_count, 6)
        append_call.assert_called_once()
        update_call.assert_called_once()
        self.assertEqual(len(authoritative), 2)
        self.assertEqual(len(effective), 2)
        by_stream = {row.stream_id: row for row in effective}
        self.assertTrue(by_stream["review-1"].runtime_eligible)
        self.assertFalse(by_stream["new-2"].runtime_eligible)

    def test_recheck_apply_caps_smart_rules_and_ai_outage_is_nonblocking(self) -> None:
        originals = [self.disabled_review(f"r{index:03d}") for index in range(102)]
        approved_rows: list[dict[str, str]] = []
        for index, original in enumerate(originals):
            row = dict(original)
            if index < 101:
                row.update(
                    {
                        "enabled": "TRUE",
                        "action": "AUTO_EPGSHARE",
                        "source": "epgshare01",
                        "epg_feed": "ALL_SOURCES1",
                        "epg_id": f"Verified.{index:03d}.us2",
                        "reason": "Smart Rules verified",
                    }
                )
            approved_rows.append(row)
        shortlist = self.ai_shortlist("r101")
        outcome = self.fake_recheck_outcome(
            approved_rows, shortlists=(shortlist,)
        )
        provider = inventory(
            "server_1",
            [
                {
                    "stream_id": row["stream_id"],
                    "name": row["channel_name"],
                    "category_name": "General",
                }
                for row in originals
            ],
        )
        captured_updates: list[dict[str, str]] = []
        current_mapping_rows = [dict(row) for row in originals]

        def apply_updates(
            _session,
            _sheet_id,
            _tab_name,
            base_table,
            rows,
            *,
            pre_write_check=None,
        ):
            if pre_write_check is not None:
                pre_write_check()
            captured_updates.extend(dict(row) for row in rows)
            by_key = {
                (row["server_id"], row["stream_id"]): dict(row) for row in rows
            }
            final_rows = [
                by_key.get((row["server_id"], row["stream_id"]), dict(row))
                for row in base_table.rows
            ]
            current_mapping_rows[:] = [dict(row) for row in final_rows]
            return len(rows), table(final_rows)

        def read_mapping(*_args, **_kwargs):
            return mapping_values(current_mapping_rows)

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            sync, "google_sheet_values", side_effect=read_mapping
        ) as mapping_read, mock.patch.object(
            sync,
            "google_sync_alert_values",
            return_value=[list(sync.ALERT_COLUMNS)],
        ), mock.patch.object(
            sync, "append_sync_alert_rows", return_value=0
        ), mock.patch.object(
            sync.automatch, "auto_match_and_spool", return_value=outcome
        ), mock.patch.object(
            sync, "update_google_sheet_review_rows", side_effect=apply_updates
        ), mock.patch.object(
            sync.gemini_review,
            "review_flagged_channels",
            side_effect=TimeoutError("Gemini unavailable"),
        ):
            root = Path(temporary)
            summary = sync.run_sync(
                # Deliberately stale input proves apply mode snapshots the
                # authoritative Google re-read, not this caller copy.
                table=table([]),
                inventories=[provider],
                output_dir=root / "reports",
                generated_at="2026-09-16T00:00:00Z",
                snapshot_out=root / "effective.csv",
                authoritative_snapshot_out=root / "authoritative.csv",
                snapshot_manifest_out=root / "manifest.json",
                google_session=object(),
                sheet_id="a" * 30,
                all_source_file=root / "all.xml.gz",
                all_source_catalog_file=root / "all.txt",
                epgshare_spool_out=root / "selected.sqlite3",
                review_recheck_mode="apply",
                review_recheck_servers=("server_1",),
                use_gemini_ai=True,
                gemini_api_key="test-key",
            )
            authoritative = streaming.parse_mapping_csv(
                (root / "authoritative.csv").read_bytes(),
                {"server_1"},
                require_enabled_servers=False,
            )

        self.assertEqual(mapping_read.call_count, 2)
        self.assertEqual(len(captured_updates), sync.MAX_RECHECK_APPLIES_PER_RUN)
        self.assertTrue(
            all(row["action"] == "AUTO_EPGSHARE" for row in captured_updates)
        )
        self.assertEqual(summary["review_recheck_deferred_rows"], 1)
        self.assertEqual(summary["review_recheck_rows_updated"], 100)
        self.assertEqual(summary["ai_review_error_rows"], 1)
        self.assertEqual(summary["ai_review_status"], "failed_closed")
        self.assertEqual(len(authoritative), 102)
        self.assertEqual(sum(row.runtime_eligible for row in authoritative), 100)
        by_stream = {row.stream_id: row for row in authoritative}
        self.assertTrue(by_stream["r099"].runtime_eligible)
        self.assertFalse(by_stream["r100"].runtime_eligible)
        self.assertFalse(by_stream["r101"].runtime_eligible)


if __name__ == "__main__":
    unittest.main()
