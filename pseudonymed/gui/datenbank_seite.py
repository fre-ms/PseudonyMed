"""Step 8: the pseudonym database itself: statistics, backup and restore.

Everything re-identification depends on is here: the database with identities and
mappings and the key file the codes are derived from.  The step shows what is stored
(counts per category, size, integrity, dates) and writes or reads a backup archive
with both, optionally with the configuration and the user's vocabulary, protected by a
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

from pseudonymed.erkennung import zusatzdaten as zusatzmodul
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme import sicherung as sichmodul


class DatenbankSeite(QWidget):
    """Step 11: what the database holds, where it and the key live, backup and restore,
    and the reference data the user may fetch for it."""

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self.manifest: sichmodul.Manifest | None = None
        self.archiv: Path | None = None
        aussen = QVBoxLayout(self)
        self._baue_kopf(aussen)
        self._baue_tabelle(aussen)
        self._baue_speicherort(aussen)
        self._baue_sichern(aussen)
        self._baue_wiederherstellen(aussen)
        self._baue_zusatzdaten(aussen)
        self.holen = zusatzmodul.abrufen  # replaced in tests

    # -- layout ------------------------------------------------------------------------------------
    def _baue_kopf(self, aussen: QVBoxLayout) -> None:
        """The explanatory line and the refresh button."""
        kopf = QHBoxLayout()
        kopf.addWidget(QLabel(self.tr(
            "Datenbank und Schlüssel: alles, was zur Rückersetzung nötig ist. Sichern Sie beides"
            " regelmäßig; ohne den Schlüssel sind die Pseudonyme nicht mehr auflösbar.")), 1)
        neu = QPushButton(self.tr("Aktualisieren"))
        neu.clicked.connect(self.aktualisiere)
        kopf.addWidget(neu)
        aussen.addLayout(kopf)

    def _baue_tabelle(self, aussen: QVBoxLayout) -> None:
        """The two-column statistics table (feature, value)."""
        self.tabelle = QTableWidget(0, 2)
        self.tabelle.setHorizontalHeaderLabels([self.tr("Merkmal"), self.tr("Wert")])
        kopfzeile = self.tabelle.horizontalHeader()
        kopfzeile.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        kopfzeile.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tabelle.verticalHeader().setVisible(False)
        self.tabelle.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        aussen.addWidget(self.tabelle, 1)

    def _baue_speicherort(self, aussen: QVBoxLayout) -> None:
        """The group for moving database and key file; fixed when the session was started
        with an explicit path (tests, --speicher)."""
        ort = QGroupBox(self.tr("Speicherort"))
        of = QFormLayout(ort)
        z1 = QHBoxLayout()
        self.ort_datenbank = QLineEdit()
        db_waehlen = QPushButton("…")
        db_waehlen.clicked.connect(self.ort_datenbank_waehlen)
        z1.addWidget(self.ort_datenbank, 1)
        z1.addWidget(db_waehlen)
        of.addRow(self.tr("Datenbank"), z1)
        z2 = QHBoxLayout()
        self.ort_schluessel = QLineEdit()
        key_waehlen = QPushButton("…")
        key_waehlen.clicked.connect(self.ort_schluessel_waehlen)
        z2.addWidget(self.ort_schluessel, 1)
        z2.addWidget(key_waehlen)
        of.addRow(self.tr("Schlüsseldatei"), z2)
        self.ort_kopieren = QCheckBox(self.tr(
            "Vorhandene Dateien an den neuen Ort kopieren (die alten werden umbenannt)"))
        self.ort_kopieren.setChecked(True)
        of.addRow(self.ort_kopieren)
        oz = QHBoxLayout()
        self.ort_status = QLabel(self.tr(
            "Beide Dateien dürfen in beliebigen Ordnern liegen, auch getrennt; wer die Datenbank"
            " ohne den Schlüssel hat, kann nichts auflösen."))
        self.ort_status.setWordWrap(True)
        self.ort_knopf = QPushButton(self.tr("Speicherort übernehmen"))
        self.ort_knopf.clicked.connect(self.speicherort_setzen)
        oz.addWidget(self.ort_status, 1)
        oz.addWidget(self.ort_knopf)
        of.addRow(oz)
        if self.sitzung.speicherpfad is not None:  # an explicit path (tests, --speicher): fixed
            ort.setEnabled(False)
        aussen.addWidget(ort)

    def _baue_sichern(self, aussen: QVBoxLayout) -> None:
        """The backup group: target folder, what to include, passphrase or plain."""
        export = QGroupBox(self.tr("Sichern (Export)"))
        ef = QFormLayout(export)
        zeile = QHBoxLayout()
        self.export_ziel = QLineEdit()
        self.export_ziel.setPlaceholderText(self.tr("Zielordner (Dateiname wird vergeben)"))
        waehlen = QPushButton("…")
        waehlen.clicked.connect(self.export_ziel_waehlen)
        zeile.addWidget(self.export_ziel, 1)
        zeile.addWidget(waehlen)
        ef.addRow(self.tr("Ziel"), zeile)
        self.mit_konfig = QCheckBox(self.tr("Konfiguration (konfig.toml) einschließen"))
        self.mit_konfig.setChecked(True)
        self.mit_vokabular = QCheckBox(self.tr("Nutzervokabular einschließen"))
        self.mit_vokabular.setChecked(True)
        ef.addRow(self.mit_konfig)
        ef.addRow(self.mit_vokabular)
        pw = QHBoxLayout()
        self.export_passphrase = QLineEdit()
        self.export_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.export_passphrase2 = QLineEdit()
        self.export_passphrase2.setEchoMode(QLineEdit.EchoMode.Password)
        self.export_passphrase2.setPlaceholderText(self.tr("Wiederholung"))
        pw.addWidget(self.export_passphrase, 1)
        pw.addWidget(self.export_passphrase2, 1)
        ef.addRow(self.tr("Passphrase"), pw)
        self.unverschluesselt = QCheckBox(self.tr(
            "Unverschlüsselt sichern (nicht empfohlen: Archiv enthält Schlüssel und Klardaten)"))
        ef.addRow(self.unverschluesselt)
        if not pgpmodul.verfuegbar():
            self.unverschluesselt.setChecked(True)
            self.unverschluesselt.setEnabled(False)
            self.export_passphrase.setEnabled(False)
            self.export_passphrase2.setEnabled(False)
            self.unverschluesselt.setToolTip(self.tr(
                "Verschlüsselung braucht das Extra 'pgp' (pysequoia) oder GnuPG."))
        ez = QHBoxLayout()
        self.export_status = QLabel("")
        self.export_status.setWordWrap(True)
        self.export_knopf = QPushButton(self.tr("Sichern"))
        self.export_knopf.clicked.connect(self.exportieren)
        ez.addWidget(self.export_status, 1)
        ez.addWidget(self.export_knopf)
        ef.addRow(ez)
        aussen.addWidget(export)

    def _baue_wiederherstellen(self, aussen: QVBoxLayout) -> None:
        """The restore group: archive, passphrase, the manifest, mode and what to take over."""
        imp = QGroupBox(self.tr("Wiederherstellen (Import)"))
        jf = QFormLayout(imp)
        zeile2 = QHBoxLayout()
        self.import_pfad = QLineEdit()
        self.import_pfad.setPlaceholderText(self.tr("Sicherungsarchiv (.zip oder .zip.gpg)"))
        waehlen2 = QPushButton("…")
        waehlen2.clicked.connect(self.archiv_waehlen)
        pruefen = QPushButton(self.tr("Prüfen"))
        pruefen.clicked.connect(self.archiv_pruefen)
        zeile2.addWidget(self.import_pfad, 1)
        zeile2.addWidget(waehlen2)
        zeile2.addWidget(pruefen)
        jf.addRow(self.tr("Archiv"), zeile2)
        self.import_passphrase = QLineEdit()
        self.import_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        jf.addRow(self.tr("Passphrase"), self.import_passphrase)
        self.archiv_info = QLabel("")
        self.archiv_info.setWordWrap(True)
        jf.addRow(self.archiv_info)
        self.modus_ersetzen = QRadioButton(self.tr(
            "Ersetzen: Datenbank und Schlüssel aus dem Archiv übernehmen"
            " (die jetzigen werden vorher daneben gesichert)"))
        self.modus_ersetzen.setChecked(True)
        self.modus_zusammen = QRadioButton(self.tr(
            "Zusammenführen: Einträge des Archivs ergänzen (nur bei gleichem Schlüssel)"))
        jf.addRow(self.modus_ersetzen)
        jf.addRow(self.modus_zusammen)
        self.import_konfig = QCheckBox(self.tr("Konfiguration aus dem Archiv übernehmen"))
        self.import_konfig.setChecked(True)
        self.import_vokabular = QCheckBox(self.tr("Nutzervokabular aus dem Archiv übernehmen"))
        self.import_vokabular.setChecked(True)
        jf.addRow(self.import_konfig)
        jf.addRow(self.import_vokabular)
        iz = QHBoxLayout()
        self.import_status = QLabel("")
        self.import_status.setWordWrap(True)
        self.import_knopf = QPushButton(self.tr("Importieren"))
        self.import_knopf.clicked.connect(self.importieren)
        self.import_knopf.setEnabled(False)
        iz.addWidget(self.import_status, 1)
        iz.addWidget(self.import_knopf)
        jf.addRow(iz)
        aussen.addWidget(imp)

    def _baue_zusatzdaten(self, aussen: QVBoxLayout) -> None:
        """The group for the reference data that is not bundled (Austrian hospitals)."""
        zusatz = QGroupBox(self.tr("Zusatzdaten (nicht mitgeliefert)"))
        zl = QVBoxLayout(zusatz)
        hinweis = QLabel(self.tr(
            "Krankenanstalten in Österreich: Das Sozialministerium gestattet den Nachdruck seiner"
            " Liste nur außer zu kommerziellen Zwecken, deshalb liefert PseudonyMed sie nicht mit."
            " Auf Wunsch lädt PseudonyMed die aktuelle Liste direkt beim Ministerium in Ihre lokale"
            " Datenbank und gibt sie nicht weiter. Sie dient später der Einschätzung, welchen Ort"
            " ein stehen gelassener Klinikname verrät."))
        hinweis.setWordWrap(True)
        zl.addWidget(hinweis)
        zk = QHBoxLayout()
        self.zusatz_status = QLabel("")
        self.zusatz_status.setWordWrap(True)
        self.zusatz_knopf = QPushButton(self.tr("Beim Ministerium laden"))
        self.zusatz_knopf.clicked.connect(lambda: self.kliniken_at_laden())
        zusatz_datei = QPushButton(self.tr("Aus Datei …"))
        zusatz_datei.clicked.connect(self.kliniken_at_aus_datei)
        zk.addWidget(self.zusatz_status, 1)
        zk.addWidget(self.zusatz_knopf)
        zk.addWidget(zusatz_datei)
        zl.addLayout(zk)
        aussen.addWidget(zusatz)

    # -- optional reference data -------------------------------------------------------------------
    def kliniken_at_aus_datei(self) -> None:
        """Load the ministry's list of Austrian hospitals from a workbook the user picks."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Liste der Krankenanstalten (Excel-Mappe des Ministeriums)"), "",
            self.tr("Excel-Mappe (*.xlsx)"))
        if pfad:
            self.kliniken_at_laden(Path(pfad))

    def kliniken_at_laden(self, datei: Path | None = None
                          ) -> zusatzmodul.Ladebericht | None:
        """Load the Austrian hospital list into the local database, from the ministry's
        site or from ``datei``; None when the user declined or the load failed."""
        from pseudonymed.erkennung.vokabular import Vokabular

        if not self.synchron:
            if not self._zusatzdaten_bestaetigt(datei):
                return None
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with Vokabular(self.sitzung.speicherpfad) as v:
                bericht = zusatzmodul.lade_kliniken_at(v.verbindung, datei=datei,
                                                      holen=self.holen)
        except (zusatzmodul.ZusatzFehler, OSError, ValueError, KeyError, zipfile.BadZipFile,
                ET.ParseError) as e:
            self._fehlgeschlagen(self.zusatz_status,
                                 self.tr("Laden fehlgeschlagen: {fehler}").format(fehler=e), e)
            return None
        finally:
            if not self.synchron:
                QApplication.restoreOverrideCursor()
        self.zusatz_status.setText(self.tr(
            "{anzahl} Einträge geladen, {ohne} ohne Gemeinde (Stand {stand}).").format(
            anzahl=bericht.anzahl, ohne=bericht.ohne_gemeinde, stand=bericht.stand or "–"))
        self.aktualisiere()
        return bericht

    def _zusatzdaten_bestaetigt(self, datei: Path | None) -> bool:
        """True when the user, told the source and its licence, wants the list loaded now."""
        frage = self.tr(
            "Quelle: BMASGPK, Liste der Krankenanstalten in Österreich. Nachdruck mit"
            " Quellenangabe außer zu kommerziellen Zwecken gestattet. Die Liste kommt nur in"
            " Ihre lokale Datenbank.") + "\n\n" + (
            self.tr("Jetzt beim Ministerium laden?") if datei is None else
            self.tr("Jetzt aus der gewählten Datei laden?"))
        return QMessageBox.question(self, self.tr("Zusatzdaten"), frage) == \
            QMessageBox.StandardButton.Yes

    # -- statistics --------------------------------------------------------------------------------
    def aktualisiere(self) -> sichmodul.Statistik:
        """Re-read the statistics into the table and the paths into the location fields
        (a field being edited keeps its text); returns the statistics."""
        st = sichmodul.statistik(self.sitzung.speicherpfad)
        if not self.ort_datenbank.hasFocus():
            self.ort_datenbank.setText(str(st.pfad))
        if not self.ort_schluessel.hasFocus():
            self.ort_schluessel.setText(str(st.schluessel_pfad or ""))
        zeilen = st.zeilen()
        self.tabelle.setRowCount(len(zeilen))
        for i, (merkmal, wert) in enumerate(zeilen):
            self.tabelle.setItem(i, 0, QTableWidgetItem(merkmal))
            self.tabelle.setItem(i, 1, QTableWidgetItem(wert))
        return st

    def _melde(self, titel: str, text: str, fehler: bool = False) -> None:
        """Show a message box; synchronous mode (tests) raises instead of blocking."""
        if self.synchron:
            raise RuntimeError(text)
        (QMessageBox.warning if fehler else QMessageBox.information)(self, titel, text)

    def _fehlgeschlagen(self, anzeige: QLabel, text: str, fehler: Exception) -> None:
        """Report a failed action in its status line; synchronous mode (tests) raises the
        failure instead of leaving it on the page."""
        anzeige.setText(text)
        if self.synchron:
            raise RuntimeError(str(fehler)) from fehler

    def _offene_dokumente(self) -> list[Dokument]:
        """Documents analysed but not yet exported: their mappings are not in the database
        yet, so it must not be moved or replaced under them."""
        return [d for d in self.sitzung.dokumente if d.pruefbereit]

    # -- storage location --------------------------------------------------------------------------
    def ort_datenbank_waehlen(self) -> None:
        """Pick the database file in a save dialog."""
        pfad, _ = QFileDialog.getSaveFileName(self, self.tr("Datenbank"), self.ort_datenbank.text(),
                                              self.tr("SQLite (*.sqlite)"))
        if pfad:
            self.ort_datenbank.setText(pfad)

    def ort_schluessel_waehlen(self) -> None:
        """Pick the key file in a save dialog."""
        pfad, _ = QFileDialog.getSaveFileName(self, self.tr("Schlüsseldatei"),
                                              self.ort_schluessel.text(), self.tr("Text (*.txt)"))
        if pfad:
            self.ort_schluessel.setText(pfad)

    def speicherort_setzen(self) -> sichmodul.Verlegung | None:
        """Move database and key to the paths entered (copying when ticked); None when
        documents still wait for their export or the move failed."""
        if self._offene_dokumente():
            self._melde(self.tr("Speicherort"), self.tr(
                "Es gibt analysierte, noch nicht exportierte Dokumente. Bitte zuerst exportieren"
                " oder entfernen."), True)
            return None
        try:
            v = sichmodul.verlege(Path(self.ort_datenbank.text().strip()),
                                  Path(self.ort_schluessel.text().strip()),
                                  self.ort_kopieren.isChecked())
        except (sichmodul.SicherungFehler, OSError) as e:
            self._fehlgeschlagen(self.ort_status, self.tr(
                "Speicherort nicht übernommen: {fehler}").format(fehler=e), e)
            return None
        self.ort_status.setText(v.beschreibung())
        self.aktualisiere()
        return v

    # -- export --------------------------------------------------------------------------------
    def export_ziel_waehlen(self) -> None:
        """Pick the folder the backup archive goes to."""
        ordner = QFileDialog.getExistingDirectory(self, self.tr("Zielordner der Sicherung"))
        if ordner:
            self.export_ziel.setText(ordner)

    def exportieren(self) -> Path | None:
        """Write the backup archive into the target folder, encrypted with the passphrase
        unless plain was chosen; returns its path, None when input was missing or it failed."""
        ziel = self.export_ziel.text().strip()
        if not ziel:
            self._melde(self.tr("Sichern"), self.tr("Bitte einen Zielordner wählen."), True)
            return None
        passphrase: str | None = None
        if not self.unverschluesselt.isChecked():
            fehler = self._passphrase_fehler()
            if fehler:
                self._melde(self.tr("Sichern"), fehler, True)
                return None
            passphrase = self.export_passphrase.text()
        try:
            pfad = sichmodul.exportiere(Path(ziel), self.sitzung.speicherpfad,
                                        konfig=self.mit_konfig.isChecked(),
                                        vokabular=self.mit_vokabular.isChecked(),
                                        passphrase=passphrase)
        except (sichmodul.SicherungFehler, pgpmodul.PGPFehler, OSError) as e:
            self._fehlgeschlagen(self.export_status, self.tr(
                "Sicherung fehlgeschlagen: {fehler}").format(fehler=e), e)
            return None
        self.export_status.setText(self.tr("Gesichert: {pfad}").format(pfad=pfad) + (
            self.tr(" (verschlüsselt; gpg --decrypt öffnet das Archiv auch ohne PseudonyMed)")
            if passphrase else self.tr(" (UNVERSCHLÜSSELT, enthält Schlüssel und Klardaten)")))
        self.export_passphrase.clear()
        self.export_passphrase2.clear()
        return pfad

    def _passphrase_fehler(self) -> str:
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
    def archiv_waehlen(self) -> None:
        """Pick a backup archive and check it at once."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Sicherungsarchiv"), "",
            self.tr("PseudonyMed-Sicherungen (*.zip *.gpg);;Alle Dateien (*)"))
        if pfad:
            self.import_pfad.setText(pfad)
            self.archiv_pruefen()

    def archiv_pruefen(self) -> sichmodul.Manifest | None:
        """Read and verify the archive's manifest, compare its key with the local one and
        enable the import; None when no archive is named or it cannot be read."""
        pfad = self.import_pfad.text().strip()
        self.manifest, self.archiv = None, None
        self.import_knopf.setEnabled(False)
        if not pfad:
            self.archiv_info.setText(self.tr("Bitte ein Archiv wählen."))
            return None
        try:
            m = sichmodul.pruefe(Path(pfad), self.import_passphrase.text() or None)
        except (sichmodul.SicherungFehler, OSError) as e:
            self._fehlgeschlagen(self.archiv_info,
                                 self.tr("Archiv nicht lesbar: {fehler}").format(fehler=e), e)
            return None
        st = sichmodul.statistik(self.sitzung.speicherpfad)
        self.manifest, self.archiv = m, Path(pfad)
        self._importmodus_anbieten(m, st)
        self.import_knopf.setEnabled(True)
        return m

    def _importmodus_anbieten(self, m: sichmodul.Manifest, st: sichmodul.Statistik) -> None:
        """Describe the archive and offer merging only when its key is the local one (or
        there is no local database yet); with another key only replacing remains."""
        gleich = st.schluessel_kennung and st.schluessel_kennung == m.schluessel_kennung
        self.archiv_info.setText(self.tr("Archiv: {beschreibung}. ").format(
            beschreibung=m.beschreibung()) + (
            self.tr("Gleicher Schlüssel wie lokal: Zusammenführen möglich.") if gleich else
            self.tr("Anderer Schlüssel als lokal (oder noch keiner): nur Ersetzen.")))
        self.modus_zusammen.setEnabled(bool(gleich) or not st.existiert)
        if not gleich and st.existiert:
            self.modus_ersetzen.setChecked(True)

    def importieren(self) -> sichmodul.Importbericht | None:
        """Restore from the checked archive in the chosen mode; None when no archive was
        checked, documents still wait for their export, the user declined or it failed."""
        if self.manifest is None or self.archiv is None:
            self._melde(self.tr("Importieren"), self.tr("Bitte zuerst ein Archiv prüfen."), True)
            return None
        if self._offene_dokumente():
            self._melde(self.tr("Importieren"), self.tr(
                "Es gibt analysierte, noch nicht exportierte Dokumente. Bitte zuerst exportieren"
                " oder entfernen, dann importieren."), True)
            return None
        modus = "ersetzen" if self.modus_ersetzen.isChecked() else "zusammenfuehren"
        if not self._import_bestaetigt(modus):
            return None
        try:
            b = sichmodul.importiere(self.archiv, self.sitzung.speicherpfad,
                                     self.import_passphrase.text() or None, modus,
                                     konfig=self.import_konfig.isChecked(),
                                     vokabular=self.import_vokabular.isChecked())
        except (sichmodul.SicherungFehler, OSError) as e:
            self._fehlgeschlagen(self.import_status,
                                 self.tr("Import fehlgeschlagen: {fehler}").format(fehler=e), e)
            return None
        self.import_status.setText(b.beschreibung())
        self.import_passphrase.clear()
        self.aktualisiere()
        return b

    def _import_bestaetigt(self, modus: str) -> bool:
        """True when the user confirmed the import in the given mode; synchronous mode
        (tests) never asks."""
        if self.synchron:
            return True
        frage = (self.tr("Datenbank und Schlüssel durch das Archiv ersetzen? Die jetzigen"
                         " werden vorher daneben gesichert.") if modus == "ersetzen" else
                 self.tr("Einträge des Archivs in die lokale Datenbank übernehmen?"))
        return QMessageBox.question(self, self.tr("Importieren"), frage) == \
            QMessageBox.StandardButton.Yes
