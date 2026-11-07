from __future__ import annotations

import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_epg_streaming as builder
import provider_guide_links as links
from tests.test_build_epg_streaming import _mapping_row, _mapping_bytes

NOW = 1_800_000_000


def mapping(name="UK || SKY SPORTS PLUS", sid="101", **overrides):
    raw = _mapping_row(server_id="server_1", stream_id=sid, channel_name=name,
                       category_name="SPORTS | SKY UK (4K)", epg_id="")
    raw.update(enabled="FALSE", action="REVIEW")
    raw.update(overrides)
    parsed = builder.parse_mapping_csv(_mapping_bytes([raw]), {raw["server_id"]}, require_enabled_servers=False)[0]
    return replace(parsed, reason=str(overrides.get("reason", "")))


def inventory(rows):
    values = [{"server_id": row.server_id, "stream_id": row.stream_id,
               "channel_name": row.channel_name, "category_name": row.category_name,
               "provider_playlist_epg_id_alias": epg_id,
               "provider_playlist_name_aliases": json.dumps([row.channel_name]),
               "provider_playlist_link_status": "verified" if epg_id else "name_only",
               "provider_playlist_identity_source": "player_api"} for row, epg_id in rows]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=values[0])
    writer.writeheader()
    writer.writerows(values)
    return out.getvalue().encode()


class GuideConnectionIntegrationTests(unittest.TestCase):
    def test_generic_identity_keeps_timeshift_and_channel_boundaries(self):
        for left, right in [("ITV +1", "ITV1"), ("BBC One +1", "BBC One 1"),
                            ("UK || SKY SPORTS +", "UK || SKY SPORTS")]:
            self.assertFalse(links.same_channel_name(left, right))
        self.assertTrue(links.same_channel_name("IN:  Aaj_Tak", "IN: Aaj_Tak"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = builder.create_database(Path(self.temp.name) / "test.sqlite")
        self.addCleanup(self.db.close)

    def programme(self, channel, start=NOW - 60, stop=NOW + 1800, title="Verified sport"):
        self.db.execute("INSERT OR IGNORE INTO channels VALUES(?,?,?,?,?)", ("epgshare01", channel, channel, channel, ""))
        self.db.execute("INSERT OR IGNORE INTO programmes VALUES(?,?,?,?,?,?,?,?,?,?)", ("epgshare01", channel, start, stop, title, "", "", "[]", 0, "{}"))
        if start == NOW - 60 and stop == NOW + 1800:
            self.db.execute("INSERT OR IGNORE INTO programmes VALUES(?,?,?,?,?,?,?,?,?,?)", ("epgshare01", channel, stop, NOW + 7 * 3600, "Next real sporting event", "", "", "[]", 0, "{}"))

    def overlay(self, rows, content=None):
        return links.apply_provider_playlist_aliases(rows, content or inventory([(r, "Provider  Exact.uk") for r in rows]),
            {r.server_id for r in rows}, quarantine_reason=builder.EFFECTIVE_QUARANTINE_REASON)

    def test_review_plus_gets_sport_not_4seven_and_exact_playlist_id(self):
        self.programme("SkySp+HD.uk")
        self.programme("4seven.uk", title="Gogglebox")
        rows, proof, _ = self.overlay([mapping()])
        rows, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertTrue(rows[0].runtime_eligible)
        self.assertEqual(rows[0].epg_id, "SkySp+HD.uk")
        stats = builder.load_schedule_stats(self.db, NOW)
        self.assertEqual(stats[("epgshare01", "SkySp+HD.uk")].current_rows, 1)
        entries, streams, _ = builder.build_xml_entries(rows=rows, connection=self.db, schedule_stats=stats, icon_overrides=builder.empty_icon_overrides())
        self.assertEqual(entries["Provider  Exact.uk"].channel_key, "SkySp+HD.uk")
        self.assertIn("101", streams)
        self.assertEqual(audit[0]["outcome"], "connected")

    def test_expired_or_far_future_schedule_cannot_activate_review(self):
        self.programme("SkySp+HD.uk", NOW - 3600, NOW)
        self.programme("SkySp+.uk", NOW + 86400, NOW + 90000)
        rows, proof, _ = self.overlay([mapping()])
        rows, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertFalse(rows[0].runtime_eligible)
        self.assertEqual(audit[0]["outcome"], "no_current_or_upcoming_schedule")

    def test_no_authenticated_identity_no_activation(self):
        row = mapping()
        self.programme("SkySp+HD.uk")
        rows, audit = builder.apply_reviewed_guide_connections([row], self.db, now_epoch=NOW, provider_links={})
        self.assertEqual(rows, [row])
        self.assertEqual(audit, [])

    def test_source_placeholder_rows_cannot_activate_a_real_guide(self):
        self.programme("SkySp+HD.uk", NOW - 60, NOW + 3 * 3600, "No information")
        self.programme("SkySp+HD.uk", NOW + 3 * 3600, NOW + 12 * 3600, "No information")
        rows, proof, _ = self.overlay([mapping()])
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertFalse(rows[0].runtime_eligible)

    def test_exclusions_and_both_quarantines_stay_excluded(self):
        self.programme("SkySp+HD.uk")
        for row in [mapping(action="IGNORE"), mapping(action="SKIP"),
                    mapping(reason=builder.EFFECTIVE_QUARANTINE_REASON), mapping(reason=builder.LEGACY_EFFECTIVE_QUARANTINE_REASON)]:
            rows, proof, _ = self.overlay([row])
            result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
            self.assertFalse(result[0].runtime_eligible)
            self.assertEqual(audit, [])

    def test_changed_identity_and_shared_ids_do_not_get_aliases(self):
        row = mapping()
        changed = replace(row, channel_name="UK || 4seven")
        rows, proof, _ = self.overlay([row], inventory([(changed, "4seven.uk")]))
        self.assertFalse(proof[("server_1", "101")]["identity_verified"])
        self.assertEqual(rows[0].provider_epg_id_aliases, ())
        output, proof, _ = self.overlay([row, mapping(sid="102")])
        self.assertTrue(all(not r.provider_epg_id_aliases for r in output))
        self.assertTrue(all(p["status"] == "shared_id" for p in proof.values()))

    def test_unsafe_attribute_is_flagged_without_losing_healthy_channels(self):
        bad, good = mapping(), mapping(sid="102")
        rows, proof, _ = self.overlay([bad, good], inventory([
            (bad, "https://provider.invalid/guide"), (good, "GoodProviderID"),
        ]))
        self.assertEqual(rows[0].provider_epg_id_aliases, ())
        self.assertEqual(proof[("server_1", "101")]["status"], "invalid_id")
        self.assertEqual(rows[1].provider_epg_id_aliases, ("GoodProviderID",))

    def test_shared_id_connects_only_when_every_member_has_same_real_schedule(self):
        hd = mapping(name="UK || SKY SPORTS PLUS HD", sid="101")
        fhd = mapping(name="UK || SKY SPORTS PLUS FHD", sid="102")
        content = inventory([(hd, ""), (fhd, "")]).decode()
        parsed = list(csv.DictReader(io.StringIO(content)))
        for record in parsed:
            record.update(provider_playlist_link_status="shared_id",
                          provider_playlist_shared_epg_id="SharedPlus.uk",
                          provider_playlist_shared_stream_count="2")
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=parsed[0])
        writer.writeheader()
        writer.writerows(parsed)
        rows, proof, _ = self.overlay([hd, fhd], out.getvalue().encode())
        self.programme("SkySp+HD.uk")
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        connected, count = links.apply_shared_playlist_aliases(rows, proof)
        self.assertEqual(count, 2)
        self.assertTrue(all(r.provider_epg_id_aliases == ("SharedPlus.uk",) for r in connected))
        proof[("server_1", "101")].update(status="shared_id", shared_stream_count=3)
        proof[("server_1", "102")].update(status="shared_id", shared_stream_count=3)
        _, count = links.apply_shared_playlist_aliases(rows, proof)
        self.assertEqual(count, 0)

    def test_bad_plus_provider_id_does_not_replace_4seven_canonical_guide(self):
        plus = mapping()
        four = mapping(name="4seven", sid="102", enabled="TRUE", action="APPROVED", epg_id="4seven.uk")
        self.programme("SkySp+HD.uk")
        self.programme("4seven.uk", title="Gogglebox")
        rows, proof, _ = self.overlay([plus, four], inventory([(plus, "4seven.uk"), (four, "FourSevenProvider")]))
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        stats = builder.load_schedule_stats(self.db, NOW)
        entries, _, conflicts = builder.build_xml_entries(rows=rows, connection=self.db,
            schedule_stats=stats, icon_overrides=builder.empty_icon_overrides())
        self.assertEqual(entries["4seven.uk"].channel_key, "4seven.uk")
        _, records = builder.tivimate_guide_health.build_guide_link_health(rows=rows, entries=entries,
            schedule_stats=stats, generated_at=NOW, provider_links=proof)
        self.assertEqual(records[0]["provider_epg_id_status"], "wrong_schedule")

    def test_server1_legacy_native_id_becomes_epgshare_only(self):
        row = mapping(enabled="TRUE", action="KEEP_PANEL", source="panel", epg_feed="PANEL", epg_id="wrong-native-id")
        self.programme("SkySp+HD.uk")
        rows, proof, _ = self.overlay([row])
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(rows[0].source_key, "epgshare01")
        self.assertEqual(rows[0].epg_id, "SkySp+HD.uk")
        self.assertFalse(rows[0].source_policy_blocked)

    def test_sony_numbered_feed_connects_and_fills_known_india_metadata(self):
        original = mapping(name="CRIC || SONY SPORTS 1 HD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=[], region="unknown", primary_language="unknown", languages=[]))
        self.programme("Sony.Ten.1.HD.in")
        rows, proof, _ = self.overlay([original], inventory([(original, "sonyten1.provider")]))
        rows, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        connected = rows[0]
        self.assertTrue(connected.runtime_eligible)
        self.assertEqual((connected.source_key, connected.epg_feed, connected.epg_id),
                         ("epgshare01", "IN1", "Sony.Ten.1.HD.in"))
        self.assertEqual(connected.metadata.countries, ["IN"])
        self.assertEqual((connected.metadata.primary_language, connected.metadata.languages), ("en", ["en"]))
        self.assertEqual(original.metadata.countries, [])
        self.assertEqual(original.metadata.primary_language, "unknown")
        self.assertEqual(audit[0]["outcome"], "connected")
        stats = builder.load_schedule_stats(self.db, NOW)
        entries, _, _ = builder.build_xml_entries(rows=rows, connection=self.db,
            schedule_stats=stats, icon_overrides=builder.empty_icon_overrides())
        self.assertEqual(entries["sonyten1.provider"].channel_key, "Sony.Ten.1.HD.in")

    def test_sony_hindi_feed_uses_current_in4_fallback_in_app_and_metadata(self):
        original = mapping(name="CRIC || SONY SPORTS 3 HD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=[], region="unknown", primary_language="unknown", languages=[]))
        self.programme("Sony.Ten.3.HD.Hindi.in", NOW - 3600, NOW)
        self.programme("SONY.SPORTS.TEN.3.HD.in")
        rows, proof, _ = self.overlay([original], inventory([(original, "sonyten3.provider")]))
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        connected = rows[0]
        self.assertTrue(connected.runtime_eligible)
        self.assertEqual((connected.epg_feed, connected.epg_id), ("IN4", "SONY.SPORTS.TEN.3.HD.in"))
        self.assertEqual((connected.metadata.primary_language, connected.metadata.languages), ("hi", ["hi"]))
        icons = builder.empty_icon_overrides()
        app_path = Path(self.temp.name) / "sony-app.json.gz"
        metadata_path = Path(self.temp.name) / "sony-metadata.json.gz"
        builder.write_app_epg(destination=app_path, rows=rows, connection=self.db,
            generated_at=NOW, window_start=NOW - 3600, window_end=NOW + 86400, icon_overrides=icons)
        builder.write_metadata_json(destination=metadata_path, rows=rows, connection=self.db,
            generated_at=NOW, mapping_sha256="a" * 64, icon_overrides=icons)
        app = json.loads(gzip.decompress(app_path.read_bytes()))
        metadata = json.loads(gzip.decompress(metadata_path.read_bytes()))["streamMetadata"][original.stream_id]
        self.assertEqual(app["streamToEpg"][original.stream_id], "IN4::SONY.SPORTS.TEN.3.HD.in")
        self.assertEqual(len(app["programmes"]["IN4::SONY.SPORTS.TEN.3.HD.in"]), 2)
        self.assertEqual(metadata["guideMode"], "epgshare")
        self.assertEqual(metadata["countries"], ["IN"])
        self.assertEqual((metadata["primaryLanguage"], metadata["languages"]), ("hi", ["hi"]))

    def test_hub_singapore_connection_does_not_infer_audio_from_english_titles(self):
        original = mapping(name="Hub Sports 4 FHD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=[], region="unknown", primary_language="unknown", languages=[]))
        self.programme("Hub.Sports.4.HD.sg", title="English programme title")
        rows, proof, _ = self.overlay([original], inventory([(original, "hubsports4.provider")]))
        rows, _ = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        connected = rows[0]
        self.assertTrue(connected.runtime_eligible)
        self.assertEqual((connected.epg_feed, connected.epg_id), ("SG1", "Hub.Sports.4.HD.sg"))
        self.assertEqual(connected.metadata.countries, ["SG"])
        self.assertEqual((connected.metadata.primary_language, connected.metadata.languages), ("unknown", []))

    def test_tamil_name_never_uses_an_available_telugu_schedule(self):
        original = mapping(name="CRIC || SONY SPORTS 4 TAMIL", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=[], primary_language="unknown", languages=[]))
        self.programme("Sony.Ten.4.HD.Telugu.in")
        self.programme("Sony.Ten.4.Telugu.in")
        overlaid, proof, _ = self.overlay([original])
        rows, audit = builder.apply_reviewed_guide_connections(overlaid, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(rows, overlaid)
        self.assertEqual(rows[0].metadata, original.metadata)
        self.assertFalse(rows[0].runtime_eligible)
        self.assertEqual(audit[0]["outcome"], "no_current_or_upcoming_schedule")

    def test_generic_sony4_espn_and_star_first_stay_unmapped_with_other_sports_available(self):
        rows = [mapping(name=name, sid=str(101 + index), category_name="SPORTS | CRICKET")
                for index, name in enumerate(["CRIC || SONY SPORTS 4 HD", "CRI: SONY ESPN HD", "CRIC || STAR SPORTS FIRST"])]
        rows = [replace(row, metadata=replace(row.metadata, countries=[])) for row in rows]
        for channel in ["Sony.Ten.4.HD.Tamil.in", "Sony.Ten.4.HD.Telugu.in", "Sony.Ten.5.HD.in",
                        "Star.Sports.Khel.in", "STAR.SPORTS.2.KANNADA.in"]:
            self.programme(channel)
        overlaid, proof, _ = self.overlay(rows)
        result, audit = builder.apply_reviewed_guide_connections(overlaid, self.db, now_epoch=NOW, provider_links=proof)
        self.assertTrue(all(not row.runtime_eligible for row in result))
        self.assertEqual([row.epg_id for row in result], [row.epg_id for row in rows])
        self.assertEqual(audit, [])

    def test_locked_conflicting_sony_language_is_preserved_without_activating_guide(self):
        original = mapping(name="CRIC || SONY SPORTS 4 TAMIL", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=["IN"], primary_language="en", languages=["en"], locked=True))
        self.programme("Sony.Ten.4.HD.Tamil.in")
        rows, proof, _ = self.overlay([original])
        result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(result[0].metadata, original.metadata)
        self.assertFalse(result[0].runtime_eligible)
        self.assertEqual(audit, [])

    def test_locked_conflicting_language_list_blocks_hindi_when_primary_is_und(self):
        original = mapping(name="CRIC || SONY SPORTS 3 HD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=["IN"], primary_language="und", languages=["en"], locked=True))
        self.programme("Sony.Ten.3.HD.Hindi.in")
        rows, proof, _ = self.overlay([original])
        result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(result, rows)
        self.assertEqual(result[0].metadata, original.metadata)
        self.assertFalse(result[0].runtime_eligible)
        self.assertEqual(audit, [])

    def test_locked_compatible_hindi_list_allows_guide_without_metadata_rewrite(self):
        original = mapping(name="CRIC || SONY SPORTS 3 HD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata,
            countries=["IN"], primary_language="und", languages=["hi"], locked=True))
        self.programme("Sony.Ten.3.HD.Hindi.in")
        rows, proof, _ = self.overlay([original])
        result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertTrue(result[0].runtime_eligible)
        self.assertEqual(result[0].epg_id, "Sony.Ten.3.HD.Hindi.in")
        self.assertEqual(result[0].metadata, original.metadata)
        self.assertEqual(audit[0]["outcome"], "connected")

    def test_explicit_foreign_country_prevents_new_market_connection(self):
        original = mapping(name="CRIC || SONY SPORTS 1 HD", category_name="SPORTS | CRICKET")
        original = replace(original, metadata=replace(original.metadata, countries=["GB"]))
        self.programme("Sony.Ten.1.HD.in")
        rows, proof, _ = self.overlay([original])
        result, audit = builder.apply_reviewed_guide_connections(rows, self.db, now_epoch=NOW, provider_links=proof)
        self.assertEqual(result, rows)
        self.assertFalse(result[0].runtime_eligible)
        self.assertEqual(audit, [])
