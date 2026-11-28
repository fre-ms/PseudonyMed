"""Core data model: persons, identifiers, hits and the audit report.

A :class:`Patient` says *what* must disappear from a document, a
:class:`Hit` says *where* a rule matched, and the :class:`Report`
records what was done.  The report deliberately never repeats clear data, so it
may travel with the pseudonymised file.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pseudonymed.core.stored import as_stored


class Kind(StrEnum):
    """Kind of identifier; reports and the pseudonym store keep the value."""

    NAME = "name"
    FIRST_NAME = "first_name"
    SURNAME = "surname"
    BIRTH_NAME = "birth_name"
    BIRTH_DATE = "birth_date"
    ADDRESS = "address"
    PLACE = "place"
    PHONE = "phone"
    EMAIL = "email"
    INSURANCE_NUMBER = "insurance_number"
    PATIENT_ID = "patient_id"
    CASE_NUMBER = "case_number"
    IBAN = "iban"
    TERM = "term"
    IDENTIFIER = "identifier"
    MEDICAL = "medical"  # medications, lab values: redacted only on request
    MANUAL = "manual"


class Role(StrEnum):
    """Role of a person relative to the document."""

    PATIENT = "patient"
    RELATIVE = "relative"
    REFERRER = "referrer"


def normalised_sex(value: str) -> str:
    """"m" or "f" from what a record or a person writes ("m", "männlich", "male", "Herr";
    "f", "w", "weiblich", "female", "Frau"), "" for anything else."""
    v = value.strip().lower()
    if v.startswith(("m", "herr")):
        return "m"
    if v.startswith(("f", "w")):
        return "f"
    return ""


def normalise(text: str) -> str:
    """Case-fold, strip diacritics and collapse whitespace (for keys only)."""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", without_accents).strip().casefold()


@dataclass
class Person:
    """A natural person whose identifiers must be removed."""

    surname: str
    first_name: str = ""
    sex: str = ""  # "m", "f" or "" when unknown (see normalised_sex)
    title: str = ""
    birth_name: str = ""
    birth_date: dt.date | None = None
    street: str = ""  # street including house number
    postcode: str = ""
    place: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    insurance_number: str = ""
    patient_id: str = ""

    def __post_init__(self) -> None:
        self.sex = normalised_sex(self.sex)

    @property
    def full_name(self) -> str:
        """First name and surname, whichever are present, separated by a space."""
        return " ".join(t for t in (self.first_name.strip(), self.surname.strip()) if t)

    @property
    def first_names(self) -> list[str]:
        """Individual first names ("Max Peter" and "Max-Peter" -> ["Max", "Peter"])."""
        return [v for v in re.split(r"[\s\-‐‑–]+", self.first_name.strip()) if v]

    @property
    def female(self) -> bool:
        """True for a woman ("f")."""
        return self.sex == "f"

    @property
    def male(self) -> bool:
        """True for a man ("m")."""
        return self.sex == "m"

    def salutation_form(self, grammatical_case: str = "nominative") -> str:
        """"Herr Mustermann", "Herrn Mustermann", "Frau Mustermann" or the full name.

        Without a known gender the full name is returned, which is correct in
        every case of German grammar.
        """
        if self.female:
            return f"Frau {self.surname}"
        if self.male:
            return f"{'Herr' if grammatical_case == 'nominative' else 'Herrn'} {self.surname}"
        return self.full_name


@dataclass
class Patient(Person):
    """The main person of a document plus the people around them."""

    relatives: list[Person] = field(default_factory=list)
    referrers: list[Person] = field(default_factory=list)
    case_numbers: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)  # free terms supplied by the user
    notes: list[str] = field(default_factory=list)  # notes for display only (vCard NOTE)

    def key(self) -> str:
        """Stable key for the pseudonym store: surname|first name|birth date."""
        parts = [normalise(self.surname), normalise(self.first_name)]
        if self.birth_date:
            parts.append(self.birth_date.isoformat())
        return "|".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for the JSON patient file (``pseudonymed contacts export``): English
        keys, empty fields of a person left out, the notes (display only) left out."""

        def person(p: Person) -> dict[str, Any]:
            """A person without empty fields, the birth date in ISO form."""
            d = {f.name: as_stored(getattr(p, f.name)) for f in dataclasses.fields(Person)}
            d["birth_date"] = p.birth_date.isoformat() if p.birth_date else ""
            return {k: v for k, v in d.items() if v not in ("", [], None)}

        d = person(self)
        d.update({
            "relatives": [person(a) for a in self.relatives],
            "referrers": [person(z) for z in self.referrers],
            "case_numbers": list(self.case_numbers),
            "terms": list(self.terms),
        })
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Patient:
        """Build a patient from a plain dict (a patient file in JSON or TOML): English keys,
        or their German equivalents (``nachname``, ``angehoerige`` …), for people who write
        the file by hand."""
        d = in_english(d)

        def person(p: dict[str, Any]) -> Person:
            """A relative or referrer from their plain dict."""
            return Person(**_person_fields(in_english(p)))

        return cls(
            **_person_fields(d),
            relatives=[person(p) for p in d.get("relatives", [])],
            referrers=[person(p) for p in d.get("referrers", [])],
            case_numbers=[str(a) for a in d.get("case_numbers", [])],
            terms=[str(b) for b in d.get("terms", [])],
        )


# the keys of a patient file in German, for those who write it in German; the singular
# forms are understood as well
GERMAN_KEYS = {
    "nachname": "surname", "vorname": "first_name", "geschlecht": "sex", "titel": "title",
    "geburtsname": "birth_name", "geburtsdatum": "birth_date", "strasse": "street",
    "straße": "street", "plz": "postcode", "ort": "place", "telefone": "phones",
    "telefon": "phones", "phone": "phones", "email": "emails", "e-mail": "emails",
    "versichertennummer": "insurance_number", "patienten_id": "patient_id",
    "angehoerige": "relatives", "angehörige": "relatives", "zuweiser": "referrers",
    "aktenzeichen": "case_numbers", "begriffe": "terms", "hinweise": "notes"}


def in_english(d: dict[str, Any]) -> dict[str, Any]:
    """A patient file's mapping with its keys in English: German keys translated, English
    ones kept as they are.  Where a file names one field twice, the English key wins, and
    among the German ones the first of :data:`GERMAN_KEYS` (the plural ``telefone`` before
    the singular ``telefon``)."""
    english = {k: v for k, v in d.items() if str(k).lower() not in GERMAN_KEYS}
    order = list(GERMAN_KEYS)
    for k in sorted((k for k in d if str(k).lower() in GERMAN_KEYS),
                    key=lambda k: order.index(str(k).lower())):
        english.setdefault(GERMAN_KEYS[str(k).lower()], d[k])
    return english


_TEXT_FIELDS = ("surname", "first_name", "sex", "title", "birth_name", "street", "postcode",
                "place", "insurance_number", "patient_id")


def _date_from(value: Any) -> dt.date | None:
    """A birth date from a date string (ISO, German, compact), a date or a datetime
    (TOML parses dates with a time as such); ``None`` for anything else or unreadable."""
    if isinstance(value, str) and value:
        return parse_date(value)
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def _list_from(value: Any) -> list[str]:
    """Non-empty strings from a list, or from a single string (a lone phone number or
    e-mail address written without brackets)."""
    values = [value] if isinstance(value, str) else value
    return [str(w) for w in values if w]


def _person_fields(d: dict[str, Any]) -> dict[str, Any]:
    """The :class:`Person` constructor arguments read from a plain dict with English keys:
    text fields stripped, the birth date parsed, phone numbers and e-mail addresses as
    lists."""
    fields: dict[str, Any] = {name: str(d.get(name, "")).strip() for name in _TEXT_FIELDS}
    fields["birth_date"] = _date_from(d.get("birth_date"))
    fields["phones"] = _list_from(d.get("phones", []))
    fields["emails"] = _list_from(d.get("emails", []))
    return fields


def parse_date(text: str) -> dt.date | None:
    """ISO, German or compact date string to a date; ``None`` when unreadable."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y%m%d", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Hit:
    """One match of one rule on one page.

    ``start``/``end`` are offsets into the reconstructed page text, ``rects``
    the rectangles to redact (one per line the match spans).  ``text`` holds
    the clear text and must never be written to a report or log.
    """

    page: int
    start: int
    end: int
    text: str
    kind: Kind
    rule: str
    pseudonym: str
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    font_size: float = 10.0
    role: Role | None = None
    source: str = "text"  # "text", "ocr" or "manual"

    def as_dict(self) -> dict[str, Any]:
        """Complete form including the clear text: only for the working state, which
        sits next to the unredacted document anyway; never for a report or a log."""
        return {
            "page": self.page, "start": self.start, "end": self.end, "text": self.text,
            "kind": str(self.kind), "rule": self.rule, "pseudonym": self.pseudonym,
            "rects": [[round(v, 2) for v in r] for r in self.rects],
            "font_size": round(self.font_size, 2),
            "role": str(self.role) if self.role is not None else None, "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hit:
        """A hit from its :meth:`as_dict` form (the working state)."""
        return cls(
            page=int(d["page"]), start=int(d["start"]), end=int(d["end"]),
            text=str(d.get("text", "")), kind=Kind(d["kind"]), rule=str(d["rule"]),
            pseudonym=str(d["pseudonym"]), rects=[tuple(r) for r in d.get("rects", [])],
            font_size=float(d.get("font_size", 10.0)),
            role=Role(d["role"]) if d.get("role") else None,
            source=str(d.get("source", "text")),
        )

    def without_clear_text(self) -> dict[str, Any]:
        """Report form of the hit: everything except the clear text."""
        return {
            "page": self.page + 1,
            "kind": str(self.kind),
            "rule": self.rule,
            "pseudonym": self.pseudonym,
            "rects": [tuple(round(v, 1) for v in r) for r in self.rects],
            "source": self.source,
        }


@dataclass
class Recheck:
    """Result of re-checking the pseudonymised output."""

    residual_matches: list[dict[str, Any]] = field(default_factory=list)
    metadata_empty: bool = True
    structure_clean: bool = True  # no annotations, form fields, attachments, bookmarks left
    remarks: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)  # identifier kinds searched again
    kept_on_purpose: list[str] = field(default_factory=list)  # rules of hits kept on purpose

    @property
    def passed(self) -> bool:
        """True when nothing was found again, the metadata are empty and the structure
        is clean; only then may the file be released."""
        return not self.residual_matches and self.metadata_empty and self.structure_clean


@dataclass(frozen=True)
class RunLog:
    """What a run has to say about itself besides the hits: the options it ran with,
    the warnings that need attention, the hints of what was done, and the residual-risk
    estimate of the text."""

    options: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    residual_risk: dict[str, Any] | None = None


@dataclass
class Report:
    """Audit report for one pseudonymised document.  Contains no clear data."""

    source: str
    target: str
    pages: int
    timestamp: str
    tool: str
    text_layer: bool = True
    hits: list[dict[str, Any]] = field(default_factory=list)
    pseudonyms: list[str] = field(default_factory=list)
    recheck: Recheck | None = None
    options: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)  # things that need attention
    notes: list[str] = field(default_factory=list)  # what was done, for the record
    residual_risk: dict[str, Any] | None = None  # k-map estimate of what the text still reveals

    @property
    def passed(self) -> bool:
        """True when a re-check ran and passed."""
        return self.recheck is not None and self.recheck.passed

    def as_dict(self) -> dict[str, Any]:
        """The JSON form of the report, with the verdict as an extra key."""
        d = as_stored(self)
        d["passed"] = self.passed
        return d

    @staticmethod
    def now() -> str:
        """The current local time with offset, to the second, for the report."""
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")
