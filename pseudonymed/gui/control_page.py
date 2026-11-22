"""Step 7: control before export.

Two questions, two lights.  First the re-check: are all identifiers from step 1 gone
from the result (the finishing phase runs into a scratch folder, exactly as the export
will)?  Second the residual-risk estimate: how many people share what the text still
says (see :mod:`pseudonymed.checks.residual_risk`)?  Documents of the same patient are
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

from pseudonymed.checks import residual_risk as risk_module
from pseudonymed.checks.report import as_text
from pseudonymed.config import Settings, load_settings, save_settings
from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.export_page import TRAFFIC_LIGHT_COLOURS
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.worker import Worker
from pseudonymed.pipeline import Result


class ControlPage(QWidget):
    """Step "8 Kontrolle": the re-check light, the residual-risk light, one table row per
    re-checked document and, for the selected row, the counted features and the report."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        """Build the page region by region and start with both lights grey."""
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        e = load_settings()
        session.options.reference_population = e.reference_population
        session.options.own_places = e.own_places
        outer = QVBoxLayout(self)
        self._build_check_row(outer)
        self._build_risk_row(outer)
        self._build_reference_row(outer, e)
        self._build_tables(outer)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self._set_traffic_light(self.traffic_light, "grau")
        self._set_traffic_light(self.risk_traffic_light, "grau")

    # -- build -------------------------------------------------------------------------------------
    def _build_check_row(self, outer: QVBoxLayout) -> None:
        """Light 1: the question, its answer text, the light and the re-check button."""
        line = QHBoxLayout()
        line.addWidget(QLabel(self.tr(
            "Erneute Prüfung: alle Angaben aus „1 Patientendaten“ entfernt?")))
        self.traffic_light_text = QLabel("")
        self.traffic_light_text.setWordWrap(True)
        line.addWidget(self.traffic_light_text, 1)
        self.traffic_light = QLabel()
        self.traffic_light.setFixedSize(28, 28)
        line.addWidget(self.traffic_light)
        self.again = QPushButton(self.tr("Erneut prüfen"))
        self.again.clicked.connect(self.check)
        line.addWidget(self.again)
        outer.addLayout(line)

    def _build_risk_row(self, outer: QVBoxLayout) -> None:
        """Light 2: the residual-risk question, its summary, the light and the explanation
        of the estimate underneath."""
        line = QHBoxLayout()
        line.addWidget(QLabel(self.tr("Restrisiko: wie viele Menschen teilen, was im Text"
                                      " bleibt?")))
        self.risk_text = QLabel("")
        self.risk_text.setWordWrap(True)
        line.addWidget(self.risk_text, 1)
        self.risk_traffic_light = QLabel()
        self.risk_traffic_light.setFixedSize(28, 28)
        line.addWidget(self.risk_traffic_light)
        outer.addLayout(line)
        note = QLabel(self.tr(
            "Schätzung nach dem k-map-Prinzip (El Emam, EMA Policy 0070, Rocher et al.): Referenz"
            " ÷ Produkt der Häufigkeiten von Geschlecht, Geburtsjahr, kleinstem genanntem Ort"
            " (Destatis) und seltenster genannter Erkrankung (Orphanet). Grün ab 11 gleichartigen"
            " Personen (κ ≤ 0,09), Gelb ab 5, Rot darunter. Eigene Synthese, kein Standard; die"
            " Unabhängigkeitsannahme ist bei untypischen Kombinationen zu optimistisch."))
        note.setWordWrap(True)
        outer.addWidget(note)

    def _build_reference_row(self, outer: QVBoxLayout, e: Settings) -> None:
        """The two parameters of the estimate (reference population, own places) and the
        button that recalculates with them."""
        line = QHBoxLayout()
        line.addWidget(QLabel(self.tr("Referenzpopulation")))
        self.reference = QLineEdit(str(e.reference_population))
        self.reference.setMaximumWidth(120)
        self.reference.setToolTip(self.tr(
            "Aus wie vielen Menschen ein Angreifer wählt: Deutschland 83.600.000, oder das"
            " Einzugsgebiet der Sprechstunde, wenn der Absender bekannt ist"))
        line.addWidget(self.reference)
        line.addWidget(QLabel(self.tr("Eigene Orte (nicht gezählt)")))
        self.own_places = QLineEdit(e.own_places)
        self.own_places.setPlaceholderText(self.tr("Ort der Sprechstunde, Klinik … (Komma)"))
        line.addWidget(self.own_places, 1)
        new = QPushButton(self.tr("Neu berechnen"))
        new.clicked.connect(self.recalculate)
        line.addWidget(new)
        outer.addLayout(line)

    def _build_tables(self, outer: QVBoxLayout) -> None:
        """The document table above, the feature table and the report text below, in a
        vertical splitter."""
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            self.tr("Dokument"), self.tr("Erneute Prüfung"), self.tr("λ (Personen)"),
            self.tr("κ"), self.tr("Restrisiko"), self.tr("Größter Beitrag")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_details)
        splitter.addWidget(self.table)
        bottom = QWidget()
        ul = QHBoxLayout(bottom)
        ul.setContentsMargins(0, 0, 0, 0)
        self.features = QTableWidget(0, 3)
        self.features.setHorizontalHeaderLabels([self.tr("Merkmal"), self.tr("Bit"),
                                                 self.tr("Beleg")])
        self.features.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.features.verticalHeader().setVisible(False)
        self.features.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        ul.addWidget(self.features, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        ul.addWidget(self.log, 1)
        splitter.addWidget(bottom)
        outer.addWidget(splitter, 1)

    # -- lights ------------------------------------------------------------------------------------
    @staticmethod
    def _set_traffic_light(label: QLabel, colour: str) -> None:
        """Paint the round light in the given colour and remember the colour on the label."""
        label.setStyleSheet(
            f"background: {TRAFFIC_LIGHT_COLOURS[colour]}; border-radius: 14px;"
            " border: 1px solid #555;")
        label.setProperty("farbe", colour)

    @property
    def colour(self) -> str:
        """Colour of the re-check light: grau, gruen, gelb or rot."""
        return self.traffic_light.property("farbe") or "grau"

    @property
    def risk_colour(self) -> str:
        """Colour of the residual-risk light: grau, gruen, gelb or rot."""
        return self.risk_traffic_light.property("farbe") or "grau"

    def refresh(self) -> None:
        """Called when the step is shown: run the re-check for what needs it, then fill in."""
        if any(d.ready_to_check and not d.precheck_current for d in self.session.documents):
            self.check()
        else:
            self._render()

    def recalculate(self) -> None:
        """New reference population or own places: store them and run the check again."""
        try:
            reference = int(self.reference.text().replace(".", "").replace(" ", "") or 0)
        except ValueError:
            reference = 0
        if reference < 100:
            self.status.setText(self.tr("Referenzpopulation: bitte eine Zahl ab 100."))
            return
        e = load_settings()
        e.reference_population, e.own_places = reference, self.own_places.text().strip()
        try:
            save_settings(e)
        except OSError:
            pass
        self.session.options.reference_population = reference
        self.session.options.own_places = e.own_places
        for d in self.session.documents:
            d.precheck_version = -1  # forces a new pre-check
        self.check()

    def check(self) -> None:
        """Re-check every document that is ready for it, one after the other; a run that
        is already under way is left alone."""
        if self._queue:
            return
        self._queue = [d for d in self.session.documents if d.ready_to_check]
        if not self._queue:
            self._render()
            return
        self._set_traffic_light(self.traffic_light, "grau")
        self.traffic_light_text.setText(self.tr("Erneute Prüfung läuft …"))
        self._run_next()

    def _run_next(self) -> None:
        """Take the next document off the queue and re-check it, in a worker thread or,
        in the synchronous test mode, right here; an empty queue ends the run."""
        if not self._queue:
            self.again.setEnabled(True)
            self._render()
            return
        d = self._queue.pop(0)
        self.again.setEnabled(False)
        self.status.setText(self.tr("Prüfe {name} …").format(name=d.path.name))
        if self.synchronous:
            try:
                self.session.check(d)
            except Exception as e:  # noqa: BLE001
                d.error = optdoc_adapter.message(e, interface_language())
            self._run_next()
            return
        self._worker = Worker(partial(self.session.check, d), self)
        self._worker.done.connect(lambda _e: self._run_next())
        self._worker.error.connect(partial(self._check_failed, d))
        self._worker.start()

    def _check_failed(self, d: Document, e: object) -> None:
        """Record the worker's exception on the document and carry on with the queue."""
        d.error = optdoc_adapter.message(e, interface_language()) if isinstance(e, BaseException) \
            else str(e)
        self._run_next()

    # -- display -----------------------------------------------------------------------------------
    def _render(self) -> None:
        """Fill both lights and the document table, then the details of the selected row."""
        colour, text = self.session.traffic_light()
        self._set_traffic_light(self.traffic_light, colour)
        self.traffic_light_text.setText(self.tr(text))
        self._show_residual_risk()
        if self._fill_table() and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._show_details()
        self.status.setText("")

    def _show_residual_risk(self) -> None:
        """Light 2: the joint estimate over all documents of the patient, grey without one."""
        total, earlier = self.session.control()
        if total is None:
            self._set_traffic_light(self.risk_traffic_light, "grau")
            self.risk_text.setText(self.tr("Noch kein geprüftes Dokument."))
            return
        self._set_traffic_light(self.risk_traffic_light, total.traffic_light)
        self.risk_text.setText(self._risk_sentence(total, earlier))

    def _risk_sentence(self, total: risk_module.ResidualRisk, earlier: int) -> str:
        """The summary sentence of the joint estimate: figures and threshold, the largest
        contribution if there is one, the number of earlier released documents if any."""
        dominant = total.dominant
        sentence = self.tr("Alle Dokumente dieses Patienten zusammen: {kurz}; {schwelle}.").format(
            kurz=total.short(), schwelle=risk_module.traffic_light_text(total.traffic_light))
        if dominant:
            sentence += self.tr(" Größter Beitrag: {merkmal} ({bits:.1f} Bit).").format(
                merkmal=dominant.description, bits=dominant.bits)
        if earlier:
            sentence += self.tr(" Einschließlich {n} früher freigegebener Dokument(e).").format(
                n=earlier)
        return sentence

    def _fill_table(self) -> int:
        """One row per re-checked document, every cell tagged with the document's id so
        that the details can find it again.  Returns the number of rows."""
        checked = [(d, d.precheck) for d in self.session.documents
                   if d.precheck is not None]
        self.table.setRowCount(len(checked))
        for i, (d, e) in enumerate(checked):
            for j, w in enumerate(self._table_row(d, e)):
                entry = QTableWidgetItem(w)
                entry.setData(Qt.ItemDataRole.UserRole, id(d))
                self.table.setItem(i, j, entry)
        return len(checked)

    def _table_row(self, d: Document, e: Result) -> list[str]:
        """The six cells of a document's row: name, re-check verdict, λ, κ, the light and
        the largest contribution; dashes where the result carries no estimate."""
        verdict = self.tr("bestanden") if e.released else self.tr("NICHT bestanden")
        r = e.residual_risk
        if r is None:
            return [d.path.name, verdict, "–", "–", "–", "–"]
        contribution = (f"{r.dominant.description} ({r.dominant.bits:.1f} Bit)"
                        if r.dominant else "–")
        return [d.path.name, verdict, risk_module._number(r.lam), f"{r.kappa:.2f}",
                r.traffic_light.upper(), contribution]

    def _show_details(self) -> None:
        """Details of the selected row: the counted features with their evidence and the
        full report text; both empty without a selection."""
        d = self._selected_document()
        if d is None or d.precheck is None:
            self.features.setRowCount(0)
            self.log.clear()
            return
        lines = self._feature_rows(d.precheck.residual_risk)
        self.features.setRowCount(len(lines))
        for i, line in enumerate(lines):
            for j, w in enumerate(line):
                self.features.setItem(i, j, QTableWidgetItem(w))
        self.log.setPlainText(as_text(d.precheck.report))

    def _selected_document(self) -> Document | None:
        """The session document behind the selected table row, found by the id stored in
        the row's first cell; None without a selection."""
        line = self.table.currentRow()
        entry = self.table.item(line, 0) if line >= 0 else None
        if entry is None:
            return None
        code = entry.data(Qt.ItemDataRole.UserRole)
        return next((d for d in self.session.documents if id(d) == code), None)

    def _feature_rows(self, r: risk_module.ResidualRisk | None) -> list[tuple[str, str, str]]:
        """Rows of the feature table: counted features first, each by bits, then the ones
        only listed, then the markers prefixed with "!"; nothing without an estimate."""
        if r is None:
            return []
        features = [(m.description + ("" if m.counted else self.tr(" (nicht gezählt)")),
                     f"{m.bits:.1f}", m.evidence)
                    for m in sorted(r.features, key=lambda m: (-m.counted, -m.bits))]
        markers = [("! " + mk.text, "", mk.evidence) for mk in r.markers]
        return features + markers
