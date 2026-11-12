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

from pseudonymed.ablauf import Ergebnis
from pseudonymed.gui.arbeiter import Arbeiter
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.pruefung.bericht import als_text

AMPELFARBEN = {"grau": "#9a9a9a", "gruen": "#28a040", "gelb": "#e6a500", "rot": "#c81e1e"}


class ExportSeite(QWidget):
    """Step 9: the re-check with its two lights (re-check, residual risk), the output
    options and the export of the checked files."""

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self._arbeiter: Arbeiter | None = None
        self._warteschlange: list[Dokument] = []
        self._phase = "pruefen"
        aussen = QVBoxLayout(self)
        self._baue_ampeln(aussen)
        self._baue_optionen(aussen)
        self._baue_ausgabe(aussen)
        self._ampel_setzen("grau", self.tr("Noch nicht geprüft."))

    # -- layout ------------------------------------------------------------------------------------
    def _baue_ampeln(self, aussen: QVBoxLayout) -> None:
        """The two lights with their texts, the re-check button and the tick box that
        confirms a red residual risk."""
        kopf = QHBoxLayout()
        kopf.addWidget(QLabel(self.tr("Erneute Prüfung des Ergebnisses:")))
        self.ampel_text = QLabel("")
        self.ampel_text.setWordWrap(True)
        kopf.addWidget(self.ampel_text, 1)
        self.ampel = QLabel()
        self.ampel.setFixedSize(28, 28)
        kopf.addWidget(self.ampel)
        self.erneut = QPushButton(self.tr("Erneut prüfen"))
        self.erneut.clicked.connect(self.pruefen)
        kopf.addWidget(self.erneut)
        aussen.addLayout(kopf)
        risiko = QHBoxLayout()
        risiko.addWidget(QLabel(self.tr("Restrisiko (Schätzung aus „8 Kontrolle“):")))
        self.risiko_text = QLabel("")
        self.risiko_text.setWordWrap(True)
        risiko.addWidget(self.risiko_text, 1)
        self.risiko_ampel = QLabel()
        self.risiko_ampel.setFixedSize(28, 28)
        risiko.addWidget(self.risiko_ampel)
        aussen.addLayout(risiko)
        self.risiko_bestaetigt = QCheckBox(self.tr(
            "Rotes Restrisiko zur Kenntnis genommen: trotzdem exportieren"))
        self.risiko_bestaetigt.setVisible(False)
        self.risiko_bestaetigt.toggled.connect(lambda _an: self._knopf_schalten())
        aussen.addWidget(self.risiko_bestaetigt)

    def _baue_optionen(self, aussen: QVBoxLayout) -> None:
        """The output group: target folder and the files and features to write."""
        optionen = QGroupBox(self.tr("Ausgabe"))
        form = QFormLayout(optionen)
        zeile = QHBoxLayout()
        self.ziel = QLineEdit()
        self.ziel.setPlaceholderText(self.tr("leer = neben der Quelle"))
        waehlen = QPushButton("…")
        waehlen.clicked.connect(self.ziel_waehlen)
        zeile.addWidget(self.ziel, 1)
        zeile.addWidget(waehlen)
        form.addRow(self.tr("Zielordner"), zeile)
        self.textebene = QCheckBox(self.tr(
            "PDF mit Textebene: OCR-Text unsichtbar einbetten (Scans bleiben durchsuchbar)"))
        self.textebene.setChecked(True)
        self.textdatei = QCheckBox(
            self.tr("Textdatei (.txt) in Lesereihenfolge (Eingabe für das Sprachmodell)"))
        self.textdatei.setChecked(True)
        self.markdown = QCheckBox(self.tr("Markdown-Datei (.md) zusätzlich"))
        self.bericht = QCheckBox(self.tr("Prüfbericht (JSON)"))
        self.bericht.setChecked(True)
        self.vermerk = QCheckBox(self.tr("Vermerk in den PDF-Metadaten"))
        self.vermerk.setChecked(True)
        self.rastern = QCheckBox(self.tr(
            "PDF vollständig rastern (nur Bild; die Textebene wird danach unsichtbar ergänzt)"))
        self.vorschau = QCheckBox(self.tr("Vorschaubilder immer schreiben"))
        self.vokabular = QCheckBox(self.tr(
            "Vokabular der verworfenen Rate-Treffer (.vokabular.txt, ein Begriff je Zeile;"
            " in „3 Fachvokabular“ importierbar)"))
        self.vokabular.setChecked(True)
        for w in (self.textebene, self.textdatei, self.markdown, self.bericht, self.vokabular,
                  self.vermerk, self.rastern, self.vorschau):
            form.addRow(w)
        aussen.addWidget(optionen)

    def _baue_ausgabe(self, aussen: QVBoxLayout) -> None:
        """The protocol view, the status line and the buttons (open folder, export)."""
        self.ausgabe = QPlainTextEdit()
        self.ausgabe.setReadOnly(True)
        aussen.addWidget(self.ausgabe, 1)
        unten = QHBoxLayout()
        self.status = QLabel("")
        self.oeffnen = QPushButton(self.tr("Zielordner öffnen"))
        self.oeffnen.clicked.connect(self.ordner_oeffnen)
        self.oeffnen.setEnabled(False)
        self.knopf = QPushButton(self.tr("Exportieren"))
        self.knopf.clicked.connect(self.exportieren)
        unten.addWidget(self.status, 1)
        unten.addWidget(self.oeffnen)
        unten.addWidget(self.knopf)
        aussen.addLayout(unten)

    def ziel_waehlen(self) -> None:
        """Pick the target folder."""
        ordner = QFileDialog.getExistingDirectory(self, self.tr("Zielordner"))
        if ordner:
            self.ziel.setText(ordner)

    def _optionen_uebernehmen(self) -> None:
        """Copy the output options into the session's run options."""
        o = self.sitzung.optionen
        o.ziel_ordner = Path(self.ziel.text()) if self.ziel.text().strip() else None
        o.textdatei = self.textdatei.isChecked()
        o.markdown = self.markdown.isChecked()
        o.schwaerzen.textebene = self.textebene.isChecked()
        o.bericht = self.bericht.isChecked()
        o.vokabular = self.vokabular.isChecked()
        o.schwaerzen.vermerk = self.vermerk.isChecked()
        o.schwaerzen.rastern = self.rastern.isChecked()
        o.vorschau = "immer" if self.vorschau.isChecked() else "auto"

    def _pruefbereite(self) -> list[Dokument]:
        """The documents analysed and not yet exported."""
        return [d for d in self.sitzung.dokumente if d.pruefbereit]

    # -- traffic light -----------------------------------------------------------------------------
    def _ampel_setzen(self, farbe: str, text: str) -> None:
        """Colour the re-check light, show its text, then refresh risk light and button."""
        self.ampel.setStyleSheet(
            f"background: {AMPELFARBEN[farbe]}; border-radius: 14px; border: 1px solid #555;")
        self.ampel.setToolTip(text)
        self.ampel_text.setText(text)
        self.farbe = farbe
        self._risiko_setzen()
        self._knopf_schalten()

    def _risiko_setzen(self) -> None:
        """Colour the residual-risk light from the control step's estimate; the confirming
        tick box appears only for red and is cleared otherwise."""
        gesamt, fruehere = self.sitzung.kontrolle()
        self.risiko_farbe = gesamt.ampel if gesamt is not None else "grau"
        self.risiko_ampel.setStyleSheet(
            f"background: {AMPELFARBEN[self.risiko_farbe]}; border-radius: 14px;"
            " border: 1px solid #555;")
        if gesamt is None:
            self.risiko_text.setText(self.tr("noch nicht geschätzt"))
        else:
            self.risiko_text.setText(gesamt.kurz() + (
                self.tr(" (mit {n} früheren Dokumenten)").format(n=fruehere) if fruehere else ""))
        self.risiko_bestaetigt.setVisible(self.risiko_farbe == "rot")
        if self.risiko_farbe != "rot":
            self.risiko_bestaetigt.setChecked(False)

    def _knopf_schalten(self) -> None:
        """Enable the export button only for green or yellow, and for a red residual risk
        only once it is confirmed."""
        frei = self.farbe in ("gruen", "gelb")
        rot = getattr(self, "risiko_farbe", "grau") == "rot"
        if rot and not self.risiko_bestaetigt.isChecked():
            frei = False
        self.knopf.setEnabled(frei)

    def aktualisiere(self) -> None:
        """Called when the step is shown: run the re-check for everything that needs it."""
        farbe, text = self.sitzung.ampel()
        self._ampel_setzen(farbe, self.tr(text))
        if farbe == "grau" and any(d.pruefbereit and not d.vorpruefung_aktuell
                                   for d in self.sitzung.dokumente):
            self.pruefen()

    def pruefen(self) -> None:
        """Finishing phase into the scratch folder for every analysed document."""
        if self._warteschlange:
            return
        self._optionen_uebernehmen()
        self._phase = "pruefen"
        offen = self._pruefbereite()
        if offen:
            self._pruefung_starten(offen)

    def _pruefung_starten(self, dokumente: list[Dokument]) -> None:
        """Queue these documents for the re-check, clear the protocol and set the light."""
        self._phase = "pruefen"
        self._warteschlange = list(dokumente)
        self.ausgabe.clear()
        self._ampel_setzen("grau", self.tr("Erneute Prüfung läuft …"))
        self._naechstes()

    def _nach_pruefung(self) -> None:
        """Show the outcome of the re-check: the light, the status line and, for red, the
        residual hits per document."""
        farbe, text = self.sitzung.ampel()
        self._ampel_setzen(farbe, self.tr(text))
        if farbe == "rot":
            self._abbruch_melden()
        elif farbe == "gelb":
            self.status.setText(self.tr("Bereit; bitte die Warnungen lesen."))
        elif farbe == "gruen":
            self.status.setText(self.tr("Bereit zum Export."))

    def _abbruch_melden(self) -> None:
        """Status line and, outside synchronous mode, a dialog: the re-check still found
        patient data, nothing will be exported, with the residual hits per document."""
        meldung = self.tr(
            "Abbruch: Bei der erneuten Prüfung wurden noch Daten aus „1 Patientendaten“"
            " gefunden."
            " Es wird nichts exportiert. Bitte im Prüfschritt nacharbeiten (Kasten oder"
            " Maske) und erneut prüfen.") + "\n\n" + "\n".join(self._resttreffer_zeilen())
        self.status.setText(meldung.split("\n")[0])
        if not self.synchron:
            QMessageBox.critical(self, self.tr("Export nicht möglich"), meldung)

    def _resttreffer_zeilen(self) -> list[str]:
        """One line per document that failed the re-check: its name, the number of
        residual hits and the first of them with page and rule."""
        zeilen = []
        for d in self.sitzung.dokumente:
            if not d.pruefbereit or d.vorpruefung is None or d.vorpruefung.freigegeben:
                continue
            g = d.vorpruefung.bericht.gegenprobe
            reste = g.resttreffer if g is not None else []
            kurz = ", ".join(f"Seite {r['seite']}: {r['regel']}" for r in reste[:8])
            zeilen.append(f"{d.pfad.name}: {len(reste)} Resttreffer ({kurz})")
        return zeilen

    # -- export ------------------------------------------------------------------------------------
    def exportieren(self) -> None:
        """Write the checked files: re-check first when something changed since the last
        check, refuse while the re-check failed or a red residual risk is unconfirmed."""
        self._optionen_uebernehmen()
        offen = self._pruefbereite()
        if not offen:
            self._nichts_zu_exportieren()
            return
        if any(not d.vorpruefung_aktuell for d in offen):
            self._pruefung_starten(offen)  # something changed since the check: check again first
            return
        if not self._freigabe_steht(offen):
            return
        self._phase = "exportieren"
        self._warteschlange = list(offen)
        self._naechstes()

    def _nichts_zu_exportieren(self) -> None:
        """Tell the user that no analysed document is waiting; synchronous mode raises."""
        if self.synchron:
            raise RuntimeError("Keine analysierten Dokumente.")
        QMessageBox.information(self, self.tr("Export"),
                                self.tr("Keine analysierten Dokumente."))

    def _freigabe_steht(self, offen: list[Dokument]) -> bool:
        """True when every re-check passed and a red residual risk, if any, is confirmed;
        otherwise the page says why and synchronous mode raises."""
        if any(d.vorpruefung is None or not d.vorpruefung.freigegeben for d in offen):
            self._nach_pruefung()
            if self.synchron:
                raise RuntimeError("Erneute Prüfung nicht bestanden; nichts exportiert.")
            return False
        self._risiko_setzen()
        if self.risiko_farbe == "rot" and not self.risiko_bestaetigt.isChecked():
            self.status.setText(self.tr("Rotes Restrisiko: bitte in „8 Kontrolle“ nacharbeiten"
                                        " oder den Export ausdrücklich bestätigen."))
            if self.synchron:
                raise RuntimeError("Rotes Restrisiko nicht bestätigt; nichts exportiert.")
            return False
        return True

    def _naechstes(self) -> None:
        """Check or export the next queued document (in a worker thread unless synchronous)
        or, with the queue empty, finish the phase."""
        if not self._warteschlange:
            if self._phase == "pruefen":
                self._nach_pruefung()
            else:
                self.status.setText(self.tr("Fertig."))
                self.oeffnen.setEnabled(True)
                farbe, text = self.sitzung.ampel()
                self._ampel_setzen(farbe, self.tr(text))
            self.erneut.setEnabled(True)
            return
        d = self._warteschlange.pop(0)
        self.knopf.setEnabled(False)
        self.erneut.setEnabled(False)
        pruefen = self._phase == "pruefen"
        self.status.setText((self.tr("Prüfe {name} …") if pruefen
                             else self.tr("Exportiere {name} …")).format(name=d.pfad.name))
        if pruefen:
            aufgabe = lambda: self.sitzung.pruefe(d)  # noqa: E731
        else:
            aufgabe = lambda: self.sitzung.exportiere(d)  # noqa: E731
        if self.synchron:
            try:
                self._fertig(aufgabe())
            except Exception as e:  # noqa: BLE001
                self._fehler(e, d)
            return
        self._arbeiter = Arbeiter(aufgabe, self)
        self._arbeiter.fertig.connect(self._fertig)
        self._arbeiter.fehler.connect(lambda e, d=d: self._fehler(e, d))
        self._arbeiter.start()

    def _fertig(self, ergebnis: Ergebnis) -> None:
        """Append the document's report and outcome to the protocol, then go on."""
        text = als_text(ergebnis.bericht)
        if self._phase == "pruefen":
            text += (self.tr("Erneute Prüfung bestanden; Export möglich.") if ergebnis.freigegeben
                     else self.tr("Erneute Prüfung NICHT bestanden; Export gesperrt.")) + "\n"
        elif ergebnis.freigegeben:
            text += "".join(zeile + "\n" for zeile in self._freigabe_zeilen(ergebnis))
        else:
            text += self.tr("NICHT freigegeben.") + "\n"
        self.ausgabe.appendPlainText(text + "\n")
        self._naechstes()

    def _freigabe_zeilen(self, ergebnis: Ergebnis) -> list[str]:
        """The files written for a released document, one line each."""
        zeilen = [self.tr("Freigegeben: {pfad}").format(pfad=ergebnis.ziel)]
        if ergebnis.ziel_txt and not ergebnis.ist_text:
            zeilen.append(self.tr("Text: {pfad}").format(pfad=ergebnis.ziel_txt))
        if ergebnis.ziel_md:
            zeilen.append(self.tr("Markdown: {pfad}").format(pfad=ergebnis.ziel_md))
        if ergebnis.ziel_vokabular:
            zeilen.append(self.tr("Vokabular: {pfad}").format(pfad=ergebnis.ziel_vokabular))
        if ergebnis.ziel_kapsel:
            zeilen.append(self.tr("Schlüsselkapsel (nur an den Empfänger geben): {pfad}").format(
                pfad=ergebnis.ziel_kapsel))
        if ergebnis.vorschau:
            zeilen.append(self.tr("Vorschau: {pfad}").format(pfad=ergebnis.vorschau[0].parent))
        return zeilen

    def _fehler(self, e: Exception, d: Dokument) -> None:
        """Note the error on the document and in the protocol, then go on."""
        d.fehler = str(e)
        self.ausgabe.appendPlainText(
            self.tr("{name}: Fehler: {fehler}").format(name=d.pfad.name, fehler=e) + "\n")
        self._naechstes()

    def ordner_oeffnen(self) -> None:
        """Open the target folder (or the first document's folder) in the file manager."""
        ordner = self.sitzung.optionen.ziel_ordner
        if ordner is None and self.sitzung.dokumente:
            ordner = self.sitzung.dokumente[0].pfad.parent
        if ordner is None:
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(ordner)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(ordner)])
        else:
            subprocess.Popen(["xdg-open", str(ordner)])
