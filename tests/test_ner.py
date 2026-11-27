"""Optional NER: unknown people and places, eponyms excluded, round trip through the store."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pymupdf
import pytest
from optdoc.pagetext import read_text_layer

from pseudonymed.checks.report import as_text
from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import ner as ner_module
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.synthetic import create_report_pdf

needs_ner = pytest.mark.skipif(not ner_module.available(), reason="spaCy oder Modell fehlt")


def test_plausibility() -> None:
    assert ner_module._person_core("Dr. med. Erika Beispiel") == "Erika Beispiel"
    assert ner_module._person_core("Ehefrau Hildegund Flantz") == "Hildegund Flantz"
    assert ner_module._plausible_name("Erika Beispiel")
    assert not ner_module._plausible_name("Alzheimer")
    assert not ner_module._plausible_name("Morbus Parkinson")
    assert not ner_module._plausible_name("max mustermann")
    assert not ner_module._plausible_name("Ab")


@needs_ner
def test_find_ner_on_page(patient: Patient) -> None:
    doc = pymupdf.open()
    pdf_page = doc.new_page()
    y = 80
    for line in ("Die Ehefrau Hildegund Flantz begleitete den Patienten nach Dresden.",
                 "Diagnose: Morbus Alzheimer, Verdacht auf Parkinson-Syndrom.",
                 "Befundbericht von Dr. med. Erika Hoffmann, Klinikum Chemnitz."):
        pdf_page.insert_text((56, y), line, fontsize=10.5, fontname="helv")
        y += 14
    page = read_text_layer(pdf_page)
    alone = dataclasses.replace(patient, relatives=[], referrers=[])
    v = PseudonymAssignment(alone, "K7Q4MX", secret=bytes(32))
    matches = ner_module.find_ner(page, ner_module.NER(), v, [])
    texts = {f.text: f for f in matches}
    assert "Hildegund Flantz" in texts and texts["Hildegund Flantz"].pseudonym.startswith("Person ")
    assert "Erika Hoffmann" in texts and texts["Erika Hoffmann"].rule == "ner.person"
    assert "Dresden" in texts and texts["Dresden"].pseudonym.startswith("[Ort ")
    assert not any("Alzheimer" in t or "Parkinson" in t for t in texts)
    assert v.mapping[texts["Hildegund Flantz"].pseudonym] == "Hildegund Flantz"
    # the same name gives the same code again
    assert v.unknown_person("Hildegund  Flantz") == texts["Hildegund Flantz"].pseudonym


@needs_ner
def test_ner_in_the_pipeline(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    alone = dataclasses.replace(patient, relatives=[], referrers=[])
    report_pdf = create_report_pdf(tmp_path / "befund.pdf", patient)
    without = pseudonymise(report_pdf, alone, PipelineOptions(ner=False, suffix="_ohne"),
                           store=store)
    assert patient.relatives[0].first_name in without.text  # the rules do not know the wife
    using = pseudonymise(report_pdf, alone, PipelineOptions(ner=True, suffix="_mit"),
                         store=store)
    assert using.released, as_text(using.report)
    assert patient.relatives[0].first_name not in using.text
    assert any(f.rule == "ner.person" for f in using.hits)
    assert any("NER (" in w for w in using.report.notes)
    person = next(f for f in using.hits if f.rule == "ner.person"
                  and patient.relatives[0].surname in f.text)
    z = store.mapping(alone.key())
    r = reidentify(f"{person.pseudonym} war dabei.", z, known_codes=store.codes())
    assert patient.relatives[0].full_name in r.text and r.remaining == []


def test_without_model_warning(tmp_path: Path, report_pdf: Path, patient: Patient,
                               store: PseudonymStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ner_module, "available", lambda: False)
    res = pseudonymise(report_pdf, patient, PipelineOptions(ner=True), store=store)
    assert res.released and any("NER gewünscht" in w for w in res.report.warnings)


def test_everyday_nouns_are_not_names() -> None:
    """Findings prose and lists: common and compound nouns are not people or places,
    salutations and prepositions still make unknown words count."""
    import pytest

    from pseudonymed.detection import ner as ner_module

    if not ner_module.available():
        pytest.skip("Extra 'ner' nicht installiert")
    n = ner_module.NER()
    listing = "Konzentration: gut.\nHaltung: aufrecht.\nNierenlager: frei.\nStrichgang: unsicher."
    assert n.entities(listing) == []
    prose = ("Befund: Konzentration und Aufmerksamkeit regelrecht, Haltung aufrecht,"
             " Nierenlager frei, Strichgang unsicher.")
    assert n.entities(prose) == []
    names = "Herr Flantz kam mit Frau Erika Hoffmann aus Dresden; sie wohnen in Riesa."
    texts = {e[3] for e in n.entities(names)}
    assert {"Erika Hoffmann", "Dresden"} <= texts
    assert "Flantz" in texts or "Herr Flantz" in texts  # unknown surname, salutation before
    assert any("Riesa" in t for t in texts)


def _hit(start: int, end: int, kind: Kind) -> Hit:
    return Hit(page=0, start=start, end=end, text="x", kind=kind, rule="r",
               pseudonym="p")


def test_partial_names_yield_to_the_full_name() -> None:
    """The overlap step without the model: lone partial names inside the span give way,
    any other overlap drops the entity, an empty span never overlaps."""
    first_name, surname = _hit(0, 9, Kind.FIRST_NAME), _hit(10, 16, Kind.SURNAME)
    place = _hit(30, 35, Kind.PLACE)
    existing = [first_name, surname, place]
    assert ner_module._displaced_partial_matches(0, 16, existing, []) == [first_name, surname]
    assert existing == [first_name, surname, place]  # removing them is the caller's job
    # a hit of another kind, or a partial name reaching beyond the span, keeps the entity out
    assert ner_module._displaced_partial_matches(28, 40, existing, []) is None
    assert ner_module._displaced_partial_matches(12, 20, existing, []) is None
    # a span equal to the partial hit is no longer name
    assert ner_module._displaced_partial_matches(10, 16, existing, []) is None
    # hits NER found itself on this page are never replaced
    assert ner_module._displaced_partial_matches(0, 16, [], [surname]) is None
    assert ner_module._displaced_partial_matches(0, 16, [_hit(5, 5, Kind.SURNAME)], []) == []


def test_candidates_without_model(patient: Patient) -> None:
    """Which entities become hits, and that the code is registered as soon as it is drawn."""
    alone = dataclasses.replace(patient, relatives=[], referrers=[])
    v = PseudonymAssignment(alone, "K7Q4MX", secret=bytes(32))
    person = ner_module._candidate_from("PER", "Frau Dr. Erika Beispiel,", v)
    assert person is not None
    assert (person.core, person.kind, person.rule) == ("Erika Beispiel", Kind.NAME, "ner.person")
    assert v.mapping[person.pseudonym] == "Erika Beispiel"
    place = ner_module._candidate_from("GPE", "(Dresden)", v)
    assert place is not None and (place.core, place.rule) == ("Dresden", "ner.ort")
    assert place.pseudonym.startswith("[Ort ")
    assert ner_module._candidate_from("PER", "Morbus Parkinson", v) is None
    assert ner_module._candidate_from("LOC", "Glasgow", v) is None
    assert ner_module._candidate_from("LOC", "dresden", v) is None
    assert ner_module._candidate_from("MISC", "Erika Beispiel", v) is None


def test_vocabulary_protection_counts_spared() -> None:
    """The protection step without the model: only a span made up of medical terms is
    spared, every spared core is recorded, and without a vocabulary nothing is."""
    text = "Morbus Fabry und Erika Beispiel in Dresden"
    spared: list[str] = []
    protection = ner_module._VocabularyGuard.of(text, terms_module.standard(), spared)
    assert protection.protects(0, len("Morbus Fabry"), "Morbus Fabry")
    a = text.index("Erika")
    assert not protection.protects(a, a + len("Erika Beispiel"), "Erika Beispiel")
    assert spared == ["Morbus Fabry"]
    without = ner_module._VocabularyGuard.of(text, None, None)
    assert not without.protects(0, len("Morbus Fabry"), "Morbus Fabry")


def test_no_common_name_is_an_eponym() -> None:
    """The names struck on 27 November 2026 are common among people; none is an eponym
    any more, and the rare ones stay."""
    assert ner_module.RETIRED_EPONYMS.isdisjoint(ner_module.EPONYMS)
    assert {"becker", "weber", "hoffmann", "wilson", "jakob"} <= ner_module.RETIRED_EPONYMS
    assert {"alzheimer", "parkinson", "babinski", "lasègue"} <= ner_module.EPONYMS


class _Entities:
    """A model that names the entities it is given, in the order given."""

    def __init__(self, *found: tuple[str, str]) -> None:
        self.found = found

    def entities(self, text: str) -> list[tuple[int, int, str, str]]:
        out, at = [], 0
        for words, label in self.found:
            start = text.index(words, at)
            out.append((start, start + len(words), label, words))
            at = start + len(words)
        return out


def test_context_outweighs_the_lists(patient: Patient) -> None:
    """A salutation, title or kinship word makes a person of a name the lists know as an
    eponym or a medical term; a preposition of place makes a place of a municipality."""
    from pseudonymed.document.text_file import text_page

    alone = dataclasses.replace(patient, relatives=[], referrers=[])
    v = PseudonymAssignment(alone, "K7Q4MX", secret=bytes(32))
    # without context the eponym list decides, with it the person counts
    assert ner_module._candidate_from("PER", "Parkinson", v) is None
    named = ner_module._candidate_from("PER", "Parkinson", v, context=True)
    assert named is not None and named.named and named.core == "Parkinson"
    assert ner_module._candidate_from("PER", "Dr. Parkinson", v).named  # type: ignore[union-attr]
    # a municipality after a preposition of place is a place; elsewhere a list decides
    assert ner_module._candidate_from("LOC", "Essen", v, context=True).named  # type: ignore[union-attr]
    assert ner_module._candidate_from("LOC", "Glasgow", v, context=True) is None  # no town here
    text = ("Frau Iris Nagel begleitete ihn. Iris Nagel unauffällig. Er wohnt in Haar, das Haar"
            " ist dünn. Babinski negativ.")
    assert ner_module._in_context("PER", text, text.index("Iris"))
    assert not ner_module._in_context("PER", text, text.index("Iris Nagel unauff"))
    assert ner_module._in_context("LOC", text, text.index("Haar,"))
    assert not ner_module._in_context("LOC", text, text.index("Haar ist"))
    page = text_page(text)
    model = _Entities(("Iris Nagel", "PER"), ("Iris Nagel", "PER"), ("Haar", "LOC"),
                      ("Haar", "LOC"), ("Babinski", "PER"))
    spared: list[str] = []
    found = ner_module.find_ner(page, model, v, [], medical_terms=terms_module.load(),  # type: ignore[arg-type]
                                spared=spared)
    assert [(text[f.start:f.end], f.start) for f in found] == [
        ("Iris Nagel", 5), ("Haar", text.index("Haar,"))]
    assert spared == ["Iris Nagel", "Haar"]  # without context the vocabulary spares them
