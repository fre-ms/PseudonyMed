"""Step 5: re-check everything, show the traffic light, then write the files.

Entering the step runs the whole finishing phase into a scratch folder for every
analysed document: redaction, the re-check with the patient's identifiers, the
report.  The light sums it up: green (nothing found, ready), yellow (released,
but with warnings worth reading), red (identifiers still found, nothing will be
exported).  The export then copies the checked files to the target folder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.checks.report import as_text
from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language, shown
from pseudonymed.gui.worker import Worker
from pseudonymed.pipeline import Result

TRAFFIC_LIGHT_COLOURS = {"grau": "#9a9a9a", "gruen": "#28a040", "gelb": "#e6a500", "rot": "#c81e1e"}


class ExportPage(QWidget):
    """Step 9: the re-check with its two lights (re-check, residual risk), the output
    options and the export of the checked files."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        self._phase = "pruefen"
        outer = QVBoxLayout(self)
        self._build_traffic_lights(outer)
        self._build_options(outer)
        self._build_output(outer)
        self._set_traffic_light("grau", self.tr("Noch nicht geprüft."))

    # -- layout ------------------------------------------------------------------------------------
    def _build_traffic_lights(self, outer: QVBoxLayout) -> None:
        """The two lights with their texts, the re-check button and the tick box that
        confirms a red residual risk."""
        header = QHBoxLayout()
        header.addWidget(QLabel(self.tr("Erneute Prüfung des Ergebnisses:")))
        self.traffic_light_text = QLabel("")
        self.traffic_light_text.setWordWrap(True)
        header.addWidget(self.traffic_light_text, 1)
        self.traffic_light = QLabel()
        self.traffic_light.setFixedSize(28, 28)
        header.addWidget(self.traffic_light)
        self.again = QPushButton(self.tr("Erneut prüfen"))
        self.again.clicked.connect(self.check)
        header.addWidget(self.again)
        outer.addLayout(header)
        risk = QHBoxLayout()
        risk.addWidget(QLabel(self.tr("Restrisiko (Schätzung aus „8 Kontrolle“):")))
        self.risk_text = QLabel("")
        self.risk_text.setWordWrap(True)
        risk.addWidget(self.risk_text, 1)
        self.risk_traffic_light = QLabel()
        self.risk_traffic_light.setFixedSize(28, 28)
        risk.addWidget(self.risk_traffic_light)
        outer.addLayout(risk)
        self.risk_confirmed = QCheckBox(self.tr(
            "Rotes Restrisiko zur Kenntnis genommen: trotzdem exportieren"))
        self.risk_confirmed.setVisible(False)
        self.risk_confirmed.toggled.connect(lambda _on: self._update_button())
        outer.addWidget(self.risk_confirmed)

    def _build_options(self, outer: QVBoxLayout) -> None:
        """The output group: target folder and the files and features to write."""
        options = QGroupBox(self.tr("Ausgabe"))
        form = QFormLayout(options)
        line = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText(self.tr("leer = neben der Quelle"))
        choose_button = QPushButton("…")
        choose_button.clicked.connect(self.choose_target)
        line.addWidget(self.target, 1)
        line.addWidget(choose_button)
        form.addRow(self.tr("Zielordner"), line)
        self.text_layer = QCheckBox(self.tr(
            "PDF mit Textebene: OCR-Text unsichtbar einbetten (Scans bleiben durchsuchbar)"))
        self.text_layer.setChecked(True)
        self.text_file = QCheckBox(
            self.tr("Textdatei (.txt) in Lesereihenfolge (Eingabe für das Sprachmodell)"))
        self.text_file.setChecked(True)
        self.markdown = QCheckBox(self.tr("Markdown-Datei (.md) zusätzlich"))
        self.report = QCheckBox(self.tr("Prüfbericht (JSON)"))
        self.report.setChecked(True)
        self.notice = QCheckBox(self.tr("Vermerk in den PDF-Metadaten"))
        self.notice.setChecked(True)
        self.rasterise = QCheckBox(self.tr(
            "PDF vollständig rastern (nur Bild; die Textebene wird danach unsichtbar ergänzt)"))
        self.preview = QCheckBox(self.tr("Vorschaubilder immer schreiben"))
        self.vocabulary = QCheckBox(self.tr(
            "Vokabular der verworfenen Rate-Treffer (.vokabular.txt, ein Begriff je Zeile;"
            " in „3 Fachvokabular“ importierbar)"))
        self.vocabulary.setChecked(True)
        for w in (self.text_layer, self.text_file, self.markdown, self.report, self.vocabulary,
                  self.notice, self.rasterise, self.preview):
            form.addRow(w)
        outer.addWidget(options)

    def _build_output(self, outer: QVBoxLayout) -> None:
        """The protocol view, the status line and the buttons (open folder, export)."""
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        outer.addWidget(self.output, 1)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.opener = QPushButton(self.tr("Zielordner öffnen"))
        self.opener.clicked.connect(self.open_folder)
        self.opener.setEnabled(False)
        self.button = QPushButton(self.tr("Exportieren"))
        self.button.clicked.connect(self.run_export)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.opener)
        bottom.addWidget(self.button)
        outer.addLayout(bottom)

    def choose_target(self) -> None:
        """Pick the target folder."""
        folder = QFileDialog.getExistingDirectory(self, self.tr("Zielordner"))
        if folder:
            self.target.setText(folder)

    def _apply_options(self) -> None:
        """Copy the output options into the session's run options."""
        o = self.session.options
        o.target_folder = Path(self.target.text()) if self.target.text().strip() else None
        o.text_file = self.text_file.isChecked()
        o.markdown = self.markdown.isChecked()
        o.redaction.text_layer = self.text_layer.isChecked()
        o.report = self.report.isChecked()
        o.vocabulary = self.vocabulary.isChecked()
        o.redaction.notice = self.notice.isChecked()
        o.redaction.rasterise = self.rasterise.isChecked()
        o.preview = "immer" if self.preview.isChecked() else "auto"

    def _documents_ready_to_check(self) -> list[Document]:
        """The documents analysed and not yet exported."""
        return [d for d in self.session.documents if d.ready_to_check]

    # -- traffic light -----------------------------------------------------------------------------
    def _set_traffic_light(self, colour: str, text: str) -> None:
        """Colour the re-check light, show its text, then refresh risk light and button."""
        self.traffic_light.setStyleSheet(
            f"background: {TRAFFIC_LIGHT_COLOURS[colour]}; border-radius: 14px;"
            " border: 1px solid #555;")
        self.traffic_light.setToolTip(text)
        self.traffic_light_text.setText(text)
        self.colour = colour
        self._set_risk()
        self._update_button()

    def _set_risk(self) -> None:
        """Colour the residual-risk light from the control step's estimate; the confirming
        tick box appears only for red and is cleared otherwise."""
        total, earlier = self.session.control()
        self.risk_colour = total.traffic_light if total is not None else "grau"
        self.risk_traffic_light.setStyleSheet(
            f"background: {TRAFFIC_LIGHT_COLOURS[self.risk_colour]}; border-radius: 14px;"
            " border: 1px solid #555;")
        if total is None:
            self.risk_text.setText(self.tr("noch nicht geschätzt"))
        else:
            self.risk_text.setText(total.short(interface_language()) + (
                self.tr(" (mit {n} früheren Dokumenten)").format(n=earlier) if earlier else ""))
        self.risk_confirmed.setVisible(self.risk_colour == "rot")
        if self.risk_colour != "rot":
            self.risk_confirmed.setChecked(False)

    def _update_button(self) -> None:
        """Enable the export button only for green or yellow, and for a red residual risk
        only once it is confirmed."""
        free = self.colour in ("gruen", "gelb")
        red = getattr(self, "risk_colour", "grau") == "rot"
        if red and not self.risk_confirmed.isChecked():
            free = False
        self.button.setEnabled(free)

    def refresh(self) -> None:
        """Called when the step is shown: run the re-check for everything that needs it."""
        colour, text = self.session.traffic_light()
        self._set_traffic_light(colour, shown(text))
        if colour == "grau" and any(d.ready_to_check and not d.precheck_current
                                    for d in self.session.documents):
            self.check()

    def check(self) -> None:
        """Finishing phase into the scratch folder for every analysed document."""
        if self._queue:
            return
        self._apply_options()
        self._phase = "pruefen"
        pending = self._documents_ready_to_check()
        if pending:
            self._start_check(pending)

    def _start_check(self, documents: list[Document]) -> None:
        """Queue these documents for the re-check, clear the protocol and set the light."""
        self._phase = "pruefen"
        self._queue = list(documents)
        self.output.clear()
        self._set_traffic_light("grau", self.tr("Erneute Prüfung läuft …"))
        self._run_next()

    def _after_check(self) -> None:
        """Show the outcome of the re-check: the light, the status line and, for red, the
        residual hits per document."""
        colour, text = self.session.traffic_light()
        self._set_traffic_light(colour, shown(text))
        if colour == "rot":
            self._notify_abort()
        elif colour == "gelb":
            self.status.setText(self.tr("Bereit; bitte die Warnungen lesen."))
        elif colour == "gruen":
            self.status.setText(self.tr("Bereit zum Export."))

    def _notify_abort(self) -> None:
        """Status line and, outside synchronous mode, a dialog: the re-check still found
        patient data, nothing will be exported, with the residual hits per document."""
        message = self.tr(
            "Abbruch: Bei der erneuten Prüfung wurden noch Daten aus „1 Patientendaten“"
            " gefunden."
            " Es wird nichts exportiert. Bitte im Prüfschritt nacharbeiten (Kasten oder"
            " Maske) und erneut prüfen.") + "\n\n" + "\n".join(self._remaining_match_lines())
        self.status.setText(message.split("\n")[0])
        if not self.synchronous:
            QMessageBox.critical(self, self.tr("Export nicht möglich"), message)

    def _remaining_match_lines(self) -> list[str]:
        """One line per document that failed the re-check: its name, the number of
        residual hits and the first of them with page and rule."""
        lines = []
        for d in self.session.documents:
            if not d.ready_to_check or d.precheck is None or d.precheck.released:
                continue
            g = d.precheck.report.recheck
            remaining = g.residual_matches if g is not None else []
            short = ", ".join(f"Seite {r['seite']}: {r['regel']}" for r in remaining[:8])
            lines.append(f"{d.path.name}: {len(remaining)} Resttreffer ({short})")
        return lines

    # -- export ------------------------------------------------------------------------------------
    def run_export(self) -> None:
        """Write the checked files: re-check first when something changed since the last
        check, refuse while the re-check failed or a red residual risk is unconfirmed."""
        self._apply_options()
        pending = self._documents_ready_to_check()
        if not pending:
            self._nothing_to_export()
            return
        if any(not d.precheck_current for d in pending):
            self._start_check(pending)  # something changed since the check: check again first
            return
        if not self._release_granted(pending):
            return
        self._phase = "exportieren"
        self._queue = list(pending)
        self._run_next()

    def _nothing_to_export(self) -> None:
        """Tell the user that no analysed document is waiting; synchronous mode raises."""
        if self.synchronous:
            raise RuntimeError("Keine analysierten Dokumente.")
        QMessageBox.information(self, self.tr("Export"),
                                self.tr("Keine analysierten Dokumente."))

    def _release_granted(self, pending: list[Document]) -> bool:
        """True when every re-check passed and a red residual risk, if any, is confirmed;
        otherwise the page says why and synchronous mode raises."""
        if any(d.precheck is None or not d.precheck.released for d in pending):
            self._after_check()
            if self.synchronous:
                raise RuntimeError("Erneute Prüfung nicht bestanden; nichts exportiert.")
            return False
        self._set_risk()
        if self.risk_colour == "rot" and not self.risk_confirmed.isChecked():
            self.status.setText(self.tr("Rotes Restrisiko: bitte in „8 Kontrolle“ nacharbeiten"
                                        " oder den Export ausdrücklich bestätigen."))
            if self.synchronous:
                raise RuntimeError("Rotes Restrisiko nicht bestätigt; nichts exportiert.")
            return False
        return True

    def _run_next(self) -> None:
        """Check or export the next queued document (in a worker thread unless synchronous)
        or, with the queue empty, finish the phase."""
        if not self._queue:
            if self._phase == "pruefen":
                self._after_check()
            else:
                self.status.setText(self.tr("Fertig."))
                self.opener.setEnabled(True)
                colour, text = self.session.traffic_light()
                self._set_traffic_light(colour, shown(text))
            self.again.setEnabled(True)
            return
        d = self._queue.pop(0)
        self.button.setEnabled(False)
        self.again.setEnabled(False)
        check = self._phase == "pruefen"
        self.status.setText((self.tr("Prüfe {name} …") if check
                             else self.tr("Exportiere {name} …")).format(name=d.path.name))
        if check:
            task = lambda: self.session.check(d)  # noqa: E731
        else:
            task = lambda: self.session.export(d)  # noqa: E731
        if self.synchronous:
            try:
                self._finish(task())
            except Exception as e:  # noqa: BLE001
                self._error(e, d)
            return
        self._worker = Worker(task, self)
        self._worker.done.connect(self._finish)
        self._worker.error.connect(lambda e, d=d: self._error(e, d))
        self._worker.start()

    def _finish(self, result: Result) -> None:
        """Append the document's report and outcome to the protocol, then go on."""
        text = as_text(result.report, interface_language())
        if self._phase == "pruefen":
            text += (self.tr("Erneute Prüfung bestanden; Export möglich.") if result.released
                     else self.tr("Erneute Prüfung NICHT bestanden; Export gesperrt.")) + "\n"
        elif result.released:
            text += "".join(line + "\n" for line in self._release_lines(result))
        else:
            text += self.tr("NICHT freigegeben.") + "\n"
        self.output.appendPlainText(text + "\n")
        self._run_next()

    def _release_lines(self, result: Result) -> list[str]:
        """The files written for a released document, one line each."""
        lines = [self.tr("Freigegeben: {pfad}").format(pfad=result.target)]
        if result.target_txt and not result.is_text:
            lines.append(self.tr("Text: {pfad}").format(pfad=result.target_txt))
        if result.target_md:
            lines.append(self.tr("Markdown: {pfad}").format(pfad=result.target_md))
        if result.target_vocabulary:
            lines.append(self.tr("Vokabular: {pfad}").format(pfad=result.target_vocabulary))
        if result.target_capsule:
            lines.append(self.tr("Schlüsselkapsel (nur an den Empfänger geben): {pfad}").format(
                pfad=result.target_capsule))
        if result.preview:
            lines.append(self.tr("Vorschau: {pfad}").format(pfad=result.preview[0].parent))
        return lines

    def _error(self, e: Exception, d: Document) -> None:
        """Note the error on the document and in the protocol, then go on."""
        d.error = optdoc_adapter.message(e, interface_language())
        self.output.appendPlainText(
            self.tr("{name}: Fehler: {fehler}").format(name=d.path.name, fehler=d.error) + "\n")
        self._run_next()

    def open_folder(self) -> None:
        """Open the target folder (or the first document's folder) in the file manager."""
        folder = self.session.options.target_folder
        if folder is None and self.session.documents:
            folder = self.session.documents[0].path.parent
        if folder is None:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
