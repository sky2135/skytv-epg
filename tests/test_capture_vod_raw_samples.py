from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import capture_vod_raw_samples as capture

CONFIG = capture.vod.ServerConfig("server_1", "https://provider.invalid", "private-user", "private-password")


class Response:
    def __init__(self, raw, status=200, location=None):
        self.raw, self.status_code = raw, status
        self.headers = {"Location": location} if location else {}
        self.is_redirect = self.is_permanent_redirect = location is not None
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), 7):
            yield self.raw[start:start + 7]

    def close(self):
        self.closed = True


def decrypt(envelope, private):
    key = private.decrypt(base64.b64decode(envelope["wrappedKey"]), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=capture.AAD))
    return AESGCM(key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), capture.AAD)


class RawProviderCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def test_selected_record_preserves_exact_bytes_unknown_fields_types_and_bom(self):
        record = b'{ "stream_id" : 123, "name":"Raw \\u0915", "added":"1000", "rating":1e2, "direct_source":"https://private.invalid/u/p", "unknown":{"password":"private-password"}, "empty":null }'
        raw = b'\xef\xbb\xbf [\r\n ' + record + b', {"stream_id":999,"name":"Other"}\n]'
        result = capture.extract_raw_records(raw, {"123"})
        self.assertEqual(result["123"][0], record)
        self.assertIsInstance(result["123"][1]["added"], str)
        self.assertEqual(result["123"][1]["unknown"]["password"], "private-password")
        self.assertIn(b'"rating":1e2', result["123"][0])

    def test_invalid_array_and_duplicate_ids_fail_instead_of_reencoding(self):
        for raw in (b'[{"stream_id":1}, {"stream_id":1}]', b'{"items":[]}', b'[{} ,]', b'[] trailing'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    capture.extract_raw_records(raw, {"1"})

    def test_inventory_id_and_title_binding_is_exact_without_altering_values(self):
        targets = capture.TARGETS["server_1"]
        rows = [{"stream_id": int(identifier), "name": title, "unknown-secret": "preserved"}
                for identifier, title in targets.values()]
        rows[1]["name"] = "Recycled unrelated identity"
        raw = json.dumps(rows, indent=3).encode()
        with mock.patch.object(capture, "capture_response", return_value=(raw, {"status": "received"})):
            result, _response = capture.capture_inventory("server_1", CONFIG, capture.time.monotonic() + 50)
        self.assertEqual(result["en"][1], "confirmed")
        self.assertIn(b'"unknown-secret": "preserved"', result["en"][0])
        self.assertEqual(result["hi"][1], "identity_changed")
        self.assertIn(b'Recycled unrelated identity', result["hi"][0])

    def test_200_capture_uses_existing_transport_and_preserves_entire_body(self):
        raw = b' {"movie_data":{"stream_id":123}, "unlisted":{"password":"private-password"}}\r\n'
        response = Response(raw)
        with mock.patch.object(capture.DeadlineSession, "get", return_value=response) as request:
            body, status = capture.capture_response(CONFIG, "get_vod_info", item_id="123")
        self.assertEqual(body, raw)
        self.assertEqual((status["status"], status["httpStatus"]), ("received", 200))
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.kwargs["params"]["vod_id"], "123")
        self.assertTrue(response.closed)

    def test_unsafe_redirect_body_is_captured_but_location_is_never_followed(self):
        raw = b'<html>raw private response with password=private-password</html>'
        response = Response(raw, status=302, location="https://other-private.invalid/player_api.php")
        with mock.patch.object(capture.DeadlineSession, "get", return_value=response) as request:
            body, status = capture.capture_response(CONFIG, "get_vod_info", item_id="123")
        self.assertEqual(body, raw)
        self.assertEqual(status["status"], "redirect_refused")
        self.assertEqual(status["httpStatus"], 302)
        request.assert_called_once()

    def test_non200_empty_or_nonempty_body_is_never_fabricated_or_discarded(self):
        for raw in (b"", b'raw auth response username=private-user'):
            with self.subTest(raw=raw):
                with mock.patch.object(capture.DeadlineSession, "get", return_value=Response(raw, status=401)):
                    body, status = capture.capture_response(CONFIG, "get_vod_info", item_id="123")
                self.assertEqual(body, raw)
                self.assertEqual(status["status"], "auth_rejected")

    def test_non200_capture_limit_is_bounded_and_explicitly_labelled(self):
        with mock.patch.object(capture.DeadlineSession, "get", return_value=Response(b"raw secret long response", status=500)):
            body, status = capture.capture_response(CONFIG, "get_vod_info", item_id="123", maximum_bytes=4)
        self.assertEqual(body, b"raw ")
        self.assertTrue(status["bodyTruncated"])
        self.assertTrue(status["bodyIncomplete"])

    def test_interrupted_200_entity_body_is_retained_and_marked_incomplete(self):
        class Interrupted(Response):
            def iter_content(self, chunk_size):
                yield b'{"partial":'
                raise capture.vod.requests.ConnectionError("private transport details")
        with mock.patch.object(capture.DeadlineSession, "get", return_value=Interrupted(b"")):
            body, status = capture.capture_response(CONFIG, "get_vod_info", item_id="123")
        self.assertEqual(body, b'{"partial":')
        self.assertTrue(status["bodyIncomplete"])
        self.assertFalse(status["bodyTruncated"])
        self.assertEqual(status["status"], "transport")

    def test_encryption_roundtrip_and_gcm_tamper_rejection(self):
        plaintext = b'bytes with unknown fields, private-password and https://private.invalid/'
        first = capture.encrypt_bundle(plaintext, self.private.public_key())
        second = capture.encrypt_bundle(plaintext, self.private.public_key())
        self.assertEqual(decrypt(first, self.private), plaintext)
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotIn("private-password", json.dumps(first))
        ciphertext = bytearray(base64.b64decode(first["ciphertext"]))
        ciphertext[-1] ^= 1
        first["ciphertext"] = base64.b64encode(ciphertext).decode()
        with self.assertRaises(InvalidTag):
            decrypt(first, self.private)

    def test_runtime_requires_exact_4096_bit_pinned_public_key_and_rejects_private_pem(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipient.pem"
            path.write_bytes(self.private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            with self.assertRaises(ValueError):
                capture.load_recipient(path)
            large = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            path.write_bytes(large.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            with self.assertRaises(ValueError):
                capture.load_recipient(path)
            large = rsa.generate_private_key(public_exponent=65537, key_size=4096)
            public_der = large.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            path.write_bytes(large.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            with self.assertRaises(ValueError):
                capture.load_recipient(path)
            with mock.patch.object(capture, "RECIPIENT_SHA256", capture.hashlib.sha256(public_der).hexdigest()):
                self.assertEqual(capture.load_recipient(path).key_size, 4096)
            path.write_bytes(large.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with self.assertRaises(ValueError):
                capture.load_recipient(path)

    def test_bad_recipient_prevents_configuration_and_network_access(self):
        with mock.patch.object(capture, "load_recipient", side_effect=ValueError("key mismatch")), \
                mock.patch.object(capture.vod, "load_server_config") as configuration, \
                mock.patch.object(capture, "capture_inventory") as inventory, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(capture.main(["--recipient", "unused.pem", "--output", "unused.json"]), 2)
        configuration.assert_not_called()
        inventory.assert_not_called()

    def test_cli_writes_only_ciphertext_and_archive_keeps_nine_unfiltered_samples(self):
        raw_detail = b'{"unknown":{"password":"private-password"},"direct_source":"https://private.invalid/movie/u/p/1.mkv"}'
        def inventory(server, _config, _deadline):
            records = {code: (b'{ "stream_id":' + identifier.encode() + b', "unknown":"untouched" }', "confirmed")
                       for code, (identifier, _title) in capture.TARGETS[server].items()}
            return records, {"status": "received", "httpStatus": 200}
        detail_status = {"status": "received", "httpStatus": 200, "bodyBytes": len(raw_detail), "bodyTruncated": False}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture.enc.json"
            stdout = io.StringIO()
            with mock.patch.object(capture, "load_recipient", return_value=self.private.public_key()), \
                    mock.patch.dict(os.environ, {"ALLOW_INSECURE_PANEL_HTTP": "false"}), \
                    mock.patch.object(capture.vod, "load_server_config", return_value=CONFIG), \
                    mock.patch.object(capture, "capture_inventory", side_effect=inventory), \
                    mock.patch.object(capture, "capture_response", return_value=(raw_detail, detail_status)) as request, \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(capture.main(["--recipient", "unused.pem", "--output", str(output)]), 0)
            self.assertEqual(request.call_count, 9)
            raw = output.read_bytes()
            self.assertNotIn(b"private-password", raw)
            self.assertNotIn("private.invalid", stdout.getvalue())
            with zipfile.ZipFile(io.BytesIO(decrypt(json.loads(raw), self.private))) as archive:
                self.assertEqual(len(archive.namelist()), 19)
                self.assertEqual(archive.read("server_1/en/detail.response.body"), raw_detail)
                self.assertIn(b'"unknown":"untouched"', archive.read("server_1/en/selectedlistrecord.raw.json"))
                manifest = json.loads(archive.read("manifest.json"))
                self.assertFalse(manifest["sourceValuesFiltered"])
                dangal = next(item for item in manifest["records"] if item["serverId"] == "server_3" and item["language"] == "hi")
                self.assertEqual(dangal["selectionRole"], "verified_hindi_original_work_audio_unverified")
            self.assertEqual([path.name for path in Path(temporary).iterdir()], ["capture.enc.json"])

    def test_cli_does_not_request_details_for_unconfirmed_inventory_identity(self):
        def inventory(server, _config, _deadline):
            return {code: (b"", "identity_changed") for code in capture.TARGETS[server]}, {"status": "received"}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture.enc.json"
            with mock.patch.object(capture, "load_recipient", return_value=self.private.public_key()), \
                    mock.patch.dict(os.environ, {"ALLOW_INSECURE_PANEL_HTTP": "false"}), \
                    mock.patch.object(capture.vod, "load_server_config", return_value=CONFIG), \
                    mock.patch.object(capture, "capture_inventory", side_effect=inventory), \
                    mock.patch.object(capture, "capture_response") as request, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(capture.main(["--recipient", "unused.pem", "--output", str(output)]), 0)
            request.assert_not_called()
            with zipfile.ZipFile(io.BytesIO(decrypt(json.loads(output.read_bytes()), self.private))) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                for item in manifest["records"]:
                    self.assertEqual(item["detailResponse"], {"status": "not_requested_identity_unconfirmed", "httpStatus": None,
                        "bodyTruncated": False, "bodyIncomplete": False, "httpAttempts": 0, "bodyBytes": 0})
                self.assertEqual(archive.read("server_1/en/detail.response.body"), b"")


if __name__ == "__main__":
    unittest.main()
