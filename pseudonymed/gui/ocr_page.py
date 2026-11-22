"""Step 3: text recognition.  Choose the engine and its language packs, read all
documents, then check and correct the recognised text next to the cleaned-up page.
Only afterwards does the analysis look for identifiers; the review step no longer
edits text."""

from __future__ import annotations

from optdoc import ocr as ocr_module
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

from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.views import PREVIEW_DPI, PageView
from pseudonymed.gui.worker import Worker


class OcrPage(QWidget):
    """The text-recognition step: engine settings, reading with progress, and the
    recognised text of every page next to the cleaned-up image, editable for OCR pages."""

    read_in = Signal()
    proceed = Signal()

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self.is_loading = lambda: False
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        self._total = 0
        self.document: Document | None = None
        self.page = 0
        self._loaded: tuple[int, int] | None = None
        outer = QVBoxLayout(self)
        outer.addWidget(self._build_settings())
        self._engine_changed()
        outer.addWidget(self._build_view(), 1)
        outer.addLayout(self._build_footer())

    # -- building the page -------------------------------------------------------------------------
    def _build_settings(self) -> QGroupBox:
        """The settings group: engine, language packs, table pass, structuring, and the
        row with progress bar and start button."""
        settings = QGroupBox(self.tr("Texterkennung (OCR) für Scans und Fotos"))
        form = QFormLayout(settings)
        self.engine = QComboBox()
        self.engine.addItem(self.tr("automatisch"), "auto")
        for b in ocr_module.available_backends():
            self.engine.addItem(b, b)
        self.engine.addItem(self.tr("aus (nur Textebene)"), "aus")
        self.engine.currentIndexChanged.connect(self._engine_changed)
        form.addRow(self.tr("OCR-Programm"), self.engine)
        self.languages = QLineEdit()
        form.addRow(self.tr("Sprachpakete"), self.languages)
        self.languages_note = QLabel("")
        self.languages_note.setWordWrap(True)
        form.addRow(self.languages_note)
        self.tables = QCheckBox(self.tr(
            "Tabellen: Gitter erkennen und die Zellen ohne Linien in einem zweiten Durchgang"
            " lesen (Tesseract)"))
        self.tables.setChecked(True)
        form.addRow(self.tables)
        self.structured = QCheckBox(self.tr(
            "Erkannten Text gliedern: Absätze zusammenfügen, Überschriften, Listen und"
            " Tabellen als Markdown (für das Sprachmodell)"))
        self.structured.setChecked(self.session.options.structured)
        self.structured.toggled.connect(self._structure_changed)
        form.addRow(self.structured)
        form.addRow(self._build_start_row())
        return settings

    def _build_start_row(self) -> QHBoxLayout:
        """Progress bar, status text and the button that starts reading."""
        line = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.button = QPushButton(self.tr("Texterkennung starten"))
        self.button.setMinimumHeight(32)
        self.button.clicked.connect(self.start_reading)
        line.addWidget(self.progress, 1)
        line.addWidget(self.status, 2)
        line.addWidget(self.button)
        return line

    def _build_view(self) -> QSplitter:
        """Tree of documents and pages, the page image, and the text column."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Dokument / Seite")])
        self.tree.currentItemChanged.connect(self._page_selected)
        splitter.addWidget(self.tree)
        self.view = PageView()
        self.view.mask_mode = True  # nothing is drawn here, drag with modifier pans
        self.view.box_drawn.connect(lambda _r: None)
        splitter.addWidget(self.view)
        splitter.addWidget(self._build_text_column())
        splitter.setSizes([180, 560, 460])
        return splitter

    def _build_text_column(self) -> QWidget:
        """The recognised text with its title, the apply and reset buttons and the note
        whether the page was corrected."""
        right = QWidget()
        r = QVBoxLayout(right)
        r.setContentsMargins(0, 0, 0, 0)
        self.text_title = QLabel(self.tr("Erkannter Text"))
        self.text_title.setWordWrap(True)
        r.addWidget(self.text_title)
        self.text = QPlainTextEdit()
        r.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        self.adopt = QPushButton(self.tr("Text übernehmen"))
        self.adopt.clicked.connect(self.adopt_text)
        self.reset_button = QPushButton(self.tr("Auf OCR-Text zurücksetzen"))
        self.reset_button.clicked.connect(self.reset_text)
        self.text_note = QLabel("")
        for k in (self.adopt, self.reset_button):
            k.setMinimumHeight(32)
            buttons.addWidget(k)
        buttons.addWidget(self.text_note, 1)
        r.addLayout(buttons)
        return right

    def _build_footer(self) -> QHBoxLayout:
        """The note about unread documents and the button to the analysis."""
        bottom = QHBoxLayout()
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.proceed_button = QPushButton(self.tr("Weiter: Analysieren"))
        self.proceed_button.setMinimumHeight(32)
        self.proceed_button.setDefault(True)
        self.proceed_button.clicked.connect(self.proceed.emit)
        bottom.addWidget(self.note, 1)
        bottom.addWidget(self.proceed_button)
        return bottom

    # -- settings ----------------------------------------------------------------------------------
    def _engine_changed(self, _i: int = 0) -> None:
        """Adapt placeholder and note to the chosen engine; the table pass is Tesseract's."""
        name = self.engine.currentData()
        tesseract = name == "tesseract" or (name == "auto"
                                            and ocr_module.available_backends()[:1]
                                            == ["tesseract"])
        self.tables.setEnabled(tesseract)
        if tesseract:
            program = ocr_module.tesseract_program()
            installed = ocr_module.tesseract_languages(program) if program else []
            self.languages.setPlaceholderText("deu+eng")
            self.languages_note.setText(
                self.tr("Installierte Tesseract-Sprachen: {liste}").format(
                    liste=", ".join(installed) or "–"))
        elif name == "aus":
            self.languages.setPlaceholderText("")
            self.languages_note.setText(self.tr("Es wird nur die Textebene der PDFs gelesen."))
        else:
            self.languages.setPlaceholderText("de")
            self.languages_note.setText(self.tr(
                "RapidOCR: Sprachtyp de (Standard), en, latin, ch …; Modelle sind im Paket."))

    def _structure_changed(self, on: bool) -> None:
        """Takes effect at once: the structure was computed when the page was read."""
        self.session.options.structured = on
        for d in self.session.documents:
            for owner in (d.reading, d.preparation):
                if owner is not None and hasattr(owner, "opt"):
                    owner.opt.structured = on
        self._loaded = None
        self.draw()

    def _apply_options(self) -> None:
        """Copy engine, languages, table pass and structuring into the session options."""
        o = self.session.options
        o.ocr = self.engine.currentData()
        o.ocr_languages = self.languages.text().strip()
        o.ocr_tables = self.tables.isChecked()
        o.structured = self.structured.isChecked()

    def _notify(self, title: str, text: str) -> None:
        """Tell the user something; in the synchronous test mode raise instead of a dialog."""
        if self.synchronous:
            raise RuntimeError(text)
        QMessageBox.information(self, title, text)

    # -- reading -----------------------------------------------------------------------------------
    def start_reading(self) -> None:
        """Read every document (text layer, OCR after the clean-up)."""
        if not self.session.documents:
            self._notify(self.tr("Texterkennung"), self.tr("Bitte zuerst Dokumente hinzufügen."))
            return
        if self.is_loading():
            self._notify(self.tr("Texterkennung"),
                         self.tr("Die Dokumente werden noch geladen; bitte einen Moment warten."))
            return
        self._apply_options()
        self._queue = [d for d in self.session.documents if d.readable]
        self._total = len(self._queue)
        self.progress.setRange(0, max(1, self._total))
        self.progress.setValue(0)
        self._run_next()

    def _run_next(self) -> None:
        """Read the next queued document (inline in the synchronous test mode, else in a
        worker thread); with the queue empty, show the tree and announce the result."""
        if not self._queue:
            self.button.setEnabled(True)
            self.progress.setValue(self.progress.maximum())
            self.status.setText(self.tr("Gelesen: {n} Dokument(e).").format(n=self._total))
            self.refresh()
            self.read_in.emit()
            return
        d = self._queue[0]
        self.button.setEnabled(False)
        number = self._total - len(self._queue) + 1
        self.progress.setValue(number - 1)
        self.status.setText(self.tr("Lese {nummer} von {gesamt}: {name} …").format(
            nummer=number, gesamt=self._total, name=d.path.name))
        if self.synchronous:
            try:
                self.session.read(d)
            except Exception as e:  # noqa: BLE001
                self._error(e, d)
                return
            self._finish(d)
            return
        self._worker = Worker(lambda: self.session.read(d), self)
        self._worker.done.connect(lambda _r, d=d: self._finish(d))
        self._worker.error.connect(lambda e, d=d: self._error(e, d))
        self._worker.start()

    def _finish(self, d: Document) -> None:
        """Document ``d`` is read: take it off the queue and go on."""
        if self._queue and self._queue[0] is d:
            self._queue.pop(0)
        self._run_next()

    def _error(self, e: Exception, d: Document) -> None:
        """Reading ``d`` failed: remember the error, show it, go on with the queue."""
        d.error = optdoc_adapter.message(e, interface_language())
        if self.synchronous:
            raise e
        QMessageBox.warning(self, d.path.name, d.error)
        if self._queue and self._queue[0] is d:
            self._queue.pop(0)
        self._run_next()

    # -- navigation --------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the tree of read documents, summarise what is still unread and show
        the first page; without any read document, view and text are emptied."""
        self._fill_tree()
        self.note.setText(self._reading_status())
        if not self._select_first_page():
            self._show_empty()

    def _fill_tree(self) -> None:
        """One node per read document with one child per page, OCR pages marked."""
        self.tree.clear()
        for d in self.session.documents:
            if d.reading is None:
                continue
            node = QTreeWidgetItem([d.path.name])
            node.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
            for s in d.reading.pages:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=s.number + 1)
                                        + (self.tr(" (OCR)") if s.ocr is not None else "")])
                kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), s.number))
                node.addChild(kind)
            self.tree.addTopLevelItem(node)
            node.setExpanded(True)

    def _reading_status(self) -> str:
        """The note at the bottom: how many documents are still unread and how many
        text files need no recognition at all."""
        pending = [d for d in self.session.documents if d.readable and d.reading is None]
        texts = sum(1 for d in self.session.documents if not d.readable)
        parts: list[str] = []
        if pending:
            parts.append(self.tr("{n} Dokument(e) noch nicht gelesen").format(n=len(pending)))
        if texts:
            parts.append(self.tr("{n} Textdatei(en) brauchen keine Texterkennung").format(
                n=texts))
        return " · ".join(parts)

    def _select_first_page(self) -> bool:
        """Make the first page of the first document current; False when the tree is empty."""
        first = self.tree.topLevelItem(0)
        if first is None:
            return False
        kind = first.child(0)
        self.tree.setCurrentItem(first if kind is None else kind)
        return True

    def _show_empty(self) -> None:
        """No document read: no current document, empty view, empty read-only text."""
        self.document = None
        self.view.show_empty()
        self.text.setPlainText("")
        self.text.setReadOnly(True)

    def _page_selected(self, current: QTreeWidgetItem | None,
                       _previous: QTreeWidgetItem | None = None) -> None:
        """Show the page behind the current tree item."""
        if current is None:
            return
        code, number = current.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.session.documents if id(d) == code), None)
        if d is None or d.reading is None:
            return
        self.document = d
        self.page = max(number, 0)
        self.draw()

    def draw(self) -> None:
        """Show the current page as the OCR saw it and its text: editable with title
        and correction note for OCR pages, read-only for pages with a text layer.  The
        text is reloaded only when another page is shown, so an edit survives redraws."""
        if self.document is None or self.document.reading is None:
            return
        reading = self.document.reading
        self.view.scale_factor = PREVIEW_DPI / 72.0
        self.view.display(reading.page_image(self.page, PREVIEW_DPI), [], None)
        key = (id(self.document), self.page)
        if self._loaded != key:
            self.text.setPlainText(reading.text(self.page))
            self._loaded = key
        ocr = reading.has_ocr(self.page)
        self.text.setReadOnly(not ocr)
        self.adopt.setEnabled(ocr)
        self.reset_button.setEnabled(ocr)
        if ocr:
            self.text_title.setText(self.tr(
                "Erkannter Text (OCR, als Markdown gegliedert), von Hand korrigierbar. Die"
                " Korrektur gilt für Textdatei, Markdown und die Textebene; die Schwärzung im"
                " Bild folgt den OCR-Kästen.") if self.structured.isChecked() else self.tr(
                "Erkannter Text (OCR), von Hand korrigierbar. Die Korrektur gilt für Textdatei,"
                " Markdown und die Textebene; die Schwärzung im Bild folgt den OCR-Kästen."))
            s = reading.pages[self.page]
            self.text_note.setText(self.tr("korrigiert") if s.correction is not None
                                   else self.tr("OCR-Text unverändert"))
        else:
            self.text_title.setText(self.tr("Text der Textebene (nicht veränderbar)"))
            self.text_note.setText("")

    # -- corrections -------------------------------------------------------------------------------
    def adopt_text(self) -> None:
        """Store the edited text as the correction of the current OCR page."""
        if self.document is None or self.document.reading is None:
            return
        reading = self.document.reading
        if not reading.has_ocr(self.page):
            return
        reading.set_ocr_text(self.page, self.text.toPlainText())
        self.draw()

    def reset_text(self) -> None:
        """Drop the correction of the current OCR page and show the raw OCR text again."""
        if self.document is None or self.document.reading is None:
            return
        reading = self.document.reading
        ocr = reading.pages[self.page].ocr
        if ocr is None:
            return
        reading.set_ocr_text(self.page, ocr.text)
        self.text.setPlainText(ocr.text)
        self.draw()
