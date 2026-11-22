"""The working state of a document: everything up to the visual review, saved and reloaded.

Cleaning up a scan, reading it (OCR, corrections) and going through the review take time
and judgement.  The working state keeps all of it, so that another pseudonymisation
variant of the same document (other options, other hits kept or discarded) does not start
from scratch.  It is two files next to each other:

* ``<name>.arbeitsstand.pdf`` (or ``.txt``, ``.srt`` … for text files): the document as
  prepared, that is after the clean-up and in clear text, **not** redacted;
* ``<name>.arbeitsstand.json``: the patient, the options, the clean-up plan, every page
  (text layer, OCR words with their boxes, the corrected OCR text), the hits with their
  pseudonyms, the discarded hits, the manual boxes and marks.

Both files hold clear data and are to be kept like the original.  Dropping either of them
on the documents step restores the review: patient, reading and hits are filled in, the
review can change the selection, and a new analysis keeps the reading and the decisions
(:func:`adopt_decisions`).  A state saved in recipient mode (one-time key) cannot
be restored, so it is refused.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from optdoc import cleanup as cleanup_module
from optdoc import structure as structure_module
from optdoc.pagetext import PageText

from pseudonymed import TOOL
from pseudonymed.checks.recheck import tolerance_key
from pseudonymed.core.model import Hit, Patient
from pseudonymed.core.stored import as_stored, field_values
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.pipeline import (
    PageEntry,
    PipelineOptions,
    Preparation,
    Reading,
    TextPreparation,
    Toolkit,
    remember_medical_terms,
    toolkit,
)
from pseudonymed.pseudonyms.store import PseudonymStore
from pseudonymed.redaction.pdf import RedactionOptions

FORMAT_MARK = "pseudonymed_arbeitsstand"
VERSION = 1
INFIX = ".arbeitsstand"
EXTENSION = INFIX + ".json"
_OUTER_OPTIONS = ("cleanup", "recipient", "recipient_fingerprint", "matching")


class WorkingStateError(Exception):
    """A state cannot be captured, read or restored; the message says why."""


@dataclass
class WorkingState:
    """A parsed state: the JSON data plus the document bytes."""

    data: dict[str, Any]
    document: bytes
    path: Path | None = None  # the JSON file, None for an in-memory capture
    folder: Path | None = None  # in-memory capture: the folder of the source

    @property
    def is_text(self) -> bool:
        """True for a text file (transcript, note, subtitles), False for a PDF."""
        return bool(self.data["dokument"]["ist_text"])

    @property
    def patient(self) -> Patient:
        """The patient the state was saved for."""
        return Patient.from_dict(self.data["patient"])

    @property
    def code(self) -> str:
        """The identifier the pseudonyms were issued under; empty when the state has none."""
        return str(self.data.get("kennung", ""))

    @property
    def options(self) -> PipelineOptions:
        """The pipeline options as they were saved."""
        return _options_from(self.data["optionen"])

    @property
    def source(self) -> Path:
        """Path the outputs are named after: the original file name in the state's folder
        (an in-memory capture keeps the source's folder)."""
        folder = self.path.parent if self.path is not None else (self.folder or Path.cwd())
        return folder / str(self.data["dokument"]["quelle"])

    @property
    def document_path(self) -> Path | None:
        """The prepared document next to the JSON file; None for an in-memory capture."""
        if self.path is None:
            return None
        return self.path.parent / str(self.data["dokument"]["datei"])


# -- recognising the files -------------------------------------------------------------------------
def is_working_state(path: str | Path) -> Path | None:
    """The JSON file of a working state when ``path`` is that file or the document next
    to it (``x.arbeitsstand.pdf`` beside ``x.arbeitsstand.json``), else None."""
    path = Path(path)
    if path.name.endswith(EXTENSION):
        try:
            with path.open("rb") as f:
                return path if FORMAT_MARK.encode() in f.read(4096) else None
        except OSError:
            return None
    name = path.name
    if INFIX + "." in name:
        stem = name[:name.index(INFIX)]
        candidate = path.with_name(stem + EXTENSION)
        if candidate.is_file():
            return is_working_state(candidate)
    return None


# -- capturing -------------------------------------------------------------------------------------
def _options_as_dict(opt: PipelineOptions) -> dict[str, Any]:
    """The options as JSON data: the target folder as a string, the redaction options
    nested, the recipient and matching fields left out (they are not restored)."""
    keys = PipelineOptions.STORED_KEYS
    d = {keys.get(k, k): v for k, v in opt.__dict__.items() if k not in _OUTER_OPTIONS}
    d["ziel_ordner"] = str(opt.target_folder) if opt.target_folder else None
    d["schwaerzen"] = as_stored(opt.redaction)
    return d


def _options_from(d: dict[str, Any]) -> PipelineOptions:
    """The options read back: unknown keys and the outer fields are ignored, the
    colours become tuples again."""
    fields = {k: v for k, v in field_values(PipelineOptions, d).items() if k not in _OUTER_OPTIONS}
    fields["target_folder"] = Path(d["ziel_ordner"]) if d.get("ziel_ordner") else None
    redaction_data = dict(d.get("schwaerzen", {}))
    for colour in ("fuellfarbe", "textfarbe"):
        if colour in redaction_data:
            redaction_data[colour] = tuple(redaction_data[colour])
    fields["redaction"] = RedactionOptions(**field_values(RedactionOptions, redaction_data))
    return PipelineOptions(**fields)


def _page_as_dict(s: PageEntry) -> dict[str, Any]:
    """A page as JSON data: text layer, OCR reading, correction, resolution and hits."""
    return {
        "nummer": s.number, "text": s.text.as_dict(),
        "ocr": s.ocr.as_dict() if s.ocr is not None else None,
        "korrektur": s.correction, "ocr_dpi": s.ocr_dpi, "ocr_max_pixel": s.ocr_max_pixels,
        "fundstellen": [f.as_dict() for f in s.hits],
        "korrektur_treffer": [f.as_dict() for f in s.correction_matches],
    }


def capture(v: Preparation | TextPreparation) -> WorkingState:
    """Capture a preparation as it stands (nothing is written)."""
    if v.opt.recipient:
        raise WorkingStateError(
            "Im Empfängermodus (Einmalschlüssel) lässt sich kein Arbeitsstand speichern:"
            " die Codes wären beim Wiederaufnehmen nicht mehr dieselben.")
    if v.finalised:
        raise WorkingStateError("Diese Vorbereitung ist schon abgeschlossen.")
    if isinstance(v, TextPreparation):
        document = v.text.encode("utf-8")
        extension = v.output_extension
    else:
        document = v.doc.tobytes()
        extension = ".pdf"
    data: dict[str, Any] = {
        FORMAT_MARK: VERSION,
        "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
        "werkzeug": TOOL,
        "dokument": {"datei": "", "endung": extension,
                     "sha256": hashlib.sha256(document).hexdigest(),
                     "quelle": v.source.name, "ist_text": v.is_text,
                     "ausgabe_endung": getattr(v, "output_extension", ".txt")},
        "patient": v.patient.as_dict(),
        "kennung": v.code,
        "optionen": _options_as_dict(v.opt),
        "aufbereitung": {str(n): a.full_dict() for n, a in v.cleaned_up.items()},
        "seiten": [_page_as_dict(s) for s in v.pages],
        "ohne_text": list(v.without_text),
        "verworfen": [f.as_dict() for f in v.discarded],
        "wiederhergestellt": [list(k) for k in sorted(v.restored)],
        "warnungen": list(v.warnings),
        "hinweise": list(v.notes),
        "version": v.version,
    }
    return WorkingState(data, document, folder=v.source.parent)


def save(v: Preparation | TextPreparation, target: str | Path) -> tuple[Path, Path]:
    """Write the two files; ``target`` is the JSON file (``….arbeitsstand.json``), a file name
    with another extension, or a folder (existing, or without an extension: created and the
    files named after the source).  Returns (document, json)."""
    a = capture(v)
    target = Path(target)
    if target.is_dir() or (not target.suffix and not target.exists()):
        target = target / (v.source.stem + EXTENSION)
    elif not target.name.endswith(EXTENSION):
        name = target.name
        if INFIX + "." in name:
            name = name[:name.index(INFIX)]
        elif "." in name:
            name = name[:name.rindex(".")]
        target = target.with_name(name + EXTENSION)
    stem = target.name[:-len(EXTENSION)]
    document = target.with_name(stem + INFIX + a.data["dokument"]["endung"])
    a.data["dokument"]["datei"] = document.name
    target.parent.mkdir(parents=True, exist_ok=True)
    document.write_bytes(a.document)
    target.write_text(json.dumps(a.data, ensure_ascii=False, indent=1), encoding="utf-8")
    for p in (document, target):
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return document, target


# -- reading ---------------------------------------------------------------------------------------
def read(path: str | Path) -> WorkingState:
    """Read a state from its JSON file and the document next to it; the document's
    checksum has to match."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WorkingStateError(f"{path.name}: kein lesbarer Arbeitsstand ({e}).") from e
    if not isinstance(data, dict) or FORMAT_MARK not in data:
        raise WorkingStateError(f"{path.name}: kein Arbeitsstand von PseudonyMed.")
    if int(data[FORMAT_MARK]) > VERSION:
        raise WorkingStateError(f"{path.name}: stammt von einem neueren PseudonyMed.")
    document = path.parent / str(data["dokument"]["datei"])
    try:
        content = document.read_bytes()
    except OSError as e:
        raise WorkingStateError(
            f"Das Dokument {document.name} fehlt neben {path.name}.") from e
    if hashlib.sha256(content).hexdigest() != data["dokument"]["sha256"]:
        raise WorkingStateError(
            f"{document.name} wurde seit dem Speichern verändert; der Arbeitsstand passt nicht"
            " mehr dazu.")
    return WorkingState(data, content, path)


def _page_from(d: dict[str, Any], plan: cleanup_module.CleanupPlan | None,
               page: pymupdf.Page | None) -> PageEntry:
    """A page as it was read, without its hits: text layer, OCR reading, correction and
    resolution.  The letter-aware finish follows the clean-up plan; the layout roles are
    rebuilt from the PDF page when there is an OCR reading to place them on."""
    s = PageEntry(int(d["nummer"]), PageText.from_dict(d["text"]))
    s.ocr = PageText.from_dict(d["ocr"]) if d.get("ocr") else None
    s.correction = d.get("korrektur")
    s.ocr_dpi = int(d.get("ocr_dpi", s.ocr_dpi))
    s.ocr_max_pixels = int(d.get("ocr_max_pixel", s.ocr_max_pixels))
    if plan is not None and plan.letter_mode:
        s.ocr_filter = functools.partial(cleanup_module.letter_finish,
                                         factor=max(1, plan.upscale))
    if s.ocr is not None and page is not None:
        s.structure = structure_module.analyse_page_structure(page, s.ocr)
    return s


def _plan_from(data: dict[str, Any]) -> dict[int, cleanup_module.CleanupPlan]:
    """The clean-up plan read back, keyed by page number."""
    return {int(n): cleanup_module.CleanupPlan.from_dict(a)
            for n, a in data.get("aufbereitung", {}).items()}


def _pdf_pages_from(pages: list[dict[str, Any]], plan: dict[int, cleanup_module.CleanupPlan],
                    doc: pymupdf.Document) -> list[PageEntry]:
    """The saved pages of a PDF, each rebuilt against its page in the opened document."""
    return [_page_from(d, plan.get(int(d["nummer"])), doc[int(d["nummer"])]) for d in pages]


def _reading_messages(a: WorkingState) -> tuple[list[str], list[str]]:
    """The warnings and hints a rebuilt reading keeps: those about the text layer, the OCR
    and the clean-up, plus where the state came from."""
    warnings = [w for w in a.data.get("warnungen", []) if "Textebene" in w]
    notes = [h for h in a.data.get("hinweise", []) if "OCR" in h or "aufbereitet" in h]
    notes.append(_load_note(a))
    return warnings, notes


def reading_from(a: WorkingState, opt: PipelineOptions | None = None) -> Reading:
    """The reading phase rebuilt from a state, nothing searched yet: for a new
    analysis that keeps the clean-up, the OCR and the corrections."""
    if a.is_text:
        raise WorkingStateError("Textdateien haben keine Lesung; vorbereiten() aufrufen.")
    opt = opt or a.options
    plan = _plan_from(a.data)
    doc = pymupdf.open("pdf", a.document)
    original = pymupdf.open("pdf", a.document)
    try:
        pages = _pdf_pages_from(a.data["seiten"], plan, doc)
    except Exception:
        doc.close()
        original.close()
        raise
    warnings, notes = _reading_messages(a)
    return Reading(source=a.source, opt=opt, doc=doc, original=original, backend=None,
                   pages=pages, without_text=[int(n) for n in a.data.get("ohne_text", [])],
                   plan=plan, warnings=warnings, notes=notes)


def _load_note(a: WorkingState) -> str:
    """The hint naming the state and when, and by which tool version, it was saved."""
    name = a.path.name if a.path is not None else "dem Speicher"
    return (f"Arbeitsstand aus {name} übernommen (gespeichert {a.data.get('erstellt', '?')},"
            f" {a.data.get('werkzeug', '?')}).")


def _preparation_messages(a: WorkingState, r: Toolkit) -> tuple[list[str], list[str]]:
    """The saved warnings and hints without an earlier load hint, the current one added, and
    a warning when this database issues another identifier than the state was saved under."""
    warnings = [w for w in a.data.get("warnungen", []) if "Arbeitsstand" not in w]
    notes = [h for h in a.data.get("hinweise", []) if "Arbeitsstand" not in h]
    notes.append(_load_note(a))
    if a.code and r.code != a.code:
        warnings.append(
            f"Der Arbeitsstand wurde mit der Kennung {a.code} angelegt, diese Datenbank"
            f" vergibt {r.code} (anderer Schlüssel oder andere Datenbank): gespeicherte"
            " Pseudonyme bleiben und werden der Zuordnung hinzugefügt, neue Treffer"
            " bekommen Codes dieser Datenbank.")
    return warnings, notes


def _shared_from(a: WorkingState, r: Toolkit, patient: Patient, opt: PipelineOptions,
                 store: PseudonymStore, own_store: bool) -> dict[str, Any]:
    """The keyword fields of :class:`PreparationBase` both preparations take from a
    state, all but ``pages``: whose document, with which options, store and gear, the
    messages, and what the review had decided (discards, restores, version, plan)."""
    warnings, notes = _preparation_messages(a, r)
    return {
        "source": a.source, "patient": patient, "opt": opt, "store": store,
        "own_store": own_store, "code": r.code, "assignment": r.assignment,
        "rules": r.rules, "targets": r.targets, "warnings": warnings, "notes": notes,
        "discarded": [Hit.from_dict(f) for f in a.data.get("verworfen", [])],
        "version": int(a.data.get("version", 0)),
        "restored": {(str(k[0]), str(k[1])) for k in a.data.get("wiederhergestellt", [])},
        "cleaned_up": _plan_from(a.data),
    }


def _text_preparation_from(a: WorkingState, shared: dict[str, Any]) -> TextPreparation:
    """The text variant: the saved clear text, its output extension, and pages that have no
    PDF page behind them."""
    return TextPreparation(
        **shared, text=a.document.decode("utf-8"),
        pages=[_page_from(d, None, None) for d in a.data["seiten"]],
        output_extension=str(a.data["dokument"].get("ausgabe_endung", ".txt")))


def _pdf_preparation_from(a: WorkingState, shared: dict[str, Any]) -> Preparation:
    """The PDF variant: pages and documents come from a rebuilt reading, which is thereby
    used up."""
    reading = reading_from(a, shared["opt"])
    reading.used_up = True
    return Preparation(**shared, backend=None, doc=reading.doc, original=reading.original,
                       pages=reading.pages, without_text=reading.without_text)


def _restore_page(s: PageEntry, d: dict[str, Any], is_text: bool,
                  medical_terms: terms_module.MedicalTerms | None) -> None:
    """Put the saved hits and correction hits back on a restored page and mark the medical
    terms afresh: on the text layer where the page has one (always for a text file) and on
    the OCR reading."""
    s.hits = [Hit.from_dict(f) for f in d.get("fundstellen", [])]
    s.correction_matches = [Hit.from_dict(f) for f in d.get("korrektur_treffer", [])]
    s.term_rects, s.term_spans, s.term_count = [], [], 0
    if medical_terms is None:
        return
    if is_text or s.text.has_text_layer:
        remember_medical_terms(s, s.text, medical_terms)
    if s.ocr is not None:
        remember_medical_terms(s, s.ocr, medical_terms)


def preparation_from(a: WorkingState, store: PseudonymStore | None = None,
                     opt: PipelineOptions | None = None) -> Preparation | TextPreparation:
    """The preparation as it was saved: pages, hits, discards and marks, ready for the
    review, without reading or searching anything again.  ``opt`` replaces the saved
    options (the detection options have no effect here, the output options do)."""
    opt = opt or a.options
    patient = a.patient
    own_store = store is None
    store = store or PseudonymStore()
    try:
        r = toolkit(patient, opt, store)
        shared = _shared_from(a, r, patient, opt, store, own_store)
        v: Preparation | TextPreparation = (
            _text_preparation_from(a, shared) if a.is_text
            else _pdf_preparation_from(a, shared))
        for s, d in zip(v.pages, a.data["seiten"], strict=True):
            _restore_page(s, d, v.is_text, r.medical_terms)
    except Exception:
        if own_store:
            store.close()
        raise
    return v


# -- a new analysis that keeps the decisions -------------------------------------------------------
def _spans_overlap(f: Hit, g: Hit) -> bool:
    """True when the two hits share at least one character of the page text."""
    return f.start < g.end and f.end > g.start


def _already_marked(f: Hit, hits: list[Hit]) -> bool:
    """True when the same manual hit (same boxes and span) is already among the hits."""
    return any(g.source == "manuell" and g.rects == f.rects and g.start == f.start
               and g.end == f.end for g in hits)


def _mark_again(page: PageEntry, f: Hit) -> None:
    """Add a saved manual hit to the page.  A mark with a span replaces what the analysis
    found underneath it; the other manual hits stay."""
    if f.end > f.start:
        page.hits = [g for g in page.hits
                     if g.source == "manuell" or not _spans_overlap(f, g)]
    page.hits.append(f)


def _adopt_manual_matches(d: dict[str, Any], new: Preparation | TextPreparation) -> int:
    """Re-add the manual hits (boxes, masks, take-ups) of one saved page to the same page
    of the new preparation; returns how many were not there yet.  A page the new
    preparation does not have is skipped."""
    number = int(d["nummer"])
    if number >= len(new.pages):
        return 0
    page = new.pages[number]
    n = 0
    for f in (Hit.from_dict(x) for x in d.get("fundstellen", [])):
        if f.source != "manuell" or _already_marked(f, page.hits):
            continue
        _mark_again(page, f)
        n += 1
    return n


def _discard_again(old: WorkingState, new: Preparation | TextPreparation) -> int:
    """Discard again every hit of the new preparation whose rule and text the saved state
    had discarded; returns how many."""
    discarded = {tolerance_key(str(f["regel"]), str(f.get("text", "")))
                 for f in old.data.get("verworfen", []) if f.get("text")}
    matches = [f for f in new.hits if tolerance_key(f.rule, f.text) in discarded]
    for f in matches:
        new.discard(f)
    return len(matches)


def adopt_decisions(old: WorkingState, new: Preparation | TextPreparation) -> int:
    """After a new analysis: put the manual boxes, masks and marks of the saved state
    back and discard again what was discarded (same rule and text).  Returns how many
    hits were taken over or discarded."""
    n = sum(_adopt_manual_matches(d, new) for d in old.data["seiten"])
    n += _discard_again(old, new)
    new.notes.append(f"{n} Entscheidungen aus dem Arbeitsstand übernommen (Kästen, Masken,"
                     " Markierungen, verworfene Treffer).")
    return n
