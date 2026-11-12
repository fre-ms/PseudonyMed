"""Helpers of the lexicon loader: which bundled sources load, and how their rows are prepared."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pseudonymed.erkennung import lexikon


def _zeilen(pfad: Path) -> Iterator[lexikon.Zeile]:
    yield "  Herz   Kreislauf ", "Anatomie", lexikon.FACH, "", ""
    yield "x", "Anatomie", lexikon.FACH, "", ""  # shorter than MIN_LAENGE
    yield " ", "Anatomie", lexikon.FACH, "", ""


def _aus_code() -> Iterator[lexikon.Zeile]:
    yield "ab", "Eponym", lexikon.FACH, "", ""


def test_zeilen_von_bereinigt() -> None:
    datei = lexikon.Quelle("Probe", Path("egal"), _zeilen)
    assert list(lexikon._zeilen_von(datei)) == [("Herz Kreislauf", "Anatomie", lexikon.FACH,
                                                 "", "")]
    code = lexikon.Quelle("Code", None, _aus_code)
    assert list(lexikon._zeilen_von(code)) == [("ab", "Eponym", lexikon.FACH, "", "")]


def test_zu_laden(tmp_path: Path) -> None:
    da = tmp_path / "da.txt"
    da.write_text("x\n", encoding="utf-8")
    vorhanden = lexikon.Quelle("Da", da, _zeilen)
    fehlt = lexikon.Quelle("Fehlt", tmp_path / "fehlt.txt", _zeilen)
    code = lexikon.Quelle("Code", None, _aus_code)
    assert lexikon._zu_laden(vorhanden, None) and lexikon._zu_laden(code, None)
    assert not lexikon._zu_laden(fehlt, None) and not lexikon._zu_laden(fehlt, {"Fehlt"})
    assert lexikon._zu_laden(vorhanden, set())  # no names given means every source
    assert lexikon._zu_laden(vorhanden, {"Da"}) and not lexikon._zu_laden(vorhanden, {"Code"})
