"""The shared base of the list steps: the button texts that moved into it still translate
on both pages, the selection maps back to model rows through sort and filter, and the
model plumbing answers Qt as before."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from pseudonymed.detection import lexicon
from pseudonymed.detection.identifiers import Entry
from pseudonymed.gui.identifiers_page import IdentifiersPage
from pseudonymed.gui.session import Session
from pseudonymed.gui.vocabulary_page import VocabularyPage
from tests.test_gui import qapp  # noqa: F401

COMMON_DE = {"Ausgewählte löschen", "CSV exportieren …", "CSV oder Liste importieren …",
             "Aktualisieren"}
COMMON_EN = {"Delete selected", "Export CSV …", "Import CSV or list …", "Refresh"}


def _button_texts(page: IdentifiersPage | VocabularyPage) -> set[str]:
    return {k.text() for k in page.findChildren(QPushButton)}


def test_common_buttons_translated(qapp: QApplication) -> None:  # noqa: F811
    """The button texts that live in the base class reach the English interface of both
    pages: Qt looks them up under the page's class first, then under the base's."""
    from pseudonymed.gui import translation

    translator = translation.install(qapp, "en_gb")
    assert translator is not None
    try:
        for page in (IdentifiersPage(Session(store_path=None)),
                     VocabularyPage(Session(store_path=None))):
            texts = _button_texts(page)
            assert COMMON_EN <= texts and not COMMON_DE & texts, texts
    finally:
        qapp.removeTranslator(translator)
    assert COMMON_DE <= _button_texts(VocabularyPage(Session(store_path=None)))


def test_selection_follows_the_sorting(qapp: QApplication) -> None:  # noqa: F811
    """Selected view rows come back as model rows in model order, whatever the sort."""
    page = VocabularyPage(Session(store_path=None))
    page.model.load([("Wade", "Anatomie", "2026-01-02T10:00:00"),
                     ("Arm", "Anatomie", "2026-01-02T10:00:00"),
                     ("Knie", "Anatomie", "2026-01-02T10:00:00")])
    page.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)  # Wade, Knie, Arm
    selection = page.view.selectionModel()
    for line in (0, 2):
        selection.select(page.proxy.index(line, 0), QItemSelectionModel.SelectionFlag.Select
                         | QItemSelectionModel.SelectionFlag.Rows)
    assert page.selected_entries() == ["Wade", "Arm"]
    assert page.model.data(page.model.index(0, 2)) == "2026-01-02 10:00"


def test_model_answers_qt(qapp: QApplication) -> None:  # noqa: F811
    """Only term and class are editable, the header names the columns, a foreign role
    yields nothing, and an edit in a foreign role is refused before it reaches the page."""
    page = IdentifiersPage(Session(store_path=None))
    model = page.model
    model.load([Entry("Musterwerk AG", "Sonstiges", lexicon.USER, "", "")])
    assert model.rowCount() == 1 and model.columnCount() == 4
    assert [model.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == [
        "Begriff", "Klasse", "Angaben", "Quelle"]
    assert model.headerData(0, Qt.Orientation.Vertical) is None
    assert model.data(model.index(0, 3)) == "eigene"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole) is None
    editable = [bool(model.flags(model.index(0, i)) & Qt.ItemFlag.ItemIsEditable)
                for i in range(4)]
    assert editable == [True, True, False, False]
    assert not model.setData(model.index(0, 0), "Anderes", Qt.ItemDataRole.DisplayRole)
    assert model.data(model.index(0, 0)) == "Musterwerk AG"
