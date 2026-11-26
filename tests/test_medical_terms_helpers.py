"""PseudonyMed's vocabulary over lexcor's matcher: what the adapter adds, and the lists.

The matcher's inner workings (which first parts are eponyms, how phrases are followed, when
a span is wholly covered) are tested in lexcor, where the matcher lives.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pseudonymed.detection import medical_terms as terms_module


def test_terms_are_taken_counted_and_traced_to_their_list() -> None:
    fb = terms_module.MedicalTerms()
    n = fb.add(["Bauch (Anatomie)", "# Kommentar", "ab", "Tabletten/Kapseln",
                "eins zwei drei vier fünf sechs", "Trömner-Reflex"])
    assert n == 4  # the six-word phrase counts as taken but is too long to be filed
    assert fb.cover.single == {"bauch", "trömner-reflex", "troemner-reflex", "trömner",
                               "troemner"}
    assert fb.cover.phrases == {"tabletten": {("tabletten", "kapseln")}}
    assert fb.count == 6 and fb.sources == []
    assert fb.add(["Herz", "# Kopf"], "Liste") == 1 and fb.sources == [("Liste", 1)]


def test_the_eponym_heads_are_pseudonymeds() -> None:
    fb = terms_module.MedicalTerms()
    fb.add(["Babinski-Zeichen", "Lasègue-Manöver", "Berlin-Blau", "Müller-Gang"])
    assert fb.is_medical_term("Babinski") and fb.is_medical_term("Lasègue")
    # anatomy and chemistry heads protect neither the town nor the surname
    assert not fb.is_medical_term("Berlin") and not fb.is_medical_term("Müller")


def test_protection_asks_the_matcher() -> None:
    fb = terms_module.MedicalTerms()
    fb.add(["Morbus Parkinson", "Ohr", "Bauch"])
    text = "Morbus Parkinson in Berlin, das Ohr frei, ohr nicht, Bauches."
    spans = fb.spans(text)
    assert [text[a:b] for a, b in spans] == ["Morbus Parkinson", "Ohr", "Bauches"]
    assert fb.is_protected(text, 0, 16, spans, whole=True)
    assert fb.is_protected(text, 7, 26, spans) and not fb.is_protected(text, 7, 26, spans, True)
    assert not fb.is_protected(text, 20, 26)


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
