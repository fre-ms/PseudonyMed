"""The ticks on the lists of the lex stores are the stores' own: what is ticked protects
(lexterm's store) or is searched for (lexident's store), whoever ticked it, the steps'
lists, PseudonyMed's command line or the libraries' package window.  The steps show the
lists with their ticks, take terminologies in, applied at once or not yet, and out again,
and say where they come from."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import lexterm
import pytest
from lexcor import write_codesystem
from lexcor.window import URL
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QTextBrowser, QTreeWidgetItemIterator
from typer.testing import CliRunner

from pseudonymed import config
from pseudonymed.cli import app
from pseudonymed.detection import choice
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.detection.store_table import own_lists
from pseudonymed.gui.identifiers_page import IdentifiersPage
from pseudonymed.gui.session import Session
from pseudonymed.gui.vocabulary_page import VocabularyPage
from tests.test_foreign_packages import DIRECTORY, TERMINOLOGY, directory, terminology
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Data, stores and configuration of this test only."""
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path))
    monkeypatch.delenv("LEXCOR_DATA", raising=False)
    terms_module.standard.cache_clear()
    yield tmp_path
    terms_module.standard.cache_clear()


def test_the_ticks_are_the_stores_and_decide_what_protects(folder: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        own = {info.title: info.url for info in own_lists(v.store)}
        assert set(own.values()) == set(v.applied())  # at first every list is ticked
    with lexterm.open_store(vocab_mod.default_path()) as store:  # as lexterm sets them
        store.switch(own[vocab_mod.CATEGORY_BODY_PARTS], False)
        store.add_package(terminology())  # ticked as it comes in
    protected = terms_module.standard()  # built again: the ticks changed
    assert protected.is_medical_term("Flantzfieber")
    # the terminology is titled like the bundled body parts, which are unticked: what
    # protects is the terminology with its five wordings, not the bundled list
    assert dict(protected.sources)[vocab_mod.CATEGORY_BODY_PARTS] == 5
    with vocab_mod.Vocabulary() as v:
        choice.switch(v.store, [TERMINOLOGY], False)
    assert not terms_module.standard().is_medical_term("Flantzfieber")
    path = config.config_path()
    assert "[listen]" not in (path.read_text(encoding="utf-8") if path.exists() else "")


def test_the_ticks_decide_what_is_searched_for(folder: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_LEXICON", "1")  # the bundled lists, with the professions

    def found(text: str) -> list[str]:
        return [m.text for m in ident_module.standard().tag(text)]

    text = "Er arbeitet als Tischler, behandelt im Flantzklinikum Fremdstadt."
    with ident_module.IdentifierTable() as table:
        professions = next(i.url for i in own_lists(table.store) if i.title == "occupations")
        table.store.add_package(directory())
    assert "Tischler" in found(text) and "Flantzklinikum Fremdstadt" in found(text)
    with ident_module.IdentifierTable() as table:  # named in German, the list is found
        assert choice.switch(table.store, ["Berufe", DIRECTORY], False) == [
            professions, DIRECTORY]
    assert found(text) == []


def test_the_command_line_lists_ticks_and_unticks(folder: Path) -> None:
    runner = CliRunner()
    shown = runner.invoke(app, ["fachbegriffe", "listen"])
    assert shown.exit_code == 0 and "✓ Eponyme" in shown.output  # the label, in German
    assert vocab_mod.CATEGORY_EPONYMS in dict(terms_module.standard().sources)
    untick = runner.invoke(app, ["fachbegriffe", "abwaehlen", "Eponyme"])  # typed in German
    assert untick.exit_code == 0 and untick.output.startswith("Abgewählt: urn:uuid:")
    assert "  Eponyme" in runner.invoke(app, ["fachbegriffe", "listen"]).output
    assert vocab_mod.CATEGORY_EPONYMS not in dict(terms_module.standard().sources)
    unknown = runner.invoke(app, ["fachbegriffe", "ankreuzen", "Unbekannt"])
    assert unknown.exit_code == 1 and "Kein Paket" in unknown.output
    assert runner.invoke(app, ["fachbegriffe", "ankreuzen", "Eponyme"]).exit_code == 0
    assert vocab_mod.CATEGORY_EPONYMS in dict(terms_module.standard().sources)
    with ident_module.IdentifierTable() as table:
        table.store.add_package(directory())
    listed = runner.invoke(app, ["identifikatoren", "listen"]).output
    assert "(aus einem anderen Programm)" in listed
    assert runner.invoke(app, ["identifikatoren", "abwaehlen", DIRECTORY]).exit_code == 0
    with ident_module.IdentifierTable() as table:
        assert DIRECTORY not in choice.applied(table.store)


def _item(panel_list: object, url: str) -> object:
    iterator = QTreeWidgetItemIterator(panel_list)  # type: ignore[call-overload]
    while iterator.value():
        if iterator.value().data(0, URL) == url:
            return iterator.value()
        iterator += 1
    raise LookupError(url)


def test_the_vocabulary_step_shows_the_lists_with_the_stores_ticks(
        qapp: QApplication, folder: Path) -> None:  # noqa: F811
    with vocab_mod.Vocabulary() as v:
        v.store.add_package(terminology())
        own = [info.url for info in own_lists(v.store)]
        body_parts = v.store.local_url(vocab_mod.CATEGORY_BODY_PARTS)
        v.store.switch(own[0], False)  # unticked elsewhere: the step shows it so
    page = VocabularyPage(Session(store_path=None))
    page.synchronous = True
    page.refresh(True)
    panel = page.lists_panel
    assert panel is not None and panel.synchronous
    assert own[0] not in panel.list.chosen() and TERMINOLOGY in panel.list.chosen()
    # PseudonyMed's own list by its label in the interface language, the other program's
    # terminology by its title, though that is the English name of the bundled list
    assert _item(panel.list, body_parts).text(0) == "Körperteile"  # type: ignore[attr-defined]
    assert _item(panel.list, TERMINOLOGY).text(0) == (  # type: ignore[attr-defined]
        vocab_mod.CATEGORY_BODY_PARTS)
    assert _item(panel.list, TERMINOLOGY).text(4).startswith(  # type: ignore[attr-defined]
        "aus einem anderen Programm")
    _item(panel.list, TERMINOLOGY).setCheckState(  # type: ignore[attr-defined]
        0, Qt.CheckState.Unchecked)
    with vocab_mod.Vocabulary() as v:
        assert TERMINOLOGY not in choice.applied(v.store)  # the tick is the store's
    assert not terms_module.standard().is_medical_term("Flantzfieber")
    assert not panel.remove(own[1])  # PseudonyMed's own lists are edited in the table
    panel.list.setCurrentItem(_item(panel.list, TERMINOLOGY))  # type: ignore[arg-type]
    assert panel.drop.isEnabled() and panel.remove(TERMINOLOGY)
    write_codesystem(terminology(), folder / "terminology.json")
    panel.apply_new.setChecked(False)
    panel.take_in(folder / "terminology.json")
    taken = vocab_mod.CATEGORY_BODY_PARTS  # the terminology's own title
    assert panel.status.text() == f"Aufgenommen, noch nicht angewandt: {taken}"
    assert TERMINOLOGY not in panel.list.chosen()
    panel.remove(TERMINOLOGY)
    panel.apply_new.setChecked(True)
    panel.take_in(folder / "terminology.json")
    assert panel.status.text() == f"Aufgenommen und angewandt: {taken}"
    assert TERMINOLOGY in panel.list.chosen()
    assert terms_module.standard().is_medical_term("Flantzfieber")
    dialog = panel.show_sources()
    assert dialog is not None
    assert "Alpha-ID-SE" in dialog.findChild(QTextBrowser).toPlainText()


def test_the_identifiers_step_has_its_lists_without_sources(
        qapp: QApplication, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("PSEUDONYMED_LEXICON", "1")
    page = IdentifiersPage(Session(store_path=None))
    page.refresh(True)
    panel = page.lists_panel
    assert panel is not None and panel.list.chosen()
    texts = {button.text() for button in panel.findChildren(QPushButton)}
    assert "Terminologie aufnehmen …" in texts and "Woher kommen Terminologien?" not in texts
    assert panel.show_sources() is None
