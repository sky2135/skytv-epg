from __future__ import annotations

import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
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
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "name": "SyncAlertsTable",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": len(self.alert_values),
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
                requests = kwargs["json"]["requests"]
                append_cells = requests[0].get("appendCells", {})
                if (
                    self.commit_alert
                    and append_cells.get("tableId") == "table-alerts-v1"
                ):
                    for row in append_cells["rows"]:
                        self.alert_values.append(
                            [
                                cell["userEnteredValue"]["stringValue"]
                                for cell in row["values"]
                            ]
                        )
                payload = (
                    {
                        "spreadsheetId": "a" * 30,
                        "replies": [{} for _request in requests],
                    }
                    if self.valid_response
                    else {"replies": [{} for _request in requests]}
                )
                return FakeResponse(200, payload)

        # A syntactically valid AppendCells response is not enough: if the
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
                                "properties": {"sheetId": 42, "title": "Mappings"},
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
        self.assertIn("tables(tableId,name,range)", session.get_kwargs["params"]["fields"])
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

    def test_modern_mapping_table_uses_append_cells_with_literal_strings(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "=Formula", action="REVIEW", epg_id="")

        class ModernSession:
            def __init__(self):
                self.posts = []

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {"sheetId": 42, "title": "Mappings"},
                                "tables": [
                                    {
                                        "tableId": "table-mappings-v1",
                                        "name": "MappingsTable",
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

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse(
                    200, {"spreadsheetId": "a" * 30, "replies": [{}]}
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
        self.assertTrue(url.endswith(":batchUpdate"))
        append_cells = kwargs["json"]["requests"][0]["appendCells"]
        self.assertEqual(append_cells["tableId"], "table-mappings-v1")
        self.assertNotIn("sheetId", append_cells)
        self.assertEqual(append_cells["fields"], "userEnteredValue")
        name_index = list(streaming.SHEET_COLUMNS).index("channel_name")
        literal_name = append_cells["rows"][0]["values"][name_index]
        self.assertEqual(literal_name, {"userEnteredValue": {"stringValue": "=Formula"}})

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

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "name": "SyncAlertsTable",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": 1,
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
                return FakeResponse(
                    200, {"spreadsheetId": "a" * 30, "replies": [{}]}
                )

        session = ModernAlertSession()
        self.assertEqual(
            sync.append_sync_alert_rows(
                session, "a" * 30, "Sync Alerts", [], [alert]
            ),
            1,
        )
        append_cells = session.posts[0][1]["json"]["requests"][0]["appendCells"]
        self.assertEqual(append_cells["tableId"], "table-alerts-v1")
        self.assertEqual(len(append_cells["rows"][0]["values"]), 11)
        self.assertEqual(
            append_cells["rows"][0]["values"][4],
            {"userEnteredValue": {"stringValue": "+Literal"}},
        )

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
                                "properties": {"sheetId": 42, "title": "Mappings"},
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

    def test_modern_table_must_cover_exact_authoritative_row_count(self) -> None:
        current = table([mapping_row("server_1", "1", "One")])
        new = mapping_row("server_1", "2", "Two", action="REVIEW", epg_id="")

        for wrong_end_row in (1, 3):
            with self.subTest(end_row=wrong_end_row):
                class WrongHeightSession:
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
                                        },
                                        "tables": [
                                            {
                                                "tableId": "table-mappings-v1",
                                                "range": {
                                                    "sheetId": 42,
                                                    "startRowIndex": 0,
                                                    "endRowIndex": wrong_end_row,
                                                    "startColumnIndex": 0,
                                                    "endColumnIndex": 33,
                                                },
                                            }
                                        ],
                                    }
                                ]
                            },
                        )

                    def post(self, *_args, **_kwargs):
                        self.posts += 1
                        raise AssertionError("Geometry failure must happen pre-append")

                session = WrongHeightSession()
                with self.assertRaisesRegex(sync.SyncError, "does not uniquely cover"):
                    sync.append_google_sheet_rows(
                        session, "a" * 30, "Mappings", current, [new]
                    )
                self.assertEqual(session.posts, 0)

        alert = {column: "" for column in sync.ALERT_COLUMNS}
        alert.update(
            {
                "server_id": "server_1",
                "stream_id": "7",
                "alert_type": "POSSIBLE_STREAM_ID_REUSE",
                "status": "OPEN",
            }
        )

        class WrongAlertHeightSession:
            def __init__(self):
                self.posts = 0

            def get(self, _url, **_kwargs):
                return FakeResponse(
                    200,
                    {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 43,
                                    "title": "Sync Alerts",
                                },
                                "tables": [
                                    {
                                        "tableId": "table-alerts-v1",
                                        "range": {
                                            "sheetId": 43,
                                            "startRowIndex": 0,
                                            "endRowIndex": 2,
                                            "startColumnIndex": 0,
                                            "endColumnIndex": 11,
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                )

            def post(self, *_args, **_kwargs):
                self.posts += 1
                raise AssertionError("Alert table geometry must fail pre-append")

        alert_session = WrongAlertHeightSession()
        with self.assertRaisesRegex(sync.SyncError, "does not uniquely cover"):
            sync.append_sync_alert_rows(
                alert_session, "a" * 30, "Sync Alerts", [], [alert]
            )
        self.assertEqual(alert_session.posts, 0)

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
                                "properties": {"sheetId": 42, "title": "Mappings"},
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
                return FakeResponse(500, {"error": {"message": "redacted"}})

        with self.assertRaises(sync.SheetWriteError) as caught:
            sync.append_google_sheet_rows(
                FailingModernSession(), "a" * 30, "Mappings", current, [new]
            )
        self.assertEqual(caught.exception.appended_count, 0)

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
                                "properties": {"sheetId": 42, "title": "Mappings"},
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
        with self.assertRaisesRegex(sync.SheetWriteError, "invalid.*response") as caught:
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


if __name__ == "__main__":
    unittest.main()
