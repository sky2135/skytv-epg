"""Conservative provider-label discovery; this module never confers identity.

The original display title and metadata stay untouched. The caller must verify
the returned query against official title/year evidence. Any prefix changed by
this fallback additionally requires independent poster or bound external proof.
"""
from __future__ import annotations

import re

MAX_TITLE_CHARACTERS = 2048
MAX_DECORATIONS = 12

# These are observed provider labels, not inferred countries or audio tracks.
LEGACY_PREFIXES = frozenset((
    "AR BN CS DA DE EL EN ENG ES FA FI FR GU HE HI HU ID IN IT JA KM KN KO LO ML "
    "MR MS NE NL NO PA PB PL PT RO RU SI SK SO SV SW TA TE TH TL TR UK UR VI ZH"
).split())
ADDITIONAL_PREFIXES = frozenset((
    "EX NF LAT GR AL BG DK TG TM MA AF STH SOM KD SC TOP PK IR IS"
).split())
QUALITY_PREFIXES = frozenset({"4K", "FHD", "UHD", "HD", "SD"})
COMPACT_PREFIXES = frozenset({"DE", "PK", "IT", "FR"})
COUNTRY_LABELS = frozenset((
    "US UK GB AU CA FR DE IT JP KR ES MX BE BR PL SE ZA IN TR CO CH AR CN NO DK "
    "PK RU NL IL TW HK TH PH PT CZ SK HU RO RS HR SI GR AL BG BA AE EG SA LB IQ "
    "SY JO IR TN MA DZ BD NP LK NG GH AF IS IE NZ FI AT UA EE LT LV ID MY SG VN"
).split())
LANGUAGE_LABELS = frozenset((
    "hindi hin hi marathi mar mr tamil tam ta telugu tel te english eng en "
    "punjabi pun pan pa pb bengali bangla ben bn kannada kan kn malayalam mal ml "
    "malyalam malyalam malayalm gujarati gujrati guj gu urdu urd ur turkish tur tr "
    "french fre fra frn fr russian rus ru italian ita it spanish spa esp es "
    "chinese chi zho zh japanese jap jpn ja korean kor ko arabic ara ar "
    "german ger deu de portuguese por pt dutch dut nld nl polish pol pl "
    "persian fas per fa farsi nepali nep ne sinhala sinhalese sin si "
    "swedish swe sv danish dan da norwegian nor no finnish fin fi hebrew heb he "
    "indonesian ind id malay msa may ms vietnamese vie vi thai tha th greek gre ell el "
    "albanian alb sq romanian rum ron ro czech cze ces cs slovak slo slk sk "
    "hungarian hun hu ukrainian ukr uk"
).split())
TECHNICAL_LABELS = frozenset({
    "multi", "multiaudio", "dualaudio", "multilanguage", "multilanguages",
    "multisub", "multisubs", "multisubtitles", "vostfr",
})
LEGACY_DECORATOR = re.compile(
    r"(?:4K|FHD|UHD|HD|SD|CAM|HDCAM|HDTS|HDTC|TS|TC|DUBBED|"
    r"(?:HINDI|MARATHI|TAMIL|TELUGU|ENGLISH|PUNJABI)(?:\s+DUBBED)?)\Z", re.I
)
WRAPPED_YEAR = re.compile(r"\((?:19|20)\d{2}\)|\[(?:19|20)\d{2}\]")
BARE_YEAR = re.compile(r"\s+(?:19|20)\d{2}\s*\Z")
TAIL_WRAPPER = re.compile(r"(?:\(([^()]*)\)|\[([^\[\]]*)\])\s*\Z")
TAIL_PIPE = re.compile(r"\s*\|\s*([^|]{1,120})\s*\Z")
YEAR_PUNCTUATION = re.compile(
    r"(.+)(\((?:19|20)\d{2}\)|\[(?:19|20)\d{2}\])\s*[.,:;!?。]+\s*\Z"
)
SPACED_PREFIX = re.compile(r"^([A-Z]{2,3}|4K)\s+[-|]\s+")
COMPACT_PREFIX = re.compile(r"^(DE|PK|IT|FR)-\s*(?=\S)")
YEAR_ONLY = re.compile(
    r"(?:(?:19|20)[0-9]{2}|\((?:19|20)[0-9]{2}\)|\[(?:19|20)[0-9]{2}\])"
    r"[.,:;!?。]*\Z"
)


def _year_context(title: str, source_year: str | None) -> bool:
    if isinstance(source_year, str) and re.fullmatch(r"(?:19|20)\d{2}", source_year):
        return bool(title and any(character.isalnum() for character in title))
    for match in WRAPPED_YEAR.finditer(title):
        if any(character.isalnum() for character in title[:match.start()]):
            return True
    match = BARE_YEAR.search(title)
    return bool(match and any(character.isalpha() for character in title[:match.start()]))


def _descriptor(label: str) -> str | None:
    label = label.strip()
    if not label or len(label) > 120:
        return None
    if LEGACY_DECORATOR.fullmatch(label):
        return "legacy"
    if label in COUNTRY_LABELS:
        return "new"
    compact = re.sub(r"[\s_-]+", "", label.casefold())
    if compact in TECHNICAL_LABELS:
        return "new"
    parts = re.split(r"\s*[+,/&]\s*", label.casefold())
    if not 1 <= len(parts) <= 8:
        return None
    for part in parts:
        part = re.sub(r"\s+(?:audio|dubbed|sub|subs|subtitle|subtitles)\Z", "", part.strip())
        if part not in LANGUAGE_LABELS:
            return None
    return "new"


def _tail_edit(title: str, source_year: str | None) -> tuple[str, bool] | None:
    match = TAIL_WRAPPER.search(title)
    pipe = False
    if match:
        label = next(value for value in match.groups() if value is not None)
    else:
        match = TAIL_PIPE.search(title)
        if match is None:
            return None
        label = match.group(1)
        pipe = True
    descriptor = _descriptor(label)
    if descriptor is None:
        return None
    candidate = title[:match.start()].rstrip()
    # Pipe before a wrapped suffix is part of this observed provider field.
    pipe = pipe or candidate.endswith("|")
    candidate = re.sub(r"\s*\|\s*\Z", "", candidate).rstrip()
    if not candidate or not any(character.isalnum() for character in candidate):
        return None
    if (descriptor == "new" or pipe) and not _year_context(candidate, source_year):
        return None
    return candidate, descriptor == "new" or pipe


def _only_year_and_terminal_descriptors(title: str) -> bool:
    """Protect a possible short title when a prefix would erase all title text.

    This is a structural probe only, never a source of identity or year facts.
    Unlike normal tail cleanup it can inspect a numeric-only remainder, since
    finding only one year is exactly the reason not to remove the prefix.
    Unknown labels, title words and a second number stop the protection.
    """
    current = title.strip()
    for count in range(MAX_DECORATIONS + 1):
        if YEAR_ONLY.fullmatch(current):
            return True
        if count == MAX_DECORATIONS:
            return False
        match = TAIL_WRAPPER.search(current)
        if match:
            label = next(value for value in match.groups() if value is not None)
        else:
            match = TAIL_PIPE.search(current)
            if match is None:
                return False
            label = match.group(1)
        if _descriptor(label) is None:
            return False
        current = re.sub(r"\s*\|\s*\Z", "", current[:match.start()].rstrip()).rstrip()
        if not current:
            return False
    return False


def _preprocess(title: str, source_year: str | None, kind: str | None) -> tuple[str, bool]:
    if not isinstance(title, str):
        return "", False
    if len(title) > MAX_TITLE_CHARACTERS:
        return title, False
    original = title.strip()
    current = original
    arabic_series_context = kind == "series" and re.match(r"^AR\s+[-|]\s+", original) is not None
    new_rule = prefix_changed = False
    for _ in range(MAX_DECORATIONS):
        if arabic_series_context:
            classifier = re.match(r"^مسلسل[ .]+", current)
            if classifier:
                candidate = current[classifier.end():].strip()
                if candidate and any(character.isalnum() for character in candidate):
                    current, new_rule, prefix_changed = candidate, True, True
                    arabic_series_context = False
                    continue
        prefix = SPACED_PREFIX.match(current)
        if prefix and prefix.group(1) in LEGACY_PREFIXES | ADDITIONAL_PREFIXES | QUALITY_PREFIXES:
            candidate = current[prefix.end():].strip()
            if (candidate and any(character.isalnum() for character in candidate)
                    and not _only_year_and_terminal_descriptors(candidate)):
                current, new_rule, prefix_changed = candidate, True, True
                continue
        prefix = COMPACT_PREFIX.match(current)
        if prefix and _year_context(current, source_year):
            candidate = current[prefix.end():].strip()
            if (candidate and any(character.isalnum() for character in candidate)
                    and not _only_year_and_terminal_descriptors(candidate)):
                current, new_rule, prefix_changed = candidate, True, True
                continue
        punctuation = YEAR_PUNCTUATION.fullmatch(current)
        if punctuation and any(character.isalnum() for character in punctuation.group(1)):
            current = (punctuation.group(1) + punctuation.group(2)).rstrip()
            new_rule = True
            continue
        tail = _tail_edit(current, source_year)
        if tail is None:
            break
        current, added = tail
        new_rule |= added
    else:
        # Do not split a malformed decoration chain over this and the legacy
        # parser's independent bound, accidentally making a deep input usable.
        if (_tail_edit(current, source_year) is not None
                or SPACED_PREFIX.match(current) or COMPACT_PREFIX.match(current)):
            return original, False
    # Let the existing parser own existing-only labels and its original bound.
    return (current, prefix_changed) if new_rule else (original, False)


def preprocess_provider_decorations(
    title: str, *, source_year: str | None = None, kind: str | None = None
) -> str:
    """Return a discovery query without modifying a year or identity field."""
    return _preprocess(title, source_year, kind)[0]


def provider_cleanup_requires_corroboration(
    title: str, *, source_year: str | None = None, kind: str | None = None
) -> bool:
    """Every prefix removal in this fallback requires independent proof."""
    return _preprocess(title, source_year, kind)[1]
