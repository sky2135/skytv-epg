import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PublicSecretScannerTests(unittest.TestCase):
    def test_clean_component_and_plain_or_compressed_secret_reflection(self):
        script = Path(__file__).resolve().parents[1] / "scripts/scan_public_secrets.py"
        environment = {**os.environ, "GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({
            "private_key": "example-private-key-for-scanner-test"}),
            "SERVER_1_PASSWORD": "secret-password-987654321",
            "TMDB_API_KEY": "tmdb-test-token-123456789"}
        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp) / "public"
            public.mkdir()
            file = public / "index.json"
            file.write_text('{"schemaVersion":1}')
            def run():
                return subprocess.run([sys.executable, str(script)], cwd=tmp,
                                      env=environment, capture_output=True, text=True)
            self.assertEqual(run().returncode, 0)
            file.write_text('{"title":"secret-password-987654321"}')
            rejected = run()
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn("secret-password-987654321", rejected.stdout + rejected.stderr)
            file.write_text('{"schemaVersion":1}')
            (public / "home.json.gz").write_bytes(gzip.compress(b'{"note":"tmdb-test-token-123456789"}'))
            self.assertNotEqual(run().returncode, 0)
