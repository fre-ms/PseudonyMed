"""Step 6 on its own, offscreen with synthetic documents: the option round trip, the
guards before a run, the recipient switch and the re-analysis button."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from doccor.errors import DocError

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.core.model import Patient  # noqa: E402
from pseudonymed.gui.analysis_page import AnalysisPage  # noqa: E402
from pseudonymed.gui.session import Session  # noqa: E402
from pseudonymed.pseudonyms.pgp import Certificate  # noqa: E402
from tests.test_gui import qapp  # noqa: E402, F401

# the run options the page owns, in their default state and ticked the other way round
DEFAULTS = {"date_format": "jahr", "codes": True, "unreadable": "schwaerzen", "medical_terms": True,
            "identifiers": True, "redact_drugs": False,
            "redact_doses": False, "redact_lab_tests": False,
            "redact_measurements": False, "fuzzy_text": True}
INVERTED = {"date_format": "alter", "codes": False, "unreadable": "lassen", "medical_terms": False,
             "identifiers": False, "redact_drugs": True,
             "redact_doses": True, "redact_lab_tests": True,
             "redact_measurements": True, "fuzzy_text": False}


def _page(tmp_path: Path, patient: Patient | None = None) -> tuple[Session, AnalysisPage]:
    session = Session(store_path=tmp_path / "s.sqlite")
    session.patient = patient
    page = AnalysisPage(session)
    page.synchronous = True
    return session, page


def _options(session: Session) -> dict[str, object]:
    return {name: getattr(session.options, name) for name in DEFAULTS}


def _toggle(page: AnalysisPage, inverted: bool) -> None:
    """Tick every option the other way round, or back to the defaults."""
    page.date.setCurrentIndex(1 if inverted else 0)
    for box in (page.codes, page.unreadable, page.medical_terms, page.identifiers,
                page.fuzzy):
        box.setChecked(not inverted)
    for box in (page.drugs, page.doses, page.lab_tests, page.measurements):
        box.setChecked(inverted)


def _tidy_up(app: QApplication, session: Session, page: AnalysisPage) -> None:
    """Close the session and let Qt delete the page now: a page still alive at interpreter
    exit (a traceback keeps it, for one) would be deleted under the application."""
    session.close()
    page.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_options_round_trip(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                            patient: Patient) -> None:
    """Every tick box and the date form reach the session's options when a run starts,
    and the defaults come back the same way with the re-analysis."""
    session, page = _page(tmp_path, patient)
    assert session.add(report_pdf) is not None
    assert _options(session) == DEFAULTS
    ner_before = page.ner.isChecked()
    signals: list[bool] = []
    page.analysed.connect(lambda: signals.append(True))
    _toggle(page, True)
    page.ner.setChecked(not ner_before)
    page.button.click()
    qapp.processEvents()
    assert _options(session) == INVERTED and session.options.ner is (not ner_before)
    assert signals == [True] and "Fertig: 1" in page.status.text()
    d = session.documents[0]
    assert d.preparation is not None and d.preparation.hits
    assert any(f.pseudonym.startswith("[Alter ") for f in d.preparation.hits)
    _toggle(page, False)
    page.ner.setChecked(ner_before)
    page.again_button.click()
    qapp.processEvents()
    assert _options(session) == DEFAULTS and session.options.ner is ner_before
    assert signals == [True, True]
    assert any(f.pseudonym.startswith("[geb. ") for f in d.preparation.hits)
    _tidy_up(qapp, session, page)


def test_analyse_again_button(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                              patient: Patient) -> None:
    """„Analysieren“ leaves a document alone that is analysed and not yet exported;
    „Erneut analysieren“ runs it again and keeps the decisions of the review."""
    session, page = _page(tmp_path, patient)
    session.add(report_pdf)
    page.button.click()
    qapp.processEvents()
    d = session.documents[0]
    first = d.preparation
    assert first is not None and "Fertig: 1" in page.status.text()
    first.discard(next(f for f in first.hits if f.rule == "zuweiser1.name"))
    page.button.click()  # nothing queued: the document waits for its review and export
    qapp.processEvents()
    assert d.preparation is first and "Fertig: 0" in page.status.text()
    assert page.progress.maximum() == 1 and page.progress.value() == 1
    page.again_button.click()
    qapp.processEvents()
    assert d.preparation is not None and d.preparation is not first
    assert "Fertig: 1" in page.status.text() and page.button.isEnabled()
    assert [f.rule for f in d.preparation.discarded] == ["zuweiser1.name"]
    assert "Treffer" in page.listing.item(0).text()
    _tidy_up(qapp, session, page)


def test_guards_before_the_run(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                               patient: Patient) -> None:
    """Without a patient, without documents or while they are still loading nothing runs;
    the synchronous mode raises what the dialog would say."""
    session, page = _page(tmp_path)
    with pytest.raises(RuntimeError, match="Patienten"):
        page.analyse()
    session.patient = patient
    with pytest.raises(RuntimeError, match="Dokumente hinzufügen"):
        page.analyse()
    session.add(report_pdf)
    page.is_loading = lambda: True
    with pytest.raises(RuntimeError, match="noch geladen"):
        page.analyse()
    assert session.documents[0].preparation is None and page.status.text() == ""
    _tidy_up(qapp, session, page)


def test_recipient_switch(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                          patient: Patient) -> None:
    """A recipient needs a usable key; a checked key switches the session over, and
    unticking the box ends the recipient mode before the run."""
    session, page = _page(tmp_path, patient)
    session.add(report_pdf)
    page.sharing.setChecked(True)
    page.recipient_source.setText("")
    with pytest.raises(RuntimeError, match="öffentlichen Schlüssel"):
        page.analyse()
    assert page.certificate is None and session.recipient is None
    assert "Schlüsseldatei oder Adresse" in page.recipient_info.text()
    certificate = Certificate(fingerprint="A" * 40, armor="-----BEGIN PGP PUBLIC KEY BLOCK-----")
    page.certificate = certificate  # as after a successful key check
    page.analyse()
    qapp.processEvents()
    assert session.recipient is certificate and session.options.recipient == certificate.armor
    d = session.documents[0]
    assert d.preparation is not None and d.preparation.hits
    page.sharing.setChecked(False)
    page.analyse(again=True)
    qapp.processEvents()
    assert session.recipient is None and session.options.recipient == ""
    assert session.options.recipient_fingerprint == ""
    _tidy_up(qapp, session, page)


def test_error_in_synchronous_run(qapp: QApplication, tmp_path: Path,  # noqa: F811
                                  patient: Patient) -> None:
    """A document that cannot be read raises in synchronous mode instead of opening
    the warning dialog; the status line had already named it."""
    session, page = _page(tmp_path, patient)
    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"kein PDF")
    assert session.add(broken) is not None
    with pytest.raises(DocError):  # docrepro's step says it cannot read the file
        page.analyse()
    assert "kaputt.pdf" in page.status.text()
    _tidy_up(qapp, session, page)
