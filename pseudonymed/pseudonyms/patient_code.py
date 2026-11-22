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

from pseudonymed.core.model import Patient, normalise
from pseudonymed.pseudonyms.phonetics import cologne_phonetics

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 symbols, no 0/O/1/I
FORBIDDEN_PREFIXES = ("AZ",)  # a code must never look like the label of a file number
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
DEFAULT_LENGTH = 5  # payload characters; one check character is appended
_PARTICLES = {"von", "van", "der", "den", "de", "zu", "zur", "zum", "da", "dos", "del", "la", "le"}
CODE_PATTERN = re.compile(r"[A-Z2-9]{4,9}")
_STREET_SUFFIX = re.compile(r"[\s\-]?(stra(?:ß|ss)e|str\.?)$", re.IGNORECASE)
_HOUSE_NUMBER = re.compile(r"^(.*?)[\s,]*(\d+\s?[a-zA-Z]?(?:\s?[-/]\s?\d+\s?[a-zA-Z]?)?)?\s*$")
_PLACE_PARTICLES = {"am", "an", "a", "d", "der", "dem", "den", "im", "in", "ob", "bei", "b", "vor",
                    "auf", "unter", "ot", "ortsteil", "stadt", "bad"}


def check_character(code: str) -> str:
    """Luhn mod N check character for ``code`` (payload only)."""
    factor = 2
    sum_total = 0
    n = len(ALPHABET)
    for c in reversed(code):
        value = _INDEX[c] * factor
        sum_total += value // n + value % n
        factor = 1 if factor == 2 else 2
    rest = sum_total % n
    return ALPHABET[(n - rest) % n]


def is_valid_code(code: str) -> bool:
    """True when the check character of a hash code is consistent."""
    if len(code) < 2 or any(c not in _INDEX for c in code):
        return False
    return check_character(code[:-1]) == code[-1]


def is_hash_code(code: str) -> bool:
    """True for an identifier of the hash scheme (four or more code characters)."""
    return len(code) >= 4 and CODE_PATTERN.fullmatch(code) is not None


def main_part(surname: str) -> str:
    """The longest part of a compound surname, particles ignored."""
    parts = [t for t in re.split(r"[\s\-‐‑–]+", surname.strip()) if t]
    parts = [t for t in parts if t.lower() not in _PARTICLES] or parts
    return max(parts, key=len) if parts else ""


def canonical_identity(patient: Patient) -> str:
    """Phonetic surname | phonetic first name | ISO date of birth.

    Without a date of birth the normalised names are used instead of the
    phonetic codes, because phonetic codes alone would merge different people.
    """
    surname = main_part(patient.surname)
    first_names = patient.first_names
    first_name = first_names[0] if first_names else ""
    if patient.birth_date is None:
        return f"{normalise(surname)}|{normalise(first_name)}|"
    date = patient.birth_date.isoformat()
    return f"{cologne_phonetics(surname)}|{cologne_phonetics(first_name)}|{date}"


def hash_code(secret: bytes, identity: str, length: int = DEFAULT_LENGTH,
              variant: int = 0) -> str:
    """Keyed hash of ``identity`` rendered as ``length`` characters plus a check character.

    ``variant`` > 0 derives an alternative code for the rare case that two
    different people share a canonical identity.
    """
    message = identity if variant == 0 else f"{identity}#{variant}"
    while True:
        digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
        bits = int.from_bytes(digest, "big")
        chars = []
        for _ in range(length):
            chars.append(ALPHABET[bits & 31])
            bits >>= 5
        code = "".join(chars)
        if not code.startswith(FORBIDDEN_PREFIXES):
            return code + check_character(code)
        message += "~"  # would read like a file-number label ("AZ 2023/1"): derive again


def load_secret(path: Path, create: bool = True) -> bytes:
    """Read the secret key (hex, one line) or create a new random one (0600)."""
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        try:
            return bytes.fromhex(text)
        except ValueError as e:
            raise ValueError(f"Pseudonymschlüssel in {path} ist nicht lesbar.") from e
    if not create:
        raise FileNotFoundError(path)
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret.hex() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def split_house_number(street: str) -> tuple[str, str]:
    """"Musterstraße 12 a" -> ("Musterstraße", "12a"); number may be absent."""
    m = _HOUSE_NUMBER.match(street.strip())
    if not m:
        return street.strip(), ""
    name = (m.group(1) or "").strip(" ,")
    number = re.sub(r"\s+", "", (m.group(2) or "")).lower()
    return name, number


def canonical_street(street: str) -> str:
    """Phonetic street name with the "straße/strasse/str." suffix normalised, plus house number.

    "Musterstraße 12", "Musterstr. 12" and "Muster-Strasse 12" all give the same form;
    "Lindenweg 5" keeps its suffix inside the phonetic code.
    """
    name, number = split_house_number(street)
    suffix = ""
    m = _STREET_SUFFIX.search(name)
    if m and name[: m.start()].strip(" -"):
        name, suffix = name[: m.start()].strip(" -"), "str"
    # phonetic code of the words run together, so that "Linden-Weg" equals "Lindenweg"
    words = [w for w in re.split(r"[\s\-‐‑–.]+", name) if w]
    return f"strasse:{cologne_phonetics(''.join(words))}:{suffix}:{number}"


def canonical_place(place: str) -> str:
    """Phonetic town name, particles ("am", "an der", "Bad", "OT") ignored.

    "München" and "Muenchen" agree; "Frankfurt am Main" and "Frankfurt (Oder)" differ.
    """
    words = [w for w in re.split(r"[\s\-‐‑–./()]+", place) if w]
    core = [w for w in words if w.lower().rstrip(".") not in _PLACE_PARTICLES] or words
    return "ort:" + cologne_phonetics("".join(core))
