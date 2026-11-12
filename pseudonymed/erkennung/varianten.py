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
GENITIV = r"(?:s|'s|’s)?"

_TRANS = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"}
)
_TITEL = (
    r"(?:(?:Univ\.-Prof\.|Prof\.|PD|Priv\.-Doz\.|Dr\.|med\.|dent\.|phil\.|rer\.|nat\.|h\.c\."
    r"|Dipl\.-\w+\.?|Mag\.|MUDr\.|MD|PhD)\s*)*"
)
_MONATE = {
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
_STRASSEN_SUFFIX = re.compile(r"[\s\-]?(stra(?:ß|ss)e|str\.?)$", re.IGNORECASE)
_NAMENSPARTIKEL = {"von", "van", "der", "den", "de", "zu", "zur", "zum", "da", "dos", "del",
                   "la", "le", "und"}


def ohne_akzente(text: str) -> str:
    """The text with diacritics stripped and ß written as ss ("Müller" -> "Muller")."""
    zerlegt = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    return "".join(c for c in zerlegt if not unicodedata.combining(c))


def formen(text: str) -> set[str]:
    """Spelling variants of a name: as written, transliterated, without accents,
    each also in capitals and in title case."""
    basis = text.strip()
    if not basis:
        return set()
    varianten = {basis, basis.translate(_TRANS), ohne_akzente(basis)}
    out: set[str] = set()
    for v in varianten:
        out.update({v, v.upper(), v.title()})
    return {v for v in out if v}


def flex(form: str) -> str:
    """Escape a form, allowing any whitespace or hyphen between its parts."""
    teile = [re.escape(t) for t in re.split(r"[\s\-‐‑–]+", form) if t]
    return SEP.join(teile)


def alternative(formen_: set[str]) -> str:
    """A non-capturing alternation of the forms, longest first so that the regex engine
    tries the fullest spelling before a shorter one that is its prefix."""
    return "(?:" + "|".join(flex(f) for f in sorted(formen_, key=len, reverse=True)) + ")"


def name_muster(name: str, genitiv: bool = True, mindestlaenge: int = 2) -> str | None:
    """A single name (surname, first name, birth name) as a whole word."""
    if len(name.strip()) < mindestlaenge:
        return None
    return f"{L}{alternative(formen(name))}{GENITIV if genitiv else ''}{R}"


def vollname_muster(vornamen: list[str], nachname: str) -> str | None:
    """"Max Mustermann", "Max Peter Mustermann", "Mustermann, Max", "M. Mustermann"."""
    vornamen = [v for v in vornamen if v.strip()]
    if not vornamen or not nachname.strip():
        return None
    v_formen: set[str] = set()
    for v in vornamen:
        v_formen |= formen(v)
    V = alternative(v_formen)
    N = alternative(formen(nachname))
    initialen = alternative({f[0] for f in formen(vornamen[0]) if f})
    varianten = [
        rf"{V}(?:{SEP}{V})*{SEP}{N}{GENITIV}",
        rf"{N},?\s+{V}(?:{SEP}{V})*",
        rf"{initialen}\.\s?{N}{GENITIV}",
    ]
    return f"{L}(?:{'|'.join(varianten)}){R}"


def titel_name_muster(vornamen: list[str], nachname: str) -> str | None:
    """Name with optional academic titles in front ("Dr. med. Anna Schmidt", "Dr. Schmidt").

    Used for referring physicians so that the title is replaced together with the
    name and the pseudonym "Dr. Z1" does not end up as "Dr. Dr. Z1".
    """
    if not nachname.strip():
        return None
    kern = [name_muster(nachname) or ""]
    voll = vollname_muster(vornamen, nachname)
    if voll:
        kern.insert(0, voll)
    inner = "|".join(f"(?:{k})" for k in kern if k)
    return rf"(?:{L}{_TITEL})?(?:{inner})"


def nachname_teile(nachname: str) -> list[str]:
    """Parts of a double-barrelled or compound surname worth matching on their own."""
    teile = [t for t in re.split(r"[\s\-‐‑–]+", nachname.strip()) if t]
    if len(teile) < 2:
        return []
    return [t for t in teile if len(t) >= 4 and t.lower() not in _NAMENSPARTIKEL]


def datum_muster(d: dt.date) -> str:
    """All common German spellings of one calendar date."""
    t, m, j = d.day, d.month, d.year
    tz = _ziffern_tolerant
    T, M = rf"[0OoDQ]?{tz(t)}", rf"[0OoDQ]?{tz(m)}"
    J, JJ = tz(j), rf"(?:{tz(j)}|{tz(j % 100, 2)})"
    mon = "|".join(re.escape(x) + r"\.?" for x in _MONATE[m])
    alts = [
        rf"{T}\s?\.\s?{M}\s?\.\s?{JJ}",
        rf"{T}\s?/\s?{M}\s?/\s?{JJ}",
        rf"{T}\s?-\s?{M}\s?-\s?{J}",
        rf"{J}-{tz(m, 2)}-{tz(t, 2)}",
        rf"{J}/{tz(m, 2)}/{tz(t, 2)}",
        rf"{T}\.?\s*(?i:{mon})\s*{J}",
    ]
    return r"(?<![\d.])(?:" + "|".join(f"(?:{a})" for a in alts) + r")(?!\d)"


def _ziffern_tolerant(zahl: int, stellen: int = 0) -> str:
    """Digits of ``zahl`` as OCR-tolerant character classes ("1" also matches "I" or "l")."""
    text = f"{zahl:0{stellen}d}" if stellen else str(zahl)
    return "".join(_tolerant(c) for c in text)


def telefon_muster(nummer: str) -> str | None:
    """A phone number with any separators and +49/0049/0 prefix variants."""
    ziffern = re.sub(r"\D", "", nummer)
    if ziffern.startswith("0049"):
        ziffern = ziffern[4:]
    elif ziffern.startswith("49") and len(ziffern) > 10:
        ziffern = ziffern[2:]
    if ziffern.startswith("0"):
        ziffern = ziffern[1:]
    if len(ziffern) < 5:
        return None
    S = r"[\s\-/().]*"
    kern = S.join(_tolerant(c) for c in ziffern)
    prefix = r"\(?\s*(?:\+\s?49|0049|[0O])\s*\)?\s*(?:\([0O]\))?"
    return rf"(?<![\d+]){prefix}{S}{kern}(?!\d)"


def email_muster(email: str) -> str | None:
    """The address as written, case-insensitive, as a whole token."""
    email = email.strip()
    if "@" not in email:
        return None
    # a full stop after the address (end of sentence) is fine, ".more" is a longer address
    return rf"(?<![\w.+-])(?i:{re.escape(email)})(?![\w-])(?!\.[\w-])"


_HAUSNUMMER = re.compile(r"^(.*?)[\s,]*(\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?\s*$")


def strasse_und_nummer(strasse: str) -> tuple[str, str]:
    """Split "Sieringgasse 42 a" into ("Sieringgasse", "42 a"); no number -> ("…", "")."""
    strasse = strasse.strip()
    m = _HAUSNUMMER.match(strasse)
    if not m:
        return strasse, ""
    return m.group(1).strip(" ,"), (m.group(2) or "").strip()


def adresse_muster(strasse: str) -> str | None:
    """Street with optional house number; "straße", "strasse" and "str." are interchangeable."""
    strasse = strasse.strip()
    if len(strasse) < 3:
        return None
    name, nummer = strasse_und_nummer(strasse)
    sm = _STRASSEN_SUFFIX.search(name)
    if sm:
        basis = name[: sm.start()].rstrip(" -")
        if not basis:
            return None
        strassen_re = (
            alternative(formen(basis))
            + r"[\s\-]?(?:[Ss]tra(?:ß|ss)e|[Ss]tr\.?|STRA(?:SS|ß)E|STR\.?)"
        )
    else:
        strassen_re = alternative(formen(name))
    nummer_re = ""
    ziffern = re.match(r"\d+", nummer)  # the house number always starts with digits
    if ziffern:
        nummer_re = rf"(?:\s*,?\s*{ziffern.group(0)}\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?"
    return rf"{L}{strassen_re}{nummer_re}(?![^\W_])"


def ort_muster(ort: str) -> str | None:
    """The town on its own ("Musterstadt", "MUSTERSTADT", "Muenchen")."""
    ort = ort.strip()
    if len(ort) < 3:
        return None
    return rf"{L}{alternative(formen(ort))}{R}"


def plz_ort_muster(plz: str, ort: str) -> str | None:
    """"12345 Musterstadt" or "D-12345 Musterstadt"; needs both parts."""
    ort = ort.strip()
    plz = re.sub(r"\D", "", plz)
    if len(ort) < 3 or not plz:
        return None
    return rf"(?<![\w])(?:D[\-\s])?{plz}\s*{alternative(formen(ort))}{R}"


# Characters OCR engines confuse with each other; codes are matched tolerant to these.
_OCR_VERWECHSLER = {
    "0": "0OoDQ", "O": "O0Q", "D": "D0", "1": "1IlL|", "I": "I1l", "L": "L1", "5": "5S", "S": "S5",
    "8": "8B", "B": "B8", "2": "2Z", "Z": "Z2", "6": "6G", "G": "G6", "4": "4A", "A": "A4",
}


def _tolerant(zeichen: str) -> str:
    """Character class for one code character: both cases plus its OCR look-alikes."""
    klasse = _OCR_VERWECHSLER.get(zeichen.upper(), "")
    if not klasse and not zeichen.isalpha():
        return re.escape(zeichen)
    zeichen_menge = {*klasse, *klasse.lower(), zeichen.upper(), zeichen.lower()} - {"|"}
    inhalt = "".join(sorted(zeichen_menge)).replace("\\", "\\\\").replace("]", "\\]")
    return "[" + inhalt + ("|" if "|" in klasse else "") + "]"


def exakt_muster(wert: str) -> str | None:
    """Alphanumeric code (insurance number, patient id, file number) as written,
    tolerant to spaces inside, case and the usual OCR confusions (0/O, 1/I, 5/S, 8/B).

    Boundaries follow the character class at each end: a code starting with a
    letter may follow a digit (OCR often glues "68202938AZ 2023/171"), a code
    starting with a digit may follow a letter, and the same at the end.
    """
    wert = wert.strip()
    if len(wert) < 4:
        return None
    teile = ["".join(_tolerant(c) for c in t) for t in re.split(r"\s+", wert) if t]
    links = r"(?<![^\W\d_])" if wert[0].isalpha() else r"(?<!\d)"
    rechts = R if wert[-1].isalpha() else r"(?!\d)"
    return rf"{links}{r'\s*'.join(teile)}{rechts}"


def nummer_muster(wert: str) -> str | None:
    """A numeric id, tolerant to OCR confusions of single digits."""
    ziffern = re.sub(r"\D", "", wert)
    if len(ziffern) < 4:
        return None
    return rf"(?<![\dOoIl]){''.join(_tolerant(c) for c in ziffern)}(?![\dOoIl])"


def begriff_muster(begriff: str) -> str | None:
    """A free term from the user's lists, case-insensitive, flexible whitespace."""
    begriff = begriff.strip()
    if len(begriff) < 3:
        return None
    return rf"{L}(?i:{flex(begriff)}){R}"
