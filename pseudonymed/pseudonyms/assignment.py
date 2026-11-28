"""Assign pseudonyms for one patient and remember what they stand for.

The main person becomes "Patient K7Q4MX" (or "Patientin …"; with the letter
scheme "Patient A"), relatives "Angehörige K7Q4MX-1", referring physicians
"Dr. Z1"; other identifiers become bracketed placeholders such as
"[geb. 1990]".  Streets and towns get their own reproducible codes derived
like the patient's identifier ("[Straße K7Q4MX]", "[PLZ Ort 2F7QV4]",
"[Ort 2F7QV4]"); without a secret key they fall back to "[Wohnort]".  The
identifier is handed in by the store so that it stays stable across
documents, sessions and machines.
"""

from __future__ import annotations

import datetime as dt

from pseudonymed.core.model import Hit, Kind, Patient, Person, normalise
from pseudonymed.pseudonyms.patient_code import canonical_place, canonical_street, hash_code

PLACEHOLDERS: dict[Kind, str] = {
    Kind.ADDRESS: "[Wohnort]",
    Kind.PLACE: "[Wohnort]",
    Kind.PHONE: "[Telefon]",
    Kind.EMAIL: "[E-Mail]",
    Kind.INSURANCE_NUMBER: "[Versichertennummer]",
    Kind.PATIENT_ID: "[Patienten-ID]",
    Kind.CASE_NUMBER: "[Aktenzeichen]",
    Kind.IBAN: "[IBAN]",
}


class PseudonymAssignment:
    """Pseudonyms for one patient plus the mapping back to clear data."""

    def __init__(
        self,
        patient: Patient,
        code: str,
        date_format: str = "year",
        today: dt.date | None = None,
        secret: bytes | None = None,
    ) -> None:
        self.patient = patient
        self.code = code
        self.secret = secret
        self.date_format = date_format
        self.today = today or dt.date.today()
        self.mapping: dict[str, str] = {}
        self.kinds: dict[str, str] = {}

    # -- bookkeeping ---------------------------------------------------------------------------
    def _remember(self, pseudonym: str, plaintext: str, kind: Kind) -> str:
        plaintext = plaintext.strip()
        if plaintext:
            old = self.mapping.get(pseudonym)
            if old is None:
                self.mapping[pseudonym] = plaintext
            elif plaintext not in old.split("; "):
                self.mapping[pseudonym] = f"{old}; {plaintext}"
        self.kinds.setdefault(pseudonym, str(kind))
        return pseudonym

    def register_matches(self, hits: list[Hit]) -> None:
        """Record the clear text of generic hits (e-mail, insurance number, IBAN)."""
        for f in hits:
            if f.pseudonym not in self.mapping or f.rule.startswith("pattern."):
                self._remember(f.pseudonym, " ".join(f.text.split()), f.kind)

    # -- the main person -----------------------------------------------------------------------
    def main_person(self) -> str:
        """The patient's own pseudonym, "Patient" or "Patientin" with the identifier."""
        word = "Patientin" if self.patient.female else "Patient"
        return self._remember(f"{word} {self.code}", self.patient.full_name, Kind.NAME)

    def birth_date(self) -> str:
        """The birth-date pseudonym: the year or the age, as the options say."""
        d = self.patient.birth_date
        if d is None:
            return "[Geburtsdatum]"
        if self.date_format == "age":
            not_yet = (self.today.month, self.today.day) < (d.month, d.day)
            age = self.today.year - d.year - not_yet
            pseudonym = f"[Alter {age}]"
        else:
            pseudonym = f"[geb. {d.year}]"
        return self._remember(pseudonym, d.strftime("%d.%m.%Y"), Kind.BIRTH_DATE)

    # -- addresses: one code per street and per town ----------------------------------------------
    def _address_code(self, identity: str) -> str:
        assert self.secret is not None
        return hash_code(self.secret, identity)

    def street(self, person: Person | None = None) -> str:
        """The street pseudonym of ``person`` (the patient without one): a code, or the
        placeholder."""
        person = person or self.patient
        if self.secret is None or not person.street:
            own = person is self.patient
            placeholder = PLACEHOLDERS[Kind.ADDRESS] if own else "[Absenderanschrift]"
            return self._remember(placeholder, person.street, Kind.ADDRESS)
        code = self._address_code(canonical_street(person.street))
        return self._remember(f"[Straße {code}]", person.street, Kind.ADDRESS)

    def place(self, person: Person | None = None) -> str:
        """The town pseudonym of ``person`` (the patient without one)."""
        person = person or self.patient
        if self.secret is None or not person.place:
            return self._remember(PLACEHOLDERS[Kind.PLACE], person.place, Kind.PLACE)
        code = self._address_code(canonical_place(person.place))
        return self._remember(f"[Ort {code}]", person.place, Kind.PLACE)

    def postcode_place(self, person: Person | None = None) -> str:
        """The pseudonym of postcode and town together."""
        person = person or self.patient
        plaintext = f"{person.postcode} {person.place}".strip()
        if self.secret is None or not person.place:
            return self._remember(PLACEHOLDERS[Kind.PLACE], plaintext, Kind.PLACE)
        code = self._address_code(canonical_place(person.place))
        return self._remember(f"[PLZ Ort {code}]", plaintext, Kind.PLACE)

    def address(self) -> str:
        """Compatibility: the street placeholder of the main person."""
        return self.street()

    def phone(self) -> str:
        """The placeholder for the patient's phone numbers."""
        return self._remember(PLACEHOLDERS[Kind.PHONE], "; ".join(self.patient.phones), Kind.PHONE)

    def email(self) -> str:
        """The placeholder for the patient's e-mail addresses."""
        return self._remember(PLACEHOLDERS[Kind.EMAIL], "; ".join(self.patient.emails), Kind.EMAIL)

    def insurance_number(self) -> str:
        """The placeholder for the insurance number."""
        return self._remember(PLACEHOLDERS[Kind.INSURANCE_NUMBER], self.patient.insurance_number,
                              Kind.INSURANCE_NUMBER)

    def patient_id(self) -> str:
        """The placeholder for the patient id."""
        return self._remember(PLACEHOLDERS[Kind.PATIENT_ID], self.patient.patient_id,
                              Kind.PATIENT_ID)

    def case_numbers(self) -> str:
        """The placeholder for the file numbers."""
        return self._remember(PLACEHOLDERS[Kind.CASE_NUMBER], "; ".join(self.patient.case_numbers),
                              Kind.CASE_NUMBER)

    def generic(self, kind: Kind) -> str:
        """Placeholder for a generic pattern hit; the clear text is recorded per hit."""
        return PLACEHOLDERS.get(kind, f"[{kind}]")

    # -- people around the patient -------------------------------------------------------------
    def relatives(self, number: int, person: Person) -> str:
        """The pseudonym of the ``number``-th relative, derived from the patient's identifier."""
        return self._remember(f"Angehörige {self.code}-{number}", person.full_name, Kind.NAME)

    def referrers(self, number: int, person: Person) -> str:
        """The pseudonym of the ``number``-th referring doctor."""
        plaintext = " ".join(t for t in (person.title, person.full_name) if t)
        return self._remember(f"Dr. Z{number}", plaintext, Kind.NAME)

    def term(self, number: int, text: str) -> str:
        """The pseudonym of the ``number``-th term the user listed."""
        return self._remember(f"[Begriff {number}]", text, Kind.TERM)

    # -- identifiers from the lexicon: professions, hospitals, studies, titles ---------------------
    def identifier(self, prefix: str, text: str, kind: Kind = Kind.IDENTIFIER) -> str:
        """An identifier of the identifiers step (or a medication or lab value): "[Klinik
        <code>]" from the keyed hash of the class and the normalised text, so that the same
        hospital gets the same code."""
        text = " ".join(text.split())
        if self.secret is None:
            return self._remember(f"[{prefix}]", text, kind)
        code = hash_code(self.secret, f"{prefix.casefold()}:" + normalise(text))
        return self._remember(f"[{prefix} {code}]", text, kind)

    # -- people and places found by NER ----------------------------------------------------------
    def unknown_person(self, name: str) -> str:
        """A person the rules did not know: "Person <code>" from the keyed hash of the name."""
        if self.secret is None:
            return self._remember("[Person]", name, Kind.NAME)
        code = hash_code(self.secret, "person:" + normalise(name))
        return self._remember(f"Person {code}", name, Kind.NAME)

    def unknown_place(self, place: str) -> str:
        """A place the rules did not know, coded like the address places."""
        if self.secret is None:
            return self._remember(PLACEHOLDERS[Kind.PLACE], place, Kind.PLACE)
        code = self._address_code(canonical_place(place))
        return self._remember(f"[Ort {code}]", place, Kind.PLACE)
