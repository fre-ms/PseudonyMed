"""Medical vocabulary: matching, protection of NER and fuzzy search, import."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import pymupdf
import pytest
from optdoc.ocr import OCRWord, page_text_from_words
from typer.testing import CliRunner

from pseudonymed.core.model import Patient
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import ner as ner_module
from pseudonymed.detection.fuzzy import find_fuzzy, targets_for
from pseudonymed.pseudonyms.assignment import PseudonymAssignment


def _small() -> terms_module.MedicalTerms:
    fb = terms_module.MedicalTerms()
    fb.add(["Bauch", "Herz", "Morbus Parkinson", "arterielle Hypertonie", "Hypertonie",
            "Ramipril", "Babinski-Zeichen", "Mini-Mental-Status-Test", "Alzheimer",
            "Parkinson"], "test")
    return fb


def test_single_word_inflection_and_hyphen() -> None:
    fb = _small()
    for w in ("Bauch", "BAUCH", "Bauches", "Herzen", "Ramipril", "Parkinson-Syndrom",
              "Alzheimer-Demenz", "Babinski", "Mini-Mental-Status-Test", "Hypertonie"):
        assert fb.is_medical_term(w), w
    for w in ("Bauer", "Baur", "Flantz", "Herzog", "Bau"):
        assert not fb.is_medical_term(w), w


def test_spans_multi_word() -> None:
    fb = _small()
    text = "Diagnosen: Morbus Parkinson, arterielle Hypertonie; Bauch weich, Herz rhythmisch."
    spans = fb.spans(text)
    parts = [text[a:b] for a, b in spans]
    assert parts == ["Morbus Parkinson", "arterielle Hypertonie", "Bauch", "Herz"]
    assert fb.is_protected(text, text.index("Morbus"), text.index("Morbus") + 6, spans)
    assert fb.is_protected(text, text.index("Bauch"), text.index("Bauch") + 5, spans, whole=True)
    assert not fb.is_protected(text, 0, 9, spans)


def test_base_vocabulary_loaded() -> None:
    fb = terms_module.standard()
    assert fb.count > 20000
    for w in ("Alzheimer", "Hypertonie", "Ramipril", "Lumbalpunktion", "Bauch"):
        assert fb.is_medical_term(w), w
    for w in ("Berlin", "Flantz", "Gotthard", "Dresden", "Müller", "Hildegund", "Briemer"):
        assert not fb.is_medical_term(w), w
    text = "Morbus Parkinson wurde in Berlin diagnostiziert."
    assert [text[a:b] for a, b in fb.spans(text)] == ["Morbus Parkinson"]
    assert fb.is_protected(text, 0, 16, whole=True) and not fb.is_protected(text, 0, 23, whole=True)


def test_fuzzy_search_spares_medical_terms() -> None:
    """Patient Hertz: "Herz" (one edit away) is an organ, "Hertx" is a misread name."""
    p = Patient(surname="Hertz", first_name="Karl", birth_date=dt.date(1950, 1, 2))
    v = PseudonymAssignment(p, "K7Q4MX", secret=bytes(32))
    words = [OCRWord(w, 50 + 60 * i, 100, 100 + 60 * i, 112)
             for i, w in enumerate(["Das", "Herz", "schlägt", "ruhig", "Hertx", "kam"])]
    s = page_text_from_words(words, 0, 595, 842)
    without = {f.text for f in find_fuzzy(s, targets_for(p, v), [])}
    using = {f.text for f in find_fuzzy(s, targets_for(p, v), [], _small())}
    assert "Herz" in without and "Hertx" in without
    assert "Herz" not in using and "Hertx" in using


@pytest.mark.skipif(not ner_module.available(), reason="spaCy oder Modell fehlt")
def test_ner_spares_medical_terms(patient: Patient) -> None:
    from optdoc.pagetext import read_text_layer

    doc = pymupdf.open()
    pdf_page = doc.new_page()
    y = 80
    for line in ("Verdacht auf Morbus Wilson und Morbus Parkinson, Babinski positiv.",
                 "Die Tochter Erika Hoffmann berichtet über Stürze in Dresden."):
        pdf_page.insert_text((56, y), line, fontsize=10.5, fontname="helv")
        y += 14
    page = read_text_layer(pdf_page)
    alone = dataclasses.replace(patient, relatives=[], referrers=[])
    v = PseudonymAssignment(alone, "K7Q4MX", secret=bytes(32))
    spared: list[str] = []
    matches = ner_module.find_ner(page, ner_module.NER(), v, [], "text", terms_module.standard(),
                                  spared)
    texts = {f.text for f in matches}
    assert "Erika Hoffmann" in texts and "Dresden" in texts
    assert not any("Wilson" in t or "Parkinson" in t or "Babinski" in t for t in texts)


def test_import_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    terms_module.standard.cache_clear()
    listing = tmp_path / "eigene.txt"
    listing.write_text("Flantzitis\nMusterkrankheit\n", encoding="utf-8")
    csv = tmp_path / "icd.csv"
    csv.write_text("A00;Cholera;x\nA01;Typhus abdominalis;y\n", encoding="utf-8")
    mesh = tmp_path / "mesh.xml"
    mesh.write_text('<DescriptorRecordSet><DescriptorRecord><DescriptorName><String>Gurkenkrankheit'
                    '</String></DescriptorName><ConceptList><Concept><TermList><Term><String>'
                    'Cucumis-Syndrom</String></Term></TermList></Concept></ConceptList>'
                    '</DescriptorRecord></DescriptorRecordSet>', encoding="utf-8")
    terms_module.import_file(listing, "eigene")
    terms_module.import_file(csv, "icd", "csv", column=1)
    terms_module.import_file(mesh, "mesh", "mesh-xml")
    fb = terms_module.standard()
    for w in ("Flantzitis", "Typhus abdominalis", "Cucumis-Syndrom", "Gurkenkrankheit"):
        assert fb.is_protected(w, 0, len(w), whole=True), w
    assert any(name == "eigene" for name, _n in fb.sources)

    from pseudonymed.cli import app

    r = CliRunner().invoke(app, ["fachbegriffe", "status"])
    assert r.exit_code == 0 and "eigene" in r.output and "vokabular" in r.output
    r = CliRunner().invoke(app, ["fachbegriffe", "pruefen", "Flantzitis", "Flantz",
                                 "Morbus Parkinson"])
    assert "Flantzitis: geschützt" in r.output and "Flantz: frei" in r.output
    assert "Morbus Parkinson: geschützt" in r.output
    r = CliRunner().invoke(app, ["fachbegriffe", "importieren", str(listing), "--name", "zwei"])
    assert r.exit_code == 0 and "0 Begriffe" in r.output  # already there under "eigene"
    r = CliRunner().invoke(app, ["fachbegriffe", "hinzufuegen", "Flantzose", "--kategorie", "x"])
    assert r.exit_code == 0 and "1 neue" in r.output
    assert terms_module.standard().is_medical_term("Flantzose")
    r = CliRunner().invoke(app, ["fachbegriffe", "entfernen", "Flantzose", "Cholera"])
    assert r.exit_code == 0 and "2 Begriffe gelöscht" in r.output
    assert not terms_module.standard().is_medical_term("Flantzose")
    r = CliRunner().invoke(app, ["fachbegriffe", "exportieren", str(tmp_path / "v.csv"),
                                 "--kategorie", "eigene"])
    assert r.exit_code == 0 and (tmp_path / "v.csv").read_text(encoding="utf-8").startswith(
        "begriff,kategorie\nFlantzitis,eigene\n")
    terms_module.standard.cache_clear()


def test_short_words_only_capitalised() -> None:
    fb = terms_module.standard()
    text = "Er hat Schmerzen im Arm und am Ohr; der Test HAT war positiv."
    words = {text[a:b] for a, b in fb.spans(text)}
    assert "hat" not in words and {"Arm", "Ohr", "HAT"} <= words


def test_body_parts_are_medical_terms() -> None:
    """Body parts in everyday German are protected, including the side words, so that
    NER cannot take "Linke Hand" or "Oberbauch" for a person."""
    fb = terms_module.standard()
    for w in ("Kopf", "Nacken", "Oberbauch", "Unterschenkel", "Wade", "Knöchel",
              "Lendenwirbelsäule", "Handfläche", "Schläfe", "Leiste", "Bein", "Hüfte"):
        assert fb.is_medical_term(w), w
    text = "Schmerzen in der linken Hand und im rechten Oberbauch, Knie frei."
    words = {text[a:b] for a, b in fb.spans(text)}
    assert {"linken", "Hand", "rechten", "Oberbauch", "Knie"} <= words
    assert "Körperteile" in [q for q, _n in fb.sources] if hasattr(fb, "sources") else True


def test_finding_terms_are_medical_terms() -> None:
    """Terms of the clinical examination that stand at the start of findings lines."""
    fb = terms_module.standard()
    for w in ("Konzentration", "Haltung", "Nierenlager", "Strichgang", "Gangbild",
              "Merkfähigkeit", "Darmgeräusche", "Procedere", "Finger-Nase-Versuch"):
        assert fb.is_medical_term(w), w
    assert "Befundbegriffe" in [q for q, _n in fb.sources]
    text = "Konzentration: gut. Nierenlager: frei. Erika Hoffmann wohnt in Riesa."
    words = {text[a:b] for a, b in fb.spans(text)}
    assert {"Konzentration", "Nierenlager"} <= words and "Riesa" not in words
