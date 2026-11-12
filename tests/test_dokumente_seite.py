"""The documents step on its own: files dropped on the list, the working states of the
entries, removal, loading in the background and the paper format for images.  Qt runs
offscreen; every file is synthetic (Faker de_DE)."""

from __future__ import annotations

import gc
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.gui import dokumente_seite as modul  # noqa: E402
from pseudonymed.gui.arbeiter import Arbeiter  # noqa: E402
from pseudonymed.gui.dokumente_seite import Ablage, DokumenteSeite  # noqa: E402
from pseudonymed.gui.sitzung import Sitzung  # noqa: E402
from pseudonymed.kern.modell import Patient  # noqa: E402
from tests.synthetisch import erzeuge_transkript  # noqa: E402


@pytest.fixture
def qapp() -> Iterator[QApplication]:
    """The application; afterwards the page of the test is collected on the main thread.
    A traceback (``pytest.raises``) keeps a page alive in a reference cycle; were that cycle
    collected later inside a worker thread, Shiboken would defer the C++ deletion to the
    main thread and crash on it."""
    app = QApplication.instance()
    app = app if isinstance(app, QApplication) else QApplication([])
    yield app
    gc.collect()
    app.processEvents()


class _NieGestartet(Arbeiter):
    """A worker whose thread never runs: the queue stays as it is."""

    def start(self, *_a: object) -> None:  # noqa: N802 - Qt override
        pass


class _Meldungen:
    """Stand-in for the dialogs of the page: records instead of blocking."""

    def __init__(self, dateien: list[str] | None = None) -> None:
        self.gezeigt: list[tuple[object, ...]] = []
        self.dateien = dateien or []

    def warning(self, *a: object) -> None:
        self.gezeigt.append(a)

    def getOpenFileNames(self, *_a: object) -> tuple[list[str], str]:  # noqa: N802
        return self.dateien, ""


def _mime(pfade: list[Path], fremd: bool = False) -> QMimeData:
    """Mime data of a drag from the file manager, optionally with a remote URL in it."""
    daten = QMimeData()
    urls = [QUrl.fromLocalFile(str(p)) for p in pfade]
    if fremd:
        urls.append(QUrl("https://example.invalid/befund.pdf"))
    daten.setUrls(urls)
    return daten


def _drag[E: QDropEvent](klasse: type[E], daten: QMimeData) -> E:
    """A drag event over the list carrying ``daten``, not yet accepted by anyone."""
    ereignis = klasse(QPointF(5, 5), Qt.DropAction.CopyAction, daten, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    ereignis.ignore()
    return ereignis


def _scan(befund: Path, ziel: Path) -> Path:
    """Image-only copy of the synthetic report: every page a picture, no text layer."""
    with pymupdf.open(befund) as doc, pymupdf.open() as scan:
        for page in doc:
            pix = page.get_pixmap(dpi=100, alpha=False)
            neu = scan.new_page(width=page.rect.width, height=page.rect.height)
            neu.insert_image(neu.rect, stream=pix.tobytes("png"))
        scan.save(ziel)
    return ziel


def _warte(qapp: QApplication, bedingung: Callable[[], bool], sekunden: float = 120.0) -> None:
    """Pump the event loop until ``bedingung`` holds (worker threads report through it)."""
    ende = time.monotonic() + sekunden
    while not bedingung():
        assert time.monotonic() < ende, "Zeitüberschreitung beim Warten auf den Arbeiter"
        qapp.processEvents()
        time.sleep(0.01)


def _seite(tmp_path: Path, patient: Patient | None = None) -> DokumenteSeite:
    """A documents page with its own session, working inline (no threads)."""
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    sitzung.patient = patient
    seite = DokumenteSeite(sitzung)
    seite.synchron = True
    return seite


def test_ablage_nimmt_lokale_dateien(qapp: QApplication, tmp_path: Path, befund: Path) -> None:
    """A drag with URLs is accepted, one without is not; the drop emits the local paths
    only (a remote URL is dropped, and a drop with none of them is refused)."""
    ablage = Ablage()
    empfangen: list[list[Path]] = []
    ablage.abgelegt.connect(empfangen.append)
    notiz = tmp_path / "notiz.txt"
    mit_dateien, ohne, gemischt, nur_fremd = (
        _mime([befund]), QMimeData(), _mime([befund, notiz], fremd=True), _mime([], fremd=True))
    betreten = _drag(QDragEnterEvent, mit_dateien)
    ablage.dragEnterEvent(betreten)
    assert betreten.isAccepted()
    bewegt = _drag(QDragMoveEvent, mit_dateien)
    ablage.dragMoveEvent(bewegt)
    assert bewegt.isAccepted()
    leer = _drag(QDragEnterEvent, ohne)
    ablage.dragEnterEvent(leer)
    assert not leer.isAccepted()
    abgelegt = _drag(QDropEvent, gemischt)
    ablage.dropEvent(abgelegt)
    assert abgelegt.isAccepted() and empfangen == [[befund, notiz]]
    verweigert = _drag(QDropEvent, nur_fremd)
    ablage.dropEvent(verweigert)
    assert not verweigert.isAccepted() and len(empfangen) == 1


def test_liste_zeigt_die_arbeitszustaende(qapp: QApplication, tmp_path: Path, befund: Path,
                                          patient: Patient) -> None:
    """A text-layer PDF, a folder with a transcript, an unknown extension and a scan
    arrive together; each entry and the status line name the state, and the states
    move on through analysis, a restored working state, export and an error."""
    seite = _seite(tmp_path, patient)
    gemeldet: list[int] = []
    seite.geladen.connect(lambda: gemeldet.append(1))
    ordner = tmp_path / "ordner"
    ordner.mkdir()
    erzeuge_transkript(ordner / "gespraech.txt", patient)
    (ordner / "unterordner").mkdir()  # folders inside a folder are not descended into
    scan = _scan(befund, tmp_path / "scan.pdf")
    seite.hinzufuegen([befund, ordner, tmp_path / "egal.xyz", scan])
    assert seite.liste.count() == 3 and len(seite.sitzung.dokumente) == 3
    texte = [seite.liste.item(i).text() for i in range(3)]
    assert texte[0].startswith("befund.pdf  –  ") and "Seite(n) mit Textebene" in texte[0]
    assert texte[1] == "gespraech.txt  –  Textdatei"
    assert "ohne Textebene: unten aufbereiten" in texte[2]
    assert gemeldet and not seite.laedt
    assert seite.status.text() == "3 Dokument(e), 1 mit Seiten zum Aufbereiten"
    assert seite.aufbereiten.baum.topLevelItemCount() == 1

    pdf, text, _ = seite.sitzung.dokumente
    seite.sitzung.analysiere(pdf)
    seite.sitzung.analysiere(text)
    seite.aktualisiere()
    assert "  –  analysiert, " in seite.liste.item(0).text()
    assert "  –  Text, " in seite.liste.item(1).text()
    assert seite.liste.item(0).text().endswith(" Treffer")
    pdf.arbeitsstand = tmp_path / "befund.arbeitsstand.json"  # restored from a saved state
    seite.aktualisiere()
    assert "  –  Arbeitsstand, " in seite.liste.item(0).text()
    pdf.arbeitsstand = None
    ergebnis = seite.sitzung.exportiere(pdf)
    seite.aktualisiere()
    assert ergebnis.freigegeben and seite.liste.item(0).text().endswith("  –  freigegeben")
    pdf.ergebnis = replace(ergebnis, ziel_pdf=None, ziel_txt=None)  # as after a failed re-check
    seite.aktualisiere()
    assert seite.liste.item(0).text().endswith("  –  nicht freigegeben")
    pdf.fehler = "Testfehler"
    seite.aktualisiere()
    assert seite.liste.item(0).text().endswith("  –  Fehler: Testfehler")
    seite.sitzung.schliessen()


def test_entfernen_raeumt_sitzung_und_aufbereiten(qapp: QApplication, tmp_path: Path,
                                                  befund: Path) -> None:
    """Removing needs a selection; the removed document leaves the session, and the
    clean-up panel below forgets its pages."""
    seite = _seite(tmp_path)
    scan = _scan(befund, tmp_path / "scan.pdf")
    seite.hinzufuegen([befund, scan])
    assert seite.aufbereiten.baum.topLevelItemCount() == 1
    seite.entfernen()  # nothing selected: nothing happens
    assert seite.liste.count() == 2
    seite.liste.item(1).setSelected(True)
    seite.entfernen()
    assert seite.liste.count() == 1
    assert [d.pfad for d in seite.sitzung.dokumente] == [befund]
    assert seite.aufbereiten.baum.topLevelItemCount() == 0
    assert "nichts aufzubereiten" in seite.aufbereiten.hinweis.text()
    assert seite.status.text() == "1 Dokument(e)"
    seite.sitzung.schliessen()


def test_wartende_dokumente(qapp: QApplication, tmp_path: Path, befund: Path,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    """While a document waits for the worker, its entry and the status line say so;
    removing it takes it out of the queue, and the dialog of the file chooser feeds
    the same path as a drop."""
    monkeypatch.setattr(modul, "Arbeiter", _NieGestartet)
    monkeypatch.setattr(modul, "QFileDialog", _Meldungen([str(befund)]))
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    seite = DokumenteSeite(sitzung)
    seite.dateien_waehlen()
    assert seite.liste.count() == 1 and seite.laedt
    assert seite.liste.item(0).text() == "befund.pdf  –  wird geladen …"
    assert seite.status.text() == "1 Dokument(e) – lade …"
    seite.liste.item(0).setSelected(True)
    seite.entfernen()
    assert seite.liste.count() == 0 and sitzung.dokumente == [] and not seite.laedt
    assert seite.status.text() == "0 Dokument(e)"


def test_fehler_beim_oeffnen_inline(qapp: QApplication, tmp_path: Path) -> None:
    """In the synchronous test mode a file that cannot be opened raises; the entry
    keeps the error and the queue is empty afterwards."""
    seite = _seite(tmp_path)
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein PDF")
    with pytest.raises(Exception, match="(?i)kaputt|document|pdf|file"):
        seite.hinzufuegen([kaputt])
    d = seite.sitzung.dokumente[0]
    assert d.fehler and d.vorlage is None and not seite.laedt
    seite.aktualisiere()
    assert seite.liste.item(0).text().startswith("kaputt.pdf  –  Fehler: ")


def test_laden_im_hintergrund(qapp: QApplication, tmp_path: Path, befund: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the synchronous mode the documents are opened in a worker thread one
    after the other; a failure is shown in a dialog (replaced here) and the queue
    moves on to the next file."""
    dialoge = _Meldungen()
    monkeypatch.setattr(modul, "QMessageBox", dialoge)
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    seite = DokumenteSeite(sitzung)
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein PDF")
    fertig: list[int] = []
    seite.geladen.connect(lambda: fertig.append(1))
    seite.hinzufuegen([kaputt, befund])
    assert seite.laedt and seite.status.text().endswith(" – lade …")
    _warte(qapp, lambda: bool(fertig))
    _warte(qapp, lambda: not any(a.isRunning() for a in seite.findChildren(Arbeiter)))
    assert not seite.laedt
    assert len(dialoge.gezeigt) == 1 and dialoge.gezeigt[0][1] == "kaputt.pdf"
    erstes, zweites = sitzung.dokumente
    assert erstes.fehler and erstes.vorlage is None
    assert zweites.vorlage is not None and zweites.vorlage.seitenzahl == 2
    assert seite.liste.item(0).text().startswith("kaputt.pdf  –  Fehler: ")
    assert "Seite(n) mit Textebene" in seite.liste.item(1).text()
    sitzung.schliessen()


def test_papierformat_laedt_bilder_neu(qapp: QApplication, tmp_path: Path,
                                       befund: Path) -> None:
    """Changing the paper size re-opens the images that are already in (PDFs are left
    alone) and keeps the choice in the session options."""
    seite = _seite(tmp_path)
    foto = tmp_path / "foto.png"
    with pymupdf.open(befund) as doc:
        doc[0].get_pixmap(dpi=60).save(foto)
    seite.hinzufuegen([foto, befund])
    bild, pdf = seite.sitzung.dokumente
    assert bild.vorlage is not None and pdf.vorlage is not None
    alt_bild, alt_pdf = bild.vorlage, pdf.vorlage
    hoehe_a4 = alt_bild.doc[0].rect.height
    seite.papier.setCurrentIndex(seite.papier.findData("a5"))
    assert seite.sitzung.optionen.papierformat == "a5"
    assert bild.vorlage is not None and bild.vorlage is not alt_bild
    assert pdf.vorlage is alt_pdf
    assert bild.vorlage.doc[0].rect.height < hoehe_a4
    assert "1 Seite(n)" in seite.liste.item(0).text()
    seite.sitzung.schliessen()
