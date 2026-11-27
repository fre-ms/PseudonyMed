"""The reading phase: docrepro binds, prepares and reads the document, PseudonyMed takes
its pages as they were read.  No patient, no detection yet.

docrepro's steps do the work (:mod:`pseudonymed.document.chain`): typdoc binds a photo or
an image into a PDF, optdoc prepares the pages without a text layer of their own, ocrdoc
reads the text layer and runs OCR where a page needs it.  The reading keeps the prepared
document (the PDF ocrdoc read, which PseudonyMed redacts) and the chain document that
makes it again from the original, byte for byte, with every page's text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pymupdf
from ocrdoc import ocr as ocr_module
from ocrdoc import structure as structure_module
from ocrdoc.pagetext import read_text_layer
from optdoc import cleanup as cleanup_module

from pseudonymed.core.text import say
from pseudonymed.document import chain as chain_module
from pseudonymed.document import docrepro_adapter
from pseudonymed.document.chain import DocumentChain
from pseudonymed.pipeline.options import PipelineOptions
from pseudonymed.pipeline.pages import PageEntry, reading_text, unchanged

Progress = Callable[[int, int], None]  # page (from 1) about to be read, and how many


def close_documents(*docs: pymupdf.Document) -> None:
    """Close every document, ignoring the ones that are closed already."""
    for d in docs:
        try:
            d.close()
        except Exception:  # already closed
            pass


def chain_settings(opt: PipelineOptions) -> dict[str, Any]:
    """The settings of docrepro's steps for a fresh chain under these options."""
    return docrepro_adapter.chain_settings(opt.ocr, opt.ocr_languages, opt.ocr_tables,
                                           opt.clean_up != "aus", opt.ocr_dpi)


@dataclass
class Reading:
    """The reading phase: the pages of a PDF or image are read (text layer, OCR after
    the preparation) but not yet searched.  ``doc`` is the prepared document as ocrdoc read
    it, ``original`` a second copy of it for pages that have to be rasterised; ``chain``
    the chain document that made it, ``original_data`` and ``original_suffix`` the
    original it was made from, so that both make the same reading again."""

    is_text: ClassVar[bool] = False

    source: Path
    opt: PipelineOptions
    doc: pymupdf.Document
    original: pymupdf.Document
    backend: ocr_module.OCRBackend | None
    pages: list[PageEntry]
    without_text: list[int]
    plan: dict[int, cleanup_module.CleanupPlan] = field(default_factory=dict)
    chain: dict[str, Any] = field(default_factory=dict)
    original_data: bytes = b""
    original_suffix: str = ".pdf"
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
        """PNG of the page as the OCR saw it (prepared)."""
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
        """Keep the user's correction of the OCR text, in the page and in ocrdoc's part of
        the chain document; the text as read (raw or structured) resets it."""
        s = self.pages[number]
        if s.ocr is None:
            raise ValueError(say("Diese Seite hat keinen OCR-Text.", "This page has no OCR text."))
        s.correction = None if unchanged(s, text) else text
        self.chain = chain_module.with_correction(self.chain, number, s.correction)

    def close(self) -> None:
        """Release both documents."""
        close_documents(self.doc, self.original)


def _reading_notes(
    plan: dict[int, cleanup_module.CleanupPlan], without_text: list[int], pages: list[PageEntry],
    backend_name: str | None,
) -> tuple[list[str], list[str]]:
    """What the reading has to tell: the preparations applied, pages nobody could read,
    the pages OCR read and how sure it was.  Returns warnings and hints."""
    notes: list[str] = [say(f"Seite {number + 1} vor der OCR aufbereitet:"
                            f" {docrepro_adapter.description(plan[number])}.",
                            f"Page {number + 1} cleaned up before the OCR:"
                            f" {docrepro_adapter.description(plan[number], 'en')}.")
                        for number in sorted(plan)]
    warnings: list[str] = []
    if without_text:
        pages_ = ", ".join(map(str, without_text))
        warnings.append(say(
            f"Seite(n) ohne Textebene und ohne OCR: {pages_} – OCR-Programm installieren"
            " (RapidOCR oder Tesseract) oder --ocr prüfen.",
            f"Page(s) without text layer and without OCR: {pages_} – install an OCR engine"
            " (RapidOCR or Tesseract) or check --ocr."))
    read_in = [(s.number + 1, s.ocr) for s in pages if s.ocr is not None]
    if read_in:
        uncertain = sum(ocr.uncertain_words for _, ocr in read_in)
        numbers = ", ".join(str(n) for n, _ in read_in)
        name = backend_name or "?"
        notes.append(say(
            f"Scan/Foto: Seite(n) {numbers} per OCR gelesen ({name}, {uncertain} unsichere"
            " Wörter).",
            f"Scan/photo: page(s) {numbers} read by OCR ({name}, {uncertain} uncertain words)."))
        notes.append(say("OCR-Seiten: Handschrift wird nicht erkannt; erkannten Text und"
                         " Vorschaubilder vor der Weitergabe prüfen.",
                         "OCR pages: handwriting is not recognised; check the recognised text"
                         " and the previews before passing anything on."))
    return warnings, notes


def _page_entry(project: Any, doc: pymupdf.Document, n: int) -> PageEntry:
    """Page ``n`` as ocrdoc read it: the text layer of the prepared page, and where OCR
    read it every word it read (whatever area it lies in), the filters it read through,
    the structure of its lines and the person's correction."""
    entry = PageEntry(n, read_text_layer(doc[n], n))
    entry.ocr_dpi = project.options.dpi
    entry.filters = project.filters(n)
    if project.pages[n].ocr:
        entry.ocr = project.ocr_text(n, every_word=True)
        entry.structure = structure_module.analyse_page_structure(doc[n], entry.ocr)
        entry.correction = project.pages[n].correction
    return entry


def _plans(chain: DocumentChain) -> dict[int, cleanup_module.CleanupPlan]:
    """The preparation optdoc applied, by page (from 0): the pages a plan changed."""
    project = chain.project("optdoc")
    if project is None:
        return {}
    found: dict[int, cleanup_module.CleanupPlan] = {}
    for n in project.cleaned_up:
        plan = project.active_plan(n)
        if plan is not None:
            found[n] = plan
    return found


def read_chain(chain: DocumentChain, opt: PipelineOptions, source: Path | None = None,
               progress: Progress | None = None) -> Reading:
    """The reading of a document from its chain, which stays open (the caller's): every
    page ocrdoc has not read yet is read, then the prepared document and the pages are
    taken as ocrdoc has them.  ``source`` names the outputs (default: the original)."""
    project = chain.ensure("ocrdoc")
    project.read_all(progress)
    data = project.doc.tobytes(no_new_id=True)  # the prepared document as ocrdoc read it
    doc, original = pymupdf.open("pdf", data), pymupdf.open("pdf", data)
    try:
        pages = [_page_entry(project, doc, n) for n in range(len(doc))]
        without_text = [n + 1 for n in range(len(doc))
                        if project.needs_ocr(n) and pages[n].ocr is None]
        backend = project.engine() if any(s.ocr is not None for s in pages) else None
        document = chain.document()
        plan = _plans(chain)
    except Exception:
        close_documents(doc, original)
        raise
    warnings, notes = _reading_notes(plan, without_text, pages, project.reader_name())
    return Reading(source=source or chain.chain.original, opt=opt, doc=doc, original=original,
                   backend=backend, pages=pages, without_text=without_text, plan=plan,
                   chain=document, original_data=chain.chain.original.read_bytes(),
                   original_suffix=chain.chain.original.suffix.lower(), warnings=warnings,
                   notes=notes)


def read(path: str | Path, opt: PipelineOptions | None = None,
         chain: dict[str, Any] | None = None) -> Reading:
    """Reading phase for a PDF or image: docrepro's chain on it, fresh with the options,
    or as the chain document ``chain`` says (then the person's preparation, the words OCR
    read and the corrections are taken, and nothing is read again).  No patient needed."""
    opt = opt or PipelineOptions()
    source = Path(path)
    documents = DocumentChain.open(source, chain_settings(opt), chain)
    try:
        return read_chain(documents, opt, source)
    finally:
        documents.close()
