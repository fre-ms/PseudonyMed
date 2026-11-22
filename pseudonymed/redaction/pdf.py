"""Redact hits in a PDF page and write the pseudonym into the gap.

MuPDF removes every character whose glyph box is *touched* by a redaction
rectangle, and glyph boxes of neighbouring lines overlap even at ordinary
line spacing.  The removal band is therefore clipped against the boxes of all
other lines before it is applied.  The pseudonym is then drawn by us on the
original baseline, widened into free space to the right where possible and
shrunk otherwise, so that it stays legible.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NamedTuple

import pymupdf
from optdoc.pagetext import Box, PageText, read_text_layer

from pseudonymed import TOOL
from pseudonymed.core.model import Hit

ALLOWED_METADATA = {"producer": TOOL, "creator": TOOL}
NOTICE_PREFIX = "pseudonymisiert mit PseudonyMed"

Colour = tuple[float, float, float]


@dataclass
class PageResult:
    """What happened to one page: number of boxes and whether the remaining
    text kept its position (MuPDF repositions glyphs after a removal; a wrong
    offset would hide or overlap neighbouring characters)."""

    count: int = 0
    layout_intact: bool = True


BLACK = (0.0, 0.0, 0.0)


def is_mask(f: Hit) -> bool:
    """A region the user blacked out: no pseudonym is written into it."""
    return f.rule == "maske"


@dataclass
class RedactionOptions:
    """How hits are removed and painted: fill and text colour of the pseudonym box,
    the base font and its smallest size, MuPDF's treatment of images and line art
    under a box, the metadata note, optional rasterising with its resolution, and
    whether scans get an invisible text layer."""

    STORED_KEYS: ClassVar[dict[str, str]] = {
        "fill_colour": "fuellfarbe", "text_colour": "textfarbe", "font": "schrift",
        "min_font_size": "min_schrift", "images": "bilder", "graphics": "grafik",
        "notice": "vermerk", "rasterise": "rastern", "text_layer": "textebene"}

    fill_colour: tuple[float, float, float] = (0.88, 0.88, 0.88)
    text_colour: tuple[float, float, float] = (0.0, 0.0, 0.0)
    font: str = "helv"
    min_font_size: float = 4.0
    images: int = pymupdf.PDF_REDACT_IMAGE_PIXELS
    graphics: int = pymupdf.PDF_REDACT_LINE_ART_NONE
    notice: bool = True
    rasterise: bool = False
    dpi: int = 200
    text_layer: bool = True  # embed the OCR text invisibly so that scans stay searchable


def _band(rect: pymupdf.Rect, line: int, page: PageText) -> pymupdf.Rect:
    """Shrink ``rect`` so that it touches no character of another line."""
    band = pymupdf.Rect(rect)
    middle = (band.y0 + band.y1) / 2
    for z, box in page.line_boxes.items():
        if z == line:
            continue
        other = pymupdf.Rect(box)
        if not other.intersects(band) or other.x1 <= band.x0 or other.x0 >= band.x1:
            continue
        other_middle = (other.y0 + other.y1) / 2
        if other_middle < middle:
            band.y0 = max(band.y0, other.y1 + 0.2)
        else:
            band.y1 = min(band.y1, other.y0 - 0.2)
    if band.y1 - band.y0 < 1.0:  # pathological overlap: keep the middle sliver
        band.y0, band.y1 = middle - 0.5, middle + 0.5
    band.x0 += 0.2
    band.x1 -= 0.2
    return band


def _line_of_rect(page: PageText, f: Hit, rect: pymupdf.Rect) -> int:
    """Line of the first character of ``f`` whose box touches ``rect``, or -1."""
    for i in range(f.start, min(f.end, len(page.text))):
        box = page.boxes[i]
        if box is not None and pymupdf.Rect(box).intersects(rect):
            return page.lines[i]
    return -1


def _fitting_size(text: str, width: float, start: float, opt: RedactionOptions) -> float:
    """The largest font size at or below ``start`` at which ``text`` fits into ``width``,
    never below the configured minimum."""
    size = max(start, opt.min_font_size)
    while size > opt.min_font_size and pymupdf.get_text_length(
        text, fontname=opt.font, fontsize=size
    ) > width:
        size -= 0.5
    return size


class _Overlay(NamedTuple):
    """A pseudonym to draw: the box it fills, its baseline, font size and text."""

    fill: pymupdf.Rect
    base: float
    size: float
    pseudonym: str


def _char_positions(page: PageText) -> list[tuple[int, str, Box]]:
    """Offset, character and box of every printed, non-blank character of ``page``."""
    return [
        (i, c, box) for i, c in enumerate(page.text)
        if (box := page.boxes[i]) is not None and not c.isspace()
    ]


def _by_position(chars: Iterable[tuple[str, Box]]) -> list[tuple[str, Box]]:
    """Characters in reading order: by rounded top edge, then by left edge."""
    return sorted(chars, key=lambda e: (round(e[1][1]), e[1][0]))


def _under_box(box: Box, boxes: list[pymupdf.Rect]) -> bool:
    """True when one of the applied boxes touches the character box."""
    return any(k.intersects(pymupdf.Rect(box)) for k in boxes)


def _moved(a: tuple[str, Box], b: tuple[str, Box], tolerance: float) -> bool:
    """True when the character differs or moved by more than ``tolerance`` points."""
    (c1, b1), (c2, b2) = a, b
    return c1 != c2 or abs(b1[0] - b2[0]) > tolerance or abs(b1[1] - b2[1]) > tolerance


def layout_intact(
    before: PageText, after: PageText, hits: list[Hit], tolerance: float = 0.6,
    boxes: list[pymupdf.Rect] | None = None,
) -> bool:
    """True when every character outside the hits (and outside the applied boxes
    ``boxes``) is still where it was."""
    removed = {i for f in hits for i in range(f.start, f.end)}
    boxes = list(boxes or [])
    expected = _by_position(
        (c, box) for i, c, box in _char_positions(before)
        if i not in removed and not _under_box(box, boxes)
    )
    actual = _by_position((c, box) for _i, c, box in _char_positions(after))
    if len(expected) != len(actual):
        return False
    return not any(_moved(e, i, tolerance) for e, i in zip(expected, actual, strict=True))


def _write_overlay(pdf_page: pymupdf.Page, e: _Overlay, opt: RedactionOptions) -> None:
    """Write the pseudonym on its baseline, a little inside the left edge of its box."""
    pdf_page.insert_text(
        (e.fill.x0 + 0.75, e.base),
        e.pseudonym,
        fontsize=e.size,
        fontname=opt.font,
        color=opt.text_colour,
        overlay=True,
    )


def _label(
    page: PageText, f: Hit, rect: pymupdf.Rect, opt: RedactionOptions
) -> _Overlay:
    """The pseudonym of ``f`` fitted into its first box: on the original baseline for a
    text-layer hit, otherwise near the bottom of the box."""
    base = page.baseline(f.start, f.end) if f.source == "text" else None
    if base is None:
        base = rect.y1 - 0.22 * rect.height
    size = _fitting_size(f.pseudonym, rect.width - 1.5, f.font_size, opt)
    return _Overlay(rect, base, size, f.pseudonym)


def _paint_over_matches(
    pdf_page: pymupdf.Page, page: PageText, hits: list[Hit], opt: RedactionOptions
) -> None:
    """Paint every box of every hit (black for masks), then write the pseudonyms, so
    that a neighbouring box never covers a text."""
    texts: list[_Overlay] = []
    for f in hits:
        colour = BLACK if is_mask(f) else opt.fill_colour
        for k, raw in enumerate(f.rects):
            rect = pymupdf.Rect(raw)
            pdf_page.draw_rect(rect, color=None, fill=colour, overlay=True)
            if k == 0 and not is_mask(f):
                texts.append(_label(page, f, rect, opt))
    for e in texts:
        _write_overlay(pdf_page, e, opt)


def rasterise_page(
    doc: pymupdf.Document,
    number: int,
    original: pymupdf.Document,
    page: PageText,
    hits: list[Hit],
    opt: RedactionOptions,
) -> None:
    """Replace page ``number`` of ``doc`` by an image of the *original* page with
    the hits painted over and the pseudonyms written on top.  Used when the
    text-layer redaction disturbed the layout; nothing of the text layer survives."""
    temp = pymupdf.open()
    temp.insert_pdf(original, from_page=number, to_page=number)
    tp = temp[0]
    _paint_over_matches(tp, page, hits, opt)
    pix = tp.get_pixmap(dpi=opt.dpi, alpha=False)
    width, height = tp.rect.width, tp.rect.height
    temp.close()
    new = doc.new_page(pno=number, width=width, height=height)
    new.insert_image(new.rect, pixmap=pix)
    doc.delete_page(number + 1)


OCR_MARGIN = 1.5  # points of padding around OCR word boxes before pixels are blanked
SNAP_DPI = 200


class _Ink:
    """Grey rendering of a page for snapping OCR boxes to the gaps between words.

    OCR engines report line or word boxes; the position of a hit inside them is
    an estimate, so the box is widened outward to the nearest gap that is wider
    than the gap between two letters but not wider than a word space (ink-free
    run of at least 20 % of the box height), at most one box height to each side.
    Over-covering a neighbour is accepted; leaving a character of a name or a
    date uncovered is not.
    """

    def __init__(self, pdf_page: pymupdf.Page, dpi: int = SNAP_DPI) -> None:
        self.scale_factor = dpi / 72.0
        pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(self.scale_factor, self.scale_factor),
                                  alpha=False,
                                  colorspace=pymupdf.csGRAY)
        self.width, self.height = pix.width, pix.height
        self.pixels = pix.samples  # one byte per pixel, row-major

    def _column_empty(self, x: int, y0: int, y1: int) -> bool:
        if x < 0 or x >= self.width:
            return True
        allowed = max(1, (min(self.height, y1) - max(0, y0)) // 12)  # noise specks
        dark = 0
        for y in range(max(0, y0), min(self.height, y1)):
            if self.pixels[y * self.width + x] < 128:
                dark += 1
                if dark > allowed:
                    return False
        return True

    def widen(self, rect: pymupdf.Rect) -> pymupdf.Rect:
        """``rect`` widened to the nearest word gap on each side, same height."""
        h = rect.height
        if h <= 0:
            return rect
        y0, y1 = int(rect.y0 * self.scale_factor), int(rect.y1 * self.scale_factor) + 1
        gap = max(3, int(0.2 * h * self.scale_factor))
        boundary = int(1.0 * h * self.scale_factor)
        x0 = int(rect.x0 * self.scale_factor)
        x1 = int(rect.x1 * self.scale_factor)
        new_x0 = self._search(x0, -1, y0, y1, gap, boundary)
        new_x1 = self._search(x1, +1, y0, y1, gap, boundary)
        return pymupdf.Rect(new_x0 / self.scale_factor, rect.y0, new_x1 / self.scale_factor,
                            rect.y1)

    def _search(self, start: int, direction: int, y0: int, y1: int, gap: int, boundary: int) -> int:
        """Walk from ``start`` in ``direction`` until a run of ``gap`` empty columns."""
        x = start
        empty_run = 0
        steps = 0
        while steps < boundary:
            x += direction
            steps += 1
            if self._column_empty(x, y0, y1):
                empty_run += 1
                if empty_run >= gap:
                    return x - direction * (gap - 1)
            else:
                empty_run = 0
        return start + direction * boundary


class _RemovalBand(NamedTuple):
    """How one rectangle of a hit is removed: ``area`` is the (possibly padded) box
    that is painted over, ``band`` the rectangle handed to MuPDF for the removal.
    ``on_image`` marks a box that blanks pixels rather than a known span of text-layer
    characters: it exempts the characters under it from the layout check and has no
    baseline or free space of its own."""

    area: pymupdf.Rect
    band: pymupdf.Rect
    on_image: bool


@dataclass
class _RedactionPlan:
    """Everything decided for one page before MuPDF is touched: the removal bands with
    their fill colour in hit order, the boxes the user drew (pixels only), the boxes
    whose characters the layout check must skip, the black mask areas and the
    pseudonyms to write."""

    bands: list[tuple[pymupdf.Rect, Colour]] = field(default_factory=list)
    pixels_only: list[tuple[pymupdf.Rect, Colour]] = field(default_factory=list)
    without_layout_check: list[pymupdf.Rect] = field(default_factory=list)
    areas: list[tuple[pymupdf.Rect, Colour]] = field(default_factory=list)
    overlays: list[_Overlay] = field(default_factory=list)


def _is_drawn(f: Hit, page: PageText | None) -> bool:
    """True for a box the user drew over text: its rects are the text spans per line,
    followed by the box itself."""
    return f.source == "manuell" and f.end > f.start and page is not None


def _text_band(page: PageText, f: Hit, rect: pymupdf.Rect) -> _RemovalBand:
    """The removal band of a text-layer hit: clipped to its own line so that MuPDF
    leaves the neighbouring lines alone, or the box itself when no character of the
    hit touches it."""
    line = _line_of_rect(page, f, rect)
    band = _band(rect, line, page) if line >= 0 else pymupdf.Rect(rect)
    return _RemovalBand(pymupdf.Rect(rect), band, False)


def _image_band(pdf_page: pymupdf.Page, rect: pymupdf.Rect, ink: _Ink | None) -> _RemovalBand:
    """The removal box of a hit on image pixels (OCR, codes): an OCR box is first
    widened to the nearest word gaps, then every box gets a margin, because the
    position of a hit inside an OCR box is an estimate."""
    if ink is not None:
        rect = ink.widen(rect)
    margin = max(OCR_MARGIN, 0.15 * rect.height)
    area = pymupdf.Rect(rect.x0 - margin, rect.y0 - OCR_MARGIN,
                        rect.x1 + margin, rect.y1 + OCR_MARGIN) & pdf_page.rect
    return _RemovalBand(pymupdf.Rect(area), pymupdf.Rect(area), True)


def _removal_band(
    pdf_page: pymupdf.Page, page: PageText | None, f: Hit, rect: pymupdf.Rect,
    drawn: bool, ink: _Ink | None,
) -> _RemovalBand:
    """Decide how ``rect`` of ``f`` is removed: by a line-clipped band when the hit
    lives in the text layer, by the box itself for a box drawn on an image page,
    and by a padded box for OCR and code hits."""
    if page is not None and (f.source == "text" or drawn):
        return _text_band(page, f, rect)
    if f.source == "manuell":
        return _RemovalBand(pymupdf.Rect(rect), pymupdf.Rect(rect) & pdf_page.rect, True)
    return _image_band(pdf_page, rect, ink if f.source == "ocr" else None)


def _overlay_for(
    page: PageText | None, f: Hit, rb: _RemovalBand, opt: RedactionOptions
) -> _Overlay:
    """Decide where and how large the pseudonym of ``f`` is written: on the original
    baseline for a text-layer hit, widened to the right into free space when the
    pseudonym needs more room than the hit, and shrunk otherwise.  Widens
    ``rb.area`` and ``rb.band`` in place so that removal and fill cover that room."""
    area, band = rb.area, rb.band
    base = free = None
    if page is not None and not rb.on_image:
        base = page.baseline(f.start, f.end)
        free = page.free_space_right(f.end)
    if base is None:
        base = area.y1 - 0.22 * area.height
    if free is None:
        free = area.x1
    needed = pymupdf.get_text_length(
        f.pseudonym, fontname=opt.font, fontsize=f.font_size
    ) + 2.0
    x1 = max(area.x1, min(area.x0 + needed, free - 1.0))
    area.x1 = band.x1 = max(band.x1, x1 - 0.2)
    size = _fitting_size(f.pseudonym, area.width - 1.5, f.font_size, opt)
    return _Overlay(area, base, size, f.pseudonym)


def _collect_match(
    pdf_page: pymupdf.Page, page: PageText | None, f: Hit, ink: _Ink | None,
    opt: RedactionOptions, plan: _RedactionPlan,
) -> None:
    """Add the boxes of one hit to ``plan``.  A box the user drew over text is
    separated from the text spans in front of it: it only blanks pixels, because a
    drawn box reaches into the glyph boxes of neighbouring lines.  The first text
    span carries the pseudonym; masks get a black area instead."""
    drawn = _is_drawn(f, page)
    colour = BLACK if is_mask(f) else opt.fill_colour
    spans = list(f.rects)
    if drawn and len(spans) > 1:
        plan.pixels_only.append((pymupdf.Rect(spans.pop()) & pdf_page.rect, colour))
    for k, raw in enumerate(spans):
        rb = _removal_band(pdf_page, page, f, pymupdf.Rect(raw), drawn, ink)
        if rb.on_image:
            plan.without_layout_check.append(pymupdf.Rect(rb.band))
        if is_mask(f):
            plan.areas.append((rb.area, BLACK))
        elif k == 0:
            plan.overlays.append(_overlay_for(page, f, rb, opt))
        plan.bands.append((rb.band, colour))


def _collect_boxes(
    pdf_page: pymupdf.Page, page: PageText | None, hits: list[Hit],
    opt: RedactionOptions,
) -> _RedactionPlan:
    """Decide for every rectangle of every hit how it is removed and painted, without
    touching the page yet.  The grey rendering for snapping OCR boxes is taken once,
    and only when an OCR hit needs it."""
    plan = _RedactionPlan()
    ink = _Ink(pdf_page) if any(f.source == "ocr" for f in hits) else None
    for f in hits:
        _collect_match(pdf_page, page, f, ink, opt, plan)
    return plan


def _apply_boxes(pdf_page: pymupdf.Page, plan: _RedactionPlan, opt: RedactionOptions) -> int:
    """Hand the boxes to MuPDF in two passes and return their number: first the
    bands that remove text, then the boxes the user drew, which must not touch
    any text."""
    for band, colour in plan.bands:
        pdf_page.add_redact_annot(band, fill=colour, cross_out=False)
    if plan.bands:
        pdf_page.apply_redactions(
            images=opt.images, graphics=opt.graphics, text=pymupdf.PDF_REDACT_TEXT_REMOVE
        )
    if plan.pixels_only:
        for rect, colour in plan.pixels_only:
            pdf_page.add_redact_annot(rect, fill=colour, cross_out=False)
        pdf_page.apply_redactions(images=opt.images, graphics=opt.graphics,
                                  text=pymupdf.PDF_REDACT_TEXT_NONE)
    return len(plan.bands) + len(plan.pixels_only)


def _layout_kept(
    pdf_page: pymupdf.Page, page: PageText | None, hits: list[Hit],
    plan: _RedactionPlan,
) -> bool:
    """Re-read the page and check that every character outside the hits kept its
    place.  Only a page with a text layer that a hit or a box touched is checked."""
    if page is None:
        return True
    matches = [f for f in hits if f.source in ("text", "manuell")]
    if not matches and not plan.without_layout_check:
        return True
    return layout_intact(page, read_text_layer(pdf_page, page.number), matches,
                         boxes=plan.without_layout_check)


def _draw_overlays(pdf_page: pymupdf.Page, plan: _RedactionPlan, opt: RedactionOptions) -> None:
    """Paint all boxes first, then all pseudonyms, so that a neighbouring box never
    covers a text."""
    for rect, colour in plan.pixels_only + plan.areas:
        pdf_page.draw_rect(rect, color=None, fill=colour, overlay=True)
    for e in plan.overlays:
        pdf_page.draw_rect(e.fill, color=None, fill=opt.fill_colour, overlay=True)
    for e in _without_overlap(plan.overlays, opt):
        _write_overlay(pdf_page, e, opt)


def redact_page(
    pdf_page: pymupdf.Page,
    page: PageText | None,
    hits: list[Hit],
    opt: RedactionOptions | None = None,
) -> PageResult:
    """Apply all hits of one page in two passes.

    Pass one removes text: text-layer hits and the text spans of manual boxes
    use the band clipped to their line (so neighbouring lines are untouched),
    OCR and code hits use their padded box.  Pass two blanks image pixels under
    the boxes the user drew without touching any text, because a drawn box
    reaches into the glyph boxes of neighbouring lines.  Returns the number of
    boxes and whether the remaining text kept its position; the caller
    rasterises the page if not.
    """
    opt = opt or RedactionOptions()
    plan = _collect_boxes(pdf_page, page, hits, opt)
    if not plan.bands and not plan.pixels_only:
        return PageResult(0, True)
    count = _apply_boxes(pdf_page, plan, opt)
    if not _layout_kept(pdf_page, page, hits, plan):
        return PageResult(count, False)
    _draw_overlays(pdf_page, plan, opt)
    return PageResult(count, True)


def _right_limit(e: _Overlay, other: list[_Overlay]) -> float:
    """Left edge of the nearest box on the same line to the right of ``e``, or its
    own right edge when nothing follows."""
    right = e.fill.x1
    for a in other:
        if a is e or abs(a.base - e.base) > 3:
            continue
        if e.fill.x0 < a.fill.x0 < right:
            right = a.fill.x0
    return right


def _without_overlap(
    overlays: list[_Overlay], opt: RedactionOptions
) -> list[_Overlay]:
    """Shrink a pseudonym to the room before the next box on the same line, so that
    overlapping boxes (frequent on scans) never print one text over another."""
    result = []
    for e in overlays:
        right = _right_limit(e, overlays)
        size = e.size
        if right < e.fill.x1:
            size = _fitting_size(e.pseudonym, right - e.fill.x0 - 1.5, size, opt)
        result.append(e._replace(size=size))
    return result


def _word_box(page: PageText, a: int, b: int) -> pymupdf.Rect | None:
    """The union of the boxes of the word ``seite.text[a:b]``, or None without boxes."""
    box = page.rects(a, b)
    if not box:
        return None
    word = pymupdf.Rect(box[0])
    for r in box[1:]:
        word |= r
    return word


def _in_match(a: int, b: int, matches: list[Hit]) -> bool:
    """True when the span ``a:b`` overlaps the text span of a hit."""
    return any(a < f.end and b > f.start for f in matches)


def _under_match(word: pymupdf.Rect, rects: list[pymupdf.Rect]) -> bool:
    """True when the word box touches the box of a hit."""
    return any(r.intersects(word) for r in rects)


def _free_words(
    page: PageText, hits: list[Hit]
) -> list[tuple[str, pymupdf.Rect]]:
    """The words of ``page`` with their boxes, except words a hit covers: by offset
    for hits with a text span, by rectangle for the boxes of every hit."""
    matches = [f for f in hits if f.end > f.start]
    rects = [pymupdf.Rect(r) for f in hits for r in f.rects]
    free: list[tuple[str, pymupdf.Rect]] = []
    for a, b in page.words():
        word = None if _in_match(a, b, matches) else _word_box(page, a, b)
        if word is not None and not _under_match(word, rects):
            free.append((page.text[a:b], word))
    return free


def _invisible_size(text: str, rect: pymupdf.Rect, opt: RedactionOptions) -> float:
    """The font size that makes ``text`` as wide as its box (so that selecting it in a
    reader covers the printed word), capped a little above the box height."""
    by_height = max(2.0, rect.height * 0.8)
    unit = pymupdf.get_text_length(text, fontname=opt.font, fontsize=1.0)
    size = min(by_height * 1.15, rect.width / unit) if unit > 0 else by_height
    return max(2.0, size)


def _write_invisible(
    pdf_page: pymupdf.Page, text: str, rect: pymupdf.Rect, opt: RedactionOptions
) -> bool:
    """Write ``text`` invisibly (render mode 3) over ``rect``; False when there is
    nothing to write.  A glyph the base font lacks is replaced by what it can encode."""
    if rect.is_empty or rect.height <= 0 or not text.strip():
        return False
    size = _invisible_size(text, rect, opt)
    try:
        pdf_page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), text, fontsize=size,
                             fontname=opt.font, render_mode=3, overlay=True)
    except Exception:
        replacement = text.encode("cp1252", "replace").decode("cp1252")
        pdf_page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), replacement, fontsize=size,
                             fontname=opt.font, render_mode=3, overlay=True)
    return True


def _write_pseudonyms(
    pdf_page: pymupdf.Page, hits: list[Hit], opt: RedactionOptions
) -> int:
    """Write the pseudonym of every hit invisibly over its first box (masks have
    none) and return how many were written."""
    count = 0
    for f in hits:
        if not f.rects or not f.pseudonym or is_mask(f):
            continue
        if _write_invisible(pdf_page, f.pseudonym, pymupdf.Rect(f.rects[0]), opt):
            count += 1
    return count


def invisible_text_layer(
    pdf_page: pymupdf.Page, page: PageText, hits: list[Hit],
    with_pseudonyms: bool = False, opt: RedactionOptions | None = None,
) -> int:
    """Write the words of ``page`` invisibly (render mode 3) where they stand, except
    words that a hit covers; with ``with_pseudonyms`` the pseudonyms are written
    invisibly too (for rasterised pages, whose visible pseudonyms are pixels).  The
    result is a searchable text layer for scans and photos, which a language model
    or a PDF reader can extract like the text of a born-digital document.
    Returns the number of words written."""
    opt = opt or RedactionOptions()
    count = 0
    for text, word in _free_words(page, hits):
        if _write_invisible(pdf_page, text, word, opt):
            count += 1
    if with_pseudonyms:
        count += _write_pseudonyms(pdf_page, hits, opt)
    return count


def sanitise(doc: pymupdf.Document, notice: bool = True, date: dt.date | None = None) -> None:
    """Remove metadata, XMP, attachments, bookmarks, annotations and form fields.

    Hidden text stays: it was read and redacted like visible text, and it is the
    text layer of scanned PDFs (theirs or ours)."""
    doc.scrub(
        attached_files=True,
        clean_pages=True,
        embedded_files=True,
        hidden_text=False,
        javascript=True,
        metadata=True,
        redactions=True,
        redact_images=0,
        remove_links=True,
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    for pdf_page in doc:
        for widget in list(pdf_page.widgets()):
            pdf_page.delete_widget(widget)
        for annot in list(pdf_page.annots()):
            pdf_page.delete_annot(annot)
    doc.set_toc([])
    meta = {k: "" for k in ("title", "author", "subject", "keywords", "creator", "producer",
                            "creationDate", "modDate")}
    if notice:
        date = date or dt.date.today()
        meta.update(ALLOWED_METADATA)
        meta["subject"] = f"{NOTICE_PREFIX} am {date.strftime('%d.%m.%Y')}"
    doc.set_metadata(meta)


def rasterise_document(doc: pymupdf.Document, dpi: int = 200) -> pymupdf.Document:
    """Return a new document whose pages are plain images of the given one."""
    new = pymupdf.open()
    for pdf_page in doc:
        pix = pdf_page.get_pixmap(dpi=dpi, alpha=False)
        page = new.new_page(width=pdf_page.rect.width, height=pdf_page.rect.height)
        page.insert_image(page.rect, pixmap=pix)
    return new


def save(doc: pymupdf.Document, target: Path) -> None:
    """Full (non-incremental) save with garbage collection so that nothing removed survives."""
    doc.save(
        target,
        garbage=4,
        deflate=True,
        clean=True,
        incremental=False,
        encryption=pymupdf.PDF_ENCRYPT_NONE,
    )
