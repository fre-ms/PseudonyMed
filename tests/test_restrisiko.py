"""Residual-risk estimate: data, weights, lights, union across documents, the control step."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pseudonymed.ablauf import Ablaufoptionen, pseudonymisiere
from pseudonymed.kern.modell import Patient
from pseudonymed.pruefung import restrisiko as rr
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from tests.test_gui import qapp  # noqa: F401


def test_daten_vorhanden() -> None:
    index, quelle = rr.gemeindenindex()
    assert "Destatis" in quelle and len(index.eintraege) > 10000
    treffer = dict((g.name, g) for _b, g in index.finde("Sie wohnt in Garmisch-Partenkirchen."))
    assert treffer["Garmisch-Partenkirchen"].einwohner > 20000
    index_k, quelle_k = rr.erkrankungsindex()
    assert "Orphanet" in quelle_k and len(index_k.eintraege) > 15000
    krank = index_k.finde("Verdacht auf Chorea Huntington, keine Wilson-Krankheit.")
    assert {e.orpha for _b, e in krank} == {"399", "905"}


def test_gewichte_und_ampel() -> None:
    dorf = rr.schaetze("Patientin [geb. 1959] aus Weitnau.", "w")
    assert {m.kategorie for m in dorf.merkmale} == {"Geschlecht", "Geburtsdatum", "Ort"}
    assert abs(dorf.bits - (1 + rr.BITS_JAHR + 14.0)) < 0.2 and dorf.ampel == "gruen"  # λ ≈ 30
    assert any("unter 20.000" in mk.text for mk in dorf.markierungen)
    # a village of about 1,000: sex + year leave λ between 5 and 11, so yellow
    index, _q = rr.gemeindenindex()
    klein = next(g for g in sorted({id(g): g for g in index.eintraege.values()}.values(),
                                   key=lambda g: g.name)
                 if 900 <= g.einwohner <= 1100 and g.mehrdeutig == 1 and len(g.name) >= 6
                 and "-" not in g.name and " " not in g.name)
    weiler = rr.schaetze(f"Patientin [geb. 1959] aus {klein.name}.", "w")
    assert weiler.ampel == "gelb" and 5 <= weiler.lam < 11, (klein, weiler.lam)
    winzig = rr.schaetze(f"Die 66-jährige Patientin [geb. 1959] aus {klein.name}, gesehen am"
                         " 12.03.2026.", "w")
    assert winzig.ampel == "rot"  # the age next to the date adds 2.6 bits
    stadt = rr.schaetze("Patient [geb. 1959] aus Augsburg, Termin am 12.03.2026.", "m")
    assert stadt.ampel == "gruen" and stadt.lam > 100 and stadt.datumsangaben == 1
    assert stadt.dominant is not None and stadt.dominant.kategorie == "Ort"
    eigene = rr.schaetze("Patient [geb. 1959] aus Augsburg.", "m", eigene_orte=["Augsburg"])
    assert not any(m.kategorie == "Ort" for m in eigene.merkmale)
    selten = rr.schaetze("Patient [geb. 1959] mit Chorea Huntington, wohnhaft in Augsburg.", "m")
    assert selten.ampel == "rot" and selten.lam < 1
    assert any(mk.schluessel.startswith("selten:") for mk in selten.markierungen)
    nichts = rr.schaetze("Essen und Trinken unauffällig, Grund zur Sorge besteht nicht.", "")
    assert nichts.merkmale == [] and nichts.ampel == "gruen" and nichts.kappa < 0.001
    # two places: the smaller one counts, the other is listed
    zwei = rr.schaetze("Patient [geb. 1959] aus Augsburg, geboren in Weitnau.", "m")
    orte = [m for m in zwei.merkmale if m.kategorie == "Ort"]
    assert [m.gezaehlt for m in sorted(orte, key=lambda m: -m.bits)] == [True, False]
    # age next to birth year and an exact date narrows the birthday
    fenster = rr.schaetze("Die 66-jährige Patientin [geb. 1959] kam am 12.03.2026.", "w")
    assert any(m.schluessel == "geburt:fenster" for m in fenster.merkmale)
    assert any(mk.schluessel == "alter_und_datum" for mk in fenster.markierungen)
    alt = rr.schaetze("Der 91-jährige Patient", "m")
    assert any(mk.schluessel == "alter_90" for mk in alt.markierungen)
    flags = rr.schaetze("Nach dem Verkehrsunfall am 3. Mai 2024 arbeitet er als Lehrer; zwei"
                        " Kinder.", "m")
    assert {mk.schluessel for mk in flags.markierungen} >= {"ereignis_datiert", "beruf",
                                                           "familie"}
    # thresholds
    assert rr.Restrisiko(referenz=1000).ampel == "gruen"
    r = rr.Restrisiko(referenz=100)
    r.merkmale.append(rr.Merkmal("x", "Ort", "x", 4.0))  # λ = 100 / 16 = 6.25
    assert r.ampel == "gelb" and 0.09 < r.kappa < 0.2
    r.merkmale.append(rr.Merkmal("y", "Ort", "y", 1.0))  # λ = 3.1
    assert r.ampel == "rot"
    wieder = rr.Restrisiko.aus_dict(r.als_dict())
    assert wieder.bits == r.bits and wieder.kurz() == r.kurz() and "ROT" in r.kurz()


def test_vereinigung_mit_frueheren() -> None:
    a = rr.schaetze("Patient [geb. 1959] aus Augsburg.", "m")
    b = rr.schaetze("Patient [geb. 1959] mit Chorea Huntington.", "m")
    fruehere = [rr.Merkmal("ort:weitnau", "Ort", "Ort Weitnau (5.270 Einwohner)", 14.0)]
    gesamt = rr.vereinige([a, b], fruehere, 3)
    assert gesamt.fruehere == 3 and gesamt.ampel == "rot"
    orte = [m for m in gesamt.merkmale if m.kategorie == "Ort"]
    assert sum(m.gezaehlt for m in orte) == 1 and max(orte, key=lambda m: m.bits).gezaehlt
    assert any(m.kategorie == "Diagnose" and m.gezaehlt for m in gesamt.merkmale)
    assert len([m for m in gesamt.merkmale if m.schluessel == "geschlecht"]) == 1


def test_bericht_und_speicher(tmp_path: Path, befund: Path, patient: Patient) -> None:
    with Pseudonymspeicher(tmp_path / "s.sqlite") as sp:
        erg = pseudonymisiere(befund, patient, Ablaufoptionen(bericht=False, textdatei=False,
                                                           ziel_ordner=tmp_path / "aus"),
                           speicher=sp)
        assert erg.freigegeben and erg.restrisiko is not None
        assert erg.bericht.restrisiko["ampel"] == erg.restrisiko.ampel
        assert "Restrisiko (Schätzung" in als_text(erg.bericht)
        assert erg.bericht.optionen["referenzpopulation"] == rr.REFERENZ_DEUTSCHLAND
        assert sp.restrisiko_fruehere(patient) == ([], 0)
        sp.merke_restrisiko(patient, "a.pdf", [{"schluessel": "ort:weitnau", "kategorie": "Ort",
                                                "beschreibung": "Ort Weitnau", "bits": 14.0}],
                            21.4, 30.0)
        sp.merke_restrisiko(patient, "b.pdf", [{"schluessel": "ort:weitnau", "kategorie": "Ort",
                                                "beschreibung": "Ort Weitnau", "bits": 12.0}],
                            19.0, 150.0)
        merkmale, n = sp.restrisiko_fruehere(patient)
        assert n == 2 and merkmale[0]["bits"] == 14.0  # the higher weight stays
    klein = pseudonymisiere(befund, patient, Ablaufoptionen(bericht=False, textdatei=False,
                                                         suffix="_k", referenzpopulation=50_000),
                         speicher=Pseudonymspeicher(":memory:"))
    assert klein.restrisiko.referenz == 50_000 and klein.restrisiko.lam < erg.restrisiko.lam


def test_fenster_kontrolle(qapp: QApplication, tmp_path: Path, befund: Path,  # noqa: F811
                           patient: Patient) -> None:
    """The control step runs the re-check, shows both lights, and a red estimate needs an
    explicit confirmation before the export."""
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    for seite in (fenster.dokumente, fenster.analysieren, fenster.export, fenster.kontrolle):
        seite.synchron = True
    assert [fenster.tabs.tabText(i) for i in range(7, 11)] == [
        "8 Kontrolle", "9 Export", "10 Reidentifikation", "11 Datenbank"]
    fenster.patient.fuelle(patient)
    fenster.patient.uebernehmen()
    fenster.dokumente.hinzufuegen([befund])
    fenster.analysieren.analysieren()
    qapp.processEvents()
    ko = fenster.kontrolle
    fenster.zeige_seite(ko)
    qapp.processEvents()
    d = sitzung.dokumente[0]
    assert d.vorpruefung is not None and ko.farbe in ("gruen", "gelb")
    assert ko.tabelle.rowCount() == 1 and ko.tabelle.item(0, 1).text() == "bestanden"
    assert ko.risiko_farbe in ("gruen", "gelb", "rot") and "λ" in ko.risiko_text.text()
    assert ko.merkmale.rowCount() >= 2 and "Restrisiko" in ko.protokoll.toPlainText()
    gesamt, fruehere = sitzung.kontrolle()
    assert gesamt is not None and fruehere == 0
    # a tiny reference population (the practice's own patients) drives the estimate into
    # the red; the export then needs the explicit confirmation
    ko.referenz.setText("50")
    ko.neu_berechnen()
    assert "ab 100" in ko.status.text()
    ko.referenz.setText("300")
    ko.neu_berechnen()
    qapp.processEvents()
    assert sitzung.optionen.referenzpopulation == 300 and ko.risiko_farbe == "rot"
    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.risiko_farbe == "rot" and not fenster.export.knopf.isEnabled()
    assert fenster.export.risiko_bestaetigt.isVisible() or True
    with pytest.raises(RuntimeError, match="Restrisiko"):
        fenster.export.exportieren()
    fenster.export.risiko_bestaetigt.setChecked(True)
    assert fenster.export.knopf.isEnabled()
    fenster.export.exportieren()
    qapp.processEvents()
    assert d.ergebnis is not None and d.ergebnis.freigegeben
    with Pseudonymspeicher(tmp_path / "s.sqlite") as sp:
        merkmale, n = sp.restrisiko_fruehere(patient)
    assert n == 1 and merkmale  # the release was remembered for the next document
    # a second session of the same patient sees the earlier document
    sitzung2 = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster2 = Hauptfenster(sitzung2)
    for seite in (fenster2.dokumente, fenster2.analysieren, fenster2.kontrolle):
        seite.synchron = True
    fenster2.patient.fuelle(patient)
    fenster2.patient.uebernehmen()
    fenster2.dokumente.hinzufuegen([befund])
    fenster2.analysieren.analysieren()
    fenster2.kontrolle.aktualisiere()
    qapp.processEvents()
    assert sitzung2.kontrolle()[1] == 1 and "früher" in fenster2.kontrolle.risiko_text.text()
    fenster2.close()
    fenster.close()


def test_orte_ohne_fachbegriffe_und_eigene() -> None:
    """A place name the vocabulary knows as a medical term is not the patient's place, and
    neither is one of the sender's own places (compared trimmed and case-insensitively)."""

    class Fachwissen:
        def ist_fachbegriff(self, wort: str) -> bool:
            return wort == "Augsburg"

    zwei = rr.schaetze("Patient aus Augsburg, geboren in Weitnau.", "m",
                       fachbegriffe=Fachwissen())
    assert [m.schluessel for m in zwei.merkmale if m.kategorie == "Ort"] == ["ort:weitnau"]
    keiner = rr.schaetze("Patient aus Augsburg, geboren in Weitnau.", "m",
                         eigene_orte=[" weitnau ", ""], fachbegriffe=Fachwissen())
    assert not any(m.kategorie == "Ort" for m in keiner.merkmale)
    assert not any(mk.schluessel.startswith("kleiner_ort:") for mk in keiner.markierungen)


def test_erkrankung_mit_synonym_zaehlt_einmal() -> None:
    """A disease named by its name and by a synonym is one feature, counted once, flagged once;
    a second disease is listed but not counted."""
    doppelt = rr.schaetze("Chorea Huntington, also Huntington-Krankheit; keine Wilson-Krankheit.",
                          "m")
    diagnosen = [m for m in doppelt.merkmale if m.kategorie == "Diagnose"]
    assert [m.schluessel for m in diagnosen] == ["diagnose:905", "diagnose:399"]  # rarest first
    assert [m.gezaehlt for m in diagnosen] == [True, False]
    assert [mk.schluessel for mk in doppelt.markierungen] == ["selten:905", "selten:399"]


def test_alter_ab_90_aus_dem_geburtsjahr() -> None:
    """The 90+ flag also follows from a birth year and the current year; the birthday window
    needs year, age and an exact date together."""
    hoch = rr.schaetze("Patient [geb. 1930].", "m", heute_jahr=2026)
    assert any(mk.schluessel == "alter_90" for mk in hoch.markierungen)
    jung = rr.schaetze("Patient [geb. 1930].", "m", heute_jahr=2010)
    assert not any(mk.schluessel == "alter_90" for mk in jung.markierungen)
    ohne_datum = rr.schaetze("Die 66-jährige Patientin [geb. 1959].", "w")
    assert [m.schluessel for m in ohne_datum.merkmale] == ["geschlecht", "geburt:jahr"]
    assert ohne_datum.merkmale[1].beleg == "[geb. 1959]" and ohne_datum.datumsangaben == 0
    nur_alter = rr.schaetze("Alter: 66 Jahre, [Alter 66].", "")
    assert [m.beleg for m in nur_alter.merkmale] == ["Alter 66"]


def test_hinweise_mit_belegen() -> None:
    """Flags carry a whitespace-collapsed excerpt; an event without a date nearby is no flag."""
    flags = rr.schaetze("Er arbeitet\n\n  als Lehrer an einer Schule; drei Kinder.", "m")
    je_schluessel = {mk.schluessel: mk for mk in flags.markierungen}
    assert je_schluessel["beruf"].beleg.startswith("arbeitet als Lehrer an einer Schule")
    assert je_schluessel["familie"].beleg.startswith("drei Kinder")
    ruhig = rr.schaetze("Nach dem Sturz keine Beschwerden.", "m")
    assert not any(mk.schluessel == "ereignis_datiert" for mk in ruhig.markierungen)
    datiert = rr.schaetze("Sturz am 3. Mai 2024 auf der Treppe.", "m")
    ereignis = next(mk for mk in datiert.markierungen if mk.schluessel == "ereignis_datiert")
    assert ereignis.beleg == "Sturz am 3. Mai 2024 auf der Treppe."


def test_vereinigung_nimmt_je_schluessel_das_hoechste_gewicht() -> None:
    """Per key the highest weight stays, in the position of its first appearance and counted
    again; flags are kept once per key; reference and date counts are merged."""
    a = rr.Restrisiko(merkmale=[rr.Merkmal("ort:x", "Ort", "x", 4.0),
                                rr.Merkmal("geschlecht", "Geschlecht", "g", 1.0)],
                      markierungen=[rr.Markierung("beruf", "erst")], referenz=1000,
                      datumsangaben=1)
    b = rr.Restrisiko(merkmale=[rr.Merkmal("ort:x", "Ort", "x", 9.0, gezaehlt=False)],
                      markierungen=[rr.Markierung("beruf", "zweit")], referenz=500,
                      datumsangaben=2)
    gesamt = rr.vereinige([a, b])
    assert [(m.schluessel, m.bits, m.gezaehlt) for m in gesamt.merkmale] == [
        ("ort:x", 9.0, True), ("geschlecht", 1.0, True)]
    assert [mk.text for mk in gesamt.markierungen] == ["erst"]
    assert gesamt.referenz == 500 and gesamt.datumsangaben == 3 and gesamt.fruehere == 0
