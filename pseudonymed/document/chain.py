"""docrepro's chain on one original, worked step by step: PseudonyMed's document layer.

PseudonyMed redacts PDFs, and takes them from docrepro: typdoc binds a photo or an image
into a PDF, optdoc prepares its pages (upright, unwarped, straightened, evenly lit), ocrdoc
recognises them (the text layer, OCR where a page has none, its areas).  A
:class:`DocumentChain` holds docrepro's :class:`~docrepro.chain.Chain` on the original and
opens those steps one after another, as docrepro's own window does: each step on what the
step before gives as it stands, and again whenever that changed, with everything it held,
so that the step's own rule decides what stays of what a person decided in it (a page that
came from the same place and lies as it lay keeps it, :mod:`doccor.origins`).  tagdoc, the
step that describes a document, is not PseudonyMed's business and stays off.

The chain document (:meth:`DocumentChain.document`, format ``docrepro-chain``) holds the
original's SHA-256 and every step's configuration document, nothing else; the words OCR
read and what a person corrected are in ocrdoc's.  The original and the chain document
give the prepared document and its reading again, byte for byte, without running OCR
again: that is what PseudonyMed's working state keeps (:mod:`pseudonymed.working_state`).

Everything the steps write on the way goes into a private folder of the chain's own
(:attr:`DocumentChain.work`), which :meth:`DocumentChain.close` removes.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doccor import documents
from doccor.errors import DocError
from doccor.pictures import is_picture
from docrepro import steps as registry
from docrepro.chain import Chain
from docrepro.errors import DocreproError

from pseudonymed.core.text import Text, say

STEPS = ("typdoc", "optdoc", "ocrdoc")  # docrepro's steps PseudonyMed works with, in order
_FRESH = b"fresh"  # what a step stands on that starts without a configuration document


@dataclass(eq=False)
class Stage:
    """One step of the chain: its step and adapter; its project once opened, on ``given``,
    standing on ``basis`` (what the steps before it were then); the file it last wrote for
    the step after it, named by what it was made from."""

    step: registry.Step
    adapter: Any
    project: Any = None
    given: Path | list[Path] | None = None
    basis: bytes | None = None
    output: tuple[bytes, Path] | None = None

    @property
    def name(self) -> str:
        """The step's tool: "typdoc", "optdoc" or "ocrdoc"."""
        return self.step.name


@dataclass
class Opened:
    """A step opened anew, to be put in place: its project, what it was opened on and
    stands on, the pages that lost a person's decisions, and whether it had to start anew
    (what it held belonged to another document)."""

    stage: Stage
    project: Any
    given: Path | list[Path]
    basis: bytes
    dropped: list[int]
    anew: bool


def _note(stage: Stage, decided: bool, done: Opened) -> list[Text]:
    """What became of a person's decisions in a step opened anew; nothing where there were
    none."""
    if not decided:
        return []
    if done.anew:
        return [say(f"{stage.name} bekommt jetzt andere Seiten und beginnt neu; was darin"
                    " entschieden war, ist verworfen.",
                    f"{stage.name} is handed other pages now and starts anew; what was decided"
                    " in it went.")]
    if done.dropped:
        pages = ", ".join(str(n + 1) for n in done.dropped)
        return [say(f"{stage.name}: Seite(n) {pages} liegen jetzt anders; was dort entschieden"
                    " war, ist verworfen.",
                    f"{stage.name}: page(s) {pages} lie otherwise now; what was decided there"
                    " went.")]
    return [say(f"{stage.name}: ein Schritt davor hat sich geändert; was darin entschieden war,"
                " bleibt.",
                f"{stage.name}: a step before it changed; what was decided in it stays.")]


class DocumentChain:
    """docrepro's chain on one original and the steps PseudonyMed works with, opened one
    after another (see the module).  Open with :meth:`open`, release with :meth:`close`.
    ``work`` is the chain's private folder for what the steps write on the way."""

    def __init__(self, chain: Chain, work: Path) -> None:
        self.chain = chain
        self.work = work
        self.stages = [Stage(step, adapter) for step, adapter in chain.steps
                       if step.name in STEPS and step.name in chain.used]
        self.folders: list[Path] = [work]  # removed on closing: the private folder, and more
        self._round = 0

    @classmethod
    def open(cls, original: str | Path, settings: dict[str, Any] | None = None,
             document: dict[str, Any] | None = None,
             parent: Path | None = None) -> DocumentChain:
        """The chain on ``original``: as the chain document ``document`` says, else fresh
        with the steps' ``settings`` (:func:`pseudonymed.document.docrepro_adapter.
        chain_settings`).  ocrdoc is always in use, and so is typdoc for a picture:
        PseudonyMed reads every page and redacts a PDF.  The private folder is made in
        ``parent`` (else the system's temporary folder).  Raises docrepro's errors: the
        original missing, a chain document of another original ("config-mismatch") or of
        a step that is not installed ("step-missing"), one that makes no sense
        ("bad-config")."""
        without = [step.name for step in registry.STEPS if step.name not in STEPS]
        chain = Chain.open(original, document, settings, without=without)
        chain.used |= {"ocrdoc"} | ({"typdoc"} if is_picture(original) else set())
        installed = {step.name for step, _adapter in chain.steps}
        missing = sorted((chain.used & set(STEPS)) - installed)
        if missing:
            raise DocreproError("step-missing", step=missing[0])
        return cls(chain, Path(tempfile.mkdtemp(prefix="pseudonymed-", dir=parent)))

    # -- what the chain holds ------------------------------------------------------------------
    def stage(self, name: str) -> Stage | None:
        """The stage of a step, where it is in use."""
        return next((s for s in self.stages if s.name == name), None)

    def project(self, name: str) -> Any:
        """A step's project, where it is open."""
        stage = self.stage(name)
        return None if stage is None else stage.project

    def uses(self, name: str) -> bool:
        """True when the step is in use for this document."""
        return self.stage(name) is not None

    def configs(self) -> dict[str, dict[str, Any]]:
        """The configuration documents of the steps: of an open project as it stands, else
        as the chain document had it (a step PseudonyMed does not use keeps its own)."""
        found = dict(self.chain.configs)
        for stage in self.stages:
            if stage.project is not None:
                found[stage.name] = stage.adapter.config(stage.project)
        return found

    def document(self) -> dict[str, Any]:
        """The chain document as the chain stands, every step in use opened first (a step
        in use needs its configuration document)."""
        if self.stages:
            self.ensure(self.stages[-1].name)
        return self.chain.document(self.configs())

    # -- the steps joined ----------------------------------------------------------------------
    def _config_bytes(self, stage: Stage, pending: dict[str, Any]) -> bytes:
        """A step's configuration document as it stands: of its project (the one just
        opened where there is one), else the chain document's, else that it starts fresh."""
        project = pending.get(stage.name, stage.project)
        if project is not None:
            return documents.document_bytes(stage.adapter.config(project))
        held = self.chain.configs.get(stage.name)
        return documents.document_bytes(held) if held is not None else _FRESH

    def _basis(self, stage: Stage, pending: dict[str, Any] | None = None) -> bytes:
        """What a step stands on, named: the originals and the configuration document of
        every step before it (which names its input, so that what it was handed is in it
        as well)."""
        pending = pending or {}
        parts = [str(o).encode() for o in self.chain.originals]
        for earlier in self.stages[:self.stages.index(stage)]:
            parts.append(earlier.name.encode() + b":" + self._config_bytes(earlier, pending))
        return b"\n".join(parts)

    def _given(self, stage: Stage, pending: dict[str, Any]) -> Path | list[Path]:
        """What a step is handed: what the step before it gives, else the original (the
        originals, to the step that binds pictures)."""
        k = self.stages.index(stage)
        if k > 0:
            return self.output(self.stages[k - 1].name, pending)
        if stage.step.takes == "pictures":
            return list(self.chain.originals)
        return self.chain.original

    def output(self, name: str, pending: dict[str, Any] | None = None) -> Path:
        """The file a step gives as it stands, written once for what it was made from (in a
        folder of its own each time, so that an old project can still be closed)."""
        stage = self.stage(name)
        assert stage is not None, name
        pending = pending or {}
        if pending.get(name, stage.project) is None:
            self.put(self.bring(name))
        key = self._basis(stage, pending) + b"\n" + self._config_bytes(stage, pending)
        if stage.output is not None and stage.output[0] == key:
            return stage.output[1]
        self._round += 1
        folder = self.work / name / str(self._round)
        project = pending.get(name, stage.project)
        made: Path = stage.adapter.write(project, folder, self.chain.original.stem,
                                         None)["document"]
        stage.output = (key, made)
        return made

    def current(self, name: str) -> bool:
        """True when the step is open on what the steps before it give now."""
        stage = self.stage(name)
        return stage is not None and stage.project is not None \
            and stage.basis == self._basis(stage)

    def bring(self, name: str) -> list[Opened]:
        """Open every step in use up to ``name`` that is not open on what the steps before
        it give now, in their order, each with what it held (its project's configuration
        document, else the chain document's); a step whose rule refuses what it held starts
        anew.  Nothing is put in place yet (:meth:`put`): a window may do this off its
        thread and put it in place on it."""
        pending: dict[str, Any] = {}
        opened: list[Opened] = []
        for stage in self.stages:
            basis = self._basis(stage, pending)
            if stage.project is None or stage.basis != basis:
                held = (stage.adapter.config(stage.project) if stage.project is not None
                        else self.chain.configs.get(stage.name))
                done = self._open(stage, self._given(stage, pending), held, basis)
                pending[stage.name] = done.project
                opened.append(done)
            if stage.name == name:
                break
        return opened

    def _open(self, stage: Stage, given: Path | list[Path], held: dict[str, Any] | None,
              basis: bytes) -> Opened:
        """A step's project on ``given`` with what it held, else fresh where what it held
        belongs to another document."""
        folder = self.work / stage.name
        anew = False
        try:
            project = self.chain.opened(stage.adapter, given, held, folder)
        except DocError as e:
            if e.kind != "config-mismatch" or held is None:
                raise
            project, anew = self.chain.opened(stage.adapter, given, None, folder), True
        return Opened(stage, project, given, basis, list(stage.adapter.dropped(project)), anew)

    def put(self, opened: list[Opened]) -> list[Text]:
        """Put the steps opened anew in place, their old projects closed; returns what
        became of a person's decisions in them (nothing where there were none)."""
        notes: list[Text] = []
        for done in opened:
            stage, old = done.stage, done.stage.project
            decided = old is not None and registry.by_person(stage.adapter, old)
            stage.project, stage.given, stage.basis = done.project, done.given, done.basis
            if old is not None:
                stage.adapter.close(old)
            notes += _note(stage, decided, done)
        return notes

    def ensure(self, name: str) -> Any:
        """The project of a step, opened (with the steps before it) on what they give now."""
        self.put(self.bring(name))
        return self.project(name)

    def reset(self, name: str, settings: dict[str, Any]) -> None:
        """Start a step afresh with new ``settings`` (another OCR engine): what it held
        goes, and it is opened anew when it is next needed."""
        self.chain.settings.update(settings)
        self.chain.configs.pop(name, None)
        stage = self.stage(name)
        if stage is not None and stage.project is not None:
            stage.adapter.close(stage.project)
            stage.project, stage.basis, stage.output = None, None, None

    def close(self) -> None:
        """Close every project and remove the private folder (and the folders handed to
        the chain with it, :attr:`folders`)."""
        for stage in self.stages:
            if stage.project is not None:
                stage.adapter.close(stage.project)
                stage.project = None
        for folder in self.folders:
            shutil.rmtree(folder, ignore_errors=True)


def with_correction(document: dict[str, Any], number: int, text: str | None) -> dict[str, Any]:
    """A copy of a chain document in which ocrdoc's configuration document holds ``text`` as
    the person's correction of page ``number`` (from 0; None takes it back), as ocrdoc's
    text panel would have kept it."""
    changed = copy.deepcopy(document)
    for entry in changed.get("steps", []):
        config = entry.get("config")
        if entry.get("tool") == "ocrdoc" and isinstance(config, dict):
            config["pages"][number]["correction"] = text
    return changed


def originals_digest(document: dict[str, Any]) -> str | None:
    """The SHA-256 of the (first) original a chain document names."""
    named = document.get("originals")
    if isinstance(named, list) and named and isinstance(named[0], dict):
        digest = named[0].get("sha256")
        return digest if isinstance(digest, str) else None
    return None
