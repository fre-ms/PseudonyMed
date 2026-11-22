"""Minimal vCard 3.0/4.0 reader for the fields that identify a patient.

Handles line unfolding, escaping, parameters (quoted, multi-valued, bare
vCard 2.1 types), property groups and quoted-printable values.  Field mapping
follows what DoctoNext writes (salutation as honorific prefix in N, legal
gender in GENDER, X-MAIDENNAME, X-DOCTOLIB-PATIENT-ID) and works for hand-made
cards as well.
"""

from __future__ import annotations

import datetime as dt
import quopri
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from pseudonymed.core.model import Patient, parse_date
from pseudonymed.core.text import say

_KVNR = re.compile(r"(?<![A-Z0-9])[A-Z]\d{9}(?!\d)")
_SALUTATION_M = {"herr", "herrn", "hr.", "hr", "mr", "mr.", "sr."}
_SALUTATION_F = {"frau", "fr.", "fr", "mrs", "mrs.", "ms", "ms.", "sra."}
# Custom fields that may carry the insured-person number, in order of preference.
_INSURANCE_NUMBER_FIELDS = ("X-VERSICHERTENNUMMER", "X-KVNR", "X-INSURANCE-NUMBER")


@dataclass
class Property:
    """One content line of a card: name, parameters, raw (escaped) value and group."""

    name: str
    params: dict[str, list[str]] = field(default_factory=dict)
    value: str = ""
    group: str = ""

    @property
    def types(self) -> set[str]:
        """The TYPE parameter values in lower case (``home``, ``cell`` ...)."""
        return {t.lower() for t in self.params.get("TYPE", [])}

    @property
    def text(self) -> str:
        """The value with backslash escapes resolved."""
        return unescape(self.value)

    def parts(self) -> list[str]:
        """The structured value split at unescaped semicolons, each part unescaped."""
        return [unescape(t) for t in _split(self.value, ";")]


@dataclass
class VCard:
    """One card: its properties in file order, with lookups by property name."""

    properties: list[Property] = field(default_factory=list)

    def all_items(self, name: str) -> list[Property]:
        """All properties called ``name`` (case-insensitive), in file order."""
        name = name.upper()
        return [e for e in self.properties if e.name == name]

    def first(self, name: str) -> Property | None:
        """The first property called ``name``, or ``None``."""
        matches = self.all_items(name)
        return matches[0] if matches else None

    def value(self, name: str, standard: str = "") -> str:
        """The stripped, unescaped text of the first property ``name``, else ``standard``."""
        e = self.first(name)
        return e.text.strip() if e else standard

    @property
    def uid(self) -> str:
        """The UID property, empty when absent."""
        return self.value("UID")

    @property
    def fn(self) -> str:
        """The formatted name (FN), empty when absent."""
        return self.value("FN")

    @property
    def version(self) -> str:
        """The VERSION property (``3.0`` or ``4.0``), empty when absent."""
        return self.value("VERSION")


def unescape(value: str) -> str:
    """Resolve vCard backslash escapes: an escaped n is a newline, anything else is itself."""
    out: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split(value: str, sep: str) -> list[str]:
    """Split on ``sep`` unless it is escaped with a backslash."""
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            current.append(value[i : i + 2])
            i += 2
            continue
        if c == sep:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    parts.append("".join(current))
    return parts


def _unfold(text: str) -> list[str]:
    """Logical lines of ``text``: folded continuations joined, blank lines dropped."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _head_and_value(line: str) -> tuple[str, str]:
    """Split a content line at the first colon outside double quotes into head and value."""
    in_quotes = False
    for i, c in enumerate(line):
        if c == '"':
            in_quotes = not in_quotes
        elif c == ":" and not in_quotes:
            return line[:i], line[i + 1 :]
    return line, ""


def _param_tokens(header: str) -> list[str]:
    """Split a property head at semicolons outside double quotes; the first token is the name."""
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    for c in header:
        if c == '"':
            in_quotes = not in_quotes
        if c == ";" and not in_quotes:
            tokens.append("".join(current))
            current = []
        else:
            current.append(c)
    tokens.append("".join(current))
    return tokens


def _parameter(tokens: list[str]) -> dict[str, list[str]]:
    """Parameter tokens as ``NAME -> values``; a bare token is a vCard 2.1 type such as CELL."""
    params: dict[str, list[str]] = {}
    for token in tokens:
        if "=" in token:
            k, v = token.split("=", 1)
            values = [w.strip().strip('"') for w in v.split(",")]
        else:
            k, values = "TYPE", [token.strip().strip('"')]
        params.setdefault(k.strip().upper(), []).extend(w for w in values if w)
    return params


def _decoded(value: str, params: dict[str, list[str]]) -> str:
    """The value with quoted-printable undone when the parameters ask for it, else unchanged."""
    if not any(e.upper() == "QUOTED-PRINTABLE" for e in params.get("ENCODING", [])):
        return value
    charset = (params.get("CHARSET") or ["utf-8"])[0]
    try:
        return quopri.decodestring(value.encode("latin-1")).decode(charset, errors="replace")
    except (LookupError, ValueError):
        return value


def _property(line: str) -> Property | None:
    """One unfolded content line as a property; ``None`` for a line without a name."""
    header, value = _head_and_value(line)
    tokens = _param_tokens(header)
    name = tokens[0].strip()
    if not name:
        return None
    group = ""
    if "." in name:
        group, name = name.rsplit(".", 1)
    params = _parameter(tokens[1:])
    return Property(
        name=name.upper(), params=params, value=_decoded(value, params), group=group
    )


def parse_vcards(text: str) -> list[VCard]:
    """All cards in ``text`` (BEGIN:VCARD ... END:VCARD); lines outside a card are ignored."""
    cards: list[VCard] = []
    current_card: VCard | None = None
    for line in _unfold(text):
        e = _property(line)
        if e is None:
            continue
        if e.name == "BEGIN" and e.value.strip().upper() == "VCARD":
            current_card = VCard()
        elif e.name == "END" and e.value.strip().upper() == "VCARD":
            if current_card is not None:
                cards.append(current_card)
            current_card = None
        elif current_card is not None:
            current_card.properties.append(e)
    return cards


def load_vcf(path: str | Path) -> list[VCard]:
    """Parse the cards of a .vcf file read as UTF-8 (BOM tolerated), else as Latin-1."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return parse_vcards(text)


def parse_bday(value: str) -> dt.date | None:
    """BDAY value as a date; ``None`` when empty, unreadable or without a year (``--MMDD``)."""
    value = value.strip()
    if not value or value.startswith("--"):
        return None  # no year: useless as an identifier
    value = value.split("T")[0]
    return parse_date(value)


class _NameParts(NamedTuple):
    """Surname, first name(s) and honorific prefix as read from N or, failing that, FN."""

    surname: str
    first_name: str
    prefix: str


def _name_from_fn(fn: str) -> tuple[str, str]:
    """Surname and first names from a formatted name: the last word is the surname."""
    fn_parts = fn.split()
    if not fn_parts:
        return "", ""
    return fn_parts[-1], " ".join(fn_parts[:-1])


def _name_parts(card: VCard) -> _NameParts:
    """Name parts of the card; FN stands in when N is empty, middle names join the first name."""
    n = card.first("N")
    parts = (n.parts() + [""] * 5) if n else [""] * 5
    surname, first_name, further, prefix = (t.strip() for t in parts[:4])
    if not surname and not first_name:
        surname, first_name = _name_from_fn(card.fn)
    if further:
        first_name = f"{first_name} {further}".strip()
    return _NameParts(surname, first_name, prefix)


class _Salutation(NamedTuple):
    """Legal gender read from a salutation and the remaining prefix words as the title."""

    sex: str
    title: str


def _salutation_and_title(prefix: str) -> _Salutation:
    """Gender from a salutation token in the N prefix; the other tokens make up the title."""
    sex = ""
    title_parts: list[str] = []
    for token in filter(None, re.split(r"[\s,]+", prefix)):
        if token.lower() in _SALUTATION_M:
            sex = "m"
        elif token.lower() in _SALUTATION_F:
            sex = "w"
        else:
            title_parts.append(token)
    return _Salutation(sex, " ".join(title_parts))


def _sex(card: VCard, from_salutation: str) -> str:
    """``m``/``w`` from the salutation, else from GENDER's first component, else empty."""
    if from_salutation:
        return from_salutation
    gender = card.value("GENDER").split(";")[0].strip().upper()
    return {"M": "m", "F": "w"}.get(gender, "")


def _phone(value: str) -> str:
    """A TEL value without the ``tel:`` URI scheme."""
    value = value.strip()
    if value.lower().startswith("tel:"):
        value = value[4:]
    return value


def _phones(card: VCard) -> list[str]:
    """Distinct phone numbers of the card with at least five digits, in file order."""
    numbers = (_phone(t.text) for t in card.all_items("TEL"))
    return list(dict.fromkeys(n for n in numbers if len(re.sub(r"\D", "", n)) >= 5))


def _emails(card: VCard) -> list[str]:
    """Distinct lower-case e-mail addresses (``mailto:`` dropped), in file order."""
    addresses = (e.text.strip().removeprefix("mailto:").lower() for e in card.all_items("EMAIL"))
    return list(dict.fromkeys(a for a in addresses if "@" in a))


def _address(card: VCard) -> tuple[str, str, str]:
    """Street, postcode and town of the home address (else the first ADR).

    The extended-address part stands in for an empty street; only the first
    line of a multi-line street is kept.
    """
    addresses = card.all_items("ADR")
    if not addresses:
        return "", "", ""
    selected = next((a for a in addresses if "home" in a.types), addresses[0])
    parts = selected.parts() + [""] * 7
    street = parts[2].split("\n")[0].strip()
    if not street and parts[1].strip():
        street = parts[1].split("\n")[0].strip()
    return street, parts[5].strip(), parts[3].strip()


def _notes(card: VCard) -> list[str]:
    """Non-empty NOTE texts plus the ROLE as a ``Beruf:`` line, kept for display."""
    notes = [h.text.strip() for h in card.all_items("NOTE") if h.text.strip()]
    role = card.value("ROLE")
    if role:
        notes.append(say(f"Beruf: {role}", f"Occupation: {role}"))
    return notes


def _insurance_number(card: VCard, notes: list[str]) -> str:
    """The insured-person number from a custom X field, else the first KVNR in the notes."""
    for name in _INSURANCE_NUMBER_FIELDS:
        if card.value(name):
            return card.value(name)
    for note in notes:
        m = _KVNR.search(note)
        if m:
            return m.group(0)
    return ""


def patient_from_vcard(card: VCard) -> Patient:
    """Derive the identifiers of one card; unknown fields are ignored."""
    name = _name_parts(card)
    salutation = _salutation_and_title(name.prefix)
    street, postcode, place = _address(card)
    notes = _notes(card)
    return Patient(
        surname=name.surname,
        first_name=name.first_name,
        sex=_sex(card, salutation.sex),
        title=salutation.title,
        birth_name=card.value("X-MAIDENNAME"),
        birth_date=parse_bday(card.value("BDAY")),
        street=street,
        postcode=postcode,
        place=place,
        phones=_phones(card),
        emails=_emails(card),
        insurance_number=_insurance_number(card, notes),
        patient_id=card.value("X-DOCTOLIB-PATIENT-ID"),
        notes=notes,
    )


def patients_from_vcf(path: str | Path) -> list[Patient]:
    """Patients derived from every card in the file ``path``, in file order."""
    return [patient_from_vcard(k) for k in load_vcf(path)]
