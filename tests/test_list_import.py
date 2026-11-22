"""The shared list reader of the vocabulary and the identifiers: delimiter, header, column."""

from __future__ import annotations

from pathlib import Path

from pseudonymed.detection import list_import


def test_csv_with_header_groups_by_the_column(tmp_path: Path) -> None:
    file = tmp_path / "a.csv"
    file.write_text("Begriff;Kategorie\nGamma;Kat B\nAlpha;Kat A\nDelta;\n;ohne Begriff\n",
                    encoding="utf-8")
    g = list_import.read_list(file, "kategorie", "Standard")
    assert g.groups == {"Kat B": ["Gamma"], "Kat A": ["Alpha"], "Standard": ["Delta"]}
    assert g.lines == 3  # the header and the row without a term do not count


def test_header_names_the_column_elsewhere(tmp_path: Path) -> None:
    file = tmp_path / "b.csv"
    file.write_text("wort,quelle,klasse\nTischler,eigene,Beruf\nMusterwerk,eigene\n",
                    encoding="utf-8")
    g = list_import.read_list(file, "klasse", "Sonstiges")
    assert g.groups == {"Beruf": ["Tischler"], "Sonstiges": ["Musterwerk"]} and g.lines == 2


def test_without_header_the_second_column_counts(tmp_path: Path) -> None:
    file = tmp_path / "c.csv"
    file.write_text("Alpha\tKat A\nBeta\n", encoding="utf-8")
    g = list_import.read_list(file, "kategorie", "Liste")
    assert g.groups == {"Kat A": ["Alpha"], "Liste": ["Beta"]} and g.lines == 2


def test_list_without_separators_and_comments(tmp_path: Path) -> None:
    file = tmp_path / "d.txt"
    file.write_text("# Liste\nEpsilon\n\n  Zeta Zwei \n", encoding="utf-8")
    g = list_import.read_list(file, "kategorie", "Liste")
    assert g.groups == {"Liste": ["Epsilon", "Zeta Zwei"]} and g.lines == 2


def test_empty_file(tmp_path: Path) -> None:
    file = tmp_path / "e.txt"
    file.write_text("# nur ein Kommentar\n\n", encoding="utf-8")
    assert list_import.read_list(file, "kategorie", "Liste") == list_import.ReadResult({}, 0)
