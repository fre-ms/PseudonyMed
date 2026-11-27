"""Redaction keeps neighbours, detects shifted layout and can fall back to an image."""

from __future__ import annotations

import pymupdf
from ocrdoc.pagetext import read_text_layer

from pseudonymed.core.model import Hit, Kind
from pseudonymed.redaction.pdf import (
    OCR_MARGIN,
    RedactionOptions,
    _collect_boxes,
    _free_words,
    _image_band,
    invisible_text_layer,
    layout_intact,
    rasterise_page,
    redact_page,
    sanitise,
)

LINE = "Herrn Flantz, geb. heute   Pat.-ID 123"


def _document_of(leading: float = 1.2) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 100.0
    for text in (LINE, "Zweite Zeile bleibt unberührt", "Dritte Zeile bleibt auch"):
        page.insert_text((56, y), text, fontsize=10.5, fontname="helv")
        y += 10.5 * leading
    return doc


def _matches(page: pymupdf.Page, word: str, pseudonym: str = "Patient A") -> Hit:
    s = read_text_layer(page)
    a = s.text.index(word)
    b = a + len(word)
    return Hit(0, a, b, word, Kind.SURNAME, "test", pseudonym,
               rects=[tuple(r) for r in s.rects(a, b)],
               font_size=s.font_size(a, b))


def test_neighbours_remain_even_with_tight_line_spacing() -> None:
    for leading in (1.2, 1.0):
        doc = _document_of(leading)
        page = doc[0]
        f = _matches(page, "Flantz")
        res = redact_page(page, read_text_layer(page), [f], RedactionOptions())
        assert res.count == 1 and res.layout_intact
        text = page.get_text("text")
        assert "Flantz" not in text
        assert "Zweite Zeile bleibt unberührt" in text
        assert "Dritte Zeile bleibt auch" in text
        assert ", geb. heute" in text and "Pat.-ID 123" in text
        assert "Patient A" in text


def test_pseudonym_is_drawn_visibly() -> None:
    doc = _document_of()
    page = doc[0]
    f = _matches(page, "Flantz", "[Versichertennummer]")
    redact_page(page, read_text_layer(page), [f], RedactionOptions())
    drawings = page.get_drawings()
    assert any(d.get("fill") for d in drawings)
    # the comma after the name is still rendered to the right of the box
    s = read_text_layer(page)
    comma = s.boxes[s.text.index(",")]
    assert comma is not None and comma[0] >= f.rects[0][2] - 0.5


def test_position_intact_detects_shift() -> None:
    doc = pymupdf.open()
    before = doc.new_page()
    before.insert_text((56, 100), "Herrn Flantz, geb.", fontsize=10.5, fontname="helv")
    sv = read_text_layer(before)
    a = sv.text.index("Flantz")
    rect = tuple(sv.rects(a, a + 6)[0])
    f = Hit(0, a, a + 6, "Flantz", Kind.SURNAME, "t", "P", rects=[rect])
    x_comma = sv.boxes[a + 6][0]
    correct = doc.new_page()
    correct.insert_text((56, 100), "Herrn ", fontsize=10.5, fontname="helv")
    correct.insert_text((x_comma, 100), ", geb.", fontsize=10.5, fontname="helv")
    assert layout_intact(sv, read_text_layer(correct), [f])
    wrong = doc.new_page()
    wrong.insert_text((56, 100), "Herrn ", fontsize=10.5, fontname="helv")
    wrong.insert_text((x_comma - 6.4, 100), ", geb.", fontsize=10.5, fontname="helv")
    assert not layout_intact(sv, read_text_layer(wrong), [f])


def test_rasterise_page_replaces_page_with_image() -> None:
    doc = _document_of()
    original = pymupdf.open("pdf", doc.tobytes())
    page = doc[0]
    s = read_text_layer(page)
    f = _matches(page, "Flantz")
    rasterise_page(doc, 0, original, s, [f], RedactionOptions(dpi=72))
    assert len(doc) == 1
    assert doc[0].get_text("text").strip() == ""
    assert doc[0].get_images()
    assert abs(doc[0].rect.width - original[0].rect.width) < 0.1


def test_sanitise_removes_structure() -> None:
    doc = _document_of()
    doc.set_metadata({"title": "geheim", "author": "jemand"})
    doc.set_toc([[1, "Kapitel", 1]])
    doc[0].add_text_annot((300, 60), "Notiz")
    doc.embfile_add("a.txt", b"x")
    sanitise(doc)
    assert doc.metadata["title"] == "" and doc.metadata["author"] == ""
    assert doc.metadata["producer"].startswith("PseudonyMed")
    assert doc.get_toc() == [] and doc.embfile_count() == 0
    assert list(doc[0].annots()) == []


def test_ocr_match_deletes_image_pixels() -> None:
    """A hit from OCR on an image-only page blanks the pixels and draws the pseudonym."""
    source = _document_of()
    pix = source[0].get_pixmap(dpi=150, alpha=False)
    doc = pymupdf.open()
    page = doc.new_page(width=source[0].rect.width, height=source[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    s = read_text_layer(source[0])
    a = s.text.index("Flantz")
    rect = s.rects(a, a + 6)[0]
    before = page.get_pixmap(dpi=150, clip=rect).samples
    f = Hit(0, 0, 0, "Flantz", Kind.SURNAME, "ocr", "Patient A", rects=[tuple(rect)],
            font_size=10.0, source="ocr")
    res = redact_page(page, None, [f], RedactionOptions())
    assert res.count == 1 and res.layout_intact
    after = page.get_pixmap(dpi=150, clip=rect).samples
    assert before != after
    assert "Patient A" in page.get_text()
    assert len(page.get_images()) == 1


def test_overlay_uses_free_space_or_shrinks() -> None:
    """A pseudonym longer than the hit widens the box (and the removal band) into free
    space to the right; where the next character follows at once, the font shrinks."""
    doc = _document_of()
    page = doc[0]
    s = read_text_layer(page)
    opt = RedactionOptions()
    last = _matches(page, "123", "[Patienten-ID K7Q4MX]")  # last word of its line
    plan = _collect_boxes(page, s, [last], opt)
    e = plan.overlays[0]
    assert e.fill.x1 > last.rects[0][2] + 10
    assert e.size == last.font_size
    assert plan.bands[0][0].x1 == e.fill.x1
    assert not plan.without_layout_check and not plan.pixels_only and not plan.areas
    tight = _matches(page, "Flantz", "[Versichertennummer K7Q4MX]")  # the comma follows
    plan = _collect_boxes(page, s, [tight], opt)
    e = plan.overlays[0]
    assert e.fill.x1 <= tight.rects[0][2] + 0.01
    assert e.size < tight.font_size


def test_image_band_pads_and_crops() -> None:
    """A box on image pixels grows by the OCR margin (sideways at least 15 % of its
    height) and never leaves the page."""
    doc = pymupdf.open()
    page = doc.new_page()
    flat = _image_band(page, pymupdf.Rect(100, 100, 150, 110), None)
    assert flat.on_image and flat.band == flat.area
    assert flat.area == pymupdf.Rect(100 - OCR_MARGIN, 100 - OCR_MARGIN,
                                     150 + OCR_MARGIN, 110 + OCR_MARGIN)
    high = _image_band(page, pymupdf.Rect(-5, 0, 50, 40), None)
    assert (high.area.x0, high.area.y0, high.area.x1) == (0, 0, 56)


def test_invisible_text_layer_omits_matches() -> None:
    """The invisible layer carries every word except those a hit covers (by text span or
    by box), plus the pseudonyms on request, and leaves the rendering untouched."""
    source = _document_of()
    s = read_text_layer(source[0])
    name = _matches(source[0], "Flantz")
    a = s.text.index("Zweite")
    box = Hit(0, 0, 0, "Zweite", Kind.SURNAME, "ocr", "[Ort K7Q4MX]",
              rects=[tuple(s.rects(a, a + 6)[0])], source="ocr")
    free = [word for word, _rect in _free_words(s, [name, box])]
    assert "Flantz," not in free and "Zweite" not in free
    assert {"Pat.-ID", "123", "auch"} <= set(free)
    doc = pymupdf.open()
    page = doc.new_page(width=s.width, height=s.height)
    page.insert_image(page.rect, pixmap=source[0].get_pixmap(dpi=100, alpha=False))
    before = page.get_pixmap(dpi=72, alpha=False).samples
    count = invisible_text_layer(page, s, [name, box], with_pseudonyms=True)
    assert count == len(free) + 2
    text = page.get_text()
    assert "Flantz" not in text and "Zweite" not in text
    assert "Patient A" in text and "[Ort K7Q4MX]" in text and "auch" in text
    assert page.get_pixmap(dpi=72, alpha=False).samples == before
