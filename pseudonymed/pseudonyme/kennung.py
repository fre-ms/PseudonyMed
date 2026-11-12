"""Reproducible pseudonym codes.

Instead of a running letter, the main person's pseudonym can be derived from
the patient's identity with a keyed hash (HMAC-SHA256): the same person yields
the same code on every machine that holds the same secret key, and without
the key nobody can enumerate names to find a code.  The identity is
canonicalised first (Kölner Phonetik of the names, ISO date of birth), so
spelling variants of a name land on the same code.  The code uses a
32-character alphabet without look-alikes and ends with a check character
(Luhn mod 32) that catches every single-character error and almost every
transposition of neighbouring characters, so that a code mangled by the
language model is noticed on re-identification.

This follows the idea of the Mainzelliste's PID generator (Faldum &
Pommerening 2005) in spirit, not in construction: their PID encrypts a
counter and corrects errors; ours is a keyed hash and only detects them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path

from pseudonymed.kern.modell import Patient, normalisiere
from pseudonymed.pseudonyme.phonetik import koelner_phonetik

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O/1/I
VERBOTENE_ANFAENGE = ("AZ",)  # a code must never look like the label of a file number
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
STANDARDLAENGE = 5  # payload characters; one check character is appended
_PARTIKEL = {"von", "van", "der", "den", "de", "zu", "zur", "zum", "da", "dos", "del", "la", "le"}
MUSTER_KENNUNG = re.compile(r"[A-Z2-9]{4,9}")
_STRASSEN_SUFFIX = re.compile(r"[\s\-]?(stra(?:ß|ss)e|str\.?)$", re.IGNORECASE)
_HAUSNUMMER = re.compile(r"^(.*?)[\s,]*(\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?\s*$")
_ORTSPARTIKEL = {"am", "an", "a", "d", "der", "dem", "den", "im", "in", "ob", "bei", "b", "vor",
                 "auf", "unter", "ot", "ortsteil", "stadt", "bad"}


def pruefzeichen(code: str) -> str:
    """Luhn mod N check character for ``code`` (payload only)."""
    faktor = 2
    summe = 0
    n = len(ALPHABET)
    for c in reversed(code):
        wert = _INDEX[c] * faktor
        summe += wert // n + wert % n
        faktor = 1 if faktor == 2 else 2
    rest = summe % n
    return ALPHABET[(n - rest) % n]


def kennung_gueltig(kennung: str) -> bool:
    """True when the check character of a hash code is consistent."""
    if len(kennung) < 2 or any(c not in _INDEX for c in kennung):
        return False
    return pruefzeichen(kennung[:-1]) == kennung[-1]


def ist_hashkennung(kennung: str) -> bool:
    """True for an identifier of the hash scheme (four or more code characters)."""
    return len(kennung) >= 4 and MUSTER_KENNUNG.fullmatch(kennung) is not None


def hauptteil(nachname: str) -> str:
    """The longest part of a compound surname, particles ignored."""
    teile = [t for t in re.split(r"[\s\-‐‑–]+", nachname.strip()) if t]
    teile = [t for t in teile if t.lower() not in _PARTIKEL] or teile
    return max(teile, key=len) if teile else ""


def kanonische_identitaet(patient: Patient) -> str:
    """Phonetic surname | phonetic first name | ISO date of birth.

    Without a date of birth the normalised names are used instead of the
    phonetic codes, because phonetic codes alone would merge different people.
    """
    nachname = hauptteil(patient.nachname)
    vornamen = patient.vornamen
    vorname = vornamen[0] if vornamen else ""
    if patient.geburtsdatum is None:
        return f"{normalisiere(nachname)}|{normalisiere(vorname)}|"
    datum = patient.geburtsdatum.isoformat()
    return f"{koelner_phonetik(nachname)}|{koelner_phonetik(vorname)}|{datum}"


def hash_kennung(geheimnis: bytes, identitaet: str, laenge: int = STANDARDLAENGE,
                 variante: int = 0) -> str:
    """Keyed hash of ``identitaet`` rendered as ``laenge`` characters plus a check character.

    ``variante`` > 0 derives an alternative code for the rare case that two
    different people share a canonical identity.
    """
    nachricht = identitaet if variante == 0 else f"{identitaet}#{variante}"
    while True:
        digest = hmac.new(geheimnis, nachricht.encode("utf-8"), hashlib.sha256).digest()
        bits = int.from_bytes(digest, "big")
        zeichen = []
        for _ in range(laenge):
            zeichen.append(ALPHABET[bits & 31])
            bits >>= 5
        code = "".join(zeichen)
        if not code.startswith(VERBOTENE_ANFAENGE):
            return code + pruefzeichen(code)
        nachricht += "~"  # would read like a file-number label ("AZ 2023/1"): derive again


def geheimnis_laden(pfad: Path, erzeugen: bool = True) -> bytes:
    """Read the secret key (hex, one line) or create a new random one (0600)."""
    if pfad.exists():
        text = pfad.read_text(encoding="utf-8").strip()
        try:
            return bytes.fromhex(text)
        except ValueError as e:
            raise ValueError(f"Pseudonymschlüssel in {pfad} ist nicht lesbar.") from e
    if not erzeugen:
        raise FileNotFoundError(pfad)
    geheimnis = secrets.token_bytes(32)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(geheimnis.hex() + "\n", encoding="utf-8")
    try:
        pfad.chmod(0o600)
    except OSError:
        pass
    return geheimnis


def trenne_hausnummer(strasse: str) -> tuple[str, str]:
    """"Musterstraße 12 a" -> ("Musterstraße", "12a"); number may be absent."""
    m = _HAUSNUMMER.match(strasse.strip())
    if not m:
        return strasse.strip(), ""
    name = (m.group(1) or "").strip(" ,")
    nummer = re.sub(r"\s+", "", (m.group(2) or "")).lower()
    return name, nummer


def kanonische_strasse(strasse: str) -> str:
    """Phonetic street name with the "straße/strasse/str." suffix normalised, plus house number.

    "Musterstraße 12", "Musterstr. 12" and "Muster-Strasse 12" all give the same form;
    "Lindenweg 5" keeps its suffix inside the phonetic code.
    """
    name, nummer = trenne_hausnummer(strasse)
    suffix = ""
    m = _STRASSEN_SUFFIX.search(name)
    if m and name[: m.start()].strip(" -"):
        name, suffix = name[: m.start()].strip(" -"), "str"
    # phonetic code of the words run together, so that "Linden-Weg" equals "Lindenweg"
    woerter = [w for w in re.split(r"[\s\-‐‑–.]+", name) if w]
    return f"strasse:{koelner_phonetik(''.join(woerter))}:{suffix}:{nummer}"


def kanonischer_ort(ort: str) -> str:
    """Phonetic town name, particles ("am", "an der", "Bad", "OT") ignored.

    "München" and "Muenchen" agree; "Frankfurt am Main" and "Frankfurt (Oder)" differ.
    """
    woerter = [w for w in re.split(r"[\s\-‐‑–./()]+", ort) if w]
    kern = [w for w in woerter if w.lower().rstrip(".") not in _ORTSPARTIKEL] or woerter
    return "ort:" + koelner_phonetik("".join(kern))
