"""Helpers of the vocabulary matcher: eponym heads, phrase matching, whole-term cover, lists."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pseudonymed.detection import medical_terms as terms_module


def test_eponym_front_part() -> None:
    assert terms_module._leading_eponym("babinski-zeichen") == "babinski"
    assert terms_module._leading_eponym("berlin-blau") is None  # chemistry head
    assert terms_module._leading_eponym("konigsmark-hollander-berlin-syndrom") is None
    assert terms_module._leading_eponym("abc-test") is None  # first part too short
    assert terms_module._leading_eponym("parkinson") is None


def test_follows_with_umlauts() -> None:
    normed = ["morbus", "parkinson", "hüft-tep", "rechts"]
    assert terms_module._follows(normed, 0, ("morbus", "parkinson"))
    assert terms_module._follows(normed, 2, ("hueft-tep", "rechts"))
    assert not terms_module._follows(normed, 1, ("parkinson", "links"))
    assert not terms_module._follows(normed, 3, ("rechts", "oben"))  # runs past the end


def test_fully_covered() -> None:
    text = "Morbus Parkinson in Berlin"
    spans = [(0, 16)]
    assert terms_module._fully_covered(text, 0, 16, spans)
    assert terms_module._fully_covered(text, 7, 16, spans)
    assert not terms_module._fully_covered(text, 0, 26, spans)
    assert terms_module._fully_covered(text, 16, 17, spans)  # no token in a bare space


def test_remember_forms() -> None:
    fb = terms_module.MedicalTerms()
    n = fb.add(["Bauch (Anatomie)", "# Kommentar", "ab", "Tabletten/Kapseln",
                "eins zwei drei vier fünf sechs", "Trömner-Reflex"])
    assert n == 4  # the six-word phrase counts as taken but is too long to be filed
    assert fb.single_words == {"bauch", "trömner-reflex", "troemner-reflex", "trömner", "troemner"}
    assert fb.multi_words == {"tabletten": {("tabletten", "kapseln")}}
    assert fb.sources == []


def test_phrase_length_longest_first() -> None:
    fb = terms_module.MedicalTerms()
    fb.add(["Morbus Parkinson", "Morbus Parkinson Stadium III", "Hüft-TEP rechts"])
    normed = ["morbus", "parkinson", "stadium", "iii", "hueft-tep", "rechts"]
    assert fb._phrase_length(normed, 0) == 4
    assert fb._phrase_length(normed[:2], 0) == 2
    assert fb._phrase_length(normed, 4) == 2
    assert fb._phrase_length(normed, 1) == 0


def test_counts_short_words_individually() -> None:
    fb = terms_module.MedicalTerms()
    fb.add(["Ohr", "hat", "Bauch"])
    assert fb._counts_alone("Ohr") and fb._counts_alone("HAT")
    assert not fb._counts_alone("hat") and not fb._counts_alone("ohr")
    assert fb._counts_alone("bauches")  # longer than three letters: case does not matter
    assert not fb._counts_alone("Bauer")


def test_user_lists_and_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    assert terms_module._user_lists() == []
    folder = tmp_path / "fachbegriffe"
    folder.mkdir()
    (folder / "b.txt").write_text("Zweitkrankheit\n", encoding="utf-8")
    with gzip.open(folder / "a.txt.gz", "wt", encoding="utf-8") as f:
        f.write("# Kopf\nErstkrankheit\n")
    (folder / "c.csv").write_text("x\n", encoding="utf-8")
    (folder / "d.txt").mkdir()
    assert [p.name for p in terms_module._user_lists()] == ["a.txt.gz", "b.txt"]
    fb = terms_module.load(with_base=False)
    assert [q for q, _n in fb.sources] == ["Eponyme", "a.txt.gz", "b.txt"]
    assert fb.is_medical_term("Erstkrankheit") and fb.is_medical_term("Zweitkrankheit")
    eponyms_only = terms_module.load(with_base=False, with_user_data=False)
    assert [q for q, _n in eponyms_only.sources] == ["Eponyme"]
    assert not eponyms_only.is_medical_term("Erstkrankheit")
