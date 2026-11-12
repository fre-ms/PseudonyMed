"""Step 7: look at every page, drop false hits, draw boxes over what was missed."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed import arbeitsstand as standmodul
from pseudonymed.ablauf import Textvorbereitung, Vorbereitung
from pseudonymed.dokument.textebene import Box

# Traffic light: red = will be redacted, yellow = uncertain (read badly, blanked as a
# precaution), green = stays (medical term, discarded hit).  The source of a hit is in
# the table; guesses (NER, fuzzy search, identifiers) get a dashed outline.
from pseudonymed.gui.ansichten import (
    VORSCHAU_DPI,
    Seitenansicht,
    Textansicht,
)
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.kern.modell import Fundstelle


class PruefenSeite(QWidget):
    """Step 7, the visual review: the tree of documents and pages on the left, the page or
    the text in the middle, and on the right the table of hits with the controls to
    discard, take up, box and mask, and the button that saves the working state."""

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        """Build the three regions of the splitter; nothing is shown until
        :meth:`aktualisiere` fills the tree."""
        super().__init__(eltern)
        self.sitzung = sitzung
        self.dokument: Dokument | None = None
        self.seite = 0
        self.gewaehlt: Fundstelle | None = None
        self.auswahl: tuple[int, int] | None = None  # offsets of words picked on the page
        self.synchron = False

        aussen = QVBoxLayout(self)
        teiler = QSplitter(Qt.Orientation.Horizontal)
        self._baue_baum(teiler)
        self._baue_ansichten(teiler)
        teiler.addWidget(self._baue_rechts())
        teiler.setSizes([180, 620, 360])
        aussen.addWidget(teiler, 1)

    # -- building ----------------------------------------------------------------------------------
    def _baue_baum(self, teiler: QSplitter) -> None:
        """Left: the tree of documents and their pages."""
        self.baum = QTreeWidget()
        self.baum.setHeaderLabels([self.tr("Dokument / Seite")])
        self.baum.currentItemChanged.connect(self._seite_gewaehlt)
        teiler.addWidget(self.baum)

    def _baue_ansichten(self, teiler: QSplitter) -> None:
        """Middle: the page view and the text view, stacked; one of them is in front."""
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

    def _baue_rechts(self) -> QWidget:
        """Right: the legend, the table of hits, the options, the replacement text, the
        buttons, the hint line and the working-state button."""
        rechts = QWidget()
        r = QVBoxLayout(rechts)
        self._baue_legende(r)
        self._baue_tabelle(r)
        self._baue_optionen(r)
        self._baue_knoepfe(r)
        self.hinweis = QLabel("")
        self.hinweis.setWordWrap(True)
        r.addWidget(self.hinweis)
        self._baue_speichern(r)
        return rechts

    def _baue_legende(self, r: QVBoxLayout) -> None:
        """The legend: what the colours mean and how mouse and keys work."""
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

    def _baue_tabelle(self, r: QVBoxLayout) -> None:
        """The table of hits: page, rule, pseudonym, source; a row selects its hit."""
        self.tabelle = QTableWidget(0, 4)
        self.tabelle.setHorizontalHeaderLabels([self.tr("Seite"), self.tr("Regel"),
                                                self.tr("Pseudonym"), self.tr("Quelle")])
        self.tabelle.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabelle.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabelle.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabelle.itemSelectionChanged.connect(self._zeile_gewaehlt)
        r.addWidget(self.tabelle, 1)

    def _baue_optionen(self, r: QVBoxLayout) -> None:
        """The two options (show medical terms, remember discards) and the field with the
        replacement text for boxes and take-ups."""
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

    def _baue_knoepfe(self, r: QVBoxLayout) -> None:
        """The row of buttons: discard, take up, mask mode and the three zoom buttons."""
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

    def _baue_speichern(self, r: QVBoxLayout) -> None:
        """The button that writes the working state."""
        self.speichern = QPushButton(self.tr("Arbeitsstand speichern …"))
        self.speichern.setToolTip(self.tr(
            "Das aufbereitete Dokument und eine Konfigurationsdatei schreiben, aus denen sich"
            " die Sichtprüfung später wieder aufnehmen und abwandeln lässt (Aufbereitung,"
            " Texterkennung, Treffer, Entscheidungen). Beide Dateien enthalten Klardaten."))
        self.speichern.clicked.connect(self.arbeitsstand_waehlen)
        r.addWidget(self.speichern)

    # -- navigation -------------------------------------------------------------------------------
    def aktualisiere(self) -> None:
        """Rebuild the tree from the session's analysed, not yet exported documents and go
        to the first page."""
        self.baum.clear()
        for d in self.sitzung.dokumente:
            if d.vorbereitung is None or d.vorbereitung.abgeschlossen:
                continue
            knoten = self._baumknoten(d, d.vorbereitung)
            self.baum.addTopLevelItem(knoten)
            knoten.setExpanded(True)
        erster = self.baum.topLevelItem(0)
        if erster is not None:
            kind = erster.child(0)
            self.baum.setCurrentItem(kind if kind is not None else erster)

    def _baumknoten(self, d: Dokument, v: Vorbereitung | Textvorbereitung) -> QTreeWidgetItem:
        """The node of one document with a child per page: "Text" for a text file, else the
        page number with an OCR mark; the user-role data names document and page."""
        knoten = QTreeWidgetItem([d.pfad.name])  # file name, not translated
        knoten.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
        for s in v.seiten:
            if v.ist_text:
                kind = QTreeWidgetItem([self.tr("Text")])
            else:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=s.nummer + 1)
                                        + (" (OCR)" if s.ocr is not None else "")])
            kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), s.nummer))
            knoten.addChild(kind)
        return knoten

    def _seite_gewaehlt(self, aktuell: QTreeWidgetItem | None, _vorher=None) -> None:
        """The tree's current item changed: show that document and page, nothing selected."""
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

    def _vorbereitung(self) -> Vorbereitung | Textvorbereitung | None:
        """The preparation under review, or None before a document was chosen."""
        return None if self.dokument is None else self.dokument.vorbereitung

    def _seitenvorbereitung(self) -> Vorbereitung | None:
        """The preparation under review when it has pages to click into; None for a text
        file or before a document was chosen."""
        v = self._vorbereitung()
        return v if isinstance(v, Vorbereitung) else None

    def fundstellen(self) -> list[Fundstelle]:
        """The hits of the page under review (all of them for a text file)."""
        v = self._vorbereitung()
        if v is None:
            return []
        return [f for f in v.fundstellen if f.seite == self.seite]

    def _aktive_ansicht(self) -> Seitenansicht | Textansicht:
        """The view in front: the text view for a text file, else the page view."""
        return (self.textansicht if self.stapel.currentWidget() is self.textansicht
                else self.ansicht)

    # -- drawing -----------------------------------------------------------------------------------
    def _zeige_ansicht(self, treffer: list[Fundstelle]) -> None:
        """Show the hits in the view that fits the document and adapt the buttons to it."""
        v = self._vorbereitung()
        if v is None:
            return
        verworfen = [f for f in v.verworfen if f.seite == self.seite]
        if isinstance(v, Textvorbereitung):
            self._zeige_text(v, treffer, verworfen)
        else:
            self._zeige_seite(v, treffer, verworfen)
        self._knoepfe_anpassen(v)

    def _zeige_text(self, v: Textvorbereitung, treffer: list[Fundstelle],
                    verworfen: list[Fundstelle]) -> None:
        """Text file: the text view in front, medical terms as spans when wanted."""
        fach = v.fachbegriffe_spannen(self.seite) if self.fach_anzeigen.isChecked() else []
        self.textansicht.zeige(v.text, treffer, self.gewaehlt, fach, verworfen)
        self.stapel.setCurrentWidget(self.textansicht)

    def _zeige_seite(self, v: Vorbereitung, treffer: list[Fundstelle],
                     verworfen: list[Fundstelle]) -> None:
        """Page document: the page image with every kind of mark, the page view in front."""
        fach = v.fachbegriffe_rects(self.seite) if self.fach_anzeigen.isChecked() else []
        auswahl = self._auswahl_rects(v)
        self.ansicht.zeige(v.seitenbild(self.seite, VORSCHAU_DPI), treffer, self.gewaehlt,
                           fach, verworfen, v.unsichere_rects(self.seite), auswahl)
        self.stapel.setCurrentWidget(self.ansicht)

    def _auswahl_rects(self, v: Vorbereitung) -> list[Box]:
        """Boxes of the words the user picked on the page, one per line; empty without a
        selection."""
        if self.auswahl is None:
            return []
        return [tuple(r) for r in v.seiten[self.seite].textbasis.rects(*self.auswahl)]

    def _knoepfe_anpassen(self, v: Vorbereitung | Textvorbereitung) -> None:
        """The buttons follow the document: a take-up needs a selection on a page, masks
        exist only on pages, and the discard button turns into restore for a discarded hit."""
        self.markieren.setEnabled(v.ist_text or self.auswahl is not None)
        self.maske_modus.setVisible(not v.ist_text)
        verworfen_gewaehlt = self.gewaehlt is not None and self.gewaehlt in v.verworfen
        self.verwerfen.setText(self.tr("Treffer wiederherstellen") if verworfen_gewaehlt
                               else self.tr("Treffer verwerfen"))

    def zeichne(self) -> None:
        """Redraw the page under review: the view, the table of hits and the hint line."""
        v = self._vorbereitung()
        if v is None:
            return
        treffer = self.fundstellen()
        self._zeige_ansicht(treffer)
        self._fuelle_tabelle(treffer)
        self._zeige_hinweis(v)

    def _fuelle_tabelle(self, treffer: list[Fundstelle]) -> None:
        """One row per hit (page, rule, pseudonym, source); the selected hit's row is
        selected without firing the selection signal."""
        self.tabelle.blockSignals(True)
        self.tabelle.setRowCount(0)
        for f in treffer:
            zeile = self.tabelle.rowCount()
            self.tabelle.insertRow(zeile)
            for spalte, wert in enumerate((str(f.seite + 1), f.regel, f.pseudonym, f.quelle)):
                self.tabelle.setItem(zeile, spalte, QTableWidgetItem(wert))
            if f is self.gewaehlt:
                self.tabelle.selectRow(zeile)
        self.tabelle.blockSignals(False)

    def _zeige_hinweis(self, v: Vorbereitung | Textvorbereitung) -> None:
        """The hint line: how many medical terms are marked, and the first note about OCR,
        the text layer or the fuzzy search."""
        warnungen = [w for w in v.warnungen + v.hinweise
                     if "OCR" in w or "Textebene" in w or w.startswith("Unscharfe Suche")]
        hinweis = self.tr("{n} Fachbegriffe markiert").format(n=v.fachbegriffe_anzahl)
        self.hinweis.setText(" · ".join([hinweis] + warnungen[:1]))

    # -- actions ---------------------------------------------------------------------------------
    def waehle(self, f: Fundstelle) -> None:
        """Select a hit (a click in the view) and redraw."""
        self.gewaehlt = f
        self.zeichne()

    def _zeile_gewaehlt(self) -> None:
        """A row of the table was selected: select that hit in the view."""
        zeilen = self.tabelle.selectionModel().selectedRows()
        if zeilen:
            treffer = self.fundstellen()
            i = zeilen[0].row()
            if 0 <= i < len(treffer):
                self.gewaehlt = treffer[i]
                self._zeige_ansicht(treffer)

    def verwerfe(self) -> None:
        """Discard the selected hit, or take a discarded one back."""
        v = self._vorbereitung()
        if self.gewaehlt is None or v is None:
            return
        if self.gewaehlt in v.verworfen:
            v.wiederherstelle(self.gewaehlt)
        else:
            v.verwerfe(self.gewaehlt)
        self.gewaehlt = None
        self.zeichne()

    def _maske_umschalten(self, an: bool) -> None:
        """The mask button: dragging on the page draws a mask now (or a box again)."""
        self.ansicht.masken_modus = an

    def _merken_uebernehmen(self, an: bool) -> None:
        """The remember-discards option goes straight into the session's options."""
        self.sitzung.optionen.verworfene_merken = an

    def kasten(self, rect: Box, pseudonym: str | None = None) -> None:
        """A drawn rectangle: a black mask in mask mode, otherwise a box with the
        replacement text from the field (no dialog, so nothing can get lost behind
        the window).  The new hit is selected; errors are shown in the hint line.
        Nothing happens for a text file, which has no page to draw on."""
        v = self._seitenvorbereitung()
        if v is None:
            return
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
        v = self._seitenvorbereitung()
        if v is None:
            return
        spanne = v.wort_bei(self.seite, x, y)
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
        v = self._vorbereitung()
        if v is None:
            return
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
        v = self._vorbereitung()
        if not isinstance(v, Textvorbereitung):
            return
        spanne = self.textansicht.auswahl()
        if spanne is None:
            if self.synchron:
                raise ValueError("Keine Auswahl.")
            return
        v.markiere(self.seite, spanne[0], spanne[1], pseudonym or self.tr("[geschwärzt]"))
        self.gewaehlt = None
        self.zeichne()

    # -- working state -----------------------------------------------------------------------------
    def arbeitsstand_waehlen(self) -> None:
        """Ask where to put the working state (next to the source by default) and save it."""
        v = self._vorbereitung()
        if v is None:
            return
        vorschlag = v.quelle.with_name(v.quelle.stem + standmodul.ENDUNG)
        pfad, _ = QFileDialog.getSaveFileName(
            self, self.tr("Arbeitsstand speichern"), str(vorschlag),
            self.tr("Arbeitsstand (*.arbeitsstand.json)"))
        if pfad:
            self.arbeitsstand_speichern(Path(pfad))

    def arbeitsstand_speichern(self, pfad: Path) -> tuple[Path, Path] | None:
        """Write document and configuration; returns the two paths."""
        v = self._vorbereitung()
        if v is None:
            return None
        try:
            dokument, konfiguration = standmodul.speichere(v, pfad)
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
