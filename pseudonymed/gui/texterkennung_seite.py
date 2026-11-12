"""Step 3: text recognition.  Choose the engine and its language packs, read all
documents, then check and correct the recognised text next to the cleaned-up page.
Only afterwards does the analysis look for identifiers; the review step no longer
edits text."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.gui.arbeiter import Arbeiter
from pseudonymed.gui.pruefen_seite import VORSCHAU_DPI, Seitenansicht
from pseudonymed.gui.sitzung import Dokument, Sitzung


class TexterkennungSeite(QWidget):
    """The text-recognition step: engine settings, reading with progress, and the
    recognised text of every page next to the cleaned-up image, editable for OCR pages."""

    gelesen = Signal()
    weiter = Signal()

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self.laedt = lambda: False
        self._arbeiter: Arbeiter | None = None
        self._warteschlange: list[Dokument] = []
        self._gesamt = 0
        self.dokument: Dokument | None = None
        self.seite = 0
        self._geladen: tuple[int, int] | None = None
        aussen = QVBoxLayout(self)
        aussen.addWidget(self._baue_einstellungen())
        self._engine_gewechselt()
        aussen.addWidget(self._baue_ansicht(), 1)
        aussen.addLayout(self._baue_fusszeile())

    # -- building the page -------------------------------------------------------------------------
    def _baue_einstellungen(self) -> QGroupBox:
        """The settings group: engine, language packs, table pass, structuring, and the
        row with progress bar and start button."""
        einstellungen = QGroupBox(self.tr("Texterkennung (OCR) für Scans und Fotos"))
        form = QFormLayout(einstellungen)
        self.engine = QComboBox()
        self.engine.addItem(self.tr("automatisch"), "auto")
        for b in ocrmodul.verfuegbare_backends():
            self.engine.addItem(b, b)
        self.engine.addItem(self.tr("aus (nur Textebene)"), "aus")
        self.engine.currentIndexChanged.connect(self._engine_gewechselt)
        form.addRow(self.tr("OCR-Programm"), self.engine)
        self.sprachen = QLineEdit()
        form.addRow(self.tr("Sprachpakete"), self.sprachen)
        self.sprachen_hinweis = QLabel("")
        self.sprachen_hinweis.setWordWrap(True)
        form.addRow(self.sprachen_hinweis)
        self.tabellen = QCheckBox(self.tr(
            "Tabellen: Gitter erkennen und die Zellen ohne Linien in einem zweiten Durchgang"
            " lesen (Tesseract)"))
        self.tabellen.setChecked(True)
        form.addRow(self.tabellen)
        self.gliedern = QCheckBox(self.tr(
            "Erkannten Text gliedern: Absätze zusammenfügen, Überschriften, Listen und"
            " Tabellen als Markdown (für das Sprachmodell)"))
        self.gliedern.setChecked(self.sitzung.optionen.gliedern)
        self.gliedern.toggled.connect(self._gliedern_gewechselt)
        form.addRow(self.gliedern)
        form.addRow(self._baue_startzeile())
        return einstellungen

    def _baue_startzeile(self) -> QHBoxLayout:
        """Progress bar, status text and the button that starts reading."""
        zeile = QHBoxLayout()
        self.fortschritt = QProgressBar()
        self.fortschritt.setRange(0, 1)
        self.fortschritt.setValue(0)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.knopf = QPushButton(self.tr("Texterkennung starten"))
        self.knopf.setMinimumHeight(32)
        self.knopf.clicked.connect(self.starten)
        zeile.addWidget(self.fortschritt, 1)
        zeile.addWidget(self.status, 2)
        zeile.addWidget(self.knopf)
        return zeile

    def _baue_ansicht(self) -> QSplitter:
        """Tree of documents and pages, the page image, and the text column."""
        teiler = QSplitter(Qt.Orientation.Horizontal)
        self.baum = QTreeWidget()
        self.baum.setHeaderLabels([self.tr("Dokument / Seite")])
        self.baum.currentItemChanged.connect(self._seite_gewaehlt)
        teiler.addWidget(self.baum)
        self.ansicht = Seitenansicht()
        self.ansicht.masken_modus = True  # nothing is drawn here, drag with modifier pans
        self.ansicht.kasten_gezeichnet.connect(lambda _r: None)
        teiler.addWidget(self.ansicht)
        teiler.addWidget(self._baue_textspalte())
        teiler.setSizes([180, 560, 460])
        return teiler

    def _baue_textspalte(self) -> QWidget:
        """The recognised text with its title, the apply and reset buttons and the note
        whether the page was corrected."""
        rechts = QWidget()
        r = QVBoxLayout(rechts)
        r.setContentsMargins(0, 0, 0, 0)
        self.text_titel = QLabel(self.tr("Erkannter Text"))
        self.text_titel.setWordWrap(True)
        r.addWidget(self.text_titel)
        self.text = QPlainTextEdit()
        r.addWidget(self.text, 1)
        knoepfe = QHBoxLayout()
        self.uebernehmen = QPushButton(self.tr("Text übernehmen"))
        self.uebernehmen.clicked.connect(self.text_uebernehmen)
        self.zuruecksetzen = QPushButton(self.tr("Auf OCR-Text zurücksetzen"))
        self.zuruecksetzen.clicked.connect(self.text_zuruecksetzen)
        self.text_hinweis = QLabel("")
        for k in (self.uebernehmen, self.zuruecksetzen):
            k.setMinimumHeight(32)
            knoepfe.addWidget(k)
        knoepfe.addWidget(self.text_hinweis, 1)
        r.addLayout(knoepfe)
        return rechts

    def _baue_fusszeile(self) -> QHBoxLayout:
        """The note about unread documents and the button to the analysis."""
        unten = QHBoxLayout()
        self.hinweis = QLabel("")
        self.hinweis.setWordWrap(True)
        self.weiter_knopf = QPushButton(self.tr("Weiter: Analysieren"))
        self.weiter_knopf.setMinimumHeight(32)
        self.weiter_knopf.setDefault(True)
        self.weiter_knopf.clicked.connect(self.weiter.emit)
        unten.addWidget(self.hinweis, 1)
        unten.addWidget(self.weiter_knopf)
        return unten

    # -- settings ----------------------------------------------------------------------------------
    def _engine_gewechselt(self, _i: int = 0) -> None:
        """Adapt placeholder and note to the chosen engine; the table pass is Tesseract's."""
        name = self.engine.currentData()
        tesseract = name == "tesseract" or (name == "auto"
                                            and ocrmodul.verfuegbare_backends()[:1]
                                            == ["tesseract"])
        self.tabellen.setEnabled(tesseract)
        if tesseract:
            programm = ocrmodul.tesseract_programm()
            installiert = ocrmodul.tesseract_sprachen(programm) if programm else []
            self.sprachen.setPlaceholderText("deu+eng")
            self.sprachen_hinweis.setText(
                self.tr("Installierte Tesseract-Sprachen: {liste}").format(
                    liste=", ".join(installiert) or "–"))
        elif name == "aus":
            self.sprachen.setPlaceholderText("")
            self.sprachen_hinweis.setText(self.tr("Es wird nur die Textebene der PDFs gelesen."))
        else:
            self.sprachen.setPlaceholderText("de")
            self.sprachen_hinweis.setText(self.tr(
                "RapidOCR: Sprachtyp de (Standard), en, latin, ch …; Modelle sind im Paket."))

    def _gliedern_gewechselt(self, an: bool) -> None:
        """Takes effect at once: the structure was computed when the page was read."""
        self.sitzung.optionen.gliedern = an
        for d in self.sitzung.dokumente:
            for traeger in (d.lesung, d.vorbereitung):
                if traeger is not None and hasattr(traeger, "opt"):
                    traeger.opt.gliedern = an
        self._geladen = None
        self.zeichne()

    def _optionen_uebernehmen(self) -> None:
        """Copy engine, languages, table pass and structuring into the session options."""
        o = self.sitzung.optionen
        o.ocr = self.engine.currentData()
        o.ocr_sprachen = self.sprachen.text().strip()
        o.ocr_tabellen = self.tabellen.isChecked()
        o.gliedern = self.gliedern.isChecked()

    def _melde(self, titel: str, text: str) -> None:
        """Tell the user something; in the synchronous test mode raise instead of a dialog."""
        if self.synchron:
            raise RuntimeError(text)
        QMessageBox.information(self, titel, text)

    # -- reading -----------------------------------------------------------------------------------
    def starten(self) -> None:
        """Read every document (text layer, OCR after the clean-up)."""
        if not self.sitzung.dokumente:
            self._melde(self.tr("Texterkennung"), self.tr("Bitte zuerst Dokumente hinzufügen."))
            return
        if self.laedt():
            self._melde(self.tr("Texterkennung"),
                        self.tr("Die Dokumente werden noch geladen; bitte einen Moment warten."))
            return
        self._optionen_uebernehmen()
        self._warteschlange = [d for d in self.sitzung.dokumente if d.lesbar]
        self._gesamt = len(self._warteschlange)
        self.fortschritt.setRange(0, max(1, self._gesamt))
        self.fortschritt.setValue(0)
        self._naechstes()

    def _naechstes(self) -> None:
        """Read the next queued document (inline in the synchronous test mode, else in a
        worker thread); with the queue empty, show the tree and announce the result."""
        if not self._warteschlange:
            self.knopf.setEnabled(True)
            self.fortschritt.setValue(self.fortschritt.maximum())
            self.status.setText(self.tr("Gelesen: {n} Dokument(e).").format(n=self._gesamt))
            self.aktualisiere()
            self.gelesen.emit()
            return
        d = self._warteschlange[0]
        self.knopf.setEnabled(False)
        nummer = self._gesamt - len(self._warteschlange) + 1
        self.fortschritt.setValue(nummer - 1)
        self.status.setText(self.tr("Lese {nummer} von {gesamt}: {name} …").format(
            nummer=nummer, gesamt=self._gesamt, name=d.pfad.name))
        if self.synchron:
            try:
                self.sitzung.lese(d)
            except Exception as e:  # noqa: BLE001
                self._fehler(e, d)
                return
            self._fertig(d)
            return
        self._arbeiter = Arbeiter(lambda: self.sitzung.lese(d), self)
        self._arbeiter.fertig.connect(lambda _r, d=d: self._fertig(d))
        self._arbeiter.fehler.connect(lambda e, d=d: self._fehler(e, d))
        self._arbeiter.start()

    def _fertig(self, d: Dokument) -> None:
        """Document ``d`` is read: take it off the queue and go on."""
        if self._warteschlange and self._warteschlange[0] is d:
            self._warteschlange.pop(0)
        self._naechstes()

    def _fehler(self, e: Exception, d: Dokument) -> None:
        """Reading ``d`` failed: remember the error, show it, go on with the queue."""
        d.fehler = str(e)
        if self.synchron:
            raise e
        QMessageBox.warning(self, d.pfad.name, str(e))
        if self._warteschlange and self._warteschlange[0] is d:
            self._warteschlange.pop(0)
        self._naechstes()

    # -- navigation --------------------------------------------------------------------------------
    def aktualisiere(self) -> None:
        """Rebuild the tree of read documents, summarise what is still unread and show
        the first page; without any read document, view and text are emptied."""
        self._fuelle_baum()
        self.hinweis.setText(self._lesestand())
        if not self._waehle_erste_seite():
            self._zeige_leer()

    def _fuelle_baum(self) -> None:
        """One node per read document with one child per page, OCR pages marked."""
        self.baum.clear()
        for d in self.sitzung.dokumente:
            if d.lesung is None:
                continue
            knoten = QTreeWidgetItem([d.pfad.name])
            knoten.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
            for s in d.lesung.seiten:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=s.nummer + 1)
                                        + (self.tr(" (OCR)") if s.ocr is not None else "")])
                kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), s.nummer))
                knoten.addChild(kind)
            self.baum.addTopLevelItem(knoten)
            knoten.setExpanded(True)

    def _lesestand(self) -> str:
        """The note at the bottom: how many documents are still unread and how many
        text files need no recognition at all."""
        offen = [d for d in self.sitzung.dokumente if d.lesbar and d.lesung is None]
        texte = sum(1 for d in self.sitzung.dokumente if not d.lesbar)
        teile: list[str] = []
        if offen:
            teile.append(self.tr("{n} Dokument(e) noch nicht gelesen").format(n=len(offen)))
        if texte:
            teile.append(self.tr("{n} Textdatei(en) brauchen keine Texterkennung").format(
                n=texte))
        return " · ".join(teile)

    def _waehle_erste_seite(self) -> bool:
        """Make the first page of the first document current; False when the tree is empty."""
        erster = self.baum.topLevelItem(0)
        if erster is None:
            return False
        kind = erster.child(0)
        self.baum.setCurrentItem(erster if kind is None else kind)
        return True

    def _zeige_leer(self) -> None:
        """No document read: no current document, empty view, empty read-only text."""
        self.dokument = None
        self.ansicht.zeige_leer()
        self.text.setPlainText("")
        self.text.setReadOnly(True)

    def _seite_gewaehlt(self, aktuell: QTreeWidgetItem | None,
                        _vorher: QTreeWidgetItem | None = None) -> None:
        """Show the page behind the current tree item."""
        if aktuell is None:
            return
        kennung, nummer = aktuell.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.sitzung.dokumente if id(d) == kennung), None)
        if d is None or d.lesung is None:
            return
        self.dokument = d
        self.seite = max(nummer, 0)
        self.zeichne()

    def zeichne(self) -> None:
        """Show the current page as the OCR saw it and its text: editable with title
        and correction note for OCR pages, read-only for pages with a text layer.  The
        text is reloaded only when another page is shown, so an edit survives redraws."""
        if self.dokument is None or self.dokument.lesung is None:
            return
        lesung = self.dokument.lesung
        self.ansicht.skala = VORSCHAU_DPI / 72.0
        self.ansicht.zeige(lesung.seitenbild(self.seite, VORSCHAU_DPI), [], None)
        schluessel = (id(self.dokument), self.seite)
        if self._geladen != schluessel:
            self.text.setPlainText(lesung.text(self.seite))
            self._geladen = schluessel
        ocr = lesung.hat_ocr(self.seite)
        self.text.setReadOnly(not ocr)
        self.uebernehmen.setEnabled(ocr)
        self.zuruecksetzen.setEnabled(ocr)
        if ocr:
            self.text_titel.setText(self.tr(
                "Erkannter Text (OCR, als Markdown gegliedert), von Hand korrigierbar. Die"
                " Korrektur gilt für Textdatei, Markdown und die Textebene; die Schwärzung im"
                " Bild folgt den OCR-Kästen.") if self.gliedern.isChecked() else self.tr(
                "Erkannter Text (OCR), von Hand korrigierbar. Die Korrektur gilt für Textdatei,"
                " Markdown und die Textebene; die Schwärzung im Bild folgt den OCR-Kästen."))
            s = lesung.seiten[self.seite]
            self.text_hinweis.setText(self.tr("korrigiert") if s.korrektur is not None
                                      else self.tr("OCR-Text unverändert"))
        else:
            self.text_titel.setText(self.tr("Text der Textebene (nicht veränderbar)"))
            self.text_hinweis.setText("")

    # -- corrections -------------------------------------------------------------------------------
    def text_uebernehmen(self) -> None:
        """Store the edited text as the correction of the current OCR page."""
        if self.dokument is None or self.dokument.lesung is None:
            return
        lesung = self.dokument.lesung
        if not lesung.hat_ocr(self.seite):
            return
        lesung.setze_ocr_text(self.seite, self.text.toPlainText())
        self.zeichne()

    def text_zuruecksetzen(self) -> None:
        """Drop the correction of the current OCR page and show the raw OCR text again."""
        if self.dokument is None or self.dokument.lesung is None:
            return
        lesung = self.dokument.lesung
        ocr = lesung.seiten[self.seite].ocr
        if ocr is None:
            return
        lesung.setze_ocr_text(self.seite, ocr.text)
        self.text.setPlainText(ocr.text)
        self.zeichne()
