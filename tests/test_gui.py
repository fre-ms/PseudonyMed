"""The desktop interface, driven offscreen: session model and the steps in sequence, docrepro's
panels among them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ocrdoc import ocr as ocr_module  # noqa: E402
from optdoc import cleanup as cleanup_module  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsView  # noqa: E402

from pseudonymed.core.model import Patient  # noqa: E402
from pseudonymed.gui.main_window import MainWindow  # noqa: E402
from pseudonymed.gui.patient_page import _parse_persons  # noqa: E402
from pseudonymed.gui.session import Session  # noqa: E402
from tests.test_pipeline import found_plain_data  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_people_from_text() -> None:
    p = _parse_persons("Dr. med. Anna Schmidt\nMüller, Hans\n\nProf. Dr. Berg, Eva")
    assert [(x.title, x.first_name, x.surname) for x in p] == [
        ("Dr. med.", "Anna", "Schmidt"), ("", "Hans", "Müller"), ("Prof. Dr.", "Eva", "Berg")]


def test_session_without_interface(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    s = Session(store_path=tmp_path / "s.sqlite")
    s.patient = patient
    assert s.add(report_pdf) is not None
    assert s.add(report_pdf) is None  # no duplicates
    assert s.add(tmp_path / "notiz.xyz") is None
    d = s.documents[0]
    assert d.state == "noch nicht analysiert"
    s.analyse(d)
    assert d.preparation is not None and "Treffer" in d.state
    res = s.export(d)
    assert res.released and d.state == "freigegeben"
    s.close()


def test_session_across_threads(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    """The desktop runs analysis and export in different worker threads; the store
    opened by the first must still serve the second (and be closed from a third).
    The first thread is kept alive meanwhile, otherwise the OS may hand its thread
    id to the second and hide the problem."""
    import threading

    session = Session(store_path=tmp_path / "s.sqlite")
    session.patient = patient
    d = session.add(report_pdf)
    error: list[BaseException] = []
    analysed = threading.Event()
    exported = threading.Event()

    def analysis_text() -> None:
        try:
            session.analyse(d)
        except BaseException as e:  # noqa: BLE001
            error.append(e)
        finally:
            analysed.set()
        exported.wait(timeout=300)  # stay alive while the export runs

    def export() -> None:
        try:
            session.export(d)
        except BaseException as e:  # noqa: BLE001
            error.append(e)
        finally:
            exported.set()

    t1 = threading.Thread(target=analysis_text)
    t1.start()
    assert analysed.wait(timeout=300) and not error, error
    t2 = threading.Thread(target=export)
    t2.start()
    t2.join(timeout=300)
    t1.join(timeout=60)
    assert not error, error
    assert d.result is not None and d.result.released
    t3 = threading.Thread(target=session.close)
    t3.start()
    t3.join(timeout=60)


def test_window_round_trip(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                           patient: Patient) -> None:
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    window.documents.synchronous = True
    window.analyse.synchronous = True
    window.export.synchronous = True

    # 1 patient
    window.patient.fill_in(patient)
    assert window.patient.f["nachname"].text() == patient.surname
    assert window.patient.f["hausnummer"].text() and window.patient.f["strasse"].text()
    window.patient.adopt()
    assert session.patient is not None and session.patient.full_name == patient.full_name
    assert session.patient.street == patient.street  # street and number joined again
    assert session.patient.referrers[0].surname == patient.referrers[0].surname
    assert window.current_page() is window.documents

    # 2 documents: opened at once; a text-layer PDF has nothing to clean up
    for step in window.chain_steps:
        step.synchronous = True
    window.documents.add([report_pdf])
    assert window.documents.listing.count() == 1
    d = session.documents[0]
    assert d.chain is not None and d.image_pages == [] and not window.documents.is_loading
    window.documents.proceed_button.click()
    assert window.current_page() is window.prepare
    assert window.prepare.panel is not None  # optdoc's panel, on the first page
    assert window.prepare.panel.note.text().startswith("Diese Seite hat eine eigene Textebene")

    # 3 text recognition: a text-layer PDF is read without OCR, its text is shown read-only
    window.prepare.proceed_button.click()
    te = window.filters
    assert window.current_page() is te
    te.start_reading()
    qapp.processEvents()
    assert "Gelesen: 1 Dokument(e)." in te.status.text()
    assert d.chain.project("ocrdoc").pages[0].ocr is False  # the text layer, no OCR
    te.proceed_button.click()
    window.regions.proceed_button.click()
    assert window.current_page() is window.text
    assert window.text.panel.text.isReadOnly()
    assert "Diagnosen" in window.text.panel.text.toPlainText()
    window.text.proceed_button.click()
    assert window.current_page() is window.analyse

    # 4 analyse with the settings of that step
    window.analyse.analyse()
    qapp.processEvents()
    assert d.preparation is not None and d.preparation.hits and d.chain is not None
    assert window.current_page() is window.check
    assert "Fertig" in window.analyse.status.text()

    # 5 review: drop one hit, draw one box; medical terms are marked by default
    rv = window.check
    assert rv.tree.topLevelItemCount() == 1 and rv.document is d
    assert window.analyse.medical_terms.isChecked()
    assert d.preparation.medical_term_count >= 3  # Hypertonie, Ramipril, Atorvastatin ...
    assert d.preparation.medical_term_rects(0)
    assert rv.show_medical_terms.isChecked() and "Fachbegriffe markiert" in rv.note.text()
    rv.show_medical_terms.setChecked(False)
    rv.show_medical_terms.setChecked(True)
    from pseudonymed.detection import ner as ner_module

    assert window.analyse.ner.isChecked() == ner_module.available()
    before = len(rv.hits())
    assert rv.table.rowCount() == before > 5
    target = next(f for f in rv.hits() if f.rule == "zuweiser1.name")
    rv.select_hit(target)
    assert "verwerfen" in rv.discard_button.text()
    rv.discard()
    assert len(rv.hits()) == before - 1 and target in d.preparation.discarded
    rv.select_hit(target)  # a discarded hit stays visible (green) and can be taken back
    assert "wiederherstellen" in rv.discard_button.text()
    rv.discard()
    assert len(rv.hits()) == before and target not in d.preparation.discarded
    rv.select_hit(target)
    rv.discard()
    rv.box((56, 300, 400, 320), "[Kasten]")
    assert any(f.source == "manuell" for f in rv.hits())
    rv.mask_mode_button.setChecked(True)
    assert rv.view.mask_mode
    rv.box((56, 330, 400, 345))  # mask mode: no dialog, no label
    assert any(f.rule == "maske" for f in rv.hits())
    rv.mask_mode_button.setChecked(False)
    assert rv.table.rowCount() == len(rv.hits())
    # a real mouse drag draws a box with the replacement text from the field, no dialog
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    rv.replacement_text.setText("[Feld]")
    vp = rv.view.viewport()
    boxes_before = len(rv.hits())
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(80, 296))  # over the salutation line (page points 212-228)
    QTest.mouseMove(vp, QPoint(300, 316))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(300, 316))
    assert len(rv.hits()) == boxes_before + 1
    assert rv.selected is not None and rv.selected.pseudonym == "[Feld]"
    assert "Kasten gesetzt" in rv.note.text()
    # Shift + drag pans instead of drawing
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                     QPoint(80, 600))
    assert rv.view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    QTest.mouseMove(vp, QPoint(120, 640))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                       QPoint(120, 640))
    assert rv.view.dragMode() == QGraphicsView.DragMode.NoDrag
    assert len(rv.hits()) == boxes_before + 1
    assert rv.remember.isChecked() and session.options.remember_discarded

    # 6 export: the step re-checks first (traffic light), then copies the checked files
    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    assert window.export.colour in ("gruen", "gelb"), window.export.traffic_light_text.text()
    assert d.precheck_current and window.export.button.isEnabled()
    check_log = window.export.output.toPlainText()
    assert "Erneut geprüft nach dem Prüfschritt" in check_log
    assert "Bewusst belassen" in check_log and "zuweiser1.name" in check_log
    window.export.run_export()
    qapp.processEvents()
    text = window.export.output.toPlainText()
    assert "Gegenprobe: bestanden" in text, text
    assert d.result is not None and d.result.released and d.preparation.finalised
    assert (tmp_path / "aus" / "befund_pseudonym.pdf").exists()
    sidecar = (tmp_path / "aus" / "befund_pseudonym.txt").read_text(encoding="utf-8")
    assert "[Kasten]" in sidecar and "[Feld]" in sidecar
    assert (tmp_path / "aus" / "befund_pseudonym.bericht.json").exists()
    assert patient.surname not in sidecar
    assert patient.referrers[0].surname in sidecar  # deliberately kept

    # the decision to keep the referrer was stored for this patient
    from pseudonymed.pseudonyms.store import PseudonymStore

    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        assert any(r == "zuweiser1.name" for r, _t in sp.discarded_for(patient))

    # 7 re-identify
    window.show_page(window.reidentify)
    ri = window.reidentify
    assert ri.person.count() == 1
    k = d.result.code
    ri.input_text.setPlainText(f"Patient {k} wurde untersucht; Angehörige {k}-1 war dabei.")
    ri.reidentify()
    out = ri.output.toPlainText()
    assert patient.full_name in out and patient.relatives[0].full_name in out
    assert "nichts offen" in ri.note.text()
    assert found_plain_data(text, patient) == []  # the report shows no clear data
    window.close()


def test_window_text_file(qapp: QApplication, tmp_path: Path, patient: Patient,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript goes through the same five steps; the review shows text, not a page.
    The discarded guess ends up in the exported vocabulary, which the analysis step
    imports into the user's own list."""
    from PySide6.QtGui import QTextCursor

    from pseudonymed.detection import medical_terms as terms_module
    from tests.synthetic import create_transcript, misspelt

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    terms_module.standard.cache_clear()
    source = create_transcript(tmp_path / "gespraech.txt", patient)
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    window.documents.synchronous = True
    window.analyse.synchronous = True
    window.export.synchronous = True
    window.check.synchronous = True
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([source, tmp_path / "egal.xyz"])
    assert window.documents.listing.count() == 1
    assert session.documents[0].chain is None  # text files have no chain of docrepro
    window.filters.synchronous = True
    window.filters.start_reading()
    assert "Textdatei" in window.filters.placeholder.text()
    assert window.analyse.fuzzy.isChecked()
    window.analyse.analyse()
    qapp.processEvents()
    d = session.documents[0]
    assert d.preparation is not None and d.preparation.is_text
    assert "Text, " in window.documents.listing.item(0).text()

    rv = window.check
    assert rv.stack.currentWidget() is rv.text_view and not rv.mark_button.isHidden()
    assert rv.tree.topLevelItem(0).child(0).text(0) == "Text"
    assert rv.text_view.toPlainText() == d.preparation.text
    assert rv.table.rowCount() == len(rv.hits()) > 5
    assert any(f.source == "unscharf" for f in rv.hits())
    assert "Unscharfe Suche" in rv.note.text()
    fuzzy = next(f for f in rv.hits() if f.source == "unscharf")
    rv.select_hit(fuzzy)
    assert rv.text_view.textCursor().position() == fuzzy.start
    rv.discard()
    assert fuzzy not in rv.hits()
    with pytest.raises(ValueError):
        rv.mark_selection("[X]")  # nothing selected
    text = d.preparation.text
    a = text.index("Wörter vergesse")
    cursor = rv.text_view.textCursor()
    cursor.setPosition(a)
    cursor.setPosition(a + len("Wörter vergesse"), QTextCursor.MoveMode.KeepAnchor)
    rv.text_view.setTextCursor(cursor)
    rv.mark_selection("[Symptom]")
    assert any(f.source == "manuell" and f.pseudonym == "[Symptom]" for f in rv.hits())
    rv.show_medical_terms.setChecked(False)
    rv.show_medical_terms.setChecked(True)

    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    assert window.export.colour in ("gruen", "gelb")
    window.export.run_export()
    qapp.processEvents()
    log = window.export.output.toPlainText()
    assert "Gegenprobe: bestanden" in log and "Freigegeben" in log, log
    target = tmp_path / "aus" / "gespraech_pseudonym.txt"
    out = target.read_text(encoding="utf-8")
    assert "[Symptom]" in out and misspelt(patient.surname) in out  # discarded = kept
    assert patient.surname not in out and found_plain_data(log, patient) == []
    # the discarded fuzzy hit is a guess, so it went into the vocabulary file …
    vocabulary = tmp_path / "aus" / "gespraech_pseudonym.vokabular.txt"
    assert d.result.target_vocabulary == vocabulary and "Vokabular:" in log
    lines = [z for z in vocabulary.read_text(encoding="utf-8").splitlines()
             if z and not z.startswith("#")]
    assert lines == [fuzzy.text] and patient.surname not in lines
    # … which the vocabulary step merges into the table
    assert not terms_module.standard().is_medical_term(fuzzy.text)
    window.vocabulary.synchronous = True
    assert window.vocabulary.import_file(vocabulary) == 1
    assert terms_module.standard().is_medical_term(fuzzy.text)
    assert "1 neue von 1" in window.vocabulary.status.text()
    assert window.vocabulary.import_file(vocabulary) == 0  # already known
    window.analyse.refresh()
    assert "Nutzervokabular" in window.analyse.vocabulary_info.text()
    window.close()
    terms_module.standard.cache_clear()


def test_window_can_be_shrunk(qapp: QApplication, tmp_path: Path) -> None:
    """Every step sits in a scroll area, so the window shrinks instead of refusing."""
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    window.show()
    qapp.processEvents()
    assert window.minimumSizeHint().width() <= 720
    window.resize(720, 480)
    qapp.processEvents()
    assert window.width() <= 720 and window.height() <= 480
    for i in range(window.tabs.count()):
        window.tabs.setCurrentIndex(i)
        qapp.processEvents()
        assert window.width() <= 720, window.tabs.tabText(i)
    assert window.current_page() is window.database
    window.close()


def test_self_test(qapp: QApplication, capsys: pytest.CaptureFixture[str]) -> None:
    from pseudonymed.gui.app import main

    assert main(["--selbsttest"]) == 0
    assert "Selbsttest bestanden" in capsys.readouterr().out


def test_english_interface(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui import translation

    assert translation.determine_language(["--sprache", "en"]) == "en"
    assert translation.determine_language(["--language=EN-GB"]) == "en-gb"
    assert translation.without_language_arguments(["x", "--sprache", "en", "y"]) == ["x", "y"]
    monkeypatch.setenv("PSEUDONYMED_SPRACHE", "en")
    assert translation.determine_language([]) == "en"
    translator = translation.install(qapp, "en_gb")
    assert translator is not None
    try:
        window = MainWindow(Session(store_path=None))
        tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        assert tabs == ["1 Patient data", "2 Identifiers", "3 Medical vocabulary", "4 Documents",
                        "5 Prepare", "6 Filters for OCR", "7 Areas", "8 Check the text",
                        "9 Analysis", "10 Visual review", "11 Control", "12 Export",
                        "13 Re-identification", "14 Database"]
        assert window.patient.f["nachname"].placeholderText() == ""
        assert window.export.button.text() == "Export"
        # docrepro's panels and wording follow the installed translation
        assert translation.interface_language() == "en"
        assert window.prepare.words.language == "en"
        window.close()
    finally:
        qapp.removeTranslator(translator)
    window = MainWindow(Session(store_path=None))
    assert window.tabs.tabText(3) == "4 Dokumente"
    assert translation.interface_language() == "de"
    window.close()


@pytest.mark.skipif(not ocr_module.available_backends() or not cleanup_module.available(),
                    reason="OCR-Backend oder OpenCV fehlt")
def test_window_clean_up(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                         patient: Patient) -> None:
    """A tilted scan goes through docrepro's steps in the window: optdoc's suggestion shown
    and adjusted, ocrdoc's filters chosen, every page read, the text corrected; the analysis
    runs on the prepared pages and the corrected text, the review follows."""
    from pseudonymed.gui.chain_pages import ChainStepPage
    from tests.test_ocr import _scan

    scan = _scan(report_pdf, tmp_path / "scan.pdf", rotation=2.0)
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    for page in (window.documents, window.analyse, window.export, *window.chain_steps):
        page.synchronous = True
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([scan])
    qapp.processEvents()
    d = session.documents[0]
    assert d.chain is not None and d.image_pages == [0, 1]
    assert "aufbereiten" in window.documents.listing.item(0).text()
    # 5 prepare: optdoc's panel on the first page with its suggestion
    window.show_page(window.prepare)
    cu = window.prepare
    assert cu.tree.topLevelItemCount() == 1 and cu.document is d and cu.page == 0
    panel = cu.panel
    project = d.chain.project("optdoc")
    a = project.plan(0)
    assert a is not None and a.straighten and abs(abs(a.angle) - 2.0) < 0.5, a
    assert "begradigt" in panel.note.text()
    panel.boxes["background"].setChecked(True)
    panel.add_mask((56, 60, 300, 90))
    assert project.plan(0).background and len(project.plan(0).masks) == 1
    assert "geändert" in window.documents.listing.item(0).text() or d.preparation is None
    panel.copy_to_all()
    assert project.plan(1).background
    panel.detect_orientation()
    assert project.plan(0).rotation == 0
    panel.rotate(90)
    assert project.plan(0).rotation == 90 and not project.plan(0).masks
    panel.rotate(270)
    panel.skip()
    assert not project.plan(0).active
    panel.restore()
    assert project.plan(0).straighten
    panel.undo()  # nothing to undo any more: no error
    # the panel speaks the interface language
    from pseudonymed.gui import translation

    translator = translation.install(qapp, "en_gb")
    assert translator is not None
    try:
        english = ChainStepPage(session, "cleanup")
        english.synchronous = True
        english.refresh()
        assert "straightened" in english.panel.note.text()
        english.close()
    finally:
        qapp.removeTranslator(translator)
    # 6 filters for the OCR, then every page read
    window.show_page(window.filters)
    fi = window.filters
    assert fi.settings is not None and fi.settings.engine.currentData() == "auto"
    ocr_project = d.chain.project("ocrdoc")
    fi.panel.upscaling.setCurrentIndex(2)  # 3×
    fi.panel.boxes["letter_mode"].setChecked(True)
    assert ocr_project.filters(0).upscale == 3 and ocr_project.filters(0).letter_mode
    fi.panel.upscaling.setCurrentIndex(0)
    fi.panel.boxes["letter_mode"].setChecked(False)
    fi.start_reading()
    qapp.processEvents()
    assert all(p.ocr for p in d.chain.project("ocrdoc").pages)
    assert "Gelesen: 1 Dokument(e)." in fi.status.text()
    # 7 areas: found on the page shown
    window.show_page(window.regions)
    assert d.chain.project("ocrdoc").has_regions(0)
    # 8 the text, corrected by hand; leaving the step takes the correction in
    window.show_page(window.text)
    tp = window.text.panel
    assert not tp.text.isReadOnly()
    ocr = tp.text.toPlainText()
    assert "Diagnosen" in ocr
    tp.text.setPlainText(ocr.rstrip("\n") + "\nZusatz: Testzeile aus der Korrektur.\n")
    window.show_page(window.analyse)
    assert d.chain.project("ocrdoc").pages[0].correction is not None
    window.analyse.analyse()
    qapp.processEvents()
    assert d.preparation is not None and not d.changed
    assert window.current_page() is window.check
    assert d.preparation.pages[0].correction is not None
    assert d.preparation.cleaned_up and 0 in d.preparation.cleaned_up
    assert any("aufbereitet" in w for w in d.preparation.notes)
    assert len(d.preparation.hits) > 5
    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    # OCR leaves only notes, no warnings
    assert window.export.colour == "gruen", window.export.traffic_light_text.text()
    assert any("Handschrift" in h for h in d.precheck.report.notes)
    assert not d.precheck.report.warnings
    window.export.run_export()
    qapp.processEvents()
    log = window.export.output.toPlainText()
    assert "Gegenprobe: bestanden" in log, log
    assert found_plain_data(log, patient) == []
    txt = (tmp_path / "aus" / "scan_pseudonym.txt").read_text(encoding="utf-8")
    assert "Zusatz: Testzeile aus der Korrektur." in txt  # the correction reached the output
    window.close()


def test_export_blocked_on_red_light(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                                     patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the re-check still finds identifiers, the light is red, the export button is
    off and exporting raises (a dialog in the real app); nothing is written."""
    from pseudonymed.checks.report import create_report
    from pseudonymed.core.model import Recheck
    from pseudonymed.gui import session as session_module

    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    window.documents.synchronous = window.analyse.synchronous = window.export.synchronous = True
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([report_pdf])
    window.analyse.analyse()
    real = session_module.finalise

    def failing(v, final=True):
        res = real(v, final)
        g = Recheck(residual_matches=[{"seite": 1, "art": "name", "regel": "name.nachname",
                                       "pseudonym": "x", "rects": [], "quelle": "test"}])
        res.target_pdf = res.target_txt = res.target_md = None
        res.report = create_report(v.source, None, 2, True, res.hits, g)
        return res

    monkeypatch.setattr(session_module, "finalise", failing)
    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    assert window.export.colour == "rot" and not window.export.button.isEnabled()
    assert "Abbruch" in window.export.status.text()
    assert "nichts exportiert" in window.export.output.toPlainText()
    with pytest.raises(RuntimeError):
        window.export.run_export()
    assert not (tmp_path / "aus").exists() or not list((tmp_path / "aus").glob("*_pseudonym.pdf"))
    window.close()
