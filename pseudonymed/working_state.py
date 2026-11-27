"""The working state of a document: everything up to the visual review, saved and reloaded.

Preparing a scan, reading it (OCR, corrections) and going through the review take time
and judgement.  The working state keeps all of it, so that another pseudonymisation
variant of the same document (other options, other hits kept or discarded) does not start
from scratch.  It is two files next to each other:

* ``<name>.arbeitsstand.<ending>``: the original as it came (``.pdf``, ``.jpg``, ``.heic``
  …), byte for byte; for a text file the text (``.txt``, ``.srt`` …);
* ``<name>.arbeitsstand.json``: the patient, the options, docrepro's chain document (the
  configuration documents of typdoc, optdoc and ocrdoc: how the pages were bound and
  prepared, the words OCR read, the person's corrections and areas), the hits with their
  pseudonyms, the discarded hits, the manual boxes and marks.

The original and the chain document give the prepared document and its reading again,
byte for byte, without running OCR again (:mod:`pseudonymed.document.chain`); the hits
refer to that reading.  Both files hold clear data and are to be kept like the original.
Dropping either of them on the documents step restores the review: patient, reading and
hits are filled in, the review can change the selection, and a new analysis keeps the
reading and the decisions (:func:`adopt_decisions`).  A state saved in recipient mode
(one-time key) cannot be restored, so it is refused, and so is a state of a format that
was superseded (version 1, which kept PseudonyMed's own clean-up plans and OCR words).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ocrdoc.pagetext import PageText

from pseudonymed import TOOL
from pseudonymed.checks.recheck import tolerance_key
from pseudonymed.core.model import Hit, Patient
from pseudonymed.core.stored import as_stored, field_values
from pseudonymed.core.text import in_language, say
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.document import chain as chain_module
from pseudonymed.document.chain import DocumentChain
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
from pseudonymed.pipeline.reading import read_chain
from pseudonymed.pseudonyms.store import PseudonymStore
from pseudonymed.redaction.pdf import RedactionOptions

FORMAT_MARK = "pseudonymed_arbeitsstand"
VERSION = 2  # 2: docrepro's chain document and the original, in place of plans and OCR words
INFIX = ".arbeitsstand"
EXTENSION = INFIX + ".json"
_OUTER_OPTIONS = ("recipient", "recipient_fingerprint", "matching", "language")


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
        """The original (or the text) next to the JSON file; None for an in-memory capture."""
        if self.path is None:
            return None
        return self.path.parent / str(self.data["dokument"]["datei"])

    @property
    def chain(self) -> dict[str, Any]:
        """docrepro's chain document of a PDF or image (empty for a text file)."""
        found = self.data.get("kette")
        return found if isinstance(found, dict) else {}


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


def _page_as_dict(s: PageEntry, is_text: bool) -> dict[str, Any]:
    """A page as JSON data: its hits and the hits over its corrected text, and for a text
    file the analysis copy of the text (a PDF's pages are read again from the chain)."""
    page: dict[str, Any] = {"nummer": s.number}
    if is_text:
        page["text"] = s.text.as_dict()
    page["fundstellen"] = [f.as_dict() for f in s.hits]
    page["korrektur_treffer"] = [f.as_dict() for f in s.correction_matches]
    return page


def _captured_document(v: Preparation | TextPreparation) -> tuple[bytes, str]:
    """The bytes the state keeps beside its JSON and their ending: the text of a text file;
    the original of a PDF or image, as the chain document names it."""
    if isinstance(v, TextPreparation):
        return v.text.encode("utf-8"), v.output_extension
    if hashlib.sha256(v.original_data).hexdigest() != chain_module.originals_digest(v.chain):
        raise WorkingStateError(say(
            "Das Original passt nicht zur Kette von docrepro; der Arbeitsstand lässt sich"
            " nicht speichern.",
            "The original does not fit docrepro's chain; the working state cannot be saved."))
    return v.original_data, v.original_suffix


def capture(v: Preparation | TextPreparation) -> WorkingState:
    """Capture a preparation as it stands (nothing is written)."""
    if v.opt.recipient:
        raise WorkingStateError(say(
            "Im Empfängermodus (Einmalschlüssel) lässt sich kein Arbeitsstand speichern:"
            " die Codes wären beim Wiederaufnehmen nicht mehr dieselben.",
            "A working state cannot be saved in recipient mode (one-time key): the codes"
            " would no longer be the same when resuming."))
    if v.finalised:
        raise WorkingStateError(say("Diese Vorbereitung ist schon abgeschlossen.",
                                    "This preparation has been finished already."))
    document, extension = _captured_document(v)
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
        "kette": None if isinstance(v, TextPreparation) else v.chain,
        "seiten": [_page_as_dict(s, v.is_text) for s in v.pages],
        "ohne_text": list(v.without_text),
        "verworfen": [f.as_dict() for f in v.discarded],
        "wiederhergestellt": [list(k) for k in sorted(v.restored)],
        "warnungen": list(v.warnings),
        "hinweise": list(v.notes),
        # the same messages in English, so that a resumed review can still show them so
        "warnungen_en": [in_language(w, "en") for w in v.warnings],
        "hinweise_en": [in_language(h, "en") for h in v.notes],
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
def _check_version(path: Path, data: dict[str, Any]) -> None:
    """Refuse a state of another version: a newer PseudonyMed's, or one of a format that was
    superseded and is no longer read (no backward compatibility)."""
    try:
        version = int(data[FORMAT_MARK])
    except (TypeError, ValueError) as e:
        raise WorkingStateError(say(f"{path.name}: kein Arbeitsstand von PseudonyMed.",
                                    f"{path.name}: not a working state of PseudonyMed.")) from e
    if version > VERSION:
        raise WorkingStateError(say(f"{path.name}: stammt von einem neueren PseudonyMed.",
                                    f"{path.name}: comes from a newer PseudonyMed."))
    if version < VERSION:
        raise WorkingStateError(say(
            f"{path.name}: Arbeitsstand im abgelösten Format {version} (eigene Aufbereitungspläne"
            " und OCR-Wörter, vor docrepro); er wird nicht mehr gelesen. Bitte das Original"
            " erneut öffnen, aufbereiten und prüfen.",
            f"{path.name}: working state in the superseded format {version} (PseudonyMed's own"
            " clean-up plans and OCR words, before docrepro); it is no longer read. Please open"
            " the original again, prepare and review it."))


def read(path: str | Path) -> WorkingState:
    """Read a state from its JSON file and the document next to it; the document's
    checksum has to match."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WorkingStateError(say(f"{path.name}: kein lesbarer Arbeitsstand ({e}).",
                                    f"{path.name}: not a readable working state ({e}).")) from e
    if not isinstance(data, dict) or FORMAT_MARK not in data:
        raise WorkingStateError(say(f"{path.name}: kein Arbeitsstand von PseudonyMed.",
                                    f"{path.name}: not a working state of PseudonyMed."))
    _check_version(path, data)
    document = path.parent / str(data["dokument"]["datei"])
    try:
        content = document.read_bytes()
    except OSError as e:
        raise WorkingStateError(
            say(f"Das Dokument {document.name} fehlt neben {path.name}.",
                f"The document {document.name} is missing next to {path.name}.")) from e
    if hashlib.sha256(content).hexdigest() != data["dokument"]["sha256"]:
        raise WorkingStateError(
            say(f"{document.name} wurde seit dem Speichern verändert; der Arbeitsstand passt nicht"
                " mehr dazu.",
                f"{document.name} has changed since it was saved; the working state no longer"
                " fits it."))
    return WorkingState(data, content, path)


def _text_page_from(d: dict[str, Any]) -> PageEntry:
    """A page of a text file as it was analysed, without its hits."""
    return PageEntry(int(d["nummer"]), PageText.from_dict(d["text"]))


def _saved(a: WorkingState, key: str) -> list[str]:
    """The saved messages under ``key``, each with its English form when the state has it."""
    de = [str(x) for x in a.data.get(key, [])]
    en = a.data.get(key + "_en", [])
    if len(en) != len(de):
        return de
    return [say(d, str(e)) for d, e in zip(de, en, strict=True)]


def open_chain(a: WorkingState, parent: Path | None = None) -> DocumentChain:
    """docrepro's chain on the state's original, as its chain document says: it gives the
    prepared document and the reading the state was saved with, byte for byte.  A state
    taken in memory has its original written into the chain's private folder first."""
    if a.is_text:
        raise WorkingStateError(say("Textdateien haben keine Kette von docrepro.",
                                    "Text files have no chain of docrepro."))
    original = a.document_path
    if original is not None:
        return DocumentChain.open(original, document=a.chain, parent=parent)
    folder = Path(tempfile.mkdtemp(prefix="pseudonymed-", dir=parent))
    original = folder / (a.source.stem + str(a.data["dokument"]["endung"]))
    try:
        original.write_bytes(a.document)
        chain = DocumentChain.open(original, document=a.chain, parent=parent)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    chain.folders.append(folder)  # removed with the chain's own
    return chain


def reading_from(a: WorkingState, opt: PipelineOptions | None = None,
                 chain: DocumentChain | None = None) -> Reading:
    """The reading phase rebuilt from a state, nothing searched yet: for a new analysis that
    keeps the preparation, the OCR and the corrections.  docrepro's chain makes it again from
    the original (``chain``, where the caller holds it open, else one opened and closed
    here); OCR does not run again."""
    opt = opt or a.options
    documents = chain or open_chain(a)
    try:
        reading = read_chain(documents, opt, a.source)
    finally:
        if chain is None:
            documents.close()
    reading.notes.append(_load_note(a))
    return reading


def _load_note(a: WorkingState) -> str:
    """The hint naming the state and when, and by which tool version, it was saved."""
    saved, tool = a.data.get("erstellt", "?"), a.data.get("werkzeug", "?")
    if a.path is None:
        return say(f"Arbeitsstand aus dem Speicher übernommen (gespeichert {saved}, {tool}).",
                   f"Working state taken over from memory (saved {saved}, {tool}).")
    return say(f"Arbeitsstand aus {a.path.name} übernommen (gespeichert {saved}, {tool}).",
               f"Working state taken over from {a.path.name} (saved {saved}, {tool}).")


def _preparation_messages(a: WorkingState, r: Toolkit) -> tuple[list[str], list[str]]:
    """The saved warnings and hints without an earlier load hint, the current one added, and
    a warning when this database issues another identifier than the state was saved under."""
    warnings = [w for w in _saved(a, "warnungen") if "Arbeitsstand" not in w]
    notes = [h for h in _saved(a, "hinweise") if "Arbeitsstand" not in h]
    notes.append(_load_note(a))
    if a.code and r.code != a.code:
        warnings.append(say(
            f"Der Arbeitsstand wurde mit der Kennung {a.code} angelegt, diese Datenbank"
            f" vergibt {r.code} (anderer Schlüssel oder andere Datenbank): gespeicherte"
            " Pseudonyme bleiben und werden der Zuordnung hinzugefügt, neue Treffer"
            " bekommen Codes dieser Datenbank.",
            f"The working state was saved under the code {a.code}, this database issues"
            f" {r.code} (another key or another database): saved pseudonyms stay and are"
            " added to the mapping, new hits get codes of this database."))
    return warnings, notes


def _shared_from(a: WorkingState, r: Toolkit, patient: Patient, opt: PipelineOptions,
                 store: PseudonymStore, own_store: bool) -> dict[str, Any]:
    """The keyword fields of :class:`PreparationBase` both preparations take from a
    state, all but ``pages``: whose document, with which options, store and gear, the
    messages, and what the review had decided (discards, restores, version)."""
    warnings, notes = _preparation_messages(a, r)
    return {
        "source": a.source, "patient": patient, "opt": opt, "store": store,
        "own_store": own_store, "code": r.code, "assignment": r.assignment,
        "rules": r.rules, "targets": r.targets, "warnings": warnings, "notes": notes,
        "discarded": [Hit.from_dict(f) for f in a.data.get("verworfen", [])],
        "version": int(a.data.get("version", 0)),
        "restored": {(str(k[0]), str(k[1])) for k in a.data.get("wiederhergestellt", [])},
    }


def _text_preparation_from(a: WorkingState, shared: dict[str, Any]) -> TextPreparation:
    """The text variant: the saved clear text, its output extension, and pages that have no
    PDF page behind them."""
    return TextPreparation(
        **shared, text=a.document.decode("utf-8"),
        pages=[_text_page_from(d) for d in a.data["seiten"]],
        output_extension=str(a.data["dokument"].get("ausgabe_endung", ".txt")))


def _pdf_preparation_from(a: WorkingState, shared: dict[str, Any],
                          chain: DocumentChain | None) -> Preparation:
    """The PDF variant: pages and documents come from the reading docrepro's chain makes
    again, which is thereby used up."""
    reading = reading_from(a, shared["opt"], chain)
    reading.used_up = True
    return Preparation(**shared, backend=reading.backend, doc=reading.doc,
                       original=reading.original, pages=reading.pages,
                       without_text=reading.without_text, cleaned_up=reading.plan,
                       chain=reading.chain, original_data=reading.original_data,
                       original_suffix=reading.original_suffix)


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
                     opt: PipelineOptions | None = None,
                     chain: DocumentChain | None = None) -> Preparation | TextPreparation:
    """The preparation as it was saved: pages, hits, discards and marks, ready for the
    review, without searching anything again (the pages are made again by docrepro's chain,
    ``chain`` where the caller holds it open).  ``opt`` replaces the saved options (the
    detection options have no effect here, the output options do)."""
    opt = opt or a.options
    patient = a.patient
    own_store = store is None
    store = store or PseudonymStore()
    try:
        r = toolkit(patient, opt, store)
        shared = _shared_from(a, r, patient, opt, store, own_store)
        v: Preparation | TextPreparation = (
            _text_preparation_from(a, shared) if a.is_text
            else _pdf_preparation_from(a, shared, chain))
        if len(v.pages) != len(a.data["seiten"]):
            v.close()
            raise WorkingStateError(say("Die Seiten passen nicht mehr zum Arbeitsstand.",
                                        "The pages no longer fit the working state."))
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


def _prepared_pages(chain: dict[str, Any]) -> list[Any]:
    """The prepared pages as ocrdoc's part of a chain document names them: every page's
    size, frame and origin (where optdoc moved it, and what it shows); none without."""
    for entry in chain.get("steps", []):
        config = entry.get("config")
        if entry.get("tool") == "ocrdoc" and isinstance(config, dict):
            pages = config.get("input", {}).get("pages")
            return pages if isinstance(pages, list) else []
    return []


def moved_pages(old: WorkingState, new: Preparation | TextPreparation) -> set[int]:
    """The pages (from 0) of a new analysis that lie otherwise than when the state was
    taken: a step before changed their size, their frame (turned, unwarped, straightened
    anew) or what they show, by docrepro's own rule; a box drawn on them would no longer
    cover what it covered.  None for a text file, or where the chain is the same."""
    before = _prepared_pages(old.chain)
    after = _prepared_pages(getattr(new, "chain", {}) or {})
    if not before and not after:
        return set()
    if len(before) != len(after):
        return set(range(len(new.pages)))
    return {n for n, (a, b) in enumerate(zip(before, after, strict=True)) if a != b}


def _adopt_manual_matches(d: dict[str, Any], new: Preparation | TextPreparation,
                          moved: set[int]) -> int:
    """Re-add the manual hits (boxes, masks, take-ups) of one saved page to the same page
    of the new preparation; returns how many were not there yet.  A page the new
    preparation does not have, or one that lies otherwise now (``moved``), is skipped."""
    number = int(d["nummer"])
    if number >= len(new.pages) or number in moved:
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
    moved = moved_pages(old, new)
    n = sum(_adopt_manual_matches(d, new, moved) for d in old.data["seiten"])
    n += _discard_again(old, new)
    new.notes.append(say(f"{n} Entscheidungen aus dem Arbeitsstand übernommen (Kästen, Masken,"
                         " Markierungen, verworfene Treffer).",
                         f"{n} decisions taken over from the working state (boxes, masks,"
                         " marks, discarded hits)."))
    if moved:
        pages = ", ".join(str(k + 1) for k in sorted(moved))
        new.warnings.append(say(
            f"Seite(n) {pages} liegen nach der Aufbereitung anders als im Arbeitsstand: die"
            " Kästen, Masken und Aufnahmen dort wurden nicht übernommen; bitte in der"
            " Sichtprüfung neu setzen.",
            f"Page(s) {pages} lie otherwise after the preparation than in the working state:"
            " the boxes, masks and take-ups there were not taken over; please set them again"
            " in the review."))
    return n
