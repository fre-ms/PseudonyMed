"""Step 3: clean up scans and photos before OCR, checked by eye.

Only pages without a text layer appear here.  Each has an automatic suggestion
(tilt, sheet corners, resolution, background); the user switches steps on or
off, rotates, draws black masks on the result, compares before and after, and
then starts the analysis.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.gui.ansichten import VORSCHAU_DPI, Seitenansicht
from pseudonymed.gui.sitzung import Dokument, Sitzung


class AufbereitenSeite(QWidget):
    """The clean-up panel inside the documents step."""

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.dokument: Dokument | None = None
        self.seite = 0
        self._stumm = False  # while the form is being filled from the data
        aussen = QVBoxLayout(self)
        teiler = QSplitter(Qt.Orientation.Horizontal)
        self._baue_baum(teiler)
        self._baue_bilder(teiler)
        teiler.addWidget(self._baue_steuerung())
        teiler.setSizes([180, 760, 300])
        aussen.addWidget(teiler, 1)
        if not aufmodul.verfuegbar():
            self.hinweis.setText(self.tr(
                "Bildaufbereitung braucht OpenCV und NumPy (Extra 'ocr'); hier sind nur"
                " Masken möglich."))

    # -- building the page -------------------------------------------------------------------------
    def _baue_baum(self, teiler: QSplitter) -> None:
        """The tree of documents and their pages without a text layer, left of the views."""
        self.baum = QTreeWidget()
        self.baum.setHeaderLabels([self.tr("Dokument / Seite")])
        self.baum.currentItemChanged.connect(self._seite_gewaehlt)
        teiler.addWidget(self.baum)

    def _baue_bilder(self, teiler: QSplitter) -> None:
        """The before and after views side by side, linked in scroll and zoom.  Masks are
        drawn on the result only; Cmd/Ctrl+Z takes the last one back."""
        self.bilder = QSplitter(Qt.Orientation.Horizontal)  # the user moves the divider
        links_w = QWidget()
        links = QVBoxLayout(links_w)
        links.setContentsMargins(0, 0, 0, 0)
        vorher_titel = QLabel(self.tr("Vorher"))
        vorher_titel.setWordWrap(True)
        links.addWidget(vorher_titel)
        self.vorher = Seitenansicht()
        links.addWidget(self.vorher, 1)
        rechts_w = QWidget()
        rechts_bild = QVBoxLayout(rechts_w)
        rechts_bild.setContentsMargins(0, 0, 0, 0)
        nachher_titel = QLabel(self.tr("Nachher (so sieht es die OCR; Ziehen löscht den"
                                       " Bereich weiß, Cmd/Strg+Z holt ihn zurück;"
                                       " Umschalt/Option + Ziehen verschiebt beide Ansichten)"))
        nachher_titel.setWordWrap(True)
        rechts_bild.addWidget(nachher_titel)
        self.nachher = Seitenansicht()
        self.nachher.masken_modus = True
        self.nachher.loeschmodus = True
        self.nachher.kasten_gezeichnet.connect(self.maske)
        rechts_bild.addWidget(self.nachher, 1)
        self.vorher.masken_modus = True  # drag on the left: nothing drawn, pan with modifier
        self.vorher.kasten_gezeichnet.connect(lambda _r: None)
        Seitenansicht.koppeln(self.vorher, self.nachher)
        self.bilder.addWidget(links_w)
        self.bilder.addWidget(rechts_w)
        self.bilder.setSizes([400, 400])
        teiler.addWidget(self.bilder)
        rueckgaengig = QShortcut(QKeySequence.StandardKey.Undo, self)
        rueckgaengig.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        rueckgaengig.activated.connect(self.rueckgaengig)

    def _baue_steuerung(self) -> QWidget:
        """The column of controls on the right: note, the two step groups, the page
        buttons and the zoom buttons."""
        rechts = QWidget()
        r = QVBoxLayout(rechts)
        self.hinweis = QLabel("")
        self.hinweis.setWordWrap(True)
        r.addWidget(self.hinweis)
        schritte, form = self._baue_schritte()
        weitere = self._baue_weitere_schritte()
        self._baue_drehknoepfe(form)
        self._baue_maskenknoepfe(form)
        r.addWidget(schritte)
        r.addWidget(weitere)
        self._verbinde_formular()
        r.addLayout(self._baue_seitenknoepfe())
        r.addStretch(1)
        r.addLayout(self._baue_zoomknoepfe())
        return rechts

    def _baue_schritte(self) -> tuple[QGroupBox, QFormLayout]:
        """The group "steps for this page": tilt, sheet corners, upscaling, letter mode,
        background and black-and-white.  The form comes back as well, because the rotate
        and mask rows join it only after the sliders of the second group (the order in
        which widgets are created is the tab order)."""
        schritte = QGroupBox(self.tr("Schritte für diese Seite"))
        form = QFormLayout(schritte)
        self.begradigen = QCheckBox(self.tr("Schräglage begradigen"))
        self.winkel = QDoubleSpinBox()
        self.winkel.setRange(-15.0, 15.0)
        self.winkel.setSingleStep(0.1)
        self.winkel.setDecimals(1)
        self.winkel.setSuffix("°")
        zeile = QHBoxLayout()
        zeile.addWidget(self.begradigen, 1)
        zeile.addWidget(self.winkel)
        form.addRow(zeile)
        self.entzerren = QCheckBox(self.tr("Fotografiertes Blatt entzerren"))
        form.addRow(self.entzerren)
        self.hochskalieren = QComboBox()
        for faktor, text in ((1, self.tr("nicht")), (2, "2×"), (3, "3×"), (4, "4×")):
            self.hochskalieren.addItem(text, faktor)
        self.hochskalieren.setToolTip(self.tr(
            "Die OCR liest die Seite mit dem Vielfachen der Auflösung (bis 4×, Obergrenze"
            " 9000 Pixel); die Seite selbst wird nicht vergrößert gespeichert."))
        form.addRow(self.tr("Hochskalieren"), self.hochskalieren)
        self.textmodus = QCheckBox(self.tr("Buchstabenmodus: Lanczos-Vergrößerung, Striche"
                                           " geschärft"))
        self.textmodus.setToolTip(self.tr(
            "Vergrößert mit Lanczos und schärft die Striche in Graustufen. Eine harte"
            " Schwarz-Weiß-Schwelle verschlechterte die Erkennung in Messungen deutlich,"
            " deshalb bleibt das Bild grau. Ersetzt Hintergrund, Schärfen und Schwarz-Weiß."))
        form.addRow(self.textmodus)
        self.hintergrund = QCheckBox(self.tr("Hintergrund glätten (Schatten, graues Papier)"))
        form.addRow(self.hintergrund)
        self.schwarzweiss = QCheckBox(self.tr("Schwarz-Weiß"))
        form.addRow(self.schwarzweiss)
        return schritte, form

    def _baue_weitere_schritte(self) -> QGroupBox:
        """The group "further steps": edges, coloured ink, the three steps with a
        strength slider, lines, and the button that detects the orientation."""
        weitere = QGroupBox(self.tr("Weitere Schritte"))
        wf = QFormLayout(weitere)
        self.raender = QCheckBox(self.tr("Dunkle Ränder säubern"))
        self.farbe_entfernen = QCheckBox(self.tr("Farbige Tinte entfernen (Stempel, Marker)"))
        self.entrauschen = QCheckBox(self.tr("Entrauschen (Körnung, Staub)"))
        self.kontrast = QCheckBox(self.tr("Kontrast anheben (blasse Kopien)"))
        self.schaerfen = QCheckBox(self.tr("Schärfen"))
        self.linien_entfernen = QCheckBox(self.tr("Linien entfernen (Formulare, Tabellen)"))
        wf.addRow(self.raender)
        wf.addRow(self.farbe_entfernen)
        self.regler: dict[str, QSlider] = {}
        for name, kasten in (("entrauschen", self.entrauschen), ("kontrast", self.kontrast),
                             ("schaerfen", self.schaerfen)):
            regler = self._baue_regler(kasten)
            zeile_ = QHBoxLayout()
            zeile_.addWidget(kasten, 1)
            zeile_.addWidget(regler)
            wf.addRow(zeile_)
            self.regler[name] = regler
        wf.addRow(self.linien_entfernen)
        self.ausrichtung = QPushButton(self.tr("Ausrichtung erkennen"))
        self.ausrichtung.setToolTip(self.tr(
            "Hoch- oder Querformat aus den Textzeilen; ob der Text auf dem Kopf steht,"
            " entscheidet ein kurzer OCR-Vergleich (dauert einige Sekunden)."))
        self.ausrichtung.clicked.connect(self.ausrichtung_erkennen)
        wf.addRow(self.ausrichtung)
        return weitere

    def _baue_regler(self, kasten: QCheckBox) -> QSlider:
        """A strength slider 1 to 5 for the step of ``kasten``: enabled with the box,
        applied when the knob is released, not on every tick while it is dragged."""
        regler = QSlider(Qt.Orientation.Horizontal)
        regler.setRange(1, 5)
        regler.setValue(2)
        regler.setTickPosition(QSlider.TickPosition.TicksBelow)
        regler.setTickInterval(1)
        regler.setMaximumWidth(140)
        regler.setToolTip(self.tr("Stärke 1 (sanft) bis 5 (kräftig)"))
        regler.setEnabled(False)
        kasten.toggled.connect(regler.setEnabled)
        regler.valueChanged.connect(
            lambda _w: None if regler.isSliderDown() else self._formular_uebernehmen())
        regler.sliderReleased.connect(self._formular_uebernehmen)
        return regler

    def _baue_drehknoepfe(self, form: QFormLayout) -> None:
        """The row of rotate buttons in the steps group."""
        drehen = QHBoxLayout()
        for text, grad in ((self.tr("↺ 90°"), 270), (self.tr("↻ 90°"), 90),
                           (self.tr("180°"), 180)):
            k = QPushButton(text)
            k.setMinimumHeight(32)
            k.clicked.connect(lambda _c=False, g=grad: self.drehe(g))
            drehen.addWidget(k)
        form.addRow(self.tr("Drehen"), drehen)

    def _baue_maskenknoepfe(self, form: QFormLayout) -> None:
        """The row in the steps group that takes erased areas back: the last one or all."""
        masken_zeile = QHBoxLayout()
        self.letzte_zurueck = QPushButton(self.tr("Letzten Bereich zurückholen (Cmd/Strg+Z)"))
        self.letzte_zurueck.setMinimumHeight(32)
        self.letzte_zurueck.clicked.connect(self.rueckgaengig)
        self.masken_loeschen = QPushButton(self.tr("Alle zurückholen"))
        self.masken_loeschen.setMinimumHeight(32)
        self.masken_loeschen.clicked.connect(self.loesche_masken)
        masken_zeile.addWidget(self.letzte_zurueck, 1)
        masken_zeile.addWidget(self.masken_loeschen)
        form.addRow(masken_zeile)

    def _verbinde_formular(self) -> None:
        """Every switch, the upscaling factor and the angle write the form back into
        the plan of the page."""
        for w in (self.begradigen, self.entzerren, self.textmodus, self.hintergrund,
                  self.schwarzweiss, self.raender, self.farbe_entfernen, self.entrauschen,
                  self.kontrast, self.schaerfen, self.linien_entfernen):
            w.toggled.connect(lambda _an: self._formular_uebernehmen())
        self.hochskalieren.currentIndexChanged.connect(lambda _i: self._formular_uebernehmen())
        self.winkel.valueChanged.connect(lambda _w: self._formular_uebernehmen())

    def _baue_seitenknoepfe(self) -> QVBoxLayout:
        """The buttons that act on the plan as a whole: restore the suggestion, switch
        the page off, copy the steps to every page."""
        knoepfe = QVBoxLayout()
        self.vorschlag = QPushButton(self.tr("Vorschlag wiederherstellen"))
        self.vorschlag.clicked.connect(self.vorschlag_wiederherstellen)
        self.nichts = QPushButton(self.tr("Diese Seite nicht aufbereiten"))
        self.nichts.clicked.connect(self.nichts_aufbereiten)
        self.alle = QPushButton(self.tr("Schritte auf alle Seiten übertragen"))
        self.alle.clicked.connect(self.auf_alle_uebertragen)
        for k in (self.vorschlag, self.nichts, self.alle):
            knoepfe.addWidget(k)
        return knoepfe

    def _baue_zoomknoepfe(self) -> QHBoxLayout:
        """The zoom buttons that act on both views at once."""
        zoom = QHBoxLayout()
        for text, aktion in (("+", lambda: self._zoom(1.25)), ("−", lambda: self._zoom(0.8)),
                             ("1:1", self._zoom_zuruecksetzen)):
            k = QPushButton(text)
            k.setMaximumWidth(44)
            k.setMinimumHeight(32)
            k.clicked.connect(aktion)
            zoom.addWidget(k)
        zoom.addStretch(1)
        return zoom

    # -- navigation ------------------------------------------------------------------------------
    def aktualisiere(self) -> None:
        """Rebuild the tree of pages without a text layer and show the first of them.
        Without any, both views are emptied and the note says so."""
        self.baum.clear()
        for d in self.sitzung.dokumente:
            if d.vorlage is None or not d.vorlage.kandidaten:
                continue
            knoten = QTreeWidgetItem([d.pfad.name])
            knoten.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
            for n in d.vorlage.kandidaten:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=n + 1)])
                kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), n))
                knoten.addChild(kind)
            self.baum.addTopLevelItem(knoten)
            knoten.setExpanded(True)
        if self._waehle_erste_seite():
            return
        self.dokument = None
        self.vorher.zeige_leer()
        self.nachher.zeige_leer()
        self.hinweis.setText(self.tr(
            "Keine Seiten ohne Textebene: nichts aufzubereiten. Scans und Fotos erscheinen"
            " hier mit einem Vorschlag."))

    def _waehle_erste_seite(self) -> bool:
        """Make the first page of the first document current; False when the tree is empty."""
        erster = self.baum.topLevelItem(0)
        if erster is None:
            return False
        kind = erster.child(0)
        self.baum.setCurrentItem(erster if kind is None else kind)
        return True

    def _seite_gewaehlt(self, aktuell: QTreeWidgetItem | None,
                        _vorher: QTreeWidgetItem | None = None) -> None:
        """Show the page behind the current tree item: fill the form, draw both views."""
        if aktuell is None:
            return
        kennung, nummer = aktuell.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.sitzung.dokumente if id(d) == kennung), None)
        if d is None or d.vorlage is None:
            return
        self.dokument = d
        self.seite = max(nummer, 0)
        self._formular_fuellen()
        self.zeichne()

    # -- data <-> form -----------------------------------------------------------------------------
    def einstellung(self) -> aufmodul.Aufbereitung | None:
        """The plan of the current page, created from the suggestion on first access;
        None while no document is shown."""
        if self.dokument is None or self.dokument.vorlage is None:
            return None
        return self.dokument.aufbereitung.setdefault(
            self.seite, _kopie(self.dokument.vorlage.vorschlaege.get(self.seite)))

    def _formular_fuellen(self) -> None:
        """Copy the plan of the current page into the switches, muted so that nothing
        is written back while the form is being filled."""
        a = self.einstellung()
        if a is None:
            return
        self._stumm = True
        try:
            self.begradigen.setChecked(a.begradigen)
            self.winkel.setValue(a.winkel)
            self.entzerren.setChecked(a.entzerren)
            self.entzerren.setEnabled(bool(a.ecken))
            self.hochskalieren.setCurrentIndex(max(0, min(3, a.hochskalieren - 1)))
            self.textmodus.setChecked(a.textmodus)
            self.hintergrund.setChecked(a.hintergrund)
            self.schwarzweiss.setChecked(a.schwarzweiss)
            self.raender.setChecked(a.raender)
            self.farbe_entfernen.setChecked(a.farbe_entfernen)
            self.entrauschen.setChecked(a.entrauschen)
            self.kontrast.setChecked(a.kontrast)
            self.schaerfen.setChecked(a.schaerfen)
            self.linien_entfernen.setChecked(a.linien_entfernen)
            self.regler["entrauschen"].setValue(a.entrauschen_staerke)
            self.regler["kontrast"].setValue(a.kontrast_staerke)
            self.regler["schaerfen"].setValue(a.schaerfen_staerke)
        finally:
            self._stumm = False

    def _formular_uebernehmen(self) -> None:
        """Write the switches back into the plan of the current page and redraw."""
        if self._stumm:
            return
        a = self.einstellung()
        if a is None:
            return
        a.begradigen = self.begradigen.isChecked()
        a.winkel = self.winkel.value()
        a.entzerren = self.entzerren.isChecked() and bool(a.ecken)
        a.hochskalieren = int(self.hochskalieren.currentData() or 1)
        a.textmodus = self.textmodus.isChecked()
        a.hintergrund = self.hintergrund.isChecked()
        a.schwarzweiss = self.schwarzweiss.isChecked()
        a.raender = self.raender.isChecked()
        a.farbe_entfernen = self.farbe_entfernen.isChecked()
        a.entrauschen = self.entrauschen.isChecked()
        a.kontrast = self.kontrast.isChecked()
        a.schaerfen = self.schaerfen.isChecked()
        a.linien_entfernen = self.linien_entfernen.isChecked()
        a.entrauschen_staerke = self.regler["entrauschen"].value()
        a.kontrast_staerke = self.regler["kontrast"].value()
        a.schaerfen_staerke = self.regler["schaerfen"].value()
        self.zeichne()

    # -- actions -----------------------------------------------------------------------------------
    def drehe(self, grad: int) -> None:
        """Rotate the current page by ``grad`` degrees; masks drawn on the old
        orientation are dropped."""
        a = self.einstellung()
        if a is None:
            return
        a.drehung = (a.drehung + grad) % 360
        a.masken = []  # drawn on the old orientation
        self.zeichne()

    def maske(self, rect: tuple[float, float, float, float]) -> None:
        """Erase the area ``rect`` (points of the result page) on the current page."""
        a = self.einstellung()
        if a is None:
            return
        a.masken.append(rect)
        self.zeichne()

    def loesche_masken(self) -> None:
        """Take back every erased area of the current page."""
        a = self.einstellung()
        if a is not None:
            a.masken = []
            self.zeichne()

    def rueckgaengig(self) -> None:
        """Take back the last erased area of this page (Cmd/Ctrl+Z)."""
        a = self.einstellung()
        if a is not None and a.masken:
            a.masken.pop()
            self.zeichne()

    def ausrichtung_erkennen(self) -> None:
        """Rotate the page upright: portrait/landscape from the text lines, upside-down
        decided by a quick OCR comparison when an engine is available."""
        a = self.einstellung()
        if a is None or self.dokument is None or self.dokument.vorlage is None:
            return
        try:
            backend = ocrmodul.backend(self.sitzung.optionen.ocr)
        except Exception:  # noqa: BLE001 - no engine: lines only
            backend = None
        bild, _skala = aufmodul.rendere(self.dokument.vorlage.doc[self.seite], 150)
        try:
            grad = aufmodul.schaetze_ausrichtung(bild, backend)
        except Exception as e:  # noqa: BLE001
            self.hinweis.setText(self.tr("Ausrichtung nicht erkannt: {fehler}").format(fehler=e))
            return
        a.drehung = grad
        a.masken = []
        self.zeichne()

    def vorschlag_wiederherstellen(self) -> None:
        """Replace the plan of the current page by a fresh copy of the suggestion."""
        if self.dokument is None or self.dokument.vorlage is None:
            return
        self.dokument.aufbereitung[self.seite] = _kopie(
            self.dokument.vorlage.vorschlaege.get(self.seite))
        self._formular_fuellen()
        self.zeichne()

    def nichts_aufbereiten(self) -> None:
        """Switch every step off for the current page: the OCR reads it as it is."""
        if self.dokument is None:
            return
        self.dokument.aufbereitung[self.seite] = aufmodul.Aufbereitung()
        self._formular_fuellen()
        self.zeichne()

    def auf_alle_uebertragen(self) -> None:
        """Copy the switches (not the page's own tilt, corners or masks) to every
        candidate page of every document."""
        quelle = self.einstellung()
        if quelle is None:
            return
        for d in self.sitzung.dokumente:
            if d.vorlage is None:
                continue
            for n in d.vorlage.kandidaten:
                ziel = d.aufbereitung.setdefault(n, _kopie(d.vorlage.vorschlaege.get(n)))
                ziel.begradigen = quelle.begradigen and abs(ziel.winkel) >= 0.05
                ziel.entzerren = quelle.entzerren and bool(ziel.ecken)
                for feld in ("hochskalieren", "textmodus", "hintergrund", "schwarzweiss",
                             "raender", "farbe_entfernen", "entrauschen", "kontrast",
                             "schaerfen", "linien_entfernen"):
                    setattr(ziel, feld, getattr(quelle, feld))
        self.zeichne()

    # -- drawing -----------------------------------------------------------------------------------
    def zeichne(self) -> None:
        """Render the current page before and after the plan and describe the steps in
        the note; a failing step is reported there instead of crashing."""
        a = self.einstellung()
        if a is None or self.dokument is None or self.dokument.vorlage is None:
            return
        vorlage = self.dokument.vorlage
        self.vorher.skala = VORSCHAU_DPI / 72.0
        self.vorher.zeige(vorlage.seitenbild(self.seite, VORSCHAU_DPI), [], None)
        try:
            png, _skala = vorlage.vorschau(self.seite, a, VORSCHAU_DPI)
        except Exception as e:  # noqa: BLE001 - shown instead of crashing the step
            self.hinweis.setText(self.tr("Aufbereitung fehlgeschlagen: {fehler}").format(
                fehler=e))
            return
        bild = QImage.fromData(png)
        self.nachher.skala = bild.width() / vorlage.seitenbreite(self.seite, a)
        # an enlarged image is shown at the size of the original; zooming shows the gain
        self.nachher.setze_basis((VORSCHAU_DPI / 72.0) / self.nachher.skala)
        self.nachher.zeige(png, [], None)
        text = aufmodul.beschreibung(a)
        if a.ecken is None and not vorlage.foto:
            text += " · " + self.tr("kein fotografiertes Blatt erkannt")
        self.hinweis.setText(text)

    def _zoom(self, faktor: float) -> None:
        """Zoom both views by ``faktor``."""
        self.vorher.zoom(faktor)
        self.nachher.zoom(faktor)

    def _zoom_zuruecksetzen(self) -> None:
        """Show both views at 1:1 again."""
        self.vorher.zoom_zuruecksetzen()
        self.nachher.zoom_zuruecksetzen()


def _kopie(a: aufmodul.Aufbereitung | None) -> aufmodul.Aufbereitung:
    """An independent copy of a plan (own mask and corner lists); an empty plan for None."""
    if a is None:
        return aufmodul.Aufbereitung()
    return aufmodul.Aufbereitung(**{**a.__dict__, "masken": list(a.masken),
                                    "ecken": list(a.ecken) if a.ecken else None})
