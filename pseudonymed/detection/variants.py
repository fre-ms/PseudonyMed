"""Regular-expression builders for one patient's identifiers.

Every builder returns a pattern *string* (or ``None`` when the input is too
short to be safe).  Names are matched in their written form, in capitals and
in title case, with umlauts transliterated (Müller, Mueller, Muller) and an
optional genitive s.  Lower-case forms are deliberately not matched so that
surnames which are also ordinary words ("klein", "koch", "ernst") do not fire
in running text.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

# Word boundaries based on Unicode letters (``\\w`` would also cover digits).
L = r"(?<![^\W\d_])"
R = r"(?![^\W\d_])"
SEP = r"[\s\-‐‑–]+"
GENITIVE = r"(?:s|'s|’s)?"

_TRANS = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"}
)
_TITLES = (
    r"(?:(?:Univ\.-Prof\.|Prof\.|PD|Priv\.-Doz\.|Dr\.|med\.|dent\.|phil\.|rer\.|nat\.|h\.c\."
    r"|Dipl\.-\w+\.?|Mag\.|MUDr\.|MD|PhD)\s*)*"
)
_MONTHS = {
    1: ("Januar", "Jan", "Jänner"),
    2: ("Februar", "Feb"),
    3: ("März", "Maerz", "Mrz", "Mär"),
    4: ("April", "Apr"),
    5: ("Mai",),
    6: ("Juni", "Jun"),
    7: ("Juli", "Jul"),
    8: ("August", "Aug"),
    9: ("September", "Sept", "Sep"),
    10: ("Oktober", "Okt"),
    11: ("November", "Nov"),
    12: ("Dezember", "Dez"),
}
_STREET_SUFFIX = re.compile(r"[\s\-]?(stra(?:ß|ss)e|str\.?)$", re.IGNORECASE)
_NAME_PARTICLES = {"von", "van", "der", "den", "de", "zu", "zur", "zum", "da", "dos", "del",
                   "la", "le", "und"}


def without_accents(text: str) -> str:
    """The text with diacritics stripped and ß written as ss ("Müller" -> "Muller")."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def forms(text: str) -> set[str]:
    """Spelling variants of a name: as written, transliterated, without accents,
    each also in capitals and in title case."""
    base = text.strip()
    if not base:
        return set()
    variants = {base, base.translate(_TRANS), without_accents(base)}
    out: set[str] = set()
    for v in variants:
        out.update({v, v.upper(), v.title()})
    return {v for v in out if v}


def flex(form: str) -> str:
    """Escape a form, allowing any whitespace or hyphen between its parts."""
    parts = [re.escape(t) for t in re.split(r"[\s\-‐‑–]+", form) if t]
    return SEP.join(parts)


def alternative(forms_: set[str]) -> str:
    """A non-capturing alternation of the forms, longest first so that the regex engine
    tries the fullest spelling before a shorter one that is its prefix."""
    return "(?:" + "|".join(flex(f) for f in sorted(forms_, key=len, reverse=True)) + ")"


def name_pattern(name: str, genitive: bool = True, min_length: int = 2) -> str | None:
    """A single name (surname, first name, birth name) as a whole word."""
    if len(name.strip()) < min_length:
        return None
    return f"{L}{alternative(forms(name))}{GENITIVE if genitive else ''}{R}"


def full_name_pattern(first_names: list[str], surname: str) -> str | None:
    """"Max Mustermann", "Max Peter Mustermann", "Mustermann, Max", "M. Mustermann"."""
    first_names = [v for v in first_names if v.strip()]
    if not first_names or not surname.strip():
        return None
    first_name_forms: set[str] = set()
    for v in first_names:
        first_name_forms |= forms(v)
    V = alternative(first_name_forms)
    N = alternative(forms(surname))
    initials = alternative({f[0] for f in forms(first_names[0]) if f})
    variants = [
        rf"{V}(?:{SEP}{V})*{SEP}{N}{GENITIVE}",
        rf"{N},?\s+{V}(?:{SEP}{V})*",
        rf"{initials}\.\s?{N}{GENITIVE}",
    ]
    return f"{L}(?:{'|'.join(variants)}){R}"


def title_name_pattern(first_names: list[str], surname: str) -> str | None:
    """Name with optional academic titles in front ("Dr. med. Anna Schmidt", "Dr. Schmidt").

    Used for referring physicians so that the title is replaced together with the
    name and the pseudonym "Dr. Z1" does not end up as "Dr. Dr. Z1".
    """
    if not surname.strip():
        return None
    core = [name_pattern(surname) or ""]
    full = full_name_pattern(first_names, surname)
    if full:
        core.insert(0, full)
    inner = "|".join(f"(?:{k})" for k in core if k)
    return rf"(?:{L}{_TITLES})?(?:{inner})"


def surname_parts(surname: str) -> list[str]:
    """Parts of a double-barrelled or compound surname worth matching on their own."""
    parts = [t for t in re.split(r"[\s\-‐‑–]+", surname.strip()) if t]
    if len(parts) < 2:
        return []
    return [t for t in parts if len(t) >= 4 and t.lower() not in _NAME_PARTICLES]


def date_pattern(d: dt.date) -> str:
    """All common German spellings of one calendar date."""
    t, m, j = d.day, d.month, d.year
    tz = _tolerant_digits
    T, M = rf"[0OoDQ]?{tz(t)}", rf"[0OoDQ]?{tz(m)}"
    J, YY = tz(j), rf"(?:{tz(j)}|{tz(j % 100, 2)})"
    mon = "|".join(re.escape(x) + r"\.?" for x in _MONTHS[m])
    alts = [
        rf"{T}\s?\.\s?{M}\s?\.\s?{YY}",
        rf"{T}\s?/\s?{M}\s?/\s?{YY}",
        rf"{T}\s?-\s?{M}\s?-\s?{J}",
        rf"{J}-{tz(m, 2)}-{tz(t, 2)}",
        rf"{J}/{tz(m, 2)}/{tz(t, 2)}",
        rf"{T}\.?\s*(?i:{mon})\s*{J}",
    ]
    return r"(?<![\d.])(?:" + "|".join(f"(?:{a})" for a in alts) + r")(?!\d)"


def _tolerant_digits(number: int, width: int = 0) -> str:
    """Digits of ``number`` as OCR-tolerant character classes ("1" also matches "I" or "l")."""
    text = f"{number:0{width}d}" if width else str(number)
    return "".join(_tolerant(c) for c in text)


def phone_pattern(number: str) -> str | None:
    """A phone number with any separators and +49/0049/0 prefix variants."""
    digits = re.sub(r"\D", "", number)
    if digits.startswith("0049"):
        digits = digits[4:]
    elif digits.startswith("49") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) < 5:
        return None
    S = r"[\s\-/().]*"
    core = S.join(_tolerant(c) for c in digits)
    prefix = r"\(?\s*(?:\+\s?49|0049|[0O])\s*\)?\s*(?:\([0O]\))?"
    return rf"(?<![\d+]){prefix}{S}{core}(?!\d)"


def email_pattern(email: str) -> str | None:
    """The address as written, case-insensitive, as a whole token."""
    email = email.strip()
    if "@" not in email:
        return None
    # a full stop after the address (end of sentence) is fine, ".more" is a longer address
    return rf"(?<![\w.+-])(?i:{re.escape(email)})(?![\w-])(?!\.[\w-])"


_HOUSE_NUMBER = re.compile(r"^(.*?)[\s,]*(\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?\s*$")


def street_and_number(street: str) -> tuple[str, str]:
    """Split "Sieringgasse 42 a" into ("Sieringgasse", "42 a"); no number -> ("…", "")."""
    street = street.strip()
    m = _HOUSE_NUMBER.match(street)
    if not m:
        return street, ""
    return m.group(1).strip(" ,"), (m.group(2) or "").strip()


def address_pattern(street: str) -> str | None:
    """Street with optional house number; "straße", "strasse" and "str." are interchangeable."""
    street = street.strip()
    if len(street) < 3:
        return None
    name, number = street_and_number(street)
    sm = _STREET_SUFFIX.search(name)
    if sm:
        base = name[: sm.start()].rstrip(" -")
        if not base:
            return None
        street_re = (
            alternative(forms(base))
            + r"[\s\-]?(?:[Ss]tra(?:ß|ss)e|[Ss]tr\.?|STRA(?:SS|ß)E|STR\.?)"
        )
    else:
        street_re = alternative(forms(name))
    number_re = ""
    digits = re.match(r"\d+", number)  # the house number always starts with digits
    if digits:
        number_re = rf"(?:\s*,?\s*{digits.group(0)}\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?"
    return rf"{L}{street_re}{number_re}(?![^\W_])"


def place_pattern(place: str) -> str | None:
    """The town on its own ("Musterstadt", "MUSTERSTADT", "Muenchen")."""
    place = place.strip()
    if len(place) < 3:
        return None
    return rf"{L}{alternative(forms(place))}{R}"


def postcode_place_pattern(postcode: str, place: str) -> str | None:
    """"12345 Musterstadt" or "D-12345 Musterstadt"; needs both parts."""
    place = place.strip()
    postcode = re.sub(r"\D", "", postcode)
    if len(place) < 3 or not postcode:
        return None
    return rf"(?<![\w])(?:D[\-\s])?{postcode}\s*{alternative(forms(place))}{R}"


# Characters OCR engines confuse with each other; codes are matched tolerant to these.
_OCR_CONFUSIONS = {
    "0": "0OoDQ", "O": "O0Q", "D": "D0", "1": "1IlL|", "I": "I1l", "L": "L1", "5": "5S", "S": "S5",
    "8": "8B", "B": "B8", "2": "2Z", "Z": "Z2", "6": "6G", "G": "G6", "4": "4A", "A": "A4",
}


def _tolerant(chars: str) -> str:
    """Character class for one code character: both cases plus its OCR look-alikes."""
    category = _OCR_CONFUSIONS.get(chars.upper(), "")
    if not category and not chars.isalpha():
        return re.escape(chars)
    char_set = {*category, *category.lower(), chars.upper(), chars.lower()} - {"|"}
    content = "".join(sorted(char_set)).replace("\\", "\\\\").replace("]", "\\]")
    return "[" + content + ("|" if "|" in category else "") + "]"


def exact_pattern(value: str) -> str | None:
    """Alphanumeric code (insurance number, patient id, file number) as written,
    tolerant to spaces inside, case and the usual OCR confusions (0/O, 1/I, 5/S, 8/B).

    Boundaries follow the character class at each end: a code starting with a
    letter may follow a digit (OCR often glues "68202938AZ 2023/171"), a code
    starting with a digit may follow a letter, and the same at the end.
    """
    value = value.strip()
    if len(value) < 4:
        return None
    parts = ["".join(_tolerant(c) for c in t) for t in re.split(r"\s+", value) if t]
    left = r"(?<![^\W\d_])" if value[0].isalpha() else r"(?<!\d)"
    right = R if value[-1].isalpha() else r"(?!\d)"
    return rf"{left}{r'\s*'.join(parts)}{right}"


def number_pattern(value: str) -> str | None:
    """A numeric id, tolerant to OCR confusions of single digits."""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return None
    return rf"(?<![\dOoIl]){''.join(_tolerant(c) for c in digits)}(?![\dOoIl])"


def term_pattern(term: str) -> str | None:
    """A free term from the user's lists, case-insensitive, flexible whitespace."""
    term = term.strip()
    if len(term) < 3:
        return None
    return rf"{L}(?i:{flex(term)}){R}"
