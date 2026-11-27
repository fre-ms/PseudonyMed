"""Step 8: the pseudonym database itself: statistics, backup and restore.

Everything re-identification depends on is here: the database with identities and
mappings and the key file the codes are derived from.  The step shows what is stored
(counts per category, size, integrity, dates) and writes or reads a backup archive
with both, optionally with the configuration and the vocabulary, protected by a
passphrase unless the user insists on plain."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.detection import extra_data as extra_data_mod
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language, shown, shown_error
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms import statistics as stats_mod


class DatabasePage(QWidget):
    """Step 11: what the database holds, where it and the key live, backup and restore,
    and the reference data the user may fetch for it."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self.manifest: backup_mod.Manifest | None = None
        self.archive: Path | None = None
        outer = QVBoxLayout(self)
        self._build_header(outer)
        self._build_table(outer)
        self._build_storage_location(outer)
        self._build_backup(outer)
        self._build_restore(outer)
        self._build_extra_data(outer)
        self.fetcher = extra_data_mod.fetch  # replaced in tests

    # -- layout ------------------------------------------------------------------------------------
    def _build_header(self, outer: QVBoxLayout) -> None:
        """The explanatory line and the refresh button."""
        header = QHBoxLayout()
        header.addWidget(QLabel(self.tr(
            "Datenbank und Schlüssel: alles, was zur Rückersetzung nötig ist. Sichern Sie beides"
            " regelmäßig; ohne den Schlüssel sind die Pseudonyme nicht mehr auflösbar.")), 1)
        new = QPushButton(self.tr("Aktualisieren"))
        new.clicked.connect(self.refresh)
        header.addWidget(new)
        outer.addLayout(header)

    def _build_table(self, outer: QVBoxLayout) -> None:
        """The two-column statistics table (feature, value)."""
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([self.tr("Merkmal"), self.tr("Wert")])
        header_line = self.table.horizontalHeader()
        header_line.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_line.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        outer.addWidget(self.table, 1)

    def _build_storage_location(self, outer: QVBoxLayout) -> None:
        """The group for moving database and key file; fixed when the session was started
        with an explicit path (tests, --speicher)."""
        place = QGroupBox(self.tr("Speicherort"))
        of = QFormLayout(place)
        db_row = QHBoxLayout()
        self.database_location = QLineEdit()
        choose_db_button = QPushButton("…")
        choose_db_button.clicked.connect(self.choose_database_location)
        db_row.addWidget(self.database_location, 1)
        db_row.addWidget(choose_db_button)
        of.addRow(self.tr("Datenbank"), db_row)
        key_row = QHBoxLayout()
        self.key_location = QLineEdit()
        choose_key_button = QPushButton("…")
        choose_key_button.clicked.connect(self.choose_key_location)
        key_row.addWidget(self.key_location, 1)
        key_row.addWidget(choose_key_button)
        of.addRow(self.tr("Schlüsseldatei"), key_row)
        self.copy_to_location = QCheckBox(self.tr(
            "Vorhandene Dateien an den neuen Ort kopieren (die alten werden umbenannt)"))
        self.copy_to_location.setChecked(True)
        of.addRow(self.copy_to_location)
        location_row = QHBoxLayout()
        self.location_status = QLabel(self.tr(
            "Beide Dateien dürfen in beliebigen Ordnern liegen, auch getrennt; wer die Datenbank"
            " ohne den Schlüssel hat, kann nichts auflösen."))
        self.location_status.setWordWrap(True)
        self.location_button = QPushButton(self.tr("Speicherort übernehmen"))
        self.location_button.clicked.connect(self.set_storage_location)
        location_row.addWidget(self.location_status, 1)
        location_row.addWidget(self.location_button)
        of.addRow(location_row)
        if self.session.store_path is not None:  # an explicit path (tests, --speicher): fixed
            place.setEnabled(False)
        outer.addWidget(place)

    def _build_backup(self, outer: QVBoxLayout) -> None:
        """The backup group: target folder, what to include, passphrase or plain."""
        export = QGroupBox(self.tr("Sichern (Export)"))
        ef = QFormLayout(export)
        line = QHBoxLayout()
        self.export_target = QLineEdit()
        self.export_target.setPlaceholderText(self.tr("Zielordner (Dateiname wird vergeben)"))
        choose_button = QPushButton("…")
        choose_button.clicked.connect(self.choose_export_target)
        line.addWidget(self.export_target, 1)
        line.addWidget(choose_button)
        ef.addRow(self.tr("Ziel"), line)
        self.with_config = QCheckBox(self.tr("Konfiguration (konfig.toml) einschließen"))
        self.with_config.setChecked(True)
        self.with_vocabulary = QCheckBox(self.tr("Fachvokabular einschließen"))
        self.with_vocabulary.setChecked(True)
        ef.addRow(self.with_config)
        ef.addRow(self.with_vocabulary)
        pw = QHBoxLayout()
        self.export_passphrase = QLineEdit()
        self.export_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.export_passphrase2 = QLineEdit()
        self.export_passphrase2.setEchoMode(QLineEdit.EchoMode.Password)
        self.export_passphrase2.setPlaceholderText(self.tr("Wiederholung"))
        pw.addWidget(self.export_passphrase, 1)
        pw.addWidget(self.export_passphrase2, 1)
        ef.addRow(self.tr("Passphrase"), pw)
        self.unencrypted = QCheckBox(self.tr(
            "Unverschlüsselt sichern (nicht empfohlen: Archiv enthält Schlüssel und Klardaten)"))
        ef.addRow(self.unencrypted)
        if not pgp_module.available():
            self.unencrypted.setChecked(True)
            self.unencrypted.setEnabled(False)
            self.export_passphrase.setEnabled(False)
            self.export_passphrase2.setEnabled(False)
            self.unencrypted.setToolTip(self.tr(
                "Verschlüsselung braucht das Extra 'pgp' (pysequoia) oder GnuPG."))
        export_row = QHBoxLayout()
        self.export_status = QLabel("")
        self.export_status.setWordWrap(True)
        self.export_button = QPushButton(self.tr("Sichern"))
        self.export_button.clicked.connect(self.run_export)
        export_row.addWidget(self.export_status, 1)
        export_row.addWidget(self.export_button)
        ef.addRow(export_row)
        outer.addWidget(export)

    def _build_restore(self, outer: QVBoxLayout) -> None:
        """The restore group: archive, passphrase, the manifest, mode and what to take over."""
        imp = QGroupBox(self.tr("Wiederherstellen (Import)"))
        jf = QFormLayout(imp)
        row2 = QHBoxLayout()
        self.import_path = QLineEdit()
        self.import_path.setPlaceholderText(self.tr("Sicherungsarchiv (.zip oder .zip.gpg)"))
        choose_button2 = QPushButton("…")
        choose_button2.clicked.connect(self.choose_archive)
        check = QPushButton(self.tr("Prüfen"))
        check.clicked.connect(self.check_archive)
        row2.addWidget(self.import_path, 1)
        row2.addWidget(choose_button2)
        row2.addWidget(check)
        jf.addRow(self.tr("Archiv"), row2)
        self.import_passphrase = QLineEdit()
        self.import_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        jf.addRow(self.tr("Passphrase"), self.import_passphrase)
        self.archive_info = QLabel("")
        self.archive_info.setWordWrap(True)
        jf.addRow(self.archive_info)
        self.mode_replace = QRadioButton(self.tr(
            "Ersetzen: Datenbank und Schlüssel aus dem Archiv übernehmen"
            " (die jetzigen werden vorher daneben gesichert)"))
        self.mode_replace.setChecked(True)
        self.mode_merge = QRadioButton(self.tr(
            "Zusammenführen: Einträge des Archivs ergänzen (nur bei gleichem Schlüssel)"))
        jf.addRow(self.mode_replace)
        jf.addRow(self.mode_merge)
        self.import_config = QCheckBox(self.tr("Konfiguration aus dem Archiv übernehmen"))
        self.import_config.setChecked(True)
        self.import_vocabulary = QCheckBox(self.tr("Fachvokabular aus dem Archiv übernehmen"))
        self.import_vocabulary.setChecked(True)
        jf.addRow(self.import_config)
        jf.addRow(self.import_vocabulary)
        import_row = QHBoxLayout()
        self.import_status = QLabel("")
        self.import_status.setWordWrap(True)
        self.import_button = QPushButton(self.tr("Importieren"))
        self.import_button.clicked.connect(self.run_import)
        self.import_button.setEnabled(False)
        import_row.addWidget(self.import_status, 1)
        import_row.addWidget(self.import_button)
        jf.addRow(import_row)
        outer.addWidget(imp)

    def _build_extra_data(self, outer: QVBoxLayout) -> None:
        """The group for the reference data that is not bundled (Austrian hospitals)."""
        extra = QGroupBox(self.tr("Zusatzdaten (nicht mitgeliefert)"))
        extra_layout = QVBoxLayout(extra)
        note = QLabel(self.tr(
            "Krankenanstalten in Österreich: Das Sozialministerium gestattet den Nachdruck seiner"
            " Liste nur außer zu kommerziellen Zwecken, deshalb liefert PseudonyMed sie nicht mit."
            " Auf Wunsch lädt PseudonyMed die aktuelle Liste direkt beim Ministerium in Ihre lokale"
            " Datenbank und gibt sie nicht weiter. Sie dient später der Einschätzung, welchen Ort"
            " ein stehen gelassener Klinikname verrät."))
        note.setWordWrap(True)
        extra_layout.addWidget(note)
        extra_row = QHBoxLayout()
        self.extra_status = QLabel("")
        self.extra_status.setWordWrap(True)
        self.extra_button = QPushButton(self.tr("Beim Ministerium laden"))
        self.extra_button.clicked.connect(lambda: self.load_clinics_at())
        from_file_button = QPushButton(self.tr("Aus Datei …"))
        from_file_button.clicked.connect(self.clinics_at_from_file)
        extra_row.addWidget(self.extra_status, 1)
        extra_row.addWidget(self.extra_button)
        extra_row.addWidget(from_file_button)
        extra_layout.addLayout(extra_row)
        outer.addWidget(extra)

    # -- optional reference data -------------------------------------------------------------------
    def clinics_at_from_file(self) -> None:
        """Load the ministry's list of Austrian hospitals from a workbook the user picks."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Liste der Krankenanstalten (Excel-Mappe des Ministeriums)"), "",
            self.tr("Excel-Mappe (*.xlsx)"))
        if path:
            self.load_clinics_at(Path(path))

    def load_clinics_at(self, file: Path | None = None
                        ) -> extra_data_mod.LoadReport | None:
        """Load the Austrian hospital list into the local database, from the ministry's
        site or from ``file``; None when the user declined or the load failed."""
        from pseudonymed.detection.identifiers import IdentifierTable

        if not self.synchronous:
            if not self._extra_data_confirmed(file):
                return None
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with IdentifierTable() as table:
                report = extra_data_mod.load_clinics_at(table, file=file, fetcher=self.fetcher)
        except (extra_data_mod.ExtraDataError, OSError, ValueError, KeyError, zipfile.BadZipFile,
                ET.ParseError) as e:
            self._failed(self.extra_status,
                         self.tr("Laden fehlgeschlagen: {fehler}").format(fehler=shown_error(e)), e)
            return None
        finally:
            if not self.synchronous:
                QApplication.restoreOverrideCursor()
        self.extra_status.setText(self.tr(
            "{anzahl} Einträge geladen, {ohne} ohne Gemeinde (Stand {stand}).").format(
            anzahl=report.count, ohne=report.without_municipality, stand=report.state or "–"))
        self.refresh()
        return report

    def _extra_data_confirmed(self, file: Path | None) -> bool:
        """True when the user, told the source and its licence, wants the list loaded now."""
        question = self.tr(
            "Quelle: BMASGPK, Liste der Krankenanstalten in Österreich. Nachdruck mit"
            " Quellenangabe außer zu kommerziellen Zwecken gestattet. Die Liste kommt nur in"
            " Ihre lokale Datenbank.") + "\n\n" + (
            self.tr("Jetzt beim Ministerium laden?") if file is None else
            self.tr("Jetzt aus der gewählten Datei laden?"))
        return QMessageBox.question(self, self.tr("Zusatzdaten"), question) == \
            QMessageBox.StandardButton.Yes

    # -- statistics --------------------------------------------------------------------------------
    def refresh(self) -> stats_mod.Statistics:
        """Re-read the statistics into the table and the paths into the location fields
        (a field being edited keeps its text); returns the statistics."""
        st = stats_mod.statistics(self.session.store_path)
        if not self.database_location.hasFocus():
            self.database_location.setText(str(st.path))
        if not self.key_location.hasFocus():
            self.key_location.setText(str(st.key_path or ""))
        lines = st.lines()
        self.table.setRowCount(len(lines))
        for i, (feature, value) in enumerate(lines):
            self.table.setItem(i, 0, QTableWidgetItem(shown(feature)))
            self.table.setItem(i, 1, QTableWidgetItem(value))
        return st

    def _notify(self, title: str, text: str, error: bool = False) -> None:
        """Show a message box; synchronous mode (tests) raises instead of blocking."""
        if self.synchronous:
            raise RuntimeError(text)
        (QMessageBox.warning if error else QMessageBox.information)(self, title, text)

    def _failed(self, status_label: QLabel, text: str, error: Exception) -> None:
        """Report a failed action in its status line; synchronous mode (tests) raises the
        failure instead of leaving it on the page."""
        status_label.setText(text)
        if self.synchronous:
            raise RuntimeError(str(error)) from error

    def _pending_documents(self) -> list[Document]:
        """Documents analysed but not yet exported: their mappings are not in the database
        yet, so it must not be moved or replaced under them."""
        return [d for d in self.session.documents if d.ready_to_check]

    # -- storage location --------------------------------------------------------------------------
    def choose_database_location(self) -> None:
        """Pick the database file in a save dialog."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Datenbank"),
                                              self.database_location.text(),
                                              self.tr("SQLite (*.sqlite)"))
        if path:
            self.database_location.setText(path)

    def choose_key_location(self) -> None:
        """Pick the key file in a save dialog."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Schlüsseldatei"),
                                              self.key_location.text(), self.tr("Text (*.txt)"))
        if path:
            self.key_location.setText(path)

    def set_storage_location(self) -> backup_mod.Relocation | None:
        """Move database and key to the paths entered (copying when ticked); None when
        documents still wait for their export or the move failed."""
        if self._pending_documents():
            self._notify(self.tr("Speicherort"), self.tr(
                "Es gibt analysierte, noch nicht exportierte Dokumente. Bitte zuerst exportieren"
                " oder entfernen."), True)
            return None
        try:
            v = backup_mod.relocate(Path(self.database_location.text().strip()),
                                    Path(self.key_location.text().strip()),
                                    self.copy_to_location.isChecked())
        except (backup_mod.BackupError, OSError) as e:
            self._failed(self.location_status, self.tr(
                "Speicherort nicht übernommen: {fehler}").format(fehler=shown_error(e)), e)
            return None
        self.location_status.setText(v.description(interface_language()))
        self.refresh()
        return v

    # -- export --------------------------------------------------------------------------------
    def choose_export_target(self) -> None:
        """Pick the folder the backup archive goes to."""
        folder = QFileDialog.getExistingDirectory(self, self.tr("Zielordner der Sicherung"))
        if folder:
            self.export_target.setText(folder)

    def run_export(self) -> Path | None:
        """Write the backup archive into the target folder, encrypted with the passphrase
        unless plain was chosen; returns its path, None when input was missing or it failed."""
        target = self.export_target.text().strip()
        if not target:
            self._notify(self.tr("Sichern"), self.tr("Bitte einen Zielordner wählen."), True)
            return None
        passphrase: str | None = None
        if not self.unencrypted.isChecked():
            error = self._passphrase_error()
            if error:
                self._notify(self.tr("Sichern"), error, True)
                return None
            passphrase = self.export_passphrase.text()
        try:
            path = backup_mod.export(Path(target), self.session.store_path,
                                     config=self.with_config.isChecked(),
                                     vocabulary=self.with_vocabulary.isChecked(),
                                     passphrase=passphrase)
        except (backup_mod.BackupError, pgp_module.PGPError, OSError) as e:
            self._failed(self.export_status, self.tr(
                "Sicherung fehlgeschlagen: {fehler}").format(fehler=shown_error(e)), e)
            return None
        self.export_status.setText(self.tr("Gesichert: {pfad}").format(pfad=path) + (
            self.tr(" (verschlüsselt; gpg --decrypt öffnet das Archiv auch ohne PseudonyMed)")
            if passphrase else self.tr(" (UNVERSCHLÜSSELT, enthält Schlüssel und Klardaten)")))
        self.export_passphrase.clear()
        self.export_passphrase2.clear()
        return path

    def _passphrase_error(self) -> str:
        """Why the entered passphrase cannot be used (too short, repetition differs), or an
        empty string when it can."""
        if len(self.export_passphrase.text()) < 8:
            return self.tr(
                "Bitte eine Passphrase mit mindestens 8 Zeichen eingeben oder"
                " „Unverschlüsselt“ wählen.")
        if self.export_passphrase.text() != self.export_passphrase2.text():
            return self.tr("Die Wiederholung stimmt nicht.")
        return ""

    # -- import ------------------------------------------------------------------------------------
    def choose_archive(self) -> None:
        """Pick a backup archive and check it at once."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Sicherungsarchiv"), "",
            self.tr("PseudonyMed-Sicherungen (*.zip *.gpg);;Alle Dateien (*)"))
        if path:
            self.import_path.setText(path)
            self.check_archive()

    def check_archive(self) -> backup_mod.Manifest | None:
        """Read and verify the archive's manifest, compare its key with the local one and
        enable the import; None when no archive is named or it cannot be read."""
        path = self.import_path.text().strip()
        self.manifest, self.archive = None, None
        self.import_button.setEnabled(False)
        if not path:
            self.archive_info.setText(self.tr("Bitte ein Archiv wählen."))
            return None
        try:
            m = backup_mod.check(Path(path), self.import_passphrase.text() or None)
        except (backup_mod.BackupError, OSError) as e:
            self._failed(self.archive_info,
                         self.tr("Archiv nicht lesbar: {fehler}").format(fehler=shown_error(e)), e)
            return None
        st = stats_mod.statistics(self.session.store_path)
        self.manifest, self.archive = m, Path(path)
        self._offer_import_mode(m, st)
        self.import_button.setEnabled(True)
        return m

    def _offer_import_mode(self, m: backup_mod.Manifest, st: stats_mod.Statistics) -> None:
        """Describe the archive and offer merging only when its key is the local one (or
        there is no local database yet); with another key only replacing remains."""
        same = st.key_id and st.key_id == m.key_id
        self.archive_info.setText(self.tr("Archiv: {beschreibung}. ").format(
            beschreibung=m.description(interface_language())) + (
            self.tr("Gleicher Schlüssel wie lokal: Zusammenführen möglich.") if same else
            self.tr("Anderer Schlüssel als lokal (oder noch keiner): nur Ersetzen.")))
        self.mode_merge.setEnabled(bool(same) or not st.exists)
        if not same and st.exists:
            self.mode_replace.setChecked(True)

    def run_import(self) -> backup_mod.ImportReport | None:
        """Restore from the checked archive in the chosen mode; None when no archive was
        checked, documents still wait for their export, the user declined or it failed."""
        if self.manifest is None or self.archive is None:
            self._notify(self.tr("Importieren"), self.tr("Bitte zuerst ein Archiv prüfen."), True)
            return None
        if self._pending_documents():
            self._notify(self.tr("Importieren"), self.tr(
                "Es gibt analysierte, noch nicht exportierte Dokumente. Bitte zuerst exportieren"
                " oder entfernen, dann importieren."), True)
            return None
        mode = "ersetzen" if self.mode_replace.isChecked() else "zusammenfuehren"
        if not self._import_confirmed(mode):
            return None
        try:
            b = backup_mod.import_file(self.archive, self.session.store_path,
                                       self.import_passphrase.text() or None, mode,
                                       config=self.import_config.isChecked(),
                                       vocabulary=self.import_vocabulary.isChecked())
        except (backup_mod.BackupError, OSError) as e:
            self._failed(self.import_status, self.tr(
                "Import fehlgeschlagen: {fehler}").format(fehler=shown_error(e)), e)
            return None
        self.import_status.setText(b.description(interface_language()))
        self.import_passphrase.clear()
        self.refresh()
        return b

    def _import_confirmed(self, mode: str) -> bool:
        """True when the user confirmed the import in the given mode; synchronous mode
        (tests) never asks."""
        if self.synchronous:
            return True
        question = (self.tr("Datenbank und Schlüssel durch das Archiv ersetzen? Die jetzigen"
                            " werden vorher daneben gesichert.") if mode == "ersetzen" else
                    self.tr("Einträge des Archivs in die lokale Datenbank übernehmen?"))
        return QMessageBox.question(self, self.tr("Importieren"), question) == \
            QMessageBox.StandardButton.Yes
