"""Shared base of the list steps "2 Identifikatoren" and "3 Fachvokabular".

Both steps show one database table as a sortable, filterable, editable grid: a header
text, search field and filter box, the table, a row for a new entry, and a button row
(delete, CSV out, CSV in, one button of the page's own, refresh) beside a status line.
The base builds the regions that are the same and asks the page for everything that
names its things: the texts, the filter row, the row for new entries and the model."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.gui.session import Session

CellIndex = QModelIndex | QPersistentModelIndex


class ListModel(QAbstractTableModel):
    """Table model base: the column titles in ``title``, the first two columns editable,
    the Qt plumbing of ``data`` and ``setData``.  A page's model keeps the rows and
    answers :meth:`_cell` (the text of a cell) and :meth:`_apply_edit` (an edit)."""

    title: list[str]

    def columnCount(self, parent: CellIndex | None = None) -> int:  # noqa: N802
        return len(self.title)

    def data(self, index: CellIndex, role: int = Qt.ItemDataRole.DisplayRole) -> str | None:
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._cell(index.row(), index.column())
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> str | None:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.title[section]
        return None

    def flags(self, index: CellIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if index.column() < 2:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: CellIndex, value: Any,  # noqa: N802
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        return self._apply_edit(index.row(), index.column(), str(value))

    def _cell(self, line: int, column: int) -> str:
        """The text shown and edited in row ``line``, column ``column``."""
        raise NotImplementedError

    def _apply_edit(self, line: int, column: int, value: str) -> bool:
        """Write an edited cell through to the database; True when the row changed."""
        raise NotImplementedError


class ListPage(QWidget):
    """Page base: header text, filter row, table, row for a new entry and the button row
    with the status line, built in that order because it is the tab order.  A page
    supplies its model, its texts and the two rows that name its things, and keeps every
    method that talks to its database."""

    # column the search field filters; -1 searches every column
    FILTER_COLUMN = -1
    # columns sized to their contents; the first column takes the remaining width
    NARROW_COLUMNS: tuple[int, ...] = ()
    model: ListModel

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self._version = -1
        outer = QVBoxLayout(self)
        self._build_header(outer)
        self._build_filter(outer)
        self._build_table(outer)
        self._build_new(outer)
        self._build_buttons(outer)

    # -- regions shared by the pages ---------------------------------------------------------------
    def _build_header(self, outer: QVBoxLayout) -> None:
        """The explanatory text above the table, wrapped."""
        header = QLabel(self._header_text())
        header.setWordWrap(True)
        outer.addWidget(header)

    def _build_table(self, outer: QVBoxLayout) -> None:
        """Model, case-insensitive sort and filter proxy, and the view: sorted by the first
        column, whole rows selected, several at once."""
        self.model = self._new_model()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(self.FILTER_COLUMN)
        self.proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header_line = self.view.horizontalHeader()
        header_line.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in self.NARROW_COLUMNS:
            header_line.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.view.verticalHeader().setVisible(False)
        outer.addWidget(self.view, 1)

    def _build_buttons(self, outer: QVBoxLayout) -> None:
        """Delete, CSV export, CSV import, the page's own button, refresh, and the status
        line that takes the remaining width."""
        bottom = QHBoxLayout()
        delete = QPushButton(self.tr("Ausgewählte löschen"))
        delete.clicked.connect(self.delete)
        export = QPushButton(self.tr("CSV exportieren …"))
        export.clicked.connect(self.choose_and_export)
        import_ = QPushButton(self.tr("CSV oder Liste importieren …"))
        import_.setToolTip(self._import_note())
        import_.clicked.connect(self.choose_and_import)
        own = self._build_own_button()
        reload_button = QPushButton(self.tr("Aktualisieren"))
        reload_button.clicked.connect(lambda: self.refresh(True))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        for w in (delete, export, import_, own, reload_button):
            bottom.addWidget(w)
        bottom.addWidget(self.status, 1)
        outer.addLayout(bottom)

    # -- shared behaviour --------------------------------------------------------------------------
    def _selected_rows(self) -> list[int]:
        """Rows of the model behind the selected view rows, in model order and without
        repeats, however the view is sorted or filtered."""
        selected = self.view.selectionModel().selectedRows()
        return sorted({self.proxy.mapToSource(i).row() for i in selected})

    def _notify(self, title: str, text: str) -> None:
        """Show an information box; in synchronous (test) mode raise instead, because a
        modal dialog never returns offscreen."""
        if self.synchronous:
            raise RuntimeError(text)
        QMessageBox.information(self, title, text)

    # -- what a page supplies ----------------------------------------------------------------------
    def _refill_filter(self, combo: QComboBox, categories: list[tuple[str, int]]) -> None:
        """Refill a category filter: "alle" first, then each category with its count, keeping
        the current choice and without signalling the refill as a change."""
        selected = combo.currentData() or ""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.tr("alle"), "")
        for k, n in categories:
            combo.addItem(f"{k} ({n})", k)
        combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.blockSignals(False)

    def _header_text(self) -> str:
        """The explanatory text above the table."""
        raise NotImplementedError

    def _build_filter(self, outer: QVBoxLayout) -> None:
        """Search field, the page's filter box and the row count."""
        raise NotImplementedError

    def _new_model(self) -> ListModel:
        """The page's table model."""
        raise NotImplementedError

    def _build_new(self, outer: QVBoxLayout) -> None:
        """Field and box for a new entry with its add button."""
        raise NotImplementedError

    def _import_note(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        raise NotImplementedError

    def _build_own_button(self) -> QPushButton:
        """The page's own button between import and refresh, already wired."""
        raise NotImplementedError

    def refresh(self, force: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        raise NotImplementedError

    def delete(self) -> int:
        """Delete the selected rows after a confirmation; returns how many went."""
        raise NotImplementedError

    def choose_and_export(self) -> None:
        """Ask for a target and write the table as CSV."""
        raise NotImplementedError

    def choose_and_import(self) -> None:
        """Ask for a file and take its rows into the table."""
        raise NotImplementedError
