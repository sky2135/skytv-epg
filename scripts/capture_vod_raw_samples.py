#!/usr/bin/env python3
"""One-off CI capture: exact source bytes in memory, ciphertext-only output."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import build_vod_catalogs as vod
from vod_provider_evidence import DeadlineSession, _error_category

AAD = b"skytv-vod-raw-samples-v1"
RECIPIENT_SHA256 = 'c2f1f9262e93d7fb324773ecca7e110faef408237a831562251fb7e6918ef3f2'
TARGETS = {'server_1': {'pa': ('597489', 'Sardaarji 3 (2025)(Punnjabi)'), 'hi': ('694169', 'Death Whisperer 3 (2025) (Hindi.CAM)'), 'en': ('826039', 'The End of Nowhere (2026) (English)')}, 'server_2': {'en': ('2558274', 'Miss Bala'), 'hi': ('2353100', 'K.G.N.'), 'pa': ('2464081', 'Mirza Jutt')}, 'server_3': {'en': ('1423919', 'EN - Theres a Man over There (2026)'), 'pa': ('1030073', 'PB - Lehmber Ginni'), 'hi': ('1166312', 'EN - Vedaa - Hindi English Subtitles (2024)')}}
MAX_DETAIL_BYTES = 512 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_ZIP_BYTES = 16 * 1024 * 1024


def extract_raw_records(raw, wanted):
    """Slice original UTF-8 object text; never serialize a decoded record."""
    text = raw.decode("utf-8-sig")
    decoder = json.JSONDecoder()
    position = 0
    def whitespace(index):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index
    position = whitespace(position)
    if position >= len(text) or text[position] != "[":
        raise ValueError("Unsupported inventory shape")
    position += 1
    found = {}
    while True:
        position = whitespace(position)
        if position >= len(text):
            raise ValueError("Incomplete array")
        if text[position] == "]":
            if whitespace(position + 1) != len(text):
                raise ValueError("Trailing data")
            return found
        start = position
        value, position = decoder.raw_decode(text, position)
        if isinstance(value, dict):
            identifier = next((str(value[key]) for key in ("stream_id", "vod_id", "id")
                               if type(value.get(key)) in (int, str)), "")
            if identifier in wanted:
                if identifier in found:
                    raise ValueError("Ambiguous duplicate ID")
                record = text[start:position].encode("utf-8")
                if len(record) > MAX_RECORD_BYTES:
                    raise ValueError("Record limit")
                found[identifier] = (record, value)
        position = whitespace(position)
        if position < len(text) and text[position] == ",":
            position = whitespace(position + 1)
            if position >= len(text) or text[position] == "]":
                raise ValueError("Invalid array separator")
        elif position >= len(text) or text[position] != "]":
            raise ValueError("Invalid array separator")


class CaptureSession(DeadlineSession):
    def __init__(self, deadline, maximum_bytes):
        super().__init__(deadline, {"requests": 0}, 5)
        self.maximum_bytes = maximum_bytes
        self.body = bytearray()
        self.http_status = None
        self.truncated = False
        self.incomplete = False
        self.headers["Accept-Encoding"] = "identity"

    def get(self, url, **kwargs):
        response = super().get(url, **kwargs)
        self.body = bytearray()
        self.http_status = response.status_code
        self.truncated = self.incomplete = False
        original = response.iter_content
        rejected = response.status_code != 200 or response.is_redirect or response.is_permanent_redirect
        limit = min(self.maximum_bytes, MAX_DETAIL_BYTES) if rejected else self.maximum_bytes
        def chunks(*args, **options):
            try:
                for chunk in original(*args, **options):
                    if not chunk:
                        continue
                    available = limit - len(self.body)
                    self.body.extend(chunk[:available])
                    if len(chunk) > available:
                        self.truncated = True
                        raise vod.CatalogBuildError("Capture response limit")
                    yield chunk
            except Exception:
                self.incomplete = True
                raise
        response.iter_content = chunks
        if rejected:
            # The unchanged transport will reject/validate this response next.
            # It otherwise does not read redirect or non-200 entity bodies.
            try:
                for _chunk in chunks(chunk_size=65536):
                    pass
            except Exception:
                self.incomplete = True
        return response


def capture_response(config, action, *, item_id="", maximum_bytes=MAX_DETAIL_BYTES, deadline=None):
    deadline = min(deadline or float("inf"), time.monotonic() + (150 if action == "get_vod_streams" else 25))
    session = CaptureSession(deadline, maximum_bytes)
    status = "received"
    try:
        vod._request_panel_json_once(session, config, action, item_id=item_id,
                                    maximum_bytes=maximum_bytes, budget=vod.DownloadBudget(limit=maximum_bytes))
    except Exception as error:
        status = "deadline" if time.monotonic() >= deadline else _error_category(error, config.server_id)
    finally:
        session.close()
    return bytes(session.body), {"status": status, "httpStatus": session.http_status,
        "bodyTruncated": session.truncated, "bodyIncomplete": session.incomplete,
        "httpAttempts": session.stats["requests"], "bodyBytes": len(session.body)}


def capture_inventory(server_id, config, deadline):
    raw, response = capture_response(config, "get_vod_streams", maximum_bytes=vod.MAX_RESPONSE_BYTES, deadline=deadline)
    results = {}
    try:
        records = extract_raw_records(raw, {item[0] for item in TARGETS[server_id].values()}) if response["status"] == "received" else {}
        for code, (identifier, title) in TARGETS[server_id].items():
            item = records.get(identifier)
            matches = bool(item and item[1].get("name", item[1].get("title")) == title)
            # Preserve the selected ID's actual record even if its title changed.
            # The manifest flags the conflict; detail requests still require confirmation.
            results[code] = (item[0] if item else b"", "confirmed" if matches else "identity_changed" if item else "not_found")
    except Exception:
        results = {code: (b"", "inventory_parse_failed") for code in TARGETS[server_id]}
    return results, response


def load_recipient(path):
    if path.stat().st_size > 16 * 1024:
        raise ValueError("Recipient limit")
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 4096 or key.public_numbers().e != 65537:
        raise ValueError("Recipient must be an RSA public key")
    public_der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if hashlib.sha256(public_der).hexdigest() != RECIPIENT_SHA256:
        raise ValueError("Recipient does not match intended capture recipient")
    return key


def encrypt_bundle(plaintext, recipient):
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)
    wrapped = recipient.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD))
    public_der = recipient.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"schemaVersion": 1, "algorithm": "RSA-OAEP-SHA256+A256GCM", "recipientSha256": hashlib.sha256(public_der).hexdigest(),
        "wrappedKey": base64.b64encode(wrapped).decode("ascii"), "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii")}


def make_archive(captures, manifest):
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in captures.items():
            total += len(raw)
            if total > MAX_ZIP_BYTES:
                raise ValueError("Capture limit")
            archive.writestr(name, raw)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode())
    if buffer.tell() > MAX_ZIP_BYTES:
        raise ValueError("Archive limit")
    return buffer.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        recipient = load_recipient(args.recipient)
        allow_http = os.environ.get("ALLOW_INSECURE_PANEL_HTTP", "false").strip().casefold()
        if allow_http not in {"true", "false"}:
            raise ValueError("HTTP setting")
        configs = {server: vod.load_server_config(server, allow_insecure_http=allow_http == "true") for server in TARGETS}
        deadline = time.monotonic() + 360
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {server: executor.submit(capture_inventory, server, config, deadline) for server, config in configs.items()}
            inventories = {server: future.result() for server, future in futures.items()}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {(server, code): executor.submit(capture_response, configs[server], "get_vod_info", item_id=item[0], deadline=deadline)
                       for server, languages in TARGETS.items() for code, item in languages.items()
                       if inventories[server][0][code][1] == "confirmed"}
            details = {key: future.result() for key, future in futures.items()}
        captures, records = {}, []
        for server, languages in TARGETS.items():
            for code, (identifier, _title) in languages.items():
                list_bytes, list_status = inventories[server][0][code]
                detail_bytes, detail_status = details.get((server, code), (b"", {
                    "status": "not_requested_identity_unconfirmed", "httpStatus": None,
                    "bodyTruncated": False, "bodyIncomplete": False, "httpAttempts": 0, "bodyBytes": 0}))
                prefix = server + "/" + code + "/"
                for suffix, raw in (("selectedlistrecord.raw.json", list_bytes), ("detail.response.body", detail_bytes)):
                    captures[prefix + suffix] = raw
                records.append({"serverId": server, "language": code, "providerId": identifier, "listStatus": list_status,
                    "listBytes": len(list_bytes), "listSha256": hashlib.sha256(list_bytes).hexdigest(),
                    "detailSha256": hashlib.sha256(detail_bytes).hexdigest(), "detailResponse": detail_status,
                    "selectionRole": "explicit_provider_hindi_title_english_subtitles_audio_unverified" if server == "server_3" and code == "hi" else "explicit_provider_language_label_audio_unverified"})
        manifest = {"schemaVersion": 1, "capturedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "bodyEncoding": "exact_http_entity_body_after_content_encoding_decoding", "sourceValuesFiltered": False,
            "audioAvailabilityVerified": False, "inventoryResponses": {server: result[1] for server, result in inventories.items()}, "records": records}
        envelope = encrypt_bundle(make_archive(captures, manifest), recipient)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Only encrypted bytes ever reach the CI filesystem, including tempfiles.
        temporary = vod._temporary_path(args.output.parent, "encrypted-provider-capture")
        try:
            temporary.write_bytes(json.dumps(envelope, sort_keys=True).encode() + b"\n")
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(json.dumps({"selectedRecords": 9, "confirmedListRecords": sum(item["listStatus"] == "confirmed" for item in records),
                          "detailResponses": sum(item["detailResponse"]["httpStatus"] is not None for item in records)}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"status": "capture_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
