"""The views of the visual review, driven offscreen with synthetic hits: which rectangles
and spans are painted for which kind of mark, and what the page does without a page."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsRectItem  # noqa: E402

from pseudonymed.core.model import Hit, Kind, Patient  # noqa: E402
from pseudonymed.gui import views as module  # noqa: E402
from pseudonymed.gui.review_page import ReviewPage  # noqa: E402
from pseudonymed.gui.session import Session  # noqa: E402
from pseudonymed.gui.views import PageView, TextView  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _png(width: int = 200, height: int = 100) -> bytes:
    """A white page image of the given size."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _matches(rule: str, rects: list[tuple[float, float, float, float]] = (),
             start: int = 0, end: int = 0, source: str = "text") -> Hit:
    """A synthetic hit of the rule with the given boxes or span; its pseudonym names it."""
    return Hit(page=0, start=start, end=end, text="x", kind=Kind.MANUAL, rule=rule,
               pseudonym=f"[{rule}]", rects=list(rects), source=source)


def _with_colour(boxes: list[QGraphicsRectItem], colour: QColor) -> list[QGraphicsRectItem]:
    return [k for k in boxes if k.pen().color() == colour]


def test_page_view_paints_every_kind(qapp: QApplication) -> None:
    """Every kind of mark gets its colour, the hits lie on top, and only hits and discarded
    hits are clickable."""
    view = PageView()
    red = _matches("name.nachname", [(10, 10, 50, 20)])
    suspected = _matches("ner.person", [(10, 30, 50, 40)])
    yellow = _matches("ocr.unlesbar", [(10, 50, 50, 60)], source="ocr")
    add_mask = _matches("maske", [(60, 10, 90, 20)], source="manuell")
    discarded = _matches("zuweiser1.name", [(60, 30, 90, 40), (60, 42, 90, 52)])
    view.display(_png(), [red, suspected, yellow, add_mask], suspected,
                 term_rects=[(100, 10, 140, 20)], discarded=[discarded],
                 uncertain=[(100, 30, 140, 40)], selection=[(100, 50, 140, 60)])
    boxes = [i for i in view.scene().items() if isinstance(i, QGraphicsRectItem)]
    assert len(boxes) == 4 + 2 + 3  # four hits, two lines of the discarded one, three marks
    assert len(_with_colour(boxes, module.RED)) == 2  # the rule hit and the guess
    assert len(_with_colour(boxes, module.YELLOW)) == 2  # the unreadable line, the unsure word
    assert len(_with_colour(boxes, module.GREEN)) == 3  # the term, the discarded hit's lines
    assert len(_with_colour(boxes, module.BLUE)) == 1  # the picked word
    assert len(_with_colour(boxes, module.BLACK)) == 1  # the mask
    # the selected guess is heavier, more opaque and dashed; a plain hit is thin and solid
    [g] = [k for k in boxes if k.toolTip() == "ner.person → [ner.person]"]
    assert g.pen().widthF() == 3 and g.pen().style() == Qt.PenStyle.DashLine
    assert g.brush().color().alpha() == 90
    [r] = [k for k in boxes if k.toolTip() == "name.nachname → [name.nachname]"]
    assert r.pen().widthF() == 1.5 and r.pen().style() == Qt.PenStyle.SolidLine
    assert r.brush().color().alpha() == 45
    [m] = _with_colour(boxes, module.BLACK)
    assert m.brush().color().alpha() == 160 and m.pen().style() == Qt.PenStyle.SolidLine
    # the discarded hit: dashed green, both lines lead back to it
    green = _with_colour(boxes, module.GREEN)
    assert sum(1 for k in green if view._items.get(id(k)) is discarded) == 2
    assert all(k.pen().style() == Qt.PenStyle.DashLine for k in green
               if view._items.get(id(k)) is discarded)
    # the marks are not clickable; the unsure word is an outline only
    assert {id(f) for f in view._items.values()} == {
        id(red), id(suspected), id(yellow), id(add_mask), id(discarded)}
    [u] = [k for k in boxes if k.toolTip() == "unsicher gelesen"]
    assert u.brush().color().alpha() == 0
    assert any(k.toolTip() == "Fachbegriff (bleibt)" for k in boxes)
    # painting order: the scene lists the topmost first, so hits lie above the term marks
    assert view._items.get(id(boxes[0])) is add_mask
    assert boxes[-1].toolTip() == "Fachbegriff (bleibt)"
    view.show_empty()
    assert not view.scene().items() and not view._items


def test_page_view_without_marks(qapp: QApplication) -> None:
    """A page with only the image: nothing but the pixmap, nothing clickable; a second
    call replaces the first page completely."""
    view = PageView()
    view.display(_png(), [_matches("name.vorname", [(1, 1, 5, 5)])], None)
    assert len(view._items) == 1
    view.display(_png(50, 50), [], None)
    assert len(view.scene().items()) == 1 and not view._items
    assert view.scene().sceneRect().width() == 50


def test_text_view_spans(qapp: QApplication) -> None:
    """Spans in painting order (terms, discarded, hits), empty spans left out, the cursor on
    the selected hit, and the text loaded only once."""
    view = TextView()
    text = "Die Person kam am Montag mit Kopfschmerzen in die Sprechstunde."
    a = text.index("Montag")
    matches = _matches("muster.datum", start=a, end=a + len("Montag"))
    empty = _matches("manuell", start=3, end=3)
    discarded = _matches("ner.person", start=4, end=4 + len("Person"))
    k = text.index("Kopfschmerzen")
    view.display(text, [matches, empty], matches, term_spans=[(k, k + 13)],
                 discarded=[discarded])
    assert view.toPlainText() == text
    assert view._matches == [matches, discarded]  # the empty span is not clickable
    spans = [(s.cursor.selectionStart(), s.cursor.selectionEnd(),
              s.format.background().color()) for s in view.extraSelections()]
    assert [(x, y) for x, y, _c in spans] == [(k, k + 13), (4, 10), (a, a + 6)]
    assert [c.alpha() for _x, _y, c in spans] == [45, 70, 170]  # the hit is selected
    assert spans[0][2].rgb() == module.GREEN.rgb() and spans[2][2].rgb() == module.RED.rgb()
    assert view.textCursor().position() == a
    revision = view.document().revision()
    view.display(text, [matches], None, discarded=[discarded])
    assert view.document().revision() == revision  # same text: not reloaded
    assert [c.alpha() for s in view.extraSelections()
            for c in [s.format.background().color()]] == [70, 80]
    view.display(text, [empty], empty)
    assert view.textCursor().position() == a  # an empty selection moves nothing
    view.display(text + " Nachtrag.", [], None)
    assert view.document().revision() != revision and not view.extraSelections()
    font = view.font()
    view.zoom(1.25)
    view.zoom(0.8)
    view.reset_zoom()
    assert view.font() == font


def test_review_page_without_document(qapp: QApplication, tmp_path: Path) -> None:
    """Before a document was chosen every action is a no-op and nothing is written."""
    page = ReviewPage(Session(store_path=tmp_path / "s.sqlite"))
    page.refresh()
    assert page.document is None and page.hits() == []
    page.draw()
    page.box((0, 0, 10, 10))
    page.point(1.0, 1.0)
    page.discard()
    page.take_up()
    page.mark_selection()
    assert page.save_working_state(tmp_path / "x.arbeitsstand.json") is None
    assert page.table.rowCount() == 0 and page.note.text() == ""
    assert page._active_view() is page.view
    assert not list(tmp_path.glob("*.arbeitsstand.*"))


def test_review_page_text_file(qapp: QApplication, tmp_path: Path, patient: Patient) -> None:
    """A text file has no page: boxes and word picks do nothing, the text view is in front,
    and the replacement for a take-up comes from the field unless it holds the default."""
    from tests.synthetic import create_transcript

    session = Session(store_path=tmp_path / "s.sqlite")
    session.patient = patient
    d = session.add(create_transcript(tmp_path / "gespraech.txt", patient))
    assert d is not None
    session.analyse(d)
    page = ReviewPage(session)
    page.synchronous = True
    page.refresh()
    assert page.document is d and page.stack.currentWidget() is page.text_view
    assert page._active_view() is page.text_view
    assert page.mark_button.isEnabled() and page.mask_mode_button.isHidden()
    assert page.table.rowCount() == len(page.hits()) > 0
    note = page.note.text()
    before = list(page.hits())
    page.box((10, 10, 50, 20), "[Kasten]")  # no page to draw on
    page.point(10.0, 10.0)
    assert page.note.text() == note and page.selection is None
    assert page.hits() == before
    assert page._replacement(None) is None and page._replacement("[X]") == "[X]"
    page.replacement_text.setText("  ")
    assert page._replacement(None) is None
    page.replacement_text.setText("[Begriff]")
    assert page._replacement(None) == "[Begriff]" and page._replacement("[X]") == "[X]"
    with pytest.raises(ValueError):
        page.take_up()  # nothing selected
    session.close()
