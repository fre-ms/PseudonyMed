"""Phase zero and the reading phase: open the file, clean up image pages, read the text
layer and run OCR where a page needs it.  No patient, no detection yet."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pymupdf
from optdoc import cleanup as cleanup_module
from optdoc import ocr as ocr_module
from optdoc import structure as structure_module
from optdoc.loading import HEIC_SUFFIXES, IMAGE_SUFFIXES
from optdoc.pagetext import PageText, read_text_layer

from pseudonymed.document import optdoc_adapter
from pseudonymed.document.optdoc_adapter import open_file
from pseudonymed.pipeline.options import PipelineOptions
from pseudonymed.pipeline.pages import PageEntry, reading_text, unchanged

_SUGGESTION_DPI = 150  # resolution the clean-up suggestion looks at a page with
_MAX_PIXELS_UPSCALED = 9000  # cap for the OCR image of an upscaled page


def close_documents(*docs: pymupdf.Document) -> None:
    """Close every document, ignoring the ones that are closed already."""
    for d in docs:
        try:
            d.close()
        except Exception:  # already closed
            pass


def _is_photo(source: Path) -> bool:
    """True for an image file (photo, scan as JPEG, PNG, HEIC) rather than a PDF."""
    return source.suffix.lower() in IMAGE_SUFFIXES | HEIC_SUFFIXES


def _suggestion(page: pymupdf.Page, photo: bool) -> cleanup_module.CleanupPlan:
    """The automatic clean-up suggestion for one image-only page."""
    image, scale_factor = cleanup_module.render(page, _SUGGESTION_DPI)
    return cleanup_module.suggest(image, photo, scale_factor, cleanup_module.native_dpi(page))


@dataclass
class OpenedDocument:
    """Phase zero: the file is open and its pages are known, nothing is read in detail.

    Pages without a text layer (scans, photos) are candidates for the clean-up step;
    each comes with an automatic suggestion.  Call ``close`` when done.
    """

    source: Path
    doc: pymupdf.Document
    photo: bool
    candidates: list[int]
    suggestions: dict[int, cleanup_module.CleanupPlan]

    @property
    def page_count(self) -> int:
        """Number of pages of the open document."""
        return len(self.doc)

    def page_image(self, number: int, dpi: int = 100) -> bytes:
        """PNG of page ``number`` (0-based) as it is."""
        return self.doc[number].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def preview(self, number: int, a: cleanup_module.CleanupPlan,
                dpi: int = 100) -> tuple[bytes, float]:
        """PNG of the processed page and its pixels per point."""
        return cleanup_module.preview_png(self.doc[number], a, dpi)

    def page_width(self, number: int, a: cleanup_module.CleanupPlan) -> float:
        """Width in points of page ``number`` after the rotation ``a`` asks for."""
        r = self.doc[number].rect
        return r.width if a.rotation in (0, 180) else r.height

    def close(self) -> None:
        """Release the document."""
        close_documents(self.doc)


def load(path: str | Path, opt: PipelineOptions | None = None) -> OpenedDocument:
    """Open a PDF or image and find the pages worth cleaning up before OCR."""
    opt = opt or PipelineOptions()
    source = Path(path)
    doc = open_file(source, opt.paper_size)
    photo = _is_photo(source)
    candidates = [i for i, page in enumerate(doc) if not read_text_layer(page, i).has_text_layer]
    suggestions: dict[int, cleanup_module.CleanupPlan] = {}
    if cleanup_module.available() and opt.clean_up != "aus":
        suggestions = {i: _suggestion(doc[i], photo) for i in candidates}
    return OpenedDocument(source, doc, photo, candidates, suggestions)


def _suggested(doc: pymupdf.Document, photo: bool, plan: dict[int, cleanup_module.CleanupPlan],
               ) -> dict[int, cleanup_module.CleanupPlan]:
    """Suggestions for the image-only pages the plan does not cover yet, active ones only."""
    new: dict[int, cleanup_module.CleanupPlan] = {}
    for i, page in enumerate(doc):
        if i in plan or read_text_layer(page, i).has_text_layer:
            continue
        a = _suggestion(page, photo)
        if a.active:
            new[i] = a
    return new


def _cleanup_plan(
    doc: pymupdf.Document, source: Path, opt: PipelineOptions,
    explicit: dict[int, cleanup_module.CleanupPlan] | None,
) -> dict[int, cleanup_module.CleanupPlan]:
    """Which pages get which clean-up: the explicit choice, else the suggestion for
    every image-only page when ``clean_up`` is "auto"."""
    plan = {n: a for n, a in (explicit or opt.cleanup or {}).items() if a.active}
    automatic = (opt.clean_up == "auto" and cleanup_module.available()
                 and explicit is None and opt.cleanup is None)
    if automatic:
        plan.update(_suggested(doc, _is_photo(source), plan))
    return plan


def _page_with_plan(number: int, text_layer: PageText, opt: PipelineOptions,
                    plan: cleanup_module.CleanupPlan | None) -> PageEntry:
    """A page entry with the OCR resolution and finish its clean-up plan asks for: an
    upscaled page is read at that multiple of the OCR resolution (capped), a page in
    letter mode gets the letter-aware finish before the engine."""
    entry = PageEntry(number, text_layer)
    factor = max(1, plan.upscale) if plan is not None else 1
    entry.ocr_dpi = opt.ocr_dpi * factor
    entry.ocr_max_pixels = min(ocr_module.MAX_PIXELS * factor, _MAX_PIXELS_UPSCALED)
    if plan is not None and plan.letter_mode:
        entry.ocr_filter = functools.partial(cleanup_module.letter_finish, factor=factor)
    return entry


def _read_raw(
    doc: pymupdf.Document, opt: PipelineOptions, backend: ocr_module.OCRBackend | None,
    plans: dict[int, cleanup_module.CleanupPlan] | None = None,
) -> tuple[list[PageEntry], list[int]]:
    """Read every page: text layer and, where needed and possible, OCR.  No detection
    yet.  Returns the pages and the numbers (1-based) of pages that needed OCR but got
    none."""
    pages: list[PageEntry] = []
    without_text: list[int] = []
    for i, page in enumerate(doc):
        entry = _page_with_plan(i, read_text_layer(page, i), opt, (plans or {}).get(i))
        if ocr_module.needs_ocr(page, entry.text):
            if backend is None:
                without_text.append(i + 1)
            else:
                entry.ocr = entry.read_ocr(page, backend)
                entry.structure = structure_module.analyse_page_structure(page, entry.ocr)
        pages.append(entry)
    return pages, without_text


@dataclass
class Reading:
    """The reading phase: the pages of a PDF or image are read (text layer, OCR after
    the clean-up) but not yet searched.  The desktop lets the user check and correct
    the recognised text here before :func:`analyse` looks for identifiers."""

    is_text: ClassVar[bool] = False

    source: Path
    opt: PipelineOptions
    doc: pymupdf.Document
    original: pymupdf.Document
    backend: ocr_module.OCRBackend | None
    pages: list[PageEntry]
    without_text: list[int]
    plan: dict[int, cleanup_module.CleanupPlan] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    used_up: bool = False

    @property
    def page_count(self) -> int:
        """Number of pages read."""
        return len(self.pages)

    @property
    def ocr_pages(self) -> list[int]:
        """Numbers (1-based) of the pages that were read by OCR."""
        return [s.number + 1 for s in self.pages if s.ocr is not None]

    def has_ocr(self, number: int) -> bool:
        """True when page ``number`` (0-based) was read by OCR."""
        return self.pages[number].ocr is not None

    def page_image(self, number: int, dpi: int = 100) -> bytes:
        """PNG of the page as the OCR saw it (cleaned up)."""
        return self.doc[number].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def text(self, number: int) -> str:
        """Text of the page: the (corrected, structured) OCR text for OCR pages, else
        the text layer."""
        s = self.pages[number]
        return reading_text(s, self.opt.structured) if s.ocr is not None else s.text.text

    def ocr_text(self, number: int) -> str:
        """The OCR text of page ``number`` as shown: corrected, else as read."""
        return reading_text(self.pages[number], self.opt.structured)

    def set_ocr_text(self, number: int, text: str) -> None:
        """Keep the user's correction of the OCR text; the text as read (raw or
        structured) resets it."""
        s = self.pages[number]
        if s.ocr is None:
            raise ValueError("Diese Seite hat keinen OCR-Text.")
        s.correction = None if unchanged(s, text) else text

    def close(self) -> None:
        """Release both documents."""
        close_documents(self.doc, self.original)


def _reading_notes(
    plan: dict[int, cleanup_module.CleanupPlan], without_text: list[int], pages: list[PageEntry],
    backend: ocr_module.OCRBackend | None,
) -> tuple[list[str], list[str]]:
    """What the reading has to tell: the clean-ups applied, pages nobody could read,
    the pages OCR read and how sure it was.  Returns warnings and hints."""
    notes = [f"Seite {number + 1} vor der OCR aufbereitet:"
             f" {optdoc_adapter.description(plan[number])}." for number in sorted(plan)]
    warnings: list[str] = []
    if without_text:
        warnings.append(
            "Seite(n) ohne Textebene und ohne OCR: " + ", ".join(map(str, without_text))
            + " – OCR-Backend installieren (Extra 'ocr' oder Tesseract) oder --ocr prüfen."
        )
    read_in = [(s.number + 1, s.ocr) for s in pages if s.ocr is not None]
    if read_in:
        uncertain = sum(ocr.uncertain_words for _, ocr in read_in)
        notes.append(
            f"Scan/Foto: Seite(n) {', '.join(str(n) for n, _ in read_in)} per OCR gelesen"
            f" ({backend.name if backend else '?'}, {uncertain} unsichere Wörter)."
        )
        notes.append("OCR-Seiten: Handschrift wird nicht erkannt; erkannten Text und"
                     " Vorschaubilder vor der Weitergabe prüfen.")
    return warnings, notes


def read(
    path: str | Path,
    opt: PipelineOptions | None = None,
    cleanup: dict[int, cleanup_module.CleanupPlan] | None = None,
) -> Reading:
    """Reading phase for a PDF or image: clean-up, text layer, OCR.  No patient needed."""
    opt = opt or PipelineOptions()
    source = Path(path)
    backend = optdoc_adapter.backend(opt.ocr, opt.ocr_languages, tables=opt.ocr_tables)
    doc = open_file(source, opt.paper_size)
    original = open_file(source, opt.paper_size)
    try:
        plan = _cleanup_plan(doc, source, opt, cleanup)
        for number in sorted(plan):
            cleanup_module.replace_pages([doc, original], number, plan[number], opt.ocr_dpi)
        pages, without_text = _read_raw(doc, opt, backend, plan)
    except Exception:
        doc.close()
        original.close()
        raise
    warnings, notes = _reading_notes(plan, without_text, pages, backend)
    return Reading(source=source, opt=opt, doc=doc, original=original, backend=backend,
                   pages=pages, without_text=without_text, plan=plan, warnings=warnings,
                   notes=notes)
