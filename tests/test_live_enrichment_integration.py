import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_epg_streaming as builder
if __package__:
    from .test_build_epg_streaming import _mapping_bytes, _mapping_row
else:
    from test_build_epg_streaming import _mapping_bytes, _mapping_row


class LogoPublicationEvidenceTests(unittest.TestCase):
    def test_shared_url_keeps_each_streams_actual_identity_evidence(self):
        raw = [_mapping_row(server_id="server_1", stream_id=str(n), channel_name=f"Alpha {n}", category_name="News", epg_id=f"Alpha{n}.us") for n in (1, 2)]
        rows = builder.parse_mapping_csv(_mapping_bytes(raw), {"server_1"})
        url = "https://upload.wikimedia.org/example.png"
        overrides = [builder.IconOverride("server_1", "1", "", "", url, 100, "BRAND_LOGO: reviewed"), builder.IconOverride("server_1", "2", "", "", url, 100, "REGISTRY_LOGO: exact")]
        self.assertEqual(builder.configured_icon(overrides, rows[0], with_notes=True), (url, "BRAND_LOGO: reviewed"))
        self.assertEqual(builder.configured_icon(overrides, rows[1], with_notes=True), (url, "REGISTRY_LOGO: exact"))

    def test_known_failed_image_is_not_published_as_a_channel_logo(self):
        raw = _mapping_row(server_id="server_1", stream_id="1", channel_name="Alpha", category_name="News", epg_id="Alpha.us")
        row = builder.parse_mapping_csv(_mapping_bytes([raw]), {"server_1"})[0]
        row.logo_url = "https://images.example/channel.png"
        overrides = builder.empty_icon_overrides()
        with tempfile.TemporaryDirectory() as directory:
            connection = builder.create_database(Path(directory) / "guide.sqlite3")
            try:
                overrides.verification = {row.logo_url: {"status": "invalid_image"}}
                self.assertEqual(builder.resolve_row_icon(row, connection, overrides), "")
                overrides.verification[row.logo_url]["status"] = "network_error"
                self.assertEqual(builder.resolve_row_icon(row, connection, overrides), row.logo_url)
            finally:
                connection.close()

if __name__ == "__main__": unittest.main()
