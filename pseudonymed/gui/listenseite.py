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
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.gui.sitzung import Sitzung

Zellindex = QModelIndex | QPersistentModelIndex


class ListenModell(QAbstractTableModel):
    """Table model base: the column titles in ``titel``, the first two columns editable,
    the Qt plumbing of ``data`` and ``setData``.  A page's model keeps the rows and
    answers :meth:`_zelle` (the text of a cell) and :meth:`_uebernehme` (an edit)."""

    titel: list[str]

    def columnCount(self, parent: Zellindex | None = None) -> int:  # noqa: N802
        return len(self.titel)

    def data(self, index: Zellindex, role: int = Qt.ItemDataRole.DisplayRole) -> str | None:
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._zelle(index.row(), index.column())
        return None

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> str | None:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.titel[section]
        return None

    def flags(self, index: Zellindex) -> Qt.ItemFlag:
        basis = super().flags(index)
        if index.column() < 2:
            return basis | Qt.ItemFlag.ItemIsEditable
        return basis

    def setData(self, index: Zellindex, wert: Any,  # noqa: N802
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        return self._uebernehme(index.row(), index.column(), str(wert))

    def _zelle(self, zeile: int, spalte: int) -> str:
        """The text shown and edited in row ``zeile``, column ``spalte``."""
        raise NotImplementedError

    def _uebernehme(self, zeile: int, spalte: int, wert: str) -> bool:
        """Write an edited cell through to the database; True when the row changed."""
        raise NotImplementedError


class ListenSeite(QWidget):
    """Page base: header text, filter row, table, row for a new entry and the button row
    with the status line, built in that order because it is the tab order.  A page
    supplies its model, its texts and the two rows that name its things, and keeps every
    method that talks to its database."""

    # column the search field filters; -1 searches every column
    FILTER_SPALTE = -1
    # columns sized to their contents; the first column takes the remaining width
    SCHMALE_SPALTEN: tuple[int, ...] = ()
    modell: ListenModell

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self._version = -1
        aussen = QVBoxLayout(self)
        self._baue_kopf(aussen)
        self._baue_filter(aussen)
        self._baue_tabelle(aussen)
        self._baue_neu(aussen)
        self._baue_knoepfe(aussen)

    # -- regions shared by the pages ---------------------------------------------------------------
    def _baue_kopf(self, aussen: QVBoxLayout) -> None:
        """The explanatory text above the table, wrapped."""
        kopf = QLabel(self._kopftext())
        kopf.setWordWrap(True)
        aussen.addWidget(kopf)

    def _baue_tabelle(self, aussen: QVBoxLayout) -> None:
        """Model, case-insensitive sort and filter proxy, and the view: sorted by the first
        column, whole rows selected, several at once."""
        self.modell = self._neues_modell()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.modell)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(self.FILTER_SPALTE)
        self.proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.ansicht = QTableView()
        self.ansicht.setModel(self.proxy)
        self.ansicht.setSortingEnabled(True)
        self.ansicht.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.ansicht.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ansicht.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        kopfzeile = self.ansicht.horizontalHeader()
        kopfzeile.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for spalte in self.SCHMALE_SPALTEN:
            kopfzeile.setSectionResizeMode(spalte, QHeaderView.ResizeMode.ResizeToContents)
        self.ansicht.verticalHeader().setVisible(False)
        aussen.addWidget(self.ansicht, 1)

    def _baue_knoepfe(self, aussen: QVBoxLayout) -> None:
        """Delete, CSV export, CSV import, the page's own button, refresh, and the status
        line that takes the remaining width."""
        unten = QHBoxLayout()
        loeschen = QPushButton(self.tr("Ausgewählte löschen"))
        loeschen.clicked.connect(self.loeschen)
        export = QPushButton(self.tr("CSV exportieren …"))
        export.clicked.connect(self.exportiere_waehlen)
        import_ = QPushButton(self.tr("CSV oder Liste importieren …"))
        import_.setToolTip(self._import_hinweis())
        import_.clicked.connect(self.importiere_waehlen)
        eigener = self._baue_eigenen_knopf()
        neu_laden = QPushButton(self.tr("Aktualisieren"))
        neu_laden.clicked.connect(lambda: self.aktualisiere(True))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        for w in (loeschen, export, import_, eigener, neu_laden):
            unten.addWidget(w)
        unten.addWidget(self.status, 1)
        aussen.addLayout(unten)

    # -- shared behaviour --------------------------------------------------------------------------
    def _gewaehlte_zeilen(self) -> list[int]:
        """Rows of the model behind the selected view rows, in model order and without
        repeats, however the view is sorted or filtered."""
        gewaehlt = self.ansicht.selectionModel().selectedRows()
        return sorted({self.proxy.mapToSource(i).row() for i in gewaehlt})

    def _melde(self, titel: str, text: str) -> None:
        """Show an information box; in synchronous (test) mode raise instead, because a
        modal dialog never returns offscreen."""
        if self.synchron:
            raise RuntimeError(text)
        QMessageBox.information(self, titel, text)

    # -- what a page supplies ----------------------------------------------------------------------
    def _kopftext(self) -> str:
        """The explanatory text above the table."""
        raise NotImplementedError

    def _baue_filter(self, aussen: QVBoxLayout) -> None:
        """Search field, the page's filter box and the row count."""
        raise NotImplementedError

    def _neues_modell(self) -> ListenModell:
        """The page's table model."""
        raise NotImplementedError

    def _baue_neu(self, aussen: QVBoxLayout) -> None:
        """Field and box for a new entry with its add button."""
        raise NotImplementedError

    def _import_hinweis(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        raise NotImplementedError

    def _baue_eigenen_knopf(self) -> QPushButton:
        """The page's own button between import and refresh, already wired."""
        raise NotImplementedError

    def aktualisiere(self, erzwingen: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        raise NotImplementedError

    def loeschen(self) -> int:
        """Delete the selected rows after a confirmation; returns how many went."""
        raise NotImplementedError

    def exportiere_waehlen(self) -> None:
        """Ask for a target and write the table as CSV."""
        raise NotImplementedError

    def importiere_waehlen(self) -> None:
        """Ask for a file and take its rows into the table."""
        raise NotImplementedError
