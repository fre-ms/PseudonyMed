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

from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import vokabular as vokmodul
from pseudonymed.gui.listenseite import ListenModell, ListenSeite, Zellindex


class VokabularModell(ListenModell):
    """Rows of the vocabulary table: term, category and creation time, the first two
    editable; an edit goes through the page to the database."""

    def __init__(self, seite: VokabularSeite) -> None:
        super().__init__(seite)
        self.seite = seite
        self.zeilen: list[list[str]] = []
        self.titel = [self.tr("Begriff"), self.tr("Kategorie"), self.tr("Angelegt")]

    def lade(self, zeilen: list[tuple[str, str, str]]) -> None:
        """Replace all rows."""
        self.beginResetModel()
        self.zeilen = [list(z) for z in zeilen]
        self.endResetModel()

    def rowCount(self, parent: Zellindex | None = None) -> int:  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self.zeilen)

    def _zelle(self, zeile: int, spalte: int) -> str:
        """The cell text; the creation time cut to the minute, without the "T"."""
        wert = self.zeilen[zeile][spalte]
        return wert[:16].replace("T", " ") if spalte == 2 else wert

    def _uebernehme(self, zeile: int, spalte: int, wert: str) -> bool:
        """Hand the edit to the page and, when it took, keep the text with its whitespace
        collapsed, as the table stores it."""
        if not self.seite.aendere(self.zeilen[zeile][0], spalte, wert):
            return False
        self.zeilen[zeile][spalte] = " ".join(wert.split())
        index = self.index(zeile, spalte)
        self.dataChanged.emit(index, index)
        return True


class VokabularSeite(ListenSeite):
    """Step "3 Fachvokabular": the protected terms with category filter, cell editing,
    add, delete, CSV exchange and the reload of the bundled lists."""

    FILTER_SPALTE = 0
    SCHMALE_SPALTEN = (1,)
    modell: VokabularModell

    # -- regions of the page -----------------------------------------------------------------------
    def _kopftext(self) -> str:
        """What the step protects and how the table is used."""
        return self.tr(
            "Begriffe, die NER und die unscharfe Suche nie schwärzen dürfen: Fachbegriffe,"
            " Körperteile, Befundwörter, Eponyme und Ihre eigenen. Spaltenkopf anklicken"
            " sortiert; Doppelklick ändert Begriff oder Kategorie.")

    def _baue_filter(self, aussen: QVBoxLayout) -> None:
        """Search field, category filter and the row count."""
        filter_ = QHBoxLayout()
        self.suche = QLineEdit()
        self.suche.setPlaceholderText(self.tr("Suchen …"))
        self.suche.textChanged.connect(self._filtern)
        self.kategorie_filter = QComboBox()
        self.kategorie_filter.currentIndexChanged.connect(lambda _i: self.aktualisiere(True))
        self.anzahl = QLabel("")
        filter_.addWidget(self.suche, 2)
        filter_.addWidget(QLabel(self.tr("Kategorie")))
        filter_.addWidget(self.kategorie_filter, 1)
        filter_.addWidget(self.anzahl)
        aussen.addLayout(filter_)

    def _neues_modell(self) -> VokabularModell:
        """The model over the vocabulary rows."""
        return VokabularModell(self)

    def _baue_neu(self, aussen: QVBoxLayout) -> None:
        """Field for a new term, its category and the add button."""
        neu = QHBoxLayout()
        self.neuer_begriff = QLineEdit()
        self.neuer_begriff.setPlaceholderText(self.tr("Neuer Begriff (auch mehrere Wörter)"))
        self.neuer_begriff.returnPressed.connect(self.hinzufuegen)
        self.neue_kategorie = QComboBox()
        self.neue_kategorie.setEditable(True)
        knopf_neu = QPushButton(self.tr("Hinzufügen"))
        knopf_neu.clicked.connect(self.hinzufuegen)
        neu.addWidget(self.neuer_begriff, 2)
        neu.addWidget(self.neue_kategorie, 1)
        neu.addWidget(knopf_neu)
        aussen.addLayout(neu)

    def _import_hinweis(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        return self.tr(
            "CSV (begriff,kategorie; Kopfzeile optional; Komma, Semikolon oder Tabulator) oder"
            " eine Liste mit einem Begriff je Zeile, etwa die beim Export geschriebene"
            " .vokabular.txt")

    def _baue_eigenen_knopf(self) -> QPushButton:
        """The button that loads the bundled lists again."""
        grund = QPushButton(self.tr("Mitgelieferte Listen erneut einspielen"))
        grund.setToolTip(self.tr("Fehlende Begriffe der mitgelieferten Listen wieder aufnehmen"))
        grund.clicked.connect(self.grundbestand_einspielen)
        return grund

    # -- data ----------------------------------------------------------------------------------
    def _vokabular(self) -> vokmodul.Vokabular:
        """A fresh handle on the vocabulary table, opened per operation."""
        return vokmodul.Vokabular()

    def _cache_leeren(self) -> None:
        """Forget the cached matcher so the next analysis is built from the edited rows.
        ``standard`` attaches ``cache_clear`` at run time, unseen by the type checker."""
        fachmodul.standard.cache_clear()

    def aktualisiere(self, erzwingen: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        with self._vokabular() as v:
            version = v.version()
            if not erzwingen and version == self._version:
                return self.modell.rowCount()
            kategorie = self.kategorie_filter.currentData() or ""
            zeilen = v.alle(kategorie=kategorie)
            kategorien = v.kategorien()
            gesamt = v.anzahl()
        self._version = version
        self.modell.lade(zeilen)
        self._kategorien_fuellen(kategorien)
        self.anzahl.setText(self.tr("{n} von {gesamt} Begriffen").format(
            n=len(zeilen), gesamt=gesamt))
        self._filtern()
        return len(zeilen)

    def _kategorien_fuellen(self, kategorien: list[tuple[str, int]]) -> None:
        """Refill the category filter (with counts) and the category box for new entries,
        keeping what was chosen in each; the user's category is always offered."""
        gewaehlt = self.kategorie_filter.currentData() or ""
        self.kategorie_filter.blockSignals(True)
        self.kategorie_filter.clear()
        self.kategorie_filter.addItem(self.tr("alle"), "")
        for k, n in kategorien:
            self.kategorie_filter.addItem(f"{k} ({n})", k)
        i = self.kategorie_filter.findData(gewaehlt)
        self.kategorie_filter.setCurrentIndex(max(0, i))
        self.kategorie_filter.blockSignals(False)
        aktuell = self.neue_kategorie.currentText() or vokmodul.KATEGORIE_NUTZER
        self.neue_kategorie.clear()
        self.neue_kategorie.addItems([k for k, _n in kategorien]
                                     or [vokmodul.KATEGORIE_NUTZER])
        if vokmodul.KATEGORIE_NUTZER not in [k for k, _n in kategorien]:
            self.neue_kategorie.addItem(vokmodul.KATEGORIE_NUTZER)
        self.neue_kategorie.setCurrentText(aktuell)

    def _filtern(self) -> None:
        """Apply the search text to the proxy and show "n of total", reading the total
        back from the label when a reload has written it."""
        self.proxy.setFilterFixedString(self.suche.text().strip())
        self.anzahl.setText(self.tr("{n} von {gesamt} Begriffen").format(
            n=self.proxy.rowCount(), gesamt=self.anzahl.text().split()[-2]
            if "von" in self.anzahl.text() else self.modell.rowCount()))

    def ausgewaehlte(self) -> list[str]:
        """The selected terms in table order."""
        return [self.modell.zeilen[r][0] for r in self._gewaehlte_zeilen()]

    # -- editing -----------------------------------------------------------------------------------
    def aendere(self, begriff: str, spalte: int, wert: str) -> bool:
        """Rename a term (column 0) or set its category (column 1); True when it took."""
        with self._vokabular() as v:
            if spalte == 0:
                ok = v.benenne_um(begriff, wert)
            else:
                ok = v.setze_kategorie([begriff], wert) > 0
        if ok:
            self._cache_leeren()
            self.status.setText(self.tr("Geändert: {begriff}").format(begriff=begriff))
        else:
            self.status.setText(self.tr(
                "Nicht geändert: {begriff} (zu kurz oder schon vorhanden)").format(begriff=wert))
        return ok

    def hinzufuegen(self, begriff: str | None = None, kategorie: str | None = None) -> int:
        """Add one term (from the arguments or the entry row) in the given category;
        returns 1 when it was new, 0 when known or too short."""
        begriff = (begriff if begriff is not None else self.neuer_begriff.text()).strip()
        kategorie = (kategorie if kategorie is not None
                     else self.neue_kategorie.currentText()).strip()
        if len(begriff) < vokmodul.MIN_LAENGE:
            self.status.setText(self.tr("Begriffe haben mindestens drei Zeichen."))
            return 0
        with self._vokabular() as v:
            neu = v.fuege_hinzu([begriff], kategorie or vokmodul.KATEGORIE_NUTZER)
        self._cache_leeren()
        self.status.setText(self.tr("Hinzugefügt: {begriff}").format(begriff=begriff) if neu
                            else self.tr("Schon vorhanden: {begriff}").format(begriff=begriff))
        self.neuer_begriff.clear()
        self.aktualisiere(True)
        return neu

    def loeschen(self, begriffe: list[str] | None = None) -> int:
        """Delete the given (or selected) terms after a confirmation; returns how many
        went."""
        begriffe = begriffe if begriffe is not None else self.ausgewaehlte()
        if not begriffe:
            self.status.setText(self.tr("Nichts ausgewählt."))
            return 0
        if not self.synchron and QMessageBox.question(
                self, self.tr("Löschen"),
                self.tr("{n} Begriff(e) aus dem Vokabular löschen?").format(n=len(begriffe))) \
                != QMessageBox.StandardButton.Yes:
            return 0
        with self._vokabular() as v:
            n = v.entferne(begriffe)
        self._cache_leeren()
        self.status.setText(self.tr("{n} Begriff(e) gelöscht.").format(n=n))
        self.aktualisiere(True)
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def exportiere_waehlen(self) -> None:
        """Ask for a target and write the vocabulary as CSV."""
        pfad, _ = QFileDialog.getSaveFileName(self, self.tr("Vokabular als CSV"),
                                              "vokabular.csv", self.tr("CSV (*.csv)"))
        if pfad:
            self.exportiere_csv(Path(pfad))

    def exportiere_csv(self, pfad: Path) -> int:
        """Write the rows of the current category filter to ``pfad``; returns the count."""
        kategorie = self.kategorie_filter.currentData() or ""
        with self._vokabular() as v:
            n = v.exportiere_csv(pfad, kategorie)
        self.status.setText(self.tr("{n} Begriffe nach {pfad} geschrieben.").format(
            n=n, pfad=pfad))
        return n

    def importiere_waehlen(self) -> None:
        """Ask for a CSV or list file and take its terms into the table."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Vokabular importieren"), "",
            self.tr("CSV und Listen (*.csv *.txt);;Alle Dateien (*)"))
        if pfad:
            self.importiere_datei(Path(pfad))

    def importiere_datei(self, pfad: Path, kategorie: str | None = None) -> int:
        """Take the terms of a CSV or list file into the table, rows without a category
        into ``kategorie`` (default: the category box); returns how many were new."""
        kategorie = (kategorie if kategorie is not None
                     else self.neue_kategorie.currentText()).strip() or vokmodul.KATEGORIE_NUTZER
        try:
            with self._vokabular() as v:
                neu, gelesen = v.importiere_csv(pfad, kategorie)
        except OSError as e:
            self._melde(self.tr("Vokabular"), str(e))
            return 0
        self._cache_leeren()
        self.status.setText(self.tr("{neu} neue von {gelesen} Begriffen aus {name} übernommen.")
                            .format(neu=neu, gelesen=gelesen, name=pfad.name))
        self.aktualisiere(True)
        return neu

    def grundbestand_einspielen(self) -> int:
        """Load the bundled lists again, adding only what is missing; returns how many
        terms came back."""
        with self._vokabular() as v:
            zaehlung = v.grundbestand()
        self._cache_leeren()
        n = sum(zaehlung.values())
        self.status.setText(self.tr(
            "{n} fehlende Begriffe der mitgelieferten Listen aufgenommen.").format(n=n))
        self.aktualisiere(True)
        return n
