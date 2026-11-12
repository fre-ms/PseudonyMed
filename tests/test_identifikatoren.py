"""Identifiers beyond the patient's data: context rules, the table, the pipeline, GUI, CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.ablauf import Ablaufoptionen, pseudonymisiere
from pseudonymed.cli import app
from pseudonymed.dokument.textdatei import textseite
from pseudonymed.erkennung import identifikatoren as identmodul
from pseudonymed.erkennung import lexikon
from pseudonymed.kern.modell import Art, Fundstelle, Patient
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def frisch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    identmodul.standard.cache_clear()
    yield tmp_path / "daten"
    identmodul.standard.cache_clear()


def _erkenner(*zeilen: tuple[str, str]) -> identmodul.Erkenner:
    e = identmodul.Erkenner()
    for begriff, klasse in zeilen:
        e.fuege_hinzu(begriff, klasse)
    return e


def _gefunden(e: identmodul.Erkenner, text: str) -> list[tuple[str, str]]:
    return [(text[k.start:k.ende], k.klasse) for k in e.kandidaten(text)]


# -- context rules --------------------------------------------------------------------------------
def test_kliniken_mit_kurzformen() -> None:
    e = _erkenner(("Musterklinik Nord GmbH – Psychiatrische Tagesklinik", "Klinik"),
                  ("Klinik für Geriatrische Rehabilitation", "Klinik"),
                  ("Saarland Heilstätten", "Klinik"))
    e.fuege_ort_hinzu("Halle (Saale)")
    text = ("Verlegung aus der Musterklinik Nord, später ins Klinikum Halle (Saale) und in die"
            " Klinik für Geriatrische Rehabilitation; das Saarland ist schön. Bericht des"
            " Klinikums Halle.")
    assert _gefunden(e, text) == [("Musterklinik Nord", "Klinik"),
                                  ("Klinikum Halle (Saale)", "Klinik"),
                                  ("Klinikums Halle", "Klinik")]
    assert identmodul.klinik_formen("Klinik für Geriatrische Rehabilitation") == set()
    assert identmodul.klinik_formen("Caritas") == set()
    assert "Eifelklinik" in identmodul.klinik_formen("Eifelklinik")
    assert _gefunden(e, "Klinikum halle") == []  # proper nouns stay capitalised


def test_berufe_nur_im_kontext() -> None:
    e = _erkenner(("Tischler", "Beruf"), ("Arzthelferin", "Beruf"), ("Hausarzt", "Beruf"),
                  ("Bauer", "Beruf"), ("Koch", "Beruf"), ("Angehörige", "Beruf"))
    for satz in ("Er arbeitet seit 2010 als Tischler.", "Er ist gelernter Tischler.",
                 "Er ist Tischler.", "Er ist Tischler von Beruf.", "Beruf: Tischler",
                 "Er war zuletzt als Tischler tätig.",
                 "Sozialanamnese:\nverheiratet, zwei Kinder, Tischler, Nichtraucher."):
        assert _gefunden(e, satz) == [("Tischler", "Beruf")], satz
    for satz in ("Der Tischler hat angerufen.", "Herr Bauer ist seit Jahren tätig.",
                 "Herr Koch kam zur Kontrolle.", "Der Arbeitgeber und Angehörige wissen es.",
                 "Sozialanamnese:\nverheiratet.\nBefund:\nDer Tischler war da."):
        assert _gefunden(e, satz) == [], satz
    # the first profession after a cue takes it
    assert _gefunden(e, "Sie arbeitet als Arzthelferin beim Hausarzt.") == [
        ("Arzthelferin", "Beruf")]


def test_studien_akronyme_und_nummern() -> None:
    e = _erkenner(("CARE", "Studie"), ("HoPe", "Studie"), ("Dekan", "Studie"))
    assert "dekan" not in e.index  # plain words are no acronyms
    assert _gefunden(e, "Teilnahme an der CARE-Studie (NCT01234567).") == [
        ("CARE", "Studie"), ("NCT01234567", "Studie")]
    assert _gefunden(e, "Die Studie HoPe läuft.") == [("HoPe", "Studie")]
    assert _gefunden(e, "Sie braucht CARE und care.") == []
    assert _gefunden(e, "Registriert unter DRKS00012345, EudraCT-Nr. 2019-001234-56 und"
                        " EU CT 2023-501234-12-00.") == [
        ("DRKS00012345", "Studie"), ("2019-001234-56", "Studie"),
        ("2023-501234-12-00", "Studie")]
    assert _gefunden(e, "Laborwert 2019-001234-56 ohne Bezeichnung") == []


def _person(text: str, wort: str) -> Fundstelle:
    a = text.index(wort)
    return Fundstelle(seite=0, start=a, ende=a + len(wort), text=wort, art=Art.NAME,
                      regel="ner.person", pseudonym="Person ABCDEF")


def test_titel_nur_an_personen(patient: Patient) -> None:
    e = _erkenner(("Prof.", "Titel"), ("Dr. med.", "Titel"), ("Dipl.-Ing.", "Titel"),
                  ("MBA", "Titel"), ("zur", "Titel"))
    vergabe = Pseudonymvergabe(patient, "K7Q4MX", geheimnis=bytes(range(32)))
    text = "Prof. Dr. med. Hans Meier, Dipl.-Ing., MBA, bat zur Kontrolle. Dr. med. allein."
    seite = textseite(text)
    treffer = identmodul.finde_titel(seite, e, vergabe, [_person(text, "Hans Meier")])
    assert [f.text for f in treffer] == ["Prof. Dr. med.", "Dipl.-Ing., MBA"]
    assert all(f.regel == "identifikator.titel" and f.pseudonym.startswith("[Titel ")
               for f in treffer)
    assert identmodul.finde_titel(seite, e, vergabe, []) == []
    assert "zur" not in e.index


# -- the table -------------------------------------------------------------------------------------
def test_tabelle_eigene_eintraege(frisch: Path, tmp_path: Path) -> None:
    with identmodul.Identifikatoren() as ident:
        assert ident.fuege_hinzu(["Musterwerk AG", "Musterwerk AG", "x"]) == 1
        assert ident.fuege_hinzu(["Tischler"], "Beruf") == 1
        assert ident.klassen() == [("Beruf", 1), ("Sonstiges", 1)]
        v0 = ident.version()
        e = ident.alle("Sonstiges")[0]
        assert e.eigen and e.begriff == "Musterwerk AG"
        neu = ident.aendere(e.schluessel, begriff="Musterwerk GmbH")
        assert neu is not None and neu.begriff == "Musterwerk GmbH" and ident.version() > v0
        assert ident.aendere(neu.schluessel, klasse="Klinik").klasse == "Klinik"
        assert ident.aendere(ident.alle("Beruf")[0].schluessel, begriff="ab") is None
        csv = tmp_path / "ident.csv"
        assert ident.exportiere_csv(csv) == 2
        assert csv.read_text(encoding="utf-8").splitlines()[0] == "begriff,klasse,quelle,angaben"
        assert ident.entferne(ident.schluessel_fuer("tischler")) == 1
        assert ident.importiere_csv(csv) == (1, 2)  # the hospital row exists already
        liste = tmp_path / "liste.txt"
        liste.write_text("# eigene\nSportverein Musterdorf\n", encoding="utf-8")
        assert ident.importiere_csv(liste) == (1, 1)
        assert ident.geloescht() == 0  # own rows leave no trace
    assert identmodul.standard().je_klasse == {"Klinik": 1, "Beruf": 1, "Sonstiges": 1}


def test_geloeschte_mitgelieferte_bleiben_geloescht(frisch: Path) -> None:
    with identmodul.Identifikatoren() as ident:
        lexikon.sicherstellen(ident.verbindung, erzwingen=True, quellen=["Kliniken"])
        alle = ident.alle("Klinik")
        assert len(alle) > 4000 and all(e.quelle == "Kliniken" for e in alle)
        opfer, umbenannt = alle[0], alle[1]
        assert opfer.angaben.endswith(")")  # town and country
        assert ident.entferne([opfer.schluessel]) == 1
        eigen = ident.aendere(umbenannt.schluessel, begriff=umbenannt.begriff + " Nord")
        assert eigen is not None and eigen.eigen and eigen.kennung == umbenannt.kennung
        assert ident.geloescht() == 2
        lexikon.sicherstellen(ident.verbindung, erzwingen=True, quellen=["Kliniken"])
        schluessel = {e.schluessel for e in ident.alle("Klinik")}
        assert opfer.schluessel not in schluessel and umbenannt.schluessel not in schluessel
        assert eigen.schluessel in schluessel
        assert ident.wiederherstellen() == 2
        schluessel = {e.schluessel for e in ident.alle("Klinik")}
        assert opfer.schluessel in schluessel and umbenannt.schluessel in schluessel
        assert ident.geloescht() == 0


def test_erwerbsstatus_und_studiennummern_unsichtbar(frisch: Path) -> None:
    with identmodul.Identifikatoren() as ident:
        ident.verbindung.executemany(
            "INSERT INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, 'merkmal', ?, ?, ?)",
            [("Rentnerin", "Erwerbsstatus", "Berufe", "", ""),
             ("NCT01234567", "Studie", "Studien", "ClinicalTrials.gov:NCT01234567", '{"x":1}'),
             ("ALPHA", "Studie", "Studien", "ClinicalTrials.gov:NCT01234567", "")])
        ident.verbindung.commit()
        assert [(e.begriff, e.angaben) for e in ident.alle()] == [("ALPHA", "NCT01234567")]


def test_angaben_je_klasse() -> None:
    def e(klasse: str, kennung: str = "", zusatz: str = "") -> identmodul.Eintrag:
        return identmodul.Eintrag("Begriff", klasse, "Quelle", kennung, zusatz)

    assert e("Klinik", zusatz='{"ort": "Musterstadt", "land": "DE"}').angaben == "Musterstadt (DE)"
    assert e("Klinik", zusatz='{"ort": "Musterstadt"}').angaben == "Musterstadt"
    assert e("Klinik", zusatz='{"land": "DE"}').angaben == "(DE)"
    assert e("Klinik", zusatz="{").angaben == "" and e("Klinik").angaben == ""
    assert e("Studie", "ClinicalTrials.gov:NCT01234567").angaben == "NCT01234567"
    assert e("Studie", "NCT01234567").angaben == ""
    assert e("Titel", "alle|Medizin|").angaben == "Medizin" and e("Titel", "Dr.").angaben == ""
    assert e("Beruf", "12345").angaben == "KldB 12345" and e("Beruf", "abc").angaben == ""
    assert e("Sonstiges", "12345", '{"ort": "x"}').angaben == ""


def test_standard_kennt_die_orte_der_kliniken(frisch: Path) -> None:
    with identmodul.Identifikatoren() as ident:
        ident.verbindung.executemany(
            "INSERT INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, 'Klinik', 'merkmal', 'Kliniken', ?, ?)",
            [("Musterklinik Musterstadt", "K1", '{"ort": "Musterstadt", "land": "DE"}'),
             ("Beispielklinik", "K2", "kein JSON"), ("Fachklinik Nord", "K3", "")])
        ident.verbindung.commit()
    e = identmodul.standard()
    assert e.je_klasse == {"Klinik": 3}
    text = "Verlegt ins Krankenhaus Musterstadt, danach in die Musterklinik Musterstadt."
    assert _gefunden(e, text) == [("Krankenhaus Musterstadt", "Klinik"),
                                  ("Musterklinik Musterstadt", "Klinik")]


# -- the pipeline ----------------------------------------------------------------------------------
def test_textdatei_schwaerzt_identifikatoren(frisch: Path, tmp_path: Path, patient: Patient,
                                             speicher: Pseudonymspeicher) -> None:
    with identmodul.Identifikatoren() as ident:
        ident.fuege_hinzu(["Tischler"], "Beruf")
        ident.fuege_hinzu(["Musterklinik Nord"], "Klinik")
        ident.fuege_hinzu(["Sportverein Musterdorf"])
    quelle = tmp_path / "notiz.txt"
    quelle.write_text(
        f"{patient.vorname} {patient.nachname} arbeitet als Tischler und ist im Sportverein"
        " Musterdorf aktiv. Aufnahme aus der Musterklinik Nord am Vortag. Teilnahme an der"
        " Studie unter NCT01234567. Der Tischler aus dem Nachbarort hat geholfen.\n",
        encoding="utf-8")
    erg = pseudonymisiere(quelle, patient, Ablaufoptionen(), speicher=speicher)
    assert erg.freigegeben, als_text(erg.bericht)
    text = erg.ziel_txt.read_text(encoding="utf-8")
    for klartext in ("arbeitet als Tischler", "Sportverein Musterdorf", "Musterklinik Nord",
                     "NCT01234567"):
        assert klartext not in text, text
    assert "Der Tischler aus dem Nachbarort" in text  # no cue: stays
    for praefix in ("[Beruf ", "[Identifikator ", "[Klinik ", "[Studie "):
        assert praefix in text, text
    assert any(h.startswith("Identifikatoren: 4 Treffer") for h in erg.bericht.hinweise)
    assert erg.bericht.optionen["identifikatoren"] is True
    z = speicher.zuordnung(patient.schluessel())
    zurueck = rueckersetzen(text, z, bekannte_kennungen=speicher.kennungen())
    assert "arbeitet als Tischler" in zurueck.text and "Musterklinik Nord" in zurueck.text
    aus = pseudonymisiere(quelle, patient, Ablaufoptionen(identifikatoren=False, suffix="_aus"),
                       speicher=speicher)
    assert "Musterklinik Nord" in aus.ziel_txt.read_text(encoding="utf-8")


def test_pdf_mit_kasten_und_titel(frisch: Path, tmp_path: Path, patient: Patient,
                                  speicher: Pseudonymspeicher) -> None:
    with identmodul.Identifikatoren() as ident:
        ident.fuege_hinzu(["Musterklinik Nord"], "Klinik")
        ident.fuege_hinzu(["Dr. med."], "Titel")
    pdf = tmp_path / "brief.pdf"
    doc = pymupdf.open()
    seite = doc.new_page()
    seite.insert_text((72, 100), f"Patient: Herr Dr. med. {patient.vorname} {patient.nachname}",
                      fontsize=11)
    seite.insert_text((72, 120), "Verlegt aus der Musterklinik Nord.", fontsize=11)
    doc.save(pdf)
    doc.close()
    erg = pseudonymisiere(pdf, patient, Ablaufoptionen(), speicher=speicher)
    assert erg.freigegeben, als_text(erg.bericht)
    treffer = [f for f in erg.fundstellen if f.art == Art.IDENTIFIKATOR]
    assert {f.regel for f in treffer} == {"identifikator.klinik", "identifikator.titel"}
    assert all(f.rects for f in treffer)
    with pymupdf.open(erg.ziel_pdf) as aus:
        rest = aus[0].get_text()
    assert "Musterklinik" not in rest and "Dr. med." not in rest


# -- GUI and command line --------------------------------------------------------------------------
def test_seite(qapp: QApplication, frisch: Path, tmp_path: Path) -> None:  # noqa: F811
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    fenster = Hauptfenster(Sitzung())
    seite = fenster.identifikatoren
    seite.synchron = True
    assert fenster.tabs.tabText(1) == "2 Identifikatoren"
    fenster.zeige_seite(seite)
    qapp.processEvents()
    assert seite.modell.rowCount() == 0
    assert seite.hinzufuegen("Musterwerk AG") == 1
    assert seite.hinzufuegen("Tischler", "Beruf") == 1
    assert seite.modell.rowCount() == 2 and seite.anzahl.text().startswith("2 von 2")
    seite.suche.setText("muster")
    assert seite.proxy.rowCount() == 1
    seite.suche.setText("")
    zeile = next(i for i, e in enumerate(seite.modell.eintraege) if e.begriff == "Tischler")
    assert seite.modell.setData(seite.modell.index(zeile, 0), "Schreiner")
    assert seite.modell.eintraege[zeile].begriff == "Schreiner"
    i = seite.klassen_filter.findData("Beruf")
    seite.klassen_filter.setCurrentIndex(i)
    assert seite.modell.rowCount() == 1
    csv = tmp_path / "ident.csv"
    assert seite.exportiere_csv(csv) == 1
    seite.klassen_filter.setCurrentIndex(0)
    assert seite.loeschen(seite.modell.eintraege) == 2
    assert seite.importiere_datei(csv) == 1
    assert seite.modell.eintraege[0].klasse == "Beruf"
    fenster.analysieren.identifikatoren.setChecked(False)
    fenster.analysieren._optionen_uebernehmen()
    assert fenster.sitzung.optionen.identifikatoren is False
    fenster.close()


def test_kommandozeile(frisch: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["identifikatoren", "hinzufuegen", "--klasse", "Beruf", "Tischler"])
    assert r.exit_code == 0 and "1 neue" in r.output, r.output
    r = runner.invoke(app, ["identifikatoren", "status"])
    assert r.exit_code == 0 and "Beruf" in r.output, r.output
    r = runner.invoke(app, ["identifikatoren", "pruefen",
                            "Er arbeitet als Tischler (NCT01234567)."])
    assert r.exit_code == 0 and "Beruf: Tischler" in r.output and "Studie: NCT01234567" in r.output
    r = runner.invoke(app, ["identifikatoren", "entfernen", "tischler"])
    assert r.exit_code == 0 and "1 Identifikatoren gelöscht" in r.output, r.output
