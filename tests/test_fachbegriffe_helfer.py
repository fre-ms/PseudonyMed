"""Helpers of the vocabulary matcher: eponym heads, phrase matching, whole-term cover, lists."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pseudonymed.erkennung import fachbegriffe as fachmodul


def test_eponym_vorderteil() -> None:
    assert fachmodul._eponym_vorderteil("babinski-zeichen") == "babinski"
    assert fachmodul._eponym_vorderteil("berlin-blau") is None  # chemistry head
    assert fachmodul._eponym_vorderteil("konigsmark-hollander-berlin-syndrom") is None
    assert fachmodul._eponym_vorderteil("abc-test") is None  # first part too short
    assert fachmodul._eponym_vorderteil("parkinson") is None


def test_folgt_mit_umlauten() -> None:
    normen = ["morbus", "parkinson", "hüft-tep", "rechts"]
    assert fachmodul._folgt(normen, 0, ("morbus", "parkinson"))
    assert fachmodul._folgt(normen, 2, ("hueft-tep", "rechts"))
    assert not fachmodul._folgt(normen, 1, ("parkinson", "links"))
    assert not fachmodul._folgt(normen, 3, ("rechts", "oben"))  # runs past the end


def test_ganz_bedeckt() -> None:
    text = "Morbus Parkinson in Berlin"
    spannen = [(0, 16)]
    assert fachmodul._ganz_bedeckt(text, 0, 16, spannen)
    assert fachmodul._ganz_bedeckt(text, 7, 16, spannen)
    assert not fachmodul._ganz_bedeckt(text, 0, 26, spannen)
    assert fachmodul._ganz_bedeckt(text, 16, 17, spannen)  # no token in a bare space


def test_merke_formen() -> None:
    fb = fachmodul.Fachbegriffe()
    n = fb.fuege_hinzu(["Bauch (Anatomie)", "# Kommentar", "ab", "Tabletten/Kapseln",
                        "eins zwei drei vier fünf sechs", "Trömner-Reflex"])
    assert n == 4  # the six-word phrase counts as taken but is too long to be filed
    assert fb.einzel == {"bauch", "trömner-reflex", "troemner-reflex", "trömner", "troemner"}
    assert fb.mehrwort == {"tabletten": {("tabletten", "kapseln")}}
    assert fb.quellen == []


def test_phrasenlaenge_laengste_zuerst() -> None:
    fb = fachmodul.Fachbegriffe()
    fb.fuege_hinzu(["Morbus Parkinson", "Morbus Parkinson Stadium III", "Hüft-TEP rechts"])
    normen = ["morbus", "parkinson", "stadium", "iii", "hueft-tep", "rechts"]
    assert fb._phrasenlaenge(normen, 0) == 4
    assert fb._phrasenlaenge(normen[:2], 0) == 2
    assert fb._phrasenlaenge(normen, 4) == 2
    assert fb._phrasenlaenge(normen, 1) == 0


def test_zaehlt_einzeln_kurze_woerter() -> None:
    fb = fachmodul.Fachbegriffe()
    fb.fuege_hinzu(["Ohr", "hat", "Bauch"])
    assert fb._zaehlt_einzeln("Ohr") and fb._zaehlt_einzeln("HAT")
    assert not fb._zaehlt_einzeln("hat") and not fb._zaehlt_einzeln("ohr")
    assert fb._zaehlt_einzeln("bauches")  # longer than three letters: case does not matter
    assert not fb._zaehlt_einzeln("Bauer")


def test_nutzerlisten_und_laden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    assert fachmodul._nutzerlisten() == []
    ordner = tmp_path / "fachbegriffe"
    ordner.mkdir()
    (ordner / "b.txt").write_text("Zweitkrankheit\n", encoding="utf-8")
    with gzip.open(ordner / "a.txt.gz", "wt", encoding="utf-8") as f:
        f.write("# Kopf\nErstkrankheit\n")
    (ordner / "c.csv").write_text("x\n", encoding="utf-8")
    (ordner / "d.txt").mkdir()
    assert [p.name for p in fachmodul._nutzerlisten()] == ["a.txt.gz", "b.txt"]
    fb = fachmodul.laden(mit_basis=False)
    assert [q for q, _n in fb.quellen] == ["Eponyme", "a.txt.gz", "b.txt"]
    assert fb.ist_fachbegriff("Erstkrankheit") and fb.ist_fachbegriff("Zweitkrankheit")
    nur_eponyme = fachmodul.laden(mit_basis=False, mit_nutzerdaten=False)
    assert [q for q, _n in nur_eponyme.quellen] == ["Eponyme"]
    assert not nur_eponyme.ist_fachbegriff("Erstkrankheit")
