"""Fuzzy token matching for OCR text.

OCR misreads single characters ("Jull" for "Juli", "Fiantz" for "Flantz"),
which defeats exact patterns.  For pages read by OCR every identifier is also
searched as a sequence of tokens with a small Levenshtein tolerance that grows
with the token length (1 for 4–6 characters, 2 up to 11, 3 up to 19, 4 beyond).
A spurious hit on a harmless word is cheap; a misread date of birth left
visible is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung.varianten import ohne_akzente
from pseudonymed.kern.modell import Art, Fundstelle, Patient, Person, Rolle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

if TYPE_CHECKING:
    import datetime as dt

    from pseudonymed.erkennung.fachbegriffe import Fachbegriffe

_MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"]
_RAND = ",.;:()*\"'[]«»"
_TOKEN = re.compile(r"\S+")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


@dataclass
class Ziel:
    """One token sequence to search for, with the pseudonym, kind, rule name and role
    that its hits carry."""

    tokens: list[str]
    pseudonym: str
    art: Art
    regel: str
    rolle: Rolle | None = None


def _kern(token: str) -> str:
    return token.strip(_RAND).casefold()


def _formen(token: str) -> set[str]:
    k = _kern(token)
    return {k, k.translate(_TRANS), ohne_akzente(k)} - {""}


def toleranz(token: str) -> int:
    """Allowed edits by token length: 0 below 4, 1 up to 6, 2 up to 11, 3 up to 19, else 4."""
    n = len(_kern(token))
    if n < 4:
        return 0
    if n < 7:
        return 1
    if n < 12:
        return 2
    return 3 if n < 20 else 4


def distanz(a: str, b: str, maximum: int) -> int:
    """Levenshtein distance, stopping early once it exceeds ``maximum``."""
    if abs(len(a) - len(b)) > maximum:
        return maximum + 1
    vorher = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        aktuell = [i]
        bestes = i
        for j, cb in enumerate(b, 1):
            wert = min(vorher[j] + 1, aktuell[j - 1] + 1, vorher[j - 1] + (ca != cb))
            aktuell.append(wert)
            bestes = min(bestes, wert)
        if bestes > maximum:
            return maximum + 1
        vorher = aktuell
    return vorher[-1]


def _passt(ziel_token: str, ocr_token: str) -> bool:
    t = toleranz(ziel_token)
    ziel_formen = _formen(ziel_token)
    ocr_formen = _formen(ocr_token)
    if not ziel_formen or not ocr_formen:
        return False
    return any(distanz(z, o, t) <= t for z in ziel_formen for o in ocr_formen)


# -- targets ---------------------------------------------------------------------------------------
def _nachnamen(nachname: str) -> list[str]:
    """The surname and its parts split at spaces and hyphens, each once and only from
    four characters on."""
    teile = re.split(r"[\s\-]+", nachname)
    return [t for t in dict.fromkeys([nachname, *teile]) if len(t) >= 4]


def _namensziele(praefix: str, person: Person, pseudonym: str, rolle: Rolle) -> list[Ziel]:
    """Targets for one person's names: first name with surname, the surname and its parts,
    every first name and the maiden name, each only from four characters on."""
    vornamen = [v for v in person.vornamen if len(v) >= 4]
    ziele: list[Ziel] = []
    if vornamen and person.nachname:
        ziele.append(Ziel([vornamen[0], person.nachname], pseudonym, Art.NAME,
                          f"{praefix}.vollname", rolle))
    ziele += [Ziel([t], pseudonym, Art.NACHNAME, f"{praefix}.nachname", rolle)
              for t in _nachnamen(person.nachname)]
    ziele += [Ziel([v], pseudonym, Art.VORNAME, f"{praefix}.vorname", rolle) for v in vornamen]
    if len(person.geburtsname) >= 4:
        ziele.append(Ziel([person.geburtsname], pseudonym, Art.GEBURTSNAME,
                          f"{praefix}.geburtsname", rolle))
    return ziele


def _adressziele(praefix: str, person: Person, vergabe: Pseudonymvergabe,
                 rolle: Rolle) -> list[Ziel]:
    """Targets for one person's address: the whole street line and its first word, the
    town, and postcode with town; a street whose name is shorter than four characters
    is skipped."""
    ziele: list[Ziel] = []
    tokens = person.strasse.split()
    if tokens and len(tokens[0]) >= 4:
        pseudonym = vergabe.strasse(person)
        ziele.append(Ziel(tokens, pseudonym, Art.ADRESSE, f"{praefix}.strasse", rolle))
        ziele.append(Ziel([tokens[0]], pseudonym, Art.ADRESSE, f"{praefix}.strasse", rolle))
    if len(person.ort) >= 4:
        ziele.append(Ziel([person.ort], vergabe.ort(person), Art.ORT, f"{praefix}.ort", rolle))
        if person.plz:
            ziele.append(Ziel([person.plz, person.ort], vergabe.plz_ort(person), Art.ORT,
                              f"{praefix}.plz_ort", rolle))
    return ziele


def _datumsziele(datum: dt.date, pseudonym: str) -> list[Ziel]:
    """The date of birth in the forms a document writes it: numeric with and without
    leading zeros or the century, and with the month written out or abbreviated."""
    monat = _MONATE[datum.month - 1]
    tag = f"{datum.day}."
    # no "%-d": that strftime flag is a glibc/BSD extension and raises on Windows
    formen = [[datum.strftime("%d.%m.%Y")], [f"{datum.day}.{datum.month}.{datum.year}"],
              [datum.strftime("%d.%m.%y")], [tag, monat, str(datum.year)],
              [tag, monat[:3] + ".", str(datum.year)]]
    return [Ziel(tokens, pseudonym, Art.GEBURTSDATUM, "datum.geburtsdatum", Rolle.PATIENT)
            for tokens in formen]


def _kennungsziele(patient: Patient, vergabe: Pseudonymvergabe) -> list[Ziel]:
    """Targets for the patient's identifiers: insurance number and patient id from six
    characters on, e-mail addresses from eight, and every file number split into tokens."""
    ziele: list[Ziel] = []
    if len(patient.versichertennummer) >= 6:
        ziele.append(Ziel([patient.versichertennummer], vergabe.versichertennummer(),
                          Art.VERSICHERTENNUMMER, "versichertennummer", Rolle.PATIENT))
    if len(patient.patienten_id) >= 6:
        ziele.append(Ziel([patient.patienten_id], vergabe.patienten_id(), Art.PATIENTEN_ID,
                          "patienten_id", Rolle.PATIENT))
    ziele += [Ziel([e], vergabe.email(), Art.EMAIL, "email", Rolle.PATIENT)
              for e in patient.emails if len(e) >= 8]
    ziele += [Ziel(a.split(), vergabe.aktenzeichen(), Art.AKTENZEICHEN, "aktenzeichen",
                   Rolle.PATIENT) for a in patient.aktenzeichen]
    return ziele


def ziele_fuer(patient: Patient, vergabe: Pseudonymvergabe) -> list[Ziel]:
    """Token sequences worth a fuzzy search, with their pseudonyms."""
    ziele = _namensziele("name", patient, vergabe.hauptperson(), Rolle.PATIENT)
    ziele += _adressziele("adresse", patient, vergabe, Rolle.PATIENT)
    if patient.geburtsdatum:
        ziele += _datumsziele(patient.geburtsdatum, vergabe.geburtsdatum())
    ziele += _kennungsziele(patient, vergabe)
    for i, person in enumerate(patient.angehoerige, 1):
        ziele += _namensziele(f"angehoerige{i}", person, vergabe.angehoerige(i, person),
                              Rolle.ANGEHOERIGE)
        ziele += _adressziele(f"angehoerige{i}", person, vergabe, Rolle.ANGEHOERIGE)
    for i, person in enumerate(patient.zuweiser, 1):
        ziele += _namensziele(f"zuweiser{i}", person, vergabe.zuweiser(i, person), Rolle.ZUWEISER)
        ziele += _adressziele(f"zuweiser{i}", person, vergabe, Rolle.ZUWEISER)
    return ziele


# -- the search ------------------------------------------------------------------------------------
_GROSS = (Art.NAME, Art.NACHNAME, Art.VORNAME, Art.GEBURTSNAME, Art.ORT, Art.ADRESSE)
_Spanne = tuple[int, int]
_Kandidat = tuple[int, int, Ziel]


class _Token(NamedTuple):
    """One whitespace-delimited token of the page with its character offsets."""

    start: int
    ende: int
    text: str


@dataclass(frozen=True)
class _Suchlauf:
    """What every step of one page's fuzzy search shares: the page, its tokens, the
    spans of protected medical terms and whether names must be capitalised."""

    seite: Seitentext
    tokens: list[_Token]
    fachbegriffe: Fachbegriffe | None
    schutz: list[_Spanne]
    grossschreibung: bool


def _tokens(text: str) -> list[_Token]:
    """The page split at whitespace, every token with its offsets."""
    return [_Token(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]


def _suchbar(ziel: Ziel) -> bool:
    """A target can be searched only when every token keeps characters after trimming."""
    return bool(ziel.tokens) and all(_kern(t) for t in ziel.tokens)


def _fenster(tokens: list[_Token], ziel: Ziel) -> Iterator[_Spanne]:
    """Index ranges (first, last) of consecutive tokens matching the target token by token."""
    n = len(ziel.tokens)
    for i in range(len(tokens) - n + 1):
        if all(_passt(ziel.tokens[k], tokens[i + k].text) for k in range(n)):
            yield i, i + n - 1


def _auf_einer_zeile(seite: Seitentext, stuecke: list[_Token]) -> bool:
    """Do all pieces sit on the line of the first one?"""
    zeile = seite.zeile_von(stuecke[0].start)
    return all(seite.zeile_von(t.start) == zeile for t in stuecke[1:])


def _geteilte_fenster(seite: Seitentext, tokens: list[_Token],
                      ziel_token: str) -> Iterator[_Spanne]:
    """Index ranges of two or three neighbouring tokens on one line whose concatenation
    matches a long target token: OCR splits such tokens ("gotthard. flantz@example. org")."""
    laenge = len(_kern(ziel_token))
    for breite in (2, 3):
        for i in range(len(tokens) - breite + 1):
            stuecke = tokens[i:i + breite]
            if not _auf_einer_zeile(seite, stuecke):
                continue
            zusammen = "".join(t.text for t in stuecke)
            # a whole neighbouring word glued on is not a split token
            if len(_kern(zusammen)) <= laenge + 2 and _passt(ziel_token, zusammen):
                yield i, i + breite - 1


def _alle_fenster(lauf: _Suchlauf, ziel: Ziel) -> list[_Spanne]:
    """Every token window matching the target: token by token, and for a single long
    token also across the pieces OCR may have split it into."""
    fenster = list(_fenster(lauf.tokens, ziel))
    if len(ziel.tokens) == 1 and len(_kern(ziel.tokens[0])) >= 12:
        fenster += _geteilte_fenster(lauf.seite, lauf.tokens, ziel.tokens[0])
    return fenster


def _ohne_rand(text: str, a: int, b: int) -> _Spanne:
    """The span ``text[a:b]`` shrunk past the punctuation glued to either end."""
    while a < b and text[a] in _RAND:
        a += 1
    while b > a and text[b - 1] in _RAND:
        b -= 1
    return a, b


def _gross_genug(ziel: Ziel, gefunden: str, grossschreibung: bool) -> bool:
    """With ``grossschreibung`` a name or place must start with a capital letter, which
    keeps "lang" or "hoch" safe in correctly cased text; OCR text is not trusted that far."""
    if grossschreibung and ziel.art in _GROSS:
        return gefunden[:1].isupper()
    return True


def _geschuetzter_begriff(lauf: _Suchlauf, a: int, b: int, ziel: Ziel) -> bool:
    """Is the inexact match a protected medical term ("Bauch" for a patient called
    Bauer)?  An exact spelling of the target is never protected."""
    if lauf.fachbegriffe is None:
        return False
    text = lauf.seite.text
    if _kern(text[a:b]) == _kern(" ".join(ziel.tokens)):
        return False
    return lauf.fachbegriffe.geschuetzt(text, a, b, lauf.schutz, ganz=True)


def _kandidaten(lauf: _Suchlauf, ziele: list[Ziel]) -> list[_Kandidat]:
    """Trimmed spans of every matching window that passes the capitalisation rule and
    the protected-term guard, in the order the targets and windows were searched."""
    kandidaten: list[_Kandidat] = []
    text = lauf.seite.text
    for ziel in filter(_suchbar, ziele):
        for i, j in _alle_fenster(lauf, ziel):
            a, b = _ohne_rand(text, lauf.tokens[i].start, lauf.tokens[j].ende)
            if (_gross_genug(ziel, text[a:b], lauf.grossschreibung)
                    and not _geschuetzter_begriff(lauf, a, b, ziel)):
                kandidaten.append((a, b, ziel))
    return kandidaten


def _ohne_ueberlappung(kandidaten: list[_Kandidat], belegt: list[_Spanne]) -> list[_Kandidat]:
    """Longest candidates first, each kept only where neither an exact hit nor an earlier
    candidate lies; ``belegt`` grows with every kept span."""
    gewaehlt: list[_Kandidat] = []
    for a, b, ziel in sorted(kandidaten, key=lambda k: (-(k[1] - k[0]), k[0])):
        if any(a < y and b > x for x, y in belegt):
            continue
        belegt.append((a, b))
        gewaehlt.append((a, b, ziel))
    return gewaehlt


def _fundstelle(seite: Seitentext, a: int, b: int, ziel: Ziel, quelle: str) -> Fundstelle:
    """The hit for one span, with its boxes and font size read from the page."""
    return Fundstelle(
        seite=seite.nummer, start=a, ende=b, text=seite.text[a:b], art=ziel.art,
        regel=f"unscharf.{ziel.regel}", pseudonym=ziel.pseudonym,
        rects=[tuple(r) for r in seite.rects(a, b)], schriftgroesse=seite.schriftgroesse(a, b),
        rolle=ziel.rolle, quelle=quelle,
    )


def finde_unscharf(
    seite: Seitentext, ziele: list[Ziel], vorhandene: list[Fundstelle],
    fachbegriffe: Fachbegriffe | None = None, grossschreibung: bool = False,
    quelle: str = "ocr",
) -> list[Fundstelle]:
    """Fuzzy hits on an OCR page (or a transcript) that do not overlap exact hits.

    An inexact match whose text is a medical term ("Bauch" for a patient
    called Bauer) is not a hit; exact spellings still are.  With
    ``grossschreibung`` names and places must start with a capital letter,
    which keeps "lang" or "hoch" safe in correctly cased text (OCR text is
    not trusted that far).
    """
    schutz = fachbegriffe.spannen(seite.text) if fachbegriffe is not None else []
    lauf = _Suchlauf(seite, _tokens(seite.text), fachbegriffe, schutz, grossschreibung)
    belegt = [(f.start, f.ende) for f in vorhandene if f.ende > f.start]
    gewaehlt = _ohne_ueberlappung(_kandidaten(lauf, ziele), belegt)
    treffer = [_fundstelle(seite, a, b, ziel, quelle) for a, b, ziel in gewaehlt]
    treffer.sort(key=lambda f: f.start)
    return treffer
