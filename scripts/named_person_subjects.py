"""Dependency-free exact subject parsing for named 24/7 channel artwork."""
from __future__ import annotations

import re
import unicodedata


PERSON_CATEGORIES = {
    "bollywood singers 24/7": "singer",
    "punjabi singers 24/7": "singer",
    "pakistani singers 24/7": "singer",
    "bollywood movies/actors 24/7": "actor",
}

# Provider spellings that were manually reviewed when the public subject asset
# catalog was created.  This is an exact correction table, not fuzzy matching.
CANONICAL_NAME_CORRECTIONS = {
    "A.R RAHMAN": "A. R. Rahman",
    "ANURADHA PAWDWAL": "Anuradha Paudwal",
    "ARJIT SINGH": "Arijit Singh",
    "GURUR RANDHAWA": "Guru Randhawa",
    "K.K": "KK",
    "K S CHITHRA": "K. S. Chithra",
    "KISHOR KUMAR": "Kishore Kumar",
    "MOHAMMAD AZIZ": "Mohammed Aziz",
    "MOHAMMAD RAFI": "Mohammed Rafi",
    "MOHIT CHAHUAN": "Mohit Chauhan",
    "PANKAJ UDAS": "Pankaj Udhas",
    "B PAARK": "B Praak",
    "GURMAN BHULLAR": "Gurnam Bhullar",
    "GURSHABD": "Gurshabad",
    "HAPPY RIAKOTI": "Happy Raikoti",
    "JAGJID SINGH": "Jagjit Singh",
    "JASMINE SANDLES": "Jasmine Sandlas",
    "LEHMBER HUSSAIN PURIA": "Lehmber Hussainpuri",
    "MAHINDER BUTTER": "Maninder Buttar",
    "MAKIT SINGH": "Malkit Singh",
    "NACHHHATAR GILL": "Nachhatar Gill",
    "RANJET BAEA": "Ranjit Bawa",
    "RAVINDER GRWAL": "Ravinder Grewal",
    "SARDOOL SIKANDR": "Sardool Sikander",
    "SATHWINDER BITTI": "Satwinder Bitti",
    "SHEERA JASVIE": "Sheera Jasvir",
    "SURJIT BINDRAKHIK": "Surjit Bindrakhia",
    "TARSEM JASSAR D": "Tarsem Jassar",
    "ALI FAZAR": "Ali Zafar",
    "GUL PANVA": "Gul Panra",
    "NABEEL SHOUKAT": "Nabeel Shaukat Ali",
    "QURAT UL AIN BALOUCH": "Qurat-ul-Ain Balouch",
    "RAHAT FATEHA ALI KHAN": "Rahat Fateh Ali Khan",
    "HRTHIK ROSHAN": "Hrithik Roshan",
    "AFTAB SHIVDASNI": "Aftab Shivdasani",
    "AJAY DEVGAN": "Ajay Devgn",
    "AKSHEY KUMAR": "Akshay Kumar",
    "AMITABH BACHAN": "Amitabh Bachchan",
    "AYUSHMAN KHURRANA": "Ayushmann Khurrana",
    "FARHAN AKTHAR": "Farhan Akhtar",
    "IRFAN KHAN": "Irrfan Khan",
    "JHON ABRAHAM": "John Abraham",
    "JHONY WALKER": "Johnny Walker",
    "MITRHAN CHAKRABORTY": "Mithun Chakraborty",
    "NASEERUDIN SHAH": "Naseeruddin Shah",
    "RAJENDR KUMAR": "Rajendra Kumar",
    "VINOOD KHANNA": "Vinod Khanna",
}


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def normalized_key(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_name.casefold()))


def slugify(name: str) -> str:
    """Return the stable filename form used by public named-person assets."""
    return normalized_key(name).replace(" ", "-")


def canonical_name(raw_name: str) -> str:
    raw_name = clean(raw_name)
    if raw_name in CANONICAL_NAME_CORRECTIONS:
        return CANONICAL_NAME_CORRECTIONS[raw_name]
    words: list[str] = []
    for word in raw_name.split():
        if len(word) == 1:
            words.append(word.upper())
        elif word == "KK":
            words.append(word)
        else:
            words.append(word.title())
    return " ".join(words)


def classify_person_subject(
    category_name: object, channel_name: object
) -> tuple[str, str]:
    """Return an exact category role and anchored provider-name subject."""
    category = clean(category_name).casefold()
    role = PERSON_CATEGORIES.get(category, "")
    if not role:
        return "", ""

    candidate = clean(channel_name)
    if role == "singer":
        prefixes = (
            r"^HINDI\s*[-|]\s*(?:SINGER\s+)?",
            r"^PAKISTANI\s+SINGER\s*(?:[-|]\s*)?",
            r"^PAKISTANI\s*[-|]\s*",
            r"^PUNJABI\s*[-|]\s*SINGER\s*[-|]?\s*",
        )
    else:
        prefixes = (r"^HINDI\s*[-|]\s*(?:ACTOR\s+)?",)

    for pattern in prefixes:
        candidate, substitutions = re.subn(
            pattern, "", candidate, flags=re.IGNORECASE
        )
        if substitutions:
            break
    else:
        return role, ""

    if role == "singer":
        candidate = re.sub(
            r"\s+(?:SONGS?|SNOGS)\s*(?:HD|UHD|4K)?$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    else:
        candidate = re.sub(
            r"\s+MOVIES?\s*(?:HD|UHD|4K)?$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    candidate = re.sub(
        r"\s+(?:HD|UHD|4K)$", "", candidate, flags=re.IGNORECASE
    )
    candidate = clean(candidate).strip(" -|")
    if not candidate or candidate.casefold() in {
        "bollywood",
        "hindi",
        "singer",
        "actor",
    }:
        return role, ""
    return role, candidate
