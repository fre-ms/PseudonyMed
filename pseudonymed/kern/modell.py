"""Core data model: persons, identifiers, hits and the audit report.

A :class:`Patient` says *what* must disappear from a document, a
:class:`Fundstelle` says *where* a rule matched, and the :class:`Pruefbericht`
records what was done.  The report deliberately never repeats clear data, so it
may travel with the pseudonymised file.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Art(StrEnum):
    """Kind of identifier.  The value doubles as the prefix of rule ids."""

    NAME = "name"
    VORNAME = "vorname"
    NACHNAME = "nachname"
    GEBURTSNAME = "geburtsname"
    GEBURTSDATUM = "geburtsdatum"
    ADRESSE = "adresse"
    ORT = "ort"
    TELEFON = "telefon"
    EMAIL = "email"
    VERSICHERTENNUMMER = "versichertennummer"
    PATIENTEN_ID = "patienten_id"
    AKTENZEICHEN = "aktenzeichen"
    IBAN = "iban"
    BEGRIFF = "begriff"
    IDENTIFIKATOR = "identifikator"
    MEDIZIN = "medizin"  # medications, lab values: redacted only on request
    MANUELL = "manuell"


class Rolle(StrEnum):
    """Role of a person relative to the document."""

    PATIENT = "patient"
    ANGEHOERIGE = "angehoerige"
    ZUWEISER = "zuweiser"


def normalisiere(text: str) -> str:
    """Case-fold, strip diacritics and collapse whitespace (for keys only)."""
    zerlegt = unicodedata.normalize("NFKD", text)
    ohne_akzente = "".join(c for c in zerlegt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ohne_akzente).strip().casefold()


@dataclass
class Person:
    """A natural person whose identifiers must be removed."""

    nachname: str
    vorname: str = ""
    geschlecht: str = ""  # "m", "w" or "" when unknown
    titel: str = ""
    geburtsname: str = ""
    geburtsdatum: dt.date | None = None
    strasse: str = ""  # street including house number
    plz: str = ""
    ort: str = ""
    telefone: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    versichertennummer: str = ""
    patienten_id: str = ""

    @property
    def vollname(self) -> str:
        """First name and surname, whichever are present, separated by a space."""
        return " ".join(t for t in (self.vorname.strip(), self.nachname.strip()) if t)

    @property
    def vornamen(self) -> list[str]:
        """Individual first names ("Max Peter" and "Max-Peter" -> ["Max", "Peter"])."""
        return [v for v in re.split(r"[\s\-‐‑–]+", self.vorname.strip()) if v]

    @property
    def weiblich(self) -> bool:
        """True for a gender recorded as "w"/"weiblich" or "f"/"female"."""
        return self.geschlecht.lower().startswith(("w", "f"))

    @property
    def maennlich(self) -> bool:
        """True for a gender recorded as "m"/"männlich"/"male"."""
        return self.geschlecht.lower().startswith("m")

    def anredeform(self, kasus: str = "nominativ") -> str:
        """"Herr Mustermann", "Herrn Mustermann", "Frau Mustermann" or the full name.

        Without a known gender the full name is returned, which is correct in
        every case of German grammar.
        """
        if self.weiblich:
            return f"Frau {self.nachname}"
        if self.maennlich:
            return f"{'Herr' if kasus == 'nominativ' else 'Herrn'} {self.nachname}"
        return self.vollname


@dataclass
class Patient(Person):
    """The main person of a document plus the people around them."""

    angehoerige: list[Person] = field(default_factory=list)
    zuweiser: list[Person] = field(default_factory=list)
    aktenzeichen: list[str] = field(default_factory=list)
    begriffe: list[str] = field(default_factory=list)  # free terms supplied by the user
    hinweise: list[str] = field(default_factory=list)  # notes for display only (vCard NOTE)

    def schluessel(self) -> str:
        """Stable key for the pseudonym store: surname|first name|birth date."""
        teile = [normalisiere(self.nachname), normalisiere(self.vorname)]
        if self.geburtsdatum:
            teile.append(self.geburtsdatum.isoformat())
        return "|".join(teile)

    def als_dict(self) -> dict[str, Any]:
        """Plain dict for the JSON patient file (``pseudonymed kontakte export``)."""

        def person(p: Person) -> dict[str, Any]:
            """A person without empty fields, the birth date in ISO form."""
            d = asdict(p)
            d["geburtsdatum"] = p.geburtsdatum.isoformat() if p.geburtsdatum else ""
            return {k: v for k, v in d.items() if v not in ("", [], None)}

        d = person(self)
        d.update(
            angehoerige=[person(a) for a in self.angehoerige],
            zuweiser=[person(z) for z in self.zuweiser],
            aktenzeichen=list(self.aktenzeichen),
            begriffe=list(self.begriffe),
        )
        d.pop("hinweise", None)
        return d

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Patient:
        """Build a patient from a plain dict (JSON/TOML input, M1 CLI)."""

        def person(p: dict[str, Any]) -> Person:
            """A relative or referrer from their plain dict."""
            return Person(**_personenfelder(p))

        felder = _personenfelder(d)
        return cls(
            **felder,
            angehoerige=[person(p) for p in d.get("angehoerige", [])],
            zuweiser=[person(p) for p in d.get("zuweiser", [])],
            aktenzeichen=[str(a) for a in d.get("aktenzeichen", [])],
            begriffe=[str(b) for b in d.get("begriffe", [])],
        )


_TEXTFELDER = ("nachname", "vorname", "geschlecht", "titel", "geburtsname", "strasse", "plz",
               "ort", "versichertennummer", "patienten_id")


def _datum_aus(wert: Any) -> dt.date | None:
    """A birth date from a date string (ISO, German, compact), a date or a datetime
    (TOML parses dates with a time as such); ``None`` for anything else or unreadable."""
    if isinstance(wert, str) and wert:
        return parse_datum(wert)
    if isinstance(wert, dt.datetime):
        return wert.date()
    return wert if isinstance(wert, dt.date) else None


def _liste_aus(wert: Any) -> list[str]:
    """Non-empty strings from a list, or from a single string (a lone phone number or
    e-mail address written without brackets)."""
    werte = [wert] if isinstance(wert, str) else wert
    return [str(w) for w in werte if w]


def _personenfelder(d: dict[str, Any]) -> dict[str, Any]:
    """The :class:`Person` constructor arguments read from a plain dict: text fields
    stripped, the birth date parsed, phone numbers and e-mail addresses as lists (the
    singular keys ``telefon``/``email`` are accepted too)."""
    felder: dict[str, Any] = {name: str(d.get(name, "")).strip() for name in _TEXTFELDER}
    felder["geburtsdatum"] = _datum_aus(d.get("geburtsdatum"))
    felder["telefone"] = _liste_aus(d.get("telefone", d.get("telefon", [])))
    felder["emails"] = _liste_aus(d.get("emails", d.get("email", [])))
    return felder


def parse_datum(text: str) -> dt.date | None:
    """ISO, German or compact date string to a date; ``None`` when unreadable."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y%m%d", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Fundstelle:
    """One match of one rule on one page.

    ``start``/``ende`` are offsets into the reconstructed page text, ``rects``
    the rectangles to redact (one per line the match spans).  ``text`` holds
    the clear text and must never be written to a report or log.
    """

    seite: int
    start: int
    ende: int
    text: str
    art: Art
    regel: str
    pseudonym: str
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)
    schriftgroesse: float = 10.0
    rolle: Rolle | None = None
    quelle: str = "text"  # "text", "ocr" or "manuell"

    def als_dict(self) -> dict[str, Any]:
        """Complete form including the clear text: only for the working state, which
        sits next to the unredacted document anyway; never for a report or a log."""
        return {
            "seite": self.seite, "start": self.start, "ende": self.ende, "text": self.text,
            "art": str(self.art), "regel": self.regel, "pseudonym": self.pseudonym,
            "rects": [[round(v, 2) for v in r] for r in self.rects],
            "schriftgroesse": round(self.schriftgroesse, 2),
            "rolle": str(self.rolle) if self.rolle is not None else None, "quelle": self.quelle,
        }

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Fundstelle:
        """A hit from its :meth:`als_dict` form (the working state)."""
        return cls(
            seite=int(d["seite"]), start=int(d["start"]), ende=int(d["ende"]),
            text=str(d.get("text", "")), art=Art(d["art"]), regel=str(d["regel"]),
            pseudonym=str(d["pseudonym"]), rects=[tuple(r) for r in d.get("rects", [])],
            schriftgroesse=float(d.get("schriftgroesse", 10.0)),
            rolle=Rolle(d["rolle"]) if d.get("rolle") else None,
            quelle=str(d.get("quelle", "text")),
        )

    def ohne_klartext(self) -> dict[str, Any]:
        """Report form of the hit: everything except the clear text."""
        return {
            "seite": self.seite + 1,
            "art": str(self.art),
            "regel": self.regel,
            "pseudonym": self.pseudonym,
            "rects": [tuple(round(v, 1) for v in r) for r in self.rects],
            "quelle": self.quelle,
        }


@dataclass
class Gegenprobe:
    """Result of re-checking the pseudonymised output."""

    resttreffer: list[dict[str, Any]] = field(default_factory=list)
    metadaten_leer: bool = True
    struktur_sauber: bool = True  # no annotations, form fields, attachments, bookmarks left
    anmerkungen: list[str] = field(default_factory=list)
    geprueft: list[str] = field(default_factory=list)  # identifier kinds searched again
    belassen: list[str] = field(default_factory=list)  # rules of hits kept on purpose

    @property
    def bestanden(self) -> bool:
        """True when nothing was found again, the metadata are empty and the structure
        is clean; only then may the file be released."""
        return not self.resttreffer and self.metadaten_leer and self.struktur_sauber


@dataclass(frozen=True)
class Laufprotokoll:
    """What a run has to say about itself besides the hits: the options it ran with,
    the warnings that need attention, the hints of what was done, and the residual-risk
    estimate of the text."""

    optionen: dict[str, Any] = field(default_factory=dict)
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    restrisiko: dict[str, Any] | None = None


@dataclass
class Pruefbericht:
    """Audit report for one pseudonymised document.  Contains no clear data."""

    quelle: str
    ziel: str
    seiten: int
    zeitpunkt: str
    werkzeug: str
    textebene: bool = True
    fundstellen: list[dict[str, Any]] = field(default_factory=list)
    pseudonyme: list[str] = field(default_factory=list)
    gegenprobe: Gegenprobe | None = None
    optionen: dict[str, Any] = field(default_factory=dict)
    warnungen: list[str] = field(default_factory=list)  # things that need attention
    hinweise: list[str] = field(default_factory=list)  # what was done, for the record
    restrisiko: dict[str, Any] | None = None  # k-map estimate of what the text still reveals

    @property
    def bestanden(self) -> bool:
        """True when a re-check ran and passed."""
        return self.gegenprobe is not None and self.gegenprobe.bestanden

    def als_dict(self) -> dict[str, Any]:
        """The JSON form of the report, with the verdict as an extra key."""
        d = asdict(self)
        d["bestanden"] = self.bestanden
        return d

    @staticmethod
    def jetzt() -> str:
        """The current local time with offset, to the second, for the report."""
        return dt.datetime.now().astimezone().isoformat(timespec="seconds")
