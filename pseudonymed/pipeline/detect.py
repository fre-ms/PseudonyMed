"""The detection over pages that were read: rules, vocabulary marks, identifiers,
medical content, NER and titles on the text layer; on OCR text additionally the fuzzy
search (before the identifiers) and the unreadable lines; codes on the page image; the
user's corrected text as text-only hits."""

from __future__ import annotations

import pymupdf
from optdoc.pagetext import PageText

from pseudonymed.checks.recheck import tolerance_key
from pseudonymed.core.model import Hit
from pseudonymed.detection import codes as codes_module
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import ner as ner_module
from pseudonymed.detection.fuzzy import find_fuzzy
from pseudonymed.detection.rules import find
from pseudonymed.document import optdoc_adapter
from pseudonymed.document import text_file as text_module
from pseudonymed.pipeline.options import PipelineOptions
from pseudonymed.pipeline.pages import PageEntry
from pseudonymed.pipeline.toolkit import Toolkit


def add_matches(page: PageText, matches: list[Hit], source: str, r: Toolkit,
                spared: list[str] | None = None, ner: bool = True) -> None:
    """Identifiers, medical content, NER (when asked for) and titles over ``page``,
    appended to ``matches`` in place and in that order: every stage sees what the ones
    before found, and NER drops the partial hits it completes to a full name."""
    if r.identifiers is not None:
        matches += ident_module.find_identifiers(page, r.identifiers, r.assignment, matches, source)
    if r.medical is not None:
        matches += r.medical.find(page, r.assignment, matches, source)
    if ner and r.ner is not None:
        matches += ner_module.find_ner(page, r.ner, r.assignment, matches, source,
                                       r.medical_terms, spared)
    if r.identifiers is not None:
        matches += ident_module.find_titles(page, r.identifiers, r.assignment, matches, source)


def remember_medical_terms(entry: PageEntry, page: PageText,
                           medical_terms: terms_module.MedicalTerms) -> None:
    """Remember where medical terms sit, for the green marks of the review step."""
    spans = medical_terms.spans(page.text)
    entry.term_count += len(spans)
    entry.term_spans += spans
    for a, b in spans:
        entry.term_rects += [tuple(r) for r in page.rects(a, b)]


def _recognise_text_layer(entry: PageEntry, r: Toolkit, spared: list[str]) -> None:
    """The rules first, then the vocabulary marks and the chain of searches on the text
    layer; a page without one keeps only what the rules found (nothing)."""
    text_layer = entry.text
    entry.hits = find(text_layer, r.rules)
    entry.term_rects, entry.term_spans, entry.term_count = [], [], 0
    if not text_layer.has_text_layer:
        return
    if r.medical_terms is not None:
        remember_medical_terms(entry, text_layer, r.medical_terms)
    add_matches(text_layer, entry.hits, "text", r, spared)


def _unreadable_lines(ocr: PageText, hits: list[Hit]) -> list[Hit]:
    """Lines the OCR could not read, as hits, where no OCR hit covers them already."""
    occupied = [(f.start, f.end) for f in hits if f.source == "ocr"]
    return [u for u in optdoc_adapter.unreadable_matches(ocr)
            if not any(u.start < y and u.end > x for x, y in occupied)]


def _recognise_ocr(entry: PageEntry, ocr: PageText, r: Toolkit, opt: PipelineOptions,
                   spared: list[str]) -> None:
    """The OCR reading: vocabulary marks, exact rules, fuzzy search, then the chain of
    searches over the OCR hits only; unreadable lines and the user's correction last."""
    if r.medical_terms is not None:
        remember_medical_terms(entry, ocr, r.medical_terms)
    exact = find(ocr, r.rules)
    for f in exact:
        f.source = "ocr"
    entry.hits += exact + find_fuzzy(ocr, r.targets, exact, r.medical_terms)
    ocr_matches = [f for f in entry.hits if f.source == "ocr"]
    add_matches(ocr, ocr_matches, "ocr", r, spared)
    entry.hits = [f for f in entry.hits if f.source != "ocr"] + ocr_matches
    if opt.unreadable == "schwaerzen":
        entry.hits += _unreadable_lines(ocr, entry.hits)
    if entry.correction is not None:
        entry.correction_matches = correction_matches(entry, r, opt)


def recognise_pages(doc: pymupdf.Document, pages: list[PageEntry], r: Toolkit,
                    opt: PipelineOptions, spared: list[str]) -> None:
    """Run the detection over pages that were read already; ``spared`` collects the
    NER candidates the vocabulary protected."""
    for entry in pages:
        _recognise_text_layer(entry, r, spared)
        if entry.ocr is not None:
            _recognise_ocr(entry, entry.ocr, r, opt, spared)
        if opt.codes and codes_module.available():
            entry.hits += codes_module.find_codes(doc[entry.number], entry.number)


def correction_matches(entry: PageEntry, r: Toolkit, opt: PipelineOptions) -> list[Hit]:
    """Rules, fuzzy search, identifiers and the chosen medical content over the user's
    corrected OCR text: text-only hits."""
    assert entry.correction is not None
    page = text_module.text_page(entry.correction, entry.number)
    matches = find(page, r.rules)
    if opt.fuzzy_text:
        matches += find_fuzzy(page, r.targets, matches, r.medical_terms, capitalisation=True)
    add_matches(page, matches, "text", r, ner=False)
    for f in matches:
        f.source = "korrektur"
        f.rects = []
    return matches


def discard_remembered(pages: list[PageEntry], remembered: set[tuple[str, str]],
                       notes: list[str]) -> list[Hit]:
    """Move hits the user discarded in an earlier session (same rule and text) out of
    the pages; they come back green and can be restored.  Adds a hint when any were."""
    discarded: list[Hit] = []
    if not remembered:
        return discarded
    for s in pages:
        for f in list(s.hits):
            if f.text and tolerance_key(f.rule, f.text) in remembered:
                s.hits.remove(f)
                discarded.append(f)
    if discarded:
        notes.append(f"{len(discarded)} Treffer nach gespeicherter Entscheidung verworfen"
                     " (grün im Prüfschritt; Wiederherstellen hebt sie auf).")
    return discarded
