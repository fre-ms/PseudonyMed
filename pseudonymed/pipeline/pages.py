"""One page as it was read, and the text of pages with their hits replaced."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ocrdoc import filters as filters_module
from ocrdoc import ocr as ocr_module
from ocrdoc import structure as structure_module
from ocrdoc.filters import Filters
from ocrdoc.pagetext import Box, PageText
from ocrdoc.reading import MAX_PIXELS_UPSCALED

from pseudonymed.core.model import Hit
from pseudonymed.core.text import say
from pseudonymed.document import text_file as text_module

_TRAILING_PUNCTUATION = ".,;:!?)]}"
_LEADING_BRACKETS = "([{„\"'"


@dataclass
class PageEntry:
    """A page of the document: its text layer, its OCR reading and what was found.  The
    reading is ocrdoc's: every word OCR read on the page (whatever area it lies in, since
    whatever is printed there is to be searched), the filters ocrdoc read it through and
    the person's correction of its text."""

    number: int
    text: PageText
    ocr: PageText | None = None
    hits: list[Hit] = field(default_factory=list)
    term_rects: list[Box] = field(default_factory=list)
    term_spans: list[tuple[int, int]] = field(default_factory=list)
    term_count: int = 0
    correction: str | None = None  # the OCR text as the user corrected it
    correction_matches: list[Hit] = field(default_factory=list)
    ocr_dpi: int = ocr_module.OCR_DPI  # resolution the page was (and is re-)read at
    filters: Filters | None = None  # what ocrdoc's rendering of the page went through
    structure: structure_module.Structure | None = None  # layout roles of the OCR lines

    @property
    def is_image_page(self) -> bool:
        """True for a page whose only text is the OCR reading."""
        return self.ocr is not None and not self.text.has_text_layer

    @property
    def text_base(self) -> PageText:
        """The text the review clicks into: the OCR reading on image pages, else the
        text layer."""
        return self.ocr if self.ocr is not None and self.is_image_page else self.text

    def read_ocr(self, page: object, backend: ocr_module.OCRBackend) -> PageText:
        """Read the redacted page again for the re-check, as ocrdoc read it: at this page's
        resolution times its upscaling, through its filters, but for one: coloured ink is
        not taken away.  Where the OCR's filters took it away, the page passed on shows it
        taken away as well (:func:`pseudonymed.pipeline.reading.show_as_read`); whatever
        coloured ink is still on the page must be read, and found where it is legible."""
        chosen = replace(self.filters or Filters(), remove_colour=False)
        factor = max(1, chosen.upscale)
        run = filters_module.pipeline(chosen) if filters_module.available() else None
        return ocr_module.recognise_page(
            page, backend, self.number, self.ocr_dpi * factor,
            max_pixel=min(ocr_module.MAX_PIXELS * factor, MAX_PIXELS_UPSCALED), filter_=run)

    def matches_on(self, base: str) -> list[Hit]:
        """The hits that belong to ``base`` ("text" or "ocr"), manual boxes included
        where they were drawn on that basis; empty spans left out."""
        with_manual = (base == "text") == self.text.has_text_layer
        sources = (base, "manual") if with_manual else (base,)
        return [f for f in self.hits if f.source in sources and f.end > f.start]


def reading_text(s: PageEntry, structured: bool) -> str:
    """The OCR text as it is shown and corrected: the user's correction, else the raw
    reading, structured as Markdown when asked for."""
    if s.correction is not None:
        return s.correction
    if s.ocr is None:
        return ""
    if structured and s.structure is not None:
        return s.structure.render(s.ocr.text)
    return s.ocr.text


def unchanged(s: PageEntry, text: str) -> bool:
    """True when ``text`` is the OCR reading itself, raw or structured."""
    assert s.ocr is not None
    candidates = [s.ocr.text.strip()]
    if s.structure is not None:
        candidates.append(s.structure.render(s.ocr.text).strip())
    return text.strip() in candidates


def trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """``[start, ende)`` clipped to the text and stripped of surrounding whitespace."""
    start, end = max(0, start), min(end, len(text))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end <= start:
        raise ValueError(say("Leere Auswahl.", "Empty selection."))
    return start, end


def _char_under(base: PageText, x: float, y: float, tolerance: float) -> int | None:
    """Offset of the first character whose box, widened by ``tolerance``, holds (x, y)."""
    for i, box in enumerate(base.boxes):
        if box is None:
            continue
        if (box[0] - tolerance <= x <= box[2] + tolerance
                and box[1] - tolerance <= y <= box[3] + tolerance):
            return i
    return None


def _word_bounds(text: str, i: int) -> tuple[int, int]:
    """The run of non-whitespace around offset ``i``, trailing punctuation and
    leading brackets left out."""
    a, b = i, i + 1
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    while b < len(text) and not text[b].isspace():
        b += 1
    while b - 1 > a and text[b - 1] in _TRAILING_PUNCTUATION:
        b -= 1
    while a < b - 1 and text[a] in _LEADING_BRACKETS:
        a += 1
    return a, b


def word_at(base: PageText, x: float, y: float,
            tolerance: float = 2.0) -> tuple[int, int] | None:
    """Offsets of the word whose character box lies under (x, y), widened by ``tolerance``
    points; None without one.  A word runs between whitespace, trailing punctuation left
    out."""
    found = _char_under(base, x, y, tolerance)
    if found is None or base.text[found].isspace():
        return None
    return _word_bounds(base.text, found)


def overlap(a: Box, b: Box) -> bool:
    """Same line and overlapping: glyph boxes of neighbouring lines touch, so the boxes have
    to share at least half the lower height."""
    height = min(a[3] - a[1], b[3] - b[1])
    return (a[0] < b[2] and b[0] < a[2]
            and min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * height)


def markdown_from_text(text: str) -> str:
    """The reading-order text as Markdown: page markers become headings."""
    lines = ["# Pseudonymisierter Text", ""]
    for line in text.splitlines():
        if line.startswith("=== Seite ") and line.endswith(" ==="):
            lines.append("## " + line.strip("= ").strip())
        elif line.strip() == "--- Bildtext (OCR, ungeprüft) ---":
            lines.append("## Bildtext (OCR, ungeprüft)")
        else:
            lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _pseudonym_text(text: str, hits: list[Hit]) -> str:
    """``text`` with the hits replaced, blank runs collapsed."""
    return re.sub(r"\n{3,}", "\n\n", text_module.replace(text, hits)).strip()


def _ocr_text_pseudonym(s: PageEntry, structured: bool = True) -> str:
    """The OCR text of a page with pseudonyms: the user's correction when there is one,
    else the reading with the hits replaced, structured as Markdown when asked for
    (the structure is applied after the replacement: both texts share their lines)."""
    if s.correction is not None:
        return _pseudonym_text(s.correction, s.correction_matches)
    assert s.ocr is not None
    raw = text_module.replace(s.ocr.text, s.matches_on("ocr"))
    if structured and s.structure is not None:
        raw = s.structure.render(raw)
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _block(s: PageEntry, text: str) -> str:
    """One page's text under its page marker."""
    return f"=== Seite {s.number + 1} ===\n\n{text}"


def text_with_pseudonyms(pages: list[PageEntry], structured: bool = True) -> str:
    """Reading-order text with pseudonyms: text layer first, OCR text as its own block."""
    blocks = [_block(s, _pseudonym_text(s.text.text, s.matches_on("text"))) for s in pages]
    parts = "\n\n".join(blocks) + "\n"
    ocr_pages = [s for s in pages if s.ocr is not None]
    if ocr_pages:
        ocr_text = "\n\n".join(_block(s, _ocr_text_pseudonym(s, structured))
                                for s in ocr_pages) + "\n"
        parts = parts.rstrip("\n") + "\n\n--- Bildtext (OCR, ungeprüft) ---\n\n" + ocr_text
    return parts
