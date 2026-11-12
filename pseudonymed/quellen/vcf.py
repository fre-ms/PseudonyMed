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

from pseudonymed.kern.modell import Patient, parse_datum

_KVNR = re.compile(r"(?<![A-Z0-9])[A-Z]\d{9}(?!\d)")
_ANREDE_M = {"herr", "herrn", "hr.", "hr", "mr", "mr.", "sr."}
_ANREDE_W = {"frau", "fr.", "fr", "mrs", "mrs.", "ms", "ms.", "sra."}
# Custom fields that may carry the insured-person number, in order of preference.
_VERSICHERTENNUMMER_FELDER = ("X-VERSICHERTENNUMMER", "X-KVNR", "X-INSURANCE-NUMBER")


@dataclass
class Eigenschaft:
    """One content line of a card: name, parameters, raw (escaped) value and group."""

    name: str
    params: dict[str, list[str]] = field(default_factory=dict)
    wert: str = ""
    gruppe: str = ""

    @property
    def typen(self) -> set[str]:
        """The TYPE parameter values in lower case (``home``, ``cell`` ...)."""
        return {t.lower() for t in self.params.get("TYPE", [])}

    @property
    def text(self) -> str:
        """The value with backslash escapes resolved."""
        return entschluessele(self.wert)

    def teile(self) -> list[str]:
        """The structured value split at unescaped semicolons, each part unescaped."""
        return [entschluessele(t) for t in _trenne(self.wert, ";")]


@dataclass
class VCard:
    """One card: its properties in file order, with lookups by property name."""

    eigenschaften: list[Eigenschaft] = field(default_factory=list)

    def alle(self, name: str) -> list[Eigenschaft]:
        """All properties called ``name`` (case-insensitive), in file order."""
        name = name.upper()
        return [e for e in self.eigenschaften if e.name == name]

    def erste(self, name: str) -> Eigenschaft | None:
        """The first property called ``name``, or ``None``."""
        treffer = self.alle(name)
        return treffer[0] if treffer else None

    def wert(self, name: str, standard: str = "") -> str:
        """The stripped, unescaped text of the first property ``name``, else ``standard``."""
        e = self.erste(name)
        return e.text.strip() if e else standard

    @property
    def uid(self) -> str:
        """The UID property, empty when absent."""
        return self.wert("UID")

    @property
    def fn(self) -> str:
        """The formatted name (FN), empty when absent."""
        return self.wert("FN")

    @property
    def version(self) -> str:
        """The VERSION property (``3.0`` or ``4.0``), empty when absent."""
        return self.wert("VERSION")


def entschluessele(wert: str) -> str:
    """Resolve vCard backslash escapes: an escaped n is a newline, anything else is itself."""
    out: list[str] = []
    i = 0
    while i < len(wert):
        c = wert[i]
        if c == "\\" and i + 1 < len(wert):
            n = wert[i + 1]
            out.append("\n" if n in "nN" else n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _trenne(wert: str, sep: str) -> list[str]:
    """Split on ``sep`` unless it is escaped with a backslash."""
    teile: list[str] = []
    aktuell: list[str] = []
    i = 0
    while i < len(wert):
        c = wert[i]
        if c == "\\" and i + 1 < len(wert):
            aktuell.append(wert[i : i + 2])
            i += 2
            continue
        if c == sep:
            teile.append("".join(aktuell))
            aktuell = []
        else:
            aktuell.append(c)
        i += 1
    teile.append("".join(aktuell))
    return teile


def _entfalte(text: str) -> list[str]:
    """Logical lines of ``text``: folded continuations joined, blank lines dropped."""
    zeilen: list[str] = []
    for roh in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if roh[:1] in (" ", "\t") and zeilen:
            zeilen[-1] += roh[1:]
        elif roh.strip():
            zeilen.append(roh)
    return zeilen


def _kopf_und_wert(zeile: str) -> tuple[str, str]:
    """Split a content line at the first colon outside double quotes into head and value."""
    in_anfuehrung = False
    for i, c in enumerate(zeile):
        if c == '"':
            in_anfuehrung = not in_anfuehrung
        elif c == ":" and not in_anfuehrung:
            return zeile[:i], zeile[i + 1 :]
    return zeile, ""


def _param_tokens(kopf: str) -> list[str]:
    """Split a property head at semicolons outside double quotes; the first token is the name."""
    tokens: list[str] = []
    aktuell: list[str] = []
    in_anfuehrung = False
    for c in kopf:
        if c == '"':
            in_anfuehrung = not in_anfuehrung
        if c == ";" and not in_anfuehrung:
            tokens.append("".join(aktuell))
            aktuell = []
        else:
            aktuell.append(c)
    tokens.append("".join(aktuell))
    return tokens


def _parameter(tokens: list[str]) -> dict[str, list[str]]:
    """Parameter tokens as ``NAME -> values``; a bare token is a vCard 2.1 type such as CELL."""
    params: dict[str, list[str]] = {}
    for token in tokens:
        if "=" in token:
            k, v = token.split("=", 1)
            werte = [w.strip().strip('"') for w in v.split(",")]
        else:
            k, werte = "TYPE", [token.strip().strip('"')]
        params.setdefault(k.strip().upper(), []).extend(w for w in werte if w)
    return params


def _dekodiert(wert: str, params: dict[str, list[str]]) -> str:
    """The value with quoted-printable undone when the parameters ask for it, else unchanged."""
    if not any(e.upper() == "QUOTED-PRINTABLE" for e in params.get("ENCODING", [])):
        return wert
    zeichensatz = (params.get("CHARSET") or ["utf-8"])[0]
    try:
        return quopri.decodestring(wert.encode("latin-1")).decode(zeichensatz, errors="replace")
    except (LookupError, ValueError):
        return wert


def _eigenschaft(zeile: str) -> Eigenschaft | None:
    """One unfolded content line as a property; ``None`` for a line without a name."""
    kopf, wert = _kopf_und_wert(zeile)
    tokens = _param_tokens(kopf)
    name = tokens[0].strip()
    if not name:
        return None
    gruppe = ""
    if "." in name:
        gruppe, name = name.rsplit(".", 1)
    params = _parameter(tokens[1:])
    return Eigenschaft(
        name=name.upper(), params=params, wert=_dekodiert(wert, params), gruppe=gruppe
    )


def parse_vcards(text: str) -> list[VCard]:
    """All cards in ``text`` (BEGIN:VCARD ... END:VCARD); lines outside a card are ignored."""
    karten: list[VCard] = []
    aktuelle: VCard | None = None
    for zeile in _entfalte(text):
        e = _eigenschaft(zeile)
        if e is None:
            continue
        if e.name == "BEGIN" and e.wert.strip().upper() == "VCARD":
            aktuelle = VCard()
        elif e.name == "END" and e.wert.strip().upper() == "VCARD":
            if aktuelle is not None:
                karten.append(aktuelle)
            aktuelle = None
        elif aktuelle is not None:
            aktuelle.eigenschaften.append(e)
    return karten


def lade_vcf(pfad: str | Path) -> list[VCard]:
    """Parse the cards of a .vcf file read as UTF-8 (BOM tolerated), else as Latin-1."""
    roh = Path(pfad).read_bytes()
    try:
        text = roh.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = roh.decode("latin-1")
    return parse_vcards(text)


def parse_bday(wert: str) -> dt.date | None:
    """BDAY value as a date; ``None`` when empty, unreadable or without a year (``--MMDD``)."""
    wert = wert.strip()
    if not wert or wert.startswith("--"):
        return None  # no year: useless as an identifier
    wert = wert.split("T")[0]
    return parse_datum(wert)


class _Namensteile(NamedTuple):
    """Surname, first name(s) and honorific prefix as read from N or, failing that, FN."""

    nachname: str
    vorname: str
    prefix: str


def _name_aus_fn(fn: str) -> tuple[str, str]:
    """Surname and first names from a formatted name: the last word is the surname."""
    fn_teile = fn.split()
    if not fn_teile:
        return "", ""
    return fn_teile[-1], " ".join(fn_teile[:-1])


def _namensteile(card: VCard) -> _Namensteile:
    """Name parts of the card; FN stands in when N is empty, middle names join the first name."""
    n = card.erste("N")
    teile = (n.teile() + [""] * 5) if n else [""] * 5
    nachname, vorname, weitere, prefix = (t.strip() for t in teile[:4])
    if not nachname and not vorname:
        nachname, vorname = _name_aus_fn(card.fn)
    if weitere:
        vorname = f"{vorname} {weitere}".strip()
    return _Namensteile(nachname, vorname, prefix)


class _Anrede(NamedTuple):
    """Legal gender read from a salutation and the remaining prefix words as the title."""

    geschlecht: str
    titel: str


def _anrede_und_titel(prefix: str) -> _Anrede:
    """Gender from a salutation token in the N prefix; the other tokens make up the title."""
    geschlecht = ""
    titel_teile: list[str] = []
    for token in filter(None, re.split(r"[\s,]+", prefix)):
        if token.lower() in _ANREDE_M:
            geschlecht = "m"
        elif token.lower() in _ANREDE_W:
            geschlecht = "w"
        else:
            titel_teile.append(token)
    return _Anrede(geschlecht, " ".join(titel_teile))


def _geschlecht(card: VCard, aus_anrede: str) -> str:
    """``m``/``w`` from the salutation, else from GENDER's first component, else empty."""
    if aus_anrede:
        return aus_anrede
    gender = card.wert("GENDER").split(";")[0].strip().upper()
    return {"M": "m", "F": "w"}.get(gender, "")


def _telefon(wert: str) -> str:
    """A TEL value without the ``tel:`` URI scheme."""
    wert = wert.strip()
    if wert.lower().startswith("tel:"):
        wert = wert[4:]
    return wert


def _telefone(card: VCard) -> list[str]:
    """Distinct phone numbers of the card with at least five digits, in file order."""
    nummern = (_telefon(t.text) for t in card.alle("TEL"))
    return list(dict.fromkeys(n for n in nummern if len(re.sub(r"\D", "", n)) >= 5))


def _emails(card: VCard) -> list[str]:
    """Distinct lower-case e-mail addresses (``mailto:`` dropped), in file order."""
    adressen = (e.text.strip().removeprefix("mailto:").lower() for e in card.alle("EMAIL"))
    return list(dict.fromkeys(a for a in adressen if "@" in a))


def _adresse(card: VCard) -> tuple[str, str, str]:
    """Street, postcode and town of the home address (else the first ADR).

    The extended-address part stands in for an empty street; only the first
    line of a multi-line street is kept.
    """
    adressen = card.alle("ADR")
    if not adressen:
        return "", "", ""
    gewaehlt = next((a for a in adressen if "home" in a.typen), adressen[0])
    teile = gewaehlt.teile() + [""] * 7
    strasse = teile[2].split("\n")[0].strip()
    if not strasse and teile[1].strip():
        strasse = teile[1].split("\n")[0].strip()
    return strasse, teile[5].strip(), teile[3].strip()


def _hinweise(card: VCard) -> list[str]:
    """Non-empty NOTE texts plus the ROLE as a ``Beruf:`` line, kept for display."""
    hinweise = [h.text.strip() for h in card.alle("NOTE") if h.text.strip()]
    rolle = card.wert("ROLE")
    if rolle:
        hinweise.append(f"Beruf: {rolle}")
    return hinweise


def _versichertennummer(card: VCard, hinweise: list[str]) -> str:
    """The insured-person number from a custom X field, else the first KVNR in the notes."""
    for name in _VERSICHERTENNUMMER_FELDER:
        if card.wert(name):
            return card.wert(name)
    for hinweis in hinweise:
        m = _KVNR.search(hinweis)
        if m:
            return m.group(0)
    return ""


def patient_aus_vcard(card: VCard) -> Patient:
    """Derive the identifiers of one card; unknown fields are ignored."""
    name = _namensteile(card)
    anrede = _anrede_und_titel(name.prefix)
    strasse, plz, ort = _adresse(card)
    hinweise = _hinweise(card)
    return Patient(
        nachname=name.nachname,
        vorname=name.vorname,
        geschlecht=_geschlecht(card, anrede.geschlecht),
        titel=anrede.titel,
        geburtsname=card.wert("X-MAIDENNAME"),
        geburtsdatum=parse_bday(card.wert("BDAY")),
        strasse=strasse,
        plz=plz,
        ort=ort,
        telefone=_telefone(card),
        emails=_emails(card),
        versichertennummer=_versichertennummer(card, hinweise),
        patienten_id=card.wert("X-DOCTOLIB-PATIENT-ID"),
        hinweise=hinweise,
    )


def patienten_aus_vcf(pfad: str | Path) -> list[Patient]:
    """Patients derived from every card in the file ``pfad``, in file order."""
    return [patient_aus_vcard(k) for k in lade_vcf(pfad)]
