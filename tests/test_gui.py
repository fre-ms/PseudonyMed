"""The desktop interface, driven offscreen: session model and the five pages in sequence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGraphicsView  # noqa: E402

from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.gui.hauptfenster import Hauptfenster  # noqa: E402
from pseudonymed.gui.patient_seite import _personen  # noqa: E402
from pseudonymed.gui.sitzung import Sitzung  # noqa: E402
from pseudonymed.kern.modell import Patient  # noqa: E402
from tests.test_ablauf import gefundene_klardaten  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_personen_aus_text() -> None:
    p = _personen("Dr. med. Anna Schmidt\nMüller, Hans\n\nProf. Dr. Berg, Eva")
    assert [(x.titel, x.vorname, x.nachname) for x in p] == [
        ("Dr. med.", "Anna", "Schmidt"), ("", "Hans", "Müller"), ("Prof. Dr.", "Eva", "Berg")]


def test_sitzung_ohne_oberflaeche(tmp_path: Path, befund: Path, patient: Patient) -> None:
    s = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    s.patient = patient
    assert s.fuege_hinzu(befund) is not None
    assert s.fuege_hinzu(befund) is None  # no duplicates
    assert s.fuege_hinzu(tmp_path / "notiz.xyz") is None
    d = s.dokumente[0]
    assert d.zustand == "noch nicht analysiert"
    s.analysiere(d)
    assert d.vorbereitung is not None and "Treffer" in d.zustand
    erg = s.exportiere(d)
    assert erg.freigegeben and d.zustand == "freigegeben"
    s.schliessen()


def test_sitzung_ueber_threads(tmp_path: Path, befund: Path, patient: Patient) -> None:
    """The desktop runs analysis and export in different worker threads; the store
    opened by the first must still serve the second (and be closed from a third).
    The first thread is kept alive meanwhile, otherwise the OS may hand its thread
    id to the second and hide the problem."""
    import threading

    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    sitzung.patient = patient
    d = sitzung.fuege_hinzu(befund)
    fehler: list[BaseException] = []
    analysiert = threading.Event()
    exportiert = threading.Event()

    def analyse() -> None:
        try:
            sitzung.analysiere(d)
        except BaseException as e:  # noqa: BLE001
            fehler.append(e)
        finally:
            analysiert.set()
        exportiert.wait(timeout=300)  # stay alive while the export runs

    def export() -> None:
        try:
            sitzung.exportiere(d)
        except BaseException as e:  # noqa: BLE001
            fehler.append(e)
        finally:
            exportiert.set()

    t1 = threading.Thread(target=analyse)
    t1.start()
    assert analysiert.wait(timeout=300) and not fehler, fehler
    t2 = threading.Thread(target=export)
    t2.start()
    t2.join(timeout=300)
    t1.join(timeout=60)
    assert not fehler, fehler
    assert d.ergebnis is not None and d.ergebnis.freigegeben
    t3 = threading.Thread(target=sitzung.schliessen)
    t3.start()
    t3.join(timeout=60)


def test_fenster_rundlauf(qapp: QApplication, tmp_path: Path, befund: Path,
                          patient: Patient) -> None:
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    fenster.dokumente.synchron = True
    fenster.analysieren.synchron = True
    fenster.export.synchron = True

    # 1 patient
    fenster.patient.fuelle(patient)
    assert fenster.patient.f["nachname"].text() == patient.nachname
    assert fenster.patient.f["hausnummer"].text() and fenster.patient.f["strasse"].text()
    fenster.patient.uebernehmen()
    assert sitzung.patient is not None and sitzung.patient.vollname == patient.vollname
    assert sitzung.patient.strasse == patient.strasse  # street and number joined again
    assert sitzung.patient.zuweiser[0].nachname == patient.zuweiser[0].nachname
    assert fenster.aktuelle_seite() is fenster.dokumente

    # 2 documents: opened at once; a text-layer PDF has nothing to clean up
    fenster.dokumente.hinzufuegen([befund])
    assert fenster.dokumente.liste.count() == 1
    d = sitzung.dokumente[0]
    assert d.vorlage is not None and d.vorlage.kandidaten == [] and not fenster.dokumente.laedt
    assert "nichts aufzubereiten" in fenster.dokumente.aufbereiten.hinweis.text()
    fenster.dokumente.weiter_knopf.click()
    assert fenster.aktuelle_seite() is fenster.texterkennung

    # 3 text recognition: a text-layer PDF is read without OCR, its text is shown read-only
    te = fenster.texterkennung
    te.synchron = True
    te.starten()
    qapp.processEvents()
    assert d.lesung is not None and d.lesung.ocr_seiten == [] and te.text.isReadOnly()
    assert "Diagnosen" in te.text.toPlainText()
    te.weiter_knopf.click()
    assert fenster.aktuelle_seite() is fenster.analysieren

    # 4 analyse with the settings of that step
    fenster.analysieren.analysieren()
    qapp.processEvents()
    assert d.vorbereitung is not None and d.vorbereitung.fundstellen and d.lesung is None
    assert fenster.aktuelle_seite() is fenster.pruefen
    assert "Fertig" in fenster.analysieren.status.text()

    # 5 review: drop one hit, draw one box; medical terms are marked by default
    pr = fenster.pruefen
    assert pr.baum.topLevelItemCount() == 1 and pr.dokument is d
    assert fenster.analysieren.fachbegriffe.isChecked()
    assert d.vorbereitung.fachbegriffe_anzahl >= 3  # Hypertonie, Ramipril, Atorvastatin ...
    assert d.vorbereitung.fachbegriffe_rects(0)
    assert pr.fach_anzeigen.isChecked() and "Fachbegriffe markiert" in pr.hinweis.text()
    pr.fach_anzeigen.setChecked(False)
    pr.fach_anzeigen.setChecked(True)
    from pseudonymed.erkennung import ner as nermodul

    assert fenster.analysieren.ner.isChecked() == nermodul.verfuegbar()
    vorher = len(pr.fundstellen())
    assert pr.tabelle.rowCount() == vorher > 5
    ziel = next(f for f in pr.fundstellen() if f.regel == "zuweiser1.name")
    pr.waehle(ziel)
    assert "verwerfen" in pr.verwerfen.text()
    pr.verwerfe()
    assert len(pr.fundstellen()) == vorher - 1 and ziel in d.vorbereitung.verworfen
    pr.waehle(ziel)  # a discarded hit stays visible (green) and can be taken back
    assert "wiederherstellen" in pr.verwerfen.text()
    pr.verwerfe()
    assert len(pr.fundstellen()) == vorher and ziel not in d.vorbereitung.verworfen
    pr.waehle(ziel)
    pr.verwerfe()
    pr.kasten((56, 300, 400, 320), "[Kasten]")
    assert any(f.quelle == "manuell" for f in pr.fundstellen())
    pr.maske_modus.setChecked(True)
    assert pr.ansicht.masken_modus
    pr.kasten((56, 330, 400, 345))  # mask mode: no dialog, no label
    assert any(f.regel == "maske" for f in pr.fundstellen())
    pr.maske_modus.setChecked(False)
    assert pr.tabelle.rowCount() == len(pr.fundstellen())
    # a real mouse drag draws a box with the replacement text from the field, no dialog
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    pr.ersatztext.setText("[Feld]")
    vp = pr.ansicht.viewport()
    vorher_kaesten = len(pr.fundstellen())
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(80, 296))  # over the salutation line (page points 212-228)
    QTest.mouseMove(vp, QPoint(300, 316))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(300, 316))
    assert len(pr.fundstellen()) == vorher_kaesten + 1
    assert pr.gewaehlt is not None and pr.gewaehlt.pseudonym == "[Feld]"
    assert "Kasten gesetzt" in pr.hinweis.text()
    # Shift + drag pans instead of drawing
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                     QPoint(80, 600))
    assert pr.ansicht.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
    QTest.mouseMove(vp, QPoint(120, 640))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                       QPoint(120, 640))
    assert pr.ansicht.dragMode() == QGraphicsView.DragMode.NoDrag
    assert len(pr.fundstellen()) == vorher_kaesten + 1
    assert pr.merken.isChecked() and sitzung.optionen.verworfene_merken

    # 6 export: the step re-checks first (traffic light), then copies the checked files
    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.farbe in ("gruen", "gelb"), fenster.export.ampel_text.text()
    assert d.vorpruefung_aktuell and fenster.export.knopf.isEnabled()
    pruefprotokoll = fenster.export.ausgabe.toPlainText()
    assert "Erneut geprüft nach dem Prüfschritt" in pruefprotokoll
    assert "Bewusst belassen" in pruefprotokoll and "zuweiser1.name" in pruefprotokoll
    fenster.export.exportieren()
    qapp.processEvents()
    text = fenster.export.ausgabe.toPlainText()
    assert "Gegenprobe: bestanden" in text, text
    assert d.ergebnis is not None and d.ergebnis.freigegeben and d.vorbereitung.abgeschlossen
    assert (tmp_path / "aus" / "befund_pseudonym.pdf").exists()
    sidecar = (tmp_path / "aus" / "befund_pseudonym.txt").read_text(encoding="utf-8")
    assert "[Kasten]" in sidecar and "[Feld]" in sidecar
    assert (tmp_path / "aus" / "befund_pseudonym.bericht.json").exists()
    assert patient.nachname not in sidecar
    assert patient.zuweiser[0].nachname in sidecar  # deliberately kept

    # the decision to keep the referrer was stored for this patient
    from pseudonymed.pseudonyme.speicher import Pseudonymspeicher

    with Pseudonymspeicher(tmp_path / "s.sqlite") as sp:
        assert any(r == "zuweiser1.name" for r, _t in sp.verworfene(patient))

    # 7 re-identify
    fenster.zeige_seite(fenster.rueckersetzen)
    rs = fenster.rueckersetzen
    assert rs.person.count() == 1
    k = d.ergebnis.kennung
    rs.eingabe.setPlainText(f"Patient {k} wurde untersucht; Angehörige {k}-1 war dabei.")
    rs.rueckersetzen()
    aus = rs.ausgabe.toPlainText()
    assert patient.vollname in aus and patient.angehoerige[0].vollname in aus
    assert "nichts offen" in rs.hinweis.text()
    assert gefundene_klardaten(text, patient) == []  # the report shows no clear data
    fenster.close()


def test_fenster_textdatei(qapp: QApplication, tmp_path: Path, patient: Patient,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript goes through the same five steps; the review shows text, not a page.
    The discarded guess ends up in the exported vocabulary, which the analysis step
    imports into the user's own list."""
    from PySide6.QtGui import QTextCursor

    from pseudonymed.erkennung import fachbegriffe as fachmodul
    from tests.synthetisch import erzeuge_transkript, verschrieben

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    fachmodul.standard.cache_clear()
    quelle = erzeuge_transkript(tmp_path / "gespraech.txt", patient)
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    fenster.dokumente.synchron = True
    fenster.analysieren.synchron = True
    fenster.export.synchron = True
    fenster.pruefen.synchron = True
    fenster.patient.fuelle(patient)
    fenster.patient.uebernehmen()
    fenster.dokumente.hinzufuegen([quelle, tmp_path / "egal.xyz"])
    assert fenster.dokumente.liste.count() == 1
    assert sitzung.dokumente[0].vorlage is None  # text files are not opened as documents
    fenster.texterkennung.synchron = True
    fenster.texterkennung.starten()
    assert sitzung.dokumente[0].lesung is None
    assert "Textdatei" in fenster.texterkennung.hinweis.text()
    assert fenster.analysieren.unscharf.isChecked()
    fenster.analysieren.analysieren()
    qapp.processEvents()
    d = sitzung.dokumente[0]
    assert d.vorbereitung is not None and d.vorbereitung.ist_text
    assert "Text, " in fenster.dokumente.liste.item(0).text()

    pr = fenster.pruefen
    assert pr.stapel.currentWidget() is pr.textansicht and not pr.markieren.isHidden()
    assert pr.baum.topLevelItem(0).child(0).text(0) == "Text"
    assert pr.textansicht.toPlainText() == d.vorbereitung.text
    assert pr.tabelle.rowCount() == len(pr.fundstellen()) > 5
    assert any(f.quelle == "unscharf" for f in pr.fundstellen())
    assert "Unscharfe Suche" in pr.hinweis.text()
    unscharf = next(f for f in pr.fundstellen() if f.quelle == "unscharf")
    pr.waehle(unscharf)
    assert pr.textansicht.textCursor().position() == unscharf.start
    pr.verwerfe()
    assert unscharf not in pr.fundstellen()
    with pytest.raises(ValueError):
        pr.markiere_auswahl("[X]")  # nothing selected
    text = d.vorbereitung.text
    a = text.index("Wörter vergesse")
    cursor = pr.textansicht.textCursor()
    cursor.setPosition(a)
    cursor.setPosition(a + len("Wörter vergesse"), QTextCursor.MoveMode.KeepAnchor)
    pr.textansicht.setTextCursor(cursor)
    pr.markiere_auswahl("[Symptom]")
    assert any(f.quelle == "manuell" and f.pseudonym == "[Symptom]" for f in pr.fundstellen())
    pr.fach_anzeigen.setChecked(False)
    pr.fach_anzeigen.setChecked(True)

    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.farbe in ("gruen", "gelb")
    fenster.export.exportieren()
    qapp.processEvents()
    protokoll = fenster.export.ausgabe.toPlainText()
    assert "Gegenprobe: bestanden" in protokoll and "Freigegeben" in protokoll, protokoll
    ziel = tmp_path / "aus" / "gespraech_pseudonym.txt"
    aus = ziel.read_text(encoding="utf-8")
    assert "[Symptom]" in aus and verschrieben(patient.nachname) in aus  # discarded = kept
    assert patient.nachname not in aus and gefundene_klardaten(protokoll, patient) == []
    # the discarded fuzzy hit is a guess, so it went into the vocabulary file …
    vokabular = tmp_path / "aus" / "gespraech_pseudonym.vokabular.txt"
    assert d.ergebnis.ziel_vokabular == vokabular and "Vokabular:" in protokoll
    zeilen = [z for z in vokabular.read_text(encoding="utf-8").splitlines()
              if z and not z.startswith("#")]
    assert zeilen == [unscharf.text] and patient.nachname not in zeilen
    # … which the vocabulary step merges into the table
    assert not fachmodul.standard().ist_fachbegriff(unscharf.text)
    fenster.vokabular.synchron = True
    assert fenster.vokabular.importiere_datei(vokabular) == 1
    assert fachmodul.standard().ist_fachbegriff(unscharf.text)
    assert "1 neue von 1" in fenster.vokabular.status.text()
    assert fenster.vokabular.importiere_datei(vokabular) == 0  # already known
    fenster.analysieren.aktualisiere()
    assert "Nutzervokabular" in fenster.analysieren.vokabular_info.text()
    fenster.close()
    fachmodul.standard.cache_clear()


def test_fenster_laesst_sich_verkleinern(qapp: QApplication, tmp_path: Path) -> None:
    """Every step sits in a scroll area, so the window shrinks instead of refusing."""
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    fenster.show()
    qapp.processEvents()
    assert fenster.minimumSizeHint().width() <= 720
    fenster.resize(720, 480)
    qapp.processEvents()
    assert fenster.width() <= 720 and fenster.height() <= 480
    for i in range(fenster.tabs.count()):
        fenster.tabs.setCurrentIndex(i)
        qapp.processEvents()
        assert fenster.width() <= 720, fenster.tabs.tabText(i)
    assert fenster.aktuelle_seite() is fenster.datenbank
    fenster.close()


def test_selbsttest(qapp: QApplication, capsys: pytest.CaptureFixture[str]) -> None:
    from pseudonymed.gui.app import main

    assert main(["--selbsttest"]) == 0
    assert "Selbsttest bestanden" in capsys.readouterr().out


def test_englische_oberflaeche(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui import uebersetzung

    assert uebersetzung.sprache_bestimmen(["--sprache", "en"]) == "en"
    assert uebersetzung.sprache_bestimmen(["--language=EN-GB"]) == "en-gb"
    assert uebersetzung.ohne_sprachargumente(["x", "--sprache", "en", "y"]) == ["x", "y"]
    monkeypatch.setenv("PSEUDONYMED_SPRACHE", "en")
    assert uebersetzung.sprache_bestimmen([]) == "en"
    uebersetzer = uebersetzung.installiere(qapp, "en_gb")
    assert uebersetzer is not None
    try:
        fenster = Hauptfenster(Sitzung(speicherpfad=None))
        tabs = [fenster.tabs.tabText(i) for i in range(fenster.tabs.count())]
        assert tabs == ["1 Patient data", "2 Identifiers", "3 Medical vocabulary", "4 Documents",
                    "5 Text recognition", "6 Analysis", "7 Visual review", "8 Control", "9 Export",
                    "10 Re-identification",
                    "11 Database"]
        assert fenster.patient.f["nachname"].placeholderText() == ""
        assert fenster.export.knopf.text() == "Export"
        fenster.close()
    finally:
        qapp.removeTranslator(uebersetzer)
    fenster = Hauptfenster(Sitzung(speicherpfad=None))
    assert fenster.tabs.tabText(3) == "4 Dokumente"
    fenster.close()


@pytest.mark.skipif(not ocrmodul.verfuegbare_backends() or not aufmodul.verfuegbar(),
                    reason="OCR-Backend oder OpenCV fehlt")
def test_fenster_aufbereiten(qapp: QApplication, tmp_path: Path, befund: Path,
                             patient: Patient) -> None:
    """A tilted scan goes through the clean-up step: suggestion shown, user adjusts,
    analysis runs on the corrected page, the review follows."""
    from tests.test_ocr import _scan

    scan = _scan(befund, tmp_path / "scan.pdf", drehung=2.0)
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    fenster.dokumente.synchron = True
    fenster.analysieren.synchron = True
    fenster.export.synchron = True
    fenster.patient.fuelle(patient)
    fenster.patient.uebernehmen()
    fenster.dokumente.hinzufuegen([scan])
    qapp.processEvents()
    d = sitzung.dokumente[0]
    assert d.vorlage is not None and d.vorlage.kandidaten == [0, 1]
    assert "aufbereiten" in fenster.dokumente.liste.item(0).text()
    au = fenster.dokumente.aufbereiten
    assert au.baum.topLevelItemCount() == 1 and au.dokument is d and au.seite == 0
    a = au.einstellung()
    assert a is not None and a.begradigen and abs(abs(a.winkel) - 2.0) < 0.5, a
    assert "begradigt" in au.hinweis.text()
    au.hintergrund.setChecked(True)
    au.maske((56, 60, 300, 90))
    assert d.aufbereitung[0].hintergrund and len(d.aufbereitung[0].masken) == 1
    au.auf_alle_uebertragen()
    assert d.aufbereitung[1].hintergrund
    au.hochskalieren.setCurrentIndex(2)  # 3×
    au.textmodus.setChecked(True)
    au.farbe_entfernen.setChecked(True)
    assert d.aufbereitung[0].hochskalieren == 3 and d.aufbereitung[0].textmodus
    assert d.aufbereitung[0].farbe_entfernen and "Buchstabenmodus" in au.hinweis.text()
    au.hochskalieren.setCurrentIndex(0)
    au.textmodus.setChecked(False)
    # the two views are linked: pan one, the other follows; zoom likewise
    au.nachher.zoom(2.0)
    assert abs(au.vorher.zoomfaktor - au.nachher.zoomfaktor) < 1e-6  # relative to each base
    au.vorher.verticalScrollBar().setValue(au.vorher.verticalScrollBar().maximum() // 2)
    v1 = au.vorher.verticalScrollBar().value() / max(1, au.vorher.verticalScrollBar().maximum())
    v2 = au.nachher.verticalScrollBar().value() / max(1, au.nachher.verticalScrollBar().maximum())
    assert abs(v1 - v2) < 0.02, (v1, v2)
    au.vorher.zoom_zuruecksetzen()
    assert abs(au.nachher.zoomfaktor - 1.0) < 1e-6
    au.ausrichtung_erkennen()
    assert d.aufbereitung[0].drehung == 0
    au.drehe(90)
    assert d.aufbereitung[0].drehung == 90 and not d.aufbereitung[0].masken
    au.drehe(270)
    au.nichts_aufbereiten()
    assert not d.aufbereitung[0].aktiv
    au.vorschlag_wiederherstellen()
    assert d.aufbereitung[0].begradigen
    au.rueckgaengig()  # nothing to undo any more: no error
    # 3 text recognition on the cleaned-up pages, with a correction
    te = fenster.texterkennung
    te.synchron = True
    assert te.engine.currentData() == "auto" and te.weiter_knopf.isEnabled()
    te.starten()
    qapp.processEvents()
    assert d.lesung is not None and d.lesung.ocr_seiten == [1, 2]
    assert te.baum.topLevelItemCount() == 1 and not te.text.isReadOnly()
    ocr = te.text.toPlainText()
    assert "Diagnosen" in ocr
    te.text.setPlainText(ocr.rstrip("\n") + "\nZusatz: Testzeile aus der Korrektur.\n")
    te.text_uebernehmen()
    assert d.lesung.seiten[0].korrektur is not None and "korrigiert" in te.text_hinweis.text()
    fenster.analysieren.analysieren()
    qapp.processEvents()
    assert d.vorbereitung is not None and d.vorlage is None and d.lesung is None
    assert fenster.aktuelle_seite() is fenster.pruefen
    assert d.vorbereitung.seiten[0].korrektur is not None
    assert d.vorbereitung.aufbereitet and 0 in d.vorbereitung.aufbereitet
    assert any("aufbereitet" in w for w in d.vorbereitung.hinweise)
    assert len(d.vorbereitung.fundstellen) > 5
    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.farbe == "gruen", fenster.export.ampel_text.text()  # OCR: only notes
    assert any("Handschrift" in h for h in d.vorpruefung.bericht.hinweise)
    assert not d.vorpruefung.bericht.warnungen
    fenster.export.exportieren()
    qapp.processEvents()
    protokoll = fenster.export.ausgabe.toPlainText()
    assert "Gegenprobe: bestanden" in protokoll, protokoll
    assert gefundene_klardaten(protokoll, patient) == []
    txt = (tmp_path / "aus" / "scan_pseudonym.txt").read_text(encoding="utf-8")
    assert "Zusatz: Testzeile aus der Korrektur." in txt  # the correction reached the output
    fenster.close()


def test_export_gesperrt_bei_roter_ampel(qapp: QApplication, tmp_path: Path, befund: Path,
                                         patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the re-check still finds identifiers, the light is red, the export button is
    off and exporting raises (a dialog in the real app); nothing is written."""
    from pseudonymed.gui import sitzung as sitzungsmodul
    from pseudonymed.kern.modell import Gegenprobe
    from pseudonymed.pruefung.bericht import erstelle_bericht

    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    fenster.dokumente.synchron = fenster.analysieren.synchron = fenster.export.synchron = True
    fenster.patient.fuelle(patient)
    fenster.patient.uebernehmen()
    fenster.dokumente.hinzufuegen([befund])
    fenster.analysieren.analysieren()
    echtes = sitzungsmodul.abschliessen

    def durchgefallen(v, endgueltig=True):
        erg = echtes(v, endgueltig)
        g = Gegenprobe(resttreffer=[{"seite": 1, "art": "name", "regel": "name.nachname",
                                     "pseudonym": "x", "rects": [], "quelle": "test"}])
        erg.ziel_pdf = erg.ziel_txt = erg.ziel_md = None
        erg.bericht = erstelle_bericht(v.quelle, None, 2, True, erg.fundstellen, g)
        return erg

    monkeypatch.setattr(sitzungsmodul, "abschliessen", durchgefallen)
    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.farbe == "rot" and not fenster.export.knopf.isEnabled()
    assert "Abbruch" in fenster.export.status.text()
    assert "nichts exportiert" in fenster.export.ausgabe.toPlainText()
    with pytest.raises(RuntimeError):
        fenster.export.exportieren()
    assert not (tmp_path / "aus").exists() or not list((tmp_path / "aus").glob("*_pseudonym.pdf"))
    fenster.close()
