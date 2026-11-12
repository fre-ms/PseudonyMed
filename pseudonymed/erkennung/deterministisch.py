"""Deterministic rules built from a patient's identifiers, and the matcher.

Matching is longest-match-first across all rules, so "Max Mustermann" wins
over the separate surname and first-name rules and yields one pseudonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import varianten as v
from pseudonymed.erkennung.muster import GLOBALE_MUSTER
from pseudonymed.kern.modell import Art, Fundstelle, Patient, Person, Rolle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


@dataclass
class Regel:
    id: str
    muster: re.Pattern[str]
    art: Art
    pseudonym: str
    prioritaet: int = 0
    rolle: Rolle | None = None


def regeln_fuer(patient: Patient, vergabe: Pseudonymvergabe) -> list[Regel]:
    """Compile the rule set for one patient; pseudonyms come from ``vergabe``."""
    regeln: list[Regel] = []

    def add(
        id_: str,
        pattern: str | None,
        art: Art,
        pseudonym: str,
        prio: int,
        rolle: Rolle | None,
    ) -> None:
        if pattern:
            regeln.append(Regel(id_, re.compile(pattern), art, pseudonym, prio, rolle))

    def namensregeln(prefix: str, person: Person, pseudonym: str, rolle: Rolle) -> None:
        add(f"{prefix}.vollname", v.vollname_muster(person.vornamen, person.nachname),
            Art.NAME, pseudonym, 30, rolle)
        add(f"{prefix}.nachname", v.name_muster(person.nachname), Art.NACHNAME, pseudonym, 20,
            rolle)
        for teil in v.nachname_teile(person.nachname):
            add(f"{prefix}.nachname.teil", v.name_muster(teil), Art.NACHNAME, pseudonym, 10, rolle)
        for vorname in person.vornamen:
            if len(vorname) >= 3:
                add(f"{prefix}.vorname", v.name_muster(vorname), Art.VORNAME, pseudonym, 10, rolle)
        if person.geburtsname:
            add(f"{prefix}.geburtsname.vollname",
                v.vollname_muster(person.vornamen, person.geburtsname), Art.NAME, pseudonym, 30,
                rolle)
            add(f"{prefix}.geburtsname", v.name_muster(person.geburtsname),
                Art.GEBURTSNAME, pseudonym, 20, rolle)

    p = patient
    ps = vergabe.hauptperson()
    namensregeln("name", p, ps, Rolle.PATIENT)
    if p.geburtsdatum:
        add("datum.geburtsdatum", v.datum_muster(p.geburtsdatum), Art.GEBURTSDATUM,
            vergabe.geburtsdatum(), 20, Rolle.PATIENT)
    def adressregeln(prefix: str, person: Person, rolle: Rolle) -> None:
        if person.strasse:
            add(f"{prefix}.strasse", v.adresse_muster(person.strasse), Art.ADRESSE,
                vergabe.strasse(person), 20, rolle)
        if person.ort:
            add(f"{prefix}.plz_ort", v.plz_ort_muster(person.plz, person.ort), Art.ORT,
                vergabe.plz_ort(person), 20, rolle)
            add(f"{prefix}.ort", v.ort_muster(person.ort), Art.ORT, vergabe.ort(person), 15,
                rolle)

    adressregeln("adresse", p, Rolle.PATIENT)
    for t in p.telefone:
        add("telefon", v.telefon_muster(t), Art.TELEFON, vergabe.telefon(), 20, Rolle.PATIENT)
    for e in p.emails:
        add("email", v.email_muster(e), Art.EMAIL, vergabe.email(), 20, Rolle.PATIENT)
    if p.versichertennummer:
        add("versichertennummer", v.exakt_muster(p.versichertennummer), Art.VERSICHERTENNUMMER,
            vergabe.versichertennummer(), 25, Rolle.PATIENT)
    if p.patienten_id:
        add("patienten_id", v.nummer_muster(p.patienten_id), Art.PATIENTEN_ID,
            vergabe.patienten_id(), 25, Rolle.PATIENT)
    for a in p.aktenzeichen:
        add("aktenzeichen", v.exakt_muster(a), Art.AKTENZEICHEN, vergabe.aktenzeichen(), 25,
            Rolle.PATIENT)

    for i, person in enumerate(p.angehoerige, 1):
        pseudonym = vergabe.angehoerige(i, person)
        namensregeln(f"angehoerige{i}", person, pseudonym, Rolle.ANGEHOERIGE)
        adressregeln(f"angehoerige{i}", person, Rolle.ANGEHOERIGE)
        for t in person.telefone:
            add(f"angehoerige{i}.telefon", v.telefon_muster(t), Art.TELEFON, "[Telefon]", 20,
                Rolle.ANGEHOERIGE)
        for e in person.emails:
            add(f"angehoerige{i}.email", v.email_muster(e), Art.EMAIL, "[E-Mail]", 20,
                Rolle.ANGEHOERIGE)

    for i, person in enumerate(p.zuweiser, 1):
        pseudonym = vergabe.zuweiser(i, person)
        add(f"zuweiser{i}.name", v.titel_name_muster(person.vornamen, person.nachname), Art.NAME,
            pseudonym, 30, Rolle.ZUWEISER)
        for teil in v.nachname_teile(person.nachname):
            add(f"zuweiser{i}.nachname.teil", v.name_muster(teil), Art.NACHNAME, pseudonym, 10,
                Rolle.ZUWEISER)
        adressregeln(f"zuweiser{i}", person, Rolle.ZUWEISER)
        for t in person.telefone:
            add(f"zuweiser{i}.telefon", v.telefon_muster(t), Art.TELEFON, "[Telefon]", 20,
                Rolle.ZUWEISER)
        for e in person.emails:
            add(f"zuweiser{i}.email", v.email_muster(e), Art.EMAIL, "[E-Mail]", 20, Rolle.ZUWEISER)

    for i, b in enumerate(p.begriffe, 1):
        add("begriff", v.begriff_muster(b), Art.BEGRIFF, vergabe.begriff(i, b), 15, None)

    for id_, muster, art in GLOBALE_MUSTER:
        regeln.append(Regel(id_, muster, art, vergabe.generisch(art), 5, None))
    return regeln


def finde_im_text(text: str, regeln: list[Regel]) -> list[tuple[int, int, Regel]]:
    """Non-overlapping matches, longest first, then by rule priority."""
    kandidaten: list[tuple[int, int, Regel]] = []
    for regel in regeln:
        for m in regel.muster.finditer(text):
            if "wert" in regel.muster.groupindex and m.group("wert"):
                a, b = m.span("wert")
            else:
                a, b = m.start(), m.end()
            # trim surrounding whitespace the pattern may have swallowed
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            if b > a:
                kandidaten.append((a, b, regel))
    kandidaten.sort(key=lambda k: (-(k[1] - k[0]), -k[2].prioritaet, k[0]))
    belegt: list[tuple[int, int]] = []
    gewaehlt: list[tuple[int, int, Regel]] = []
    for a, b, regel in kandidaten:
        if any(a < y and b > x for x, y in belegt):
            continue
        belegt.append((a, b))
        gewaehlt.append((a, b, regel))
    gewaehlt.sort(key=lambda k: k[0])
    return gewaehlt


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
    x0, y0, x1, y1 = rect

    def bedeckt(box: tuple[float, float, float, float]) -> bool:
        breite = min(x1, box[2]) - max(x0, box[0])
        hoehe = min(y1, box[3]) - max(y0, box[1])
        if breite <= 0 or hoehe <= 0:
            return False
        flaeche = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
        return breite * hoehe >= 0.4 * flaeche

    innen = [i for i, box in enumerate(seite.boxen) if box is not None and bedeckt(box)]
    if not innen:
        return [Fundstelle(seite=seite.nummer, start=0, ende=0, text="", art=Art.MANUELL,
                           regel=quelle, pseudonym=pseudonym, rects=[tuple(rect)],
                           schriftgroesse=9.0, quelle=quelle)]
    treffer: list[Fundstelle] = []
    start = innen[0]
    vorher = innen[0]
    for i in innen[1:] + [None]:
        zusammen = (
            i is not None and i == vorher + 1 and seite.zeilen[i] == seite.zeilen[start]
        ) or (i is not None and seite.zeilen[i] == seite.zeilen[start]
              and all(seite.text[k].isspace() for k in range(vorher + 1, i)))
        if not zusammen:
            ende = vorher + 1
            treffer.append(Fundstelle(
                seite=seite.nummer, start=start, ende=ende, text=seite.text[start:ende],
                art=Art.MANUELL, regel=quelle, pseudonym=pseudonym,
                rects=[tuple(r) for r in seite.rects(start, ende)],
                schriftgroesse=seite.schriftgroesse(start, ende), quelle=quelle,
            ))
            if i is None:
                break
            start = i
        vorher = i
    # the drawn box itself is covered too, for ink that is not text (image, stamp)
    treffer[0].rects = treffer[0].rects + [tuple(rect)]
    return treffer
