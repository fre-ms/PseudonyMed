"""Medications and lab values in four levels: off by default, redacted on request, restored."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.ablauf import Ablaufoptionen, abschliessen, pseudonymisiere, vorbereiten
from pseudonymed.cli import app
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import lexikon
from pseudonymed.erkennung import medizininhalte as medmodul
from pseudonymed.erkennung.medizininhalte import Auswahl
from pseudonymed.erkennung.vokabular import Vokabular
from pseudonymed.kern.modell import Art, Patient
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from tests.test_cli import _patientendatei
from tests.test_gui import qapp  # noqa: F401

ZEILEN = [  # begriff, klasse, quelle
    ("Levetiracetam", "Medikament", "Wikidata"),
    ("Lamotrigin", "Medikament", "Wikidata"),
    ("Ibuprofen", "Medikament", "Wikidata"),
    ("Paracetamol", "Medikament", "Wikidata"),
    ("Sauerstoff", "Medikament", "Wikidata"),
    ("ASS", "Medikament", "Wikidata"), ("ASS", "Erkrankung", "Wikidata"),  # also autism
    ("Kalium", "Medikament", "Wikidata"), ("Kalium", "Laborwert", "Laborwerte"),  # both
    ("TSH", "Laborwert", "Laborwerte"),
    ("Hb", "Laborwert", "Laborwerte"), ("HbA1c", "Laborwert", "Laborwerte"),
    ("Kreatinin", "Laborwert", "Laborwerte"), ("Ferritin", "Laborwert", "Laborwerte"),
    ("Zuname", "Laborwert", "Wikidata"),  # the noisy Wikidata lab rows stay unused
]
ALLE = Auswahl(True, True, True, True)


def _caches_leeren() -> None:
    medmodul.standard.cache_clear()
    fachmodul.standard.cache_clear()


@pytest.fixture
def lexikon_klein(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    _caches_leeren()
    with Vokabular() as v:
        v.verbindung.executescript(lexikon.SCHEMA)
        v.verbindung.executemany(
            "INSERT INTO lexikon (begriff, klasse, rolle, quelle) VALUES (?, ?, 'fach', ?)",
            ZEILEN)
        lexikon.erhoehe_version(v.verbindung)
        v.verbindung.commit()
        v.fuege_hinzu(["Marcumar"], "Medikament")  # a trade name of the user's own
        v.fuege_hinzu(["Levetiracetam"], "Nutzervokabular")
    yield tmp_path / "daten"
    _caches_leeren()


def _spannen(text: str, auswahl: Auswahl = ALLE) -> list[tuple[str, str]]:
    suche = medmodul.standard(auswahl)
    assert suche is not None
    return [(text[a:b], art) for a, b, art in suche.spannen(text)]


# -- the selection and the lists -----------------------------------------------------------------
def test_auswahl() -> None:
    assert medmodul.standard(Auswahl()) is None and not Auswahl().aktiv
    opt = Ablaufoptionen(dosierungen_schwaerzen=True, messwerte_schwaerzen=True)
    a = Auswahl.aus(opt)
    assert a == Auswahl(dosierungen=True, messwerte=True)
    assert a.medikamentennamen and a.labornamen and not a.medikamente and not a.laborwerte


def test_listen(lexikon_klein: Path) -> None:
    suche = medmodul.standard(ALLE)
    assert suche is not None
    assert suche.erkenner.je_klasse == {"Medikament": 6, "Laborwert": 6}  # no ASS, Sauerstoff
    assert suche.beide == {"kalium"} and suche.fehlende_listen() == []
    assert _spannen("ASS 100 mg, Sauerstoff 2 l/min, Zuname 12 mg/l, Marcumar nach INR.") == [
        ("Marcumar", "Medikament")]


# -- medications: substance and dose as separate levels -------------------------------------------
def test_medikamente_zwei_stufen(lexikon_klein: Path) -> None:
    text = ("Levetiracetam 500 mg 1-0-1, Lamotrigin 50 mg abends, 2 Tbl. Ibuprofen 400 mg bei"
            " Bedarf, Paracetamol. Donepezil 10 mg zur Nacht, p. o. Keppra seit 3 Jahren.")
    assert _spannen(text, Auswahl(medikamente=True)) == [
        ("Levetiracetam", "Medikament"), ("Lamotrigin", "Medikament"), ("Ibuprofen", "Medikament"),
        ("Paracetamol", "Medikament")]
    assert _spannen(text, Auswahl(dosierungen=True)) == [
        ("500 mg 1-0-1", "Dosierung"), ("50 mg abends", "Dosierung"), ("2 Tbl.", "Dosierung"),
        ("400 mg bei Bedarf", "Dosierung")]
    assert len(_spannen(text, Auswahl(medikamente=True, dosierungen=True))) == 8


def test_dosis_muster(lexikon_klein: Path) -> None:
    faelle = {
        "Levetiracetam 500 mg 1-0-1, Lamotrigin 50 mg": ["500 mg 1-0-1", "50 mg"],
        "Levetiracetam\t500 mg\t1-0-1\nLamotrigin\t50 mg\t0-0-1": ["500 mg\t1-0-1", "50 mg\t0-0-1"],
        "Ibuprofen 400 mg bei Bedarf, 1 Tbl. Paracetamol 500 mg, 1-0-1":
            ["400 mg bei Bedarf", "1 Tbl.", "500 mg, 1-0-1"],
        "Levetiracetam 2019 begonnen, seit 3 Jahren, ab 8 Uhr, Lamotrigin 2× 1000 mg/d":
            ["2× 1000 mg/d"],
        "Ibuprofen ½-0-½, Paracetamol 3x1 Tbl. tgl., Lamotrigin 20 mg/kg KG i. v.":
            ["½-0-½", "3x1 Tbl. tgl.", "20 mg/kg KG i. v."],
    }
    for text, erwartet in faelle.items():
        assert [t for t, _a in _spannen(text, Auswahl(dosierungen=True))] == erwartet, text


# -- lab values: examination and measurement as separate levels ----------------------------------
def test_laborwerte_zwei_stufen(lexikon_klein: Path) -> None:
    text = ("TSH 2,3 mU/l, Hb: 12,4 g/dl, HbA1c von 6,2 %, Kreatinin (i. S.) < 1,1 mg/dl."
            " Kontrolle des TSH in 3 Monaten; TSH 12.03.2026 abgenommen; Ferritin erniedrigt;"
            " HB 12 und hb 12 sind keine Namen; Zuname 12 mg/l.")
    assert [t for t, _a in _spannen(text, Auswahl(laborwerte=True))] == [
        "TSH", "Hb", "HbA1c", "Kreatinin", "TSH", "TSH", "Ferritin"]
    assert _spannen(text, Auswahl(messwerte=True)) == [
        ("2,3 mU/l", "Messwert"), ("12,4 g/dl", "Messwert"), ("6,2 %", "Messwert"),
        ("< 1,1 mg/dl", "Messwert")]
    assert len(_spannen(text, Auswahl(laborwerte=True, messwerte=True))) == 11


def test_begriff_auf_beiden_listen(lexikon_klein: Path) -> None:
    text = "Kalium 4,1 mmol/l; Kalium 40 mmol 1-0-1; Kalium substituiert."
    assert _spannen(text) == [
        ("Kalium", "Laborwert"), ("4,1 mmol/l", "Messwert"),
        ("Kalium", "Medikament"), ("40 mmol 1-0-1", "Dosierung"),
        ("Kalium", "Laborwert")]
    assert _spannen(text, Auswahl(medikamente=True)) == [("Kalium", "Medikament")] * 2
    assert _spannen(text, Auswahl(messwerte=True)) == [("4,1 mmol/l", "Messwert")]


# -- the pipeline ---------------------------------------------------------------------------------
def test_standardmaessig_bleibt_alles(lexikon_klein: Path, tmp_path: Path, patient: Patient,
                                      speicher: Pseudonymspeicher) -> None:
    quelle = tmp_path / "notiz.txt"
    quelle.write_text(f"{patient.vorname} {patient.nachname}: Levetiracetam 500 mg 1-0-1,"
                      " TSH 2,3 mU/l.\n", encoding="utf-8")
    erg = pseudonymisiere(quelle, patient, Ablaufoptionen(), speicher=speicher)
    text = erg.ziel_txt.read_text(encoding="utf-8")
    assert erg.freigegeben and "Levetiracetam 500 mg 1-0-1" in text and "TSH 2,3 mU/l" in text
    for name in ("medikamente", "dosierungen", "laborwerte", "messwerte"):
        assert erg.bericht.optionen[name + "_schwaerzen"] is False


def test_textdatei_jede_stufe(lexikon_klein: Path, tmp_path: Path, patient: Patient,
                              speicher: Pseudonymspeicher) -> None:
    quelle = tmp_path / "notiz.txt"
    quelle.write_text(
        f"{patient.vorname} {patient.nachname} nimmt Levetiracetam 500 mg 1-0-1 und Marcumar."
        " Labor: TSH 2,3 mU/l, Hb 12,4 g/dl. Levetiracetam wird fortgeführt.\n",
        encoding="utf-8")
    erwartung = {  # option -> (gone, still there)
        "medikamente_schwaerzen": (["Levetiracetam", "Marcumar"], ["500 mg 1-0-1", "TSH 2,3"]),
        "dosierungen_schwaerzen": (["500 mg 1-0-1"], ["Levetiracetam", "TSH 2,3 mU/l"]),
        "laborwerte_schwaerzen": (["TSH", "Hb "], ["2,3 mU/l", "12,4 g/dl", "Levetiracetam"]),
        "messwerte_schwaerzen": (["2,3 mU/l", "12,4 g/dl"], ["TSH ", "Hb ", "500 mg 1-0-1"]),
    }
    for option, (weg, bleibt) in erwartung.items():
        opt = Ablaufoptionen(**{option: True}, suffix="_" + option)
        erg = pseudonymisiere(quelle, patient, opt, speicher=speicher)
        assert erg.freigegeben, als_text(erg.bericht)
        text = erg.ziel_txt.read_text(encoding="utf-8")
        for klartext in weg:
            assert klartext not in text, (option, text)
        for klartext in bleibt:
            assert klartext in text, (option, text)
    opt = Ablaufoptionen(medikamente_schwaerzen=True, dosierungen_schwaerzen=True,
                         laborwerte_schwaerzen=True, messwerte_schwaerzen=True)
    erg = pseudonymisiere(quelle, patient, opt, speicher=speicher)
    text = erg.ziel_txt.read_text(encoding="utf-8")
    codes = [w for w in text.split("[")[1:] if w.startswith("Medikament ")]
    assert len(codes) == 3 and codes[0][:17] == codes[2][:17]  # same drug, same code
    for praefix in ("[Dosierung ", "[Laborwert ", "[Messwert "):
        assert praefix in text, text
    assert any(h.startswith("Medizinischer Inhalt: 8 Treffer") for h in erg.bericht.hinweise)
    z = speicher.zuordnung(patient.schluessel())
    zurueck = rueckersetzen(text, z, bekannte_kennungen=speicher.kennungen())
    assert "Levetiracetam 500 mg 1-0-1" in zurueck.text and "TSH 2,3 mU/l" in zurueck.text


def test_pdf_ohne_gruene_marke_unter_dem_treffer(lexikon_klein: Path, tmp_path: Path,
                                                 patient: Patient,
                                                 speicher: Pseudonymspeicher) -> None:
    pdf = tmp_path / "brief.pdf"
    doc = pymupdf.open()
    seite = doc.new_page()
    seite.insert_text((72, 100), f"Patient: {patient.vorname} {patient.nachname}", fontsize=11)
    seite.insert_text((72, 120), "Medikation: Levetiracetam 500 mg", fontsize=11)
    seite.insert_text((72, 140), "Levetiracetam gut vertragen", fontsize=11)
    doc.save(pdf)
    doc.close()
    opt = Ablaufoptionen(medikamente_schwaerzen=True)
    v = vorbereiten(pdf, patient, opt, speicher=speicher)
    treffer = [f for f in v.fundstellen if f.art == Art.MEDIZIN]
    assert [f.regel for f in treffer] == ["medizin.medikament"] * 2
    assert all(f.rects for f in treffer)

    def unter_treffern() -> int:
        return sum(1 for r in v.fachbegriffe_rects(0) for f in treffer if f in v.fundstellen
                   for b in f.rects if r[0] < b[2] and b[0] < r[2] and r[1] < b[3] and b[1] < r[3])

    gruen = len(v.fachbegriffe_rects(0))  # "Medikation" stays green
    assert gruen >= 1 and unter_treffern() == 0
    v.verwerfe(treffer[1])  # discarded: the term is protected again and green
    assert len(v.fachbegriffe_rects(0)) == gruen + 1 and unter_treffern() == 0
    erg = abschliessen(v)
    assert erg.freigegeben, als_text(erg.bericht)
    with pymupdf.open(erg.ziel_pdf) as aus:
        rest = aus[0].get_text()
    assert rest.count("Levetiracetam") == 1 and "[Medikament " in erg.text


def test_warnung_bei_leerer_liste(tmp_path: Path, patient: Patient, speicher: Pseudonymspeicher,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "leer"))
    _caches_leeren()
    quelle = tmp_path / "notiz.txt"
    quelle.write_text("Levetiracetam 500 mg\n", encoding="utf-8")
    erg = pseudonymisiere(quelle, patient, Ablaufoptionen(dosierungen_schwaerzen=True),
                          speicher=speicher)
    assert any("Medikamente schwärzen gewählt, aber die Liste ist leer" in w
               for w in erg.bericht.warnungen)
    _caches_leeren()


# -- interface and command line -------------------------------------------------------------------
def test_oberflaeche(qapp: QApplication, lexikon_klein: Path) -> None:  # noqa: F811
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    fenster = Hauptfenster(Sitzung())
    seite = fenster.analysieren
    kaesten = (seite.medikamente, seite.dosierungen, seite.laborwerte, seite.messwerte)
    assert not any(k.isChecked() for k in kaesten)
    seite.dosierungen.setChecked(True)
    seite.messwerte.setChecked(True)
    seite._optionen_uebernehmen()
    o = fenster.sitzung.optionen
    assert Auswahl.aus(o) == Auswahl(dosierungen=True, messwerte=True)
    fenster.close()


def test_kommandozeile(lexikon_klein: Path, tmp_path: Path, patient: Patient) -> None:
    quelle = tmp_path / "notiz.txt"
    quelle.write_text("Levetiracetam 500 mg, TSH 2,3 mU/l.\n", encoding="utf-8")
    r = CliRunner().invoke(app, [
        "pseudonymisieren", str(quelle), "--patient", str(_patientendatei(tmp_path, patient)),
        "--speicher", str(tmp_path / "s.sqlite"), "--messwerte-schwaerzen",
        "--dosierungen-schwaerzen"])
    assert r.exit_code == 0, r.output
    text = (tmp_path / "notiz_pseudonym.txt").read_text(encoding="utf-8")
    assert "Levetiracetam [Dosierung " in text and "TSH [Messwert " in text
    assert "500 mg" not in text and "2,3" not in text


def test_dosislauf_endet_an_neuer_menge_nach_komma() -> None:
    """Inside a run, an amount with a unit behind a comma belongs to the next drug; at the
    start of a run the same token is taken."""
    text = "400 mg bei Bedarf, 1 Tbl. Paracetamol"
    erster = medmodul._dosis_token_nach(text, 0, im_lauf=False)
    assert erster is not None and erster.group() == "400 mg"
    zweiter = medmodul._dosis_token_nach(text, erster.end(), im_lauf=True)
    assert zweiter is not None and zweiter.group() == "bei Bedarf"
    assert medmodul._dosis_token_nach(text, zweiter.end(), im_lauf=True) is None
    dritter = medmodul._dosis_token_nach(text, zweiter.end(), im_lauf=False)
    assert dritter is not None and dritter.group() == "1 Tbl."
    assert medmodul.dosis_nach(text, 0) == (0, len("400 mg bei Bedarf"))
