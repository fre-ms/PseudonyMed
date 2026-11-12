"""The two views of the review step: the page with its boxes and marks, and the text
of a text file with its highlighted spans.  Both are reused by the clean-up and the
text-recognition steps."""


from __future__ import annotations

from collections.abc import Sequence

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

from pseudonymed.dokument.textebene import Box
from pseudonymed.kern.modell import Fundstelle

# Traffic light: red = will be redacted, yellow = uncertain (read badly, blanked as a
# precaution), green = stays (medical term, discarded hit).  The source of a hit is in
# the table; guesses (NER, fuzzy search, identifiers) get a dashed outline.
ROT = QColor(200, 30, 30)
GELB = QColor(230, 165, 0)
GRUEN = QColor(40, 160, 60)
BLAU = QColor(30, 90, 200)  # a word the user selected to take into the list
SCHWARZ = QColor(0, 0, 0)
VORSCHAU_DPI = 100


def farbe(f: Fundstelle) -> QColor:
    """The traffic-light colour of a hit: yellow for a line blanked as unreadable, red for
    everything that will be redacted."""
    return GELB if f.regel == "ocr.unlesbar" else ROT


def geraten(f: Fundstelle) -> bool:
    """A hit from a heuristic rather than a rule or the user."""
    return f.regel.startswith(("ner.", "unscharf.", "identifikator."))


def _trefferfarbe(f: Fundstelle, gewaehlt: bool) -> tuple[QColor, int]:
    """Colour and fill opacity of a hit: a mask is solid black, every other hit wears its
    traffic-light colour, more opaque when it is the selected one."""
    if f.regel == "maske":
        return SCHWARZ, 160
    return farbe(f), 90 if gewaehlt else 45


def _rect(rect: Box, skala: float) -> QRectF:
    """A page rectangle (points) as a scene rectangle (pixels of the preview)."""
    return QRectF(rect[0] * skala, rect[1] * skala, (rect[2] - rect[0]) * skala,
                  (rect[3] - rect[1]) * skala)


class Seitenansicht(QGraphicsView):
    """One page as an image with a rectangle per hit; drag on empty space draws a box."""

    treffer_gewaehlt = Signal(object)
    kasten_gezeichnet = Signal(object)  # (x0, y0, x1, y1) in page points
    punkt_gewaehlt = Signal(float, float, bool)  # a click on text: x, y in points, extend?
    gescrollt = Signal(float, float)  # relative scroll position (0..1, 0..1)
    gezoomt = Signal(float)  # absolute scale factor

    def __init__(self, eltern: QWidget | None = None) -> None:
        """An empty scene at preview scale; the scroll bars report their position."""
        super().__init__(eltern)
        self._szene = QGraphicsScene(self)
        self.setScene(self._szene)
        self.setRenderHints(self.renderHints())
        self.skala = VORSCHAU_DPI / 72.0
        self._items: dict[int, Fundstelle] = {}
        self._start: QPointF | None = None
        self._gummi: QGraphicsRectItem | None = None
        self._erweitern = False  # Ctrl/Cmd was held when the drag (or click) began
        self.masken_modus = False  # drag draws a mask instead of a labelled box
        self.loeschmodus = False  # the mask erases (white) rather than redacts (black)
        self._folgt = False  # while following another view
        self.basis_skala = 1.0  # extra scale so that a larger image shows at the same size
        self.horizontalScrollBar().valueChanged.connect(self._scroll_melden)
        self.verticalScrollBar().valueChanged.connect(self._scroll_melden)

    # -- linking two views -------------------------------------------------------------------------
    def _relativ(self, leiste) -> float:
        """A scroll bar's position as a fraction of its range (0 when it cannot scroll)."""
        return leiste.value() / leiste.maximum() if leiste.maximum() > 0 else 0.0

    def _scroll_melden(self, _wert: int) -> None:
        """Tell a linked view where we are, unless we are following it right now."""
        if not self._folgt:
            self.gescrollt.emit(self._relativ(self.horizontalScrollBar()),
                                self._relativ(self.verticalScrollBar()))

    def folge_scroll(self, x: float, y: float) -> None:
        """Move to the same relative position as another view."""
        self._folgt = True
        try:
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(round(x * h.maximum()))
            v.setValue(round(y * v.maximum()))
        finally:
            self._folgt = False

    def folge_zoom(self, faktor: float) -> None:
        """Apply a zoom factor relative to this view's base scale."""
        self._folgt = True
        try:
            self.resetTransform()
            self.scale(faktor * self.basis_skala, faktor * self.basis_skala)
        finally:
            self._folgt = False

    @property
    def zoomfaktor(self) -> float:
        """The zoom the user chose, relative to the base scale."""
        return self.transform().m11() / self.basis_skala

    def setze_basis(self, basis: float) -> None:
        """Change the base scale, keeping the zoom factor the user chose."""
        faktor = self.zoomfaktor
        self.basis_skala = basis
        self.folge_zoom(faktor)

    def _zoom_melden(self) -> None:
        """Tell a linked view the new zoom factor, unless we are following it right now."""
        if not self._folgt:
            self.gezoomt.emit(self.zoomfaktor)

    @staticmethod
    def koppeln(a: Seitenansicht, b: Seitenansicht) -> None:
        """Keep two views congruent: pan one, the other follows; zoom too."""
        a.gescrollt.connect(b.folge_scroll)
        b.gescrollt.connect(a.folge_scroll)
        a.gezoomt.connect(b.folge_zoom)
        b.gezoomt.connect(a.folge_zoom)

    # -- painting ----------------------------------------------------------------------------------
    def _kasten(self, rect: Box, farbe_: QColor, breite: float,
                alpha: int, gestrichelt: bool = False, hinweis: str = "") -> QGraphicsRectItem:
        """Add one rectangle to the scene: outline in the colour, fill in the same colour
        with the given opacity, dashed for a guess, a tooltip when there is one."""
        item = QGraphicsRectItem(_rect(rect, self.skala))
        stift = QPen(farbe_, breite)
        if gestrichelt:
            stift.setStyle(Qt.PenStyle.DashLine)
        item.setPen(stift)
        fuell = QColor(farbe_)
        fuell.setAlpha(alpha)
        item.setBrush(QBrush(fuell))
        if hinweis:
            item.setToolTip(hinweis)
        self._szene.addItem(item)
        return item

    def zeige(self, png: bytes, fundstellen: list[Fundstelle], gewaehlt: Fundstelle | None,
              fachrects: Sequence[Box] = (),
              verworfen: Sequence[Fundstelle] = (),
              unsicher: Sequence[Box] = (),
              auswahl: Sequence[Box] = ()) -> None:
        """Paint the page and everything on it, in this order (later lies on top): the
        protected medical terms, the words the user picked, the badly read words, the
        discarded hits, the hits.  Only hits and discarded hits are clickable."""
        self._zeige_bild(png)
        self._zeige_marken(fachrects, auswahl, unsicher)
        self._zeige_verworfene(verworfen, gewaehlt)
        self._zeige_treffer(fundstellen, gewaehlt)

    def _zeige_bild(self, png: bytes) -> None:
        """Start over with the page image; nothing is clickable yet."""
        self._szene.clear()
        self._items.clear()
        bild = QImage.fromData(png)
        self._szene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(bild)))
        self._szene.setSceneRect(0, 0, bild.width(), bild.height())

    def _zeige_marken(self, fachrects: Sequence[Box], auswahl: Sequence[Box],
                      unsicher: Sequence[Box]) -> None:
        """Marks that are not hits: green for a protected medical term (it stays), blue for
        a word the user picked (not yet in the list), a yellow outline for a word the OCR
        read badly (worth a look)."""
        for rect in fachrects:
            self._kasten(rect, GRUEN, 1, 28, hinweis=self.tr("Fachbegriff (bleibt)"))
        for rect in auswahl:
            self._kasten(rect, BLAU, 2, 50, hinweis=self.tr("Auswahl (aufnehmen?)"))
        for rect in unsicher:
            self._kasten(rect, GELB, 1, 0, hinweis=self.tr("unsicher gelesen"))

    def _zeige_verworfene(self, verworfen: Sequence[Fundstelle],
                          gewaehlt: Fundstelle | None) -> None:
        """Discarded hits: green and dashed, clickable so that they can be taken back; the
        selected one heavier and more opaque."""
        for f in verworfen:
            hinweis = self.tr("verworfen, bleibt: {regel}").format(regel=f.regel)
            for rect in f.rects:
                item = self._kasten(rect, GRUEN, 3 if f is gewaehlt else 1.5,
                                    60 if f is gewaehlt else 30, gestrichelt=True,
                                    hinweis=hinweis)
                self._items[id(item)] = f

    def _zeige_treffer(self, fundstellen: Sequence[Fundstelle],
                       gewaehlt: Fundstelle | None) -> None:
        """The hits: traffic-light colour or black for a mask, dashed for a guess, the
        selected one heavier; the tooltip names rule and pseudonym."""
        for f in fundstellen:
            c, alpha = _trefferfarbe(f, f is gewaehlt)
            for rect in f.rects:
                item = self._kasten(rect, c, 3 if f is gewaehlt else 1.5, alpha,
                                    gestrichelt=geraten(f),
                                    hinweis=f"{f.regel} → {f.pseudonym}")
                self._items[id(item)] = f

    def zeige_leer(self) -> None:
        """Clear the view (no document chosen)."""
        self._szene.clear()
        self._items.clear()

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Ctrl (Cmd) + wheel zooms, plain wheel scrolls."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            faktor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(faktor, faktor)
            self._zoom_melden()
            return
        super().wheelEvent(event)

    def zoom(self, faktor: float) -> None:
        """Zoom by the factor (the zoom buttons) and tell a linked view."""
        self.scale(faktor, faktor)
        self._zoom_melden()

    def zoom_zuruecksetzen(self) -> None:
        """Back to the base scale and tell a linked view."""
        self.resetTransform()
        self.scale(self.basis_skala, self.basis_skala)
        self._zoom_melden()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & (Qt.KeyboardModifier.ShiftModifier
                                | Qt.KeyboardModifier.AltModifier) \
                or event.button() == Qt.MouseButton.MiddleButton:
            # Shift or Option/Alt (or the middle button) + drag moves the view
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            if event.button() == Qt.MouseButton.MiddleButton:
                event = _als_linksklick(event)
            super().mousePressEvent(event)
            return
        pos = self.mapToScene(event.position().toPoint())
        for item in self._szene.items(pos):
            f = self._items.get(id(item))
            if f is not None:
                self.treffer_gewaehlt.emit(f)
                return
        self._erweitern = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._start = pos
        self._gummi = QGraphicsRectItem(QRectF(pos, pos))
        farbe_ = QColor(120, 120, 120) if self.loeschmodus else (
            SCHWARZ if self.masken_modus else ROT)
        self._gummi.setPen(QPen(farbe_, 1.5, Qt.PenStyle.SolidLine if self.masken_modus
                                else Qt.PenStyle.DashLine))
        self._szene.addItem(self._gummi)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseMoveEvent(event)
            return
        if self._start is not None and self._gummi is not None:
            pos = self.mapToScene(event.position().toPoint())
            self._gummi.setRect(QRectF(self._start, pos).normalized())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            return
        if self._start is None or self._gummi is None:
            return
        r = self._gummi.rect()
        self._szene.removeItem(self._gummi)
        self._start, self._gummi = None, None
        if r.width() > 4 and r.height() > 4:
            self.kasten_gezeichnet.emit((r.left() / self.skala, r.top() / self.skala,
                                         r.right() / self.skala, r.bottom() / self.skala))
        else:  # a plain click: the word under it (Ctrl/Cmd extends the selection)
            self.punkt_gewaehlt.emit(r.left() / self.skala, r.top() / self.skala,
                                     self._erweitern)


def _als_linksklick(event):
    """The middle button as a left button, so that Qt's hand-drag accepts it."""
    from PySide6.QtGui import QMouseEvent

    return QMouseEvent(event.type(), event.position(), event.globalPosition(),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, event.modifiers())


class Textansicht(QTextEdit):
    """A text file with a coloured span per hit; a click on a span selects the hit,
    a text selection can be marked by hand."""

    treffer_gewaehlt = Signal(object)

    def __init__(self, eltern: QWidget | None = None) -> None:
        """A read-only editor; the font is remembered so that the zoom can be reset."""
        super().__init__(eltern)
        self.setReadOnly(True)
        self._treffer: list[Fundstelle] = []
        self._text: str | None = None
        self._schrift = self.font()

    def zeige(self, text: str, fundstellen: list[Fundstelle], gewaehlt: Fundstelle | None,
              fachspannen: Sequence[tuple[int, int]] = (),
              verworfen: Sequence[Fundstelle] = ()) -> None:
        """Show the text with a background span per medical term, discarded hit and hit,
        and scroll to the selected hit.  Empty spans are neither shown nor clickable."""
        self._setze_text(text)
        aktiv = [f for f in fundstellen if f.ende > f.start]
        bleibt = [f for f in verworfen if f.ende > f.start]
        self._treffer = aktiv + bleibt
        self.setExtraSelections(self._hervorhebungen(fachspannen, bleibt, aktiv, gewaehlt))
        self._springe_zu(gewaehlt, text)

    def _setze_text(self, text: str) -> None:
        """Load the text only when it changed, so that a redraw keeps scroll position and
        selection."""
        if text != self._text:
            self._text = text
            self.setPlainText(text)

    def _hervorhebungen(self, fachspannen: Sequence[tuple[int, int]],
                        bleibt: Sequence[Fundstelle], aktiv: Sequence[Fundstelle],
                        gewaehlt: Fundstelle | None) -> list[QTextEdit.ExtraSelection]:
        """One background span each: green for medical terms and discarded hits, the
        traffic-light colour for hits; the selected hit is more opaque."""
        auswahlen = [self._auswahl(a, b, GRUEN, 45) for a, b in fachspannen]
        auswahlen += [self._auswahl(f.start, f.ende, GRUEN, 150 if f is gewaehlt else 70)
                      for f in bleibt]
        auswahlen += [self._auswahl(f.start, f.ende, farbe(f), 170 if f is gewaehlt else 80)
                      for f in aktiv]
        return auswahlen

    def _springe_zu(self, gewaehlt: Fundstelle | None, text: str) -> None:
        """Put the cursor at the start of the selected hit and scroll it into view."""
        if gewaehlt is None or gewaehlt.ende <= gewaehlt.start:
            return
        cursor = self.textCursor()
        cursor.setPosition(min(gewaehlt.start, len(text)))
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _auswahl(self, a: int, b: int, c: QColor, alpha: int) -> QTextEdit.ExtraSelection:
        """A background span over ``[a, b)`` in the colour with the given opacity."""
        s = QTextEdit.ExtraSelection()
        cursor = QTextCursor(self.document())
        cursor.setPosition(a)
        cursor.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
        s.cursor = cursor
        fmt = QTextCharFormat()
        hell = QColor(c)
        hell.setAlpha(alpha)
        fmt.setBackground(QBrush(hell))
        s.format = fmt
        return s

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        pos = self.cursorForPosition(event.position().toPoint()).position()
        for f in self._treffer:
            if f.start <= pos < f.ende:
                self.treffer_gewaehlt.emit(f)
                return

    def auswahl(self) -> tuple[int, int] | None:
        """Offsets of the current selection, or None."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        return cursor.selectionStart(), cursor.selectionEnd()

    def zoom(self, faktor: float) -> None:
        """One font step larger for a factor above 1, else one smaller (the zoom buttons)."""
        if faktor > 1:
            self.zoomIn(1)
        else:
            self.zoomOut(1)

    def zoom_zuruecksetzen(self) -> None:
        """Back to the font the view started with."""
        self.setFont(self._schrift)
