"""The shared base of the list steps: the button texts that moved into it still translate
on both pages, the selection maps back to model rows through sort and filter, and the
model plumbing answers Qt as before."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from pseudonymed.erkennung import lexikon
from pseudonymed.erkennung.identifikatoren import Eintrag
from pseudonymed.gui.identifikatoren_seite import IdentifikatorenSeite
from pseudonymed.gui.sitzung import Sitzung
from pseudonymed.gui.vokabular_seite import VokabularSeite
from tests.test_gui import qapp  # noqa: F401

GEMEINSAM_DE = {"Ausgewählte löschen", "CSV exportieren …", "CSV oder Liste importieren …",
                "Aktualisieren"}
GEMEINSAM_EN = {"Delete selected", "Export CSV …", "Import CSV or list …", "Refresh"}


def _knopftexte(seite: IdentifikatorenSeite | VokabularSeite) -> set[str]:
    return {k.text() for k in seite.findChildren(QPushButton)}


def test_gemeinsame_knoepfe_uebersetzt(qapp: QApplication) -> None:  # noqa: F811
    """The button texts that live in the base class reach the English interface of both
    pages: Qt looks them up under the page's class first, then under the base's."""
    from pseudonymed.gui import uebersetzung

    uebersetzer = uebersetzung.installiere(qapp, "en_gb")
    assert uebersetzer is not None
    try:
        for seite in (IdentifikatorenSeite(Sitzung(speicherpfad=None)),
                      VokabularSeite(Sitzung(speicherpfad=None))):
            texte = _knopftexte(seite)
            assert GEMEINSAM_EN <= texte and not GEMEINSAM_DE & texte, texte
    finally:
        qapp.removeTranslator(uebersetzer)
    assert GEMEINSAM_DE <= _knopftexte(VokabularSeite(Sitzung(speicherpfad=None)))


def test_auswahl_folgt_der_sortierung(qapp: QApplication) -> None:  # noqa: F811
    """Selected view rows come back as model rows in model order, whatever the sort."""
    seite = VokabularSeite(Sitzung(speicherpfad=None))
    seite.modell.lade([("Wade", "Anatomie", "2026-01-02T10:00:00"),
                       ("Arm", "Anatomie", "2026-01-02T10:00:00"),
                       ("Knie", "Anatomie", "2026-01-02T10:00:00")])
    seite.ansicht.sortByColumn(0, Qt.SortOrder.DescendingOrder)  # Wade, Knie, Arm
    auswahl = seite.ansicht.selectionModel()
    for zeile in (0, 2):
        auswahl.select(seite.proxy.index(zeile, 0), QItemSelectionModel.SelectionFlag.Select
                       | QItemSelectionModel.SelectionFlag.Rows)
    assert seite.ausgewaehlte() == ["Wade", "Arm"]
    assert seite.modell.data(seite.modell.index(0, 2)) == "2026-01-02 10:00"


def test_modell_antwortet_qt(qapp: QApplication) -> None:  # noqa: F811
    """Only term and class are editable, the header names the columns, a foreign role
    yields nothing, and an edit in a foreign role is refused before it reaches the page."""
    seite = IdentifikatorenSeite(Sitzung(speicherpfad=None))
    modell = seite.modell
    modell.lade([Eintrag("Musterwerk AG", "Sonstiges", lexikon.NUTZER, "", "")])
    assert modell.rowCount() == 1 and modell.columnCount() == 4
    assert [modell.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == [
        "Begriff", "Klasse", "Angaben", "Quelle"]
    assert modell.headerData(0, Qt.Orientation.Vertical) is None
    assert modell.data(modell.index(0, 3)) == "eigene"
    assert modell.data(modell.index(0, 0), Qt.ItemDataRole.ToolTipRole) is None
    editierbar = [bool(modell.flags(modell.index(0, i)) & Qt.ItemFlag.ItemIsEditable)
                  for i in range(4)]
    assert editierbar == [True, True, False, False]
    assert not modell.setData(modell.index(0, 0), "Anderes", Qt.ItemDataRole.DisplayRole)
    assert modell.data(modell.index(0, 0)) == "Musterwerk AG"
