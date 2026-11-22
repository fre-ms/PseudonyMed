"""Helpers of the lexicon loader: which bundled sources load, and how their rows are prepared."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pseudonymed.detection import lexicon


def _rows(path: Path) -> Iterator[lexicon.Row]:
    yield "  Herz   Kreislauf ", "Anatomie", lexicon.MEDICAL, "", ""
    yield "x", "Anatomie", lexicon.MEDICAL, "", ""  # shorter than MIN_LAENGE
    yield " ", "Anatomie", lexicon.MEDICAL, "", ""


def _from_code() -> Iterator[lexicon.Row]:
    yield "ab", "Eponym", lexicon.MEDICAL, "", ""


def test_lines_from_cleaned() -> None:
    file = lexicon.Source("Probe", Path("egal"), _rows)
    assert list(lexicon._rows_of(file)) == [("Herz Kreislauf", "Anatomie", lexicon.MEDICAL,
                                             "", "")]
    code = lexicon.Source("Code", None, _from_code)
    assert list(lexicon._rows_of(code)) == [("ab", "Eponym", lexicon.MEDICAL, "", "")]


def test_to_load(tmp_path: Path) -> None:
    da = tmp_path / "da.txt"
    da.write_text("x\n", encoding="utf-8")
    existing = lexicon.Source("Da", da, _rows)
    missing = lexicon.Source("Fehlt", tmp_path / "fehlt.txt", _rows)
    code = lexicon.Source("Code", None, _from_code)
    assert lexicon._should_load(existing, None) and lexicon._should_load(code, None)
    assert not lexicon._should_load(missing, None) and not lexicon._should_load(missing, {"Fehlt"})
    assert lexicon._should_load(existing, set())  # no names given means every source
    assert lexicon._should_load(existing, {"Da"}) and not lexicon._should_load(existing, {"Code"})
