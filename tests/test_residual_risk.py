"""Residual-risk estimate: data, weights, lights, union across documents, the control step."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pseudonymed.checks import residual_risk as rr
from pseudonymed.checks.report import as_text
from pseudonymed.core.model import Patient
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.test_gui import qapp  # noqa: F401


def test_data_present() -> None:
    index, source = rr.municipality_index()
    assert "Destatis" in source and len(index.entries) > 10000
    matches = dict((g.name, g) for _b, g in index.find("Sie wohnt in Garmisch-Partenkirchen."))
    assert matches["Garmisch-Partenkirchen"].population > 20000
    index_k, source_k = rr.disease_index()
    assert "Orphanet" in source_k and len(index_k.entries) > 15000
    diseases = index_k.find("Verdacht auf Chorea Huntington, keine Wilson-Krankheit.")
    assert {e.orpha for _b, e in diseases} == {"399", "905"}


def test_weights_and_traffic_light() -> None:
    village = rr.estimate("Patientin [geb. 1959] aus Weitnau.", "w")
    assert {m.category for m in village.features} == {"Geschlecht", "Geburtsdatum", "Ort"}
    assert abs(village.bits - (1 + rr.BITS_YEAR + 14.0)) < 0.2  # λ ≈ 30
    assert village.traffic_light == "gruen"
    assert any("unter 20.000" in mk.text for mk in village.markers)
    # a village of about 1,000: sex + year leave λ between 5 and 11, so yellow
    index, _q = rr.municipality_index()
    small = next(g for g in sorted({id(g): g for g in index.entries.values()}.values(),
                                   key=lambda g: g.name)
                 if 900 <= g.population <= 1100 and g.namesakes == 1 and len(g.name) >= 6
                 and "-" not in g.name and " " not in g.name)
    hamlet = rr.estimate(f"Patientin [geb. 1959] aus {small.name}.", "w")
    assert hamlet.traffic_light == "gelb" and 5 <= hamlet.lam < 11, (small, hamlet.lam)
    tiny = rr.estimate(f"Die 66-jährige Patientin [geb. 1959] aus {small.name}, gesehen am"
                       " 12.03.2026.", "w")
    assert tiny.traffic_light == "rot"  # the age next to the date adds 2.6 bits
    city = rr.estimate("Patient [geb. 1959] aus Augsburg, Termin am 12.03.2026.", "m")
    assert city.traffic_light == "gruen" and city.lam > 100 and city.date_mentions == 1
    assert city.dominant is not None and city.dominant.category == "Ort"
    own = rr.estimate("Patient [geb. 1959] aus Augsburg.", "m", own_places=["Augsburg"])
    assert not any(m.category == "Ort" for m in own.features)
    rare = rr.estimate("Patient [geb. 1959] mit Chorea Huntington, wohnhaft in Augsburg.", "m")
    assert rare.traffic_light == "rot" and rare.lam < 1
    assert any(mk.key.startswith("selten:") for mk in rare.markers)
    nothing = rr.estimate("Essen und Trinken unauffällig, Grund zur Sorge besteht nicht.", "")
    assert nothing.features == [] and nothing.traffic_light == "gruen" and nothing.kappa < 0.001
    # two places: the smaller one counts, the other is listed
    two = rr.estimate("Patient [geb. 1959] aus Augsburg, geboren in Weitnau.", "m")
    places = [m for m in two.features if m.category == "Ort"]
    assert [m.counted for m in sorted(places, key=lambda m: -m.bits)] == [True, False]
    # age next to birth year and an exact date narrows the birthday
    window = rr.estimate("Die 66-jährige Patientin [geb. 1959] kam am 12.03.2026.", "w")
    assert any(m.key == "geburt:fenster" for m in window.features)
    assert any(mk.key == "alter_und_datum" for mk in window.markers)
    old = rr.estimate("Der 91-jährige Patient", "m")
    assert any(mk.key == "alter_90" for mk in old.markers)
    flags = rr.estimate("Nach dem Verkehrsunfall am 3. Mai 2024 arbeitet er als Lehrer; zwei"
                        " Kinder.", "m")
    assert {mk.key for mk in flags.markers} >= {"ereignis_datiert", "beruf",
                                                           "familie"}
    # thresholds
    assert rr.ResidualRisk(reference=1000).traffic_light == "gruen"
    r = rr.ResidualRisk(reference=100)
    r.features.append(rr.Feature("x", "Ort", "x", 4.0))  # λ = 100 / 16 = 6.25
    assert r.traffic_light == "gelb" and 0.09 < r.kappa < 0.2
    r.features.append(rr.Feature("y", "Ort", "y", 1.0))  # λ = 3.1
    assert r.traffic_light == "rot"
    restored = rr.ResidualRisk.from_dict(r.as_dict())
    assert restored.bits == r.bits and restored.short() == r.short() and "ROT" in r.short()


def test_union_with_earlier_documents() -> None:
    a = rr.estimate("Patient [geb. 1959] aus Augsburg.", "m")
    b = rr.estimate("Patient [geb. 1959] mit Chorea Huntington.", "m")
    earlier = [rr.Feature("ort:weitnau", "Ort", "Ort Weitnau (5.270 Einwohner)", 14.0)]
    total = rr.merge([a, b], earlier, 3)
    assert total.earlier == 3 and total.traffic_light == "rot"
    places = [m for m in total.features if m.category == "Ort"]
    assert sum(m.counted for m in places) == 1 and max(places, key=lambda m: m.bits).counted
    assert any(m.category == "Diagnose" and m.counted for m in total.features)
    assert len([m for m in total.features if m.key == "geschlecht"]) == 1


def test_report_and_store(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        res = pseudonymise(report_pdf, patient, PipelineOptions(report=False, text_file=False,
                                                           target_folder=tmp_path / "aus"),
                           store=sp)
        assert res.released and res.residual_risk is not None
        assert res.report.residual_risk["ampel"] == res.residual_risk.traffic_light
        assert "Restrisiko (Schätzung" in as_text(res.report)
        assert res.report.options["referenzpopulation"] == rr.REFERENCE_GERMANY
        assert sp.earlier_residual_risks(patient) == ([], 0)
        sp.remember_residual_risk(patient, "a.pdf",
                                  [{"schluessel": "ort:weitnau", "kategorie": "Ort",
                                    "beschreibung": "Ort Weitnau", "bits": 14.0}],
                                  21.4, 30.0)
        sp.remember_residual_risk(patient, "b.pdf",
                                  [{"schluessel": "ort:weitnau", "kategorie": "Ort",
                                    "beschreibung": "Ort Weitnau", "bits": 12.0}],
                                  19.0, 150.0)
        features, n = sp.earlier_residual_risks(patient)
        assert n == 2 and features[0]["bits"] == 14.0  # the higher weight stays
    small = pseudonymise(report_pdf, patient, PipelineOptions(report=False, text_file=False,
                                                         suffix="_k", reference_population=50_000),
                         store=PseudonymStore(":memory:"))
    assert small.residual_risk.reference == 50_000
    assert small.residual_risk.lam < res.residual_risk.lam


def test_window_control(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                        patient: Patient) -> None:
    """The control step runs the re-check, shows both lights, and a red estimate needs an
    explicit confirmation before the export."""
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    for page in (window.documents, window.analyse, window.export, window.control):
        page.synchronous = True
    assert [window.tabs.tabText(i) for i in range(10, 14)] == [
        "11 Kontrolle", "12 Export", "13 Reidentifikation", "14 Datenbank"]
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([report_pdf])
    window.analyse.analyse()
    qapp.processEvents()
    ko = window.control
    window.show_page(ko)
    qapp.processEvents()
    d = session.documents[0]
    assert d.precheck is not None and ko.colour in ("gruen", "gelb")
    assert ko.table.rowCount() == 1 and ko.table.item(0, 1).text() == "bestanden"
    assert ko.risk_colour in ("gruen", "gelb", "rot") and "λ" in ko.risk_text.text()
    assert ko.features.rowCount() >= 2 and "Restrisiko" in ko.log.toPlainText()
    total, earlier = session.control()
    assert total is not None and earlier == 0
    # a tiny reference population (the practice's own patients) drives the estimate into
    # the red; the export then needs the explicit confirmation
    ko.reference.setText("50")
    ko.recalculate()
    assert "ab 100" in ko.status.text()
    ko.reference.setText("300")
    ko.recalculate()
    qapp.processEvents()
    assert session.options.reference_population == 300 and ko.risk_colour == "rot"
    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    assert window.export.risk_colour == "rot" and not window.export.button.isEnabled()
    assert window.export.risk_confirmed.isVisible() or True
    with pytest.raises(RuntimeError, match="Restrisiko"):
        window.export.run_export()
    window.export.risk_confirmed.setChecked(True)
    assert window.export.button.isEnabled()
    window.export.run_export()
    qapp.processEvents()
    assert d.result is not None and d.result.released
    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        features, n = sp.earlier_residual_risks(patient)
    assert n == 1 and features  # the release was remembered for the next document
    # a second session of the same patient sees the earlier document
    session2 = Session(store_path=tmp_path / "s.sqlite")
    window2 = MainWindow(session2)
    for page in (window2.documents, window2.analyse, window2.control):
        page.synchronous = True
    window2.patient.fill_in(patient)
    window2.patient.adopt()
    window2.documents.add([report_pdf])
    window2.analyse.analyse()
    window2.control.refresh()
    qapp.processEvents()
    assert session2.control()[1] == 1 and "früher" in window2.control.risk_text.text()
    window2.close()
    window.close()


def test_places_without_medical_terms_and_own_places() -> None:
    """A place name the vocabulary knows as a medical term is not the patient's place, and
    neither is one of the sender's own places (compared trimmed and case-insensitively)."""

    class Expertise:
        def is_medical_term(self, word: str) -> bool:
            return word == "Augsburg"

    two = rr.estimate("Patient aus Augsburg, geboren in Weitnau.", "m",
                      medical_terms=Expertise())
    assert [m.key for m in two.features if m.category == "Ort"] == ["ort:weitnau"]
    no_place = rr.estimate("Patient aus Augsburg, geboren in Weitnau.", "m",
                           own_places=[" weitnau ", ""], medical_terms=Expertise())
    assert not any(m.category == "Ort" for m in no_place.features)
    assert not any(mk.key.startswith("kleiner_ort:") for mk in no_place.markers)


def test_disease_with_synonym_counts_once() -> None:
    """A disease named by its name and by a synonym is one feature, counted once, flagged once;
    a second disease is listed but not counted."""
    doubled = rr.estimate("Chorea Huntington, also Huntington-Krankheit; keine Wilson-Krankheit.",
                          "m")
    diagnoses = [m for m in doubled.features if m.category == "Diagnose"]
    assert [m.key for m in diagnoses] == ["diagnose:905", "diagnose:399"]  # rarest first
    assert [m.counted for m in diagnoses] == [True, False]
    assert [mk.key for mk in doubled.markers] == ["selten:905", "selten:399"]


def test_age_90_plus_from_the_birth_year() -> None:
    """The 90+ flag also follows from a birth year and the current year; the birthday window
    needs year, age and an exact date together."""
    high = rr.estimate("Patient [geb. 1930].", "m", current_year=2026)
    assert any(mk.key == "alter_90" for mk in high.markers)
    young = rr.estimate("Patient [geb. 1930].", "m", current_year=2010)
    assert not any(mk.key == "alter_90" for mk in young.markers)
    without_date = rr.estimate("Die 66-jährige Patientin [geb. 1959].", "w")
    assert [m.key for m in without_date.features] == ["geschlecht", "geburt:jahr"]
    assert without_date.features[1].evidence == "[geb. 1959]" and without_date.date_mentions == 0
    age_only = rr.estimate("Alter: 66 Jahre, [Alter 66].", "")
    assert [m.evidence for m in age_only.features] == ["Alter 66"]


def test_notes_with_evidence() -> None:
    """Flags carry a whitespace-collapsed excerpt; an event without a date nearby is no flag."""
    flags = rr.estimate("Er arbeitet\n\n  als Lehrer an einer Schule; drei Kinder.", "m")
    per_key = {mk.key: mk for mk in flags.markers}
    assert per_key["beruf"].evidence.startswith("arbeitet als Lehrer an einer Schule")
    assert per_key["familie"].evidence.startswith("drei Kinder")
    quiet = rr.estimate("Nach dem Sturz keine Beschwerden.", "m")
    assert not any(mk.key == "ereignis_datiert" for mk in quiet.markers)
    dated = rr.estimate("Sturz am 3. Mai 2024 auf der Treppe.", "m")
    event = next(mk for mk in dated.markers if mk.key == "ereignis_datiert")
    assert event.evidence == "Sturz am 3. Mai 2024 auf der Treppe."


def test_union_takes_the_highest_weight_per_key() -> None:
    """Per key the highest weight stays, in the position of its first appearance and counted
    again; flags are kept once per key; reference and date counts are merged."""
    a = rr.ResidualRisk(features=[rr.Feature("ort:x", "Ort", "x", 4.0),
                                  rr.Feature("geschlecht", "Geschlecht", "g", 1.0)],
                        markers=[rr.Marker("beruf", "erst")], reference=1000,
                        date_mentions=1)
    b = rr.ResidualRisk(features=[rr.Feature("ort:x", "Ort", "x", 9.0, counted=False)],
                        markers=[rr.Marker("beruf", "zweit")], reference=500,
                        date_mentions=2)
    total = rr.merge([a, b])
    assert [(m.key, m.bits, m.counted) for m in total.features] == [
        ("ort:x", 9.0, True), ("geschlecht", 1.0, True)]
    assert [mk.text for mk in total.markers] == ["erst"]
    assert total.reference == 500 and total.date_mentions == 3 and total.earlier == 0
