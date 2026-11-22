"""Phase two for PDFs and images (redact, clean, save, check, report) and the one-call
entry points of the pipeline."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pymupdf
from optdoc import cleanup as cleanup_module
from optdoc.alignment import text_layer_from_correction
from optdoc.pagetext import PageText

from pseudonymed.checks.preview import write_preview
from pseudonymed.checks.recheck import (
    check,
    in_pseudonym,
    pseudonym_spans_tolerant,
    tolerance_key,
)
from pseudonymed.checks.report import create_report
from pseudonymed.core.model import Hit, Patient, Recheck, RunLog
from pseudonymed.core.text import say
from pseudonymed.detection import codes as codes_module
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection.fuzzy import find_fuzzy
from pseudonymed.detection.rules import find
from pseudonymed.document import text_file as text_module
from pseudonymed.pipeline.options import PipelineOptions, Result, target_path
from pseudonymed.pipeline.output import (
    pseudonyms_of,
    remember_decisions,
    residual_risk,
    write_capsule,
    write_report,
    write_vocabulary,
)
from pseudonymed.pipeline.pages import PageEntry, markdown_from_text, text_with_pseudonyms
from pseudonymed.pipeline.preparation import Preparation, analyse
from pseudonymed.pipeline.reading import read
from pseudonymed.pipeline.text_files import TextPreparation, finalise_text, prepare_text
from pseudonymed.pipeline.toolkit import toolkit
from pseudonymed.pseudonyms.store import PseudonymStore
from pseudonymed.redaction.pdf import (
    RedactionOptions,
    invisible_text_layer,
    rasterise_document,
    rasterise_page,
    redact_page,
    sanitise,
    save,
)


def _correction_layer(s: PageEntry, rasterised: bool) -> tuple[PageText, list[Hit]]:
    """The corrected (or restructured) text, every word on the box of the OCR word it
    came from, so that selecting it in a reader covers the image word; on a rasterised
    page the hits get those boxes too."""
    assert s.ocr is not None and s.correction is not None
    layer = text_layer_from_correction(s.correction, s.ocr)
    matches = s.correction_matches
    if rasterised:
        matches = [dataclasses.replace(f, rects=[tuple(r) for r in layer.rects(f.start, f.end)])
                   for f in matches]
    return layer, matches


def _embed_text_layer(doc: pymupdf.Document, pages: list[PageEntry], rasterised: set[int],
                      opt: RedactionOptions) -> None:
    """Invisible text for pages that have none of their own: OCR pages get their OCR
    words; rasterised pages get their text-layer words and their pseudonyms as well."""
    for s in pages:
        page = doc[s.number]
        with_pseudonyms = s.number in rasterised
        if with_pseudonyms:
            invisible_text_layer(page, s.text, s.hits, with_pseudonyms=True, opt=opt)
        if s.ocr is None:
            continue
        if s.correction is not None:
            layer, matches = _correction_layer(s, with_pseudonyms)
        else:
            layer = s.ocr
            matches = [f for f in s.hits if f.source in ("ocr", "manuell")]
        invisible_text_layer(page, layer, matches, with_pseudonyms=with_pseudonyms, opt=opt)


def _redact_pages(doc: pymupdf.Document, original: pymupdf.Document, pages: list[PageEntry],
                  opt: RedactionOptions, warnings: list[str]) -> set[int]:
    """Redact every page; a page whose text layout shifted is rasterised from the
    original instead.  Returns the numbers of the rasterised pages."""
    rasterised: set[int] = set()
    for s in pages:
        layout = redact_page(doc[s.number], s.text, s.hits, opt)
        if not layout.layout_intact:
            rasterise_page(doc, s.number, original, s.text, s.hits, opt)
            rasterised.add(s.number)
            warnings.append(say(f"Seite {s.number + 1}: Textlayout nach der Redaktion verschoben,"
                                " Seite deshalb gerastert.",
                                f"Page {s.number + 1}: the text layout shifted after the redaction,"
                                " so the page was rasterised."))
    return rasterised


def _rasterised_output(doc: pymupdf.Document, pages: list[PageEntry], opt: RedactionOptions,
                       notes: list[str]) -> pymupdf.Document:
    """The output as page images, with the invisible text layer of every page when
    asked for."""
    output = rasterise_document(doc, opt.dpi)
    if opt.text_layer:
        _embed_text_layer(output, pages, {s.number for s in pages}, opt)
    sanitise(output, notice=opt.notice)
    notes.append(say("Ausgabe gerastert.", "Output rasterised."))
    return output


def _write_pdf(v: Preparation, target: Path, warnings: list[str],
               notes: list[str]) -> None:
    """Redact copies of the documents, clean them, rasterise when asked, save to ``target``."""
    opt = v.opt.redaction
    doc = pymupdf.open("pdf", v.doc.tobytes())
    original = pymupdf.open("pdf", v.original.tobytes())
    try:
        rasterised = _redact_pages(doc, original, v.pages, opt, warnings)
        if opt.text_layer and not opt.rasterise:
            _embed_text_layer(doc, v.pages, rasterised, opt)
        sanitise(doc, notice=opt.notice)
        output = _rasterised_output(doc, v.pages, opt, notes) if opt.rasterise else doc
        target.parent.mkdir(parents=True, exist_ok=True)
        save(output, target)
        if output is not doc:
            output.close()
    finally:
        doc.close()
        original.close()


def _ocr_residual_matches(page: pymupdf.Page, s: PageEntry, v: Preparation, recheck: Recheck,
                          pseudonyms: list[str]) -> None:
    """Read the redacted page again and record every identifier still legible, unless it
    sits inside a pseudonym or the user tolerated it."""
    assert v.backend is not None
    after = s.read_ocr(page, v.backend)
    spans = pseudonym_spans_tolerant(after.text, pseudonyms)
    exact = find(after, v.rules)
    terms = terms_module.standard() if v.opt.medical_terms else None
    tolerated = v.tolerated
    for f in exact + find_fuzzy(after, v.targets, exact, terms):
        if in_pseudonym(f.start, f.end, spans):
            continue
        if tolerance_key(f.rule, f.text) in tolerated:
            if not v.opt.redaction.text_layer:  # else the text layer counted it
                recheck.kept_on_purpose.append(f.rule)
            continue
        entry = f.without_clear_text()
        entry["quelle"] = "ocr"
        recheck.residual_matches.append(entry)


def _recheck_images(doc: pymupdf.Document, v: Preparation, recheck: Recheck,
                    pseudonyms: list[str]) -> None:
    """OCR the output pages that were read by OCR and look for leftovers (exact and
    fuzzy); re-scan codes."""
    for s in v.pages:
        if s.number >= len(doc):
            continue
        if s.ocr is not None and v.backend is not None:
            _ocr_residual_matches(doc[s.number], s, v, recheck, pseudonyms)
        if v.opt.codes and codes_module.available():
            for f in codes_module.find_codes(doc[s.number], s.number):
                recheck.residual_matches.append(f.without_clear_text())


def _verify_output(v: Preparation, target: Path, text: str,
                   pseudonyms: list[str]) -> tuple[Recheck, int, list[Path]]:
    """Open the written file and check it: text search, OCR of the OCR pages, codes;
    the preview images when asked for.  Returns the check, the page count, the previews."""
    preview: list[Path] = []
    with pymupdf.open(target) as control:
        recheck = check(control, v.rules, extra_texts=[text] if v.opt.text_file else [],
                        tolerated=v.tolerated, pseudonyms=pseudonyms)
        _recheck_images(control, v, recheck, pseudonyms)
        page_count = len(control)
        if v.opt.preview == "immer" or (v.opt.preview == "auto" and v.ocr_pages):
            preview = write_preview(control, target_path(v.source, v.opt, "_vorschau"))
    return recheck, page_count, preview


def _release(v: Preparation, result: Result, target: Path, text: str,
             notes: list[str]) -> None:
    """Keep the output: the PDF, the text and the Markdown when asked for, vocabulary,
    capsule."""
    opt = v.opt
    result.target_pdf = target
    if opt.text_file:
        result.target_txt = target_path(v.source, opt, ".txt")
        result.target_txt.write_text(text, encoding="utf-8")
    if opt.markdown:
        result.target_md = target_path(v.source, opt, ".md")
        result.target_md.write_text(markdown_from_text(text), encoding="utf-8")
    write_vocabulary(v, result, notes)
    write_capsule(v, result, notes)


def _report_options(v: Preparation) -> dict[str, object]:
    """The options for the report, with the clean-up applied per page (1-based)."""
    options = v.opt.as_dict()
    if v.cleaned_up:
        options["aufbereitung"] = {str(n + 1): a.as_dict() for n, a in v.cleaned_up.items()}
    return options


def _finalise_pdf(v: Preparation, final: bool) -> Result:
    """Phase two for a PDF or image: redact, clean, save, check, report.  The caller
    closes the preparation."""
    opt, source = v.opt, v.source
    warnings, notes = list(v.warnings), list(v.notes)
    hits = v.hits + v.correction_matches
    v.assignment.register_matches(hits)
    text = text_with_pseudonyms(v.pages, opt.structured)
    target = target_path(source, opt)
    _write_pdf(v, target, warnings, notes)
    pseudonyms = pseudonyms_of(v, hits)
    recheck, page_count, preview = _verify_output(v, target, text, pseudonyms)
    v.store.remember(v.patient, v.assignment)
    if final:
        remember_decisions(v)
    result = Result(source=source, report=None, hits=hits,  # type: ignore[arg-type]
                    code=v.code, text=text, preview=preview)
    result.residual_risk = residual_risk(v, text)
    if v.without_text:
        recheck.remarks.append(say("Seiten ohne Textebene konnten nicht geprüft werden",
                                   "Pages without a text layer could not be checked"))
    if recheck.passed and not v.without_text:
        _release(v, result, target, text, notes)
    else:
        target.unlink(missing_ok=True)
        warnings.append(say("Nicht freigegeben: Ausgabe verworfen.",
                            "Not released: output discarded."))
    log = RunLog(_report_options(v), warnings, notes,
                 result.residual_risk.as_dict())
    result.report = create_report(source, result.target_pdf, page_count,
                                  not v.ocr_pages and not v.without_text, hits,
                                  recheck, log)
    write_report(result, opt)
    return result


def finalise(v: Preparation | TextPreparation, final: bool = True) -> Result:
    """Phase two: redact, clean, save, check, report.

    Works on copies of the documents, so it can run more than once: the desktop
    runs it as the check before the export and again for the export itself.  With
    ``final`` the preparation is closed afterwards and the discard decisions
    are stored when the options ask for it.
    """
    if v.finalised:
        raise RuntimeError(say("Diese Vorbereitung wurde bereits abgeschlossen.",
                               "This preparation has been finished already."))
    try:
        if isinstance(v, TextPreparation):
            return finalise_text(v, final)
        return _finalise_pdf(v, final)
    finally:
        if final:
            v.finalised = True
            v.close()


def prepare(
    path: str | Path,
    patient: Patient,
    opt: PipelineOptions | None = None,
    store: PseudonymStore | None = None,
    cleanup: dict[int, cleanup_module.CleanupPlan] | None = None,
) -> Preparation | TextPreparation:
    """Phase one of :func:`pseudonymise`: read the file and find the hits.

    PDFs and images yield a :class:`Preparation` (reading and detection in one go,
    see :func:`read` and :func:`analyse` for the two halves), text files
    (``.txt``, ``.md``, ``.srt``, ``.vtt``, ``.json``, ``.eml``) a
    :class:`TextPreparation`.  ``cleanup`` (per page, 0-based) replaces
    image-only pages by their cleaned-up image before OCR; without it the suggestion
    is applied when ``opt.clean_up`` is "auto".
    """
    opt = opt or PipelineOptions()
    source = Path(path)
    if text_module.is_text_file(source):
        own_store = store is None
        store = store or PseudonymStore()
        try:
            r = toolkit(patient, opt, store)
            return prepare_text(source, patient, opt, store, own_store, r)
        except Exception:
            if own_store:
                store.close()
            raise
    reading = read(source, opt, cleanup)
    try:
        return analyse(reading, patient, opt, store)
    except Exception:
        reading.close()
        raise


def pseudonymise(
    path: str | Path,
    patient: Patient,
    opt: PipelineOptions | None = None,
    store: PseudonymStore | None = None,
) -> Result:
    """Pseudonymise one file in one go.  The output is only kept when the check passes."""
    return finalise(prepare(path, patient, opt, store))
