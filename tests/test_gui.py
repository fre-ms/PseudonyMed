"""The desktop interface, driven offscreen: session model and the five pages in sequence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from optdoc import cleanup as cleanup_module
from optdoc import ocr as ocr_module
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
    window.documents.add([report_pdf])
    assert window.documents.listing.count() == 1
    d = session.documents[0]
    assert d.opened is not None and d.opened.candidates == [] and not window.documents.is_loading
    assert "nichts aufzubereiten" in window.documents.clean_up.note.text()
    window.documents.proceed_button.click()
    assert window.current_page() is window.ocr

    # 3 text recognition: a text-layer PDF is read without OCR, its text is shown read-only
    te = window.ocr
    te.synchronous = True
    te.start_reading()
    qapp.processEvents()
    assert d.reading is not None and d.reading.ocr_pages == [] and te.text.isReadOnly()
    assert "Diagnosen" in te.text.toPlainText()
    te.proceed_button.click()
    assert window.current_page() is window.analyse

    # 4 analyse with the settings of that step
    window.analyse.analyse()
    qapp.processEvents()
    assert d.preparation is not None and d.preparation.hits and d.reading is None
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
    assert session.documents[0].opened is None  # text files are not opened as documents
    window.ocr.synchronous = True
    window.ocr.start_reading()
    assert session.documents[0].reading is None
    assert "Textdatei" in window.ocr.note.text()
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
                    "5 Text recognition", "6 Analysis", "7 Visual review", "8 Control", "9 Export",
                    "10 Re-identification",
                    "11 Database"]
        assert window.patient.f["nachname"].placeholderText() == ""
        assert window.export.button.text() == "Export"
        # the clean-up wording comes from optdoc and follows the installed translation
        assert translation.interface_language() == "en"
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
    """A tilted scan goes through the clean-up step: suggestion shown, user adjusts,
    analysis runs on the corrected page, the review follows."""
    from tests.test_ocr import _scan

    scan = _scan(report_pdf, tmp_path / "scan.pdf", rotation=2.0)
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    window.documents.synchronous = True
    window.analyse.synchronous = True
    window.export.synchronous = True
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([scan])
    qapp.processEvents()
    d = session.documents[0]
    assert d.opened is not None and d.opened.candidates == [0, 1]
    assert "aufbereiten" in window.documents.listing.item(0).text()
    cu = window.documents.clean_up
    assert cu.tree.topLevelItemCount() == 1 and cu.document is d and cu.page == 0
    a = cu.current_plan()
    assert a is not None and a.straighten and abs(abs(a.angle) - 2.0) < 0.5, a
    assert "begradigt" in cu.note.text()
    cu.background_smoothing.setChecked(True)
    cu.add_mask((56, 60, 300, 90))
    assert d.cleanup[0].background and len(d.cleanup[0].masks) == 1
    cu.apply_to_all()
    assert d.cleanup[1].background
    cu.upscaling.setCurrentIndex(2)  # 3×
    cu.text_mode.setChecked(True)
    cu.colour_removal.setChecked(True)
    assert d.cleanup[0].upscale == 3 and d.cleanup[0].letter_mode
    assert d.cleanup[0].remove_colour and "Buchstabenmodus" in cu.note.text()
    # optdoc's part of the hint follows the interface language like the rest of the page
    from pseudonymed.gui import translation

    translator = translation.install(qapp, "en_gb")
    assert translator is not None
    try:
        cu.draw()
        assert "letter mode" in cu.note.text() and "Buchstabenmodus" not in cu.note.text()
    finally:
        qapp.removeTranslator(translator)
    cu.draw()
    assert "Buchstabenmodus" in cu.note.text()
    cu.upscaling.setCurrentIndex(0)
    cu.text_mode.setChecked(False)
    # the two views are linked: pan one, the other follows; zoom likewise
    cu.after.zoom(2.0)
    assert abs(cu.before.zoom_factor - cu.after.zoom_factor) < 1e-6  # relative to each base
    cu.before.verticalScrollBar().setValue(cu.before.verticalScrollBar().maximum() // 2)
    v1 = cu.before.verticalScrollBar().value() / max(1, cu.before.verticalScrollBar().maximum())
    v2 = cu.after.verticalScrollBar().value() / max(1, cu.after.verticalScrollBar().maximum())
    assert abs(v1 - v2) < 0.02, (v1, v2)
    cu.before.reset_zoom()
    assert abs(cu.after.zoom_factor - 1.0) < 1e-6
    cu.detect_orientation()
    assert d.cleanup[0].rotation == 0
    cu.rotate(90)
    assert d.cleanup[0].rotation == 90 and not d.cleanup[0].masks
    cu.rotate(270)
    cu.skip_cleanup()
    assert not d.cleanup[0].active
    cu.restore_suggestion()
    assert d.cleanup[0].straighten
    cu.undo()  # nothing to undo any more: no error
    # 3 text recognition on the cleaned-up pages, with a correction
    te = window.ocr
    te.synchronous = True
    assert te.engine.currentData() == "auto" and te.proceed_button.isEnabled()
    te.start_reading()
    qapp.processEvents()
    assert d.reading is not None and d.reading.ocr_pages == [1, 2]
    assert te.tree.topLevelItemCount() == 1 and not te.text.isReadOnly()
    ocr = te.text.toPlainText()
    assert "Diagnosen" in ocr
    te.text.setPlainText(ocr.rstrip("\n") + "\nZusatz: Testzeile aus der Korrektur.\n")
    te.adopt_text()
    assert d.reading.pages[0].correction is not None and "korrigiert" in te.text_note.text()
    window.analyse.analyse()
    qapp.processEvents()
    assert d.preparation is not None and d.opened is None and d.reading is None
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
