"""The working state: save after the review, restore it, vary it, take words into the list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from optdoc import ocr as ocr_module
from optdoc.pagetext import PageText
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed import working_state as state_mod
from pseudonymed.checks.report import as_text
from pseudonymed.cli import app
from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.pipeline import PageEntry, PipelineOptions, analyse, finalise, prepare
from pseudonymed.pipeline.preparation import PreparationBase
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore, one_time_store
from tests.synthetic import create_transcript
from tests.test_gui import qapp  # noqa: F401
from tests.test_ocr import _scan


def _prepared(report_pdf: Path, patient: Patient, store: PseudonymStore):
    """A reviewed preparation: one hit discarded, a box, a mask, one word taken up."""
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    target = next(f for f in v.hits if f.rule == "zuweiser1.name")
    v.discard(target)
    v.box(0, (56, 300, 400, 320), "[Kasten]")
    v.add_mask(0, (56, 330, 400, 345))
    text = v.pages[0].text_base.text
    i = text.index("Gemeinschaftspraxis")  # the letterhead: no hit, taken up by hand
    v.take_up(0, i, i + len("Gemeinschaftspraxis"))
    return v


# -- saving and restoring --------------------------------------------------------------------------
def test_round_trip_pdf(tmp_path: Path, report_pdf: Path, patient: Patient,
                        store: PseudonymStore) -> None:
    v = _prepared(report_pdf, patient, store)
    document, configuration = state_mod.save(v, tmp_path / "ablage")  # a folder
    assert document.name == "befund.arbeitsstand.pdf"
    assert configuration.name == "befund.arbeitsstand.json"
    data = json.loads(configuration.read_text(encoding="utf-8"))
    assert data[state_mod.FORMAT_MARK] == state_mod.VERSION and data["kennung"] == v.code
    assert data["patient"]["nachname"] == patient.surname  # clear data, by design
    assert state_mod.is_working_state(document) == configuration
    assert state_mod.is_working_state(configuration) == configuration
    assert state_mod.is_working_state(report_pdf) is None

    a = state_mod.read(configuration)
    v2 = state_mod.preparation_from(a, store=store)
    assert v2.source.name == "befund.pdf" and v2.code == v.code
    assert [f.as_dict() for f in v2.hits] == [f.as_dict() for f in v.hits]
    assert [f.rule for f in v2.discarded] == ["zuweiser1.name"]
    assert v2.medical_term_count == v.medical_term_count
    assert any("Arbeitsstand aus befund.arbeitsstand.json" in h for h in v2.notes)
    e1 = finalise(v, final=False)
    e2 = finalise(v2, final=False)
    assert e1.released and e2.released, as_text(e2.report)
    assert e1.text == e2.text and "[Kasten]" in e2.text and "[Begriff " in e2.text
    assert "Gemeinschaftspraxis" not in e2.text
    z = store.mapping(patient.key())
    back = reidentify(e2.text, z, known_codes=store.codes())
    assert "Gemeinschaftspraxis" in back.text


def test_round_trip_text_file(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    source = create_transcript(tmp_path / "gespraech.srt", patient, "srt")
    v = prepare(source, patient, PipelineOptions(), store=store)
    i = v.text.index("Hypertonie")
    v.take_up(0, i, i + len("Hypertonie"))  # a protected term, taken up on purpose
    document, configuration = state_mod.save(v, tmp_path / "x.arbeitsstand.json")
    assert document.name == "x.arbeitsstand.srt"
    v2 = state_mod.preparation_from(state_mod.read(configuration), store=store)
    assert v2.is_text and v2.output_extension == ".srt" and v2.text == v.text
    e1, e2 = finalise(v, final=False), finalise(v2, final=False)
    assert e1.released and e2.released and e1.text == e2.text
    assert "[Begriff " in e2.text and "Hypertonie" not in e2.text


def test_changed_document_and_foreign_file(tmp_path: Path, report_pdf: Path, patient: Patient,
                                           store: PseudonymStore) -> None:
    v = _prepared(report_pdf, patient, store)
    document, configuration = state_mod.save(v, tmp_path)
    v.close()
    document.write_bytes(document.read_bytes() + b"\n")
    with pytest.raises(state_mod.WorkingStateError, match="verändert"):
        state_mod.read(configuration)
    foreign = tmp_path / "fremd.arbeitsstand.json"
    foreign.write_text('{"foo": 1}', encoding="utf-8")
    assert state_mod.is_working_state(foreign) is None
    with pytest.raises(state_mod.WorkingStateError):
        state_mod.read(foreign)


def test_recipient_mode_refused(report_pdf: Path, patient: Patient) -> None:
    with one_time_store() as sp:
        opt = PipelineOptions(recipient="x", recipient_fingerprint="y")
        v = prepare(report_pdf, patient, opt, store=sp)
        with pytest.raises(state_mod.WorkingStateError, match="Empfängermodus"):
            state_mod.capture(v)
        v.close()


# -- a new analysis keeps the reading and the decisions --------------------------------------------
def test_reanalysis_adopts_decisions(report_pdf: Path, patient: Patient,
                                     store: PseudonymStore) -> None:
    v = _prepared(report_pdf, patient, store)
    old = state_mod.capture(v)
    assert old.path is None and old.source == report_pdf  # outputs stay beside the source
    v.close()
    reading = state_mod.reading_from(old, PipelineOptions(identifiers=False))
    assert reading.backend is None and not reading.used_up and reading.source == report_pdf
    v3 = analyse(reading, patient, PipelineOptions(identifiers=False), store=store)
    n = state_mod.adopt_decisions(old, v3)
    assert n == 5  # box (two lines), mask, take-up, one discard
    assert [f.rule for f in v3.discarded] == ["zuweiser1.name"]
    assert sorted(f.rule for f in v3.hits if f.source == "manuell") == [
        "aufnahme", "manuell", "manuell", "maske"]
    assert state_mod.adopt_decisions(old, v3) == 0  # idempotent
    e = finalise(v3, final=False)
    assert e.released and "[Kasten]" in e.text and "[Begriff " in e.text


@pytest.mark.skipif(not ocr_module.available_backends(), reason="kein OCR-Backend installiert")
def test_scan_without_repeated_ocr(tmp_path: Path, report_pdf: Path, patient: Patient,
                                   store: PseudonymStore,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    v = prepare(scan, patient, PipelineOptions(), store=store)
    assert v.ocr_pages == [1, 2]
    v.set_ocr_text(0, v.ocr_text(0).replace("Diagnosen", "Diagnosen (korrigiert)"))
    document, configuration = state_mod.save(v, tmp_path)
    v.close()
    monkeypatch.setattr(ocr_module, "recognise_page",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR lief erneut")))
    a = state_mod.read(configuration)
    v2 = state_mod.preparation_from(a, store=store)
    assert v2.ocr_pages == [1, 2] and "(korrigiert)" in v2.ocr_text(0)
    assert v2.pages[0].structure is not None and v2.uncertain_rects(0) is not None
    reading = state_mod.reading_from(a)
    v3 = analyse(reading, patient, PipelineOptions(), store=store)
    assert v3.ocr_pages == [1, 2] and "(korrigiert)" in v3.ocr_text(0)
    assert {f.rule for f in v3.hits} & {f.rule for f in v2.hits}
    v2.close()
    v3.close()


# -- taking words into the list --------------------------------------------------------------------
def test_word_at_and_take_up(report_pdf: Path, patient: Patient,
                             store: PseudonymStore) -> None:
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    base = v.pages[0].text_base
    i = base.text.index("Diagnosen")
    box = base.boxes[i + 2]
    span = v.word_at(0, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    assert span == (i, i + len("Diagnosen"))
    assert v.word_at(0, 5.0, 5.0) is None  # the margin
    before = len(v.hits)
    f = v.take_up(0, *span)
    assert f.pseudonym.startswith("[Begriff ") and f.rects and f.source == "manuell"
    assert len(v.hits) == before + 1
    g = v.take_up(0, span[0], span[1], "[Wort]")  # replaces the first take-up
    assert g.pseudonym == "[Wort]" and len(v.hits) == before + 1
    with pytest.raises(ValueError):
        v.take_up(0, span[0], span[0])
    v.close()


# -- interface -------------------------------------------------------------------------------------
def test_interface(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                   patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = tmp_path / "s.sqlite"
    with PseudonymStore(db) as sp:
        v = _prepared(report_pdf, patient, sp)
        count = len(v.hits)
        document, configuration = state_mod.save(v, tmp_path / "ablage")
        v.close()

    session = Session(store_path=db)
    window = MainWindow(session)
    for page in (window.documents, window.analyse, window.check, window.export):
        page.synchronous = True
    assert session.patient is None
    window.documents.add([document])  # the document beside the JSON is enough
    qapp.processEvents()
    d = session.documents[0]
    assert d.working_state == configuration and d.preparation is not None and not d.error
    assert len(d.preparation.hits) == count and len(d.preparation.discarded) == 1
    assert session.patient is not None and session.patient.surname == patient.surname
    assert window.patient.f["nachname"].text() == patient.surname  # step 1 filled in
    assert "Arbeitsstand" in window.documents.listing.item(0).text()
    assert window.current_page() is window.check  # straight to the review
    rv = window.check
    assert rv.document is d
    assert rv.table.rowCount() == sum(1 for f in d.preparation.hits if f.page == 0)
    assert window.documents.add([configuration]) is None  # already in

    # pick a word on the page, extend it, take it up with a code of its own
    base = d.preparation.pages[0].text_base
    i = base.text.index("Diagnosen")
    box = base.boxes[i + 1]
    rv.point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    assert rv.selection == (i, i + len("Diagnosen")) and rv.mark_button.isEnabled()
    j = base.text.index(" ", i + len("Diagnosen") + 1) + 1
    box2 = base.boxes[j]
    rv.point((box2[0] + box2[2]) / 2, (box2[1] + box2[3]) / 2, extend=True)
    assert rv.selection is not None and rv.selection[0] == i and rv.selection[1] > i + 9
    rv.take_up()
    assert rv.selection is None and rv.selected is not None
    assert rv.selected.rule == "aufnahme" and rv.selected.pseudonym.startswith("[Begriff ")
    rv.point(5.0, 5.0)  # the margin clears the selection
    assert rv.selection is None and not rv.mark_button.isEnabled()

    # vary: analyse again without identifiers, decisions survive, then save again
    window.analyse.identifiers.setChecked(False)
    window.analyse.analyse(again=True)
    qapp.processEvents()
    assert d.preparation is not None and not d.error
    assert [f.rule for f in d.preparation.discarded] == ["zuweiser1.name"]
    assert sorted(f.rule for f in d.preparation.hits if f.source == "manuell") == [
        "aufnahme", "aufnahme", "manuell", "maske"]  # the take-up replaced one box line
    rv.document = d
    paths = rv.save_working_state(tmp_path / "variante.arbeitsstand.json")
    assert paths is not None and paths[1].name == "variante.arbeitsstand.json"
    assert "Arbeitsstand gespeichert" in rv.note.text()
    window.close()


def test_command_line(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    from tests.test_cli import _patient_file

    runner = CliRunner()
    db = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient",
                            str(_patient_file(tmp_path, patient)), "--speicher", str(db),
                            "--arbeitsstand", "--ziel", str(tmp_path / "aus")])
    assert r.exit_code == 0, r.output
    configuration = tmp_path / "aus" / "befund.arbeitsstand.json"
    assert configuration.exists() and "Arbeitsstand:" in r.output
    assert (tmp_path / "aus" / "befund_pseudonym.pdf").exists()
    r = runner.invoke(app, ["pseudonymisieren", str(configuration), "--speicher", str(db),
                            "--ziel", str(tmp_path / "wie-gespeichert")])
    assert r.exit_code == 0, r.output  # no --patient needed: the state carries it
    text = (tmp_path / "wie-gespeichert" / "befund_pseudonym.txt").read_text("utf-8")
    assert "[geb. " in text and "Arbeitsstand befund.arbeitsstand.json" in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(configuration), "--speicher", str(db),
                            "--ziel", str(tmp_path / "variante"), "--datum", "alter",
                            "--neu-analysieren"])
    assert r.exit_code == 0, r.output
    assert "[Alter " in (tmp_path / "variante" / "befund_pseudonym.txt").read_text("utf-8")


# -- carrying decisions over to a fresh analysis ---------------------------------------------------
class _Reanalysis(PreparationBase):
    """The least a fresh preparation needs here: pages, discards, hints."""

    def __init__(self, pages: list[PageEntry]) -> None:
        self.pages, self.discarded, self.restored, self.version = pages, [], set(), 0
        self.notes: list[str] = []

    @property
    def hits(self) -> list[Hit]:
        return [f for s in self.pages for f in s.hits]


def _matches(start: int, end: int, rule: str = "patient.nachname", source: str = "text",
             text: str | None = None,
             rects: list[tuple[float, float, float, float]] | None = None) -> Hit:
    """A synthetic hit on page 0; the text is filler unless given."""
    return Hit(page=0, start=start, end=end, text=text or "x" * (end - start),
               kind=Kind.SURNAME, rule=rule, pseudonym="[P]", rects=rects or [],
               source=source)


def _page(*hits: Hit) -> PageEntry:
    page = PageEntry(0, PageText(0, 100.0, 100.0, text="a" * 60))
    page.hits = list(hits)
    return page


def test_mark_again_displaces_only_analysis_matches_beneath() -> None:
    page = _page(_matches(0, 10), _matches(20, 30), _matches(40, 50, source="manuell"))
    mark = _matches(25, 45, rule="aufnahme", source="manuell")
    state_mod._mark_again(page, mark)
    assert [(f.start, f.end) for f in page.hits] == [(0, 10), (40, 50), (25, 45)]
    box = _matches(0, 0, rule="manuell", source="manuell", rects=[(1.0, 2.0, 3.0, 4.0)])
    state_mod._mark_again(page, box)  # a box without a span replaces nothing
    assert len(page.hits) == 4
    assert state_mod._already_marked(box, page.hits)
    other = _matches(0, 0, rule="manuell", source="manuell", rects=[(1.0, 2.0, 3.0, 5.0)])
    assert not state_mod._already_marked(other, page.hits)


def test_adopt_decisions_counts_and_skips_missing_pages() -> None:
    page = _page(_matches(0, 7, text="Treffer"), _matches(22, 28))
    new = _Reanalysis([page])
    old = state_mod.WorkingState(data={
        "seiten": [
            {"nummer": 0, "fundstellen": [
                _matches(20, 30, rule="aufnahme", source="manuell").as_dict(),
                _matches(50, 55).as_dict(),  # an analysis hit is never carried over
            ]},
            {"nummer": 3, "fundstellen": [_matches(0, 5, source="manuell").as_dict()]},
        ],
        "verworfen": [{"regel": "patient.nachname", "text": " treffer"},  # normalised match
                      {"regel": "patient.vorname", "text": ""}],  # no text: nothing to match
    }, document=b"")
    assert state_mod.adopt_decisions(old, new) == 2  # the mark and the discard
    assert [(f.start, f.end) for f in page.hits] == [(20, 30)]  # (22, 28) lay underneath
    assert [(f.start, f.end) for f in new.discarded] == [(0, 7)]
    assert new.notes == ["2 Entscheidungen aus dem Arbeitsstand übernommen (Kästen, Masken,"
                         " Markierungen, verworfene Treffer)."]
    assert state_mod.adopt_decisions(old, new) == 0  # idempotent
