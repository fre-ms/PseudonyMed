"""State of one desktop session, kept free of Qt so that it can be tested on its own."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from optdoc import cleanup as cleanup_module

from pseudonymed import working_state as state_mod
from pseudonymed.checks import residual_risk as risk_module
from pseudonymed.core.model import Patient
from pseudonymed.core.stored import field_values
from pseudonymed.core.text import Text, say
from pseudonymed.document.text_file import TEXT_EXTENSIONS, is_text_file
from pseudonymed.pipeline import (
    OpenedDocument,
    PipelineOptions,
    Preparation,
    Reading,
    Result,
    TextPreparation,
    analyse,
    finalise,
    load,
    prepare,
    read,
    remember_decisions,
)
from pseudonymed.pseudonyms.pgp import Certificate
from pseudonymed.pseudonyms.store import PseudonymStore, UncertainMatch, one_time_store


def _ocr_notice(pages: list[int]) -> Text:
    """The ", OCR auf Seite(n) …" suffix of a state line; empty when no page needed OCR."""
    if not pages:
        return say("", "")
    listed = ", ".join(map(str, pages))
    return say(f", OCR auf Seite(n) {listed}", f", OCR on page(s) {listed}")


def _state_analysed(v: Preparation | TextPreparation) -> str:
    """State line of an analysed document: pages, hits and the OCR pages; a text file
    has no pages to count."""
    if v.is_text:
        return say(f"Text, {len(v.hits)} Treffer", f"Text, {len(v.hits)} hit(s)")
    ocr = _ocr_notice(v.ocr_pages)
    return say(f"{len(v.pages)} Seite(n), {len(v.hits)} Treffer{ocr}",
               f"{len(v.pages)} page(s), {len(v.hits)} hit(s){ocr.en}")


def _state_loaded(opened: OpenedDocument) -> str:
    """State line of an opened document: its pages and how many lack a text layer."""
    n = len(opened.candidates)
    if n:
        return say(f"{opened.page_count} Seite(n), {n} ohne Textebene (aufbereiten)",
                   f"{opened.page_count} page(s), {n} without text layer (clean up)")
    return say(f"{opened.page_count} Seite(n), geladen", f"{opened.page_count} page(s), loaded")


@dataclass
class Document:
    """One document of the session with everything it has reached so far: the opened
    template and its clean-up plan, the reading, the preparation, the pre-check, the
    result; ``error`` holds the message of the step that failed."""

    path: Path
    opened: OpenedDocument | None = None  # phase zero: open, pages known, clean-up suggested
    cleanup: dict[int, cleanup_module.CleanupPlan] = field(default_factory=dict)
    reading: Reading | None = None  # reading phase: text layer and OCR, correctable
    preparation: Preparation | TextPreparation | None = None
    precheck: Result | None = None  # the re-check run into a scratch folder
    precheck_version: int = -1  # review state the pre-check belongs to
    result: Result | None = None
    error: str = ""
    working_state: Path | None = None  # the JSON file the document was restored from

    @property
    def text_source(self) -> Path:
        """The text file a new analysis of a text document reads: the saved one of a
        working state, else the file itself."""
        if self.working_state is not None:
            return state_mod.read(self.working_state).document_path or self.path
        return self.path

    @property
    def needs_cleanup(self) -> bool:
        """True when the opened document has pages without a text layer to clean up."""
        return self.opened is not None and bool(self.opened.candidates)

    @property
    def readable(self) -> bool:
        """PDFs and images go through the reading step; text files do not."""
        return not is_text_file(self.path)

    @property
    def ready_to_check(self) -> bool:
        """True while an analysed document waits for its export: the preparation is open."""
        return self.preparation is not None and not self.preparation.finalised

    @property
    def precheck_current(self) -> bool:
        """True when the pre-check was run on the review state as it is now."""
        v = self.preparation
        if v is None or self.precheck is None:
            return False
        return self.ready_to_check and self.precheck_version == v.version

    @property
    def state(self) -> str:
        """One line for the document list: the error, else the furthest phase reached."""
        if self.error:
            return say(f"Fehler: {self.error}", f"Error: {self.error}")
        if self.result is not None:
            return (say("freigegeben", "released") if self.result.released
                    else say("nicht freigegeben", "not released"))
        if self.preparation is not None:
            return _state_analysed(self.preparation)
        if self.reading is not None:
            ocr = _ocr_notice(self.reading.ocr_pages)
            return say(f"{self.reading.page_count} Seite(n) gelesen{ocr}",
                       f"{self.reading.page_count} page(s) read{ocr.en}")
        if self.opened is not None:
            return _state_loaded(self.opened)
        return say("noch nicht analysiert", "not analysed yet")


def _pending_preparation(d: Document) -> Preparation | TextPreparation:
    """The open preparation of an analysed document; ValueError for any other state."""
    if d.preparation is None or not d.ready_to_check:
        raise ValueError(say("Dokument ist nicht analysiert.", "The document is not analysed."))
    return d.preparation


def _capture_state(d: Document) -> state_mod.WorkingState | None:
    """Capture the review of a document analysed already and close its preparation;
    None for a fresh document, and in recipient mode, where no state can be captured
    and the file is read again."""
    v = d.preparation
    if v is None or v.finalised:
        return None
    old: state_mod.WorkingState | None
    try:
        old = state_mod.capture(v)
    except state_mod.WorkingStateError:
        old = None  # recipient mode: read again from the file
    v.close()
    return old


def _fresh_analysis(d: Document, patient: Patient, opt: PipelineOptions,
                    store: PseudonymStore) -> Preparation | TextPreparation:
    """Phase one of a document not analysed before: over its reading when the user went
    through the text-recognition step, else from the file with the clean-up plan."""
    if d.reading is not None and not d.reading.used_up:
        return analyse(d.reading, patient, opt, store=store)
    cleanup = dict(d.cleanup) if d.opened is not None else None
    return prepare(d.path, patient, opt, store=store, cleanup=cleanup)


def _analysis_from_state(d: Document, old: state_mod.WorkingState, patient: Patient,
                         opt: PipelineOptions, store: PseudonymStore,
                         ) -> Preparation | TextPreparation:
    """Phase one over a saved working state: a text file is read again from its saved
    text, a PDF or image from the saved reading, so clean-up, OCR and corrections stay."""
    if old.is_text:
        return prepare(d.text_source, patient, opt, store=store)
    reading = state_mod.reading_from(old, opt)
    try:
        return analyse(reading, patient, opt, store=store)
    except Exception:
        reading.close()
        raise


_OUTPUT_FIELDS = ("target_pdf", "target_txt", "target_md", "target_report", "target_vocabulary",
                  "target_capsule")


def _copy_outputs(source: Result, target_folder: Path) -> Result:
    """Copy the files of a passed pre-check into the target folder and describe the
    copies as a result of their own (the residual risk is attached by the caller)."""
    target_folder.mkdir(parents=True, exist_ok=True)
    copy = Result(source=source.source, report=source.report, hits=source.hits,
                  code=source.code, text=source.text, is_text=source.is_text)
    for field_name in _OUTPUT_FIELDS:
        path = getattr(source, field_name)
        if path is not None:
            new = target_folder / path.name
            shutil.copy2(path, new)
            setattr(copy, field_name, new)
    for image in source.preview:
        new_folder = target_folder / image.parent.name
        new_folder.mkdir(exist_ok=True)
        shutil.copy2(image, new_folder / image.name)
        copy.preview.append(new_folder / image.name)
    return copy


_TRAFFIC_LIGHT_GREEN = ("gruen",
                        say("Erneute Prüfung bestanden: nichts mehr gefunden, bereit zum Export.",
                            "Re-check passed: nothing found any more, ready for the export."))


def _traffic_light_check_pending(pending: list[Document]) -> tuple[str, str] | None:
    """Grey while a document's pre-check is missing or older than its review."""
    if all(d.precheck_current for d in pending):
        return None
    return "grau", say("Erneute Prüfung steht aus.", "The re-check is still to come.")


def _traffic_light_remaining_matches(pending: list[Document]) -> tuple[str, str] | None:
    """Red while a pre-check still found identifiers: nothing will be exported."""
    red = sum(1 for d in pending if d.precheck is not None and not d.precheck.released)
    if not red:
        return None
    return "rot", say(f"Identifikatoren noch gefunden in {red} Dokument(en): nichts wird"
                      " exportiert.",
                      f"Identifiers still found in {red} document(s): nothing will be exported.")


def _traffic_light_warnings(pending: list[Document]) -> tuple[str, str] | None:
    """Yellow when a passed pre-check carries warnings the user should read first."""
    yellow = sum(1 for d in pending if d.precheck is not None and d.precheck.report.warnings)
    if not yellow:
        return None
    return "gelb", say(f"Erneute Prüfung bestanden; {yellow} Dokument(e) mit Warnungen, bitte"
                       " lesen.",
                       f"Re-check passed; {yellow} document(s) with warnings, please read them.")


@dataclass
class Session:
    """The session behind the desktop: the patient, the documents in their phases, the
    options every step reads and the store the pseudonyms come from.  Nothing in here
    touches Qt; the pages call it from their worker threads."""

    store_path: Path | None = None
    schema: str = "hash"
    patient: Patient | None = None
    options: PipelineOptions = field(default_factory=PipelineOptions)
    documents: list[Document] = field(default_factory=list)
    matching: str | None = None  # decision for an uncertain match, set by the dialog
    recipient: Certificate | None = None  # pseudonyms for someone else: one-time key, capsule
    language: str = "de"  # of the interface: reports and vocabulary files are written in it
    _scratch: Path | None = None
    _one_time: PseudonymStore | None = None

    def store(self) -> PseudonymStore:
        """The local store, or in recipient mode one in-memory store for the whole session
        (the same one-time key for every document, nothing written locally)."""
        if self.recipient is not None:
            if self._one_time is None:
                self._one_time = one_time_store(self.schema)
            # the wrapper answers every call of the store and only ignores close()
            return cast(PseudonymStore, _NonClosing(self._one_time))
        return PseudonymStore(self.store_path, schema=self.schema)

    def set_recipient(self, certificate: Certificate | None) -> None:
        """Switch recipient mode on (a fresh one-time key) or off."""
        self.recipient = certificate
        self._one_time = None
        self.options.recipient = certificate.armor if certificate else ""
        self.options.recipient_fingerprint = certificate.fingerprint if certificate else ""

    def scratch(self) -> Path:
        """Folder for the pre-check output, removed when the session closes."""
        if self._scratch is None or not self._scratch.exists():
            self._scratch = Path(tempfile.mkdtemp(prefix="pseudonymed-"))
        return self._scratch

    def add(self, path: str | Path) -> Document | None:
        """Add a file: a document, or a saved working state by its JSON or the document
        beside it.  None when the type is unknown or the file is in the list already."""
        path = Path(path)
        state = state_mod.is_working_state(path)
        if state is not None:  # a saved working state, by its JSON or the document beside it
            if any(d.path == state for d in self.documents):
                return None
            d = Document(state, working_state=state)
            self.documents.append(d)
            return d
        if path.suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp",
                                       ".heic", ".heif", ".webp", *TEXT_EXTENSIONS}:
            return None
        if any(d.path == path for d in self.documents):
            return None
        d = Document(path)
        self.documents.append(d)
        return d

    def remove(self, d: Document) -> None:
        """Drop a document from the session and release what it holds open."""
        if d.preparation is not None and not d.preparation.finalised:
            d.preparation.close()
        if d.opened is not None:
            d.opened.close()
        if d.reading is not None:
            d.reading.close()
        self.documents.remove(d)

    def read(self, d: Document) -> None:
        """Reading phase for one document with the clean-up the user chose; text files
        have nothing to read."""
        if d.reading is not None:
            d.reading.close()
        d.reading, d.preparation, d.result, d.error = None, None, None, ""
        if not d.readable:
            return
        try:
            d.reading = read(d.path, self.options, dict(d.cleanup) if d.opened else None)
        except Exception as e:
            d.error = str(e)
            raise

    def load(self, d: Document) -> None:
        """Phase zero for one document: open it and suggest the clean-up of its
        image-only pages.  Text files have nothing to clean up."""
        if d.opened is not None:
            d.opened.close()
        d.opened, d.cleanup, d.preparation, d.result, d.error = None, {}, None, None, ""
        d.reading, d.precheck = None, None
        if d.working_state is not None:
            self._load_working_state(d)
            return
        if is_text_file(d.path):
            return
        try:
            d.opened = load(d.path, self.options)
        except Exception as e:
            d.error = str(e)
            raise
        d.cleanup = {n: cleanup_module.CleanupPlan(**{**a.__dict__, "masks": list(a.masks)})
                     for n, a in d.opened.suggestions.items()}

    def _load_working_state(self, d: Document) -> None:
        """Restore a saved working state: the review is filled in, the patient taken over."""
        assert d.working_state is not None
        if self.recipient is not None:
            d.error = say("Im Empfängermodus lässt sich kein Arbeitsstand laden.",
                          "A working state cannot be loaded in recipient mode.")
            raise ValueError(d.error)
        try:
            a = state_mod.read(d.working_state)
            opt = a.options
            opt.matching = self.matching
            opt.remember_discarded = self.options.remember_discarded
            store = self.store()
            try:
                d.preparation = state_mod.preparation_from(a, store=store, opt=opt)
            except Exception:
                store.close()
                raise
        except Exception as e:
            d.error = str(e)
            raise
        d.preparation.own_store = True
        if self.patient is None or self.patient.key() != a.patient.key():
            self.patient = a.patient

    def analyse(self, d: Document) -> None:
        """Phase one for one document; raises UncertainMatch for the dialog.  A document
        analysed already is analysed again on its saved reading (clean-up, OCR and
        corrections stay), and the boxes, masks, marks and discards are taken over."""
        if self.patient is None:
            raise ValueError(say("Kein Patient gewählt.", "No patient chosen."))
        patient = self.patient
        old = _capture_state(d)
        d.preparation, d.result, d.error = None, None, ""
        opt = PipelineOptions(**{**self.options.__dict__, "matching": self.matching,
                                 "language": self.language})
        store = self.store()
        try:
            if old is None:
                new = _fresh_analysis(d, patient, opt, store)
            else:
                new = _analysis_from_state(d, old, patient, opt, store)
            d.preparation = new
            if old is not None:
                state_mod.adopt_decisions(old, new)
        except UncertainMatch:
            store.close()
            raise
        except Exception as e:
            store.close()
            d.error = str(e)
            raise
        new.own_store = True  # the preparation owns and closes it
        d.reading = None  # its documents now belong to the preparation
        if d.opened is not None:
            d.opened.close()
            d.opened = None

    def _options_for(self, v: Preparation | TextPreparation,
                     target_folder: Path | None) -> PipelineOptions:
        """The options for finishing ``v``: the session's, with the match decision, the
        date form the analysis used and the folder the files go to."""
        return PipelineOptions(**{**self.options.__dict__, "matching": self.matching,
                                 "language": self.language, "date_format": v.opt.date_format,
                                 "target_folder": target_folder})

    def check(self, d: Document) -> Result:
        """The whole finishing phase into a scratch folder: redaction, re-check, report.
        Nothing is exported; the result tells whether the export may happen."""
        v = _pending_preparation(d)
        folder = self.scratch() / str(id(d))
        if folder.exists():
            shutil.rmtree(folder)
        v.opt = self._options_for(v, folder)
        d.precheck = finalise(v, final=False)
        d.precheck_version = v.version
        return d.precheck

    def export(self, d: Document) -> Result:
        """Export: copy the checked files when the pre-check is current, otherwise run
        the finishing phase again into the target.  A failed re-check exports nothing."""
        v = _pending_preparation(d)
        if d.precheck_current and d.precheck is not None:
            return self._export_precheck(d, v, d.precheck)
        v.opt = self._options_for(v, self.options.target_folder)
        d.result = finalise(v, final=True)
        self._remember_residual_risk(d)
        return d.result

    def _export_precheck(self, d: Document, v: Preparation | TextPreparation,
                         source: Result) -> Result:
        """Export from a current pre-check.  A failed one becomes the result and only
        closes the preparation (decisions stored, nothing written); a passed one has its
        files copied to the target and the mapping stored as the final run would."""
        if not source.released:
            d.result = source
            v.opt = self._options_for(v, self.options.target_folder)
            finalise(v, final=True)  # stores decisions, closes
            return d.result
        copy = _copy_outputs(source, self.options.target_folder or d.path.parent)
        v.opt = self._options_for(v, self.options.target_folder)
        self._finish_without_output(v)
        copy.residual_risk = source.residual_risk
        d.result = copy
        self._remember_residual_risk(d)
        return d.result

    def _finish_without_output(self, v: Preparation | TextPreparation) -> None:
        """Close a preparation whose files were copied: store the mapping and the
        discard decisions as the final run would, without redacting again."""
        v.store.remember(v.patient, v.assignment)
        remember_decisions(v)
        v.finalised = True
        v.close()

    def traffic_light(self) -> tuple[str, str]:
        """Traffic light for the export step: "gruen" (re-check clean, ready), "gelb"
        (released with warnings), "rot" (identifiers still found, nothing exported),
        "grau" (not checked yet)."""
        pending = [d for d in self.documents if d.ready_to_check]
        if not pending:
            return self._traffic_light_after_export()
        return (_traffic_light_check_pending(pending) or _traffic_light_remaining_matches(pending)
                or _traffic_light_warnings(pending) or _TRAFFIC_LIGHT_GREEN)

    def _traffic_light_after_export(self) -> tuple[str, str]:
        """Without open documents: green once every finished one was released, else grey."""
        done = [d.result for d in self.documents if d.result is not None]
        if done and all(e.released for e in done):
            return "gruen", say("Exportiert.", "Exported.")
        return "grau", say("Keine analysierten Dokumente.", "No analysed documents.")

    def control(self) -> tuple[risk_module.ResidualRisk | None, int]:
        """All checked documents of the session together with what earlier released
        documents of the same patient revealed; (None, 0) before any check."""
        risks = [d.precheck.residual_risk for d in self.documents
                 if d.precheck is not None and d.precheck.residual_risk is not None]
        if not risks:
            return None, 0
        earlier: list = []
        count = 0
        if self.patient is not None and self.recipient is None:
            try:
                with self.store() as sp:
                    stored, count = sp.earlier_residual_risks(self.patient)
                earlier = [risk_module.Feature(**field_values(risk_module.Feature, m))
                           for m in stored]
            except Exception:  # noqa: BLE001
                earlier, count = [], 0
        return risk_module.merge(risks, earlier, count), count

    def _remember_residual_risk(self, d: Document) -> None:
        """After a release: remember categories and weights for the patient (never clear
        text), so that later documents are judged together with this one."""
        e = d.result
        if e is None or e.residual_risk is None or not e.released or self.patient is None \
                or self.recipient is not None:
            return
        r = e.residual_risk
        try:
            with self.store() as sp:
                sp.remember_residual_risk(self.patient, d.path.name,
                                          [{"schluessel": m.key, "kategorie": m.category,
                                      "beschreibung": m.description, "bits": m.bits}
                                     for m in r.features], r.bits, r.lam)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        """Release every document and remove the pre-check folder."""
        for d in self.documents:
            if d.preparation is not None and not d.preparation.finalised:
                d.preparation.close()
            if d.opened is not None:
                d.opened.close()
            if d.reading is not None:
                d.reading.close()
        if self._scratch is not None and self._scratch.exists():
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


class _NonClosing:
    """The session's one-time store handed out to callers that close what they get:
    closing is a no-op, the store lives as long as the recipient mode does."""

    def __init__(self, store: PseudonymStore) -> None:
        """Wrap the session's one-time store."""
        self._store = store

    def __getattr__(self, name: str) -> Any:
        """Everything but closing is the store's."""
        return getattr(self._store, name)

    def close(self) -> None:
        """Closing is left to the session: the store outlives every single document."""
        return None

    def __enter__(self) -> _NonClosing:
        """Usable as a context manager like the store itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Leaving the context does not close the store either."""
        return None
