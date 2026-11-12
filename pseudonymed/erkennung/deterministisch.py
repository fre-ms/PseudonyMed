"""Deterministic rules built from a patient's identifiers, and the matcher.

Matching is longest-match-first across all rules, so "Max Mustermann" wins
over the separate surname and first-name rules and yields one pseudonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pseudonymed.dokument.textebene import Box, Seitentext
from pseudonymed.erkennung import varianten as v
from pseudonymed.erkennung.muster import GLOBALE_MUSTER
from pseudonymed.kern.modell import Art, Fundstelle, Patient, Person, Rolle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


@dataclass
class Regel:
    """One compiled pattern with the pseudonym it yields and its priority.

    Among candidates of equal length the higher priority wins.  ``rolle`` says
    whose identifier the rule stands for (patient, relative, referrer) and is
    None for the user's own terms and the global patterns.
    """

    id: str
    muster: re.Pattern[str]
    art: Art
    pseudonym: str
    prioritaet: int = 0
    rolle: Rolle | None = None


Treffer = tuple[int, int, Regel]


# -- the rule families ----------------------------------------------------------------------------
def _regel(id_: str, muster: str | None, art: Art, pseudonym: str, prioritaet: int,
           rolle: Rolle | None) -> list[Regel]:
    """The compiled rule as a one-element list, empty when the pattern builder refused
    the input as too short to be safe.  The pseudonym is handed in ready-made, so the
    assignment has recorded it even when no rule results."""
    if not muster:
        return []
    return [Regel(id_, re.compile(muster), art, pseudonym, prioritaet, rolle)]


def _namensregeln(prefix: str, person: Person, pseudonym: str, rolle: Rolle) -> list[Regel]:
    """Full name, surname, parts of a compound surname, first names and the birth name."""
    regeln = _regel(f"{prefix}.vollname", v.vollname_muster(person.vornamen, person.nachname),
                    Art.NAME, pseudonym, 30, rolle)
    regeln += _regel(f"{prefix}.nachname", v.name_muster(person.nachname), Art.NACHNAME,
                     pseudonym, 20, rolle)
    for teil in v.nachname_teile(person.nachname):
        regeln += _regel(f"{prefix}.nachname.teil", v.name_muster(teil), Art.NACHNAME, pseudonym,
                         10, rolle)
    for vorname in person.vornamen:
        if len(vorname) >= 3:
            regeln += _regel(f"{prefix}.vorname", v.name_muster(vorname), Art.VORNAME, pseudonym,
                             10, rolle)
    if person.geburtsname:
        regeln += _regel(f"{prefix}.geburtsname.vollname",
                         v.vollname_muster(person.vornamen, person.geburtsname), Art.NAME,
                         pseudonym, 30, rolle)
        regeln += _regel(f"{prefix}.geburtsname", v.name_muster(person.geburtsname),
                         Art.GEBURTSNAME, pseudonym, 20, rolle)
    return regeln


def _geburtsdatumsregel(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """The birth date in every common spelling; nothing when the date is unknown."""
    if not patient.geburtsdatum:
        return []
    return _regel("datum.geburtsdatum", v.datum_muster(patient.geburtsdatum), Art.GEBURTSDATUM,
                  vergabe.geburtsdatum(), 20, Rolle.PATIENT)


def _adressregeln(prefix: str, person: Person, rolle: Rolle,
                  vergabe: Pseudonymvergabe) -> list[Regel]:
    """Street, postcode with town, and the town on its own, each with its own code."""
    regeln: list[Regel] = []
    if person.strasse:
        regeln += _regel(f"{prefix}.strasse", v.adresse_muster(person.strasse), Art.ADRESSE,
                         vergabe.strasse(person), 20, rolle)
    if person.ort:
        regeln += _regel(f"{prefix}.plz_ort", v.plz_ort_muster(person.plz, person.ort), Art.ORT,
                         vergabe.plz_ort(person), 20, rolle)
        regeln += _regel(f"{prefix}.ort", v.ort_muster(person.ort), Art.ORT, vergabe.ort(person),
                         15, rolle)
    return regeln


def _nummernregeln(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """The patient's phone numbers, e-mail addresses, insurance number, patient id and
    file numbers."""
    regeln: list[Regel] = []
    for t in patient.telefone:
        regeln += _regel("telefon", v.telefon_muster(t), Art.TELEFON, vergabe.telefon(), 20,
                         Rolle.PATIENT)
    for e in patient.emails:
        regeln += _regel("email", v.email_muster(e), Art.EMAIL, vergabe.email(), 20, Rolle.PATIENT)
    if patient.versichertennummer:
        regeln += _regel("versichertennummer", v.exakt_muster(patient.versichertennummer),
                         Art.VERSICHERTENNUMMER, vergabe.versichertennummer(), 25, Rolle.PATIENT)
    if patient.patienten_id:
        regeln += _regel("patienten_id", v.nummer_muster(patient.patienten_id), Art.PATIENTEN_ID,
                         vergabe.patienten_id(), 25, Rolle.PATIENT)
    for a in patient.aktenzeichen:
        regeln += _regel("aktenzeichen", v.exakt_muster(a), Art.AKTENZEICHEN,
                         vergabe.aktenzeichen(), 25, Rolle.PATIENT)
    return regeln


def _kontaktregeln(prefix: str, person: Person, rolle: Rolle) -> list[Regel]:
    """Phone numbers and e-mail addresses of a relative or referrer; they share one
    placeholder each and get no code of their own."""
    regeln: list[Regel] = []
    for t in person.telefone:
        regeln += _regel(f"{prefix}.telefon", v.telefon_muster(t), Art.TELEFON, "[Telefon]", 20,
                         rolle)
    for e in person.emails:
        regeln += _regel(f"{prefix}.email", v.email_muster(e), Art.EMAIL, "[E-Mail]", 20, rolle)
    return regeln


def _angehoerigenregeln(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """Names, address and contact details of every relative, numbered in order."""
    regeln: list[Regel] = []
    for i, person in enumerate(patient.angehoerige, 1):
        pseudonym = vergabe.angehoerige(i, person)
        prefix = f"angehoerige{i}"
        regeln += _namensregeln(prefix, person, pseudonym, Rolle.ANGEHOERIGE)
        regeln += _adressregeln(prefix, person, Rolle.ANGEHOERIGE, vergabe)
        regeln += _kontaktregeln(prefix, person, Rolle.ANGEHOERIGE)
    return regeln


def _zuweiserregeln(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """Referring physicians: the name with its academic titles (so that "Dr. Z1" does not
    end up as "Dr. Dr. Z1"), parts of a compound surname, address and contact details."""
    regeln: list[Regel] = []
    for i, person in enumerate(patient.zuweiser, 1):
        pseudonym = vergabe.zuweiser(i, person)
        prefix = f"zuweiser{i}"
        regeln += _regel(f"{prefix}.name", v.titel_name_muster(person.vornamen, person.nachname),
                         Art.NAME, pseudonym, 30, Rolle.ZUWEISER)
        for teil in v.nachname_teile(person.nachname):
            regeln += _regel(f"{prefix}.nachname.teil", v.name_muster(teil), Art.NACHNAME,
                             pseudonym, 10, Rolle.ZUWEISER)
        regeln += _adressregeln(prefix, person, Rolle.ZUWEISER, vergabe)
        regeln += _kontaktregeln(prefix, person, Rolle.ZUWEISER)
    return regeln


def _begriffsregeln(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """The user's own terms (employer, nursing service, …), numbered in order."""
    regeln: list[Regel] = []
    for i, b in enumerate(patient.begriffe, 1):
        regeln += _regel("begriff", v.begriff_muster(b), Art.BEGRIFF, vergabe.begriff(i, b), 15,
                         None)
    return regeln


def _globale_regeln(vergabe: Pseudonymvergabe) -> list[Regel]:
    """The patient-independent patterns (insurance number, IBAN, e-mail, labelled values)
    with the lowest priority, so that a patient rule wins at equal length."""
    return [Regel(id_, muster, art, vergabe.generisch(art), 5, None)
            for id_, muster, art in GLOBALE_MUSTER]


def regeln_fuer(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """Compile the rule set for one patient; pseudonyms come from ``vergabe``.

    The families keep this order: at equal length and priority the earlier rule
    wins (see :func:`finde_im_text`), and the assignment records the pseudonyms
    in the order they are asked for.
    """
    hauptperson = vergabe.hauptperson()
    return [
        *_namensregeln("name", patient, hauptperson, Rolle.PATIENT),
        *_geburtsdatumsregel(patient, vergabe),
        *_adressregeln("adresse", patient, Rolle.PATIENT, vergabe),
        *_nummernregeln(patient, vergabe),
        *_angehoerigenregeln(patient, vergabe),
        *_zuweiserregeln(patient, vergabe),
        *_begriffsregeln(patient, vergabe),
        *_globale_regeln(vergabe),
    ]


# -- matching -------------------------------------------------------------------------------------
def _treffer_spanne(text: str, regel: Regel, m: re.Match[str]) -> tuple[int, int]:
    """The span a match stands for: the ``wert`` group of a labelled pattern (the value
    behind "KVNR", not the label), otherwise the whole match; trimmed of the whitespace
    a pattern may have swallowed at either end."""
    if "wert" in regel.muster.groupindex and m.group("wert"):
        a, b = m.span("wert")
    else:
        a, b = m.start(), m.end()
    teil = text[a:b]
    a += len(teil) - len(teil.lstrip())
    return a, a + len(teil.strip())


def _kandidaten(text: str, regeln: list[Regel]) -> list[Treffer]:
    """Every non-empty match of every rule, in rule order; overlaps not yet resolved."""
    kandidaten: list[Treffer] = []
    for regel in regeln:
        for m in regel.muster.finditer(text):
            a, b = _treffer_spanne(text, regel, m)
            if b > a:
                kandidaten.append((a, b, regel))
    return kandidaten


def _ohne_ueberlappung(kandidaten: list[Treffer]) -> list[Treffer]:
    """Greedy choice in text order: the longest candidate first, at equal length the
    higher priority, then the earlier one; whatever overlaps a chosen hit is dropped."""
    belegt: list[tuple[int, int]] = []
    gewaehlt: list[Treffer] = []
    for a, b, regel in sorted(kandidaten, key=lambda k: (-(k[1] - k[0]), -k[2].prioritaet, k[0])):
        if any(a < y and b > x for x, y in belegt):
            continue
        belegt.append((a, b))
        gewaehlt.append((a, b, regel))
    return sorted(gewaehlt, key=lambda k: k[0])


def finde_im_text(text: str, regeln: list[Regel]) -> list[tuple[int, int, Regel]]:
    """Non-overlapping matches, longest first, then by rule priority."""
    return _ohne_ueberlappung(_kandidaten(text, regeln))


def verschmelze(treffer: list[tuple[int, int, Regel]], text: str) -> list[tuple[int, int, Regel]]:
    """Join neighbouring hits with the same pseudonym ("Street 1, 12345 Town" -> one
    "[Wohnort]") when only commas or whitespace lie between them."""
    ergebnis: list[tuple[int, int, Regel]] = []
    for a, b, regel in treffer:
        if ergebnis:
            pa, pb, pregel = ergebnis[-1]
            zwischen = text[pb:a]
            if (
                pregel.pseudonym == regel.pseudonym
                and "\n" not in zwischen
                and re.fullmatch(r"[\s,]*", zwischen)
            ):
                ergebnis[-1] = (pa, b, pregel if pregel.prioritaet >= regel.prioritaet else regel)
                continue
        ergebnis.append((a, b, regel))
    return ergebnis


def finde(seite: Seitentext, regeln: list[Regel]) -> list[Fundstelle]:
    """Run all rules over one page and return hits with rectangles."""
    fundstellen: list[Fundstelle] = []
    for a, b, regel in verschmelze(finde_im_text(seite.text, regeln), seite.text):
        fundstellen.append(
            Fundstelle(
                seite=seite.nummer,
                start=a,
                ende=b,
                text=seite.text[a:b],
                art=regel.art,
                regel=regel.id,
                pseudonym=regel.pseudonym,
                rects=[tuple(r) for r in seite.rects(a, b)],
                schriftgroesse=seite.schriftgroesse(a, b),
                rolle=regel.rolle,
            )
        )
    return fundstellen


# -- boxes drawn by the user ----------------------------------------------------------------------
def _als_box(rect: tuple[float, float, float, float]) -> Box:
    """The rectangle as a plain four-tuple, whatever sequence the caller handed in."""
    x0, y0, x1, y1 = rect
    return (x0, y0, x1, y1)


def _bedeckt(kasten: Box, box: Box) -> bool:
    """Whether the drawn box covers at least two fifths of the glyph box, so that a box
    drawn a little below the line's centre still takes the line."""
    x0, y0, x1, y1 = kasten
    breite = min(x1, box[2]) - max(x0, box[0])
    hoehe = min(y1, box[3]) - max(y0, box[1])
    if breite <= 0 or hoehe <= 0:
        return False
    flaeche = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
    return breite * hoehe >= 0.4 * flaeche


def _bedeckte_zeichen(seite: Seitentext, kasten: Box) -> list[int]:
    """Indices of the printed characters the drawn box covers."""
    return [i for i, box in enumerate(seite.boxen) if box is not None and _bedeckt(kasten, box)]


def _setzt_fort(seite: Seitentext, start: int, vorher: int, i: int) -> bool:
    """Whether character ``i`` continues the run begun at ``start``: the same line, and
    nothing but whitespace between the previous covered character and this one."""
    return seite.zeilen[i] == seite.zeilen[start] and all(
        seite.text[k].isspace() for k in range(vorher + 1, i))


def _zeilenlaeufe(seite: Seitentext, innen: list[int]) -> list[tuple[int, int]]:
    """The covered characters as ``(start, ende)`` runs, one per line of the box."""
    laeufe: list[tuple[int, int]] = []
    start = vorher = innen[0]
    for i in innen[1:]:
        if not _setzt_fort(seite, start, vorher, i):
            laeufe.append((start, vorher + 1))
            start = i
        vorher = i
    laeufe.append((start, vorher + 1))
    return laeufe


def _manueller_treffer(seite: Seitentext, start: int, ende: int, pseudonym: str,
                       quelle: str) -> Fundstelle:
    """The hit for one run of covered characters, with its rectangles and font size."""
    return Fundstelle(
        seite=seite.nummer, start=start, ende=ende, text=seite.text[start:ende],
        art=Art.MANUELL, regel=quelle, pseudonym=pseudonym,
        rects=[tuple(r) for r in seite.rects(start, ende)],
        schriftgroesse=seite.schriftgroesse(start, ende), quelle=quelle,
    )


def _kasten_ohne_text(seite: Seitentext, kasten: Box, pseudonym: str, quelle: str) -> Fundstelle:
    """The hit for a box over an image page: no text span, the rectangle only."""
    return Fundstelle(seite=seite.nummer, start=0, ende=0, text="", art=Art.MANUELL,
                      regel=quelle, pseudonym=pseudonym, rects=[kasten], schriftgroesse=9.0,
                      quelle=quelle)


def kasten_zu_fundstellen(
    seite: Seitentext, rect: tuple[float, float, float, float], pseudonym: str = "[geschwärzt]",
    quelle: str = "manuell",
) -> list[Fundstelle]:
    """A box drawn by the user becomes hits with text spans where the page has text.

    Characters that the box covers (at least two fifths of the glyph box, so a
    box drawn a little below the line's centre still takes the line) are grouped
    per line into one hit each, so that the text file, the layout check and the
    re-check see the same thing as the redaction.  Without characters (image
    page) the box is a hit of its own with the rectangle only.
    """
    kasten = _als_box(rect)
    innen = _bedeckte_zeichen(seite, kasten)
    if not innen:
        return [_kasten_ohne_text(seite, kasten, pseudonym, quelle)]
    treffer = [_manueller_treffer(seite, start, ende, pseudonym, quelle)
               for start, ende in _zeilenlaeufe(seite, innen)]
    # the drawn box itself is covered too, for ink that is not text (image, stamp)
    treffer[0].rects = treffer[0].rects + [kasten]
    return treffer
