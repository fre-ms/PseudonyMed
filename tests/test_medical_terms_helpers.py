"""PseudonyMed's vocabulary over lexcor's matcher: what the adapter adds, and the lists.

The matcher's inner workings (which first parts are eponyms, how phrases are followed, when
a span is wholly covered) are tested in lexcor, where the matcher lives.
"""

from __future__ import annotations

from pathlib import Path

import lexterm
import pytest
from lexcor import StoreError

from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod


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


def test_the_eponym_heads_are_lexterms() -> None:
    fb = terms_module.MedicalTerms()
    assert fb.cover.heads >= {"zeichen", "manöver"} and "gang" not in lexterm.EPONYM_HEADS
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


def test_without_a_store_the_bundled_lists_protect(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback reads the bundled lists directly, each under its category; a store
    that cannot be opened is the case for it."""
    fb = terms_module.load()
    assert fb.sources == [(k, len(terms)) for k, terms in vocab_mod.bundled().items()]
    assert fb.is_medical_term("Nierenlager") and not fb.is_medical_term("Flantzitis")
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path))
    terms_module.standard.cache_clear()
    with vocab_mod.Vocabulary() as v:
        v.add(["Flantzitis"])
    assert terms_module.standard().is_medical_term("Flantzitis")

    def refuse(self: vocab_mod.Vocabulary, *arguments: object, **options: object) -> None:
        raise StoreError("no-store", "Die Ablage lässt sich nicht öffnen.",
                         "The store cannot be opened.")

    monkeypatch.setattr(vocab_mod.Vocabulary, "__init__", refuse)
    terms_module.standard.cache_clear()
    without = terms_module.standard()
    assert without.cover.single == fb.cover.single and without.cover.phrases == fb.cover.phrases
    terms_module.standard.cache_clear()
