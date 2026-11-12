"""Step 3: look at every page, drop false hits, draw boxes over what was missed."""

from __future__ import annotations

from pathlib import Path

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
    QCheckBox,
    QFileDialog,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed import arbeitsstand as standmodul
from pseudonymed.gui.sitzung import Dokument, Sitzung
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
    return GELB if f.regel == "ocr.unlesbar" else ROT


def geraten(f: Fundstelle) -> bool:
    """A hit from a heuristic rather than a rule or the user."""
    return f.regel.startswith(("ner.", "unscharf.", "identifikator."))


def _rect(rect: tuple[float, float, float, float], skala: float) -> QRectF:
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
        super().__init__(eltern)
        self._szene = QGraphicsScene(self)
        self.setScene(self._szene)
        self.setRenderHints(self.renderHints())
        self.skala = VORSCHAU_DPI / 72.0
        self._items: dict[int, Fundstelle] = {}
        self._start: QPointF | None = None
        self._gummi: QGraphicsRectItem | None = None
        self.masken_modus = False  # drag draws a mask instead of a labelled box
        self.loeschmodus = False  # the mask erases (white) rather than redacts (black)
        self._folgt = False  # while following another view
        self.basis_skala = 1.0  # extra scale so that a larger image shows at the same size
        self.horizontalScrollBar().valueChanged.connect(self._scroll_melden)
        self.verticalScrollBar().valueChanged.connect(self._scroll_melden)

    # -- linking two views -------------------------------------------------------------------------
    def _relativ(self, leiste) -> float:
        return leiste.value() / leiste.maximum() if leiste.maximum() > 0 else 0.0

    def _scroll_melden(self, _wert: int) -> None:
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
        return self.transform().m11() / self.basis_skala

    def setze_basis(self, basis: float) -> None:
        """Change the base scale, keeping the zoom factor the user chose."""
        faktor = self.zoomfaktor
        self.basis_skala = basis
        self.folge_zoom(faktor)

    def _zoom_melden(self) -> None:
        if not self._folgt:
            self.gezoomt.emit(self.zoomfaktor)

    @staticmethod
    def koppeln(a: Seitenansicht, b: Seitenansicht) -> None:
        """Keep two views congruent: pan one, the other follows; zoom too."""
        a.gescrollt.connect(b.folge_scroll)
        b.gescrollt.connect(a.folge_scroll)
        a.gezoomt.connect(b.folge_zoom)
        b.gezoomt.connect(a.folge_zoom)

    def _kasten(self, rect: tuple[float, float, float, float], farbe_: QColor, breite: float,
                alpha: int, gestrichelt: bool = False, hinweis: str = "") -> QGraphicsRectItem:
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
              fachrects: list[tuple[float, float, float, float]] = (),
              verworfen: list[Fundstelle] = (),
              unsicher: list[tuple[float, float, float, float]] = (),
              auswahl: list[tuple[float, float, float, float]] = ()) -> None:
        self._szene.clear()
        self._items.clear()
        bild = QImage.fromData(png)
        self._szene.addItem(QGraphicsPixmapItem(QPixmap.fromImage(bild)))
        self._szene.setSceneRect(0, 0, bild.width(), bild.height())
        for rect in fachrects:  # protected medical terms: stay, click to select the word
            self._kasten(rect, GRUEN, 1, 28, hinweis=self.tr("Fachbegriff (bleibt)"))
        for rect in auswahl:  # words picked by the user, not yet in the list
            self._kasten(rect, BLAU, 2, 50, hinweis=self.tr("Auswahl (aufnehmen?)"))
        for rect in unsicher:  # badly read words: look, not hits
            self._kasten(rect, GELB, 1, 0, hinweis=self.tr("unsicher gelesen"))
        for f in verworfen:  # discarded hits: stay, click to take back
            for rect in f.rects:
                item = self._kasten(rect, GRUEN, 3 if f is gewaehlt else 1.5,
                                    60 if f is gewaehlt else 30, gestrichelt=True,
                                    hinweis=self.tr("verworfen, bleibt: {regel}").format(
                                        regel=f.regel))
                self._items[id(item)] = f
        for f in fundstellen:
            c = farbe(f)
            if f.regel == "maske":
                c, alpha = SCHWARZ, 160
            else:
                alpha = 90 if f is gewaehlt else 45
            for rect in f.rects:
                item = self._kasten(rect, c, 3 if f is gewaehlt else 1.5, alpha,
                                    gestrichelt=geraten(f),
                                    hinweis=f"{f.regel} → {f.pseudonym}")
                self._items[id(item)] = f

    def zeige_leer(self) -> None:
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
        self.scale(faktor, faktor)
        self._zoom_melden()

    def zoom_zuruecksetzen(self) -> None:
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
                                     getattr(self, "_erweitern", False))


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
        super().__init__(eltern)
        self.setReadOnly(True)
        self._treffer: list[Fundstelle] = []
        self._text: str | None = None
        self._schrift = self.font()

    def zeige(self, text: str, fundstellen: list[Fundstelle], gewaehlt: Fundstelle | None,
              fachspannen: list[tuple[int, int]] = (), verworfen: list[Fundstelle] = ()) -> None:
        if text != self._text:
            self._text = text
            self.setPlainText(text)
        aktiv = [f for f in fundstellen if f.ende > f.start]
        bleibt = [f for f in verworfen if f.ende > f.start]
        self._treffer = aktiv + bleibt
        auswahlen = [self._auswahl(a, b, GRUEN, 45) for a, b in fachspannen]
        for f in bleibt:
            auswahlen.append(self._auswahl(f.start, f.ende, GRUEN, 150 if f is gewaehlt else 70))
        for f in aktiv:
            auswahlen.append(self._auswahl(f.start, f.ende, farbe(f),
                                           170 if f is gewaehlt else 80))
        self.setExtraSelections(auswahlen)
        if gewaehlt is not None and gewaehlt.ende > gewaehlt.start:
            cursor = self.textCursor()
            cursor.setPosition(min(gewaehlt.start, len(text)))
            self.setTextCursor(cursor)
            self.ensureCursorVisible()

    def _auswahl(self, a: int, b: int, c: QColor, alpha: int) -> QTextEdit.ExtraSelection:
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
        if faktor > 1:
            self.zoomIn(1)
        else:
            self.zoomOut(1)

    def zoom_zuruecksetzen(self) -> None:
        self.setFont(self._schrift)


class PruefenSeite(QWidget):
    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.dokument: Dokument | None = None
        self.seite = 0
        self.gewaehlt: Fundstelle | None = None
        self.auswahl: tuple[int, int] | None = None  # offsets of words picked on the page
        self.synchron = False

        aussen = QVBoxLayout(self)
        teiler = QSplitter(Qt.Orientation.Horizontal)
        self.baum = QTreeWidget()
        self.baum.setHeaderLabels([self.tr("Dokument / Seite")])
        self.baum.currentItemChanged.connect(self._seite_gewaehlt)
        teiler.addWidget(self.baum)

        self.ansicht = Seitenansicht()
        self.ansicht.treffer_gewaehlt.connect(self.waehle)
        self.ansicht.kasten_gezeichnet.connect(self.kasten)
        self.ansicht.punkt_gewaehlt.connect(self.punkt)
        self.textansicht = Textansicht()
        self.textansicht.treffer_gewaehlt.connect(self.waehle)
        self.stapel = QStackedWidget()
        self.stapel.addWidget(self.ansicht)
        self.stapel.addWidget(self.textansicht)
        teiler.addWidget(self.stapel)

        rechts = QWidget()
        r = QVBoxLayout(rechts)
        legende = QLabel(self.tr(
            "Rot: wird geschwärzt (gestrichelt: Vorschlag von NER oder unscharfer Suche) ·"
            " Gelb: unsicher gelesen (unlesbare Zeilen werden vorsorglich geschwärzt) ·"
            " Grün: bleibt stehen (Fachbegriff, verworfener Treffer; Klick stellt ihn wieder"
            " her). Klick auf ein Wort wählt es (blau), Strg/Cmd + Klick erweitert die Auswahl"
            " bis dorthin, „Auswahl aufnehmen“ macht daraus einen Treffer; so kommen auch"
            " Fachbegriffe oder Wörter in die Liste, die die Analyse nicht gefunden hat."
            " Ziehen zeichnet einen Kasten mit dem Ersatztext aus dem Feld, mit „Maske"
            " zeichnen“ eine schwarze Maske; Umschalt + Ziehen verschiebt den Ausschnitt,"
            " Strg/Cmd + Rad zoomt. In Textdateien nimmt „Auswahl aufnehmen“ den"
            " ausgewählten Text auf. Die Quelle jedes Treffers steht in der Tabelle."))
        legende.setWordWrap(True)
        r.addWidget(legende)
        self.tabelle = QTableWidget(0, 4)
        self.tabelle.setHorizontalHeaderLabels([self.tr("Seite"), self.tr("Regel"),
                                                self.tr("Pseudonym"), self.tr("Quelle")])
        self.tabelle.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabelle.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabelle.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabelle.itemSelectionChanged.connect(self._zeile_gewaehlt)
        r.addWidget(self.tabelle, 1)
        self.fach_anzeigen = QCheckBox(self.tr("Fachbegriffe markieren"))
        self.fach_anzeigen.setChecked(True)
        self.fach_anzeigen.toggled.connect(lambda _an: self.zeichne())
        r.addWidget(self.fach_anzeigen)
        self.merken = QCheckBox(self.tr(
            "Verworfene Treffer für diesen Patienten merken (beim Export gespeichert)"))
        self.merken.setChecked(True)
        self.sitzung.optionen.verworfene_merken = True
        self.merken.toggled.connect(self._merken_uebernehmen)
        r.addWidget(self.merken)
        ersatz = QHBoxLayout()
        ersatz.addWidget(QLabel(self.tr("Ersatztext für Kästen und Aufnahmen")))
        self.ersatztext = QLineEdit(self.tr("[geschwärzt]"))
        self.ersatztext.setToolTip(self.tr(
            "Für Kästen der Text, der hineingeschrieben wird. Für aufgenommene Wörter gilt:"
            " bleibt hier „[geschwärzt]“ stehen, bekommt das Wort einen eigenen Code"
            " ([Begriff …]), den die Reidentifikation zurücksetzt."))
        ersatz.addWidget(self.ersatztext, 1)
        r.addLayout(ersatz)
        knoepfe = QHBoxLayout()
        self.verwerfen = QPushButton(self.tr("Treffer verwerfen"))
        self.verwerfen.clicked.connect(self.verwerfe)
        knoepfe.addWidget(self.verwerfen)
        self.markieren = QPushButton(self.tr("Auswahl aufnehmen"))
        self.markieren.setToolTip(self.tr(
            "Die gewählten Wörter (Seite) oder den markierten Text (Textdatei) in die"
            " Trefferliste aufnehmen."))
        self.markieren.clicked.connect(lambda: self.aufnehmen())
        knoepfe.addWidget(self.markieren)
        self.maske_modus = QPushButton(self.tr("Maske zeichnen"))
        self.maske_modus.setCheckable(True)
        self.maske_modus.setToolTip(self.tr(
            "Eingeschaltet: Ziehen schwärzt den Bereich schwarz, ohne Ersatztext;"
            " Text und Bild darunter verschwinden."))
        self.maske_modus.toggled.connect(self._maske_umschalten)
        knoepfe.addWidget(self.maske_modus)
        for text, aktion in (("+", lambda: self._aktive_ansicht().zoom(1.25)),
                             ("−", lambda: self._aktive_ansicht().zoom(0.8)),
                             ("1:1", lambda: self._aktive_ansicht().zoom_zuruecksetzen())):
            k = QPushButton(text)
            k.setMaximumWidth(40)
            k.clicked.connect(aktion)
            knoepfe.addWidget(k)
        r.addLayout(knoepfe)
        self.hinweis = QLabel("")
        self.hinweis.setWordWrap(True)
        r.addWidget(self.hinweis)
        self.speichern = QPushButton(self.tr("Arbeitsstand speichern …"))
        self.speichern.setToolTip(self.tr(
            "Das aufbereitete Dokument und eine Konfigurationsdatei schreiben, aus denen sich"
            " die Sichtprüfung später wieder aufnehmen und abwandeln lässt (Aufbereitung,"
            " Texterkennung, Treffer, Entscheidungen). Beide Dateien enthalten Klardaten."))
        self.speichern.clicked.connect(self.arbeitsstand_waehlen)
        r.addWidget(self.speichern)
        teiler.addWidget(rechts)
        teiler.setSizes([180, 620, 360])
        aussen.addWidget(teiler, 1)

    # -- navigation -------------------------------------------------------------------------------
    def aktualisiere(self) -> None:
        self.baum.clear()
        for d in self.sitzung.dokumente:
            if d.vorbereitung is None or d.vorbereitung.abgeschlossen:
                continue
            knoten = QTreeWidgetItem([d.pfad.name])  # file name, not translated
            knoten.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
            for s in d.vorbereitung.seiten:
                if d.vorbereitung.ist_text:
                    kind = QTreeWidgetItem([self.tr("Text")])
                else:
                    kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=s.nummer + 1)
                                            + (" (OCR)" if s.ocr is not None else "")])
                kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), s.nummer))
                knoten.addChild(kind)
            self.baum.addTopLevelItem(knoten)
            knoten.setExpanded(True)
        if self.baum.topLevelItemCount():
            erster = self.baum.topLevelItem(0)
            self.baum.setCurrentItem(erster.child(0) if erster.childCount() else erster)

    def _seite_gewaehlt(self, aktuell: QTreeWidgetItem | None, _vorher=None) -> None:
        if aktuell is None:
            return
        kennung, nummer = aktuell.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.sitzung.dokumente if id(d) == kennung), None)
        if d is None or d.vorbereitung is None:
            return
        self.dokument = d
        self.seite = max(nummer, 0)
        self.gewaehlt = None
        self.auswahl = None
        self.zeichne()

    def fundstellen(self) -> list[Fundstelle]:
        if self.dokument is None or self.dokument.vorbereitung is None:
            return []
        return [f for f in self.dokument.vorbereitung.fundstellen if f.seite == self.seite]

    def _aktive_ansicht(self):
        return self.stapel.currentWidget()

    def _zeige_ansicht(self, treffer: list[Fundstelle]) -> None:
        v = self.dokument.vorbereitung
        zeigen = self.fach_anzeigen.isChecked()
        verworfen = [f for f in v.verworfen if f.seite == self.seite]
        if v.ist_text:
            fach = v.fachbegriffe_spannen(self.seite) if zeigen else []
            self.textansicht.zeige(v.text, treffer, self.gewaehlt, fach, verworfen)
            self.stapel.setCurrentWidget(self.textansicht)
        else:
            fach = v.fachbegriffe_rects(self.seite) if zeigen else []
            auswahl = ([tuple(r) for r in v.seiten[self.seite].textbasis.rects(*self.auswahl)]
                       if self.auswahl is not None else [])
            self.ansicht.zeige(v.seitenbild(self.seite, VORSCHAU_DPI), treffer, self.gewaehlt,
                               fach, verworfen, v.unsichere_rects(self.seite), auswahl)
            self.stapel.setCurrentWidget(self.ansicht)
        self.markieren.setEnabled(v.ist_text or self.auswahl is not None)
        self.maske_modus.setVisible(not v.ist_text)
        verworfen_gewaehlt = self.gewaehlt is not None and self.gewaehlt in v.verworfen
        self.verwerfen.setText(self.tr("Treffer wiederherstellen") if verworfen_gewaehlt
                               else self.tr("Treffer verwerfen"))

    def zeichne(self) -> None:
        if self.dokument is None or self.dokument.vorbereitung is None:
            return
        v = self.dokument.vorbereitung
        self._zeige_ansicht(self.fundstellen())
        self.tabelle.blockSignals(True)
        self.tabelle.setRowCount(0)
        for f in self.fundstellen():
            zeile = self.tabelle.rowCount()
            self.tabelle.insertRow(zeile)
            for spalte, wert in enumerate((str(f.seite + 1), f.regel, f.pseudonym, f.quelle)):
                self.tabelle.setItem(zeile, spalte, QTableWidgetItem(wert))
            if f is self.gewaehlt:
                self.tabelle.selectRow(zeile)
        self.tabelle.blockSignals(False)
        warnungen = [w for w in v.warnungen + v.hinweise
                     if "OCR" in w or "Textebene" in w or w.startswith("Unscharfe Suche")]
        hinweis = self.tr("{n} Fachbegriffe markiert").format(n=v.fachbegriffe_anzahl)
        self.hinweis.setText(" · ".join([hinweis] + warnungen[:1]))

    # -- actions ---------------------------------------------------------------------------------
    def waehle(self, f: Fundstelle) -> None:
        self.gewaehlt = f
        self.zeichne()

    def _zeile_gewaehlt(self) -> None:
        zeilen = self.tabelle.selectionModel().selectedRows()
        if zeilen:
            treffer = self.fundstellen()
            i = zeilen[0].row()
            if 0 <= i < len(treffer):
                self.gewaehlt = treffer[i]
                self._zeige_ansicht(treffer)

    def verwerfe(self) -> None:
        """Discard the selected hit, or take a discarded one back."""
        if self.gewaehlt is None or self.dokument is None or self.dokument.vorbereitung is None:
            return
        v = self.dokument.vorbereitung
        if self.gewaehlt in v.verworfen:
            v.wiederherstelle(self.gewaehlt)
        else:
            v.verwerfe(self.gewaehlt)
        self.gewaehlt = None
        self.zeichne()

    def _maske_umschalten(self, an: bool) -> None:
        self.ansicht.masken_modus = an

    def _merken_uebernehmen(self, an: bool) -> None:
        self.sitzung.optionen.verworfene_merken = an

    def kasten(self, rect: tuple[float, float, float, float], pseudonym: str | None = None) -> None:
        """A drawn rectangle: a black mask in mask mode, otherwise a box with the
        replacement text from the field (no dialog, so nothing can get lost behind
        the window).  The new hit is selected; errors are shown in the hint line."""
        if self.dokument is None or self.dokument.vorbereitung is None:
            return
        v = self.dokument.vorbereitung
        try:
            if pseudonym is None and self.maske_modus.isChecked():
                neue = v.maske(self.seite, rect)
            else:
                text = pseudonym if pseudonym is not None else self.ersatztext.text().strip()
                neue = v.kasten(self.seite, rect, text or self.tr("[geschwärzt]"))
        except Exception as e:  # noqa: BLE001 - must be visible, never silent
            self.hinweis.setText(self.tr("Kasten nicht gesetzt: {fehler}").format(fehler=e))
            return
        self.gewaehlt = neue[0] if neue else None
        self.zeichne()
        if neue:
            self.hinweis.setText(self.tr("{n} Kasten gesetzt: {pseudonym}").format(
                n=len(neue), pseudonym=neue[0].pseudonym) + " · " + self.hinweis.text())

    def punkt(self, x: float, y: float, erweitern: bool = False) -> None:
        """A click on the page: select the word there, or extend the selection to it."""
        if self.dokument is None or self.dokument.vorbereitung is None \
                or self.dokument.vorbereitung.ist_text:
            return
        spanne = self.dokument.vorbereitung.wort_bei(self.seite, x, y)
        if spanne is None:
            self.auswahl = None
        elif erweitern and self.auswahl is not None:
            self.auswahl = (min(self.auswahl[0], spanne[0]), max(self.auswahl[1], spanne[1]))
        else:
            self.auswahl = spanne
        self.gewaehlt = None
        self.zeichne()

    def _ersatz(self, pseudonym: str | None) -> str | None:
        """The replacement for a take-up: the argument, the field, or None for a code
        when the field still holds the default."""
        if pseudonym is not None:
            return pseudonym
        text = self.ersatztext.text().strip()
        return None if text in ("", "[geschwärzt]", self.tr("[geschwärzt]")) else text

    def aufnehmen(self, pseudonym: str | None = None) -> None:
        """Take the picked words (page) or the selected text (text file) into the list."""
        if self.dokument is None or self.dokument.vorbereitung is None:
            return
        v = self.dokument.vorbereitung
        spanne = self.textansicht.auswahl() if v.ist_text else self.auswahl
        if spanne is None:
            if self.synchron:
                raise ValueError("Keine Auswahl.")
            self.hinweis.setText(self.tr("Erst ein Wort anklicken oder Text markieren."))
            return
        try:
            f = v.aufnehmen(self.seite, spanne[0], spanne[1], self._ersatz(pseudonym))
        except ValueError as e:
            self.hinweis.setText(str(e))
            return
        self.auswahl = None
        self.gewaehlt = f
        self.zeichne()
        self.hinweis.setText(self.tr("Aufgenommen: {pseudonym}").format(pseudonym=f.pseudonym)
                             + " · " + self.hinweis.text())

    def markiere_auswahl(self, pseudonym: str | None = None) -> None:
        """Text files: the selected text becomes a manual hit with the given replacement
        (kept for scripts; the button calls :meth:`aufnehmen`)."""
        if self.dokument is None or self.dokument.vorbereitung is None \
                or not self.dokument.vorbereitung.ist_text:
            return
        spanne = self.textansicht.auswahl()
        if spanne is None:
            if self.synchron:
                raise ValueError("Keine Auswahl.")
            return
        self.dokument.vorbereitung.markiere(self.seite, spanne[0], spanne[1],
                                            pseudonym or self.tr("[geschwärzt]"))
        self.gewaehlt = None
        self.zeichne()

    # -- working state -----------------------------------------------------------------------------
    def arbeitsstand_waehlen(self) -> None:
        if self.dokument is None or self.dokument.vorbereitung is None:
            return
        v = self.dokument.vorbereitung
        vorschlag = v.quelle.with_name(v.quelle.stem + standmodul.ENDUNG)
        pfad, _ = QFileDialog.getSaveFileName(
            self, self.tr("Arbeitsstand speichern"), str(vorschlag),
            self.tr("Arbeitsstand (*.arbeitsstand.json)"))
        if pfad:
            self.arbeitsstand_speichern(Path(pfad))

    def arbeitsstand_speichern(self, pfad: Path) -> tuple[Path, Path] | None:
        """Write document and configuration; returns the two paths."""
        if self.dokument is None or self.dokument.vorbereitung is None:
            return None
        try:
            dokument, konfiguration = standmodul.speichere(self.dokument.vorbereitung, pfad)
        except (standmodul.ArbeitsstandFehler, OSError) as e:
            if self.synchron:
                raise
            QMessageBox.warning(self, self.tr("Arbeitsstand"), str(e))
            return None
        self.hinweis.setText(self.tr(
            "Arbeitsstand gespeichert: {dokument} und {konfiguration} (beide mit Klardaten,"
            " wie das Original aufbewahren).").format(dokument=dokument.name,
                                                     konfiguration=konfiguration.name))
        return dokument, konfiguration
