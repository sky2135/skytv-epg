import os
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.private_matcher_cache import encryption_key, restore, seal


class PrivateMatcherCacheTests(unittest.TestCase):
    def test_raw_and_base64_service_accounts_use_same_key(self):
        account = json.dumps({"private_key": "service-account-private-key"})
        with patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "", "GOOGLE_SERVICE_ACCOUNT_JSON": account}):
            plain_key = encryption_key()
        with patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "", "GOOGLE_SERVICE_ACCOUNT_JSON": base64.b64encode(account.encode()).decode()}):
            self.assertEqual(encryption_key(), plain_key)
        self.assertIsNotNone(plain_key)

    def test_authenticated_encryption_and_wrong_key_or_tampering_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "test-private-key"}):
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            content = b'{"private-channel":"private-alias"}'
            (source / "decisions.json").write_bytes(content)
            cipher = root / "cache.enc"
            self.assertTrue(seal(source, cipher))
            self.assertNotIn(b"private-channel", cipher.read_bytes())
            self.assertTrue(restore(cipher, root / "restored"))
            self.assertEqual((root / "restored/decisions.json").read_bytes(), content)
            with patch.dict(os.environ, {"SKYTV_MATCH_CACHE_KEY": "another-key"}):
                self.assertFalse(restore(cipher, root / "wrong-key"))
            damaged = bytearray(cipher.read_bytes())
            damaged[-1] ^= 1
            cipher.write_bytes(damaged)
            self.assertFalse(restore(cipher, root / "damaged"))
            self.assertFalse((root / "damaged").exists())

    def test_no_credential_does_not_persist_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            self.assertFalse(seal(Path(tmp), Path(tmp) / "cache.enc"))
            self.assertFalse(restore(Path(tmp) / "missing", Path(tmp) / "restored"))
