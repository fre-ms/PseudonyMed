"""PseudonyMed's adapter to docrepro: its settings as ocrdoc reads them, PseudonyMed's names
of the engines, the settings of docrepro's steps, docrepro's wording and messages in the
language of the user, and the hits for unreadable lines; and docrepro's chain as
PseudonyMed works it."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
from doccor.errors import DocError
from ocrdoc import ocr
from ocrdoc.ocr import OCRWord, page_text_from_words
from ocrdoc.settings import load_settings
from optdoc import cleanup

import pseudonymed.config as config
from pseudonymed.core.text import say
from pseudonymed.document import chain as chain_module
from pseudonymed.document import docrepro_adapter
from pseudonymed.document.chain import DocumentChain
from pseudonymed.pipeline import PipelineOptions, chain_settings


def _settings(monkeypatch: pytest.MonkeyPatch, backend: str, path: str = "",
              languages: str = "") -> None:
    monkeypatch.setattr(config, "load_settings", lambda *a, **k: SimpleNamespace(
        ocr_backend=backend, tesseract_path=path, tesseract_languages=languages))


def test_ocrdoc_reads_pseudonymeds_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """ocrdoc reads PseudonyMed's settings anew on every access, "aus" in its spelling."""
    _settings(monkeypatch, "aus", "/opt/tesseract", "deu+eng")
    s = load_settings()
    assert (s.ocr_backend, s.tesseract_path, s.tesseract_languages) == (
        "none", "/opt/tesseract", "deu+eng")
    assert docrepro_adapter.backend("AUTO") is None and docrepro_adapter.backend("") is None
    _settings(monkeypatch, "Tesseract")
    assert load_settings().ocr_backend == "tesseract"


def test_engines_with_pseudonymeds_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """"aus" is no engine; an unknown or missing engine is worded for the user in both
    languages (ocrdoc's kinds of error)."""
    assert docrepro_adapter.backend("aus") is None and docrepro_adapter.backend("AUS") is None
    with pytest.raises(docrepro_adapter.OCRError) as unknown:
        docrepro_adapter.backend("Zauber")
    assert docrepro_adapter.message(unknown.value) == (
        "Unbekannte Texterkennung: zauber (auto, rapidocr, tesseract, none).")
    assert docrepro_adapter.message(unknown.value, "en_gb") == (
        "Unknown OCR engine: zauber (auto, rapidocr, tesseract, none).")
    monkeypatch.setattr(ocr, "tesseract_program", lambda *_a, **_k: None)
    with pytest.raises(docrepro_adapter.OCRError) as missing:
        docrepro_adapter.backend("tesseract")
    assert missing.value.kind == "no-reader"
    assert "nicht installiert" in docrepro_adapter.message(missing.value)


def test_settings_of_the_steps() -> None:
    """PseudonyMed's options become the settings of optdoc and ocrdoc."""
    assert docrepro_adapter.chain_settings() == {
        "clean_up": True, "reader": "auto", "languages": None, "tables": True, "dpi": 300}
    opt = PipelineOptions(ocr="aus", ocr_languages="deu+eng", ocr_tables=False, clean_up="aus")
    assert chain_settings(opt) == {"clean_up": False, "reader": "none", "languages": "deu+eng",
                                   "tables": False, "dpi": 300}


def test_description_follows_the_language() -> None:
    plan = cleanup.CleanupPlan(rotation=90)
    assert docrepro_adapter.description(plan) == "um 90° gedreht"
    assert docrepro_adapter.description(plan, "en_gb") == "rotated by 90°"


def test_messages_in_both_languages() -> None:
    """docrepro's messages in the language asked for (they quote no path and no content);
    a message of PseudonyMed's in its own two languages; any other as it is."""
    e = DocError("unsupported", tool="optdoc", suffix=".xyz")
    assert docrepro_adapter.message(e) == e.message("de") != e.message("en")
    assert docrepro_adapter.message(e, "en") == str(e)
    own = ValueError(say("Leere Auswahl.", "Empty selection."))
    assert docrepro_adapter.message(own) == "Leere Auswahl."
    assert docrepro_adapter.message(own, "en") == "Empty selection."
    assert docrepro_adapter.message(RuntimeError("x")) == "x"


def test_unreadable_line_becomes_a_match() -> None:
    """A line of fragments read with low confidence becomes one hit "[unlesbar]" over the
    whole line; the readable line below it stays."""
    fragments = ["G6ot", "n", "e", "n", "e", "i", "l", "e", "5g"]
    words = [OCRWord(w, 50 + 30 * i, 100, 70 + 30 * i, 112, k)
             for i, (w, k) in enumerate(zip(fragments, [0.87, 0.5, 0.99, 0.9, 0.25, 0.5, 0.47,
                                                           0.97, 0.81], strict=True))]
    words += [OCRWord(w, 50 + 40 * i, 130, 85 + 40 * i, 142, 0.99)
              for i, w in enumerate(["in", "Riesa.", "Flantz,", "berichtet"])]
    s = page_text_from_words(words, 0, 595, 842)
    matches = docrepro_adapter.unreadable_matches(s)
    assert len(matches) == 1
    r = matches[0]
    assert (r.pseudonym, r.source, r.rule, r.page) == ("[unlesbar]", "ocr", "ocr.unlesbar", 0)
    assert s.text[r.start:r.end] == r.text == "G6ot n e n e i l e 5g"
    assert r.rects and r.font_size > 0


# -- docrepro's chain as PseudonyMed works it ------------------------------------------------------
def test_the_chain_of_a_pdf(report_pdf: Path) -> None:
    """A born-digital PDF: typdoc has nothing to do, optdoc and ocrdoc are in use, tagdoc
    (the description) is not; the private folder goes with the chain."""
    chain = DocumentChain.open(report_pdf, chain_settings(PipelineOptions()))
    assert [s.name for s in chain.stages] == ["optdoc", "ocrdoc"]
    assert chain.uses("ocrdoc") and not chain.uses("typdoc") and chain.project("ocrdoc") is None
    assert not chain.current("ocrdoc")
    project = chain.ensure("ocrdoc")
    assert chain.current("ocrdoc") and chain.current("optdoc")
    assert chain.output("optdoc") == chain.output("optdoc")  # written once for what it is
    document = chain.document()
    entries = {s["tool"]: s for s in document["steps"]}
    assert entries["tagdoc"] == {"tool": "tagdoc", "used": False}
    assert entries["ocrdoc"]["config"] == project.config()
    assert chain_module.originals_digest(document) == document["originals"][0]["sha256"]
    assert chain_module.originals_digest({}) is None
    corrected = chain_module.with_correction(document, 0, "neu")
    assert corrected["steps"] != document["steps"]  # a copy: the document is left alone
    assert [s for s in corrected["steps"] if s["tool"] == "ocrdoc"][0][
        "config"]["pages"][0]["correction"] == "neu"
    work = chain.work
    assert work.is_dir()
    chain.close()
    assert not work.exists() and chain.project("ocrdoc") is None


def test_a_person_decides_and_a_step_before_changes(tmp_path: Path, report_pdf: Path) -> None:
    """What a person decided in ocrdoc stays when optdoc changes a page without moving it,
    and the chain says so; another engine starts ocrdoc afresh."""
    from tests.test_ocr import _scan

    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    chain = DocumentChain.open(scan, chain_settings(PipelineOptions(ocr="aus")))
    ocr_project = chain.ensure("ocrdoc")
    ocr_project.set_regions(0, [])  # a person's areas (a correction would need OCR)
    assert chain.put(chain.bring("ocrdoc")) == []  # nothing to bring: all is current
    optdoc_project = chain.project("optdoc")
    plan = optdoc_project.plan(0) or cleanup.CleanupPlan()
    optdoc_project.set_plan(0, dataclasses.replace(plan, contrast=True))
    assert not chain.current("ocrdoc")
    notes = chain.put(chain.bring("ocrdoc"))
    assert notes and "bleibt" in notes[0] and notes[0].en.endswith("stays.")
    assert chain.project("ocrdoc").regions_by_person(0)
    chain.reset("ocrdoc", {"reader": "tesseract"})
    assert chain.project("ocrdoc") is None and chain.chain.settings["reader"] == "tesseract"
    assert chain.ensure("ocrdoc").options.reader == "tesseract"
    chain.close()


def test_a_chain_document_of_another_original(tmp_path: Path, report_pdf: Path) -> None:
    """A chain document belongs to its original: another one is refused."""
    chain = DocumentChain.open(report_pdf, chain_settings(PipelineOptions()))
    document = chain.document()
    chain.close()
    other = tmp_path / "anders.pdf"
    other.write_bytes(report_pdf.read_bytes() + b"\n")
    with pytest.raises(DocError) as refused:
        DocumentChain.open(other, document=document)
    assert refused.value.kind == "config-mismatch"
