"""Put the clear data back into text a language model produced from the
pseudonymised document."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pseudonymed.kern.modell import Person
from pseudonymed.pseudonyme.kennung import ist_hashkennung, kennung_gueltig

_KENNUNG = r"(?:[A-Z]{1,3}|[A-Z2-9]{4,9})"
_PATIENT = re.compile(rf"^(Patient|Patientin) ({_KENNUNG})$")
_PERSON = re.compile(rf"^Person ({_KENNUNG})$")
_ANGEHOERIGE = re.compile(rf"^Angehörige ({_KENNUNG})-(\d+)$")
_ZUWEISER = re.compile(r"^Dr\. Z(\d+)$")
_KLAMMER = re.compile(r"^\[(.+)\]$")

_REST_MUSTER = [
    re.compile(rf"\bPatient(?:in|en)?\s+{_KENNUNG}(?![\w])"),
    re.compile(rf"\bPerson\s+{_KENNUNG}(?![\w])"),
    re.compile(rf"\bAngehörige[nr]?\s+{_KENNUNG}-\d+(?!\d)"),
    re.compile(r"\bDr\.\s*Z\d+(?!\d)"),
    re.compile(r"\[(?:geb\.|Alter|Wohnort|Straße|Strasse|PLZ Ort|Ort|Telefon|E-Mail|"
               r"Versichertennummer|Patienten-ID|Aktenzeichen|IBAN|Begriff|Geburtsdatum|"
               r"Praxisanschrift)[^\]\n]{0,30}\]"),
]


@dataclass
class Rueckersetzung:
    """The re-identified text, how many pseudonyms were replaced, and what was left."""

    text: str
    ersetzt: int = 0
    verbleibend: list[str] = field(default_factory=list)
    hinweise: dict[str, str] = field(default_factory=dict)  # leftover -> what is wrong with it


def _person_aus(info: dict[str, Any] | None) -> Person | None:
    """The patient as a :class:`Person` from a store row or capsule info; ``None`` without."""
    if not info:
        return None
    return Person(
        nachname=info.get("nachname", ""),
        vorname=info.get("vorname", ""),
        geschlecht=info.get("geschlecht", ""),
        titel=info.get("titel", ""),
    )


def _ersetzer(pseudonym: str, klartext: str, person: Person | None, form: str):
    """Return (pattern, replacement function) for one pseudonym."""
    m = _PATIENT.match(pseudonym)
    if m:
        wort, b = m.groups()
        muster = re.compile(rf"\b(?:Patient(in|en)?|Pat\.)\s+{re.escape(b)}(?![\w])")

        def ersetze(t: re.Match[str]) -> str:
            """The full name, or "Herr/Herrn/Frau Nachname" by case when asked and known."""
            if form == "anrede" and person is not None and (person.weiblich or person.maennlich):
                kasus = "obliquus" if t.group(1) == "en" else "nominativ"
                return person.anredeform(kasus)
            return klartext.split("; ")[0]

        return muster, ersetze
    m = _PERSON.match(pseudonym)
    if m:
        code = m.group(1)
        return re.compile(rf"\bPerson\s+{re.escape(code)}(?![\w])"), lambda t: klartext
    m = _ANGEHOERIGE.match(pseudonym)
    if m:
        b, n = m.groups()
        return re.compile(rf"\bAngehörige[nr]?\s+{re.escape(b)}-?{n}(?!\d)"), lambda t: klartext
    m = _ZUWEISER.match(pseudonym)
    if m:
        n = m.group(1)
        return re.compile(rf"(?:\bDr\.\s*)?(?<![\w])Z{n}(?!\d)"), lambda t: klartext
    m = _KLAMMER.match(pseudonym)
    if m:
        teile = [re.escape(t) for t in m.group(1).split()]
        return re.compile(r"\[\s*" + r"\s+".join(teile) + r"\s*\]"), lambda t: klartext
    teile = [re.escape(t) for t in pseudonym.split()]
    return re.compile(r"(?<![\w])" + r"\s+".join(teile) + r"(?![\w])"), lambda t: klartext


def _abstand_eins(a: str, b: str) -> bool:
    """One substitution or one transposition of neighbours apart."""
    if len(a) != len(b):
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] \
        and a[diff[1]] == b[diff[0]]


def rueckersetzen(
    text: str,
    zuordnung: dict[str, str],
    info: dict[str, Any] | None = None,
    form: str = "vollname",
    bekannte_kennungen: list[str] | None = None,
) -> Rueckersetzung:
    """Replace every known pseudonym in ``text`` by its clear data.

    ``form`` is "vollname" (always the full name) or "anrede" ("Herr/Herrn/Frau
    Nachname" by grammatical case when the gender is known).  Pseudonyms that
    look like ours but are not in the mapping are reported in ``verbleibend``;
    for hash codes the check character tells whether the model mangled the
    code, and a stored code one edit away is suggested.
    """
    text, ersetzt = _ersetze_alle(text, zuordnung, _person_aus(info), form)
    verbleibend = _verbleibende(text)
    hinweise = _hinweise_zu(verbleibend, bekannte_kennungen or [])
    return Rueckersetzung(text=text, ersetzt=ersetzt, verbleibend=verbleibend, hinweise=hinweise)


def _ersetze_alle(
    text: str, zuordnung: dict[str, str], person: Person | None, form: str
) -> tuple[str, int]:
    """The replacement pass: every pseudonym in its inflected forms, longest pseudonyms
    first so that a shorter one never eats part of a longer one; returns the text and
    the number of replacements."""
    ersetzt = 0
    for pseudonym in sorted(zuordnung, key=len, reverse=True):
        muster, ersetze = _ersetzer(pseudonym, zuordnung[pseudonym], person, form)
        text, n = muster.subn(ersetze, text)
        ersetzt += n
    return text, ersetzt


def _verbleibende(text: str) -> list[str]:
    """Leftovers that look like our pseudonyms, each once, in order of first discovery."""
    return list(dict.fromkeys(m.group(0) for muster in _REST_MUSTER
                              for m in muster.finditer(text)))


def _hinweise_zu(verbleibend: list[str], bekannte: list[str]) -> dict[str, str]:
    """What is wrong with each leftover whose code is a hash code (silent otherwise)."""
    return {rest: hinweis for rest in verbleibend if (hinweis := _hinweis(rest, bekannte))}


def _hinweis(rest: str, bekannte: list[str]) -> str:
    """The hint for one leftover: a wrong check character means the model mangled the
    code, and a stored code one edit away is suggested; an empty string when the code
    is not a hash code or nothing can be said."""
    code = rest.strip("[]").split()[-1].split("-")[0]
    if not ist_hashkennung(code):
        return ""
    aehnlich = [k for k in bekannte if _abstand_eins(code, k)]
    vorschlag = aehnlich[0] if len(aehnlich) == 1 else ""
    if not kennung_gueltig(code):
        zusatz = f"; gemeint wohl {vorschlag}" if vorschlag else ""
        return "Prüfzeichen falsch, vermutlich verschrieben" + zusatz
    return f"unbekannt; ähnlich: {vorschlag}" if vorschlag else ""
