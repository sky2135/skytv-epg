"""Series edition labels under the reviewed provider naming convention.

Title naming and a work's verified original language are classification evidence,
not a probe of playable audio tracks. Generic language/category labels never
override an explicit edition suffix or the original-version convention.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

try:
    from .build_vod_catalogs import LANGUAGE_NAMES, LANGUAGE_CODES
except ImportError:
    from build_vod_catalogs import LANGUAGE_NAMES, LANGUAGE_CODES


_ISO3 = dict(zip(
    "ara ben zho ces dan nld eng fas fil fin fra deu ell guj heb hin hun ind ita jpn kan khm kor lao msa mal mar nep nor pan pol por ron rus sin slk som spa swa swe tam tel tha tur ukr urd vie".split(),
    "ar bn zh cs da nl en fa tl fi fr de el gu he hi hu id it ja kn km ko lo ms ml mr ne no pa pl pt ro ru si sk so es sw sv ta te th tr uk ur vi".split(),
))
_ISO3.update(chi="zh", cze="cs", dut="nl", per="fa", fre="fr", ger="de",
             gre="el", may="ms", rum="ro", slo="sk", tgl="tl")
_CODES = LANGUAGE_CODES - {"und", "mul"}
_MULTI = re.compile(r"\b(?:multi[ -]?(?:audio|language|lingual)|multilingual|bilingual|dual[ -]?audio|multi)\b", re.I)
_BRACKET_END = re.compile(r"(?:\(([^()]*)\)|\[([^\[\]]*)\]|\{([^{}]*)\})\s*$")
_SEPARATORS = re.compile(r"\s*(?:[,/|+;&]|\band\b)\s*", re.I)
_WRAPPER = re.compile(
    r"(?:(?:19|20)\d{2}|(?:480|576|720|1080|1440|2160|4320)[pi]|[248]k|"
    r"[suf]?hd|sd|uhd|hdr10\+?|hdr|dv|hevc|avc|x26[45]|h[ .]?26[45]|"
    r"web[ -]?(?:dl|rip)|blu[ -]?ray|brrip|webrip|remux|10bit|8bit|"
    r"s\d{1,3}(?:e\d{1,4})?|season\s*\d{1,3})", re.I)
_BARE_WRAPPER_END = re.compile(r"(?:^|\s|\s[-|:]\s)([^\s]+|season\s*\d{1,3})\s*$", re.I)


def _code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value).casefold().strip().replace("_", "-")
    if value in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[value]
    if value in _ISO3:
        return _ISO3[value]
    if value in _CODES:
        return value
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,8}){1,2}", value):
        return _code(value.split("-", 1)[0])
    return None


def _label(value: str) -> tuple[list[str], bool] | None:
    """Accept a complete language label, never a language word in a title."""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    multi = bool(_MULTI.search(value))
    value = _MULTI.sub(" ", value)
    value = re.sub(r"\b(?:audio|languages?|dubbed|dub|version)\b", " ", value).strip(" ._-:/|+;&")
    if not value:
        return ([], True) if multi else None
    direct = _code(value)
    if direct:
        return [direct], multi
    parts = _SEPARATORS.split(value)
    if len(parts) == 1:
        parts = value.split()
    codes = [_code(part.strip()) for part in parts]
    if not codes or any(code is None for code in codes):
        return None
    return list(dict.fromkeys(codes)), multi


def _wrappers_only(value: str) -> bool:
    # Hyphens inside WEB-DL/H.265 are meaningful; split on whitespace only.
    value = re.sub(r"\bseason\s+(\d{1,3})\b", r"s\1", value, flags=re.I)
    return bool(value.strip()) and all(_WRAPPER.fullmatch(part) for part in re.split(r"\s+", value.strip()))


def _title_suffix(title: Any) -> tuple[list[str], bool] | None:
    if not isinstance(title, str):
        return None
    remaining = unicodedata.normalize("NFKC", title[:4096]).strip()
    codes: list[str] = []
    multi = False
    found = False
    for _ in range(16):
        match = _BRACKET_END.search(remaining)
        if match:
            label = next(part for part in match.groups() if part is not None)
            parsed = _label(label)
            if parsed is not None:
                codes = list(dict.fromkeys(parsed[0] + codes))
                multi = multi or parsed[1]
                found = True
            elif not _wrappers_only(label):
                break
            remaining = remaining[:match.start()].rstrip(" -|:")
            continue
        # An unbracketed language label needs an explicit presentation delimiter.
        match = re.search(r"\s[-|:]\s([^\n]+)$", remaining)
        if match:
            parsed = _label(match.group(1))
            if parsed is not None:
                codes = list(dict.fromkeys(parsed[0] + codes))
                multi = multi or parsed[1]
                found = True
                remaining = remaining[:match.start()].rstrip()
                continue
        match = _BARE_WRAPPER_END.search(remaining)
        if match and _wrappers_only(match.group(1)):
            remaining = remaining[:match.start()].rstrip()
            continue
        break
    return (codes, multi) if found else None


def _result(codes: list[str], source: str, *, multi=False, complete=True) -> dict[str, Any]:
    codes = list(dict.fromkeys(codes))
    return {"language": "mul" if len(codes) > 1 else codes[0] if codes else "und",
            "languages": codes or ["und"], "isMultilingual": multi or len(codes) > 1,
            "languageListComplete": bool(codes) and complete,
            "languageSource": source}


def _provider_audio(sources: Any, original: str | None) -> dict[str, Any] | None:
    if isinstance(sources, Mapping):
        sources = [sources]
    elif not isinstance(sources, (list, tuple)):
        return None
    # As in the provider normalizer, the last/deepest source is most specific.
    for source in reversed(sources):
        if not isinstance(source, Mapping):
            continue
        for key in ("audio_languages", "audio_language"):
            raw = source.get(key)
            values = raw if isinstance(raw, (list, tuple)) else [raw]
            codes, multi, incomplete = [], False, False
            for value in values[:100]:
                if isinstance(value, Mapping):
                    value = next((value[name] for name in ("iso_639_1", "code", "name") if name in value), None)
                parsed = _label(value) if isinstance(value, str) else None
                if parsed is None:
                    incomplete = True
                else:
                    codes.extend(parsed[0])
                    multi = multi or parsed[1]
            if codes or multi:
                if multi and original:
                    codes.insert(0, original)
                return _result(codes, "provider_audio", multi=multi,
                               complete=not (multi or incomplete or len(values) > 100))
    return None


def classify_series_language(title: Any, original_language: Any, provider_sources=(), *,
                             metadata_verified: bool = False) -> dict[str, Any]:
    """Classify a provider edition without changing its title or guessing tracks.

    Explicit audio fields win. A language suffix wins over generic provider
    language/category fields (which this function deliberately ignores). Bare
    titles mean the original version only when its work identity is verified.
    """
    original = _code(original_language) if metadata_verified is True else None
    audio = _provider_audio(provider_sources, original)
    if audio is not None:
        return audio
    suffix = _title_suffix(title)
    if suffix is not None:
        codes, multi = suffix
        if multi:
            codes = list(dict.fromkeys(([original] if original else []) + codes))
            return _result(codes, "provider_multilingual", multi=True, complete=False)
        return _result(codes, "title_suffix")
    if original:
        return _result([original], "verified_original")
    return _result([], "unknown", complete=False)
