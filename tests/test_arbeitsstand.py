"""The working state: save after the review, restore it, vary it, take words into the list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed import arbeitsstand as standmodul
from pseudonymed.ablauf import Ablaufoptionen, abschliessen, analysiere, vorbereiten
from pseudonymed.cli import app
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.kern.modell import Patient
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher, einmalspeicher
from tests.synthetisch import erzeuge_transkript
from tests.test_gui import qapp  # noqa: F401
from tests.test_ocr import _scan


def _vorbereitet(befund: Path, patient: Patient, speicher: Pseudonymspeicher):
    """A reviewed preparation: one hit discarded, a box, a mask, one word taken up."""
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    ziel = next(f for f in v.fundstellen if f.regel == "zuweiser1.name")
    v.verwerfe(ziel)
    v.kasten(0, (56, 300, 400, 320), "[Kasten]")
    v.maske(0, (56, 330, 400, 345))
    text = v.seiten[0].textbasis.text
    i = text.index("Gemeinschaftspraxis")  # the letterhead: no hit, taken up by hand
    v.aufnehmen(0, i, i + len("Gemeinschaftspraxis"))
    return v


# -- saving and restoring --------------------------------------------------------------------------
def test_rundlauf_pdf(tmp_path: Path, befund: Path, patient: Patient,
                      speicher: Pseudonymspeicher) -> None:
    v = _vorbereitet(befund, patient, speicher)
    dokument, konfiguration = standmodul.speichere(v, tmp_path / "ablage")  # a folder
    assert dokument.name == "befund.arbeitsstand.pdf"
    assert konfiguration.name == "befund.arbeitsstand.json"
    daten = json.loads(konfiguration.read_text(encoding="utf-8"))
    assert daten[standmodul.MARKE] == 1 and daten["kennung"] == v.kennung
    assert daten["patient"]["nachname"] == patient.nachname  # clear data, by design
    assert standmodul.ist_arbeitsstand(dokument) == konfiguration
    assert standmodul.ist_arbeitsstand(konfiguration) == konfiguration
    assert standmodul.ist_arbeitsstand(befund) is None

    a = standmodul.lies(konfiguration)
    v2 = standmodul.vorbereitung_aus(a, speicher=speicher)
    assert v2.quelle.name == "befund.pdf" and v2.kennung == v.kennung
    assert [f.als_dict() for f in v2.fundstellen] == [f.als_dict() for f in v.fundstellen]
    assert [f.regel for f in v2.verworfen] == ["zuweiser1.name"]
    assert v2.fachbegriffe_anzahl == v.fachbegriffe_anzahl
    assert any("Arbeitsstand aus befund.arbeitsstand.json" in h for h in v2.hinweise)
    e1 = abschliessen(v, endgueltig=False)
    e2 = abschliessen(v2, endgueltig=False)
    assert e1.freigegeben and e2.freigegeben, als_text(e2.bericht)
    assert e1.text == e2.text and "[Kasten]" in e2.text and "[Begriff " in e2.text
    assert "Gemeinschaftspraxis" not in e2.text
    z = speicher.zuordnung(patient.schluessel())
    zurueck = rueckersetzen(e2.text, z, bekannte_kennungen=speicher.kennungen())
    assert "Gemeinschaftspraxis" in zurueck.text


def test_rundlauf_textdatei(tmp_path: Path, patient: Patient, speicher: Pseudonymspeicher) -> None:
    quelle = erzeuge_transkript(tmp_path / "gespraech.srt", patient, "srt")
    v = vorbereiten(quelle, patient, Ablaufoptionen(), speicher=speicher)
    i = v.text.index("Hypertonie")
    v.aufnehmen(0, i, i + len("Hypertonie"))  # a protected term, taken up on purpose
    dokument, konfiguration = standmodul.speichere(v, tmp_path / "x.arbeitsstand.json")
    assert dokument.name == "x.arbeitsstand.srt"
    v2 = standmodul.vorbereitung_aus(standmodul.lies(konfiguration), speicher=speicher)
    assert v2.ist_text and v2.ausgabe_endung == ".srt" and v2.text == v.text
    e1, e2 = abschliessen(v, endgueltig=False), abschliessen(v2, endgueltig=False)
    assert e1.freigegeben and e2.freigegeben and e1.text == e2.text
    assert "[Begriff " in e2.text and "Hypertonie" not in e2.text


def test_veraendertes_dokument_und_fremde_datei(tmp_path: Path, befund: Path, patient: Patient,
                                                speicher: Pseudonymspeicher) -> None:
    v = _vorbereitet(befund, patient, speicher)
    dokument, konfiguration = standmodul.speichere(v, tmp_path)
    v.schliessen()
    dokument.write_bytes(dokument.read_bytes() + b"\n")
    with pytest.raises(standmodul.ArbeitsstandFehler, match="verändert"):
        standmodul.lies(konfiguration)
    fremd = tmp_path / "fremd.arbeitsstand.json"
    fremd.write_text('{"foo": 1}', encoding="utf-8")
    assert standmodul.ist_arbeitsstand(fremd) is None
    with pytest.raises(standmodul.ArbeitsstandFehler):
        standmodul.lies(fremd)


def test_empfaengermodus_verweigert(befund: Path, patient: Patient) -> None:
    with einmalspeicher() as sp:
        opt = Ablaufoptionen(empfaenger="x", empfaenger_fingerabdruck="y")
        v = vorbereiten(befund, patient, opt, speicher=sp)
        with pytest.raises(standmodul.ArbeitsstandFehler, match="Empfängermodus"):
            standmodul.erfasse(v)
        v.schliessen()


# -- a new analysis keeps the reading and the decisions --------------------------------------------
def test_erneute_analyse_uebernimmt_entscheidungen(befund: Path, patient: Patient,
                                                   speicher: Pseudonymspeicher) -> None:
    v = _vorbereitet(befund, patient, speicher)
    alt = standmodul.erfasse(v)
    assert alt.pfad is None and alt.quelle == befund  # outputs stay beside the source
    v.schliessen()
    lesung = standmodul.lesung_aus(alt, Ablaufoptionen(identifikatoren=False))
    assert lesung.backend is None and not lesung.verbraucht and lesung.quelle == befund
    v3 = analysiere(lesung, patient, Ablaufoptionen(identifikatoren=False), speicher=speicher)
    n = standmodul.uebernehme_entscheidungen(alt, v3)
    assert n == 5  # box (two lines), mask, take-up, one discard
    assert [f.regel for f in v3.verworfen] == ["zuweiser1.name"]
    assert sorted(f.regel for f in v3.fundstellen if f.quelle == "manuell") == [
        "aufnahme", "manuell", "manuell", "maske"]
    assert standmodul.uebernehme_entscheidungen(alt, v3) == 0  # idempotent
    e = abschliessen(v3, endgueltig=False)
    assert e.freigegeben and "[Kasten]" in e.text and "[Begriff " in e.text


@pytest.mark.skipif(not ocrmodul.verfuegbare_backends(), reason="kein OCR-Backend installiert")
def test_scan_ohne_erneute_ocr(tmp_path: Path, befund: Path, patient: Patient,
                               speicher: Pseudonymspeicher,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    scan = _scan(befund, tmp_path / "scan.pdf")
    v = vorbereiten(scan, patient, Ablaufoptionen(), speicher=speicher)
    assert v.ocr_seiten == [1, 2]
    v.setze_ocr_text(0, v.ocr_text(0).replace("Diagnosen", "Diagnosen (korrigiert)"))
    dokument, konfiguration = standmodul.speichere(v, tmp_path)
    v.schliessen()
    monkeypatch.setattr(ocrmodul, "erkenne_seite",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR lief erneut")))
    a = standmodul.lies(konfiguration)
    v2 = standmodul.vorbereitung_aus(a, speicher=speicher)
    assert v2.ocr_seiten == [1, 2] and "(korrigiert)" in v2.ocr_text(0)
    assert v2.seiten[0].gliederung is not None and v2.unsichere_rects(0) is not None
    lesung = standmodul.lesung_aus(a)
    v3 = analysiere(lesung, patient, Ablaufoptionen(), speicher=speicher)
    assert v3.ocr_seiten == [1, 2] and "(korrigiert)" in v3.ocr_text(0)
    assert {f.regel for f in v3.fundstellen} & {f.regel for f in v2.fundstellen}
    v2.schliessen()
    v3.schliessen()


# -- taking words into the list --------------------------------------------------------------------
def test_wort_bei_und_aufnehmen(befund: Path, patient: Patient,
                                speicher: Pseudonymspeicher) -> None:
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    basis = v.seiten[0].textbasis
    i = basis.text.index("Diagnosen")
    box = basis.boxen[i + 2]
    spanne = v.wort_bei(0, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    assert spanne == (i, i + len("Diagnosen"))
    assert v.wort_bei(0, 5.0, 5.0) is None  # the margin
    vorher = len(v.fundstellen)
    f = v.aufnehmen(0, *spanne)
    assert f.pseudonym.startswith("[Begriff ") and f.rects and f.quelle == "manuell"
    assert len(v.fundstellen) == vorher + 1
    g = v.aufnehmen(0, spanne[0], spanne[1], "[Wort]")  # replaces the first take-up
    assert g.pseudonym == "[Wort]" and len(v.fundstellen) == vorher + 1
    with pytest.raises(ValueError):
        v.aufnehmen(0, spanne[0], spanne[0])
    v.schliessen()


# -- interface -------------------------------------------------------------------------------------
def test_oberflaeche(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                     patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = tmp_path / "s.sqlite"
    with Pseudonymspeicher(db) as sp:
        v = _vorbereitet(befund, patient, sp)
        anzahl = len(v.fundstellen)
        dokument, konfiguration = standmodul.speichere(v, tmp_path / "ablage")
        v.schliessen()

    sitzung = Sitzung(speicherpfad=db)
    fenster = Hauptfenster(sitzung)
    for seite in (fenster.dokumente, fenster.analysieren, fenster.pruefen, fenster.export):
        seite.synchron = True
    assert sitzung.patient is None
    fenster.dokumente.hinzufuegen([dokument])  # the document beside the JSON is enough
    qapp.processEvents()
    d = sitzung.dokumente[0]
    assert d.arbeitsstand == konfiguration and d.vorbereitung is not None and not d.fehler
    assert len(d.vorbereitung.fundstellen) == anzahl and len(d.vorbereitung.verworfen) == 1
    assert sitzung.patient is not None and sitzung.patient.nachname == patient.nachname
    assert fenster.patient.f["nachname"].text() == patient.nachname  # step 1 filled in
    assert "Arbeitsstand" in fenster.dokumente.liste.item(0).text()
    assert fenster.aktuelle_seite() is fenster.pruefen  # straight to the review
    pr = fenster.pruefen
    assert pr.dokument is d
    assert pr.tabelle.rowCount() == sum(1 for f in d.vorbereitung.fundstellen if f.seite == 0)
    assert fenster.dokumente.hinzufuegen([konfiguration]) is None  # already in

    # pick a word on the page, extend it, take it up with a code of its own
    basis = d.vorbereitung.seiten[0].textbasis
    i = basis.text.index("Diagnosen")
    box = basis.boxen[i + 1]
    pr.punkt((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    assert pr.auswahl == (i, i + len("Diagnosen")) and pr.markieren.isEnabled()
    j = basis.text.index(" ", i + len("Diagnosen") + 1) + 1
    box2 = basis.boxen[j]
    pr.punkt((box2[0] + box2[2]) / 2, (box2[1] + box2[3]) / 2, erweitern=True)
    assert pr.auswahl is not None and pr.auswahl[0] == i and pr.auswahl[1] > i + 9
    pr.aufnehmen()
    assert pr.auswahl is None and pr.gewaehlt is not None
    assert pr.gewaehlt.regel == "aufnahme" and pr.gewaehlt.pseudonym.startswith("[Begriff ")
    pr.punkt(5.0, 5.0)  # the margin clears the selection
    assert pr.auswahl is None and not pr.markieren.isEnabled()

    # vary: analyse again without identifiers, decisions survive, then save again
    fenster.analysieren.identifikatoren.setChecked(False)
    fenster.analysieren.analysieren(erneut=True)
    qapp.processEvents()
    assert d.vorbereitung is not None and not d.fehler
    assert [f.regel for f in d.vorbereitung.verworfen] == ["zuweiser1.name"]
    assert sorted(f.regel for f in d.vorbereitung.fundstellen if f.quelle == "manuell") == [
        "aufnahme", "aufnahme", "manuell", "maske"]  # the take-up replaced one box line
    pr.dokument = d
    pfade = pr.arbeitsstand_speichern(tmp_path / "variante.arbeitsstand.json")
    assert pfade is not None and pfade[1].name == "variante.arbeitsstand.json"
    assert "Arbeitsstand gespeichert" in pr.hinweis.text()
    fenster.close()


def test_kommandozeile(tmp_path: Path, befund: Path, patient: Patient) -> None:
    from tests.test_cli import _patientendatei

    runner = CliRunner()
    db = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient",
                            str(_patientendatei(tmp_path, patient)), "--speicher", str(db),
                            "--arbeitsstand", "--ziel", str(tmp_path / "aus")])
    assert r.exit_code == 0, r.output
    konfiguration = tmp_path / "aus" / "befund.arbeitsstand.json"
    assert konfiguration.exists() and "Arbeitsstand:" in r.output
    assert (tmp_path / "aus" / "befund_pseudonym.pdf").exists()
    r = runner.invoke(app, ["pseudonymisieren", str(konfiguration), "--speicher", str(db),
                            "--ziel", str(tmp_path / "wie-gespeichert")])
    assert r.exit_code == 0, r.output  # no --patient needed: the state carries it
    text = (tmp_path / "wie-gespeichert" / "befund_pseudonym.txt").read_text("utf-8")
    assert "[geb. " in text and "Arbeitsstand befund.arbeitsstand.json" in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(konfiguration), "--speicher", str(db),
                            "--ziel", str(tmp_path / "variante"), "--datum", "alter",
                            "--neu-analysieren"])
    assert r.exit_code == 0, r.output
    assert "[Alter " in (tmp_path / "variante" / "befund_pseudonym.txt").read_text("utf-8")
