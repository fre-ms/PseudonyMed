"""The lists of a lex store in the steps "2 Identifikatoren" and "3 Fachvokabular", each
with a box to tick: what is ticked is searched for (identifiers) or protects (vocabulary).

It is lexcor's list of packages, and the ticks are the store's: the same that lexterm's and
lexident's package window and command line set, and ``pseudonymed fachbegriffe|identifikatoren
ankreuzen|abwaehlen`` (:mod:`pseudonymed.detection.choice`).  PseudonyMed adds taking a
terminology in, applied at once or not yet, and out again (never one of PseudonyMed's own
lists, which the table below edits), and in the vocabulary step lexterm's text on where
terminologies come from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lexcor import Package, Store, StoreError, read_codesystem, read_fhir_package
from lexcor.package import is_fhir_package
from lexcor.window import URL, PackageList, Reader, take_in
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.core import names
from pseudonymed.detection import choice
from pseudonymed.detection.store_table import own_lists
from pseudonymed.gui.translation import interface_language, shown_error
from pseudonymed.gui.worker import Worker


def _read_fhir(path: Path) -> list[Package]:
    """Every CodeSystem of a FHIR package, or the one of a JSON file."""
    return read_fhir_package(path)[0] if is_fhir_package(path) else [read_codesystem(path)]


FHIR = Reader(("FHIR CodeSystem oder FHIR-Paket", "FHIR CodeSystem or FHIR package"),
              _read_fhir, False, "*.json *.tgz *.tar.gz")


@dataclass(frozen=True)
class Lists:
    """What a step shows: what a tick means there (``kind``), how its store is opened and
    where it lies, the forms of terminology it takes in, and the text on where they come
    from (HTML in a language), if any."""

    kind: str
    open_store: Callable[[Path], Store]
    path: Path
    readers: tuple[Reader, ...] = (FHIR,)
    sources: Callable[[str], str] | None = None


class ListsPanel(QGroupBox):
    """The lists of one store with their ticks, and taking terminologies in and out."""

    def __init__(self, lists: Lists, changed: Callable[[], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lists, self.changed = lists, changed
        self.synchronous = False
        self.worker: Worker | None = None
        self.store: Store | None = None
        self.setTitle(self.tr("Listen und Terminologien"))
        intro = QLabel(self._intro())
        intro.setWordWrap(True)
        self.list = PackageList(self._opened())
        self.list.setMaximumHeight(170)
        self.list.chosen_changed.connect(self._ticked)
        self.list.currentItemChanged.connect(lambda *_: self._enable())
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list)
        layout.addLayout(self._buttons())
        layout.addWidget(self.status)
        self.refresh()

    def _intro(self) -> str:
        """What a tick means in this step."""
        if self.lists.kind == choice.VOCABULARY:
            return self.tr(
                "Angekreuzte Listen schützen ihre Begriffe vor NER und unscharfer Suche. Die"
                " Häkchen gehören zur Ablage: lexterm wendet dieselben an, und auf der"
                " Kommandozeile setzt sie „pseudonymed fachbegriffe ankreuzen|abwaehlen“.")
        return self.tr(
            "Angekreuzte Listen werden in der Analyse gesucht. Die Häkchen gehören zur"
            " Ablage: lexident wendet dieselben an, und auf der Kommandozeile setzt sie"
            " „pseudonymed identifikatoren ankreuzen|abwaehlen“.")

    def _buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.take = QPushButton(self.tr("Terminologie aufnehmen …"))
        menu = QMenu(self.take)
        for reader in self.lists.readers:
            action = menu.addAction(self._name(reader) + " …")
            action.triggered.connect(lambda _=False, r=reader: self._ask_take(r))
        self.take.setMenu(menu)
        self.drop = QPushButton(self.tr("Terminologie entfernen"))
        self.drop.setToolTip(self.tr(
            "Entfernt eine aufgenommene Terminologie aus der Ablage; Ihre eigenen Listen"
            " bearbeiten Sie in der Tabelle unten"))
        self.drop.clicked.connect(self._ask_remove)
        self.apply_new = QCheckBox(self.tr("gleich anwenden"))
        self.apply_new.setChecked(True)
        self.apply_new.setToolTip(self.tr(
            "Eine aufgenommene Terminologie ist angekreuzt; ohne dieses Häkchen wird sie erst"
            " angewandt, wenn Sie sie ankreuzen"))
        row.addWidget(self.take)
        row.addWidget(self.apply_new)
        row.addWidget(self.drop)
        if self.lists.sources is not None:
            where = QPushButton(self.tr("Woher kommen Terminologien?"))
            where.clicked.connect(self.show_sources)
            row.addWidget(where)
        row.addStretch(1)
        return row

    @staticmethod
    def _name(reader: Reader) -> str:
        return reader.name[1] if interface_language().startswith("en") else reader.name[0]

    # -- the store and the ticks -------------------------------------------------------------------
    def _opened(self) -> Store:
        """The store, opened afresh: a backup put back may have put another file in its
        place."""
        if self.store is not None:
            self.store.close()
        self.store = self.lists.open_store(self.lists.path)
        return self.store

    def refresh(self) -> None:
        """Show the lists of the store again, ticked as the store keeps them."""
        if self.worker is not None:
            return
        store = self._opened()
        self.list.store = store
        self.list.refresh(choice.applied(store))
        self._mark_others()
        self._enable()

    def _mark_others(self) -> None:
        """Show PseudonyMed's own lists by their labels in the interface language, and say of
        every other list that another program brought it: a terminology may well carry the
        title of one of PseudonyMed's lists."""
        own, items = self._own(), QTreeWidgetItemIterator(self.list)
        language = interface_language()
        while items.value():
            item = items.value()
            if str(item.data(0, URL)) in own:
                item.setText(0, names.label(item.text(0), language))
            else:
                notes = [self.tr("aus einem anderen Programm"), item.text(4)]
                item.setText(4, "; ".join(n for n in notes if n))
            items += 1

    def _ticked(self, urls: list[str]) -> None:
        """Keep the ticks and let the step know that what protects or is searched changed."""
        if self.store is not None:
            choice.remember(self.store, urls)
            self.changed()

    def _own(self) -> set[str]:
        return set() if self.store is None else {info.url for info in own_lists(self.store)}

    def _enable(self) -> None:
        url = self.list.current_url()
        self.drop.setEnabled(url is not None and url not in self._own())

    # -- taking in and out -------------------------------------------------------------------------
    def _ask_take(self, reader: Reader) -> None:
        if reader.folder:
            chosen = QFileDialog.getExistingDirectory(self, self._name(reader))
        else:
            chosen, _filter = QFileDialog.getOpenFileName(
                self, self._name(reader), "", f"{self._name(reader)} ({reader.patterns})")
        if chosen:
            self.take_in(Path(chosen), reader)

    def _take(self, path: Path, reader: Reader, ticked: bool) -> list[str]:
        """Read and take in, over a store of its own (this runs beside the window), ticked
        or unticked as the person chose: the choice holds even for a package that was
        unticked before it was taken out."""
        with self.lists.open_store(self.lists.path) as store:
            infos = take_in(store, path, reader)
            choice.switch(store, [info.url for info in infos], ticked)
            return [info.title or info.url for info in infos]

    def take_in(self, path: Path, reader: Reader = FHIR) -> None:
        """Take a terminology in beside the window: applied at once, or, without the tick
        "gleich anwenden", only once the person ticks it."""
        self.status.setText(self.tr("Wird aufgenommen: {name} …").format(name=path.name))
        ticked = self.apply_new.isChecked()
        if self.synchronous:
            self._taken(self._take(path, reader, ticked))
            return
        self.setEnabled(False)
        self.worker = Worker(lambda: self._take(path, reader, ticked), self)
        self.worker.done.connect(self._taken)
        self.worker.error.connect(self._failed)
        self.worker.start()

    def _taken(self, titles: object) -> None:
        self.worker = None
        self.setEnabled(True)
        self.refresh()
        self.changed()
        names = ", ".join(map(str, titles)) if isinstance(titles, list) else ""
        self.status.setText(self.tr("Aufgenommen und angewandt: {namen}").format(namen=names)
                            if self.apply_new.isChecked() else self.tr(
                                "Aufgenommen, noch nicht angewandt: {namen}").format(namen=names))

    def _failed(self, error: object) -> None:
        self.worker = None
        self.setEnabled(True)
        message = shown_error(error) if isinstance(error, BaseException) else str(error)
        self.status.setText(self.tr("Nicht aufgenommen: {fehler}").format(fehler=message))

    def _ask_remove(self) -> None:
        url = self.list.current_url()
        if url is None or self.store is None:
            return
        if not self.synchronous and QMessageBox.question(
                self, self.tr("Terminologie entfernen"),
                self.tr("„{name}“ aus der Ablage entfernen?").format(
                    name=self.store.info(url).title or url)) != QMessageBox.StandardButton.Yes:
            return
        self.remove(url)

    def remove(self, url: str) -> bool:
        """Take a terminology of another program out of the store; PseudonyMed's own lists
        stay.  True when it went."""
        if self.store is None or url in self._own():
            return False
        try:
            title = self.store.info(url).title or url
            self.store.remove_package(url)
        except StoreError as error:
            self.status.setText(shown_error(error))
            return False
        self.refresh()
        self.changed()
        self.status.setText(self.tr("Entfernt: {name}").format(name=title))
        return True

    def show_sources(self) -> QDialog | None:
        """Show where terminologies come from, with the links, in a window of its own."""
        if self.lists.sources is None:
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Woher kommen Terminologien?"))
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setHtml(self.lists.sources(interface_language()[:2]))
        QVBoxLayout(dialog).addWidget(browser)
        dialog.resize(640, 560)
        if not self.synchronous:
            dialog.show()
        return dialog
