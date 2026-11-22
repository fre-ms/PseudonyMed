"""The main window: eleven steps as tabs, a language menu."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox, QScrollArea, QTabWidget

from pseudonymed import TOOL
from pseudonymed.config import load_settings, save_settings
from pseudonymed.gui.analysis_page import AnalysisPage
from pseudonymed.gui.control_page import ControlPage
from pseudonymed.gui.database_page import DatabasePage
from pseudonymed.gui.documents_page import DocumentsPage
from pseudonymed.gui.export_page import ExportPage
from pseudonymed.gui.icon import app_icon
from pseudonymed.gui.identifiers_page import IdentifiersPage
from pseudonymed.gui.ocr_page import OcrPage
from pseudonymed.gui.patient_page import PatientPage
from pseudonymed.gui.reidentify_page import ReidentifyPage
from pseudonymed.gui.review_page import ReviewPage
from pseudonymed.gui.session import Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.vocabulary_page import VocabularyPage


class MainWindow(QMainWindow):
    """The main window: the eleven steps as tabs around one session, a status line and
    the language menu.  Finishing a step opens the next; a step that shows state other
    steps change is refreshed whenever it is entered."""

    def __init__(self, session: Session | None = None) -> None:
        """Build the eleven pages, wrap each in a scroll area and connect the steps."""
        super().__init__()
        self.session = session or Session()
        self.session.language = interface_language()  # reports and vocabulary files follow it
        self.setWindowTitle(TOOL)
        self.setWindowIcon(app_icon())
        self.resize(1200, 820)
        self.tabs = QTabWidget()
        self.patient = PatientPage(self.session)
        self.documents = DocumentsPage(self.session)
        self.ocr = OcrPage(self.session)
        self.ocr.is_loading = lambda: self.documents.is_loading
        self.analyse = AnalysisPage(self.session)
        self.analyse.is_loading = lambda: self.documents.is_loading
        self.check = ReviewPage(self.session)
        self.export = ExportPage(self.session)
        self.reidentify = ReidentifyPage(self.session)
        self.database = DatabasePage(self.session)
        self.vocabulary = VocabularyPage(self.session)
        self.identifiers = IdentifiersPage(self.session)
        self.control = ControlPage(self.session)
        # the steps that show state other steps change: refreshed each time they are entered
        # (the patient, document and review steps keep their own state and are left alone)
        self._refresh_on_enter = (self.reidentify, self.database, self.vocabulary,
                                  self.identifiers, self.analyse, self.ocr,
                                  self.export, self.control)
        self._pages: list = []
        for page, title in ((self.patient, self.tr("1 Patientendaten")),
                            (self.identifiers, self.tr("2 Identifikatoren")),
                            (self.vocabulary, self.tr("3 Fachvokabular")),
                            (self.documents, self.tr("4 Dokumente")),
                            (self.ocr, self.tr("5 Texterkennung")),
                            (self.analyse, self.tr("6 Analyse")),
                            (self.check, self.tr("7 Sichtprüfung")),
                            (self.control, self.tr("8 Kontrolle")),
                            (self.export, self.tr("9 Export")),
                            (self.reidentify, self.tr("10 Reidentifikation")),
                            (self.database, self.tr("11 Datenbank"))):
            # a scroll area per step: the window shrinks below a step's natural size
            # and shows scrollbars instead of refusing to get smaller
            frame = QScrollArea()
            frame.setWidgetResizable(True)
            frame.setFrameShape(QScrollArea.Shape.NoFrame)
            frame.setWidget(page)
            self.tabs.addTab(frame, title)
            self._pages.append(page)
        self.setCentralWidget(self.tabs)
        self.setMinimumSize(720, 480)
        self.patient.adopted.connect(lambda _p: self.show_page(self.documents))
        self.documents.proceed.connect(lambda: self.show_page(self.ocr))
        self.documents.loaded.connect(self._after_loading)
        self.ocr.proceed.connect(lambda: self.show_page(self.analyse))
        self.ocr.read_in.connect(self.analyse.refresh)
        self.analyse.analysed.connect(self._after_analysis)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.statusBar().showMessage(self.tr(
            "Alles bleibt auf diesem Rechner; nur das Adressbuch wird über das Netz gelesen."))
        self._language_menu()

    def _language_menu(self) -> None:
        """The menu with one action per interface language."""
        menu = self.menuBar().addMenu(self.tr("Sprache"))
        for code, name in (("de", "Deutsch"), ("en", "English")):
            action = QAction(name, self)
            action.triggered.connect(lambda _c=False, code=code: self.choose_language(code))
            menu.addAction(action)

    def choose_language(self, code: str) -> None:
        """Store the chosen interface language; it applies from the next start."""
        e = load_settings()
        e.gui_language = code
        save_settings(e)
        QMessageBox.information(self, self.tr("Sprache"), self.tr(
            "Die Sprache gilt ab dem nächsten Start von PseudonyMed."))

    def _after_loading(self) -> None:
        """Every pending document is open.  A restored working state fills the patient
        step and the review and goes straight there."""
        self.ocr.refresh()
        restored = [d for d in self.session.documents
                    if d.working_state is not None and d.preparation is not None and not d.error]
        if restored:
            if self.session.patient is not None:
                self.patient.fill_in(self.session.patient)
            self.analyse.refresh()
            self._after_analysis()

    def _after_analysis(self) -> None:
        """The analysis finished: refresh the document list and the review, then show it."""
        self.documents.refresh()
        self.check.refresh()
        self.show_page(self.check)

    def page(self, index: int):
        """The step widget inside the scroll area of tab ``index``."""
        return self._pages[index] if 0 <= index < len(self._pages) else None

    def current_page(self):
        """The step widget of the current tab."""
        return self.page(self.tabs.currentIndex())

    def show_page(self, page) -> None:
        """Switch to the tab that holds the step widget."""
        self.tabs.setCurrentIndex(self._pages.index(page))

    def _tab_changed(self, index: int) -> None:
        """A step was entered: refresh it when it is one of those that show state other
        steps change."""
        page = self.page(index)
        if page is not None and page in self._refresh_on_enter:
            page.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.session.close()
        super().closeEvent(event)
