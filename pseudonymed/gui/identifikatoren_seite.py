"""Step 2: identifiers beyond the patient's data as a sortable, editable table.

Professions, hospitals, study acronyms, titles and the user's own identifiers from the
lexicon of the pseudonym database.  Like the patient's data they are redacted by default;
each class has a context rule (see :mod:`pseudonymed.erkennung.identifikatoren`), and the
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

from pseudonymed.erkennung import identifikatoren as identmodul
from pseudonymed.gui.listenseite import ListenModell, ListenSeite, Zellindex


class IdentifikatorenModell(ListenModell):
    """Rows of the identifier list: term, class, details and source, the first two
    editable; an edit goes through the page to the database."""

    def __init__(self, seite: IdentifikatorenSeite) -> None:
        super().__init__(seite)
        self.seite = seite
        self.eintraege: list[identmodul.Eintrag] = []
        self.titel = [self.tr("Begriff"), self.tr("Klasse"), self.tr("Angaben"),
                      self.tr("Quelle")]

    def lade(self, eintraege: list[identmodul.Eintrag]) -> None:
        """Replace all rows."""
        self.beginResetModel()
        self.eintraege = list(eintraege)
        self.endResetModel()

    def rowCount(self, parent: Zellindex | None = None) -> int:  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self.eintraege)

    def _zelle(self, zeile: int, spalte: int) -> str:
        """Term, class, details or source; the user's own rows show "eigene" as source."""
        e = self.eintraege[zeile]
        return (e.begriff, e.klasse, e.angaben,
                self.tr("eigene") if e.eigen else e.quelle)[spalte]

    def _uebernehme(self, zeile: int, spalte: int, wert: str) -> bool:
        """Hand the edit to the page and, when it took, show the row as the database now
        holds it (a changed bundled row becomes the user's own, so every column may move)."""
        neu = self.seite.aendere(self.eintraege[zeile], spalte, wert)
        if neu is None:
            return False
        self.eintraege[zeile] = neu
        self.dataChanged.emit(self.index(zeile, 0), self.index(zeile, 3))
        return True


class IdentifikatorenSeite(ListenSeite):
    """Step "2 Identifikatoren": the identifier list with class filter, cell editing,
    add, delete, CSV exchange and the return of deleted bundled rows."""

    FILTER_SPALTE = -1
    SCHMALE_SPALTEN = (1, 2, 3)
    modell: IdentifikatorenModell

    # -- regions of the page -----------------------------------------------------------------------
    def _kopftext(self) -> str:
        """What the step does and when each class counts as a hit."""
        return self.tr(
            "Identifikatoren außerhalb der Patientendaten: Kliniken, Berufe, Studien, Titel"
            " und Ihre eigenen (Arbeitgeber, Vereine, Einrichtungen). Wie die Patientendaten"
            " werden sie standardmäßig geschwärzt; in der Sichtprüfung lässt sich jeder Treffer"
            " verwerfen. Kliniken und eigene Einträge zählen überall, Berufe nur mit Hinweis"
            " wie „arbeitet als“ oder „von Beruf“, Studienkürzel nur neben „Studie“"
            " (Registernummern wie NCT immer), Titel nur vor oder nach einem erkannten Namen."
            " Der Erwerbsstatus (berentet, arbeitslos) bleibt nach der GeMTeX-Leitlinie stehen.")

    def _baue_filter(self, aussen: QVBoxLayout) -> None:
        """Search field, class filter and the row count."""
        filter_ = QHBoxLayout()
        self.suche = QLineEdit()
        self.suche.setPlaceholderText(self.tr("Suchen …"))
        self.suche.textChanged.connect(self._filtern)
        self.klassen_filter = QComboBox()
        self.klassen_filter.currentIndexChanged.connect(lambda _i: self.aktualisiere(True))
        self.anzahl = QLabel("")
        filter_.addWidget(self.suche, 2)
        filter_.addWidget(QLabel(self.tr("Klasse")))
        filter_.addWidget(self.klassen_filter, 1)
        filter_.addWidget(self.anzahl)
        aussen.addLayout(filter_)

    def _neues_modell(self) -> IdentifikatorenModell:
        """The model over the identifier rows."""
        return IdentifikatorenModell(self)

    def _baue_neu(self, aussen: QVBoxLayout) -> None:
        """Field for a new identifier, its class (the user's own by default) and the add
        button."""
        neu = QHBoxLayout()
        self.neuer_begriff = QLineEdit()
        self.neuer_begriff.setPlaceholderText(self.tr("Neuer Identifikator (auch mehrere Wörter)"))
        self.neuer_begriff.returnPressed.connect(self.hinzufuegen)
        self.neue_klasse = QComboBox()
        self.neue_klasse.setEditable(True)
        self.neue_klasse.addItems(list(identmodul.KLASSEN))
        self.neue_klasse.setCurrentText(identmodul.KLASSE_NUTZER)
        knopf_neu = QPushButton(self.tr("Hinzufügen"))
        knopf_neu.clicked.connect(self.hinzufuegen)
        neu.addWidget(self.neuer_begriff, 2)
        neu.addWidget(self.neue_klasse, 1)
        neu.addWidget(knopf_neu)
        aussen.addLayout(neu)

    def _import_hinweis(self) -> str:
        """Tooltip of the import button: the accepted file shapes."""
        return self.tr(
            "CSV (begriff,klasse; Kopfzeile optional; Komma, Semikolon oder Tabulator) oder"
            " eine Liste mit einem Identifikator je Zeile")

    def _baue_eigenen_knopf(self) -> QPushButton:
        """The button that brings deleted bundled rows back; enabled while any exist."""
        self.wiederherstellen_knopf = QPushButton(self.tr("Gelöschte wiederherstellen"))
        self.wiederherstellen_knopf.setToolTip(self.tr(
            "Gelöschte mitgelieferte Einträge zurückholen; selbst geladene Zusatzdaten"
            " (Kliniken Österreich) bitte in „11 Datenbank“ neu laden"))
        self.wiederherstellen_knopf.clicked.connect(self.wiederherstellen)
        return self.wiederherstellen_knopf

    # -- data ----------------------------------------------------------------------------------
    def _identifikatoren(self) -> identmodul.Identifikatoren:
        """A fresh handle on the identifier table, opened per operation."""
        return identmodul.Identifikatoren()

    def _cache_leeren(self) -> None:
        """Forget the cached matcher so the next analysis is built from the edited rows.
        ``standard`` attaches ``cache_clear`` at run time, unseen by the type checker."""
        identmodul.standard.cache_clear()

    def aktualisiere(self, erzwingen: bool = False) -> int:
        """Reload rows when the table changed (or when asked); returns the row count."""
        with self._identifikatoren() as ident:
            version = ident.version()
            if not erzwingen and version == self._version:
                return self.modell.rowCount()
            klasse = self.klassen_filter.currentData() or ""
            eintraege = ident.alle(klasse)
            klassen = ident.klassen()
            self._gesamt = ident.anzahl()
            geloescht = ident.geloescht()
        self._version = version
        self.modell.lade(eintraege)
        self._klassen_fuellen(klassen)
        self.wiederherstellen_knopf.setEnabled(geloescht > 0)
        self._filtern()
        return len(eintraege)

    def _klassen_fuellen(self, klassen: list[tuple[str, int]]) -> None:
        """Refill the class filter (with counts) and the class box for new entries,
        keeping what was chosen in each."""
        gewaehlt = self.klassen_filter.currentData() or ""
        self.klassen_filter.blockSignals(True)
        self.klassen_filter.clear()
        self.klassen_filter.addItem(self.tr("alle"), "")
        for k, n in klassen:
            self.klassen_filter.addItem(f"{k} ({n})", k)
        i = self.klassen_filter.findData(gewaehlt)
        self.klassen_filter.setCurrentIndex(max(0, i))
        self.klassen_filter.blockSignals(False)
        aktuell = self.neue_klasse.currentText() or identmodul.KLASSE_NUTZER
        self.neue_klasse.clear()
        self.neue_klasse.addItems(list(dict.fromkeys(
            list(identmodul.KLASSEN) + [k for k, _n in klassen])))
        self.neue_klasse.setCurrentText(aktuell)

    def _filtern(self) -> None:
        """Apply the search text to the proxy and show "n of total"."""
        self.proxy.setFilterFixedString(self.suche.text().strip())
        self.anzahl.setText(self.tr("{n} von {gesamt} Einträgen").format(
            n=self.proxy.rowCount(), gesamt=getattr(self, "_gesamt", self.modell.rowCount())))

    def ausgewaehlte(self) -> list[identmodul.Eintrag]:
        """The selected rows in table order."""
        return [self.modell.eintraege[r] for r in self._gewaehlte_zeilen()]

    def _geaendert(self) -> None:
        """After an in-place edit: drop the matcher cache and take over the table's new
        version and count without reloading the rows."""
        self._cache_leeren()
        with self._identifikatoren() as ident:
            self._version = ident.version()
            self._gesamt = ident.anzahl()

    # -- editing -----------------------------------------------------------------------------------
    def aendere(self, eintrag: identmodul.Eintrag, spalte: int,
                wert: str) -> identmodul.Eintrag | None:
        """Change term (column 0) or class (column 1); returns the row as it is now."""
        with self._identifikatoren() as ident:
            neu = ident.aendere(eintrag.schluessel, begriff=wert if spalte == 0 else None,
                                klasse=wert if spalte == 1 else None)
        if neu is not None:
            self._geaendert()
            self.status.setText(self.tr("Geändert: {begriff}").format(begriff=neu.begriff))
        else:
            self.status.setText(self.tr(
                "Nicht geändert: {begriff} (zu kurz oder schon vorhanden)").format(begriff=wert))
        return neu

    def hinzufuegen(self, begriff: str | None = None, klasse: str | None = None) -> int:
        """Add one identifier (from the arguments or the entry row) in the given class;
        returns 1 when it was new, 0 when known or too short."""
        begriff = (begriff if begriff is not None else self.neuer_begriff.text()).strip()
        klasse = (klasse if klasse is not None else self.neue_klasse.currentText()).strip()
        if len(begriff) < identmodul.MIN_LAENGE:
            self.status.setText(self.tr("Identifikatoren haben mindestens drei Zeichen."))
            return 0
        with self._identifikatoren() as ident:
            neu = ident.fuege_hinzu([begriff], klasse or identmodul.KLASSE_NUTZER)
        self._cache_leeren()
        self.status.setText(self.tr("Hinzugefügt: {begriff}").format(begriff=begriff) if neu
                            else self.tr("Schon vorhanden: {begriff}").format(begriff=begriff))
        self.neuer_begriff.clear()
        self.aktualisiere(True)
        return neu

    def loeschen(self, eintraege: list[identmodul.Eintrag] | None = None) -> int:
        """Delete the given (or selected) rows after a confirmation; bundled rows stay
        deleted across updates until restored.  Returns how many went."""
        eintraege = eintraege if eintraege is not None else self.ausgewaehlte()
        if not eintraege:
            self.status.setText(self.tr("Nichts ausgewählt."))
            return 0
        if not self.synchron and QMessageBox.question(
                self, self.tr("Löschen"),
                self.tr("{n} Identifikator(en) löschen? Mitgelieferte Einträge bleiben auch nach"
                        " Updates gelöscht, bis Sie sie wiederherstellen.").format(
                            n=len(eintraege))) != QMessageBox.StandardButton.Yes:
            return 0
        with self._identifikatoren() as ident:
            n = ident.entferne(e.schluessel for e in eintraege)
        self._cache_leeren()
        self.status.setText(self.tr("{n} Identifikator(en) gelöscht.").format(n=n))
        self.aktualisiere(True)
        return n

    def wiederherstellen(self) -> int:
        """Bring every deleted bundled row back; returns how many."""
        with self._identifikatoren() as ident:
            n = ident.wiederherstellen()
        self._cache_leeren()
        self.status.setText(self.tr("{n} gelöschte Einträge wiederhergestellt.").format(n=n))
        self.aktualisiere(True)
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def exportiere_waehlen(self) -> None:
        """Ask for a target and write the identifiers as CSV."""
        pfad, _ = QFileDialog.getSaveFileName(self, self.tr("Identifikatoren als CSV"),
                                              "identifikatoren.csv", self.tr("CSV (*.csv)"))
        if pfad:
            self.exportiere_csv(Path(pfad))

    def exportiere_csv(self, pfad: Path) -> int:
        """Write the rows of the current class filter to ``pfad``; returns the count."""
        klasse = self.klassen_filter.currentData() or ""
        with self._identifikatoren() as ident:
            n = ident.exportiere_csv(pfad, klasse)
        self.status.setText(self.tr("{n} Einträge nach {pfad} geschrieben.").format(
            n=n, pfad=pfad))
        return n

    def importiere_waehlen(self) -> None:
        """Ask for a CSV or list file and take its identifiers into the table."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Identifikatoren importieren"), "",
            self.tr("CSV und Listen (*.csv *.txt);;Alle Dateien (*)"))
        if pfad:
            self.importiere_datei(Path(pfad))

    def importiere_datei(self, pfad: Path, klasse: str | None = None) -> int:
        """Take the identifiers of a CSV or list file into the table, rows without a class
        into ``klasse`` (default: the class box); returns how many were new."""
        klasse = (klasse if klasse is not None
                  else self.neue_klasse.currentText()).strip() or identmodul.KLASSE_NUTZER
        try:
            with self._identifikatoren() as ident:
                neu, gelesen = ident.importiere_csv(pfad, klasse)
        except OSError as e:
            self._melde(self.tr("Identifikatoren"), str(e))
            return 0
        self._cache_leeren()
        self.status.setText(self.tr(
            "{neu} neue von {gelesen} Identifikatoren aus {name} übernommen.").format(
                neu=neu, gelesen=gelesen, name=pfad.name))
        self.aktualisiere(True)
        return neu
