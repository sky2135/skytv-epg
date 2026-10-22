"""Scan generated public outputs and diagnostics using existing publication policy."""
import base64
from collections import Counter
import gzip
import html
import json
import math
import os
import re
import stat
import zlib
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

failure = "Generated file failed its credential-safety check."
roots = (Path("public"), Path(".build/reports"))
raw_account = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
try:
    account = json.loads(raw_account)
except (TypeError, json.JSONDecodeError):
    raise SystemExit(failure) from None
if not isinstance(account, dict):
    raise SystemExit(failure)

def encoded_variants(value: str) -> set[bytes]:
    variants = {value}
    for ensure_ascii in (False, True):
        rendered = json.dumps(value, ensure_ascii=ensure_ascii)
        variants.update((rendered, rendered[1:-1]))
    csv_inner = value.replace('"', '""')
    variants.update((csv_inner, f'"{csv_inner}"'))
    variants.update(
        (
            html.escape(value, quote=False),
            html.escape(value, quote=True),
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"),
        )
    )
    for rendered in (quote(value, safe=""), quote_plus(value, safe="")):
        variants.add(rendered)
        variants.add(
            re.sub(
                r"%[0-9A-Fa-f]{2}",
                lambda match: match.group(0).lower(),
                rendered,
            )
        )
    return {item.encode("utf-8") for item in variants if item}

def url_variants(value: str) -> set[bytes]:
    variants = {value, quote(value, safe=""), quote_plus(value, safe="")}
    for rendered in tuple(variants):
        variants.add(
            re.sub(
                r"%[0-9A-Fa-f]{2}",
                lambda match: match.group(0).lower(),
                rendered,
            )
        )
    return {item.encode("utf-8") for item in variants if item}

# Usernames can be ordinary words, so short/common usernames are
# matched only in credential-shaped fields and URLs. Passwords are
# always matched globally below: a false alarm is safer than ever
# publishing a configured password.
def distinctive_provider_value(value: str) -> bool:
    raw = value.encode("utf-8")
    if len(raw) < 10:
        return False
    counts = Counter(raw)
    entropy_bits = -sum(
        count * math.log2(count / len(raw))
        for count in counts.values()
    )
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    return entropy_bits >= 40 and (classes >= 2 or len(raw) >= 16)

def value_alternative(value: str) -> bytes:
    variants = sorted(encoded_variants(value), key=len, reverse=True)
    return b"(?:" + b"|".join(re.escape(item) for item in variants) + b")"

structured_patterns: list[re.Pattern[bytes]] = []

def add_labeled_patterns(labels: tuple[str, ...], value: str) -> None:
    if not value:
        return
    label = b"(?:" + b"|".join(
        re.escape(item.encode("ascii")) for item in labels
    ) + b")"
    target = value_alternative(value)
    boundary = rb"(?![A-Za-z0-9._~%+-])"
    structured_patterns.extend(
        (
            re.compile(
                rb'(?i:"' + label + rb'")[ \t\r\n]{0,16}:'
                rb'[ \t\r\n]{0,16}["\']?' + target + boundary
            ),
            re.compile(
                rb"(?<![A-Za-z0-9_])(?i:" + label + rb")"
                rb"(?![A-Za-z0-9_])[ \t]{0,8}[=:][ \t]{0,8}[\"']?"
                + target
                + boundary
            ),
            re.compile(
                rb"(?i:<[ \t]*" + label
                + rb"(?:[ \t][^>]{0,64})?>)[ \t\r\n]{0,16}"
                + target
                + boundary
            ),
        )
    )

raw_tokens: set[bytes] = set()
casefold_tokens: set[bytes] = set()
google_values = [raw_account]
gemini_key = os.environ.get("GEMINI_API_KEY", "")
if gemini_key:
    raw_tokens.update(encoded_variants(gemini_key))
for field in ("client_email", "private_key", "private_key_id"):
    value = account.get(field)
    if isinstance(value, str) and value:
        google_values.append(value)
for value in google_values:
    raw_tokens.update(encoded_variants(value))

# Include external metadata API secrets in the public-file gate.
for name in ("TMDB_READ_ACCESS_TOKEN", "TMDB_API_KEY"):
    value = os.environ.get(name, "").strip()
    if value:
        raw_tokens.update(encoded_variants(value))

maximum_structured_value = 0
for number in (1, 2, 3):
    base_url = os.environ.get(f"SERVER_{number}_BASE_URL", "").strip()
    username = os.environ.get(f"SERVER_{number}_USERNAME", "").strip()
    password = os.environ.get(f"SERVER_{number}_PASSWORD", "")
    parsed_base = urlsplit(base_url)
    if parsed_base.hostname:
        casefold_tokens.add(
            parsed_base.hostname.casefold().encode("utf-8")
        )
    for base_variant in {
        base_url,
        base_url.rstrip("/"),
        parsed_base.netloc,
        parsed_base.hostname or "",
    } - {""}:
        raw_tokens.update(encoded_variants(base_variant))
    username_variants = encoded_variants(username) if username else set()
    password_variants = encoded_variants(password) if password else set()
    maximum_structured_value = max(
        maximum_structured_value,
        max(
            (
                len(item)
                for item in username_variants | password_variants
            ),
            default=0,
        ),
    )
    if distinctive_provider_value(username):
        raw_tokens.update(username_variants)
    # Every password is intentionally global. The inventory command
    # requires at least four characters so unsafe passwords can be
    # rotated instead of weakening this check.
    raw_tokens.update(password_variants)
    add_labeled_patterns(("username", "user", "login", "user_name"), username)
    add_labeled_patterns(("password", "pass", "passwd", "pwd"), password)
    if username and password:
        for user_variant in url_variants(username):
            for password_variant in url_variants(password):
                raw_tokens.update(
                    {
                        b"://" + user_variant + b":" + password_variant + b"@",
                        b"/live/" + user_variant + b"/" + password_variant + b"/",
                        b"/" + user_variant + b"/" + password_variant + b"/",
                    }
                )
        basic = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        )
        raw_tokens.update({b"Basic " + basic, b"basic " + basic})

tokens = tuple(sorted(raw_tokens, key=len, reverse=True))
active_roots = []
for root in roots:
    if root.is_symlink():
        raise SystemExit(failure)
    if root.is_dir():
        active_roots.append(root)
if not active_roots or not tokens or not structured_patterns:
    raise SystemExit(failure)
overlap = max(
    max(len(token) for token in tokens) - 1,
    512 + (2 * maximum_structured_value),
)
raw_pattern = re.compile(
    b"(?:" + b"|".join(re.escape(token) for token in tokens) + b")"
)
casefold_pattern = (
    re.compile(
        b"(?:"
        + b"|".join(
            re.escape(token)
            for token in sorted(casefold_tokens, key=len, reverse=True)
        )
        + b")"
    )
    if casefold_tokens
    else None
)
structured_pattern = re.compile(
    b"(?:"
    + b"|".join(pattern.pattern for pattern in structured_patterns)
    + b")"
)
structured_labels = (
    b"username", b"user_name", b"password", b"passwd",
    b"login", b"user", b"pass", b"pwd",
)
structured_span = 512 + (2 * maximum_structured_value)

def has_structured_token(window: bytes, lowered: bytes) -> bool:
    word_bytes = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
    for label in structured_labels:
        offset = 0
        while True:
            position = lowered.find(label, offset)
            if position < 0:
                break
            after = position + len(label)
            quoted = (
                position > 0
                and after < len(lowered)
                and lowered[position - 1] == 34
                and lowered[after] == 34
            )
            before = position - 1
            while before >= 0 and lowered[before] in b" \t":
                before -= 1
            element = before >= 0 and lowered[before] == 60
            left_boundary = (
                position == 0 or lowered[position - 1] not in word_bytes
            )
            right_boundary = (
                after == len(lowered) or lowered[after] not in word_bytes
            )
            separator = after
            while (
                separator < len(lowered)
                and separator - after <= 8
                and lowered[separator] in b" \t"
            ):
                separator += 1
            assignment = (
                left_boundary
                and right_boundary
                and separator - after <= 8
                and separator < len(lowered)
                and lowered[separator] in b"=:"
            )
            if quoted or element or assignment:
                start = max(0, position - 2)
                stop = min(len(window), position + structured_span)
                if structured_pattern.search(window[start:stop]):
                    return True
            offset = position + 1
    return False

def contains_token(path: Path) -> bool:
    tail = b""
    opener = gzip.open if path.suffix.casefold() == ".gz" else open
    with opener(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return False
            window = tail + chunk
            lowered = window.lower()
            if (
                raw_pattern.search(window)
                or (
                    casefold_pattern is not None
                    and casefold_pattern.search(lowered)
                )
                or has_structured_token(window, lowered)
            ):
                return True
            tail = window[-overlap:] if overlap else b""

def annotation_path(path: Path) -> str:
    return (
        str(path)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )

try:
    files = sorted(
        path for root in active_roots for path in root.rglob("*")
    )
    for path in files:
        if path.is_symlink():
            raise OSError
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise OSError
        if contains_token(path):
            print(
                f"::error file={annotation_path(path)}::{failure}",
                flush=True,
            )
            raise SystemExit(1)
except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
    raise SystemExit(failure) from None

print("Public-file safety scan passed.")
