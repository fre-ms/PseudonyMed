"""The detection over pages that were read: rules, vocabulary marks, identifiers,
medical content, NER and titles on the text layer; on OCR text additionally the fuzzy
search (before the identifiers) and the unreadable lines; codes on the page image; the
user's corrected text as text-only hits."""

from __future__ import annotations

import pymupdf

from pseudonymed.ablauf.optionen import Ablaufoptionen
from pseudonymed.ablauf.ruestzeug import Ruestzeug
from pseudonymed.ablauf.seiten import Seite
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.dokument import textdatei as textmodul
from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import codes as codemodul
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import identifikatoren as identmodul
from pseudonymed.erkennung import ner as nermodul
from pseudonymed.erkennung.deterministisch import finde
from pseudonymed.erkennung.unscharf import finde_unscharf
from pseudonymed.kern.modell import Fundstelle
from pseudonymed.pruefung.gegenprobe import toleranzschluessel


def ergaenze_treffer(seite: Seitentext, treffer: list[Fundstelle], quelle: str, r: Ruestzeug,
                     verschont: list[str] | None = None, ner: bool = True) -> None:
    """Identifiers, medical content, NER (when asked for) and titles over ``seite``,
    appended to ``treffer`` in place and in that order: every stage sees what the ones
    before found, and NER drops the partial hits it completes to a full name."""
    if r.identifikatoren is not None:
        treffer += identmodul.finde_identifikatoren(seite, r.identifikatoren, r.vergabe,
                                                    treffer, quelle)
    if r.medizin is not None:
        treffer += r.medizin.finde(seite, r.vergabe, treffer, quelle)
    if ner and r.ner is not None:
        treffer += nermodul.finde_ner(seite, r.ner, r.vergabe, treffer, quelle,
                                      r.fachbegriffe, verschont)
    if r.identifikatoren is not None:
        treffer += identmodul.finde_titel(seite, r.identifikatoren, r.vergabe, treffer, quelle)


def merke_fachbegriffe(eintrag: Seite, seite: Seitentext,
                       fachbegriffe: fachmodul.Fachbegriffe) -> None:
    """Remember where medical terms sit, for the green marks of the review step."""
    spannen = fachbegriffe.spannen(seite.text)
    eintrag.fachanzahl += len(spannen)
    eintrag.fachspannen += spannen
    for a, b in spannen:
        eintrag.fachrects += [tuple(r) for r in seite.rects(a, b)]


def _erkenne_textebene(eintrag: Seite, r: Ruestzeug, verschont: list[str]) -> None:
    """The rules first, then the vocabulary marks and the chain of searches on the text
    layer; a page without one keeps only what the rules found (nothing)."""
    textebene = eintrag.text
    eintrag.fundstellen = finde(textebene, r.regeln)
    eintrag.fachrects, eintrag.fachspannen, eintrag.fachanzahl = [], [], 0
    if not textebene.hat_textebene:
        return
    if r.fachbegriffe is not None:
        merke_fachbegriffe(eintrag, textebene, r.fachbegriffe)
    ergaenze_treffer(textebene, eintrag.fundstellen, "text", r, verschont)


def _unlesbare_zeilen(ocr: Seitentext, fundstellen: list[Fundstelle]) -> list[Fundstelle]:
    """Lines the OCR could not read, as hits, where no OCR hit covers them already."""
    belegt = [(f.start, f.ende) for f in fundstellen if f.quelle == "ocr"]
    return [u for u in ocrmodul.unlesbare_regionen(ocr)
            if not any(u.start < y and u.ende > x for x, y in belegt)]


def _erkenne_ocr(eintrag: Seite, ocr: Seitentext, r: Ruestzeug, opt: Ablaufoptionen,
                 verschont: list[str]) -> None:
    """The OCR reading: vocabulary marks, exact rules, fuzzy search, then the chain of
    searches over the OCR hits only; unreadable lines and the user's correction last."""
    if r.fachbegriffe is not None:
        merke_fachbegriffe(eintrag, ocr, r.fachbegriffe)
    genau = finde(ocr, r.regeln)
    for f in genau:
        f.quelle = "ocr"
    eintrag.fundstellen += genau + finde_unscharf(ocr, r.ziele, genau, r.fachbegriffe)
    ocr_treffer = [f for f in eintrag.fundstellen if f.quelle == "ocr"]
    ergaenze_treffer(ocr, ocr_treffer, "ocr", r, verschont)
    eintrag.fundstellen = [f for f in eintrag.fundstellen if f.quelle != "ocr"] + ocr_treffer
    if opt.unlesbar == "schwaerzen":
        eintrag.fundstellen += _unlesbare_zeilen(ocr, eintrag.fundstellen)
    if eintrag.korrektur is not None:
        eintrag.korrektur_treffer = korrektur_treffer(eintrag, r, opt)


def erkenne_seiten(doc: pymupdf.Document, seiten: list[Seite], r: Ruestzeug,
                   opt: Ablaufoptionen, verschont: list[str]) -> None:
    """Run the detection over pages that were read already; ``verschont`` collects the
    NER candidates the vocabulary protected."""
    for eintrag in seiten:
        _erkenne_textebene(eintrag, r, verschont)
        if eintrag.ocr is not None:
            _erkenne_ocr(eintrag, eintrag.ocr, r, opt, verschont)
        if opt.codes and codemodul.verfuegbar():
            eintrag.fundstellen += codemodul.finde_codes(doc[eintrag.nummer], eintrag.nummer)


def korrektur_treffer(eintrag: Seite, r: Ruestzeug, opt: Ablaufoptionen) -> list[Fundstelle]:
    """Rules, fuzzy search, identifiers and the chosen medical content over the user's
    corrected OCR text: text-only hits."""
    assert eintrag.korrektur is not None
    seite = textmodul.textseite(eintrag.korrektur, eintrag.nummer)
    treffer = finde(seite, r.regeln)
    if opt.unscharf_text:
        treffer += finde_unscharf(seite, r.ziele, treffer, r.fachbegriffe, grossschreibung=True)
    ergaenze_treffer(seite, treffer, "text", r, ner=False)
    for f in treffer:
        f.quelle = "korrektur"
        f.rects = []
    return treffer


def gemerkte_verwerfen(seiten: list[Seite], gemerkt: set[tuple[str, str]],
                       hinweise: list[str]) -> list[Fundstelle]:
    """Move hits the user discarded in an earlier session (same rule and text) out of
    the pages; they come back green and can be restored.  Adds a hint when any were."""
    verworfen: list[Fundstelle] = []
    if not gemerkt:
        return verworfen
    for s in seiten:
        for f in list(s.fundstellen):
            if f.text and toleranzschluessel(f.regel, f.text) in gemerkt:
                s.fundstellen.remove(f)
                verworfen.append(f)
    if verworfen:
        hinweise.append(f"{len(verworfen)} Treffer nach gespeicherter Entscheidung verworfen"
                        " (grün im Prüfschritt; Wiederherstellen hebt sie auf).")
    return verworfen
