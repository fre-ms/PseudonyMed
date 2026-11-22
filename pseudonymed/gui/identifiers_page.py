"""Step 2: identifiers beyond the patient's data as a sortable, editable table.

Professions, hospitals, study acronyms, titles and the user's own identifiers from the
lexicon of the pseudonym database.  Like the patient's data they are redacted by default;
each class has a context rule (see :mod:`pseudonymed.detection.identifiers`), and the
visual review may discard every hit.  Search, sort by any column, edit a cell, add and
delete rows, exchange CSV; deleted bundled rows can be brought back."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from pseudonymed.detection import identifiers as ident_module
from pseudonymed.gui.list_page import CellIndex, ListModel, ListPage
from pseudonymed.gui.translation import shown_error


class IdentifiersModel(ListModel):
    """Rows of the identifier list: term, class, details and source, the first two
    editable; an edit goes through the page to the database."""

    def __init__(self, page: IdentifiersPage) -> None:
        super().__init__(page)
        self.page = page
        self.entries: list[ident_module.Entry] = []
        self.title = [self.tr("Begriff"), self.tr("Klasse"), self.tr("Angaben"),
                      self.tr("Quelle")]

    def load(self, entries: list[ident_module.Entry]) -> None:
        """Replace all rows."""
        self.beginResetModel()
        self.entries = list(entries)
        self.endResetModel()

    def rowCount(self, parent: CellIndex | None = None) -> int:  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self.entries)

    def _cell(self, line: int, column: int) -> str:
        """Term, class, details or source; the user's own rows show "eigene" as source."""
        e = self.entries[line]
        return (e.term, e.category, e.details,
                self.tr("eigene") if e.own else e.source)[column]

    def _apply_edit(self, line: int, column: int, value: str) -> bool:
        """Hand the edit to the page and, when it took, show the row as the database now
        holds it (a changed bundled row becomes the user's own, so every column may move)."""
        new = self.page.change(self.entries[line], column, value)
        if new is None:
            return False
        self.entries[line] = new
        self.dataChanged.emit(self.index(line, 0), self.index(line, 3))
        return True


class IdentifiersPage(ListPage):
    """Step "2 Identifikatoren": the identifier list with class filter, cell editing,
    add, delete, CSV exchange and the return of deleted bundled rows."""

    FILTER_COLUMN = -1
    NARROW_COLUMNS = (1, 2, 3)
    model: IdentifiersModel

    # -- regions of the page -----------------------------------------------------------------------
    def _header_text(self) -> str:
        """What the step does and when each class counts as a hit."""
        return self.tr(
            "Identifikatoren außerhalb der Patientendaten: Kliniken, Berufe, Studien, Titel"
            " und Ihre eigenen (Arbeitgeber, Vereine, Einrichtungen). Wie die Patientendaten"
            " werden sie standardmäßig geschwärzt; in der Sichtprüfung lässt sich jeder Treffer"
            " verwerfen. Kliniken und eigene Einträge zählen überall, Berufe nur mit Hinweis"
            " wie „arbeitet als“ oder „von Beruf“, Studienkürzel nur neben „Studie“"
            " (Registernummern wie NCT immer), Titel nur vor oder nach einem erkannten Namen."
            " Der Erwerbsstatus (berentet, arbeitslos) bleibt nach der GeMTeX-Leitlinie stehen.")

    def _build_filter(self, outer: QVBoxLayout) -> None:
        """Search field, class filter and the row count."""
        filter_ = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Suchen …"))
        self.search.textChanged.connect(self._apply_filter)
        self.category_filter = QComboBox()
        self.category_filter.currentIndexChanged.connect(lambda _i: self.refresh(True))
        self.count = QLabel("")
        filter_.addWidget(self.search, 2)
        filter_.addWidget(QLabel(self.tr("Klasse")))
        filter_.addWidget(self.category_filter, 1)
        filter_.addWidget(self.count)
        outer.addLayout(filter_)

    def _new_model(self) -> IdentifiersModel:
        """The model over the identifier rows."""
        return IdentifiersModel(self)

    def _build_new(self, outer: QVBoxLayout) -> None:
        """Field for a new identifier, its class (the user's own by default) and the add
        button."""
        new = QHBoxLayout()
        self.new_term = QLineEdit()
        self.new_term.setPlaceholderText(self.tr("Neuer Identifikator (auch mehrere Wörter)"))
        self.new_term.returnPressed.connect(self.add)
        self.new_category = QComboBox()
        self.new_category.setEditable(True)
        self.new_category.addItems(list(ident_module.CATEGORIES))
        self.new_category.setCurrentText(ident_module.CATEGORY_OTHER)
        new_button = QPushButton(self.tr("Hinzufügen"))
        new_button.clicked.connect(self.add)
        new.addWidget(self.new_term, 2)
        new.addWidget(self.new_category, 1)
        new.addWidget(new_button)
        outer.addLayout(new)

    def _import_note(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        return self.tr(
            "CSV (begriff,klasse; Kopfzeile optional; Komma, Semikolon oder Tabulator) oder"
            " eine Liste mit einem Identifikator je Zeile")

    def _build_own_button(self) -> QPushButton:
        """The button that brings deleted bundled rows back; enabled while any exist."""
        self.restore_button = QPushButton(self.tr("Gelöschte wiederherstellen"))
        self.restore_button.setToolTip(self.tr(
            "Gelöschte mitgelieferte Einträge zurückholen; selbst geladene Zusatzdaten"
            " (Kliniken Österreich) bitte in „11 Datenbank“ neu laden"))
        self.restore_button.clicked.connect(self.restore)
        return self.restore_button

    # -- data ----------------------------------------------------------------------------------
    def _identifiers(self) -> ident_module.IdentifierTable:
        """A fresh handle on the identifier table, opened per operation."""
        return ident_module.IdentifierTable()

    def _clear_cache(self) -> None:
        """Forget the cached matcher so the next analysis is built from the edited rows.
        ``standard`` attaches ``cache_clear`` at run time, unseen by the type checker."""
        ident_module.standard.cache_clear()

    def refresh(self, force: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        with self._identifiers() as ident:
            version = ident.version()
            if not force and version == self._version:
                return self.model.rowCount()
            category = self.category_filter.currentData() or ""
            entries = ident.all_items(category)
            categories = ident.categories()
            self._total = ident.count()
            deleted = ident.deleted()
        self._version = version
        self.model.load(entries)
        self._fill_categories(categories)
        self.restore_button.setEnabled(deleted > 0)
        self._apply_filter()
        return len(entries)

    def _fill_categories(self, categories: list[tuple[str, int]]) -> None:
        """Refill the class filter (with counts) and the class box for new entries,
        keeping what was chosen in each."""
        self._refill_filter(self.category_filter, categories)
        current = self.new_category.currentText() or ident_module.CATEGORY_OTHER
        self.new_category.clear()
        self.new_category.addItems(list(dict.fromkeys(
            list(ident_module.CATEGORIES) + [k for k, _n in categories])))
        self.new_category.setCurrentText(current)

    def _apply_filter(self) -> None:
        """Apply the search text to the proxy and show "n of total"."""
        self.proxy.setFilterFixedString(self.search.text().strip())
        self.count.setText(self.tr("{n} von {gesamt} Einträgen").format(
            n=self.proxy.rowCount(), gesamt=getattr(self, "_total", self.model.rowCount())))

    def selected_entries(self) -> list[ident_module.Entry]:
        """The selected rows in table order."""
        return [self.model.entries[r] for r in self._selected_rows()]

    def _changed(self) -> None:
        """After an in-place edit: drop the matcher cache and take over the table's new
        version and count without reloading the rows."""
        self._clear_cache()
        with self._identifiers() as ident:
            self._version = ident.version()
            self._total = ident.count()

    # -- editing -----------------------------------------------------------------------------------
    def change(self, entry: ident_module.Entry, column: int,
               value: str) -> ident_module.Entry | None:
        """Change term (column 0) or class (column 1); returns the row as it is now."""
        with self._identifiers() as ident:
            new = ident.change(entry.key, term=value if column == 0 else None,
                               category=value if column == 1 else None)
        if new is not None:
            self._changed()
            self.status.setText(self.tr("Geändert: {begriff}").format(begriff=new.term))
        else:
            self.status.setText(self.tr(
                "Nicht geändert: {begriff} (zu kurz oder schon vorhanden)").format(begriff=value))
        return new

    def add(self, term: str | None = None, category: str | None = None) -> int:
        """Add one identifier (from the arguments or the entry row) in the given class;
        returns 1 when it was new, 0 when known or too short."""
        term = (term if term is not None else self.new_term.text()).strip()
        category = (category if category is not None else self.new_category.currentText()).strip()
        if len(term) < ident_module.MIN_LENGTH:
            self.status.setText(self.tr("Identifikatoren haben mindestens drei Zeichen."))
            return 0
        with self._identifiers() as ident:
            new = ident.add([term], category or ident_module.CATEGORY_OTHER)
        self._clear_cache()
        self.status.setText(self.tr("Hinzugefügt: {begriff}").format(begriff=term) if new
                            else self.tr("Schon vorhanden: {begriff}").format(begriff=term))
        self.new_term.clear()
        self.refresh(True)
        return new

    def delete(self, entries: list[ident_module.Entry] | None = None) -> int:
        """Delete the given (or selected) rows after a confirmation; bundled rows stay
        deleted across updates until restored.  Returns how many went."""
        entries = entries if entries is not None else self.selected_entries()
        if not entries:
            self.status.setText(self.tr("Nichts ausgewählt."))
            return 0
        if not self.synchronous and QMessageBox.question(
                self, self.tr("Löschen"),
                self.tr("{n} Identifikator(en) löschen? Mitgelieferte Einträge bleiben auch nach"
                        " Updates gelöscht, bis Sie sie wiederherstellen.").format(
                            n=len(entries))) != QMessageBox.StandardButton.Yes:
            return 0
        with self._identifiers() as ident:
            n = ident.remove(e.key for e in entries)
        self._clear_cache()
        self.status.setText(self.tr("{n} Identifikator(en) gelöscht.").format(n=n))
        self.refresh(True)
        return n

    def restore(self) -> int:
        """Bring every deleted bundled row back; returns how many."""
        with self._identifiers() as ident:
            n = ident.restore()
        self._clear_cache()
        self.status.setText(self.tr("{n} gelöschte Einträge wiederhergestellt.").format(n=n))
        self.refresh(True)
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def choose_and_export(self) -> None:
        """Ask for a target and write the identifiers as CSV."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Identifikatoren als CSV"),
                                              "identifikatoren.csv", self.tr("CSV (*.csv)"))
        if path:
            self.export_csv(Path(path))

    def export_csv(self, path: Path) -> int:
        """Write the rows of the current class filter to ``path``; returns the count."""
        category = self.category_filter.currentData() or ""
        with self._identifiers() as ident:
            n = ident.export_csv(path, category)
        self.status.setText(self.tr("{n} Einträge nach {pfad} geschrieben.").format(
            n=n, pfad=path))
        return n

    def choose_and_import(self) -> None:
        """Ask for a CSV or list file and take its identifiers into the table."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Identifikatoren importieren"), "",
            self.tr("CSV und Listen (*.csv *.txt);;Alle Dateien (*)"))
        if path:
            self.import_file(Path(path))

    def import_file(self, path: Path, category: str | None = None) -> int:
        """Take the identifiers of a CSV or list file into the table, rows without a class
        into ``category`` (default: the class box); returns how many were new."""
        category = (category if category is not None
                    else self.new_category.currentText()).strip() or ident_module.CATEGORY_OTHER
        try:
            with self._identifiers() as ident:
                new, read_in = ident.import_csv(path, category)
        except OSError as e:
            self._notify(self.tr("Identifikatoren"), shown_error(e))
            return 0
        self._clear_cache()
        self.status.setText(self.tr(
            "{neu} neue von {gelesen} Identifikatoren aus {name} übernommen.").format(
                neu=new, gelesen=read_in, name=path.name))
        self.refresh(True)
        return new
