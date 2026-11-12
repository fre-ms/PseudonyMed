"""The shared list reader of the vocabulary and the identifiers: delimiter, header, column."""

from __future__ import annotations

from pathlib import Path

from pseudonymed.erkennung import listenimport


def test_csv_mit_kopf_gruppiert_nach_der_spalte(tmp_path: Path) -> None:
    datei = tmp_path / "a.csv"
    datei.write_text("Begriff;Kategorie\nGamma;Kat B\nAlpha;Kat A\nDelta;\n;ohne Begriff\n",
                     encoding="utf-8")
    g = listenimport.lies_liste(datei, "kategorie", "Standard")
    assert g.gruppen == {"Kat B": ["Gamma"], "Kat A": ["Alpha"], "Standard": ["Delta"]}
    assert g.zeilen == 3  # the header and the row without a term do not count


def test_kopf_nennt_die_spalte_an_anderer_stelle(tmp_path: Path) -> None:
    datei = tmp_path / "b.csv"
    datei.write_text("wort,quelle,klasse\nTischler,eigene,Beruf\nMusterwerk,eigene\n",
                     encoding="utf-8")
    g = listenimport.lies_liste(datei, "klasse", "Sonstiges")
    assert g.gruppen == {"Beruf": ["Tischler"], "Sonstiges": ["Musterwerk"]} and g.zeilen == 2


def test_ohne_kopf_zaehlt_die_zweite_spalte(tmp_path: Path) -> None:
    datei = tmp_path / "c.csv"
    datei.write_text("Alpha\tKat A\nBeta\n", encoding="utf-8")
    g = listenimport.lies_liste(datei, "kategorie", "Liste")
    assert g.gruppen == {"Kat A": ["Alpha"], "Liste": ["Beta"]} and g.zeilen == 2


def test_liste_ohne_trennzeichen_und_kommentare(tmp_path: Path) -> None:
    datei = tmp_path / "d.txt"
    datei.write_text("# Liste\nEpsilon\n\n  Zeta Zwei \n", encoding="utf-8")
    g = listenimport.lies_liste(datei, "kategorie", "Liste")
    assert g.gruppen == {"Liste": ["Epsilon", "Zeta Zwei"]} and g.zeilen == 2


def test_leere_datei(tmp_path: Path) -> None:
    datei = tmp_path / "e.txt"
    datei.write_text("# nur ein Kommentar\n\n", encoding="utf-8")
    assert listenimport.lies_liste(datei, "kategorie", "Liste") == listenimport.Gelesen({}, 0)
