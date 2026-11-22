"""Phase one for PDFs and images: the document is read and the hits are known, and
the review can change them before :func:`pseudonymed.pipeline.finalise`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pymupdf
from optdoc import cleanup as cleanup_module
from optdoc import ocr as ocr_module
from optdoc.pagetext import Box

from pseudonymed.checks.recheck import tolerance_key
from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.core.text import say
from pseudonymed.detection.fuzzy import Target
from pseudonymed.detection.rules import Rule, box_to_hits
from pseudonymed.pipeline.detect import correction_matches, discard_remembered, recognise_pages
from pseudonymed.pipeline.options import PipelineOptions
from pseudonymed.pipeline.pages import (
    PageEntry,
    overlap,
    reading_text,
    trim_span,
    unchanged,
    word_at,
)
from pseudonymed.pipeline.reading import Reading, close_documents
from pseudonymed.pipeline.toolkit import Toolkit, analysis_notes, toolkit
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.store import PseudonymStore


@dataclass(kw_only=True)
class PreparationBase:
    """What the PDF and the text preparation share: the source, the patient, the options,
    the store, the identity and the rules, and the review's decisions (hits dropped and
    restored, the tolerated pairs, a version that grows with every change)."""

    is_text: ClassVar[bool]

    source: Path
    patient: Patient
    opt: PipelineOptions
    store: PseudonymStore
    own_store: bool
    code: str
    assignment: PseudonymAssignment
    rules: list[Rule]
    targets: list[Target]
    pages: list[PageEntry]
    without_text: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    discarded: list[Hit] = field(default_factory=list)
    cleaned_up: dict[int, cleanup_module.CleanupPlan] = field(default_factory=dict)
    finalised: bool = False
    version: int = 0  # grows with every change the user makes in the review
    restored: set[tuple[str, str]] = field(default_factory=set)

    @property
    def ocr_pages(self) -> list[int]:
        """Numbers (1-based) of the pages that were read by OCR."""
        return [s.number + 1 for s in self.pages if s.ocr is not None]

    @property
    def medical_term_count(self) -> int:
        """How many medical terms the vocabulary marked over all pages."""
        return sum(s.term_count for s in self.pages)

    def medical_term_rects(self, number: int) -> list[Box]:
        """Rectangles of the medical terms on page ``number`` (0-based), for display; not
        where a hit lies (a medication redacted on request, a patient called Herz)."""
        occupied = [r for f in self.pages[number].hits for r in f.rects]
        return [r for r in self.pages[number].term_rects
                if not any(overlap(r, b) for b in occupied)]

    def uncertain_rects(self, number: int) -> list[Box]:
        """Boxes of OCR words read with low confidence on page ``number``: uncertain,
        worth a look, not hits."""
        page = self.pages[number]
        if page.ocr is None:
            return []
        rects: list[Box] = []
        for (a, b), conf in zip(page.ocr.word_spans, page.ocr.confidences, strict=True):
            if conf < ocr_module.UNCERTAIN_CONFIDENCE:
                rects += [tuple(r) for r in page.ocr.rects(a, b)]
        return rects

    def discard(self, hit: Hit) -> None:
        """Drop a hit the user judged a false positive; the re-check will tolerate
        this rule/text pair.  The hit stays known (``discarded``) and can be restored."""
        for s in self.pages:
            if hit in s.hits:
                s.hits.remove(hit)
                self.discarded.append(hit)
                self.restored.discard(tolerance_key(hit.rule, hit.text))
                self.version += 1
                return

    def restore(self, hit: Hit) -> None:
        """Take a discarded hit back."""
        if hit in self.discarded:
            self.discarded.remove(hit)
            self.pages[hit.page].hits.append(hit)
            self.restored.add(tolerance_key(hit.rule, hit.text))
            self.version += 1

    @property
    def tolerated(self) -> set[tuple[str, str]]:
        """The rule/text pairs of the discarded hits, which the re-check lets pass."""
        return {tolerance_key(f.rule, f.text) for f in self.discarded if f.text}


@dataclass(kw_only=True)
class Preparation(PreparationBase):
    """Phase one: the document is read and the hits are known, nothing is changed yet.

    A user interface can drop hits (``discard``) and add boxes (``box``)
    before :func:`finalise` applies them.  Call ``close`` when the
    object is discarded without finishing.
    """

    is_text: ClassVar[bool] = False

    backend: ocr_module.OCRBackend | None
    doc: pymupdf.Document
    original: pymupdf.Document

    @property
    def hits(self) -> list[Hit]:
        """Every hit of every page, in page order."""
        return [f for s in self.pages for f in s.hits]

    def medical_term_spans(self, number: int) -> list[tuple[int, int]]:
        """Text offsets of the medical terms on page ``number`` (0-based)."""
        return list(self.pages[number].term_spans)

    def page_image(self, number: int, dpi: int = 100) -> bytes:
        """PNG of the original page for the preview."""
        return self.original[number].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def ocr_text(self, number: int) -> str:
        """The recognised text of page ``number`` as it will be written out: the user's
        correction when there is one, else the OCR reading (structured as Markdown when
        asked for); "" without OCR."""
        return reading_text(self.pages[number], self.opt.structured)

    def set_ocr_text(self, number: int, text: str) -> list[Hit]:
        """Replace the OCR text of page ``number`` by the user's correction.

        The correction feeds the text file, the Markdown and the invisible text layer;
        the redaction in the image keeps the OCR boxes (mask what the OCR misread).
        The rules and the fuzzy search run over the corrected text; their hits are
        returned and shown with source ``correction``.
        """
        s = self.pages[number]
        if s.ocr is None:
            raise ValueError(say("Diese Seite hat keinen OCR-Text.", "This page has no OCR text."))
        if unchanged(s, text):
            s.correction, s.correction_matches = None, []
            return []
        s.correction = text
        r = Toolkit.rebuilt(self.code, self.assignment, self.rules, self.targets, self.opt)
        s.correction_matches = correction_matches(s, r, self.opt)
        self.version += 1
        return s.correction_matches

    @property
    def correction_matches(self) -> list[Hit]:
        """The text-only hits over the corrected OCR texts of all pages."""
        return [f for s in self.pages for f in s.correction_matches]

    def box(self, number: int, rect: Box, pseudonym: str = "[geschwärzt]") -> list[Hit]:
        """Add a manual box on page ``number`` (0-based); returns the hits it became."""
        page = self.pages[number]
        added = box_to_hits(page.text_base, rect, pseudonym)
        for f in added:
            f.source = "manuell"
        page.hits += added
        self.version += 1
        return added

    def add_mask(self, number: int, rect: Box) -> list[Hit]:
        """Black out a region of page ``number``: text and pixels under it vanish, nothing
        is written into it, and hits lying wholly inside it are dropped as redundant.
        The text output shows ``[geschwärzt]`` in its place."""
        page = self.pages[number]
        inside = pymupdf.Rect(rect)
        page.hits = [
            f for f in page.hits
            if f.rule == "maske" or not f.rects
            or not all(inside.contains(pymupdf.Rect(r)) for r in f.rects)
        ]
        added = self.box(number, rect, "[geschwärzt]")
        for f in added:
            f.rule = "maske"
        return added

    def word_at(self, number: int, x: float, y: float) -> tuple[int, int] | None:
        """Offsets of the word under the point (page points) on page ``number``, or None
        where no printed character lies within two points."""
        return word_at(self.pages[number].text_base, x, y)

    def take_up(self, number: int, start: int, end: int,
                pseudonym: str | None = None) -> Hit:
        """Take a span of recognised text into the hit list by hand (a word the analysis
        left alone, a term from the protected vocabulary); hits it overlaps are replaced.
        Without ``pseudonym`` the text gets a code of its own, "[Begriff <code>]", so the
        re-identification can put it back."""
        page = self.pages[number]
        base = page.text_base
        start, end = trim_span(base.text, start, end)
        text = base.text[start:end]
        source = "ocr" if base is page.ocr else "text"
        page.hits = [f for f in page.hits
                     if f.source not in (source, "manuell")
                     or not (start < f.end and end > f.start)]
        f = Hit(page=number, start=start, end=end, text=text, kind=Kind.TERM,
                rule="aufnahme",
                       pseudonym=(pseudonym
                                  or self.assignment.identifier("Begriff", text, Kind.TERM)),
                rects=[tuple(r) for r in base.rects(start, end)],
                font_size=base.font_size(start, end), source="manuell")
        page.hits.append(f)
        self.version += 1
        return f

    def close(self) -> None:
        """Release the documents and, when it is ours, the pseudonym store."""
        close_documents(self.doc, self.original)
        if self.own_store:
            self.store.close()


def _page_notes(r: Toolkit, pages: list[PageEntry], spared: list[str]) -> list[str]:
    """The hints of a detection over pages: NER, identifiers, medical content, and the
    unreadable OCR lines that were redacted as a precaution."""
    all_items = [f for s in pages for f in s.hits]
    notes = analysis_notes(r, all_items, spared)
    unreadable = sum(1 for f in all_items if f.rule == "ocr.unlesbar")
    if unreadable:
        notes.append(say(f"{unreadable} unlesbare OCR-Zeile(n) vorsorglich geschwärzt.",
                         f"{unreadable} unreadable OCR line(s) redacted as a precaution."))
    return notes


def analyse(
    reading: Reading,
    patient: Patient,
    opt: PipelineOptions | None = None,
    store: PseudonymStore | None = None,
) -> Preparation:
    """Detection phase over a :class:`Reading`: rules, vocabulary, fuzzy search, NER,
    codes, unreadable lines, the corrected OCR text.  Takes over the documents of the
    reading (which must not be used afterwards)."""
    if reading.used_up:
        raise RuntimeError(say("Diese Lesung wurde bereits analysiert.",
                               "This reading has been analysed already."))
    opt = opt or reading.opt
    own_store = store is None
    store = store or PseudonymStore()
    spared: list[str] = []
    try:
        r = toolkit(patient, opt, store)
        recognise_pages(reading.doc, reading.pages, r, opt, spared)
    except Exception:
        if own_store:
            store.close()
        raise
    reading.used_up = True
    notes = list(reading.notes) + _page_notes(r, reading.pages, spared)
    discarded = discard_remembered(reading.pages, store.discarded_for(patient), notes)
    return Preparation(
        source=reading.source, patient=patient, opt=opt, store=store,
        own_store=own_store, code=r.code, assignment=r.assignment,
        rules=r.rules, targets=r.targets, backend=reading.backend, doc=reading.doc,
        original=reading.original, pages=reading.pages, without_text=reading.without_text,
        warnings=list(r.warnings) + list(reading.warnings), notes=notes,
        cleaned_up=reading.plan, discarded=discarded,
    )
