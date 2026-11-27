"""The main window: fourteen steps as tabs, a language menu.  Steps 5 to 8 are docrepro's
panels: preparing the pages, the filters for the OCR, the areas, the text."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox, QScrollArea, QTabWidget

from pseudonymed import TOOL
from pseudonymed.config import load_settings, save_settings
from pseudonymed.gui.analysis_page import AnalysisPage
from pseudonymed.gui.chain_pages import ChainStepPage
from pseudonymed.gui.control_page import ControlPage
from pseudonymed.gui.database_page import DatabasePage
from pseudonymed.gui.documents_page import DocumentsPage
from pseudonymed.gui.export_page import ExportPage
from pseudonymed.gui.icon import app_icon
from pseudonymed.gui.identifiers_page import IdentifiersPage
from pseudonymed.gui.patient_page import PatientPage
from pseudonymed.gui.reidentify_page import ReidentifyPage
from pseudonymed.gui.review_page import ReviewPage
from pseudonymed.gui.session import Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.vocabulary_page import VocabularyPage


class MainWindow(QMainWindow):
    """The main window: the fourteen steps as tabs around one session, a status line and
    the language menu.  Finishing a step opens the next; a step that shows state other
    steps change is refreshed whenever it is entered, and what was typed into docrepro's
    panels is taken into their projects whenever the step is left."""

    def __init__(self, session: Session | None = None) -> None:
        """Build the fourteen pages, wrap each in a scroll area and connect the steps."""
        super().__init__()
        self.session = session or Session()
        self.session.language = interface_language()  # reports and vocabulary files follow it
        self.setWindowTitle(TOOL)
        self.setWindowIcon(app_icon())
        self.resize(1200, 820)
        self.tabs = QTabWidget()
        self.patient = PatientPage(self.session)
        self.documents = DocumentsPage(self.session)
        self.prepare = ChainStepPage(self.session, "cleanup")
        self.filters = ChainStepPage(self.session, "filters")
        self.regions = ChainStepPage(self.session, "regions")
        self.text = ChainStepPage(self.session, "text")
        self.chain_steps = (self.prepare, self.filters, self.regions, self.text)
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
                                  self.identifiers, self.analyse, *self.chain_steps,
                                  self.export, self.control)
        self._pages: list = []
        for page, title in ((self.patient, self.tr("1 Patientendaten")),
                            (self.identifiers, self.tr("2 Identifikatoren")),
                            (self.vocabulary, self.tr("3 Fachvokabular")),
                            (self.documents, self.tr("4 Dokumente")),
                            (self.prepare, self.tr("5 Aufbereiten")),
                            (self.filters, self.tr("6 Filter für die Texterkennung")),
                            (self.regions, self.tr("7 Bereiche")),
                            (self.text, self.tr("8 Text prüfen")),
                            (self.analyse, self.tr("9 Analyse")),
                            (self.check, self.tr("10 Sichtprüfung")),
                            (self.control, self.tr("11 Kontrolle")),
                            (self.export, self.tr("12 Export")),
                            (self.reidentify, self.tr("13 Reidentifikation")),
                            (self.database, self.tr("14 Datenbank"))):
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
        self.documents.proceed.connect(lambda: self.show_page(self.prepare))
        self.documents.loaded.connect(self._after_loading)
        for step, following in zip(self.chain_steps, (*self.chain_steps[1:], self.analyse),
                                   strict=True):
            step.proceed.connect(lambda following=following: self.show_page(following))
            step.worked.connect(self.documents.refresh)
        self.filters.read_in.connect(self.analyse.refresh)
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
        """A step was entered: what was typed into docrepro's panels goes into their
        projects first; refresh the step when it is one of those that show state other
        steps change."""
        for step in self.chain_steps:
            step.commit()
        page = self.page(index)
        if page is not None and page in self._refresh_on_enter:
            page.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.session.close()
        super().closeEvent(event)
