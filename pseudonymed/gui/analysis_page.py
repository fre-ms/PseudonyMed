"""Step 3: how the documents are read, then the analysis itself with visible progress."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.config import Settings, load_settings, save_settings
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import ner as ner_module
from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language, shown, shown_error
from pseudonymed.gui.worker import Worker
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.store import UncertainMatch


class AnalysisPage(QWidget):
    """Step 6: the detection options (including the four medical levels), the recipient
    for pass-on pseudonyms and the analysis run, document by document with progress."""

    analysed = Signal()

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self.is_loading = lambda: False  # the main window tells whether documents are still loading
        self._worker: Worker | None = None
        self._queue: list[Document] = []
        self._total = 0
        self.certificate: pgp_module.Certificate | None = None
        outer = QVBoxLayout(self)
        self._build_detection(outer)
        self._build_vocabulary(outer)
        self._build_recipient(outer)
        self._build_documents(outer)
        self._build_buttons(outer)

    # -- layout ------------------------------------------------------------------------------------
    def _build_detection(self, outer: QVBoxLayout) -> None:
        """The detection options: date form, codes, unreadable lines, medical terms,
        identifiers, the four medical levels, fuzzy search and NER."""
        options = QGroupBox(self.tr("Erkennung der Identifikatoren"))
        form = QFormLayout(options)
        self.date = QComboBox()
        self.date.addItem(self.tr("Geburtsjahr [geb. 1990]"), "jahr")
        self.date.addItem(self.tr("Alter [Alter 36]"), "alter")
        form.addRow(self.tr("Geburtsdatum als"), self.date)
        self.codes = QCheckBox(self.tr("Barcodes und QR-Codes schwärzen"))
        self.codes.setChecked(True)
        form.addRow(self.codes)
        self.unreadable = QCheckBox(self.tr("Unlesbare OCR-Zeilen schwärzen"))
        self.unreadable.setChecked(True)
        form.addRow(self.unreadable)
        self.medical_terms = QCheckBox(
            self.tr("Fachbegriffe markieren und vor NER und unscharfer OCR-Suche schützen"))
        self.medical_terms.setChecked(True)
        form.addRow(self.medical_terms)
        self.identifiers = QCheckBox(self.tr(
            "Identifikatoren schwärzen: Kliniken, Berufe, Studien, Titel"
            " (siehe „2 Identifikatoren“)"))
        self.identifiers.setChecked(True)
        form.addRow(self.identifiers)
        self._build_medical_levels(form)
        self.fuzzy = QCheckBox(
            self.tr("Textdateien: abweichend geschriebene Namen unscharf suchen (Transkripte)"))
        self.fuzzy.setChecked(True)
        form.addRow(self.fuzzy)
        self.ner = QCheckBox(self.tr("Unbekannte Personen und Orte per NER suchen (rät)"))
        self.ner.setEnabled(ner_module.available())
        self.ner.setChecked(ner_module.available())
        if not ner_module.available():
            self.ner.setToolTip(
                self.tr("Extra 'ner' und ein deutsches spaCy-Modell installieren."))
        form.addRow(self.ner)
        outer.addWidget(options)

    def _build_medical_levels(self, form: QFormLayout) -> None:
        """Medical content in four levels, each on its own, all off by default: substance
        and dosage of medications, parameter and value of lab results."""
        self.drugs = QCheckBox(self.tr(
            "Medikamente: Substanz schwärzen (Wirkstoff, Handelsname)"))
        self.drugs.setToolTip(self.tr(
            "Medikamentennamen aus dem Lexikon (Wirkstoffe, einige Handelsnamen) und alle"
            " Begriffe der Kategorie „Medikament“ in „3 Fachvokabular“; Dosierung und Schema"
            " bleiben stehen. Standardmäßig aus: medizinischer Inhalt."))
        form.addRow(self.drugs)
        self.doses = QCheckBox(self.tr(
            "Medikamente: Dosierung und Verordnungsschema schwärzen"))
        self.doses.setToolTip(self.tr(
            "Was neben einer Substanz steht: Menge und Einheit, Einnahmeschema, Zeiten,"
            " Applikationsweg („500 mg 1-0-1“, „2 Tbl.“, „bei Bedarf“, „p. o.“); die Substanz"
            " selbst bleibt stehen, wenn sie nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.doses)
        self.lab_tests = QCheckBox(self.tr(
            "Laborwerte: Untersuchung schwärzen (Parameter)"))
        self.lab_tests.setToolTip(self.tr(
            "Der Name der Untersuchung, wo immer er steht („TSH“, „Kreatinin“), aus der"
            " Laborliste des Lexikons und der Kategorie „Laborwert“ in „3 Fachvokabular“; der"
            " Messwert bleibt stehen, wenn er nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.lab_tests)
        self.measurements = QCheckBox(self.tr("Laborwerte: Messwert schwärzen"))
        self.measurements.setToolTip(self.tr(
            "Der Wert direkt hinter einer Untersuchung („2,3 mU/l“ in „TSH 2,3 mU/l“, auch"
            " „< 5 mg/l“ oder „von 6,2 %“); die Untersuchung selbst bleibt stehen, wenn sie"
            " nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.measurements)

    def _build_vocabulary(self, outer: QVBoxLayout) -> None:
        """The line that says how many terms the vocabulary protects."""
        vocabulary = QGroupBox(self.tr("Fachvokabular (geschützt vor NER und unscharfer Suche)"))
        vocabulary_row = QHBoxLayout(vocabulary)
        self.vocabulary_info = QLabel("")
        self.vocabulary_info.setWordWrap(True)
        vocabulary_row.addWidget(self.vocabulary_info, 1)
        outer.addWidget(vocabulary)

    def _build_recipient(self, outer: QVBoxLayout) -> None:
        """The recipient group: tick box, key source with check button, the fingerprint
        line; disabled without an OpenPGP engine."""
        sharing = QGroupBox(self.tr("Empfänger (Pseudonyme zum Weitergeben)"))
        wf = QFormLayout(sharing)
        self.sharing = QCheckBox(self.tr(
            "Für einen Empfänger pseudonymisieren: Einmalschlüssel, die Zuordnung steht nur in"
            " einer verschlüsselten Kapsel für den Empfänger; lokal wird nichts gespeichert"))
        wf.addRow(self.sharing)
        line = QHBoxLayout()
        self.recipient_source = QLineEdit()
        self.recipient_source.setPlaceholderText(self.tr(
            "Öffentlicher Schlüssel: Datei (.asc) oder https-Adresse"))
        choose_button = QPushButton("…")
        choose_button.clicked.connect(self.choose_recipient)
        self.recipient_button = QPushButton(self.tr("Schlüssel prüfen"))
        self.recipient_button.clicked.connect(self.check_recipient)
        line.addWidget(self.recipient_source, 1)
        line.addWidget(choose_button)
        line.addWidget(self.recipient_button)
        wf.addRow(line)
        self.recipient_info = QLabel("")
        self.recipient_info.setWordWrap(True)
        self.recipient_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        wf.addRow(self.recipient_info)
        settings = load_settings()
        self.recipient_source.setText(settings.recipient_key)
        if not pgp_module.available():
            sharing.setEnabled(False)
            sharing.setToolTip(self.tr("Extra 'pgp' (pysequoia) oder GnuPG installieren."))
        outer.addWidget(sharing)

    def _build_documents(self, outer: QVBoxLayout) -> None:
        """The list of the session's documents with their state, and the progress bar."""
        outer.addWidget(QLabel(self.tr("Dokumente dieser Sitzung:")))
        self.listing = QListWidget()
        outer.addWidget(self.listing, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)

    def _build_buttons(self, outer: QVBoxLayout) -> None:
        """The status line and the two buttons: analyse, analyse again."""
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.button = QPushButton(self.tr("Analysieren"))
        self.button.setDefault(True)
        self.button.clicked.connect(self.analyse)
        self.again_button = QPushButton(self.tr("Erneut analysieren"))
        self.again_button.setToolTip(self.tr(
            "Auch schon analysierte Dokumente mit den jetzigen Einstellungen neu durchsuchen."
            " Aufbereitung, Texterkennung und Korrekturen bleiben; Kästen, Masken,"
            " Aufnahmen und verworfene Treffer werden übernommen."))
        self.again_button.clicked.connect(lambda: self.analyse(again=True))
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.button)
        bottom.addWidget(self.again_button)
        outer.addLayout(bottom)

    # -- state -----------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Refresh the document list with each document's state and the vocabulary line."""
        self.listing.clear()
        for d in self.session.documents:
            self.listing.addItem(f"{d.path.name}  –  {shown(d.state)}")
        self._show_vocabulary()

    # -- vocabulary --------------------------------------------------------------------------------
    def _show_vocabulary(self) -> None:
        """Say how many terms are protected and from which sources."""
        fb = terms_module.standard()
        sources = ", ".join(f"{name} {n}" for name, n in fb.sources)
        self.vocabulary_info.setText(self.tr(
            "{gesamt} geschützte Begriffe ({quellen}). Ansehen, ergänzen und importieren unter"
            " „3 Fachvokabular“.").format(gesamt=fb.count, quellen=sources))

    def _apply_options(self) -> None:
        """Copy the ticked options into the session's run options."""
        o = self.session.options
        o.date_format = self.date.currentData()
        o.codes = self.codes.isChecked()
        o.unreadable = "schwaerzen" if self.unreadable.isChecked() else "lassen"
        o.ner = self.ner.isChecked()
        o.medical_terms = self.medical_terms.isChecked()
        o.identifiers = self.identifiers.isChecked()
        o.redact_drugs = self.drugs.isChecked()
        o.redact_doses = self.doses.isChecked()
        o.redact_lab_tests = self.lab_tests.isChecked()
        o.redact_measurements = self.measurements.isChecked()
        o.fuzzy_text = self.fuzzy.isChecked()

    def _notify(self, title: str, text: str) -> None:
        """Show an information box; synchronous mode (tests) raises instead of blocking."""
        if self.synchronous:
            raise RuntimeError(text)
        QMessageBox.information(self, title, text)

    # -- recipient ---------------------------------------------------------------------------------
    def choose_recipient(self) -> None:
        """Pick the recipient's public key file and check it at once."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Öffentlicher Schlüssel des Empfängers"), "",
            self.tr("OpenPGP-Schlüssel (*.asc *.gpg *.pgp);;Alle Dateien (*)"))
        if path:
            self.recipient_source.setText(path)
            self.check_recipient()

    def check_recipient(self) -> pgp_module.Certificate | None:
        """Load the key, show fingerprint and names, pin the fingerprint in the settings
        and warn when a key at the same address changed."""
        source = self.recipient_source.text().strip()
        if not source:
            self.recipient_info.setText(self.tr("Bitte Schlüsseldatei oder Adresse angeben."))
            self.certificate = None
            return None
        try:
            z = pgp_module.load_certificate(source)
        except pgp_module.PGPError as e:
            self.certificate = None
            self.recipient_info.setText(self.tr("Schlüssel nicht brauchbar: {fehler}").format(
                fehler=shown_error(e)))
            return None
        settings = load_settings()
        warning = self._fingerprint_warning(settings, source, z)
        settings.recipient_key = source
        settings.recipient_fingerprint = z.fingerprint
        try:
            save_settings(settings)
        except OSError:
            pass
        self.certificate = z
        self.recipient_info.setText(self.tr(
            "Empfänger: {beschreibung}. Fingerabdruck mit dem Empfänger auf einem zweiten Weg"
            " abgleichen (Webseite, Briefkopf, Telefon).").format(
                beschreibung=z.description(interface_language()))
            + warning)
        return z

    def _fingerprint_warning(self, remembered: Settings, source: str,
                             z: pgp_module.Certificate) -> str:
        """The warning when the key at the pinned address no longer has the pinned
        fingerprint; an empty string otherwise."""
        if remembered.recipient_key == source and remembered.recipient_fingerprint \
                and remembered.recipient_fingerprint != z.fingerprint:
            return self.tr(" ACHTUNG: Der Fingerabdruck weicht vom früher geprüften ab"
                           " ({alt}). Nur weiterverwenden, wenn der Empfänger den neuen"
                           " Schlüssel bestätigt hat.").format(alt=remembered.recipient_fingerprint)
        return ""

    # -- analysis ----------------------------------------------------------------------------------
    def analyse(self, again: bool = False) -> None:
        """Run the analysis over the documents not analysed yet (over all of them with
        ``again``), after settling the recipient and checking patient and documents."""
        if not self._apply_recipient() or not self._ready_to_start():
            return
        self._apply_options()
        self._queue = self._to_analyse(again)
        self._total = len(self._queue)
        self.progress.setRange(0, max(1, self._total))
        self.progress.setValue(0)
        self._run_next()

    def _apply_recipient(self) -> bool:
        """Switch the session's recipient mode to match the tick box; False when a
        recipient is wanted but no usable key is at hand (the user has been told)."""
        if not self.sharing.isChecked():
            if self.session.recipient is not None:
                self.session.set_recipient(None)
            return True
        certificate = self.certificate if self.certificate is not None else self.check_recipient()
        if certificate is None:
            self._notify(self.tr("Empfänger"), self.tr(
                "Für einen Empfänger braucht es dessen öffentlichen Schlüssel."))
            return False
        if self.session.recipient is None or \
                self.session.recipient.fingerprint != certificate.fingerprint:
            self.session.set_recipient(certificate)
        return True

    def _ready_to_start(self) -> bool:
        """False, with a message, while the patient or the documents are missing or the
        documents are still being loaded."""
        if self.session.patient is None:
            self._notify(self.tr("Analysieren"),
                         self.tr("Bitte zuerst einen Patienten übernehmen."))
            return False
        if not self.session.documents:
            self._notify(self.tr("Analysieren"), self.tr("Bitte zuerst Dokumente hinzufügen."))
            return False
        if self.is_loading():
            self._notify(self.tr("Analysieren"),
                         self.tr("Die Dokumente werden noch geladen; bitte einen Moment warten."))
            return False
        return True

    def _to_analyse(self, again: bool) -> list[Document]:
        """The documents to run: those not analysed yet or already exported, every one
        when a re-analysis is wanted."""
        return [d for d in self.session.documents
                if again or d.preparation is None or d.preparation.finalised]

    def _run_next(self) -> None:
        """Analyse the next queued document (in a worker thread unless synchronous) or,
        with the queue empty, report completion and emit ``analysed``."""
        self.refresh()
        if not self._queue:
            self.button.setEnabled(True)
            self.progress.setValue(self.progress.maximum())
            self.status.setText(self.tr("Fertig: {n} Dokument(e) analysiert.").format(
                n=self._total))
            self.analysed.emit()
            return
        d = self._queue[0]
        self.button.setEnabled(False)
        number = self._total - len(self._queue) + 1
        self.progress.setValue(number - 1)
        self.status.setText(self.tr("Analysiere {nummer} von {gesamt}: {name} …").format(
            nummer=number, gesamt=self._total, name=d.path.name))
        if self.synchronous:
            try:
                self.session.analyse(d)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self._error(e, d)
                return
            self._finish(d)
            return
        self._worker = Worker(lambda: self.session.analyse(d), self)
        self._worker.done.connect(lambda _r, d=d: self._finish(d))
        self._worker.error.connect(lambda e, d=d: self._error(e, d))
        self._worker.start()

    def _finish(self, d: Document) -> None:
        """Take the analysed document off the queue and go on with the next."""
        if self._queue and self._queue[0] is d:
            self._queue.pop(0)
        self._run_next()

    def _error(self, e: Exception, d: Document) -> None:
        """Handle a failed analysis: an uncertain match asks the user and retries the same
        document (or stops the run), any other error is shown and the run goes on."""
        if self.synchronous:  # tests: never block on a modal dialog
            raise e
        if isinstance(e, UncertainMatch):
            decision = self.ask_uncertain_match(e)
            if decision is None:
                self._queue.clear()
                self.button.setEnabled(True)
                self.status.setText(self.tr("Abgebrochen."))
                return
            self.session.matching = decision
            self._run_next()  # same document again
            return
        d.error = optdoc_adapter.message(e, interface_language())
        QMessageBox.warning(self, d.path.name, d.error)
        if self._queue and self._queue[0] is d:
            self._queue.pop(0)
        self._run_next()

    def ask_uncertain_match(self, e: UncertainMatch) -> str | None:
        """Ask whether the patient is one of the similar stored people; None = cancel."""
        lines = [self.tr("{kennung}: {nachname}, {vorname}, geb. {geb} (Übereinstimmung {score})")
                 .format(kennung=k.code, nachname=k.surname, vorname=k.first_name,
                         geb=k.birth_date or "–", score=f"{k.score:.2f}") for k in e.candidates]
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Ähnliche Person im Pseudonymspeicher"))
        box.setText(self.tr("Diese Person ähnelt einem vorhandenen Eintrag. Dieselbe Person?"))
        box.setInformativeText("\n".join(lines))
        same = box.addButton(self.tr("Ja, {kennung}").format(kennung=e.candidates[0].code),
                             QMessageBox.ButtonRole.YesRole)
        new = box.addButton(self.tr("Nein, neue Person"), QMessageBox.ButtonRole.NoRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is same:
            return f"gleich:{e.candidates[0].code}"
        if box.clickedButton() is new:
            return "neu"
        return None
