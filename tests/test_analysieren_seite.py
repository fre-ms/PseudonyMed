"""Step 6 on its own, offscreen with synthetic documents: the option round trip, the
guards before a run, the recipient switch and the re-analysis button."""

from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.gui.analysieren_seite import AnalysierenSeite  # noqa: E402
from pseudonymed.gui.sitzung import Sitzung  # noqa: E402
from pseudonymed.kern.modell import Patient  # noqa: E402
from pseudonymed.pseudonyme.pgp import Zertifikat  # noqa: E402
from tests.test_gui import qapp  # noqa: E402, F401

# the run options the page owns, in their default state and ticked the other way round
STANDARD = {"datumsform": "jahr", "codes": True, "unlesbar": "schwaerzen", "fachbegriffe": True,
            "identifikatoren": True, "medikamente_schwaerzen": False,
            "dosierungen_schwaerzen": False, "laborwerte_schwaerzen": False,
            "messwerte_schwaerzen": False, "unscharf_text": True}
UMGEKEHRT = {"datumsform": "alter", "codes": False, "unlesbar": "lassen", "fachbegriffe": False,
             "identifikatoren": False, "medikamente_schwaerzen": True,
             "dosierungen_schwaerzen": True, "laborwerte_schwaerzen": True,
             "messwerte_schwaerzen": True, "unscharf_text": False}


def _seite(tmp_path: Path, patient: Patient | None = None) -> tuple[Sitzung, AnalysierenSeite]:
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    sitzung.patient = patient
    seite = AnalysierenSeite(sitzung)
    seite.synchron = True
    return sitzung, seite


def _optionen(sitzung: Sitzung) -> dict[str, object]:
    return {name: getattr(sitzung.optionen, name) for name in STANDARD}


def _umschalten(seite: AnalysierenSeite, umgekehrt: bool) -> None:
    """Tick every option the other way round, or back to the defaults."""
    seite.datum.setCurrentIndex(1 if umgekehrt else 0)
    for kasten in (seite.codes, seite.unlesbar, seite.fachbegriffe, seite.identifikatoren,
                   seite.unscharf):
        kasten.setChecked(not umgekehrt)
    for kasten in (seite.medikamente, seite.dosierungen, seite.laborwerte, seite.messwerte):
        kasten.setChecked(umgekehrt)


def _aufraeumen(app: QApplication, sitzung: Sitzung, seite: AnalysierenSeite) -> None:
    """Close the session and let Qt delete the page now: a page still alive at interpreter
    exit (a traceback keeps it, for one) would be deleted under the application."""
    sitzung.schliessen()
    seite.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_optionen_rundlauf(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                           patient: Patient) -> None:
    """Every tick box and the date form reach the session's options when a run starts,
    and the defaults come back the same way with the re-analysis."""
    sitzung, seite = _seite(tmp_path, patient)
    assert sitzung.fuege_hinzu(befund) is not None
    assert _optionen(sitzung) == STANDARD
    ner_vorher = seite.ner.isChecked()
    signale: list[bool] = []
    seite.analysiert.connect(lambda: signale.append(True))
    _umschalten(seite, True)
    seite.ner.setChecked(not ner_vorher)
    seite.knopf.click()
    qapp.processEvents()
    assert _optionen(sitzung) == UMGEKEHRT and sitzung.optionen.ner is (not ner_vorher)
    assert signale == [True] and "Fertig: 1" in seite.status.text()
    d = sitzung.dokumente[0]
    assert d.vorbereitung is not None and d.vorbereitung.fundstellen
    assert any(f.pseudonym.startswith("[Alter ") for f in d.vorbereitung.fundstellen)
    _umschalten(seite, False)
    seite.ner.setChecked(ner_vorher)
    seite.erneut_knopf.click()
    qapp.processEvents()
    assert _optionen(sitzung) == STANDARD and sitzung.optionen.ner is ner_vorher
    assert signale == [True, True]
    assert any(f.pseudonym.startswith("[geb. ") for f in d.vorbereitung.fundstellen)
    _aufraeumen(qapp, sitzung, seite)


def test_erneut_analysieren_knopf(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                                  patient: Patient) -> None:
    """„Analysieren“ leaves a document alone that is analysed and not yet exported;
    „Erneut analysieren“ runs it again and keeps the decisions of the review."""
    sitzung, seite = _seite(tmp_path, patient)
    sitzung.fuege_hinzu(befund)
    seite.knopf.click()
    qapp.processEvents()
    d = sitzung.dokumente[0]
    erste = d.vorbereitung
    assert erste is not None and "Fertig: 1" in seite.status.text()
    erste.verwerfe(next(f for f in erste.fundstellen if f.regel == "zuweiser1.name"))
    seite.knopf.click()  # nothing queued: the document waits for its review and export
    qapp.processEvents()
    assert d.vorbereitung is erste and "Fertig: 0" in seite.status.text()
    assert seite.fortschritt.maximum() == 1 and seite.fortschritt.value() == 1
    seite.erneut_knopf.click()
    qapp.processEvents()
    assert d.vorbereitung is not None and d.vorbereitung is not erste
    assert "Fertig: 1" in seite.status.text() and seite.knopf.isEnabled()
    assert [f.regel for f in d.vorbereitung.verworfen] == ["zuweiser1.name"]
    assert "Treffer" in seite.liste.item(0).text()
    _aufraeumen(qapp, sitzung, seite)


def test_wachen_vor_dem_lauf(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                             patient: Patient) -> None:
    """Without a patient, without documents or while they are still loading nothing runs;
    the synchronous mode raises what the dialog would say."""
    sitzung, seite = _seite(tmp_path)
    with pytest.raises(RuntimeError, match="Patienten"):
        seite.analysieren()
    sitzung.patient = patient
    with pytest.raises(RuntimeError, match="Dokumente hinzufügen"):
        seite.analysieren()
    sitzung.fuege_hinzu(befund)
    seite.laedt = lambda: True
    with pytest.raises(RuntimeError, match="noch geladen"):
        seite.analysieren()
    assert sitzung.dokumente[0].vorbereitung is None and seite.status.text() == ""
    _aufraeumen(qapp, sitzung, seite)


def test_empfaenger_schalter(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                             patient: Patient) -> None:
    """A recipient needs a usable key; a checked key switches the session over, and
    unticking the box ends the recipient mode before the run."""
    sitzung, seite = _seite(tmp_path, patient)
    sitzung.fuege_hinzu(befund)
    seite.weitergabe.setChecked(True)
    seite.empfaenger_quelle.setText("")
    with pytest.raises(RuntimeError, match="öffentlichen Schlüssel"):
        seite.analysieren()
    assert seite.zertifikat is None and sitzung.empfaenger is None
    assert "Schlüsseldatei oder Adresse" in seite.empfaenger_info.text()
    zertifikat = Zertifikat(fingerabdruck="A" * 40, armor="-----BEGIN PGP PUBLIC KEY BLOCK-----")
    seite.zertifikat = zertifikat  # as after a successful key check
    seite.analysieren()
    qapp.processEvents()
    assert sitzung.empfaenger is zertifikat and sitzung.optionen.empfaenger == zertifikat.armor
    d = sitzung.dokumente[0]
    assert d.vorbereitung is not None and d.vorbereitung.fundstellen
    seite.weitergabe.setChecked(False)
    seite.analysieren(erneut=True)
    qapp.processEvents()
    assert sitzung.empfaenger is None and sitzung.optionen.empfaenger == ""
    assert sitzung.optionen.empfaenger_fingerabdruck == ""
    _aufraeumen(qapp, sitzung, seite)


def test_fehler_im_synchronen_lauf(qapp: QApplication, tmp_path: Path,  # noqa: F811
                                   patient: Patient) -> None:
    """A document that cannot be read raises in synchronous mode instead of opening
    the warning dialog; the status line had already named it."""
    sitzung, seite = _seite(tmp_path, patient)
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein PDF")
    assert sitzung.fuege_hinzu(kaputt) is not None
    with pytest.raises(pymupdf.FileDataError):
        seite.analysieren()
    assert "kaputt.pdf" in seite.status.text()
    _aufraeumen(qapp, sitzung, seite)
