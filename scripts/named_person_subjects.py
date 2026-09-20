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
    "FAKHIR MEHMOOD": "Faakhir Mehmood",
    "GOHAR MUMTAZ": "Goher Mumtaz",
    "HUMAIRA CHANNA": "Humera Channa",
    "RAJKUMAAR RAO": "Rajkummar Rao",
}

# Broad 24/7 and regional movie categories contain a mixture of people,
# franchises, programmes, and descriptive channels. These entries are exact
# reviewed pairs, not category-wide parsing rules.
REVIEWED_EXACT_PERSON_CHANNELS = {
    ("pakistan movies 24/7", "pakistani | munawar zarif cinema 1 hd"): ("actor", "Munawar Zarif"),
    ("kannada movies 24/7", "kannada-vishnudhan movies hd"): ("actor", "Vishnuvardhan"),
    ("kannada movies 24/7", "kannada-shankar nag movies hd"): ("actor", "Shankar Nag"),
    ("kannada movies 24/7", "kannada-rajkumar movies hd"): ("actor", "Dr. Rajkumar"),
    ("kannada movies 24/7", "kannada-lokesh movies hd"): ("actor", "Lokesh"),
    ("kannada movies 24/7", "kannada-ambareesh movies hd"): ("actor", "Ambareesh"),
    ("kannada movies 24/7", "kannada-sudeep movies hd"): ("actor", "Sudeep"),
    ("kannada movies 24/7", "kannada-shiva rajkummar movies hd"): ("actor", "Shiva Rajkumar"),
    ("kannada movies 24/7", "kannada-ramesh aravind movies hd"): ("actor", "Ramesh Aravind"),
    ("kannada movies 24/7", "kannada-prwal devaraj movies hd"): ("actor", "Prajwal Devaraj"),
    ("kannada movies 24/7", "kannada-jaggesh movies hd"): ("actor", "Jaggesh"),
    ("us : 24x7", "24/7: billy connolly stand up"): ("actor", "Billy Connolly"),
    ("us : 24x7", "24/7: chris rock stand up"): ("actor", "Chris Rock"),
    ("us : 24x7", "24/7: conor mcgregor"): ("actor", "Conor McGregor"),
    ("us : 24x7", "24/7: david attenborough"): ("actor", "David Attenborough"),
    ("us : 24x7", "24/7: jimmy carr stand up"): ("actor", "Jimmy Carr"),
    ("us : 24x7", "24/7: louis theroux"): ("actor", "Louis Theroux"),
    ("us : 24x7", "24/7: michael mcintyre stand up"): ("actor", "Michael McIntyre"),
    ("us : 24x7", "24/7: michael moore"): ("actor", "Michael Moore"),
    ("us : 24x7", "24/7: micky flanagan stand up"): ("actor", "Micky Flanagan"),
    ("us : 24x7", "24/7: peter kay stand up"): ("actor", "Peter Kay"),
    ("us : 24x7", "24/7: bruce lee"): ("actor", "Bruce Lee"),
    ("us : 24x7", "24/7: hitchcock"): ("actor", "Alfred Hitchcock"),
    ("us : 24x7", "24/7: schwarzenegger"): ("actor", "Arnold Schwarzenegger"),
    ("us : 24x7", "24/7: van damme"): ("actor", "Jean-Claude Van Damme"),
    ("|eu| le meilleur des films", "fr - 24/7 bruce lee"): ("actor", "Bruce Lee"),
    ("|eu| le meilleur des films", "fr - 24/7 jcvd"): ("actor", "Jean-Claude Van Damme"),
    ("|uk| 24/7 flex", "uk - al pacino"): ("actor", "Al Pacino"),
    ("|uk| 24/7 flex", "uk - arnold schwarzenegger"): ("actor", "Arnold Schwarzenegger"),
    ("|uk| 24/7 flex", "uk - steven seagal"): ("actor", "Steven Seagal"),
    ("|uk| 24/7 flex", "uk - will ferrell movies"): ("actor", "Will Ferrell"),
    ("|na| 24/7 english", "eng - 24/7 quentin tarantino movies"): ("actor", "Quentin Tarantino"),
    ("|na| 24/7 english", "eng - 24/7 jcvd movies"): ("actor", "Jean-Claude Van Damme"),
    ("|na| 24/7 english", "eng - 24/7 al pacino"): ("actor", "Al Pacino"),
    ("|na| 24/7 english", "eng - 24/7 arnold schwarzenegger"): ("actor", "Arnold Schwarzenegger"),
    ("|na| 24/7 english", "eng - 24/7 steven seagal"): ("actor", "Steven Seagal"),
    ("|na| 24/7 english", "eng - 24/7 will ferrell movies"): ("actor", "Will Ferrell"),
    ("|na| 24/7 english", "eng - 24/7 bruce lee movies"): ("actor", "Bruce Lee"),
    ("|na| 24/7 english", "eng - 24/7 jim carrey"): ("actor", "Jim Carrey"),
    ("|na| 24/7 english", "eng - 24/7 eddy murphy"): ("actor", "Eddie Murphy"),
    ("|en| 24/7 english 4k", "en - clint eastwood"): ("actor", "Clint Eastwood"),
    ("|en| 24/7 english 4k", "en - tom hardy collection"): ("actor", "Tom Hardy"),
}

REVIEWED_CANONICAL_NAMES = frozenset(
    subject for _role, subject in REVIEWED_EXACT_PERSON_CHANNELS.values()
)


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
    if raw_name in REVIEWED_CANONICAL_NAMES:
        return raw_name
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
    channel = clean(channel_name).casefold()
    reviewed = REVIEWED_EXACT_PERSON_CHANNELS.get((category, channel))
    if reviewed is not None:
        return reviewed
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
