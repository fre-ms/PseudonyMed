"""Put the clear data back into text a language model produced from the
pseudonymised document."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pseudonymed.core.model import Person
from pseudonymed.core.text import say
from pseudonymed.pseudonyms.patient_code import is_hash_code, is_valid_code

_CODE = r"(?:[A-Z]{1,3}|[A-Z2-9]{4,9})"
_PATIENT = re.compile(rf"^(Patient|Patientin) ({_CODE})$")
_PERSON = re.compile(rf"^Person ({_CODE})$")
_RELATIVES = re.compile(rf"^Angehörige ({_CODE})-(\d+)$")
_REFERRERS = re.compile(r"^Dr\. Z(\d+)$")
_BRACKET = re.compile(r"^\[(.+)\]$")

_REST_PATTERN = [
    re.compile(rf"\bPatient(?:in|en)?\s+{_CODE}(?![\w])"),
    re.compile(rf"\bPerson\s+{_CODE}(?![\w])"),
    re.compile(rf"\bAngehörige[nr]?\s+{_CODE}-\d+(?!\d)"),
    re.compile(r"\bDr\.\s*Z\d+(?!\d)"),
    re.compile(r"\[(?:geb\.|Alter|Wohnort|Straße|Strasse|PLZ Ort|Ort|Telefon|E-Mail|"
               r"Versichertennummer|Patienten-ID|Aktenzeichen|IBAN|Begriff|Geburtsdatum|"
               r"Absenderanschrift)[^\]\n]{0,30}\]"),
]


@dataclass
class Reidentification:
    """The re-identified text, how many pseudonyms were replaced, and what was left."""

    text: str
    replaced: int = 0
    remaining: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)  # leftover -> what is wrong with it


def _person_from(info: dict[str, Any] | None) -> Person | None:
    """The patient as a :class:`Person` from a store row or capsule info; ``None`` without."""
    if not info:
        return None
    return Person(
        surname=info.get("surname", ""),
        first_name=info.get("first_name", ""),
        sex=info.get("sex", ""),
        title=info.get("title", ""),
    )


def _replacer(pseudonym: str, plaintext: str, person: Person | None, form: str):
    """Return (pattern, replacement function) for one pseudonym."""
    m = _PATIENT.match(pseudonym)
    if m:
        word, b = m.groups()
        pattern = re.compile(rf"\b(?:Patient(in|en)?|Pat\.)\s+{re.escape(b)}(?![\w])")

        def replace(t: re.Match[str]) -> str:
            """The full name, or "Herr/Herrn/Frau Nachname" by case when asked and known."""
            if form == "salutation" and person is not None and (person.female or person.male):
                grammatical_case = "oblique" if t.group(1) == "en" else "nominative"
                return person.salutation_form(grammatical_case)
            return plaintext.split("; ")[0]

        return pattern, replace
    m = _PERSON.match(pseudonym)
    if m:
        code = m.group(1)
        return re.compile(rf"\bPerson\s+{re.escape(code)}(?![\w])"), lambda t: plaintext
    m = _RELATIVES.match(pseudonym)
    if m:
        b, n = m.groups()
        return re.compile(rf"\bAngehörige[nr]?\s+{re.escape(b)}-?{n}(?!\d)"), lambda t: plaintext
    m = _REFERRERS.match(pseudonym)
    if m:
        n = m.group(1)
        return re.compile(rf"(?:\bDr\.\s*)?(?<![\w])Z{n}(?!\d)"), lambda t: plaintext
    m = _BRACKET.match(pseudonym)
    if m:
        parts = [re.escape(t) for t in m.group(1).split()]
        return re.compile(r"\[\s*" + r"\s+".join(parts) + r"\s*\]"), lambda t: plaintext
    parts = [re.escape(t) for t in pseudonym.split()]
    return re.compile(r"(?<![\w])" + r"\s+".join(parts) + r"(?![\w])"), lambda t: plaintext


def _one_edit_apart(a: str, b: str) -> bool:
    """One substitution or one transposition of neighbours apart."""
    if len(a) != len(b):
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] \
        and a[diff[1]] == b[diff[0]]


def reidentify(
    text: str,
    mapping: dict[str, str],
    info: dict[str, Any] | None = None,
    form: str = "full-name",
    known_codes: list[str] | None = None,
) -> Reidentification:
    """Replace every known pseudonym in ``text`` by its clear data.

    ``form`` is "full-name" (always the full name) or "salutation" ("Herr/Herrn/Frau
    Nachname" by grammatical case when the gender is known).  Pseudonyms that
    look like ours but are not in the mapping are reported in ``remaining``;
    for hash codes the check character tells whether the model mangled the
    code, and a stored code one edit away is suggested.
    """
    text, replaced = _replace_all(text, mapping, _person_from(info), form)
    remaining = _leftovers(text)
    notes = _notes_for(remaining, known_codes or [])
    return Reidentification(text=text, replaced=replaced, remaining=remaining, notes=notes)


def _replace_all(
    text: str, mapping: dict[str, str], person: Person | None, form: str
) -> tuple[str, int]:
    """The replacement pass: every pseudonym in its inflected forms, longest pseudonyms
    first so that a shorter one never eats part of a longer one; returns the text and
    the number of replacements."""
    replaced = 0
    for pseudonym in sorted(mapping, key=len, reverse=True):
        pattern, replace = _replacer(pseudonym, mapping[pseudonym], person, form)
        text, n = pattern.subn(replace, text)
        replaced += n
    return text, replaced


def _leftovers(text: str) -> list[str]:
    """Leftovers that look like our pseudonyms, each once, in order of first discovery."""
    return list(dict.fromkeys(m.group(0) for pattern in _REST_PATTERN
                              for m in pattern.finditer(text)))


def _notes_for(remaining: list[str], known: list[str]) -> dict[str, str]:
    """What is wrong with each leftover whose code is a hash code (silent otherwise)."""
    return {rest: note for rest in remaining if (note := _note(rest, known))}


def _note(rest: str, known: list[str]) -> str:
    """The hint for one leftover: a wrong check character means the model mangled the
    code, and a stored code one edit away is suggested; an empty string when the code
    is not a hash code or nothing can be said."""
    code = rest.strip("[]").split()[-1].split("-")[0]
    if not is_hash_code(code):
        return ""
    similar = [k for k in known if _one_edit_apart(code, k)]
    suggestion = similar[0] if len(similar) == 1 else ""
    if not is_valid_code(code):
        extra, extra_en = (f"; gemeint wohl {suggestion}", f"; probably {suggestion}") \
            if suggestion else ("", "")
        return say("Prüfzeichen falsch, vermutlich verschrieben" + extra,
                   "Check character wrong, probably a typing error" + extra_en)
    if not suggestion:
        return ""
    return say(f"unbekannt; ähnlich: {suggestion}", f"unknown; similar: {suggestion}")
