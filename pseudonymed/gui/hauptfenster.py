"""The main window: eleven steps as tabs, a language menu."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox, QScrollArea, QTabWidget

from pseudonymed import WERKZEUG
from pseudonymed.gui.analysieren_seite import AnalysierenSeite
from pseudonymed.gui.datenbank_seite import DatenbankSeite
from pseudonymed.gui.dokumente_seite import DokumenteSeite
from pseudonymed.gui.export_seite import ExportSeite
from pseudonymed.gui.icon import app_icon
from pseudonymed.gui.identifikatoren_seite import IdentifikatorenSeite
from pseudonymed.gui.kontrolle_seite import KontrolleSeite
from pseudonymed.gui.patient_seite import PatientSeite
from pseudonymed.gui.pruefen_seite import PruefenSeite
from pseudonymed.gui.rueckersetzen_seite import RueckersetzenSeite
from pseudonymed.gui.sitzung import Sitzung
from pseudonymed.gui.texterkennung_seite import TexterkennungSeite
from pseudonymed.gui.vokabular_seite import VokabularSeite
from pseudonymed.konfig import lade_einstellungen, speichere_einstellungen


class Hauptfenster(QMainWindow):
    def __init__(self, sitzung: Sitzung | None = None) -> None:
        super().__init__()
        self.sitzung = sitzung or Sitzung()
        self.setWindowTitle(WERKZEUG)
        self.setWindowIcon(app_icon())
        self.resize(1200, 820)
        self.tabs = QTabWidget()
        self.patient = PatientSeite(self.sitzung)
        self.dokumente = DokumenteSeite(self.sitzung)
        self.texterkennung = TexterkennungSeite(self.sitzung)
        self.texterkennung.laedt = lambda: self.dokumente.laedt
        self.analysieren = AnalysierenSeite(self.sitzung)
        self.analysieren.laedt = lambda: self.dokumente.laedt
        self.pruefen = PruefenSeite(self.sitzung)
        self.export = ExportSeite(self.sitzung)
        self.rueckersetzen = RueckersetzenSeite(self.sitzung)
        self.datenbank = DatenbankSeite(self.sitzung)
        self.vokabular = VokabularSeite(self.sitzung)
        self.identifikatoren = IdentifikatorenSeite(self.sitzung)
        self.kontrolle = KontrolleSeite(self.sitzung)
        self._seiten: list = []
        for seite, titel in ((self.patient, self.tr("1 Patientendaten")),
                             (self.identifikatoren, self.tr("2 Identifikatoren")),
                             (self.vokabular, self.tr("3 Fachvokabular")),
                             (self.dokumente, self.tr("4 Dokumente")),
                             (self.texterkennung, self.tr("5 Texterkennung")),
                             (self.analysieren, self.tr("6 Analyse")),
                             (self.pruefen, self.tr("7 Sichtprüfung")),
                             (self.kontrolle, self.tr("8 Kontrolle")),
                             (self.export, self.tr("9 Export")),
                             (self.rueckersetzen, self.tr("10 Reidentifikation")),
                             (self.datenbank, self.tr("11 Datenbank"))):
            # a scroll area per step: the window shrinks below a step's natural size
            # and shows scrollbars instead of refusing to get smaller
            rahmen = QScrollArea()
            rahmen.setWidgetResizable(True)
            rahmen.setFrameShape(QScrollArea.Shape.NoFrame)
            rahmen.setWidget(seite)
            self.tabs.addTab(rahmen, titel)
            self._seiten.append(seite)
        self.setCentralWidget(self.tabs)
        self.setMinimumSize(720, 480)
        self.patient.uebernommen.connect(lambda _p: self.zeige_seite(self.dokumente))
        self.dokumente.weiter.connect(lambda: self.zeige_seite(self.texterkennung))
        self.dokumente.geladen.connect(self._nach_laden)
        self.texterkennung.weiter.connect(lambda: self.zeige_seite(self.analysieren))
        self.texterkennung.gelesen.connect(self.analysieren.aktualisiere)
        self.analysieren.analysiert.connect(self._nach_analyse)
        self.tabs.currentChanged.connect(self._tab_gewechselt)
        self.statusBar().showMessage(self.tr(
            "Alles bleibt auf diesem Rechner; nur das Adressbuch wird über das Netz gelesen."))
        self._sprachmenue()

    def _sprachmenue(self) -> None:
        menue = self.menuBar().addMenu(self.tr("Sprache"))
        for code, name in (("de", "Deutsch"), ("en", "English")):
            aktion = QAction(name, self)
            aktion.triggered.connect(lambda _c=False, code=code: self.sprache_waehlen(code))
            menue.addAction(aktion)

    def sprache_waehlen(self, code: str) -> None:
        e = lade_einstellungen()
        e.gui_sprache = code
        speichere_einstellungen(e)
        QMessageBox.information(self, self.tr("Sprache"), self.tr(
            "Die Sprache gilt ab dem nächsten Start von PseudonyMed."))

    def _nach_laden(self) -> None:
        """Every pending document is open.  A restored working state fills the patient
        step and the review and goes straight there."""
        self.texterkennung.aktualisiere()
        wieder = [d for d in self.sitzung.dokumente
                  if d.arbeitsstand is not None and d.vorbereitung is not None and not d.fehler]
        if wieder:
            if self.sitzung.patient is not None:
                self.patient.fuelle(self.sitzung.patient)
            self.analysieren.aktualisiere()
            self._nach_analyse()

    def _nach_analyse(self) -> None:
        self.dokumente.aktualisiere()
        self.pruefen.aktualisiere()
        self.zeige_seite(self.pruefen)

    def seite(self, index: int):
        """The step widget inside the scroll area of tab ``index``."""
        return self._seiten[index] if 0 <= index < len(self._seiten) else None

    def aktuelle_seite(self):
        return self.seite(self.tabs.currentIndex())

    def zeige_seite(self, seite) -> None:
        self.tabs.setCurrentIndex(self._seiten.index(seite))

    def _tab_gewechselt(self, index: int) -> None:
        seite = self.seite(index)
        if seite is self.rueckersetzen:
            self.rueckersetzen.aktualisiere()
        elif seite is self.datenbank:
            self.datenbank.aktualisiere()
        elif seite is self.vokabular:
            self.vokabular.aktualisiere()
        elif seite is self.identifikatoren:
            self.identifikatoren.aktualisiere()
        elif seite is self.analysieren:
            self.analysieren.aktualisiere()
        elif seite is self.texterkennung:
            self.texterkennung.aktualisiere()
        elif seite is self.export:
            self.export.aktualisiere()
        elif seite is self.kontrolle:
            self.kontrolle.aktualisiere()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.sitzung.schliessen()
        super().closeEvent(event)
