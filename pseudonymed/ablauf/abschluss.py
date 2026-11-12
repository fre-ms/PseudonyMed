"""Phase two for PDFs and images (redact, clean, save, check, report) and the one-call
entry points of the pipeline."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pymupdf

from pseudonymed.ablauf.ausgabe import (
    bericht_schreiben,
    entscheidungen_merken,
    kapsel_schreiben,
    pseudonyme_von,
    restrisiko,
    vokabular_schreiben,
)
from pseudonymed.ablauf.lesen import lese
from pseudonymed.ablauf.optionen import Ablaufoptionen, Ergebnis, zielpfad
from pseudonymed.ablauf.ruestzeug import ruestzeug
from pseudonymed.ablauf.seiten import Seite, markdown_aus_text, text_mit_pseudonymen
from pseudonymed.ablauf.textdateien import Textvorbereitung, abschliesse_text, vorbereite_text
from pseudonymed.ablauf.vorbereitung import Vorbereitung, analysiere
from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import textdatei as textmodul
from pseudonymed.dokument.textabgleich import textebene_aus_korrektur
from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import codes as codemodul
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung.deterministisch import finde
from pseudonymed.erkennung.unscharf import finde_unscharf
from pseudonymed.kern.modell import Fundstelle, Gegenprobe, Laufprotokoll, Patient
from pseudonymed.pruefung.bericht import erstelle_bericht
from pseudonymed.pruefung.gegenprobe import (
    in_pseudonym,
    pruefe,
    pseudonymspannen_tolerant,
    toleranzschluessel,
)
from pseudonymed.pruefung.vorschau import schreibe_vorschau
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.schwaerzen.pdf import (
    Schwaerzoptionen,
    bereinige,
    rastere,
    rastere_seite,
    schwaerze_seite,
    speichere,
    unsichtbare_textebene,
)


def _korrekturebene(s: Seite, gerastert: bool) -> tuple[Seitentext, list[Fundstelle]]:
    """The corrected (or restructured) text, every word on the box of the OCR word it
    came from, so that selecting it in a reader covers the image word; on a rasterised
    page the hits get those boxes too."""
    assert s.ocr is not None and s.korrektur is not None
    ebene = textebene_aus_korrektur(s.korrektur, s.ocr)
    treffer = s.korrektur_treffer
    if gerastert:
        treffer = [dataclasses.replace(f, rects=[tuple(r) for r in ebene.rects(f.start, f.ende)])
                   for f in treffer]
    return ebene, treffer


def _textebene_einbetten(doc: pymupdf.Document, seiten: list[Seite], gerastert: set[int],
                         opt: Schwaerzoptionen) -> None:
    """Invisible text for pages that have none of their own: OCR pages get their OCR
    words; rasterised pages get their text-layer words and their pseudonyms as well."""
    for s in seiten:
        page = doc[s.nummer]
        mit_pseudonymen = s.nummer in gerastert
        if mit_pseudonymen:
            unsichtbare_textebene(page, s.text, s.fundstellen, mit_pseudonymen=True, opt=opt)
        if s.ocr is None:
            continue
        if s.korrektur is not None:
            ebene, treffer = _korrekturebene(s, mit_pseudonymen)
        else:
            ebene = s.ocr
            treffer = [f for f in s.fundstellen if f.quelle in ("ocr", "manuell")]
        unsichtbare_textebene(page, ebene, treffer, mit_pseudonymen=mit_pseudonymen, opt=opt)


def _schwaerze_seiten(doc: pymupdf.Document, original: pymupdf.Document, seiten: list[Seite],
                      opt: Schwaerzoptionen, warnungen: list[str]) -> set[int]:
    """Redact every page; a page whose text layout shifted is rasterised from the
    original instead.  Returns the numbers of the rasterised pages."""
    gerastert: set[int] = set()
    for s in seiten:
        lage = schwaerze_seite(doc[s.nummer], s.text, s.fundstellen, opt)
        if not lage.lage_intakt:
            rastere_seite(doc, s.nummer, original, s.text, s.fundstellen, opt)
            gerastert.add(s.nummer)
            warnungen.append(f"Seite {s.nummer + 1}: Textlayout nach der Redaktion verschoben,"
                             " Seite deshalb gerastert.")
    return gerastert


def _gerasterte_ausgabe(doc: pymupdf.Document, seiten: list[Seite], opt: Schwaerzoptionen,
                        hinweise: list[str]) -> pymupdf.Document:
    """The output as page images, with the invisible text layer of every page when
    asked for."""
    ausgabe = rastere(doc, opt.dpi)
    if opt.textebene:
        _textebene_einbetten(ausgabe, seiten, {s.nummer for s in seiten}, opt)
    bereinige(ausgabe, vermerk=opt.vermerk)
    hinweise.append("Ausgabe gerastert.")
    return ausgabe


def _schreibe_pdf(v: Vorbereitung, ziel: Path, warnungen: list[str],
                  hinweise: list[str]) -> None:
    """Redact copies of the documents, clean them, rasterise when asked, save to ``ziel``."""
    opt = v.opt.schwaerzen
    doc = pymupdf.open("pdf", v.doc.tobytes())
    original = pymupdf.open("pdf", v.original.tobytes())
    try:
        gerastert = _schwaerze_seiten(doc, original, v.seiten, opt, warnungen)
        if opt.textebene and not opt.rastern:
            _textebene_einbetten(doc, v.seiten, gerastert, opt)
        bereinige(doc, vermerk=opt.vermerk)
        ausgabe = _gerasterte_ausgabe(doc, v.seiten, opt, hinweise) if opt.rastern else doc
        ziel.parent.mkdir(parents=True, exist_ok=True)
        speichere(ausgabe, ziel)
        if ausgabe is not doc:
            ausgabe.close()
    finally:
        doc.close()
        original.close()


def _ocr_resttreffer(page: pymupdf.Page, s: Seite, v: Vorbereitung, gegenprobe: Gegenprobe,
                     pseudonyme: list[str]) -> None:
    """Read the redacted page again and record every identifier still legible, unless it
    sits inside a pseudonym or the user tolerated it."""
    assert v.backend is not None
    nachher = s.ocr_lesen(page, v.backend)
    spannen = pseudonymspannen_tolerant(nachher.text, pseudonyme)
    genau = finde(nachher, v.regeln)
    fach = fachmodul.standard() if v.opt.fachbegriffe else None
    toleriert = v.toleriert
    for f in genau + finde_unscharf(nachher, v.ziele, genau, fach):
        if in_pseudonym(f.start, f.ende, spannen):
            continue
        if toleranzschluessel(f.regel, f.text) in toleriert:
            if not v.opt.schwaerzen.textebene:  # else the text layer counted it
                gegenprobe.belassen.append(f.regel)
            continue
        eintrag = f.ohne_klartext()
        eintrag["quelle"] = "ocr"
        gegenprobe.resttreffer.append(eintrag)


def _gegenprobe_bilder(doc: pymupdf.Document, v: Vorbereitung, gegenprobe: Gegenprobe,
                       pseudonyme: list[str]) -> None:
    """OCR the output pages that were read by OCR and look for leftovers (exact and
    fuzzy); re-scan codes."""
    for s in v.seiten:
        if s.nummer >= len(doc):
            continue
        if s.ocr is not None and v.backend is not None:
            _ocr_resttreffer(doc[s.nummer], s, v, gegenprobe, pseudonyme)
        if v.opt.codes and codemodul.verfuegbar():
            for f in codemodul.finde_codes(doc[s.nummer], s.nummer):
                gegenprobe.resttreffer.append(f.ohne_klartext())


def _kontrolliere(v: Vorbereitung, ziel: Path, text: str,
                  pseudonyme: list[str]) -> tuple[Gegenprobe, int, list[Path]]:
    """Open the written file and check it: text search, OCR of the OCR pages, codes;
    the preview images when asked for.  Returns the check, the page count, the previews."""
    vorschau: list[Path] = []
    with pymupdf.open(ziel) as kontrolle:
        gegenprobe = pruefe(kontrolle, v.regeln, zusatztexte=[text] if v.opt.textdatei else [],
                            toleriert=v.toleriert, pseudonyme=pseudonyme)
        _gegenprobe_bilder(kontrolle, v, gegenprobe, pseudonyme)
        seitenzahl = len(kontrolle)
        if v.opt.vorschau == "immer" or (v.opt.vorschau == "auto" and v.ocr_seiten):
            vorschau = schreibe_vorschau(kontrolle, zielpfad(v.quelle, v.opt, "_vorschau"))
    return gegenprobe, seitenzahl, vorschau


def _freigeben(v: Vorbereitung, ergebnis: Ergebnis, ziel: Path, text: str,
               hinweise: list[str]) -> None:
    """Keep the output: the PDF, the text and the Markdown when asked for, vocabulary,
    capsule."""
    opt = v.opt
    ergebnis.ziel_pdf = ziel
    if opt.textdatei:
        ergebnis.ziel_txt = zielpfad(v.quelle, opt, ".txt")
        ergebnis.ziel_txt.write_text(text, encoding="utf-8")
    if opt.markdown:
        ergebnis.ziel_md = zielpfad(v.quelle, opt, ".md")
        ergebnis.ziel_md.write_text(markdown_aus_text(text), encoding="utf-8")
    vokabular_schreiben(v, ergebnis, hinweise)
    kapsel_schreiben(v, ergebnis, hinweise)


def _berichtsoptionen(v: Vorbereitung) -> dict[str, object]:
    """The options for the report, with the clean-up applied per page (1-based)."""
    optionen = v.opt.als_dict()
    if v.aufbereitet:
        optionen["aufbereitung"] = {str(n + 1): a.als_dict() for n, a in v.aufbereitet.items()}
    return optionen


def _abschliesse_pdf(v: Vorbereitung, endgueltig: bool) -> Ergebnis:
    """Phase two for a PDF or image: redact, clean, save, check, report.  The caller
    closes the preparation."""
    opt, quelle = v.opt, v.quelle
    warnungen, hinweise = list(v.warnungen), list(v.hinweise)
    fundstellen = v.fundstellen + v.korrektur_treffer
    v.vergabe.registriere_treffer(fundstellen)
    text = text_mit_pseudonymen(v.seiten, opt.gliedern)
    ziel = zielpfad(quelle, opt)
    _schreibe_pdf(v, ziel, warnungen, hinweise)
    pseudonyme = pseudonyme_von(v, fundstellen)
    gegenprobe, seitenzahl, vorschau = _kontrolliere(v, ziel, text, pseudonyme)
    v.speicher.merke(v.patient, v.vergabe)
    if endgueltig:
        entscheidungen_merken(v)
    ergebnis = Ergebnis(quelle=quelle, bericht=None, fundstellen=fundstellen,  # type: ignore[arg-type]
                        kennung=v.kennung, text=text, vorschau=vorschau)
    ergebnis.restrisiko = restrisiko(v, text)
    if v.ohne_text:
        gegenprobe.anmerkungen.append("Seiten ohne Textebene konnten nicht geprüft werden")
    if gegenprobe.bestanden and not v.ohne_text:
        _freigeben(v, ergebnis, ziel, text, hinweise)
    else:
        ziel.unlink(missing_ok=True)
        warnungen.append("Nicht freigegeben: Ausgabe verworfen.")
    protokoll = Laufprotokoll(_berichtsoptionen(v), warnungen, hinweise,
                              ergebnis.restrisiko.als_dict())
    ergebnis.bericht = erstelle_bericht(quelle, ergebnis.ziel_pdf, seitenzahl,
                                        not v.ocr_seiten and not v.ohne_text, fundstellen,
                                        gegenprobe, protokoll)
    bericht_schreiben(ergebnis, opt)
    return ergebnis


def abschliessen(v: Vorbereitung | Textvorbereitung, endgueltig: bool = True) -> Ergebnis:
    """Phase two: redact, clean, save, check, report.

    Works on copies of the documents, so it can run more than once: the desktop
    runs it as the check before the export and again for the export itself.  With
    ``endgueltig`` the preparation is closed afterwards and the discard decisions
    are stored when the options ask for it.
    """
    if v.abgeschlossen:
        raise RuntimeError("Diese Vorbereitung wurde bereits abgeschlossen.")
    try:
        if isinstance(v, Textvorbereitung):
            return abschliesse_text(v, endgueltig)
        return _abschliesse_pdf(v, endgueltig)
    finally:
        if endgueltig:
            v.abgeschlossen = True
            v.schliessen()


def vorbereiten(
    pfad: str | Path,
    patient: Patient,
    opt: Ablaufoptionen | None = None,
    speicher: Pseudonymspeicher | None = None,
    aufbereitung: dict[int, aufmodul.Aufbereitung] | None = None,
) -> Vorbereitung | Textvorbereitung:
    """Phase one of :func:`pseudonymisiere`: read the file and find the hits.

    PDFs and images yield a :class:`Vorbereitung` (reading and detection in one go,
    see :func:`lese` and :func:`analysiere` for the two halves), text files
    (``.txt``, ``.md``, ``.srt``, ``.vtt``, ``.json``, ``.eml``) a
    :class:`Textvorbereitung`.  ``aufbereitung`` (per page, 0-based) replaces
    image-only pages by their cleaned-up image before OCR; without it the suggestion
    is applied when ``opt.aufbereiten`` is "auto".
    """
    opt = opt or Ablaufoptionen()
    quelle = Path(pfad)
    if textmodul.ist_textdatei(quelle):
        eigener_speicher = speicher is None
        speicher = speicher or Pseudonymspeicher()
        try:
            r = ruestzeug(patient, opt, speicher)
            return vorbereite_text(quelle, patient, opt, speicher, eigener_speicher, r)
        except Exception:
            if eigener_speicher:
                speicher.close()
            raise
    lesung = lese(quelle, opt, aufbereitung)
    try:
        return analysiere(lesung, patient, opt, speicher)
    except Exception:
        lesung.schliessen()
        raise


def pseudonymisiere(
    pfad: str | Path,
    patient: Patient,
    opt: Ablaufoptionen | None = None,
    speicher: Pseudonymspeicher | None = None,
) -> Ergebnis:
    """Pseudonymise one file in one go.  The output is only kept when the check passes."""
    return abschliessen(vorbereiten(pfad, patient, opt, speicher))
