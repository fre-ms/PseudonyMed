"""Step 7: control before export.

Two questions, two lights.  First the re-check: are all identifiers from step 1 gone
from the result (the finishing phase runs into a scratch folder, exactly as the export
will)?  Second the residual-risk estimate: how many people share what the text still
says (see :mod:`pseudonymed.pruefung.restrisiko`)?  Documents of the same patient are
judged together, earlier released ones included."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.ablauf import Ergebnis
from pseudonymed.gui.arbeiter import Arbeiter
from pseudonymed.gui.export_seite import AMPELFARBEN
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.konfig import Einstellungen, lade_einstellungen, speichere_einstellungen
from pseudonymed.pruefung import restrisiko as risikomodul
from pseudonymed.pruefung.bericht import als_text


class KontrolleSeite(QWidget):
    """Step "8 Kontrolle": the re-check light, the residual-risk light, one table row per
    re-checked document and, for the selected row, the counted features and the report."""

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        """Build the page region by region and start with both lights grey."""
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self._arbeiter: Arbeiter | None = None
        self._warteschlange: list[Dokument] = []
        e = lade_einstellungen()
        sitzung.optionen.referenzpopulation = e.referenzpopulation
        sitzung.optionen.eigene_orte = e.eigene_orte
        aussen = QVBoxLayout(self)
        self._baue_pruefzeile(aussen)
        self._baue_risikozeile(aussen)
        self._baue_referenzzeile(aussen, e)
        self._baue_tabellen(aussen)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        aussen.addWidget(self.status)
        self._ampel_setzen(self.ampel, "grau")
        self._ampel_setzen(self.risiko_ampel, "grau")

    # -- build -------------------------------------------------------------------------------------
    def _baue_pruefzeile(self, aussen: QVBoxLayout) -> None:
        """Light 1: the question, its answer text, the light and the re-check button."""
        zeile = QHBoxLayout()
        zeile.addWidget(QLabel(self.tr(
            "Erneute Prüfung: alle Angaben aus „1 Patientendaten“ entfernt?")))
        self.ampel_text = QLabel("")
        self.ampel_text.setWordWrap(True)
        zeile.addWidget(self.ampel_text, 1)
        self.ampel = QLabel()
        self.ampel.setFixedSize(28, 28)
        zeile.addWidget(self.ampel)
        self.erneut = QPushButton(self.tr("Erneut prüfen"))
        self.erneut.clicked.connect(self.pruefen)
        zeile.addWidget(self.erneut)
        aussen.addLayout(zeile)

    def _baue_risikozeile(self, aussen: QVBoxLayout) -> None:
        """Light 2: the residual-risk question, its summary, the light and the explanation
        of the estimate underneath."""
        zeile = QHBoxLayout()
        zeile.addWidget(QLabel(self.tr("Restrisiko: wie viele Menschen teilen, was im Text"
                                       " bleibt?")))
        self.risiko_text = QLabel("")
        self.risiko_text.setWordWrap(True)
        zeile.addWidget(self.risiko_text, 1)
        self.risiko_ampel = QLabel()
        self.risiko_ampel.setFixedSize(28, 28)
        zeile.addWidget(self.risiko_ampel)
        aussen.addLayout(zeile)
        hinweis = QLabel(self.tr(
            "Schätzung nach dem k-map-Prinzip (El Emam, EMA Policy 0070, Rocher et al.): Referenz"
            " ÷ Produkt der Häufigkeiten von Geschlecht, Geburtsjahr, kleinstem genanntem Ort"
            " (Destatis) und seltenster genannter Erkrankung (Orphanet). Grün ab 11 gleichartigen"
            " Personen (κ ≤ 0,09), Gelb ab 5, Rot darunter. Eigene Synthese, kein Standard; die"
            " Unabhängigkeitsannahme ist bei untypischen Kombinationen zu optimistisch."))
        hinweis.setWordWrap(True)
        aussen.addWidget(hinweis)

    def _baue_referenzzeile(self, aussen: QVBoxLayout, e: Einstellungen) -> None:
        """The two parameters of the estimate (reference population, own places) and the
        button that recalculates with them."""
        zeile = QHBoxLayout()
        zeile.addWidget(QLabel(self.tr("Referenzpopulation")))
        self.referenz = QLineEdit(str(e.referenzpopulation))
        self.referenz.setMaximumWidth(120)
        self.referenz.setToolTip(self.tr(
            "Aus wie vielen Menschen ein Angreifer wählt: Deutschland 83.600.000, oder das"
            " Einzugsgebiet der Sprechstunde, wenn der Absender bekannt ist"))
        zeile.addWidget(self.referenz)
        zeile.addWidget(QLabel(self.tr("Eigene Orte (nicht gezählt)")))
        self.eigene_orte = QLineEdit(e.eigene_orte)
        self.eigene_orte.setPlaceholderText(self.tr("Ort der Sprechstunde, Klinik … (Komma)"))
        zeile.addWidget(self.eigene_orte, 1)
        neu = QPushButton(self.tr("Neu berechnen"))
        neu.clicked.connect(self.neu_berechnen)
        zeile.addWidget(neu)
        aussen.addLayout(zeile)

    def _baue_tabellen(self, aussen: QVBoxLayout) -> None:
        """The document table above, the feature table and the report text below, in a
        vertical splitter."""
        teiler = QSplitter(Qt.Orientation.Vertical)
        self.tabelle = QTableWidget(0, 6)
        self.tabelle.setHorizontalHeaderLabels([
            self.tr("Dokument"), self.tr("Erneute Prüfung"), self.tr("λ (Personen)"),
            self.tr("κ"), self.tr("Restrisiko"), self.tr("Größter Beitrag")])
        self.tabelle.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabelle.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.tabelle.verticalHeader().setVisible(False)
        self.tabelle.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabelle.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabelle.itemSelectionChanged.connect(self._zeige_details)
        teiler.addWidget(self.tabelle)
        unten = QWidget()
        ul = QHBoxLayout(unten)
        ul.setContentsMargins(0, 0, 0, 0)
        self.merkmale = QTableWidget(0, 3)
        self.merkmale.setHorizontalHeaderLabels([self.tr("Merkmal"), self.tr("Bit"),
                                                 self.tr("Beleg")])
        self.merkmale.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.merkmale.verticalHeader().setVisible(False)
        self.merkmale.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        ul.addWidget(self.merkmale, 1)
        self.protokoll = QPlainTextEdit()
        self.protokoll.setReadOnly(True)
        ul.addWidget(self.protokoll, 1)
        teiler.addWidget(unten)
        aussen.addWidget(teiler, 1)

    # -- lights ------------------------------------------------------------------------------------
    @staticmethod
    def _ampel_setzen(label: QLabel, farbe: str) -> None:
        """Paint the round light in the given colour and remember the colour on the label."""
        label.setStyleSheet(
            f"background: {AMPELFARBEN[farbe]}; border-radius: 14px; border: 1px solid #555;")
        label.setProperty("farbe", farbe)

    @property
    def farbe(self) -> str:
        """Colour of the re-check light: grau, gruen, gelb or rot."""
        return self.ampel.property("farbe") or "grau"

    @property
    def risiko_farbe(self) -> str:
        """Colour of the residual-risk light: grau, gruen, gelb or rot."""
        return self.risiko_ampel.property("farbe") or "grau"

    def aktualisiere(self) -> None:
        """Called when the step is shown: run the re-check for what needs it, then fill in."""
        if any(d.pruefbereit and not d.vorpruefung_aktuell for d in self.sitzung.dokumente):
            self.pruefen()
        else:
            self._darstellen()

    def neu_berechnen(self) -> None:
        """New reference population or own places: store them and run the check again."""
        try:
            referenz = int(self.referenz.text().replace(".", "").replace(" ", "") or 0)
        except ValueError:
            referenz = 0
        if referenz < 100:
            self.status.setText(self.tr("Referenzpopulation: bitte eine Zahl ab 100."))
            return
        e = lade_einstellungen()
        e.referenzpopulation, e.eigene_orte = referenz, self.eigene_orte.text().strip()
        try:
            speichere_einstellungen(e)
        except OSError:
            pass
        self.sitzung.optionen.referenzpopulation = referenz
        self.sitzung.optionen.eigene_orte = e.eigene_orte
        for d in self.sitzung.dokumente:
            d.vorpruefung_version = -1  # forces a new pre-check
        self.pruefen()

    def pruefen(self) -> None:
        """Re-check every document that is ready for it, one after the other; a run that
        is already under way is left alone."""
        if self._warteschlange:
            return
        self._warteschlange = [d for d in self.sitzung.dokumente if d.pruefbereit]
        if not self._warteschlange:
            self._darstellen()
            return
        self._ampel_setzen(self.ampel, "grau")
        self.ampel_text.setText(self.tr("Erneute Prüfung läuft …"))
        self._naechstes()

    def _naechstes(self) -> None:
        """Take the next document off the queue and re-check it, in a worker thread or,
        in the synchronous test mode, right here; an empty queue ends the run."""
        if not self._warteschlange:
            self.erneut.setEnabled(True)
            self._darstellen()
            return
        d = self._warteschlange.pop(0)
        self.erneut.setEnabled(False)
        self.status.setText(self.tr("Prüfe {name} …").format(name=d.pfad.name))
        if self.synchron:
            try:
                self.sitzung.pruefe(d)
            except Exception as e:  # noqa: BLE001
                d.fehler = str(e)
            self._naechstes()
            return
        self._arbeiter = Arbeiter(partial(self.sitzung.pruefe, d), self)
        self._arbeiter.fertig.connect(lambda _e: self._naechstes())
        self._arbeiter.fehler.connect(partial(self._pruefung_fehlgeschlagen, d))
        self._arbeiter.start()

    def _pruefung_fehlgeschlagen(self, d: Dokument, e: object) -> None:
        """Record the worker's exception on the document and carry on with the queue."""
        d.fehler = str(e)
        self._naechstes()

    # -- display -----------------------------------------------------------------------------------
    def _darstellen(self) -> None:
        """Fill both lights and the document table, then the details of the selected row."""
        farbe, text = self.sitzung.ampel()
        self._ampel_setzen(self.ampel, farbe)
        self.ampel_text.setText(self.tr(text))
        self._zeige_restrisiko()
        if self._fuelle_tabelle() and self.tabelle.currentRow() < 0:
            self.tabelle.selectRow(0)
        self._zeige_details()
        self.status.setText("")

    def _zeige_restrisiko(self) -> None:
        """Light 2: the joint estimate over all documents of the patient, grey without one."""
        gesamt, fruehere = self.sitzung.kontrolle()
        if gesamt is None:
            self._ampel_setzen(self.risiko_ampel, "grau")
            self.risiko_text.setText(self.tr("Noch kein geprüftes Dokument."))
            return
        self._ampel_setzen(self.risiko_ampel, gesamt.ampel)
        self.risiko_text.setText(self._risikosatz(gesamt, fruehere))

    def _risikosatz(self, gesamt: risikomodul.Restrisiko, fruehere: int) -> str:
        """The summary sentence of the joint estimate: figures and threshold, the largest
        contribution if there is one, the number of earlier released documents if any."""
        dominant = gesamt.dominant
        satz = self.tr("Alle Dokumente dieses Patienten zusammen: {kurz}; {schwelle}.").format(
            kurz=gesamt.kurz(), schwelle=risikomodul.ampel_text(gesamt.ampel))
        if dominant:
            satz += self.tr(" Größter Beitrag: {merkmal} ({bits:.1f} Bit).").format(
                merkmal=dominant.beschreibung, bits=dominant.bits)
        if fruehere:
            satz += self.tr(" Einschließlich {n} früher freigegebener Dokument(e).").format(
                n=fruehere)
        return satz

    def _fuelle_tabelle(self) -> int:
        """One row per re-checked document, every cell tagged with the document's id so
        that the details can find it again.  Returns the number of rows."""
        geprueft = [(d, d.vorpruefung) for d in self.sitzung.dokumente
                    if d.vorpruefung is not None]
        self.tabelle.setRowCount(len(geprueft))
        for i, (d, e) in enumerate(geprueft):
            for j, w in enumerate(self._tabellenzeile(d, e)):
                eintrag = QTableWidgetItem(w)
                eintrag.setData(Qt.ItemDataRole.UserRole, id(d))
                self.tabelle.setItem(i, j, eintrag)
        return len(geprueft)

    def _tabellenzeile(self, d: Dokument, e: Ergebnis) -> list[str]:
        """The six cells of a document's row: name, re-check verdict, λ, κ, the light and
        the largest contribution; dashes where the result carries no estimate."""
        verdikt = self.tr("bestanden") if e.freigegeben else self.tr("NICHT bestanden")
        r = e.restrisiko
        if r is None:
            return [d.pfad.name, verdikt, "–", "–", "–", "–"]
        beitrag = (f"{r.dominant.beschreibung} ({r.dominant.bits:.1f} Bit)"
                   if r.dominant else "–")
        return [d.pfad.name, verdikt, risikomodul._zahl(r.lam), f"{r.kappa:.2f}",
                r.ampel.upper(), beitrag]

    def _zeige_details(self) -> None:
        """Details of the selected row: the counted features with their evidence and the
        full report text; both empty without a selection."""
        d = self._gewaehltes_dokument()
        if d is None or d.vorpruefung is None:
            self.merkmale.setRowCount(0)
            self.protokoll.clear()
            return
        zeilen = self._merkmalzeilen(d.vorpruefung.restrisiko)
        self.merkmale.setRowCount(len(zeilen))
        for i, zeile in enumerate(zeilen):
            for j, w in enumerate(zeile):
                self.merkmale.setItem(i, j, QTableWidgetItem(w))
        self.protokoll.setPlainText(als_text(d.vorpruefung.bericht))

    def _gewaehltes_dokument(self) -> Dokument | None:
        """The session document behind the selected table row, found by the id stored in
        the row's first cell; None without a selection."""
        zeile = self.tabelle.currentRow()
        eintrag = self.tabelle.item(zeile, 0) if zeile >= 0 else None
        if eintrag is None:
            return None
        kennung = eintrag.data(Qt.ItemDataRole.UserRole)
        return next((d for d in self.sitzung.dokumente if id(d) == kennung), None)

    def _merkmalzeilen(self, r: risikomodul.Restrisiko | None) -> list[tuple[str, str, str]]:
        """Rows of the feature table: counted features first, each by bits, then the ones
        only listed, then the markers prefixed with "!"; nothing without an estimate."""
        if r is None:
            return []
        merkmale = [(m.beschreibung + ("" if m.gezaehlt else self.tr(" (nicht gezählt)")),
                     f"{m.bits:.1f}", m.beleg)
                    for m in sorted(r.merkmale, key=lambda m: (-m.gezaehlt, -m.bits))]
        markierungen = [("! " + mk.text, "", mk.beleg) for mk in r.markierungen]
        return merkmale + markierungen
