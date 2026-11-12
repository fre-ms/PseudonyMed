"""Step 2: drop the documents.  They are opened at once; scans and photos appear
below for the clean-up before OCR."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.dokument.laden import BILDENDUNGEN, HEIC_ENDUNGEN
from pseudonymed.gui.arbeiter import Arbeiter
from pseudonymed.gui.aufbereiten_seite import AufbereitenSeite
from pseudonymed.gui.sitzung import Dokument, Sitzung


class Ablage(QListWidget):
    """The file list that takes drops from the file manager: local files and folders
    arrive as paths through ``abgelegt``, anything else is ignored."""

    abgelegt = Signal(list)

    def __init__(self, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        pfade = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if pfade:
            self.abgelegt.emit(pfade)
            event.acceptProposedAction()


class DokumenteSeite(QWidget):
    """The documents step: the list of inputs with their working state, the paper
    format for images, and the embedded clean-up panel for pages without a text layer.
    Files are opened in the background as soon as they arrive."""

    geladen = Signal()  # every pending document is open and, where needed, has a suggestion
    weiter = Signal()  # the user wants to go on to the analysis

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False  # tests run the work inline
        self._arbeiter: Arbeiter | None = None
        self._warteschlange: list[Dokument] = []
        aussen = QVBoxLayout(self)

        aussen.addWidget(QLabel(self.tr(
            "Dateien hierher ziehen oder hinzufügen (PDF, Fotos, Texte, Whisper-JSON, E-Mails)."
            " Scans und Fotos erscheinen darunter zum Aufbereiten vor der Texterkennung."
            " Ein gespeicherter Arbeitsstand (…arbeitsstand.json oder das Dokument daneben)"
            " füllt alles bis zur Sichtprüfung wieder aus.")))
        oben = QHBoxLayout()
        self.liste = Ablage()
        self.liste.setMaximumHeight(150)
        self.liste.abgelegt.connect(self.hinzufuegen)
        oben.addWidget(self.liste, 1)
        knoepfe = QVBoxLayout()
        hinzu = QPushButton(self.tr("Dateien hinzufügen …"))
        hinzu.clicked.connect(self.dateien_waehlen)
        entfernen = QPushButton(self.tr("Entfernen"))
        entfernen.clicked.connect(self.entfernen)
        knoepfe.addWidget(hinzu)
        knoepfe.addWidget(entfernen)
        knoepfe.addWidget(QLabel(self.tr("Papierformat für Fotos und Bilder")))
        self.papier = QComboBox()
        for name, wert in ((self.tr("A4"), "a4"), (self.tr("A5"), "a5"), (self.tr("A3"), "a3"),
                           (self.tr("US Letter"), "letter"), (self.tr("US Legal"), "legal"),
                           (self.tr("wie das Bild"), "bild")):
            self.papier.addItem(name, wert)
        self.papier.setToolTip(self.tr(
            "Seitengröße der PDF-Ausgabe für Fotos und Bilder; Hoch- oder Querformat folgt"
            " dem Bild, das Bild wird eingepasst. Gilt für alle Bilder dieser Sitzung."))
        self.papier.currentIndexChanged.connect(self._papier_gewechselt)
        knoepfe.addWidget(self.papier)
        knoepfe.addStretch(1)
        oben.addLayout(knoepfe)
        aussen.addLayout(oben)

        self.aufbereiten = AufbereitenSeite(sitzung)
        aussen.addWidget(self.aufbereiten, 1)

        unten = QHBoxLayout()
        self.status = QLabel("")
        self.weiter_knopf = QPushButton(self.tr("Weiter: Texterkennung"))
        self.weiter_knopf.setDefault(True)
        self.weiter_knopf.clicked.connect(self.weiter.emit)
        unten.addWidget(self.status, 1)
        unten.addWidget(self.weiter_knopf)
        aussen.addLayout(unten)

    # -- files -------------------------------------------------------------------------------------
    @property
    def laedt(self) -> bool:
        """True while at least one document is still being opened."""
        return bool(self._warteschlange)

    def dateien_waehlen(self) -> None:
        """Let the user pick files in a dialog and add them."""
        pfade, _ = QFileDialog.getOpenFileNames(self, self.tr("Dokumente"), "", self.tr(
            "Dokumente (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.heic *.heif *.bmp *.webp"
            " *.txt *.md *.srt *.vtt *.json *.eml)"))
        self.hinzufuegen([Path(p) for p in pfade])

    def hinzufuegen(self, pfade: list[Path]) -> None:
        """Add files and open them straight away (in the background)."""
        neue: list[Dokument] = []
        for pfad in pfade:
            if pfad.is_dir():
                self.hinzufuegen(sorted(p for p in pfad.iterdir() if p.is_file()))
                continue
            d = self.sitzung.fuege_hinzu(pfad)
            if d is not None:
                eintrag = QListWidgetItem(self._text(d), self.liste)
                eintrag.setData(Qt.ItemDataRole.UserRole, id(d))
                neue.append(d)
        self.aktualisiere()
        if neue:
            laeuft = self.laedt
            self._warteschlange += neue
            if not laeuft:
                self._naechstes()

    def _papier_gewechselt(self, _i: int = 0) -> None:
        """Images are opened on the new paper: reload those that are already in."""
        self.sitzung.optionen.papierformat = self.papier.currentData()
        bilder = [d for d in self.sitzung.dokumente
                  if d.pfad.suffix.lower() in BILDENDUNGEN | HEIC_ENDUNGEN
                  and d not in self._warteschlange]
        if bilder:
            laeuft = self.laedt
            self._warteschlange += bilder
            self.aktualisiere()
            if not laeuft:
                self._naechstes()

    def entfernen(self) -> None:
        """Take the selected entries out of the list, the queue and the session."""
        for eintrag in self.liste.selectedItems():
            d = self._dokument(eintrag)
            if d is not None:
                if d in self._warteschlange:
                    self._warteschlange.remove(d)
                self.sitzung.entferne(d)
            self.liste.takeItem(self.liste.row(eintrag))
        self.aktualisiere()
        self.aufbereiten.aktualisiere()

    def _dokument(self, eintrag: QListWidgetItem) -> Dokument | None:
        """The session document behind a list entry; None once it has been removed."""
        kennung = eintrag.data(Qt.ItemDataRole.UserRole)
        return next((d for d in self.sitzung.dokumente if id(d) == kennung), None)

    def _text(self, d: Dokument) -> str:
        """The list entry: file name and working state."""
        return f"{d.pfad.name}  –  {self._zustand(d)}"

    def _zustand(self, d: Dokument) -> str:
        """The working state of ``d`` in the user's words, from the most advanced
        (error, exported) back to "still loading"."""
        if d.fehler:
            return self.tr("Fehler: {fehler}").format(fehler=d.fehler)
        if d.ergebnis is not None:
            if d.ergebnis.freigegeben:
                return self.tr("freigegeben")
            return self.tr("nicht freigegeben")
        if d.vorbereitung is not None:
            if d.arbeitsstand is not None:
                return self.tr("Arbeitsstand, {treffer} Treffer").format(
                    treffer=len(d.vorbereitung.fundstellen))
            if d.vorbereitung.ist_text:
                return self.tr("Text, {treffer} Treffer").format(
                    treffer=len(d.vorbereitung.fundstellen))
            return self.tr("analysiert, {treffer} Treffer").format(
                treffer=len(d.vorbereitung.fundstellen))
        if d.vorlage is not None:
            n = len(d.vorlage.kandidaten)
            if n:
                return self.tr("{seiten} Seite(n), {n} ohne Textebene: unten aufbereiten").format(
                    seiten=d.vorlage.seitenzahl, n=n)
            return self.tr("{seiten} Seite(n) mit Textebene").format(seiten=d.vorlage.seitenzahl)
        if d in self._warteschlange:
            return self.tr("wird geladen …")
        return self.tr("Textdatei")

    def aktualisiere(self) -> None:
        """Refresh every list entry and the status line (count, pages to clean up,
        loading)."""
        for i in range(self.liste.count()):
            eintrag = self.liste.item(i)
            d = self._dokument(eintrag)
            if d is not None:
                eintrag.setText(self._text(d))
        n = len(self.sitzung.dokumente)
        offen = sum(1 for d in self.sitzung.dokumente if d.aufzubereiten)
        text = self.tr("{n} Dokument(e)").format(n=n)
        if offen:
            text += self.tr(", {offen} mit Seiten zum Aufbereiten").format(offen=offen)
        if self.laedt:
            text += self.tr(" – lade …")
        self.status.setText(text)

    # -- loading -----------------------------------------------------------------------------------
    def _melde(self, titel: str, text: str) -> None:
        """Warn the user; in the synchronous test mode raise instead of a dialog."""
        if self.synchron:
            raise RuntimeError(text)
        QMessageBox.warning(self, titel, text)

    def _naechstes(self) -> None:
        """Open the next queued document (inline in the synchronous test mode, else in a
        worker thread); with the queue empty, refresh both panels and announce it."""
        if not self._warteschlange:
            self.aktualisiere()
            self.aufbereiten.aktualisiere()
            self.geladen.emit()
            return
        d = self._warteschlange[0]
        self.aktualisiere()
        if self.synchron:
            try:
                self.sitzung.lade(d)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self._fehler(e, d)
                return
            self._fertig(d)
            return
        self._arbeiter = Arbeiter(lambda: self.sitzung.lade(d), self)
        self._arbeiter.fertig.connect(lambda _r, d=d: self._fertig(d))
        self._arbeiter.fehler.connect(lambda e, d=d: self._fehler(e, d))
        self._arbeiter.start()

    def _fertig(self, d: Dokument) -> None:
        """Document ``d`` is open: take it off the queue and go on."""
        if d in self._warteschlange:
            self._warteschlange.remove(d)
        self._naechstes()

    def _fehler(self, e: Exception, d: Dokument) -> None:
        """Opening ``d`` failed: remember the error, show it, go on with the queue."""
        d.fehler = str(e)
        if d in self._warteschlange:
            self._warteschlange.remove(d)
        if self.synchron:
            raise e
        QMessageBox.warning(self, d.pfad.name, str(e))
        self._naechstes()
