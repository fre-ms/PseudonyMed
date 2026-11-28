"""Helpers of the lexicon loader: which bundled sources load, and how their rows are prepared."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pseudonymed.detection import lexicon


def _rows(path: Path) -> Iterator[lexicon.Row]:
    yield "  Herz   Kreislauf ", "anatomy", lexicon.MEDICAL, "", ""
    yield "x", "anatomy", lexicon.MEDICAL, "", ""  # shorter than MIN_LENGTH
    yield " ", "anatomy", lexicon.MEDICAL, "", ""


def _from_code() -> Iterator[lexicon.Row]:
    yield "ab", "eponym", lexicon.MEDICAL, "", ""


def test_lines_from_cleaned() -> None:
    file = lexicon.Source("Probe", Path("egal"), _rows)
    assert list(lexicon.rows_of(file)) == [("Herz Kreislauf", "anatomy", lexicon.MEDICAL,
                                            "", "")]
    code = lexicon.Source("Code", None, _from_code)
    assert list(lexicon.rows_of(code)) == [("ab", "eponym", lexicon.MEDICAL, "", "")]


def test_sources_present_and_their_state(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    da = tmp_path / "da.txt"
    da.write_text("# Source: invented; Licence: none\nx\n", encoding="utf-8")
    existing = lexicon.Source("Da", da, _rows)
    missing = lexicon.Source("Fehlt", tmp_path / "fehlt.txt", _rows)
    code = lexicon.Source("Code", None, _from_code)
    monkeypatch.setattr(lexicon, "SOURCES", (existing, missing, code))
    assert lexicon.available(("Da", "Fehlt", "Code")) == [existing, code]
    assert lexicon.available(("Fehlt",)) == [] and lexicon.available() == []  # no bundled name
    assert lexicon.note(existing) == "Source: invented; Licence: none" and lexicon.note(code) == ""
    before = lexicon.state(existing)
    assert before == lexicon.state(existing) and len(before) == 16
    da.write_text("# Source: invented\nxy\n", encoding="utf-8")
    assert lexicon.state(existing) != before  # the data changed
    assert lexicon.state(code) == lexicon.state(code)
    assert lexicon.key("ÄRZTIN Abc") == "Ärztin abc"  # the letters A to Z only
