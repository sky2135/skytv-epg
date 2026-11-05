import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts import private_panel_cache as cache
from scripts import private_matcher_cache


class PrivatePanelCacheTests(unittest.TestCase):
    credentials = ("https://private.example", "private-user", "private-password")
    captured = 1793880000
    content = b'<tv><channel id="exact.Native"/><programme channel="exact.Native"/></tv>'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "download.xmltv"
        self.source.write_bytes(self.content)
        self.directory = self.root / "private"
        self.environment = patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "private-cache-test-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def save(self, **kwargs):
        return cache.save_source("server_3", self.source, self.directory,
                                 credentials=self.credentials, captured_at=kwargs.get("captured_at", self.captured))

    def load(self, **kwargs):
        return cache.load_source("server_3", self.directory, credentials=kwargs.get("credentials", self.credentials),
                                 now_epoch=kwargs.get("now_epoch", self.captured + 60),
                                 max_age_seconds=kwargs.get("max_age_seconds", cache.MAX_AGE_SECONDS))

    def encrypted_archive(self, members):
        body = io.BytesIO()
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Duplicate name:.*", category=UserWarning)
            with ZipFile(body, "w", compression=ZIP_STORED) as archive:
                for name, content in members:
                    archive.writestr(name, content)
        nonce = b"a" * 12
        return cache.MAGIC + nonce + AESGCM(cache.encryption_key()).encrypt(nonce, body.getvalue(), cache.MAGIC)

    def test_roundtrip_preserves_capture_and_private_binding(self):
        self.assertTrue(self.save())
        loaded_path, provenance = self.load()
        self.assertEqual(loaded_path.read_bytes(), self.content)
        self.assertEqual(provenance["cacheCapturedAt"], self.captured)
        self.assertEqual(provenance["cacheAgeSeconds"], 60)
        self.assertTrue(provenance["cacheReused"])
        metadata = (self.directory / "server_3.json").read_bytes()
        for secret in self.credentials:
            self.assertNotIn(secret.encode(), metadata)
            self.assertNotIn(secret, repr(provenance))
        encrypted = self.root / "cache.enc"
        self.assertTrue(cache.seal(self.directory, encrypted))
        self.assertNotIn(self.content, encrypted.read_bytes())
        restored = self.root / "restored"
        self.assertTrue(cache.restore(encrypted, restored))
        self.assertEqual((restored / "server_3.json").read_bytes(), metadata)
        restored_result = cache.load_source("server_3", restored, credentials=self.credentials, now_epoch=self.captured + 3600)
        self.assertEqual(restored_result[1]["cacheCapturedAt"], self.captured)
        self.assertEqual(restored_result[1]["cacheAgeSeconds"], 3600)

    def test_expiry_future_dates_and_account_changes_reject(self):
        self.assertTrue(self.save())
        self.assertIsNone(self.load(now_epoch=self.captured + cache.MAX_AGE_SECONDS + 1))
        self.assertIsNone(self.load(now_epoch=self.captured - 1))
        self.assertIsNone(self.load(max_age_seconds=cache.MAX_AGE_SECONDS + 1))
        self.assertIsNone(self.load(max_age_seconds=True))
        for index in range(3):
            changed = list(self.credentials)
            changed[index] += "-changed"
            self.assertIsNone(self.load(credentials=tuple(changed)))
        self.assertIsNone(cache.load_source("server_2", self.directory,
                                           credentials=self.credentials, now_epoch=self.captured + 60))
        self.assertIsNone(cache.load_source("server_1", self.directory,
                                           credentials=self.credentials, now_epoch=self.captured + 60))

    def test_parser_snapshot_keeps_checked_bytes_if_original_cache_changes(self):
        self.assertTrue(self.save())
        snapshot = self.root / "builder-work" / "saved-guide.xmltv"
        path, provenance = cache.load_source("server_3", self.directory, credentials=self.credentials,
                                             now_epoch=self.captured + 60, snapshot_path=snapshot)
        self.assertEqual(path, snapshot)
        (self.directory / "server_3.xmltv").write_bytes(b"changed after cache lookup")
        self.assertEqual(path.read_bytes(), self.content)
        self.assertEqual(provenance["bytes"], len(self.content))
        self.assertIsNone(cache.load_source("server_3", self.directory, credentials=self.credentials,
                                            now_epoch=self.captured + 60))
        self.assertTrue(self.save())
        self.assertIsNone(cache.load_source("server_3", self.directory, credentials=self.credentials,
                                            now_epoch=self.captured + 60,
                                            snapshot_path=self.directory / "server_3.xmltv"))

    def test_bad_bytes_and_invalid_metadata_are_never_returned(self):
        self.assertTrue(self.save())
        source = self.directory / "server_3.xmltv"
        source.write_bytes(b"tampered")
        self.assertIsNone(self.load())
        self.assertFalse(cache.seal(self.directory, self.root / "bad.enc"))
        self.assertTrue(self.save())
        metadata = self.directory / "server_3.json"
        value = json.loads(metadata.read_text())
        for field, wrong_value in (("serverId", "server_2"), ("capturedAt", True),
                                   ("originFingerprint", "not-a-digest"), ("bytes", 0),
                                   ("sha256", "f" * 64)):
            broken = dict(value)
            broken[field] = wrong_value
            metadata.write_text(json.dumps(broken))
            self.assertIsNone(self.load(), field)
        metadata.write_text(json.dumps({**value, "unexpected": "private-origin"}))
        self.assertIsNone(self.load())

    def test_empty_or_oversized_new_input_does_not_overwrite_valid_cache(self):
        self.assertTrue(self.save())
        original = self.load()[0].read_bytes()
        self.source.write_bytes(b"")
        self.assertFalse(self.save())
        self.assertEqual(self.load()[0].read_bytes(), original)
        self.source.write_bytes(b"too-large")
        with patch.object(cache, "MAX_SOURCE_BYTES", 4):
            self.assertFalse(self.save())
        self.assertEqual(self.load()[0].read_bytes(), original)

    def test_wrong_encryption_key_tamper_and_no_key_do_not_restore_plaintext(self):
        self.assertTrue(self.save())
        encrypted = self.root / "cache.enc"
        self.assertTrue(cache.seal(self.directory, encrypted))
        with patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "different-key"}):
            self.assertFalse(cache.restore(encrypted, self.root / "wrong"))
        self.assertFalse((self.root / "wrong").exists())
        damaged = bytearray(encrypted.read_bytes())
        damaged[-1] ^= 1
        encrypted.write_bytes(damaged)
        self.assertFalse(cache.restore(encrypted, self.root / "damaged"))
        self.assertFalse((self.root / "damaged").exists())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cache.seal(self.directory, self.root / "unsealed.enc"))
            self.assertFalse(cache.restore(encrypted, self.root / "missing-key"))
        self.assertFalse((self.root / "unsealed.enc").exists())

    def test_encryption_key_is_domain_separated(self):
        self.assertNotEqual(cache.encryption_key(), private_matcher_cache.encryption_key())

    def test_archive_rejects_traversal_duplicates_symlinks_and_incomplete_pairs(self):
        encrypted = self.root / "crafted.enc"
        bad_symlink = ZipInfo("server_3.xmltv")
        bad_symlink.create_system = 3
        bad_symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for ordinal, members in enumerate((
            [("../outside", b"bad")],
            [("server_3.cooldown.json", b"{}"), ("server_3.cooldown.json", b"{}")],
            [(bad_symlink, b"target")],
            [("server_3.xmltv", self.content)],
        )):
            with self.subTest(ordinal=ordinal):
                encrypted.write_bytes(self.encrypted_archive(members))
                destination = self.root / ("bad-" + str(ordinal))
                self.assertFalse(cache.restore(encrypted, destination))
                self.assertFalse(destination.exists())
        self.assertFalse((self.root.parent / "outside").exists())

    def test_archive_rejects_compression_bombs_and_oversized_source(self):
        body = io.BytesIO()
        with ZipFile(body, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("server_3.xmltv", b"x" * 100)
        nonce = b"c" * 12
        encrypted = self.root / "compressed.enc"
        encrypted.write_bytes(cache.MAGIC + nonce + AESGCM(cache.encryption_key()).encrypt(nonce, body.getvalue(), cache.MAGIC))
        self.assertFalse(cache.restore(encrypted, self.root / "decompressed"))
        self.assertTrue(self.save())
        self.assertTrue(cache.seal(self.directory, encrypted))
        with patch.object(cache, "MAX_SOURCE_BYTES", 4):
            self.assertFalse(cache.restore(encrypted, self.root / "oversized"))

    def test_restored_destination_symlink_is_not_followed(self):
        self.assertTrue(self.save())
        encrypted = self.root / "cache.enc"
        self.assertTrue(cache.seal(self.directory, encrypted))
        destination = self.root / "restore"
        destination.mkdir()
        outside = self.root / "outside.xml"
        outside.write_bytes(b"preserve")
        try:
            (destination / "server_3.xmltv").symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit creating symlinks")
        self.assertFalse(cache.restore(encrypted, destination))
        self.assertEqual(outside.read_bytes(), b"preserve")
        self.assertIsNone(cache.load_source("server_3", destination, credentials=self.credentials, now_epoch=self.captured))

    def test_cooldown_survives_sealing_and_is_account_bound(self):
        next_attempt = self.captured + 5 * 86400
        self.assertTrue(cache.save_cooldown("server_3", self.directory, credentials=self.credentials,
                                            next_attempt_at=next_attempt))
        self.assertEqual(cache.load_cooldown("server_3", self.directory, credentials=self.credentials,
                                             now_epoch=self.captured), next_attempt)
        self.assertTrue(cache.save_cooldown("server_3", self.directory, credentials=self.credentials,
                                            next_attempt_at=self.captured + 1))
        self.assertEqual(cache.load_cooldown("server_3", self.directory, credentials=self.credentials,
                                             now_epoch=self.captured), next_attempt)
        self.assertIsNone(cache.load_cooldown("server_3", self.directory, credentials=self.credentials,
                                              now_epoch=next_attempt))
        self.assertIsNone(cache.load_cooldown("server_3", self.directory, credentials=("new", "user", "secret"),
                                              now_epoch=self.captured))
        encrypted = self.root / "cooldown.enc"
        self.assertTrue(cache.seal(self.directory, encrypted))
        restored = self.root / "restored-cooldown"
        self.assertTrue(cache.restore(encrypted, restored))
        self.assertEqual(cache.load_cooldown("server_3", restored, credentials=self.credentials,
                                             now_epoch=self.captured + 86400), next_attempt)
        self.assertTrue(self.save())
        self.assertIsNone(cache.load_cooldown("server_3", self.directory, credentials=self.credentials,
                                              now_epoch=self.captured))

    def test_unknown_entries_or_bad_cooldown_are_not_sealed(self):
        self.assertTrue(self.save())
        self.assertFalse(cache.save_cooldown("server_3", self.directory, credentials=self.credentials, next_attempt_at=True))
        extra = self.directory / "unexpected.json"
        extra.write_text("{}")
        self.assertFalse(cache.seal(self.directory, self.root / "unexpected.enc"))
        extra.unlink()
        (self.directory / "server_3.cooldown.json").write_text("{}")
        self.assertFalse(cache.seal(self.directory, self.root / "cooldown.enc"))

    def test_very_long_service_cooldown_is_preserved_without_shortening(self):
        far_future = self.captured + 10 ** 200
        self.assertTrue(cache.save_cooldown("server_3", self.directory, credentials=self.credentials,
                                            next_attempt_at=far_future))
        self.assertTrue(cache.save_cooldown("server_3", self.directory, credentials=self.credentials,
                                            next_attempt_at=self.captured + 60))
        self.assertEqual(cache.load_cooldown("server_3", self.directory, credentials=self.credentials,
                                             now_epoch=self.captured), far_future)
        encrypted = self.root / "long-cooldown.enc"
        self.assertTrue(cache.seal(self.directory, encrypted))
        restored = self.root / "long-cooldown"
        self.assertTrue(cache.restore(encrypted, restored))
        self.assertEqual(cache.load_cooldown("server_3", restored, credentials=self.credentials,
                                             now_epoch=self.captured), far_future)


if __name__ == "__main__":
    unittest.main()
