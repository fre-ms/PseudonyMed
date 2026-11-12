"""Re-check the pseudonymised document with the same rules that produced it.

Any hit here means the export must not be released.  OCR of the result
(for rasterised output and scans) follows in milestone M3.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pymupdf

from pseudonymed.dokument.textebene import Seitentext, lese_dokument
from pseudonymed.erkennung.deterministisch import Regel, finde, finde_im_text
from pseudonymed.erkennung.varianten import exakt_muster
from pseudonymed.kern.modell import Art, Gegenprobe, Rolle
from pseudonymed.schwaerzen.pdf import ERLAUBTE_METADATEN, VERMERK_PRAEFIX

ART_NAMEN = {
    Art.NAME: "Name", Art.VORNAME: "Vorname", Art.NACHNAME: "Nachname",
    Art.GEBURTSNAME: "Geburtsname", Art.GEBURTSDATUM: "Geburtsdatum", Art.ADRESSE: "Straße",
    Art.ORT: "Ort", Art.TELEFON: "Telefon", Art.EMAIL: "E-Mail",
    Art.VERSICHERTENNUMMER: "Versichertennummer", Art.PATIENTEN_ID: "Patienten-ID",
    Art.AKTENZEICHEN: "Aktenzeichen", Art.IBAN: "IBAN", Art.BEGRIFF: "Begriffe",
}


def geprueft_aus(regeln: Iterable[Regel]) -> list[str]:
    """Readable list of what the rules cover: the patient's identifiers by kind, then
    the people around them, for the report's "searched again" line."""
    namen: list[str] = []
    rollen: list[str] = []
    for r in regeln:
        if r.rolle == Rolle.ANGEHOERIGE:
            eintrag = "Angehörige"
        elif r.rolle == Rolle.ZUWEISER:
            eintrag = "Zuweiser"
        else:
            eintrag = ART_NAMEN.get(r.art, str(r.art))
        ziel = rollen if r.rolle in (Rolle.ANGEHOERIGE, Rolle.ZUWEISER) else namen
        if eintrag not in ziel:
            ziel.append(eintrag)
    return namen + rollen


def toleranzschluessel(regel: str, text: str) -> tuple[str, str]:
    """Key of an occurrence the user deliberately kept: rule and normalised text."""
    return regel, " ".join(text.split()).casefold()


def pseudonymspannen(text: str, pseudonyme: Iterable[str]) -> list[tuple[int, int]]:
    """Where the inserted pseudonyms sit in ``text``; rules must not fire inside them
    (a code such as "KAZ2Q4W" could otherwise look like a file number)."""
    spannen: list[tuple[int, int]] = []
    for p in pseudonyme:
        if not p:
            continue
        start = 0
        while True:
            i = text.find(p, start)
            if i < 0:
                break
            spannen.append((i, i + len(p)))
            start = i + 1
    return spannen


def in_pseudonym(a: int, b: int, spannen: list[tuple[int, int]]) -> bool:
    """Whether the span ``a``..``b`` overlaps one of the pseudonym spans."""
    return any(a < y and b > x for x, y in spannen)


_CODE = re.compile(r"\b[A-Z2-9]{6}(?:-\d+)?\b")


def pseudonymspannen_tolerant(text: str, pseudonyme: Iterable[str]) -> list[tuple[int, int]]:
    """Like :func:`pseudonymspannen`, for text that OCR read back: the codes inside the
    pseudonyms are also matched with the usual OCR confusions and regardless of the
    brackets and words around them, so that a re-read "(Ort AZT63Z)" still counts as
    the pseudonym and not as a file number."""
    pseudonyme = [p for p in pseudonyme if p]
    spannen = pseudonymspannen(text, pseudonyme)
    codes = {m.group(0) for p in pseudonyme for m in _CODE.finditer(p)}
    for code in codes:
        muster = exakt_muster(code)
        if muster:
            spannen += [(m.start(), m.end()) for m in re.finditer(muster, text)]
    return spannen


@dataclass(frozen=True)
class _Suchziele:
    """What one re-check searches for: the rules, the occurrences the user kept on
    purpose and the pseudonyms the redaction inserted, inside which no rule counts."""

    regeln: list[Regel]
    toleriert: frozenset[tuple[str, str]]
    pseudonyme: tuple[str, ...]

    def ist_toleriert(self, regel: str, text: str) -> bool:
        """Whether the user decided to keep this occurrence of ``regel``."""
        return toleranzschluessel(regel, text) in self.toleriert


def _klartexttreffer(seite: int, regel: Regel, quelle: str) -> dict[str, Any]:
    """Report entry for a hit in plain text, which has no rectangles to point at."""
    return {"seite": seite, "art": str(regel.art), "regel": regel.id,
            "pseudonym": regel.pseudonym, "rects": [], "quelle": quelle}


def _pruefe_textebene(ziele: _Suchziele, seite: Seitentext, ergebnis: Gegenprobe,
                      gesehen: set[tuple[int, str, str]]) -> None:
    """Runs the rules over one page's reconstructed text layer and reports residual hits
    with their rectangles.  Every hit, reported or not, is added to ``gesehen`` so that
    the plain-text pass does not report it a second time."""
    spannen = pseudonymspannen(seite.text, ziele.pseudonyme)
    for f in finde(seite, ziele.regeln):
        gesehen.add((f.seite, f.regel, f.text))
        if in_pseudonym(f.start, f.ende, spannen):
            continue
        if ziele.ist_toleriert(f.regel, f.text):
            ergebnis.belassen.append(f.regel)
            continue
        ergebnis.resttreffer.append(f.ohne_klartext())


def _pruefe_klartext(ziele: _Suchziele, nummer: int, text: str, ergebnis: Gegenprobe,
                     gesehen: set[tuple[int, str, str]]) -> None:
    """Runs the rules over the plain text PyMuPDF extracts from one page, which can join
    characters differently from the reconstructed layer.  Hits the text-layer pass saw
    are skipped; new ones are reported without rectangles and remembered in turn."""
    spannen = pseudonymspannen(text, ziele.pseudonyme)
    for a, b, regel in finde_im_text(text, ziele.regeln):
        schluessel = (nummer, regel.id, text[a:b])
        if schluessel in gesehen:
            continue
        if ziele.ist_toleriert(regel.id, text[a:b]):
            ergebnis.belassen.append(regel.id)
            continue
        if in_pseudonym(a, b, spannen):
            continue
        gesehen.add(schluessel)
        ergebnis.resttreffer.append(_klartexttreffer(nummer + 1, regel, "plaintext"))


def _pruefe_zusatztext(ziele: _Suchziele, nummer: int, text: str, ergebnis: Gegenprobe) -> None:
    """Runs the rules over one companion text such as the exported transcript.  It
    repeats the pages, so tolerated occurrences are not counted in ``belassen`` again."""
    spannen = pseudonymspannen(text, ziele.pseudonyme)
    for a, b, regel in finde_im_text(text, ziele.regeln):
        if ziele.ist_toleriert(regel.id, text[a:b]) or in_pseudonym(a, b, spannen):
            continue
        ergebnis.resttreffer.append(_klartexttreffer(0, regel, f"zusatztext{nummer}"))


def _metadatum_unbedenklich(feld: str, wert: str | None) -> bool:
    """Whether a metadata entry may stay: PyMuPDF's format and encryption fields, empty
    values, the tool's own producer and creator lines and the release note."""
    if feld in ("format", "encryption") or not wert:
        return True
    if ERLAUBTE_METADATEN.get(feld) == wert:
        return True
    return feld == "subject" and wert.startswith(VERMERK_PRAEFIX)


def _pruefe_metadaten(doc: pymupdf.Document, ergebnis: Gegenprobe) -> None:
    """Flags every metadata entry that still carries content, and an XMP stream."""
    for feld, wert in doc.metadata.items():
        if _metadatum_unbedenklich(feld, wert):
            continue
        ergebnis.metadaten_leer = False
        ergebnis.anmerkungen.append(f"Metadatum '{feld}' ist nicht leer")
    if doc.xref_xml_metadata() > 0:
        ergebnis.metadaten_leer = False
        ergebnis.anmerkungen.append("XMP-Metadaten vorhanden")


def _pruefe_struktur(doc: pymupdf.Document, ergebnis: Gegenprobe) -> None:
    """Flags embedded files, bookmarks, annotations and form fields: a clean export has
    none of them, and each could carry text the rules never saw."""
    befunde = [
        (doc.embfile_count(), "{n} eingebettete Datei(en)"),
        (len(doc.get_toc()), "Lesezeichen vorhanden"),
        (sum(len(list(p.annots())) for p in doc), "{n} Anmerkung(en) vorhanden"),
        (sum(len(list(p.widgets())) for p in doc), "{n} Formularfeld(er) vorhanden"),
    ]
    for anzahl, meldung in befunde:
        if anzahl:
            ergebnis.struktur_sauber = False
            ergebnis.anmerkungen.append(meldung.format(n=anzahl))


def pruefe(
    doc: pymupdf.Document, regeln: list[Regel], zusatztexte: Iterable[str] = (),
    toleriert: Iterable[tuple[str, str]] = (), pseudonyme: Iterable[str] = (),
) -> Gegenprobe:
    """Re-run the rules on the output.  ``toleriert`` lists occurrences (rule, text)
    the user decided to keep; ``pseudonyme`` are the strings the redaction inserted,
    inside which no rule counts."""
    ergebnis = Gegenprobe(geprueft=geprueft_aus(regeln))
    ziele = _Suchziele(regeln, frozenset(toleriert), tuple(p for p in pseudonyme if p))
    gesehen: set[tuple[int, str, str]] = set()
    for seite in lese_dokument(doc):
        _pruefe_textebene(ziele, seite, ergebnis, gesehen)
    for i, page in enumerate(doc):
        _pruefe_klartext(ziele, i, page.get_text("text"), ergebnis, gesehen)
    for n, text in enumerate(zusatztexte, 1):  # repeats the pages: count nothing twice
        _pruefe_zusatztext(ziele, n, text, ergebnis)
    _pruefe_metadaten(doc, ergebnis)
    _pruefe_struktur(doc, ergebnis)
    return ergebnis
