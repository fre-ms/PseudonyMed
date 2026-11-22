"""Step 2: the protected vocabulary as a sortable, editable table.

Everything NER and the fuzzy search must leave alone, in one table of the pseudonym
database: term and category.  Search, sort by any column, edit a cell, add and delete
terms, exchange CSV; the bundled lists can be loaded again."""

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

from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.gui.list_page import CellIndex, ListModel, ListPage


class VocabularyModel(ListModel):
    """Rows of the vocabulary table: term, category and creation time, the first two
    editable; an edit goes through the page to the database."""

    def __init__(self, page: VocabularyPage) -> None:
        super().__init__(page)
        self.page = page
        self.lines: list[list[str]] = []
        self.title = [self.tr("Begriff"), self.tr("Kategorie"), self.tr("Angelegt")]

    def load(self, lines: list[tuple[str, str, str]]) -> None:
        """Replace all rows."""
        self.beginResetModel()
        self.lines = [list(z) for z in lines]
        self.endResetModel()

    def rowCount(self, parent: CellIndex | None = None) -> int:  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self.lines)

    def _cell(self, line: int, column: int) -> str:
        """The cell text; the creation time cut to the minute, without the "T"."""
        value = self.lines[line][column]
        return value[:16].replace("T", " ") if column == 2 else value

    def _apply_edit(self, line: int, column: int, value: str) -> bool:
        """Hand the edit to the page and, when it took, keep the text with its whitespace
        collapsed, as the table stores it."""
        if not self.page.change(self.lines[line][0], column, value):
            return False
        self.lines[line][column] = " ".join(value.split())
        index = self.index(line, column)
        self.dataChanged.emit(index, index)
        return True


class VocabularyPage(ListPage):
    """Step "3 Fachvokabular": the protected terms with category filter, cell editing,
    add, delete, CSV exchange and the reload of the bundled lists."""

    FILTER_COLUMN = 0
    NARROW_COLUMNS = (1,)
    model: VocabularyModel

    # -- regions of the page -----------------------------------------------------------------------
    def _header_text(self) -> str:
        """What the step protects and how the table is used."""
        return self.tr(
            "Begriffe, die NER und die unscharfe Suche nie schwärzen dürfen: Fachbegriffe,"
            " Körperteile, Befundwörter, Eponyme und Ihre eigenen. Spaltenkopf anklicken"
            " sortiert; Doppelklick ändert Begriff oder Kategorie.")

    def _build_filter(self, outer: QVBoxLayout) -> None:
        """Search field, category filter and the row count."""
        filter_ = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Suchen …"))
        self.search.textChanged.connect(self._apply_filter)
        self.category_filter = QComboBox()
        self.category_filter.currentIndexChanged.connect(lambda _i: self.refresh(True))
        self.count = QLabel("")
        filter_.addWidget(self.search, 2)
        filter_.addWidget(QLabel(self.tr("Kategorie")))
        filter_.addWidget(self.category_filter, 1)
        filter_.addWidget(self.count)
        outer.addLayout(filter_)

    def _new_model(self) -> VocabularyModel:
        """The model over the vocabulary rows."""
        return VocabularyModel(self)

    def _build_new(self, outer: QVBoxLayout) -> None:
        """Field for a new term, its category and the add button."""
        new = QHBoxLayout()
        self.new_term = QLineEdit()
        self.new_term.setPlaceholderText(self.tr("Neuer Begriff (auch mehrere Wörter)"))
        self.new_term.returnPressed.connect(self.add)
        self.new_category = QComboBox()
        self.new_category.setEditable(True)
        new_button = QPushButton(self.tr("Hinzufügen"))
        new_button.clicked.connect(self.add)
        new.addWidget(self.new_term, 2)
        new.addWidget(self.new_category, 1)
        new.addWidget(new_button)
        outer.addLayout(new)

    def _import_note(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        return self.tr(
            "CSV (begriff,kategorie; Kopfzeile optional; Komma, Semikolon oder Tabulator) oder"
            " eine Liste mit einem Begriff je Zeile, etwa die beim Export geschriebene"
            " .vokabular.txt")

    def _build_own_button(self) -> QPushButton:
        """The button that loads the bundled lists again."""
        base = QPushButton(self.tr("Mitgelieferte Listen erneut einspielen"))
        base.setToolTip(self.tr("Fehlende Begriffe der mitgelieferten Listen wieder aufnehmen"))
        base.clicked.connect(self.restore_base_set)
        return base

    # -- data ----------------------------------------------------------------------------------
    def _vocabulary(self) -> vocab_mod.Vocabulary:
        """A fresh handle on the vocabulary table, opened per operation."""
        return vocab_mod.Vocabulary()

    def _clear_cache(self) -> None:
        """Forget the cached matcher so the next analysis is built from the edited rows.
        ``standard`` attaches ``cache_clear`` at run time, unseen by the type checker."""
        terms_module.standard.cache_clear()

    def refresh(self, force: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        with self._vocabulary() as v:
            version = v.version()
            if not force and version == self._version:
                return self.model.rowCount()
            category = self.category_filter.currentData() or ""
            lines = v.all_items(category=category)
            categories = v.categories()
            total = v.count()
        self._version = version
        self.model.load(lines)
        self._fill_categories(categories)
        self.count.setText(self.tr("{n} von {gesamt} Begriffen").format(
            n=len(lines), gesamt=total))
        self._apply_filter()
        return len(lines)

    def _fill_categories(self, categories: list[tuple[str, int]]) -> None:
        """Refill the category filter (with counts) and the category box for new entries,
        keeping what was chosen in each; the user's category is always offered."""
        self._refill_filter(self.category_filter, categories)
        current = self.new_category.currentText() or vocab_mod.CATEGORY_USER
        self.new_category.clear()
        self.new_category.addItems([k for k, _n in categories]
                                   or [vocab_mod.CATEGORY_USER])
        if vocab_mod.CATEGORY_USER not in [k for k, _n in categories]:
            self.new_category.addItem(vocab_mod.CATEGORY_USER)
        self.new_category.setCurrentText(current)

    def _apply_filter(self) -> None:
        """Apply the search text to the proxy and show "n of total", reading the total
        back from the label when a reload has written it."""
        self.proxy.setFilterFixedString(self.search.text().strip())
        self.count.setText(self.tr("{n} von {gesamt} Begriffen").format(
            n=self.proxy.rowCount(), gesamt=self.count.text().split()[-2]
            if "von" in self.count.text() else self.model.rowCount()))

    def selected_entries(self) -> list[str]:
        """The selected terms in table order."""
        return [self.model.lines[r][0] for r in self._selected_rows()]

    # -- editing -----------------------------------------------------------------------------------
    def change(self, term: str, column: int, value: str) -> bool:
        """Rename a term (column 0) or set its category (column 1); True when it took."""
        with self._vocabulary() as v:
            if column == 0:
                ok = v.rename(term, value)
            else:
                ok = v.set_category([term], value) > 0
        if ok:
            self._clear_cache()
            self.status.setText(self.tr("Geändert: {begriff}").format(begriff=term))
        else:
            self.status.setText(self.tr(
                "Nicht geändert: {begriff} (zu kurz oder schon vorhanden)").format(begriff=value))
        return ok

    def add(self, term: str | None = None, category: str | None = None) -> int:
        """Add one term (from the arguments or the entry row) in the given category;
        returns 1 when it was new, 0 when known or too short."""
        term = (term if term is not None else self.new_term.text()).strip()
        category = (category if category is not None
                    else self.new_category.currentText()).strip()
        if len(term) < vocab_mod.MIN_LENGTH:
            self.status.setText(self.tr("Begriffe haben mindestens drei Zeichen."))
            return 0
        with self._vocabulary() as v:
            new = v.add([term], category or vocab_mod.CATEGORY_USER)
        self._clear_cache()
        self.status.setText(self.tr("Hinzugefügt: {begriff}").format(begriff=term) if new
                            else self.tr("Schon vorhanden: {begriff}").format(begriff=term))
        self.new_term.clear()
        self.refresh(True)
        return new

    def delete(self, terms: list[str] | None = None) -> int:
        """Delete the given (or selected) terms after a confirmation; returns how many
        went."""
        terms = terms if terms is not None else self.selected_entries()
        if not terms:
            self.status.setText(self.tr("Nichts ausgewählt."))
            return 0
        if not self.synchronous and QMessageBox.question(
                self, self.tr("Löschen"),
                self.tr("{n} Begriff(e) aus dem Vokabular löschen?").format(n=len(terms))) \
                != QMessageBox.StandardButton.Yes:
            return 0
        with self._vocabulary() as v:
            n = v.remove(terms)
        self._clear_cache()
        self.status.setText(self.tr("{n} Begriff(e) gelöscht.").format(n=n))
        self.refresh(True)
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def choose_and_export(self) -> None:
        """Ask for a target and write the vocabulary as CSV."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Vokabular als CSV"),
                                              "vokabular.csv", self.tr("CSV (*.csv)"))
        if path:
            self.export_csv(Path(path))

    def export_csv(self, path: Path) -> int:
        """Write the rows of the current category filter to ``path``; returns the count."""
        category = self.category_filter.currentData() or ""
        with self._vocabulary() as v:
            n = v.export_csv(path, category)
        self.status.setText(self.tr("{n} Begriffe nach {pfad} geschrieben.").format(
            n=n, pfad=path))
        return n

    def choose_and_import(self) -> None:
        """Ask for a CSV or list file and take its terms into the table."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Vokabular importieren"), "",
            self.tr("CSV und Listen (*.csv *.txt);;Alle Dateien (*)"))
        if path:
            self.import_file(Path(path))

    def import_file(self, path: Path, category: str | None = None) -> int:
        """Take the terms of a CSV or list file into the table, rows without a category
        into ``category`` (default: the category box); returns how many were new."""
        category = (category if category is not None
                    else self.new_category.currentText()).strip() or vocab_mod.CATEGORY_USER
        try:
            with self._vocabulary() as v:
                new, read_in = v.import_csv(path, category)
        except OSError as e:
            self._notify(self.tr("Vokabular"), str(e))
            return 0
        self._clear_cache()
        self.status.setText(self.tr("{neu} neue von {gelesen} Begriffen aus {name} übernommen.")
                            .format(neu=new, gelesen=read_in, name=path.name))
        self.refresh(True)
        return new

    def restore_base_set(self) -> int:
        """Load the bundled lists again, adding only what is missing; returns how many
        terms came back."""
        with self._vocabulary() as v:
            counts = v.seed()
        self._clear_cache()
        n = sum(counts.values())
        self.status.setText(self.tr(
            "{n} fehlende Begriffe der mitgelieferten Listen aufgenommen.").format(n=n))
        self.refresh(True)
        return n
