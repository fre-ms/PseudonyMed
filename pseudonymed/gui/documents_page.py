"""Step 2: drop the documents.  They are opened at once; scans and photos appear
below for the clean-up before OCR."""

from __future__ import annotations

from pathlib import Path

from optdoc.loading import HEIC_SUFFIXES, IMAGE_SUFFIXES
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

from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.cleanup_page import CleanupPage
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.worker import Worker


class DropZone(QListWidget):
    """The file list that takes drops from the file manager: local files and folders
    arrive as paths through ``dropped``, anything else is ignored."""

    dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()


class DocumentsPage(QWidget):
    """The documents step: the list of inputs with their working state, the paper
    format for images, and the embedded clean-up panel for pages without a text layer.
    Files are opened in the background as soon as they arrive."""

    loaded = Signal()  # every pending document is open and, where needed, has a suggestion
    proceed = Signal()  # the user wants to go on to the analysis

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False  # tests run the work inline
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        outer = QVBoxLayout(self)

        outer.addWidget(QLabel(self.tr(
            "Dateien hierher ziehen oder hinzufügen (PDF, Fotos, Texte, Whisper-JSON, E-Mails)."
            " Scans und Fotos erscheinen darunter zum Aufbereiten vor der Texterkennung."
            " Ein gespeicherter Arbeitsstand (…arbeitsstand.json oder das Dokument daneben)"
            " füllt alles bis zur Sichtprüfung wieder aus.")))
        top = QHBoxLayout()
        self.listing = DropZone()
        self.listing.setMaximumHeight(150)
        self.listing.dropped.connect(self.add)
        top.addWidget(self.listing, 1)
        buttons = QVBoxLayout()
        add_button = QPushButton(self.tr("Dateien hinzufügen …"))
        add_button.clicked.connect(self.choose_files)
        remove = QPushButton(self.tr("Entfernen"))
        remove.clicked.connect(self.remove)
        buttons.addWidget(add_button)
        buttons.addWidget(remove)
        buttons.addWidget(QLabel(self.tr("Papierformat für Fotos und Bilder")))
        self.paper = QComboBox()
        for name, value in ((self.tr("A4"), "a4"), (self.tr("A5"), "a5"), (self.tr("A3"), "a3"),
                            (self.tr("US Letter"), "letter"), (self.tr("US Legal"), "legal"),
                            (self.tr("wie das Bild"), "bild")):
            self.paper.addItem(name, value)
        self.paper.setToolTip(self.tr(
            "Seitengröße der PDF-Ausgabe für Fotos und Bilder; Hoch- oder Querformat folgt"
            " dem Bild, das Bild wird eingepasst. Gilt für alle Bilder dieser Sitzung."))
        self.paper.currentIndexChanged.connect(self._paper_changed)
        buttons.addWidget(self.paper)
        buttons.addStretch(1)
        top.addLayout(buttons)
        outer.addLayout(top)

        self.clean_up = CleanupPage(session)
        outer.addWidget(self.clean_up, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.proceed_button = QPushButton(self.tr("Weiter: Texterkennung"))
        self.proceed_button.setDefault(True)
        self.proceed_button.clicked.connect(self.proceed.emit)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.proceed_button)
        outer.addLayout(bottom)

    # -- files -------------------------------------------------------------------------------------
    @property
    def is_loading(self) -> bool:
        """True while at least one document is still being opened."""
        return bool(self._queue)

    def choose_files(self) -> None:
        """Let the user pick files in a dialog and add them."""
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr("Dokumente"), "", self.tr(
            "Dokumente (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.heic *.heif *.bmp *.webp"
            " *.txt *.md *.srt *.vtt *.json *.eml)"))
        self.add([Path(p) for p in paths])

    def add(self, paths: list[Path]) -> None:
        """Add files and open them straight away (in the background)."""
        added: list[Document] = []
        for path in paths:
            if path.is_dir():
                self.add(sorted(p for p in path.iterdir() if p.is_file()))
                continue
            d = self.session.add(path)
            if d is not None:
                entry = QListWidgetItem(self._text(d), self.listing)
                entry.setData(Qt.ItemDataRole.UserRole, id(d))
                added.append(d)
        self.refresh()
        if added:
            running = self.is_loading
            self._queue += added
            if not running:
                self._run_next()

    def _paper_changed(self, _i: int = 0) -> None:
        """Images are opened on the new paper: reload those that are already in."""
        self.session.options.paper_size = self.paper.currentData()
        images = [d for d in self.session.documents
                  if d.path.suffix.lower() in IMAGE_SUFFIXES | HEIC_SUFFIXES
                  and d not in self._queue]
        if images:
            running = self.is_loading
            self._queue += images
            self.refresh()
            if not running:
                self._run_next()

    def remove(self) -> None:
        """Take the selected entries out of the list, the queue and the session."""
        for entry in self.listing.selectedItems():
            d = self._document_of(entry)
            if d is not None:
                if d in self._queue:
                    self._queue.remove(d)
                self.session.remove(d)
            self.listing.takeItem(self.listing.row(entry))
        self.refresh()
        self.clean_up.refresh()

    def _document_of(self, entry: QListWidgetItem) -> Document | None:
        """The session document behind a list entry; None once it has been removed."""
        code = entry.data(Qt.ItemDataRole.UserRole)
        return next((d for d in self.session.documents if id(d) == code), None)

    def _text(self, d: Document) -> str:
        """The list entry: file name and working state."""
        return f"{d.path.name}  –  {self._state_line(d)}"

    def _state_line(self, d: Document) -> str:
        """The working state of ``d`` in the user's words, from the most advanced
        (error, exported) back to "still loading"."""
        if d.error:
            return self.tr("Fehler: {fehler}").format(fehler=d.error)
        if d.result is not None:
            if d.result.released:
                return self.tr("freigegeben")
            return self.tr("nicht freigegeben")
        if d.preparation is not None:
            if d.working_state is not None:
                return self.tr("Arbeitsstand, {treffer} Treffer").format(
                    treffer=len(d.preparation.hits))
            if d.preparation.is_text:
                return self.tr("Text, {treffer} Treffer").format(
                    treffer=len(d.preparation.hits))
            return self.tr("analysiert, {treffer} Treffer").format(
                treffer=len(d.preparation.hits))
        if d.opened is not None:
            n = len(d.opened.candidates)
            if n:
                return self.tr("{seiten} Seite(n), {n} ohne Textebene: unten aufbereiten").format(
                    seiten=d.opened.page_count, n=n)
            return self.tr("{seiten} Seite(n) mit Textebene").format(seiten=d.opened.page_count)
        if d in self._queue:
            return self.tr("wird geladen …")
        return self.tr("Textdatei")

    def refresh(self) -> None:
        """Refresh every list entry and the status line (count, pages to clean up,
        loading)."""
        for i in range(self.listing.count()):
            entry = self.listing.item(i)
            d = self._document_of(entry)
            if d is not None:
                entry.setText(self._text(d))
        n = len(self.session.documents)
        pending = sum(1 for d in self.session.documents if d.needs_cleanup)
        text = self.tr("{n} Dokument(e)").format(n=n)
        if pending:
            text += self.tr(", {offen} mit Seiten zum Aufbereiten").format(offen=pending)
        if self.is_loading:
            text += self.tr(" – lade …")
        self.status.setText(text)

    # -- loading -----------------------------------------------------------------------------------
    def _notify(self, title: str, text: str) -> None:
        """Warn the user; in the synchronous test mode raise instead of a dialog."""
        if self.synchronous:
            raise RuntimeError(text)
        QMessageBox.warning(self, title, text)

    def _run_next(self) -> None:
        """Open the next queued document (inline in the synchronous test mode, else in a
        worker thread); with the queue empty, refresh both panels and announce it."""
        if not self._queue:
            self.refresh()
            self.clean_up.refresh()
            self.loaded.emit()
            return
        d = self._queue[0]
        self.refresh()
        if self.synchronous:
            try:
                self.session.load(d)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self._error(e, d)
                return
            self._finish(d)
            return
        self._worker = Worker(lambda: self.session.load(d), self)
        self._worker.done.connect(lambda _r, d=d: self._finish(d))
        self._worker.error.connect(lambda e, d=d: self._error(e, d))
        self._worker.start()

    def _finish(self, d: Document) -> None:
        """Document ``d`` is open: take it off the queue and go on."""
        if d in self._queue:
            self._queue.remove(d)
        self._run_next()

    def _error(self, e: Exception, d: Document) -> None:
        """Opening ``d`` failed: remember the error, show it, go on with the queue."""
        d.error = optdoc_adapter.message(e, interface_language())
        if d in self._queue:
            self._queue.remove(d)
        if self.synchronous:
            raise e
        QMessageBox.warning(self, d.path.name, d.error)
        self._run_next()
