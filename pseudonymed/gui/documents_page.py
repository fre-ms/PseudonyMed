"""Step 4: drop the documents.  They are opened at once: docrepro's chain on every PDF
and image (typdoc binds a photo into a PDF, optdoc suggests how to prepare its pages), so
that the steps after it (preparing, the filters, the areas, the text) can show them."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
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

from pseudonymed.document import docrepro_adapter
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
    """The documents step: the list of inputs with their working state.  Files are opened
    in the background as soon as they arrive."""

    loaded = Signal()  # every pending document is open and, where needed, has a suggestion
    proceed = Signal()  # the user wants to go on to the analysis

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False  # tests run the work inline
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        outer = QVBoxLayout(self)

        intro = QLabel(self.tr(
            "Dateien hierher ziehen oder hinzufügen (PDF, Fotos, Texte, Whisper-JSON, E-Mails)."
            " Scans und Fotos werden in den nächsten Schritten mit docrepro aufbereitet und"
            " gelesen; ein Foto wird dafür zu einem PDF gebunden. Ein gespeicherter"
            " Arbeitsstand (…working-state.json oder das Original daneben) füllt alles bis zur"
            " Sichtprüfung wieder aus."))
        intro.setWordWrap(True)
        outer.addWidget(intro)
        top = QHBoxLayout()
        self.listing = DropZone()
        self.listing.dropped.connect(self.add)
        top.addWidget(self.listing, 1)
        buttons = QVBoxLayout()
        add_button = QPushButton(self.tr("Dateien hinzufügen …"))
        add_button.clicked.connect(self.choose_files)
        remove = QPushButton(self.tr("Entfernen"))
        remove.clicked.connect(self.remove)
        buttons.addWidget(add_button)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        top.addLayout(buttons)
        outer.addLayout(top, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.proceed_button = QPushButton(self.tr("Weiter: Aufbereiten"))
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
            return self._analysed_line(d)
        if d.page_count:
            n = len(d.image_pages)
            if n:
                return self.tr("{seiten} Seite(n), {n} ohne Textebene: aufbereiten").format(
                    seiten=d.page_count, n=n)
            return self.tr("{seiten} Seite(n) mit Textebene").format(seiten=d.page_count)
        if d in self._queue:
            return self.tr("wird geladen …")
        return self.tr("Textdatei")

    def _analysed_line(self, d: Document) -> str:
        """The state of an analysed document: its hits, where they come from (a working
        state, a text), or that a person changed its chain since."""
        assert d.preparation is not None
        hits = len(d.preparation.hits)
        if d.changed:
            return self.tr("geändert: bitte erneut analysieren")
        if d.working_state is not None:
            return self.tr("Arbeitsstand, {treffer} Treffer").format(treffer=hits)
        if d.preparation.is_text:
            return self.tr("Text, {treffer} Treffer").format(treffer=hits)
        return self.tr("analysiert, {treffer} Treffer").format(treffer=hits)

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
        d.error = docrepro_adapter.message(e, interface_language())
        if d in self._queue:
            self._queue.remove(d)
        if self.synchronous:
            raise e
        QMessageBox.warning(self, d.path.name, d.error)
        self._run_next()
