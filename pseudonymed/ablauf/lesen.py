"""Phase zero and the reading phase: open the file, clean up image pages, read the text
layer and run OCR where a page needs it.  No patient, no detection yet."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pymupdf
from optdoc import cleanup as aufmodul
from optdoc import ocr as ocrmodul
from optdoc import structure as gliedmodul
from optdoc.loading import HEIC_SUFFIXES, IMAGE_SUFFIXES
from optdoc.pagetext import PageText, read_text_layer

from pseudonymed.ablauf.optionen import Ablaufoptionen
from pseudonymed.ablauf.seiten import Seite, lesetext, unveraendert
from pseudonymed.dokument import anbindung
from pseudonymed.dokument.anbindung import oeffne

_VORSCHLAG_DPI = 150  # resolution the clean-up suggestion looks at a page with
_MAX_PIXEL_HOCHSKALIERT = 9000  # cap for the OCR image of an upscaled page


def dokumente_schliessen(*docs: pymupdf.Document) -> None:
    """Close every document, ignoring the ones that are closed already."""
    for d in docs:
        try:
            d.close()
        except Exception:  # already closed
            pass


def _ist_foto(quelle: Path) -> bool:
    """True for an image file (photo, scan as JPEG, PNG, HEIC) rather than a PDF."""
    return quelle.suffix.lower() in IMAGE_SUFFIXES | HEIC_SUFFIXES


def _vorschlag(page: pymupdf.Page, foto: bool) -> aufmodul.CleanupPlan:
    """The automatic clean-up suggestion for one image-only page."""
    bild, skala = aufmodul.render(page, _VORSCHLAG_DPI)
    return aufmodul.suggest(bild, foto, skala, aufmodul.native_dpi(page))


@dataclass
class Vorlage:
    """Phase zero: the file is open and its pages are known, nothing is read in detail.

    Pages without a text layer (scans, photos) are candidates for the clean-up step;
    each comes with an automatic suggestion.  Call ``schliessen`` when done.
    """

    quelle: Path
    doc: pymupdf.Document
    foto: bool
    kandidaten: list[int]
    vorschlaege: dict[int, aufmodul.CleanupPlan]

    @property
    def seitenzahl(self) -> int:
        """Number of pages of the open document."""
        return len(self.doc)

    def seitenbild(self, nummer: int, dpi: int = 100) -> bytes:
        """PNG of page ``nummer`` (0-based) as it is."""
        return self.doc[nummer].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def vorschau(self, nummer: int, a: aufmodul.CleanupPlan,
                 dpi: int = 100) -> tuple[bytes, float]:
        """PNG of the processed page and its pixels per point."""
        return aufmodul.preview_png(self.doc[nummer], a, dpi)

    def seitenbreite(self, nummer: int, a: aufmodul.CleanupPlan) -> float:
        """Width in points of page ``nummer`` after the rotation ``a`` asks for."""
        r = self.doc[nummer].rect
        return r.width if a.rotation in (0, 180) else r.height

    def schliessen(self) -> None:
        """Release the document."""
        dokumente_schliessen(self.doc)


def lade(pfad: str | Path, opt: Ablaufoptionen | None = None) -> Vorlage:
    """Open a PDF or image and find the pages worth cleaning up before OCR."""
    opt = opt or Ablaufoptionen()
    quelle = Path(pfad)
    doc = oeffne(quelle, opt.papierformat)
    foto = _ist_foto(quelle)
    kandidaten = [i for i, page in enumerate(doc) if not read_text_layer(page, i).has_text_layer]
    vorschlaege: dict[int, aufmodul.CleanupPlan] = {}
    if aufmodul.available() and opt.aufbereiten != "aus":
        vorschlaege = {i: _vorschlag(doc[i], foto) for i in kandidaten}
    return Vorlage(quelle, doc, foto, kandidaten, vorschlaege)


def _vorgeschlagene(doc: pymupdf.Document, foto: bool,
                    plan: dict[int, aufmodul.CleanupPlan]) -> dict[int, aufmodul.CleanupPlan]:
    """Suggestions for the image-only pages the plan does not cover yet, active ones only."""
    neu: dict[int, aufmodul.CleanupPlan] = {}
    for i, page in enumerate(doc):
        if i in plan or read_text_layer(page, i).has_text_layer:
            continue
        a = _vorschlag(page, foto)
        if a.active:
            neu[i] = a
    return neu


def _aufbereitungsplan(
    doc: pymupdf.Document, quelle: Path, opt: Ablaufoptionen,
    explizit: dict[int, aufmodul.CleanupPlan] | None,
) -> dict[int, aufmodul.CleanupPlan]:
    """Which pages get which clean-up: the explicit choice, else the suggestion for
    every image-only page when ``aufbereiten`` is "auto"."""
    plan = {n: a for n, a in (explizit or opt.aufbereitung or {}).items() if a.active}
    automatisch = (opt.aufbereiten == "auto" and aufmodul.available()
                   and explizit is None and opt.aufbereitung is None)
    if automatisch:
        plan.update(_vorgeschlagene(doc, _ist_foto(quelle), plan))
    return plan


def _seite_mit_plan(nummer: int, textebene: PageText, opt: Ablaufoptionen,
                    plan: aufmodul.CleanupPlan | None) -> Seite:
    """A page entry with the OCR resolution and finish its clean-up plan asks for: an
    upscaled page is read at that multiple of the OCR resolution (capped), a page in
    letter mode gets the letter-aware finish before the engine."""
    eintrag = Seite(nummer, textebene)
    faktor = max(1, plan.upscale) if plan is not None else 1
    eintrag.ocr_dpi = opt.ocr_dpi * faktor
    eintrag.ocr_max_pixel = min(ocrmodul.MAX_PIXELS * faktor, _MAX_PIXEL_HOCHSKALIERT)
    if plan is not None and plan.letter_mode:
        eintrag.ocr_filter = functools.partial(aufmodul.letter_finish, factor=faktor)
    return eintrag


def _lese_roh(
    doc: pymupdf.Document, opt: Ablaufoptionen, backend: ocrmodul.OCRBackend | None,
    plaene: dict[int, aufmodul.CleanupPlan] | None = None,
) -> tuple[list[Seite], list[int]]:
    """Read every page: text layer and, where needed and possible, OCR.  No detection
    yet.  Returns the pages and the numbers (1-based) of pages that needed OCR but got
    none."""
    seiten: list[Seite] = []
    ohne_text: list[int] = []
    for i, page in enumerate(doc):
        eintrag = _seite_mit_plan(i, read_text_layer(page, i), opt, (plaene or {}).get(i))
        if ocrmodul.needs_ocr(page, eintrag.text):
            if backend is None:
                ohne_text.append(i + 1)
            else:
                eintrag.ocr = eintrag.ocr_lesen(page, backend)
                eintrag.gliederung = gliedmodul.analyse_page_structure(page, eintrag.ocr)
        seiten.append(eintrag)
    return seiten, ohne_text


@dataclass
class Lesung:
    """The reading phase: the pages of a PDF or image are read (text layer, OCR after
    the clean-up) but not yet searched.  The desktop lets the user check and correct
    the recognised text here before :func:`analysiere` looks for identifiers."""

    ist_text: ClassVar[bool] = False

    quelle: Path
    opt: Ablaufoptionen
    doc: pymupdf.Document
    original: pymupdf.Document
    backend: ocrmodul.OCRBackend | None
    seiten: list[Seite]
    ohne_text: list[int]
    plan: dict[int, aufmodul.CleanupPlan] = field(default_factory=dict)
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    verbraucht: bool = False

    @property
    def seitenzahl(self) -> int:
        """Number of pages read."""
        return len(self.seiten)

    @property
    def ocr_seiten(self) -> list[int]:
        """Numbers (1-based) of the pages that were read by OCR."""
        return [s.nummer + 1 for s in self.seiten if s.ocr is not None]

    def hat_ocr(self, nummer: int) -> bool:
        """True when page ``nummer`` (0-based) was read by OCR."""
        return self.seiten[nummer].ocr is not None

    def seitenbild(self, nummer: int, dpi: int = 100) -> bytes:
        """PNG of the page as the OCR saw it (cleaned up)."""
        return self.doc[nummer].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def text(self, nummer: int) -> str:
        """Text of the page: the (corrected, structured) OCR text for OCR pages, else
        the text layer."""
        s = self.seiten[nummer]
        return lesetext(s, self.opt.gliedern) if s.ocr is not None else s.text.text

    def ocr_text(self, nummer: int) -> str:
        """The OCR text of page ``nummer`` as shown: corrected, else as read."""
        return lesetext(self.seiten[nummer], self.opt.gliedern)

    def setze_ocr_text(self, nummer: int, text: str) -> None:
        """Keep the user's correction of the OCR text; the text as read (raw or
        structured) resets it."""
        s = self.seiten[nummer]
        if s.ocr is None:
            raise ValueError("Diese Seite hat keinen OCR-Text.")
        s.korrektur = None if unveraendert(s, text) else text

    def schliessen(self) -> None:
        """Release both documents."""
        dokumente_schliessen(self.doc, self.original)


def _lesehinweise(
    plan: dict[int, aufmodul.CleanupPlan], ohne_text: list[int], seiten: list[Seite],
    backend: ocrmodul.OCRBackend | None,
) -> tuple[list[str], list[str]]:
    """What the reading has to tell: the clean-ups applied, pages nobody could read,
    the pages OCR read and how sure it was.  Returns warnings and hints."""
    hinweise = [f"Seite {nummer + 1} vor der OCR aufbereitet:"
                f" {anbindung.beschreibung(plan[nummer])}." for nummer in sorted(plan)]
    warnungen: list[str] = []
    if ohne_text:
        warnungen.append(
            "Seite(n) ohne Textebene und ohne OCR: " + ", ".join(map(str, ohne_text))
            + " – OCR-Backend installieren (Extra 'ocr' oder Tesseract) oder --ocr prüfen."
        )
    gelesen = [(s.nummer + 1, s.ocr) for s in seiten if s.ocr is not None]
    if gelesen:
        unsicher = sum(ocr.uncertain_words for _, ocr in gelesen)
        hinweise.append(
            f"Scan/Foto: Seite(n) {', '.join(str(n) for n, _ in gelesen)} per OCR gelesen"
            f" ({backend.name if backend else '?'}, {unsicher} unsichere Wörter)."
        )
        hinweise.append("OCR-Seiten: Handschrift wird nicht erkannt; erkannten Text und"
                        " Vorschaubilder vor der Weitergabe prüfen.")
    return warnungen, hinweise


def lese(
    pfad: str | Path,
    opt: Ablaufoptionen | None = None,
    aufbereitung: dict[int, aufmodul.CleanupPlan] | None = None,
) -> Lesung:
    """Reading phase for a PDF or image: clean-up, text layer, OCR.  No patient needed."""
    opt = opt or Ablaufoptionen()
    quelle = Path(pfad)
    backend = anbindung.backend(opt.ocr, opt.ocr_sprachen, tabellen=opt.ocr_tabellen)
    doc = oeffne(quelle, opt.papierformat)
    original = oeffne(quelle, opt.papierformat)
    try:
        plan = _aufbereitungsplan(doc, quelle, opt, aufbereitung)
        for nummer in sorted(plan):
            aufmodul.replace_pages([doc, original], nummer, plan[nummer], opt.ocr_dpi)
        seiten, ohne_text = _lese_roh(doc, opt, backend, plan)
    except Exception:
        doc.close()
        original.close()
        raise
    warnungen, hinweise = _lesehinweise(plan, ohne_text, seiten, backend)
    return Lesung(quelle=quelle, opt=opt, doc=doc, original=original, backend=backend,
                  seiten=seiten, ohne_text=ohne_text, plan=plan, warnungen=warnungen,
                  hinweise=hinweise)
