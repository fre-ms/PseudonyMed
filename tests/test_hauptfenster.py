"""The main window's step switching, driven offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.gui.hauptfenster import Hauptfenster  # noqa: E402
from pseudonymed.gui.sitzung import Sitzung  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_schritte_werden_beim_betreten_aufgefrischt(qapp: QApplication, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Entering a step refreshes it when it shows state other steps change; the patient,
    document and review steps keep their own state."""
    fenster = Hauptfenster(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    betreten: list[str] = []
    seiten = {"patient": fenster.patient, "identifikatoren": fenster.identifikatoren,
              "vokabular": fenster.vokabular, "dokumente": fenster.dokumente,
              "texterkennung": fenster.texterkennung, "analysieren": fenster.analysieren,
              "pruefen": fenster.pruefen, "kontrolle": fenster.kontrolle,
              "export": fenster.export, "rueckersetzen": fenster.rueckersetzen,
              "datenbank": fenster.datenbank}
    for name, seite in seiten.items():
        monkeypatch.setattr(seite, "aktualisiere", lambda name=name: betreten.append(name),
                            raising=False)
    assert fenster.tabs.currentIndex() == 0 and fenster.aktuelle_seite() is fenster.patient
    for i in range(1, fenster.tabs.count()):
        fenster.tabs.setCurrentIndex(i)
    fenster.zeige_seite(fenster.patient)
    assert betreten == ["identifikatoren", "vokabular", "texterkennung", "analysieren",
                        "kontrolle", "export", "rueckersetzen", "datenbank"]
    assert fenster.seite(-1) is None and fenster.seite(fenster.tabs.count()) is None
    fenster._tab_gewechselt(fenster.tabs.count())  # out of range: nothing happens
    assert len(betreten) == 8
    fenster.close()
