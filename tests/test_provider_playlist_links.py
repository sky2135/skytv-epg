from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_channel_inventory as sync


def api_inventory(server: str, *rows: dict) -> sync.PanelInventory:
    normalized = [sync.normalize_stream_row(row, allow_generic_id=False) for row in rows]
    return sync.PanelInventory(server, server, [], normalized, "player_api", [])


def playlist_inventory(server: str, *rows: tuple[str, str, str]) -> sync.PanelInventory:
    text = "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 tvg-id="{epg_id}" group-title="SPORTS",{name}\n'
        f'https://panel.example/live/user123/pass123/{stream_id}.ts\n'
        for stream_id, name, epg_id in rows
    )
    categories, channels = sync.parse_xtream_m3u(text, username="user123", password="pass123")
    return sync.PanelInventory(server, server, categories, channels, "authenticated_m3u", [])


class ProviderPlaylistLinksTests(unittest.TestCase):
    def enrich(self, provider, m3u=None, *, native_servers=()):
        config = sync.ServerConfig(provider.server_id, provider.server_id, "https://panel.example", "user123", "pass123")
        with mock.patch.object(sync, "fetch_m3u_inventory", return_value=m3u) as fetch:
            inventories, stats = sync.enrich_review_inventories_from_m3u(
                object(), [provider], [config], selected_servers=native_servers,
                playlist_link_servers={provider.server_id}, allow_insecure_http=False,
            )
        return inventories, stats, fetch.call_count

    def test_api_exact_id_reuses_existing_authenticated_data_without_m3u(self):
        provider = api_inventory("server_1", {"stream_id": 101, "name": "UK || SKY SPORTS CRICKET", "epg_channel_id": "Sky  Cricket.uk"})
        inventories, _, calls = self.enrich(provider)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(calls, 0)
        self.assertEqual(row["provider_playlist_epg_id_alias"], "Sky  Cricket.uk")
        self.assertEqual(json.loads(row["provider_playlist_name_aliases"]), ["UK || SKY SPORTS CRICKET"])
        self.assertEqual(row["provider_playlist_link_status"], "verified")
        self.assertEqual(row["provider_playlist_identity_source"], "player_api")
        self.assertNotIn(sync._PROVIDER_PLAYLIST_HINT, provider.channels[0])

    def test_server1_recovers_playlist_alias_without_enabling_native_id(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Cricket"})
        playlist = playlist_inventory("server_1", ("101", "Sky Sports Cricket", "SkySportsCricket.uk"))
        inventories, stats, calls = self.enrich(provider, playlist)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(calls, 1)
        self.assertEqual(row["provider_playlist_epg_id_alias"], "SkySportsCricket.uk")
        self.assertEqual(inventories[0].channels[0]["epg_channel_id"], "")
        self.assertEqual(stats["native_review_m3u_servers_attempted"], 0)
        self.assertEqual(stats["native_review_m3u_ids_recovered"], 0)

    def test_native_and_playlist_enrichment_share_one_m3u_fetch(self):
        provider = api_inventory("server_3", {"stream_id": "101", "name": "Sky Sports Cricket"})
        playlist = playlist_inventory("server_3", ("101", "Sky Sports Cricket", "SkySportsCricket.uk"))
        inventories, stats, calls = self.enrich(provider, playlist, native_servers={"server_3"})
        self.assertEqual(calls, 1)
        self.assertEqual(stats["native_review_m3u_servers_attempted"], 1)
        self.assertEqual(stats["provider_playlist_m3u_servers_attempted"], 1)
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_epg_id_alias"], "SkySportsCricket.uk")

    def test_shared_api_ids_are_omitted_across_streams_but_not_servers(self):
        one = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports HD", "epg_channel_id": "shared"}, {"stream_id": "102", "name": "Sky Sports FHD", "epg_channel_id": "shared"})
        two = api_inventory("server_2", {"stream_id": "101", "name": "Sky Sports", "epg_channel_id": "shared"})
        first, _, _ = self.enrich(one)
        second, _, _ = self.enrich(two)
        rows = sync.inventory_rows(first + second)
        self.assertEqual([r["provider_playlist_epg_id_alias"] for r in rows], ["", "", "shared"])
        self.assertEqual([r["provider_playlist_link_status"] for r in rows], ["shared_id", "shared_id", "verified"])
        self.assertEqual([r["provider_playlist_shared_epg_id"] for r in rows], ["shared", "shared", ""])
        self.assertEqual([r["provider_playlist_shared_stream_count"] for r in rows], ["2", "2", ""])

    def test_shared_id_on_unexportable_stream_is_also_ambiguous(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports HD", "epg_channel_id": "shared"}, {"stream_id": "opaque", "name": "Different Channel", "epg_channel_id": "shared"})
        inventories, _, _ = self.enrich(provider)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")
        self.assertEqual(row["provider_playlist_link_status"], "shared_id")
        self.assertEqual(row["provider_playlist_shared_epg_id"], "shared")
        self.assertEqual(row["provider_playlist_shared_stream_count"], "2")

    def test_shared_m3u_id_including_unmatched_row_is_omitted(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Cricket"})
        playlist = playlist_inventory("server_1", ("101", "Sky Sports Cricket", "shared"), ("999", "Different Channel", "shared"))
        inventories, _, _ = self.enrich(provider, playlist)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")
        self.assertEqual(row["provider_playlist_link_status"], "shared_id")
        self.assertEqual(row["provider_playlist_shared_epg_id"], "shared")
        self.assertEqual(row["provider_playlist_shared_stream_count"], "2")

    def test_shared_members_union_api_and_unmatched_playlist_rows(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Cricket"}, {"stream_id": "102", "name": "Other Channel", "epg_channel_id": "shared"})
        playlist = playlist_inventory("server_1", ("101", "Sky Sports Cricket", "shared"), ("999", "Another Channel", "shared"))
        inventories, _, _ = self.enrich(provider, playlist)
        rows = sync.inventory_rows(inventories)
        self.assertEqual(rows[0]["provider_playlist_epg_id_alias"], "")
        self.assertEqual(rows[0]["provider_playlist_shared_stream_count"], "3")
        self.assertEqual(rows[1]["provider_playlist_shared_stream_count"], "3")

    def test_conflicting_fresh_provider_ids_fail_closed(self):
        provider = api_inventory("server_3", {"stream_id": "101", "name": "Sky Sports Cricket", "epg_channel_id": "api.id"})
        playlist = playlist_inventory("server_3", ("101", "Sky Sports Cricket", "m3u.id"))
        inventories, _, _ = self.enrich(provider, playlist, native_servers={"server_3"})
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_link_status"], "conflicting_id")
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")
        self.assertEqual(row["provider_playlist_name_aliases"], "")

    def test_same_stream_with_another_channel_name_fails_closed(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus"})
        playlist = playlist_inventory("server_1", ("101", "4seven", "4seven.uk"))
        inventories, _, _ = self.enrich(provider, playlist)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_link_status"], "name_mismatch")
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")
        self.assertEqual(row["provider_playlist_name_aliases"], "")

    def test_timeshift_plus_is_not_folded_into_numbered_channel(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "ITV +1"})
        playlist = playlist_inventory("server_1", ("101", "ITV1", "itv1.uk"))
        inventories, _, _ = self.enrich(provider, playlist)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_link_status"], "name_mismatch")
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")

    def test_wrong_server_m3u_never_links_same_numeric_stream(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus"})
        playlist = playlist_inventory("server_2", ("101", "Sky Sports Plus", "4seven.uk"))
        inventories, _, _ = self.enrich(provider, playlist)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_link_status"], "name_only")
        self.assertEqual(row["provider_playlist_epg_id_alias"], "")

    def test_cleaned_or_conflicting_raw_ids_are_never_exported(self):
        for value in [" id ", "a\tb", "a\x01b", "x" * 301]:
            with self.subTest(value=repr(value)):
                provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus", "epg_channel_id": value})
                inventories, _, _ = self.enrich(provider)
                row = sync.inventory_rows(inventories)[0]
                self.assertEqual(row["provider_playlist_link_status"], "invalid_id")
                self.assertEqual(row["provider_playlist_epg_id_alias"], "")
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus", "epg_channel_id": "one", "tvg-id": "two"})
        inventories, _, _ = self.enrich(provider)
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_link_status"], "invalid_id")

    def test_unsafe_attributes_do_not_poison_other_channel_links(self):
        for invalid in ["https://provider.invalid/guide", "username=private", "password=private", "id\u200evalue", "id\x01value"]:
            with self.subTest(invalid=repr(invalid)):
                provider = api_inventory("server_1", {"stream_id": "101", "name": "Good Channel", "epg_channel_id": "good.id"}, {"stream_id": "102", "name": "Bad ID", "epg_channel_id": invalid}, {"stream_id": "103", "name": invalid, "epg_channel_id": "id.for.bad.name"})
                inventories, _, _ = self.enrich(provider)
                rows = sync.inventory_rows(inventories)
                self.assertEqual(rows[0]["provider_playlist_epg_id_alias"], "good.id")
                self.assertEqual(rows[1]["provider_playlist_link_status"], "invalid_id")
                self.assertEqual(rows[1]["provider_playlist_epg_id_alias"], "")
                self.assertEqual(rows[2]["provider_playlist_link_status"], "unavailable")
                self.assertEqual(rows[2]["provider_playlist_name_aliases"], "")

    def test_literal_arrows_and_angle_brackets_are_safe_xml_text(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "UK < Sports > →", "epg_channel_id": "sports<uk>"})
        inventories, _, _ = self.enrich(provider)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(row["provider_playlist_epg_id_alias"], "sports<uk>")
        self.assertEqual(json.loads(row["provider_playlist_name_aliases"]), ["UK < Sports > →"])

    def test_missing_m3u_keeps_only_safe_api_name(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus"})
        inventories, _, calls = self.enrich(provider)
        row = sync.inventory_rows(inventories)[0]
        self.assertEqual(calls, 1)
        self.assertEqual(row["provider_playlist_link_status"], "name_only")
        self.assertEqual(json.loads(row["provider_playlist_name_aliases"]), ["Sky Sports Plus"])

    def test_direct_m3u_inventory_needs_no_second_fetch(self):
        playlist = playlist_inventory("server_1", ("101", "Sky Sports Plus", "SkyPlus.uk"))
        inventories, _, calls = self.enrich(playlist)
        self.assertEqual(calls, 0)
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_epg_id_alias"], "SkyPlus.uk")

    def test_stale_or_injected_proofs_are_not_carried_forward(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus"})
        provider.channels[0][sync._PROVIDER_PLAYLIST_HINT] = {"epg_id": "4seven.uk", "status": "verified"}
        inventories, _, _ = self.enrich(provider)
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_link_status"], "name_only")
        inventories[0].channels[0]["name"] = "Different Channel"
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_link_status"], "unavailable")

    def test_unrequested_inventory_has_no_links_and_no_new_fetch(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus", "epg_channel_id": "provider.id"})
        with mock.patch.object(sync, "fetch_m3u_inventory") as fetch:
            inventories, _ = sync.enrich_review_inventories_from_m3u(object(), [provider], [], selected_servers=(), allow_insecure_http=False)
        fetch.assert_not_called()
        self.assertEqual(sync.inventory_rows(inventories)[0]["provider_playlist_link_status"], "unavailable")

    def test_optional_attribute_probe_has_bounded_attempts_and_timeout(self):
        config = sync.ServerConfig("server_1", "server_1", "https://panel.example", "user123", "pass123")
        diagnostics = []
        with mock.patch.object(sync, "safe_credentialed_get", return_value=(404, b"")) as fetch:
            result = sync.fetch_m3u_inventory(object(), config, ["https://panel.example"], diagnostics, allow_insecure_http=False, maximum_attempts=2, request_timeout=(10, 60))
        self.assertIsNone(result)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == (10, 60) for call in fetch.call_args_list))

    def test_playlist_only_enrichment_uses_bounded_transport(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus"})
        config = sync.ServerConfig("server_1", "server_1", "https://panel.example", "user123", "pass123")
        with mock.patch.object(sync, "fetch_m3u_inventory", return_value=None) as fetch:
            sync.enrich_review_inventories_from_m3u(object(), [provider], [config], selected_servers=(), playlist_link_servers={"server_1"}, allow_insecure_http=False)
        self.assertEqual(fetch.call_args.kwargs["maximum_attempts"], 2)
        self.assertEqual(fetch.call_args.kwargs["request_timeout"], (10, 60))

    def test_csv_headers_include_private_playlist_fields(self):
        provider = api_inventory("server_1", {"stream_id": "101", "name": "Sky Sports Plus", "epg_channel_id": "provider.id"})
        inventories, _, _ = self.enrich(provider)
        with tempfile.TemporaryDirectory() as folder:
            sync.write_reports(Path(folder), inventories=inventories, new_rows=[], changed_rows=[], missing_rows=[], summary={})
            with (Path(folder) / "inventory.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["provider_playlist_epg_id_alias"], "provider.id")
        self.assertTrue(sync.parse_args(["--mode", "refresh", "--provider-playlist-links"]).provider_playlist_links)


if __name__ == "__main__":
    unittest.main()
