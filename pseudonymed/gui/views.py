"""The two views of the review step: the page with its boxes and marks, and the text
of a text file with its highlighted spans.  (The steps of preparation and recognition show
the pages in docrepro's own views, :mod:`doccor.gui.view`.)"""


from __future__ import annotations

from collections.abc import Sequence

from ocrdoc.pagetext import Box
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPen,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QTextEdit,
    QWidget,
)

from pseudonymed.core.model import Hit

# Traffic light: red = will be redacted, yellow = uncertain (read badly, blanked as a
# precaution), green = stays (medical term, discarded hit).  The source of a hit is in
# the table; guesses (NER, fuzzy search, identifiers) get a dashed outline.
RED = QColor(200, 30, 30)
YELLOW = QColor(230, 165, 0)
GREEN = QColor(40, 160, 60)
BLUE = QColor(30, 90, 200)  # a word the user selected to take into the list
BLACK = QColor(0, 0, 0)
PREVIEW_DPI = 100


def colour(f: Hit) -> QColor:
    """The traffic-light colour of a hit: yellow for a line blanked as unreadable, red for
    everything that will be redacted."""
    return YELLOW if f.rule == "ocr.unlesbar" else RED


def is_guess(f: Hit) -> bool:
    """A hit from a heuristic rather than a rule or the user."""
    return f.rule.startswith(("ner.", "unscharf.", "identifikator."))


def _match_colour(f: Hit, selected: bool) -> tuple[QColor, int]:
    """Colour and fill opacity of a hit: a mask is solid black, every other hit wears its
    traffic-light colour, more opaque when it is the selected one."""
    if f.rule == "maske":
        return BLACK, 160
    return colour(f), 90 if selected else 45


def _rect(rect: Box, scale_factor: float) -> QRectF:
    """A page rectangle (points) as a scene rectangle (pixels of the preview)."""
    return QRectF(rect[0] * scale_factor, rect[1] * scale_factor,
                  (rect[2] - rect[0]) * scale_factor,
                  (rect[3] - rect[1]) * scale_factor)


class PageView(QGraphicsView):
    """One page as an image with a rectangle per hit; drag on empty space draws a box."""

    match_selected = Signal(object)
    box_drawn = Signal(object)  # (x0, y0, x1, y1) in page points
    point_selected = Signal(float, float, bool)  # a click on text: x, y in points, extend?

    def __init__(self, parent: QWidget | None = None) -> None:
        """An empty scene at preview scale."""
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(self.renderHints())
        self.scale_factor = PREVIEW_DPI / 72.0
        self._items: dict[int, Hit] = {}
        self._start: QPointF | None = None
        self._rubber_band: QGraphicsRectItem | None = None
        self._extend = False  # Ctrl/Cmd was held when the drag (or click) began
        self.mask_mode = False  # drag draws a mask instead of a labelled box

    # -- painting ----------------------------------------------------------------------------------
    def _box(self, rect: Box, colour_: QColor, width: float,
             alpha: int, dashed: bool = False, note: str = "") -> QGraphicsRectItem:
        """Add one rectangle to the scene: outline in the colour, fill in the same colour
        with the given opacity, dashed for a guess, a tooltip when there is one."""
        item = QGraphicsRectItem(_rect(rect, self.scale_factor))
        pen = QPen(colour_, width)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        fill = QColor(colour_)
        fill.setAlpha(alpha)
        item.setBrush(QBrush(fill))
        if note:
            item.setToolTip(note)
        self._scene.addItem(item)
        return item

    def display(self, png: bytes, hits: list[Hit], selected: Hit | None,
                term_rects: Sequence[Box] = (),
                discarded: Sequence[Hit] = (),
                uncertain: Sequence[Box] = (),
                selection: Sequence[Box] = ()) -> None:
        """Paint the page and everything on it, in this order (later lies on top): the
        protected medical terms, the words the user picked, the badly read words, the
        discarded hits, the hits.  Only hits and discarded hits are clickable."""
        self._show_image(png)
        self._show_marks(term_rects, selection, uncertain)
        self._show_discarded(discarded, selected)
        self._show_matches(hits, selected)

    def _show_image(self, png: bytes) -> None:
        """Start over with the page image; nothing is clickable yet."""
        self._scene.clear()
        self._items.clear()
        image = QImage.fromData(png)
        self._scene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(image)))
        self._scene.setSceneRect(0, 0, image.width(), image.height())

    def _show_marks(self, term_rects: Sequence[Box], selection: Sequence[Box],
                    uncertain: Sequence[Box]) -> None:
        """Marks that are not hits: green for a protected medical term (it stays), blue for
        a word the user picked (not yet in the list), a yellow outline for a word the OCR
        read badly (worth a look)."""
        for rect in term_rects:
            self._box(rect, GREEN, 1, 28, note=self.tr("Fachbegriff (bleibt)"))
        for rect in selection:
            self._box(rect, BLUE, 2, 50, note=self.tr("Auswahl (aufnehmen?)"))
        for rect in uncertain:
            self._box(rect, YELLOW, 1, 0, note=self.tr("unsicher gelesen"))

    def _show_discarded(self, discarded: Sequence[Hit],
                        selected: Hit | None) -> None:
        """Discarded hits: green and dashed, clickable so that they can be taken back; the
        selected one heavier and more opaque."""
        for f in discarded:
            note = self.tr("verworfen, bleibt: {regel}").format(regel=f.rule)
            for rect in f.rects:
                item = self._box(rect, GREEN, 3 if f is selected else 1.5,
                                 60 if f is selected else 30, dashed=True,
                                 note=note)
                self._items[id(item)] = f

    def _show_matches(self, hits: Sequence[Hit],
                      selected: Hit | None) -> None:
        """The hits: traffic-light colour or black for a mask, dashed for a guess, the
        selected one heavier; the tooltip names rule and pseudonym."""
        for f in hits:
            c, alpha = _match_colour(f, f is selected)
            for rect in f.rects:
                item = self._box(rect, c, 3 if f is selected else 1.5, alpha,
                                 dashed=is_guess(f),
                                 note=f"{f.rule} → {f.pseudonym}")
                self._items[id(item)] = f

    def show_empty(self) -> None:
        """Clear the view (no document chosen)."""
        self._scene.clear()
        self._items.clear()

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Ctrl (Cmd) + wheel zooms, plain wheel scrolls."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            return
        super().wheelEvent(event)

    def zoom(self, factor: float) -> None:
        """Zoom by the factor (the zoom buttons)."""
        self.scale(factor, factor)

    def reset_zoom(self) -> None:
        """Back to the scale the page was shown at."""
        self.resetTransform()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & (Qt.KeyboardModifier.ShiftModifier
                                | Qt.KeyboardModifier.AltModifier) \
                or event.button() == Qt.MouseButton.MiddleButton:
            # Shift or Option/Alt (or the middle button) + drag moves the view
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            if event.button() == Qt.MouseButton.MiddleButton:
                event = _as_left_click(event)
            super().mousePressEvent(event)
            return
        pos = self.mapToScene(event.position().toPoint())
        for item in self._scene.items(pos):
            f = self._items.get(id(item))
            if f is not None:
                self.match_selected.emit(f)
                return
        self._extend = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._start = pos
        self._rubber_band = QGraphicsRectItem(QRectF(pos, pos))
        colour_ = BLACK if self.mask_mode else RED
        self._rubber_band.setPen(QPen(colour_, 1.5, Qt.PenStyle.SolidLine if self.mask_mode
                                      else Qt.PenStyle.DashLine))
        self._scene.addItem(self._rubber_band)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseMoveEvent(event)
            return
        if self._start is not None and self._rubber_band is not None:
            pos = self.mapToScene(event.position().toPoint())
            self._rubber_band.setRect(QRectF(self._start, pos).normalized())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            return
        if self._start is None or self._rubber_band is None:
            return
        r = self._rubber_band.rect()
        self._scene.removeItem(self._rubber_band)
        self._start, self._rubber_band = None, None
        if r.width() > 4 and r.height() > 4:
            self.box_drawn.emit((r.left() / self.scale_factor, r.top() / self.scale_factor,
                                         r.right() / self.scale_factor,
                                         r.bottom() / self.scale_factor))
        else:  # a plain click: the word under it (Ctrl/Cmd extends the selection)
            self.point_selected.emit(r.left() / self.scale_factor, r.top() / self.scale_factor,
                                     self._extend)


def _as_left_click(event):
    """The middle button as a left button, so that Qt's hand-drag accepts it."""
    from PySide6.QtGui import QMouseEvent

    return QMouseEvent(event.type(), event.position(), event.globalPosition(),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, event.modifiers())


class TextView(QTextEdit):
    """A text file with a coloured span per hit; a click on a span selects the hit,
    a text selection can be marked by hand."""

    match_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        """A read-only editor; the font is remembered so that the zoom can be reset."""
        super().__init__(parent)
        self.setReadOnly(True)
        self._matches: list[Hit] = []
        self._text: str | None = None
        self._font = self.font()

    def display(self, text: str, hits: list[Hit], selected: Hit | None,
                term_spans: Sequence[tuple[int, int]] = (),
                discarded: Sequence[Hit] = ()) -> None:
        """Show the text with a background span per medical term, discarded hit and hit,
        and scroll to the selected hit.  Empty spans are neither shown nor clickable."""
        self._set_text(text)
        active = [f for f in hits if f.end > f.start]
        kept = [f for f in discarded if f.end > f.start]
        self._matches = active + kept
        self.setExtraSelections(self._highlights(term_spans, kept, active, selected))
        self._jump_to(selected, text)

    def _set_text(self, text: str) -> None:
        """Load the text only when it changed, so that a redraw keeps scroll position and
        selection."""
        if text != self._text:
            self._text = text
            self.setPlainText(text)

    def _highlights(self, term_spans: Sequence[tuple[int, int]],
                    kept: Sequence[Hit], active: Sequence[Hit],
                    selected: Hit | None) -> list[QTextEdit.ExtraSelection]:
        """One background span each: green for medical terms and discarded hits, the
        traffic-light colour for hits; the selected hit is more opaque."""
        selections = [self._selection(a, b, GREEN, 45) for a, b in term_spans]
        selections += [self._selection(f.start, f.end, GREEN, 150 if f is selected else 70)
                       for f in kept]
        selections += [self._selection(f.start, f.end, colour(f), 170 if f is selected else 80)
                       for f in active]
        return selections

    def _jump_to(self, selected: Hit | None, text: str) -> None:
        """Put the cursor at the start of the selected hit and scroll it into view."""
        if selected is None or selected.end <= selected.start:
            return
        cursor = self.textCursor()
        cursor.setPosition(min(selected.start, len(text)))
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _selection(self, a: int, b: int, c: QColor, alpha: int) -> QTextEdit.ExtraSelection:
        """A background span over ``[a, b)`` in the colour with the given opacity."""
        s = QTextEdit.ExtraSelection()
        cursor = QTextCursor(self.document())
        cursor.setPosition(a)
        cursor.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
        s.cursor = cursor
        fmt = QTextCharFormat()
        light = QColor(c)
        light.setAlpha(alpha)
        fmt.setBackground(QBrush(light))
        s.format = fmt
        return s

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        pos = self.cursorForPosition(event.position().toPoint()).position()
        for f in self._matches:
            if f.start <= pos < f.end:
                self.match_selected.emit(f)
                return

    def selection(self) -> tuple[int, int] | None:
        """Offsets of the current selection, or None."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        return cursor.selectionStart(), cursor.selectionEnd()

    def zoom(self, factor: float) -> None:
        """One font step larger for a factor above 1, else one smaller (the zoom buttons)."""
        if factor > 1:
            self.zoomIn(1)
        else:
            self.zoomOut(1)

    def reset_zoom(self) -> None:
        """Back to the font the view started with."""
        self.setFont(self._font)
