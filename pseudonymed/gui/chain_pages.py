"""Steps 5 to 8: docrepro's panels as steps of PseudonyMed's window.

docrepro prepares and reads the documents (typdoc, optdoc, ocrdoc, joined by its chain,
:mod:`pseudonymed.document.chain`), and its panels are the steps here: optdoc's
preparation of a page (``CleanupPanel``), and ocrdoc's filters for the OCR
(``FiltersPanel``), its areas (``RegionsPanel``) and its text, to be corrected by hand
(``TextPanel``).  Each step shows the documents and their pages on the left and the panel
on the page chosen.  A panel works on the project of the document's step, which is opened
(with the steps before it) on what they give now whenever that changed, with everything
it held, so that docrepro's own rule decides what stays of what a person decided there;
the status line says what went.  Work that takes a while (preparing the pages for the step
after, reading a page, finding its areas, reading every document) runs off the window's
thread, one piece at a time.  The panels speak the interface language (doccor's words).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import ocrdoc.gui.words  # noqa: F401  # registers ocrdoc's words with doccor's
import optdoc.gui.words  # noqa: F401  # registers optdoc's words with doccor's
from doccor.gui.words import Words
from doccor.gui.worker import Worker
from ocrdoc import ocr as ocr_module
from ocrdoc.gui.filters_panel import FiltersPanel
from ocrdoc.gui.regions_panel import RegionsPanel
from ocrdoc.gui.text_panel import TextPanel
from optdoc.gui.cleanup_panel import CleanupPanel
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language, shown, shown_error

# the steps of the window that are docrepro's panels: their step of docrepro
KINDS = {"cleanup": "optdoc", "filters": "ocrdoc", "regions": "ocrdoc", "text": "ocrdoc"}


class ReadingSettings(QGroupBox):
    """The settings ocrdoc reads with (engine, language packs, Tesseract's table pass) and
    PseudonyMed's own structuring of the recognised text, above the filters."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setTitle(self.tr("Texterkennung (OCR) für Scans und Fotos"))
        form = QFormLayout(self)
        self.engine = QComboBox()
        self.engine.addItem(self.tr("automatisch"), "auto")
        for name in ocr_module.available_backends():
            self.engine.addItem(name, name)
        self.engine.addItem(self.tr("aus (nur Textebene)"), "aus")
        self.engine.currentIndexChanged.connect(self._engine_changed)
        form.addRow(self.tr("OCR-Programm"), self.engine)
        self.languages = QLineEdit()
        form.addRow(self.tr("Sprachpakete"), self.languages)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        form.addRow(self.note)
        self.tables = QCheckBox(self.tr(
            "Tabellen: Gitter erkennen und die Zellen ohne Linien in einem zweiten Durchgang"
            " lesen (Tesseract)"))
        self.tables.setChecked(session.options.ocr_tables)
        form.addRow(self.tables)
        self.structured = QCheckBox(self.tr(
            "Erkannten Text gliedern: Absätze zusammenfügen, Überschriften, Listen und"
            " Tabellen als Markdown (für das Sprachmodell)"))
        self.structured.setChecked(session.options.structured)
        self.structured.toggled.connect(self._structure_changed)
        form.addRow(self.structured)
        self._engine_changed()

    def _engine_changed(self, _i: int = 0) -> None:
        """Placeholder and note for the engine chosen; the table pass is Tesseract's."""
        name = self.engine.currentData()
        tesseract = name == "tesseract" or (name == "auto"
                                            and ocr_module.available_backends()[:1]
                                            == ["tesseract"])
        self.tables.setEnabled(tesseract)
        if tesseract:
            program = ocr_module.tesseract_program()
            installed = ocr_module.tesseract_languages(program) if program else []
            self.languages.setPlaceholderText("deu+eng")
            self.note.setText(self.tr("Installierte Tesseract-Sprachen: {liste}").format(
                liste=", ".join(installed) or "–"))
        elif name == "aus":
            self.languages.setPlaceholderText("")
            self.note.setText(self.tr("Es wird nur die Textebene der PDFs gelesen."))
        else:
            self.languages.setPlaceholderText("de")
            self.note.setText(self.tr(
                "RapidOCR: Sprachtyp de (Standard), en, latin, ch …; Modelle sind im Paket."))

    def _structure_changed(self, on: bool) -> None:
        """Takes effect at once: the structure is rendered when the text is shown."""
        self.session.options.structured = on
        for d in self.session.documents:
            if d.preparation is not None:
                d.preparation.opt.structured = on

    def apply(self) -> None:
        """Copy engine, languages, table pass and structuring into the session's options."""
        o = self.session.options
        o.ocr = self.engine.currentData()
        o.ocr_languages = self.languages.text().strip()
        o.ocr_tables = self.tables.isChecked()
        o.structured = self.structured.isChecked()


class ChainStepPage(QWidget):
    """One step of the window that is a panel of docrepro's (``kind``: "cleanup",
    "filters", "regions" or "text"): the documents and their pages, the panel on the page
    chosen, a status line and the way on.  The filters step carries the reading settings
    and reads every document at once."""

    proceed = Signal()  # the user wants to go on to the next step
    read_in = Signal()  # every document was read (the filters step)
    worked = Signal()  # a person changed something in the step

    def __init__(self, session: Session, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.kind = kind
        self.step = KINDS[kind]
        self.synchronous = False  # tests run the work inline
        self.words = Words(interface_language())
        self.document: Document | None = None
        self.page = 0
        self.panel: Any = None
        self._worker: Worker | None = None
        self._pending: tuple[Callable[[], object], Callable[[Any], None], str] | None = None
        self._queue: list[Document] = []
        self._total = 0
        outer = QVBoxLayout(self)
        self.settings = ReadingSettings(session) if kind == "filters" else None
        if self.settings is not None:
            outer.addWidget(self.settings)
            outer.addLayout(self._build_reading_row())
        outer.addWidget(self._build_view(), 1)
        outer.addLayout(self._build_footer())
        self._empty()

    # -- building ------------------------------------------------------------------------------
    def _build_reading_row(self) -> QHBoxLayout:
        """Progress bar, status and the button that reads every document."""
        line = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.read_button = QPushButton(self.tr("Texterkennung starten"))
        self.read_button.setMinimumHeight(32)
        self.read_button.clicked.connect(self.start_reading)
        line.addWidget(self.progress, 1)
        line.addWidget(self.read_button)
        return line

    def _build_view(self) -> QSplitter:
        """The tree of documents and pages, and the panel (a note until there is one)."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Dokument / Seite")])
        self.tree.currentItemChanged.connect(self._page_selected)
        splitter.addWidget(self.tree)
        self.area = QStackedWidget()
        self.placeholder = QLabel("")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.area.addWidget(self.placeholder)
        splitter.addWidget(self.area)
        splitter.setSizes([190, 1010])
        return splitter

    def _build_footer(self) -> QHBoxLayout:
        """The status line and the button to the next step."""
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        titles = {"cleanup": self.tr("Weiter: Filter für die Texterkennung"),
                  "filters": self.tr("Weiter: Bereiche"), "regions": self.tr("Weiter: Text prüfen"),
                  "text": self.tr("Weiter: Analysieren")}
        self.proceed_button = QPushButton(titles[self.kind])
        self.proceed_button.setMinimumHeight(32)
        self.proceed_button.setDefault(True)
        self.proceed_button.clicked.connect(self._go_on)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.proceed_button)
        return bottom

    # -- the documents and their pages ---------------------------------------------------------
    def _documents(self) -> list[Document]:
        """The documents with a chain: PDFs and images that were opened."""
        return [d for d in self.session.documents if d.chain is not None and not d.error]

    def refresh(self) -> None:
        """Rebuild the tree of documents and pages and show the page chosen (the one shown
        before where it is still there, else the first)."""
        kept = (id(self.document), self.page) if self.document is not None else None
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            chosen = None
            for d in self._documents():
                node = self._node(d)
                self.tree.addTopLevelItem(node)
                node.setExpanded(True)
                chosen = chosen or self._item(node, kept)
            first = self.tree.topLevelItem(0)
            chosen = chosen or (first.child(0) if first is not None else None)
            if chosen is not None:
                self.tree.setCurrentItem(chosen)
        finally:
            self.tree.blockSignals(False)
        if chosen is None:
            self.document = None
            self._empty()
            return
        self._page_selected(chosen)

    def _empty(self) -> None:
        """No document to show: say why (none yet, or text files only, which need neither
        preparation nor text recognition)."""
        texts = sum(1 for d in self.session.documents if not d.readable)
        if texts:
            self.placeholder.setText(self.tr(
                "{n} Textdatei(en): sie brauchen weder Aufbereitung noch Texterkennung.").format(
                n=texts))
        else:
            self.placeholder.setText(self.tr(
                "Noch kein Dokument: Scans, Fotos und PDFs im Schritt „Dokumente“ hinzufügen."))
        self.area.setCurrentWidget(self.placeholder)

    def _node(self, d: Document) -> QTreeWidgetItem:
        """A document with a child per page; scans and photos marked."""
        node = QTreeWidgetItem([d.path.name])
        node.setData(0, Qt.ItemDataRole.UserRole, (id(d), 0))
        images = set(d.image_pages)
        for n in range(d.page_count):
            label = self.tr("Seite {nummer}").format(nummer=n + 1)
            if n in images:
                label += self.tr(" (Scan/Foto)")
            child = QTreeWidgetItem([label])
            child.setData(0, Qt.ItemDataRole.UserRole, (id(d), n))
            node.addChild(child)
        return node

    @staticmethod
    def _item(node: QTreeWidgetItem, kept: tuple[int, int] | None) -> QTreeWidgetItem | None:
        """The page item of ``node`` that ``kept`` names (document and page), if any."""
        for k in range(node.childCount()):
            child = node.child(k)
            if child is not None and child.data(0, Qt.ItemDataRole.UserRole) == kept:
                return child
        return None

    def _page_selected(self, current: QTreeWidgetItem | None,
                       _previous: QTreeWidgetItem | None = None) -> None:
        """Show the page behind the current tree item."""
        if current is None:
            return
        code, number = current.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self._documents() if id(d) == code), None)
        if d is not None:
            self.show_page_of(d, number)

    # -- showing a page ------------------------------------------------------------------------
    def show_page_of(self, d: Document, n: int) -> None:
        """Show page ``n`` of ``d`` in the panel, the document's step first brought up to
        what the steps before it give now (off the window's thread)."""
        self.commit()
        self.document, self.page = d, n
        assert d.chain is not None
        if d.chain.current(self.step):
            self._display()
            return
        chain = d.chain
        self._run(lambda: chain.bring(self.step), lambda opened: self._brought(d, opened),
                  self.tr("Der Schritt wird auf das gebracht, was die Schritte davor jetzt"
                          " geben …"))

    def _brought(self, d: Document, opened: Any) -> None:
        """The step is open on what the steps before it give: put it in place, say what
        became of a person's decisions, and show the page."""
        if d.chain is None:
            return
        notes = d.chain.put(opened)
        self.status.setText(" ".join(shown(note) for note in notes))
        if d is self.document:
            self._display()

    def _display(self) -> None:
        """The panel on the page chosen: made the first time, else handed the project."""
        d = self.document
        if d is None or d.chain is None:
            return
        project = d.chain.project(self.step)
        if self.panel is None:
            self.panel = self._make_panel(project)
            if hasattr(self.panel, "changed"):
                self.panel.changed.connect(self._worked_in)
            self.area.addWidget(self.panel)
        elif self.panel.project is not project:
            self.panel.project = project
        self.area.setCurrentWidget(self.panel)
        self.panel.show_page(self.page)

    def _make_panel(self, project: Any) -> Any:
        """docrepro's panel of this step on ``project``, as its adapter makes it, with the
        work it asks for (reading a page, finding its areas) run off the window's thread."""
        if self.kind == "cleanup":
            return CleanupPanel(project, self.words)
        if self.kind == "filters":
            return FiltersPanel(project, self.words)
        if self.kind == "regions":
            return RegionsPanel(project, self.words, self._find_regions)
        return TextPanel(project, self.words, self._read_page)

    def _find_regions(self, n: int, then: Callable[[], None]) -> None:
        """Read page ``n`` and find its areas, then let the panel go on."""
        project = self.panel.project
        self._run(lambda: project.regions(n), lambda _r: then(),
                  self.words("finding_regions", number=n + 1))

    def _read_page(self, n: int, then: Callable[[], None]) -> None:
        """Read page ``n``, then let the panel go on."""
        project = self.panel.project
        self._run(lambda: project.read(n), lambda _r: then(),
                  self.words("reading_page", number=n + 1, total=len(project.pages)))

    def _worked_in(self, *_given: object) -> None:
        """A person changed something in the step: the analysis of the document is to be
        made anew."""
        if self.document is not None:
            self.document.changed = True
        self.worked.emit()

    def commit(self) -> None:
        """Take what was typed into the panel into its project (the corrected text)."""
        if self.panel is not None and hasattr(self.panel, "commit"):
            self.panel.commit()

    def _go_on(self) -> None:
        """Commit the panel and go on to the next step."""
        self.commit()
        self.proceed.emit()

    # -- reading every document ----------------------------------------------------------------
    def start_reading(self) -> None:
        """Read every document with the settings above: ocrdoc reads every page it has not
        read yet; a document read with other settings is read again."""
        assert self.settings is not None
        self.commit()
        self.settings.apply()
        self.session.apply_reading_settings()
        self._queue = list(self._documents())
        self._total = len(self._queue)
        self.progress.setRange(0, max(1, self._total))
        self.progress.setValue(0)
        self._read_next()

    def _read_next(self) -> None:
        """Read the next document of the queue; with the queue empty, show the page again
        and say so."""
        if not self._queue:
            self.progress.setValue(self.progress.maximum())
            self.refresh()
            self.status.setText(self.tr("Gelesen: {n} Dokument(e).").format(n=self._total))
            self.read_in.emit()
            return
        d = self._queue.pop(0)
        self.progress.setValue(self._total - len(self._queue) - 1)
        self._run(lambda: self.session.read(d), lambda _r: self._read_next(),
                  self.tr("Lese {name} …").format(name=d.path.name))

    # -- work off the window's thread ----------------------------------------------------------
    def _run(self, job: Callable[[], object], then: Callable[[Any], None], status: str) -> None:
        """Do ``job``, then ``then`` with its result; one at a time, the latest waits."""
        if self._worker is not None:
            self._pending = (job, then, status)
            return
        self.status.setText(status)
        self._busy(True)
        self._worker = Worker(job, self, inline=self.synchronous)
        self._worker.done.connect(lambda result: self._finished(then, result))
        self._worker.failed.connect(self._failed)
        self._worker.go()

    def _busy(self, busy: bool) -> None:
        """Nothing can be clicked while a piece of work runs."""
        for widget in (self.tree, self.area, self.proceed_button):
            widget.setEnabled(not busy)
        if self.settings is not None:
            self.read_button.setEnabled(not busy)

    def _finished(self, then: Callable[[Any], None], result: Any) -> None:
        """A piece of work is done: the page is free again, ``then`` goes on, and what
        waited starts."""
        self._worker = None
        self._busy(False)
        self.status.setText("")
        then(result)
        self._start_pending()

    def _failed(self, error: Exception) -> None:
        """A piece of work failed: say so in the status line; the queue of documents to read
        stops, what waited starts."""
        self._worker = None
        self._busy(False)
        self._queue = []
        self.status.setText(self.tr("Das ging nicht: {fehler}").format(fehler=shown_error(error)))
        if self.synchronous:
            raise error
        self._start_pending()

    def _start_pending(self) -> None:
        """Start the piece of work that waited, where nothing runs."""
        if self._worker is None and self._pending is not None:
            job, then, status = self._pending
            self._pending = None
            self._run(job, then, status)
