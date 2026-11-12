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

from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import ner as nermodul
from pseudonymed.gui.arbeiter import Arbeiter
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.konfig import Einstellungen, lade_einstellungen, speichere_einstellungen
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme.speicher import UnsichererAbgleich


class AnalysierenSeite(QWidget):
    """Step 6: the detection options (including the four medical levels), the recipient
    for pass-on pseudonyms and the analysis run, document by document with progress."""

    analysiert = Signal()

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self.laedt = lambda: False  # the main window tells whether documents are still loading
        self._arbeiter: Arbeiter | None = None
        self._warteschlange: list[Dokument] = []
        self._gesamt = 0
        self.zertifikat: pgpmodul.Zertifikat | None = None
        aussen = QVBoxLayout(self)
        self._baue_erkennung(aussen)
        self._baue_vokabular(aussen)
        self._baue_empfaenger(aussen)
        self._baue_dokumente(aussen)
        self._baue_knoepfe(aussen)

    # -- layout ------------------------------------------------------------------------------------
    def _baue_erkennung(self, aussen: QVBoxLayout) -> None:
        """The detection options: date form, codes, unreadable lines, medical terms,
        identifiers, the four medical levels, fuzzy search and NER."""
        optionen = QGroupBox(self.tr("Erkennung der Identifikatoren"))
        form = QFormLayout(optionen)
        self.datum = QComboBox()
        self.datum.addItem(self.tr("Geburtsjahr [geb. 1990]"), "jahr")
        self.datum.addItem(self.tr("Alter [Alter 36]"), "alter")
        form.addRow(self.tr("Geburtsdatum als"), self.datum)
        self.codes = QCheckBox(self.tr("Barcodes und QR-Codes schwärzen"))
        self.codes.setChecked(True)
        form.addRow(self.codes)
        self.unlesbar = QCheckBox(self.tr("Unlesbare OCR-Zeilen schwärzen"))
        self.unlesbar.setChecked(True)
        form.addRow(self.unlesbar)
        self.fachbegriffe = QCheckBox(
            self.tr("Fachbegriffe markieren und vor NER und unscharfer OCR-Suche schützen"))
        self.fachbegriffe.setChecked(True)
        form.addRow(self.fachbegriffe)
        self.identifikatoren = QCheckBox(self.tr(
            "Identifikatoren schwärzen: Kliniken, Berufe, Studien, Titel"
            " (siehe „2 Identifikatoren“)"))
        self.identifikatoren.setChecked(True)
        form.addRow(self.identifikatoren)
        self._baue_medizinstufen(form)
        self.unscharf = QCheckBox(
            self.tr("Textdateien: abweichend geschriebene Namen unscharf suchen (Transkripte)"))
        self.unscharf.setChecked(True)
        form.addRow(self.unscharf)
        self.ner = QCheckBox(self.tr("Unbekannte Personen und Orte per NER suchen (rät)"))
        self.ner.setEnabled(nermodul.verfuegbar())
        self.ner.setChecked(nermodul.verfuegbar())
        if not nermodul.verfuegbar():
            self.ner.setToolTip(
                self.tr("Extra 'ner' und ein deutsches spaCy-Modell installieren."))
        form.addRow(self.ner)
        aussen.addWidget(optionen)

    def _baue_medizinstufen(self, form: QFormLayout) -> None:
        """Medical content in four levels, each on its own, all off by default: substance
        and dosage of medications, parameter and value of lab results."""
        self.medikamente = QCheckBox(self.tr(
            "Medikamente: Substanz schwärzen (Wirkstoff, Handelsname)"))
        self.medikamente.setToolTip(self.tr(
            "Medikamentennamen aus dem Lexikon (Wirkstoffe, einige Handelsnamen) und alle"
            " Begriffe der Kategorie „Medikament“ in „3 Fachvokabular“; Dosierung und Schema"
            " bleiben stehen. Standardmäßig aus: medizinischer Inhalt."))
        form.addRow(self.medikamente)
        self.dosierungen = QCheckBox(self.tr(
            "Medikamente: Dosierung und Verordnungsschema schwärzen"))
        self.dosierungen.setToolTip(self.tr(
            "Was neben einer Substanz steht: Menge und Einheit, Einnahmeschema, Zeiten,"
            " Applikationsweg („500 mg 1-0-1“, „2 Tbl.“, „bei Bedarf“, „p. o.“); die Substanz"
            " selbst bleibt stehen, wenn sie nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.dosierungen)
        self.laborwerte = QCheckBox(self.tr(
            "Laborwerte: Untersuchung schwärzen (Parameter)"))
        self.laborwerte.setToolTip(self.tr(
            "Der Name der Untersuchung, wo immer er steht („TSH“, „Kreatinin“), aus der"
            " Laborliste des Lexikons und der Kategorie „Laborwert“ in „3 Fachvokabular“; der"
            " Messwert bleibt stehen, wenn er nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.laborwerte)
        self.messwerte = QCheckBox(self.tr("Laborwerte: Messwert schwärzen"))
        self.messwerte.setToolTip(self.tr(
            "Der Wert direkt hinter einer Untersuchung („2,3 mU/l“ in „TSH 2,3 mU/l“, auch"
            " „< 5 mg/l“ oder „von 6,2 %“); die Untersuchung selbst bleibt stehen, wenn sie"
            " nicht eigens gewählt ist. Standardmäßig aus."))
        form.addRow(self.messwerte)

    def _baue_vokabular(self, aussen: QVBoxLayout) -> None:
        """The line that says how many terms the vocabulary protects."""
        vokabular = QGroupBox(self.tr("Fachvokabular (geschützt vor NER und unscharfer Suche)"))
        vz = QHBoxLayout(vokabular)
        self.vokabular_info = QLabel("")
        self.vokabular_info.setWordWrap(True)
        vz.addWidget(self.vokabular_info, 1)
        aussen.addWidget(vokabular)

    def _baue_empfaenger(self, aussen: QVBoxLayout) -> None:
        """The recipient group: tick box, key source with check button, the fingerprint
        line; disabled without an OpenPGP engine."""
        weitergabe = QGroupBox(self.tr("Empfänger (Pseudonyme zum Weitergeben)"))
        wf = QFormLayout(weitergabe)
        self.weitergabe = QCheckBox(self.tr(
            "Für einen Empfänger pseudonymisieren: Einmalschlüssel, die Zuordnung steht nur in"
            " einer verschlüsselten Kapsel für den Empfänger; lokal wird nichts gespeichert"))
        wf.addRow(self.weitergabe)
        zeile = QHBoxLayout()
        self.empfaenger_quelle = QLineEdit()
        self.empfaenger_quelle.setPlaceholderText(self.tr(
            "Öffentlicher Schlüssel: Datei (.asc) oder https-Adresse"))
        waehlen = QPushButton("…")
        waehlen.clicked.connect(self.empfaenger_waehlen)
        self.empfaenger_knopf = QPushButton(self.tr("Schlüssel prüfen"))
        self.empfaenger_knopf.clicked.connect(self.empfaenger_pruefen)
        zeile.addWidget(self.empfaenger_quelle, 1)
        zeile.addWidget(waehlen)
        zeile.addWidget(self.empfaenger_knopf)
        wf.addRow(zeile)
        self.empfaenger_info = QLabel("")
        self.empfaenger_info.setWordWrap(True)
        self.empfaenger_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        wf.addRow(self.empfaenger_info)
        einstellungen = lade_einstellungen()
        self.empfaenger_quelle.setText(einstellungen.empfaenger_schluessel)
        if not pgpmodul.verfuegbar():
            weitergabe.setEnabled(False)
            weitergabe.setToolTip(self.tr("Extra 'pgp' (pysequoia) oder GnuPG installieren."))
        aussen.addWidget(weitergabe)

    def _baue_dokumente(self, aussen: QVBoxLayout) -> None:
        """The list of the session's documents with their state, and the progress bar."""
        aussen.addWidget(QLabel(self.tr("Dokumente dieser Sitzung:")))
        self.liste = QListWidget()
        aussen.addWidget(self.liste, 1)
        self.fortschritt = QProgressBar()
        self.fortschritt.setRange(0, 1)
        self.fortschritt.setValue(0)
        aussen.addWidget(self.fortschritt)

    def _baue_knoepfe(self, aussen: QVBoxLayout) -> None:
        """The status line and the two buttons: analyse, analyse again."""
        unten = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.knopf = QPushButton(self.tr("Analysieren"))
        self.knopf.setDefault(True)
        self.knopf.clicked.connect(self.analysieren)
        self.erneut_knopf = QPushButton(self.tr("Erneut analysieren"))
        self.erneut_knopf.setToolTip(self.tr(
            "Auch schon analysierte Dokumente mit den jetzigen Einstellungen neu durchsuchen."
            " Aufbereitung, Texterkennung und Korrekturen bleiben; Kästen, Masken,"
            " Aufnahmen und verworfene Treffer werden übernommen."))
        self.erneut_knopf.clicked.connect(lambda: self.analysieren(erneut=True))
        unten.addWidget(self.status, 1)
        unten.addWidget(self.knopf)
        unten.addWidget(self.erneut_knopf)
        aussen.addLayout(unten)

    # -- state -----------------------------------------------------------------------------------
    def aktualisiere(self) -> None:
        """Refresh the document list with each document's state and the vocabulary line."""
        self.liste.clear()
        for d in self.sitzung.dokumente:
            self.liste.addItem(f"{d.pfad.name}  –  {d.zustand}")
        self._vokabular_anzeigen()

    # -- vocabulary --------------------------------------------------------------------------------
    def _vokabular_anzeigen(self) -> None:
        """Say how many terms are protected and from which sources."""
        fb = fachmodul.standard()
        quellen = ", ".join(f"{name} {n}" for name, n in fb.quellen)
        self.vokabular_info.setText(self.tr(
            "{gesamt} geschützte Begriffe ({quellen}). Ansehen, ergänzen und importieren unter"
            " „3 Fachvokabular“.").format(gesamt=fb.anzahl, quellen=quellen))

    def _optionen_uebernehmen(self) -> None:
        """Copy the ticked options into the session's run options."""
        o = self.sitzung.optionen
        o.datumsform = self.datum.currentData()
        o.codes = self.codes.isChecked()
        o.unlesbar = "schwaerzen" if self.unlesbar.isChecked() else "lassen"
        o.ner = self.ner.isChecked()
        o.fachbegriffe = self.fachbegriffe.isChecked()
        o.identifikatoren = self.identifikatoren.isChecked()
        o.medikamente_schwaerzen = self.medikamente.isChecked()
        o.dosierungen_schwaerzen = self.dosierungen.isChecked()
        o.laborwerte_schwaerzen = self.laborwerte.isChecked()
        o.messwerte_schwaerzen = self.messwerte.isChecked()
        o.unscharf_text = self.unscharf.isChecked()

    def _melde(self, titel: str, text: str) -> None:
        """Show an information box; synchronous mode (tests) raises instead of blocking."""
        if self.synchron:
            raise RuntimeError(text)
        QMessageBox.information(self, titel, text)

    # -- recipient ---------------------------------------------------------------------------------
    def empfaenger_waehlen(self) -> None:
        """Pick the recipient's public key file and check it at once."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Öffentlicher Schlüssel des Empfängers"), "",
            self.tr("OpenPGP-Schlüssel (*.asc *.gpg *.pgp);;Alle Dateien (*)"))
        if pfad:
            self.empfaenger_quelle.setText(pfad)
            self.empfaenger_pruefen()

    def empfaenger_pruefen(self) -> pgpmodul.Zertifikat | None:
        """Load the key, show fingerprint and names, pin the fingerprint in the settings
        and warn when a key at the same address changed."""
        quelle = self.empfaenger_quelle.text().strip()
        if not quelle:
            self.empfaenger_info.setText(self.tr("Bitte Schlüsseldatei oder Adresse angeben."))
            self.zertifikat = None
            return None
        try:
            z = pgpmodul.lade_zertifikat(quelle)
        except pgpmodul.PGPFehler as e:
            self.zertifikat = None
            self.empfaenger_info.setText(self.tr("Schlüssel nicht brauchbar: {fehler}").format(
                fehler=e))
            return None
        einstellungen = lade_einstellungen()
        warnung = self._fingerabdruck_warnung(einstellungen, quelle, z)
        einstellungen.empfaenger_schluessel = quelle
        einstellungen.empfaenger_fingerabdruck = z.fingerabdruck
        try:
            speichere_einstellungen(einstellungen)
        except OSError:
            pass
        self.zertifikat = z
        self.empfaenger_info.setText(self.tr(
            "Empfänger: {beschreibung}. Fingerabdruck mit dem Empfänger auf einem zweiten Weg"
            " abgleichen (Webseite, Briefkopf, Telefon).").format(beschreibung=z.beschreibung())
            + warnung)
        return z

    def _fingerabdruck_warnung(self, gemerkt: Einstellungen, quelle: str,
                               z: pgpmodul.Zertifikat) -> str:
        """The warning when the key at the pinned address no longer has the pinned
        fingerprint; an empty string otherwise."""
        if gemerkt.empfaenger_schluessel == quelle and gemerkt.empfaenger_fingerabdruck \
                and gemerkt.empfaenger_fingerabdruck != z.fingerabdruck:
            return self.tr(" ACHTUNG: Der Fingerabdruck weicht vom früher geprüften ab"
                           " ({alt}). Nur weiterverwenden, wenn der Empfänger den neuen"
                           " Schlüssel bestätigt hat.").format(alt=gemerkt.empfaenger_fingerabdruck)
        return ""

    # -- analysis ----------------------------------------------------------------------------------
    def analysieren(self, erneut: bool = False) -> None:
        """Run the analysis over the documents not analysed yet (over all of them with
        ``erneut``), after settling the recipient and checking patient and documents."""
        if not self._empfaenger_uebernehmen() or not self._startbereit():
            return
        self._optionen_uebernehmen()
        self._warteschlange = self._zu_analysieren(erneut)
        self._gesamt = len(self._warteschlange)
        self.fortschritt.setRange(0, max(1, self._gesamt))
        self.fortschritt.setValue(0)
        self._naechstes()

    def _empfaenger_uebernehmen(self) -> bool:
        """Switch the session's recipient mode to match the tick box; False when a
        recipient is wanted but no usable key is at hand (the user has been told)."""
        if not self.weitergabe.isChecked():
            if self.sitzung.empfaenger is not None:
                self.sitzung.setze_empfaenger(None)
            return True
        zertifikat = self.zertifikat if self.zertifikat is not None else self.empfaenger_pruefen()
        if zertifikat is None:
            self._melde(self.tr("Empfänger"), self.tr(
                "Für einen Empfänger braucht es dessen öffentlichen Schlüssel."))
            return False
        if self.sitzung.empfaenger is None or \
                self.sitzung.empfaenger.fingerabdruck != zertifikat.fingerabdruck:
            self.sitzung.setze_empfaenger(zertifikat)
        return True

    def _startbereit(self) -> bool:
        """False, with a message, while the patient or the documents are missing or the
        documents are still being loaded."""
        if self.sitzung.patient is None:
            self._melde(self.tr("Analysieren"), self.tr("Bitte zuerst einen Patienten übernehmen."))
            return False
        if not self.sitzung.dokumente:
            self._melde(self.tr("Analysieren"), self.tr("Bitte zuerst Dokumente hinzufügen."))
            return False
        if self.laedt():
            self._melde(self.tr("Analysieren"),
                        self.tr("Die Dokumente werden noch geladen; bitte einen Moment warten."))
            return False
        return True

    def _zu_analysieren(self, erneut: bool) -> list[Dokument]:
        """The documents to run: those not analysed yet or already exported, every one
        when a re-analysis is wanted."""
        return [d for d in self.sitzung.dokumente
                if erneut or d.vorbereitung is None or d.vorbereitung.abgeschlossen]

    def _naechstes(self) -> None:
        """Analyse the next queued document (in a worker thread unless synchronous) or,
        with the queue empty, report completion and emit ``analysiert``."""
        self.aktualisiere()
        if not self._warteschlange:
            self.knopf.setEnabled(True)
            self.fortschritt.setValue(self.fortschritt.maximum())
            self.status.setText(self.tr("Fertig: {n} Dokument(e) analysiert.").format(
                n=self._gesamt))
            self.analysiert.emit()
            return
        d = self._warteschlange[0]
        self.knopf.setEnabled(False)
        nummer = self._gesamt - len(self._warteschlange) + 1
        self.fortschritt.setValue(nummer - 1)
        self.status.setText(self.tr("Analysiere {nummer} von {gesamt}: {name} …").format(
            nummer=nummer, gesamt=self._gesamt, name=d.pfad.name))
        if self.synchron:
            try:
                self.sitzung.analysiere(d)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self._fehler(e, d)
                return
            self._fertig(d)
            return
        self._arbeiter = Arbeiter(lambda: self.sitzung.analysiere(d), self)
        self._arbeiter.fertig.connect(lambda _r, d=d: self._fertig(d))
        self._arbeiter.fehler.connect(lambda e, d=d: self._fehler(e, d))
        self._arbeiter.start()

    def _fertig(self, d: Dokument) -> None:
        """Take the analysed document off the queue and go on with the next."""
        if self._warteschlange and self._warteschlange[0] is d:
            self._warteschlange.pop(0)
        self._naechstes()

    def _fehler(self, e: Exception, d: Dokument) -> None:
        """Handle a failed analysis: an uncertain match asks the user and retries the same
        document (or stops the run), any other error is shown and the run goes on."""
        if self.synchron:  # tests: never block on a modal dialog
            raise e
        if isinstance(e, UnsichererAbgleich):
            entscheidung = self.unsicher_dialog(e)
            if entscheidung is None:
                self._warteschlange.clear()
                self.knopf.setEnabled(True)
                self.status.setText(self.tr("Abgebrochen."))
                return
            self.sitzung.abgleich = entscheidung
            self._naechstes()  # same document again
            return
        d.fehler = str(e)
        QMessageBox.warning(self, d.pfad.name, str(e))
        if self._warteschlange and self._warteschlange[0] is d:
            self._warteschlange.pop(0)
        self._naechstes()

    def unsicher_dialog(self, e: UnsichererAbgleich) -> str | None:
        """Ask whether the patient is one of the similar stored people; None = cancel."""
        zeilen = [self.tr("{kennung}: {nachname}, {vorname}, geb. {geb} (Übereinstimmung {score})")
                  .format(kennung=k.kennung, nachname=k.nachname, vorname=k.vorname,
                          geb=k.geburtsdatum or "–", score=f"{k.score:.2f}") for k in e.kandidaten]
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Ähnliche Person im Pseudonymspeicher"))
        box.setText(self.tr("Diese Person ähnelt einem vorhandenen Eintrag. Dieselbe Person?"))
        box.setInformativeText("\n".join(zeilen))
        gleich = box.addButton(self.tr("Ja, {kennung}").format(kennung=e.kandidaten[0].kennung),
                               QMessageBox.ButtonRole.YesRole)
        neu = box.addButton(self.tr("Nein, neue Person"), QMessageBox.ButtonRole.NoRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is gleich:
            return f"gleich:{e.kandidaten[0].kennung}"
        if box.clickedButton() is neu:
            return "neu"
        return None
