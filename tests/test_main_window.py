"""The main window's step switching, driven offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.gui.main_window import MainWindow  # noqa: E402
from pseudonymed.gui.session import Session  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_steps_are_refreshed_on_entering(qapp: QApplication, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Entering a step refreshes it when it shows state other steps change; the patient,
    document and review steps keep their own state."""
    window = MainWindow(Session(store_path=tmp_path / "s.sqlite"))
    entered: list[str] = []
    pages = {"patient": window.patient, "identifikatoren": window.identifiers,
             "vokabular": window.vocabulary, "dokumente": window.documents,
             "texterkennung": window.ocr, "analysieren": window.analyse,
             "pruefen": window.check, "kontrolle": window.control,
             "export": window.export, "rueckersetzen": window.reidentify,
             "datenbank": window.database}
    for name, page in pages.items():
        monkeypatch.setattr(page, "refresh", lambda name=name: entered.append(name),
                            raising=False)
    assert window.tabs.currentIndex() == 0 and window.current_page() is window.patient
    for i in range(1, window.tabs.count()):
        window.tabs.setCurrentIndex(i)
    window.show_page(window.patient)
    assert entered == ["identifikatoren", "vokabular", "texterkennung", "analysieren",
                       "kontrolle", "export", "rueckersetzen", "datenbank"]
    assert window.page(-1) is None and window.page(window.tabs.count()) is None
    window._tab_changed(window.tabs.count())  # out of range: nothing happens
    assert len(entered) == 8
    window.close()
