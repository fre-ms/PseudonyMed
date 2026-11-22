"""Phase one and two for text files (transcripts, notes, subtitles, e-mails): the
analysis copy, the spans the user marks, the replaced text and its check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pseudonymed.checks.recheck import (
    checked_from,
    in_pseudonym,
    pseudonym_spans,
    tolerance_key,
)
from pseudonymed.checks.report import create_report
from pseudonymed.core.model import Hit, Kind, Patient, Recheck, RunLog
from pseudonymed.core.text import say
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import number_words
from pseudonymed.detection.fuzzy import Target, find_fuzzy
from pseudonymed.detection.rules import Rule, find
from pseudonymed.document import text_file as text_module
from pseudonymed.pipeline.detect import add_matches, discard_remembered, remember_medical_terms
from pseudonymed.pipeline.options import PipelineOptions, Result, target_path
from pseudonymed.pipeline.output import (
    pseudonyms_of,
    remember_decisions,
    residual_risk,
    write_capsule,
    write_report,
    write_vocabulary,
)
from pseudonymed.pipeline.pages import PageEntry, trim_span
from pseudonymed.pipeline.preparation import PreparationBase
from pseudonymed.pipeline.toolkit import Toolkit, analysis_notes
from pseudonymed.pseudonyms.store import PseudonymStore


@dataclass(kw_only=True)
class TextPreparation(PreparationBase):
    """Phase one for a text file (transcript, note, subtitles).

    Same surface as :class:`Preparation` as far as a user interface needs it:
    ``hits``, ``discard``, ``tolerated``, ``warnings``; instead of
    boxes the user marks spans (``mark``).  The hits' offsets refer to the
    analysis copy, which has the same length as ``text``.
    """

    is_text: ClassVar[bool] = True

    text: str
    output_extension: str = ".txt"  # the input's own for text and subtitles, .txt for JSON/e-mail

    @property
    def hits(self) -> list[Hit]:
        """Every hit, in text order."""
        return sorted((f for s in self.pages for f in s.hits), key=lambda f: f.start)

    def medical_term_spans(self, number: int) -> list[tuple[int, int]]:
        """Offsets of the medical terms, not where a hit lies."""
        occupied = [(f.start, f.end) for f in self.pages[number].hits]
        return [(a, b) for a, b in self.pages[number].term_spans
                if not any(a < y and b > x for x, y in occupied)]

    def mark(self, number: int, start: int, end: int,
             pseudonym: str = "[geschwärzt]") -> Hit:
        """Mark ``[start, ende)`` by hand; hits it overlaps are replaced by it."""
        page = self.pages[number]
        text = page.text.text  # the analysis copy, same length as ``self.text``
        start, end = trim_span(text, start, end)
        page.hits = [f for f in page.hits
                     if not (start < f.end and end > f.start)]
        f = Hit(page=number, start=start, end=end, text=self.text[start:end],
                kind=Kind.MANUAL, rule="manuell", pseudonym=pseudonym, source="manuell")
        page.hits.append(f)
        self.version += 1
        return f

    def take_up(self, number: int, start: int, end: int,
                pseudonym: str | None = None) -> Hit:
        """Like :meth:`mark`, with a code of its own by default so that the
        re-identification can put the text back."""
        text = self.pages[number].text.text
        start, end = trim_span(text, start, end)
        if pseudonym is None:
            pseudonym = self.assignment.identifier("Begriff", self.text[start:end], Kind.TERM)
        f = self.mark(number, start, end, pseudonym)
        f.kind, f.rule = Kind.TERM, "aufnahme"
        return f

    def close(self) -> None:
        """Release the pseudonym store when it is ours."""
        if self.own_store:
            self.store.close()


def _recognise_text(entry: PageEntry, r: Toolkit, opt: PipelineOptions,
                    spared: list[str]) -> None:
    """Rules, vocabulary marks, fuzzy search, identifiers, medical content, NER and
    titles over the analysis copy, in that order: a misspelt known name must get the
    patient's pseudonym, not a NER code."""
    page = entry.text
    entry.hits = find(page, r.rules)
    if r.medical_terms is not None:
        remember_medical_terms(entry, page, r.medical_terms)
    if opt.fuzzy_text:
        entry.hits += find_fuzzy(page, r.targets, entry.hits, r.medical_terms,
                                 capitalisation=True, source="unscharf")
    add_matches(page, entry.hits, "text", r, spared)


def _fuzzy_note(hits: list[Hit]) -> list[str]:
    """How many hits the fuzzy search contributed, worth a look in the review."""
    fuzzy_count = sum(1 for f in hits if f.source == "unscharf")
    if not fuzzy_count:
        return []
    return [say(f"Unscharfe Suche: {fuzzy_count} abweichend geschriebene Treffer"
                " (Transkript, Tippfehler); im Prüfschritt ansehen.",
                f"Fuzzy search: {fuzzy_count} hit(s) written differently (transcript, typing"
                " errors); look at them in the review.")]


def prepare_text(
    source: Path, patient: Patient, opt: PipelineOptions, store: PseudonymStore,
    own_store: bool, r: Toolkit,
) -> TextPreparation:
    """Phase one over one text file.  Number words are rewritten as digits first; hits
    touching them are widened to the words."""
    text, extension = text_module.read_source(source)
    norm = number_words.normalise(text_module.analysis_text(text, extension))
    entry = PageEntry(0, text_module.text_page(norm.text))
    spared: list[str] = []
    _recognise_text(entry, r, opt, spared)
    entry.hits = widened_to_number_words(entry.hits, norm)
    notes = analysis_notes(r, entry.hits, spared)
    notes += _fuzzy_note(entry.hits)
    discarded = discard_remembered([entry], store.discarded_for(patient), notes)
    return TextPreparation(
        source=source, patient=patient, opt=opt, store=store,
        own_store=own_store, code=r.code, assignment=r.assignment,
        rules=r.rules, targets=r.targets, text=text, pages=[entry], output_extension=extension,
        warnings=list(r.warnings), notes=notes, discarded=discarded,
    )


def widened_to_number_words(hits: list[Hit],
                            norm: number_words.Normalised) -> list[Hit]:
    """A hit on "21. Juli 1959" that stands for "einundzwanzigsten Juli neunzehnhundert-
    neunundfünfzig" must cover all those words; after widening, overlapping hits are
    reduced to the longest."""
    for f in hits:
        if f.end > f.start:
            a, b = norm.widen(f.start, f.end)
            if (a, b) != (f.start, f.end):
                f.start, f.end, f.text = a, b, norm.text[a:b]
    occupied: list[tuple[int, int]] = []
    kept: list[Hit] = []
    for f in sorted(hits, key=lambda f: (-(f.end - f.start), f.start)):
        if f.end > f.start and any(f.start < y and f.end > x for x, y in occupied):
            continue
        occupied.append((f.start, f.end))
        kept.append(f)
    return sorted(kept, key=lambda f: f.start)


def recheck_text(
    text: str, extension: str, rules: list[Rule], targets: list[Target] | None = None,
    medical_terms: terms_module.MedicalTerms | None = None,
    tolerated: set[tuple[str, str]] | None = None, pseudonyms: list[str] | None = None,
) -> Recheck:
    """Re-run the rules (and, with ``targets``, the fuzzy search) over a pseudonymised text."""
    tolerated = tolerated or set()
    analysis_text = number_words.normalise(text_module.analysis_text(text, extension)).text
    page = text_module.text_page(analysis_text)
    spans = pseudonym_spans(analysis_text, pseudonyms or [])
    exact = find(page, rules)
    rest = list(exact)
    if targets is not None:
        rest += find_fuzzy(page, targets, exact, medical_terms, capitalisation=True,
                           source="unscharf")
    g = Recheck(checked=checked_from(rules))
    for f in rest:
        if in_pseudonym(f.start, f.end, spans):
            continue
        if tolerance_key(f.rule, f.text) in tolerated:
            g.kept_on_purpose.append(f.rule)
            continue
        g.residual_matches.append(f.without_clear_text())
    return g


def recheck_text_file(path: str | Path, rules: list[Rule],
                      pseudonyms: list[str] | None = None) -> Recheck:
    """``pseudonymed pruefen`` for a text file: the rules alone, nothing tolerated; inside
    ``pseudonyms`` (at least the patient's code) no rule counts."""
    text, extension = text_module.read_source(path)
    return recheck_text(text, extension, rules, pseudonyms=pseudonyms)


def _recheck_output(v: TextPreparation, output: str,
                    hits: list[Hit]) -> Recheck:
    """The check of the replaced text with everything the preparation tolerates."""
    terms = terms_module.standard() if v.opt.medical_terms else None
    return recheck_text(output, v.output_extension, v.rules,
                        v.targets if v.opt.fuzzy_text else None, terms,
                        v.tolerated, pseudonyms_of(v, hits))


def _write_text_output(v: TextPreparation, result: Result, output: str,
                       notes: list[str]) -> None:
    """Keep the output: the text, a Markdown copy when asked for, vocabulary, capsule."""
    target = target_path(v.source, v.opt, v.output_extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output, encoding="utf-8")
    result.target_txt = target
    if v.opt.markdown and target.suffix.lower() != ".md":
        result.target_md = target_path(v.source, v.opt, ".md")
        result.target_md.write_text(output, encoding="utf-8")
    write_vocabulary(v, result, notes)
    write_capsule(v, result, notes)


def finalise_text(v: TextPreparation, final: bool = True) -> Result:
    """Phase two for a text file: replace, check, write, report.  The caller closes
    the preparation."""
    opt, source = v.opt, v.source
    warnings, notes = list(v.warnings), list(v.notes)
    hits = v.hits
    v.assignment.register_matches(hits)
    output = text_module.replace(v.text, hits)
    recheck = _recheck_output(v, output, hits)
    v.store.remember(v.patient, v.assignment)
    if final:
        remember_decisions(v)
    result = Result(source=source, report=None, hits=hits,  # type: ignore[arg-type]
                    code=v.code, text=output, is_text=True)
    result.residual_risk = residual_risk(v, output)
    if recheck.passed:
        _write_text_output(v, result, output, notes)
    else:
        warnings.append(say("Nicht freigegeben: keine Ausgabe geschrieben.",
                            "Not released: no output written."))
    log = RunLog(opt.as_dict(), warnings, notes, result.residual_risk.as_dict())
    result.report = create_report(source, result.target_txt, 1, True, hits,
                                  recheck, log)
    write_report(result, opt)
    return result
