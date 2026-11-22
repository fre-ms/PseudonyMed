"""Identifiers beyond the patient's data: context rules, the table, the pipeline, GUI, CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.checks.report import as_text
from pseudonymed.cli import app
from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import lexicon
from pseudonymed.document.text_file import text_page
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    ident_module.standard.cache_clear()
    yield tmp_path / "daten"
    ident_module.standard.cache_clear()


def _recogniser(*lines: tuple[str, str]) -> ident_module.Recogniser:
    e = ident_module.Recogniser()
    for term, category in lines:
        e.add(term, category)
    return e


def _found(e: ident_module.Recogniser, text: str) -> list[tuple[str, str]]:
    return [(text[k.start:k.end], k.category) for k in e.candidates(text)]


# -- context rules --------------------------------------------------------------------------------
def test_hospitals_with_short_forms() -> None:
    e = _recogniser(("Musterklinik Nord GmbH – Psychiatrische Tagesklinik", "Klinik"),
                    ("Klinik für Geriatrische Rehabilitation", "Klinik"),
                    ("Saarland Heilstätten", "Klinik"))
    e.add_place("Halle (Saale)")
    text = ("Verlegung aus der Musterklinik Nord, später ins Klinikum Halle (Saale) und in die"
            " Klinik für Geriatrische Rehabilitation; das Saarland ist schön. Bericht des"
            " Klinikums Halle.")
    assert _found(e, text) == [("Musterklinik Nord", "Klinik"),
                               ("Klinikum Halle (Saale)", "Klinik"),
                               ("Klinikums Halle", "Klinik")]
    assert ident_module.hospital_forms("Klinik für Geriatrische Rehabilitation") == set()
    assert ident_module.hospital_forms("Caritas") == set()
    assert "Eifelklinik" in ident_module.hospital_forms("Eifelklinik")
    assert _found(e, "Klinikum halle") == []  # proper nouns stay capitalised


def test_professions_only_in_context() -> None:
    e = _recogniser(("Tischler", "Beruf"), ("Arzthelferin", "Beruf"), ("Hausarzt", "Beruf"),
                    ("Bauer", "Beruf"), ("Koch", "Beruf"), ("Angehörige", "Beruf"))
    for sentence in ("Er arbeitet seit 2010 als Tischler.", "Er ist gelernter Tischler.",
                     "Er ist Tischler.", "Er ist Tischler von Beruf.", "Beruf: Tischler",
                     "Er war zuletzt als Tischler tätig.",
                     "Sozialanamnese:\nverheiratet, zwei Kinder, Tischler, Nichtraucher."):
        assert _found(e, sentence) == [("Tischler", "Beruf")], sentence
    for sentence in ("Der Tischler hat angerufen.", "Herr Bauer ist seit Jahren tätig.",
                     "Herr Koch kam zur Kontrolle.", "Der Arbeitgeber und Angehörige wissen es.",
                     "Sozialanamnese:\nverheiratet.\nBefund:\nDer Tischler war da."):
        assert _found(e, sentence) == [], sentence
    # the first profession after a cue takes it
    assert _found(e, "Sie arbeitet als Arzthelferin beim Hausarzt.") == [
        ("Arzthelferin", "Beruf")]


def test_study_acronyms_and_numbers() -> None:
    e = _recogniser(("CARE", "Studie"), ("HoPe", "Studie"), ("Dekan", "Studie"))
    assert "dekan" not in e.index  # plain words are no acronyms
    assert _found(e, "Teilnahme an der CARE-Studie (NCT01234567).") == [
        ("CARE", "Studie"), ("NCT01234567", "Studie")]
    assert _found(e, "Die Studie HoPe läuft.") == [("HoPe", "Studie")]
    assert _found(e, "Sie braucht CARE und care.") == []
    assert _found(e, "Registriert unter DRKS00012345, EudraCT-Nr. 2019-001234-56 und"
                        " EU CT 2023-501234-12-00.") == [
        ("DRKS00012345", "Studie"), ("2019-001234-56", "Studie"),
        ("2023-501234-12-00", "Studie")]
    assert _found(e, "Laborwert 2019-001234-56 ohne Bezeichnung") == []


def _person(text: str, word: str) -> Hit:
    a = text.index(word)
    return Hit(page=0, start=a, end=a + len(word), text=word, kind=Kind.NAME,
               rule="ner.person", pseudonym="Person ABCDEF")


def test_titles_only_next_to_people(patient: Patient) -> None:
    e = _recogniser(("Prof.", "Titel"), ("Dr. med.", "Titel"), ("Dipl.-Ing.", "Titel"),
                    ("MBA", "Titel"), ("zur", "Titel"))
    assignment = PseudonymAssignment(patient, "K7Q4MX", secret=bytes(range(32)))
    text = "Prof. Dr. med. Hans Meier, Dipl.-Ing., MBA, bat zur Kontrolle. Dr. med. allein."
    page = text_page(text)
    matches = ident_module.find_titles(page, e, assignment, [_person(text, "Hans Meier")])
    assert [f.text for f in matches] == ["Prof. Dr. med.", "Dipl.-Ing., MBA"]
    assert all(f.rule == "identifikator.titel" and f.pseudonym.startswith("[Titel ")
               for f in matches)
    assert ident_module.find_titles(page, e, assignment, []) == []
    assert "zur" not in e.index


# -- the table -------------------------------------------------------------------------------------
def test_table_own_entries(fresh: Path, tmp_path: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        assert ident.add(["Musterwerk AG", "Musterwerk AG", "x"]) == 1
        assert ident.add(["Tischler"], "Beruf") == 1
        assert ident.categories() == [("Beruf", 1), ("Sonstiges", 1)]
        v0 = ident.version()
        e = ident.all_items("Sonstiges")[0]
        assert e.own and e.term == "Musterwerk AG"
        new = ident.change(e.key, term="Musterwerk GmbH")
        assert new is not None and new.term == "Musterwerk GmbH" and ident.version() > v0
        assert ident.change(new.key, category="Klinik").category == "Klinik"
        assert ident.change(ident.all_items("Beruf")[0].key, term="ab") is None
        csv = tmp_path / "ident.csv"
        assert ident.export_csv(csv) == 2
        assert csv.read_text(encoding="utf-8").splitlines()[0] == "begriff,klasse,quelle,angaben"
        assert ident.remove(ident.keys_for("tischler")) == 1
        assert ident.import_csv(csv) == (1, 2)  # the hospital row exists already
        listing = tmp_path / "liste.txt"
        listing.write_text("# eigene\nSportverein Musterdorf\n", encoding="utf-8")
        assert ident.import_csv(listing) == (1, 1)
        assert ident.deleted() == 0  # own rows leave no trace
    assert ident_module.standard().per_class == {"Klinik": 1, "Beruf": 1, "Sonstiges": 1}


def test_deleted_bundled_entries_stay_deleted(fresh: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        lexicon.ensure(ident.connection, force=True, sources=["Kliniken"])
        all_items = ident.all_items("Klinik")
        assert len(all_items) > 4000 and all(e.source == "Kliniken" for e in all_items)
        victim, renamed = all_items[0], all_items[1]
        assert victim.details.endswith(")")  # town and country
        assert ident.remove([victim.key]) == 1
        own = ident.change(renamed.key, term=renamed.term + " Nord")
        assert own is not None and own.own and own.code == renamed.code
        assert ident.deleted() == 2
        lexicon.ensure(ident.connection, force=True, sources=["Kliniken"])
        key = {e.key for e in ident.all_items("Klinik")}
        assert victim.key not in key and renamed.key not in key
        assert own.key in key
        assert ident.restore() == 2
        key = {e.key for e in ident.all_items("Klinik")}
        assert victim.key in key and renamed.key in key
        assert ident.deleted() == 0


def test_employment_status_and_study_numbers_invisible(fresh: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.connection.executemany(
            "INSERT INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, 'merkmal', ?, ?, ?)",
            [("Rentnerin", "Erwerbsstatus", "Berufe", "", ""),
             ("NCT01234567", "Studie", "Studien", "ClinicalTrials.gov:NCT01234567", '{"x":1}'),
             ("ALPHA", "Studie", "Studien", "ClinicalTrials.gov:NCT01234567", "")])
        ident.connection.commit()
        assert [(e.term, e.details) for e in ident.all_items()] == [("ALPHA", "NCT01234567")]


def test_details_per_category() -> None:
    def e(category: str, code: str = "", extra: str = "") -> ident_module.Entry:
        return ident_module.Entry("Begriff", category, "Quelle", code, extra)

    assert e("Klinik", extra='{"ort": "Musterstadt", "land": "DE"}').details == "Musterstadt (DE)"
    assert e("Klinik", extra='{"ort": "Musterstadt"}').details == "Musterstadt"
    assert e("Klinik", extra='{"land": "DE"}').details == "(DE)"
    assert e("Klinik", extra="{").details == "" and e("Klinik").details == ""
    assert e("Studie", "ClinicalTrials.gov:NCT01234567").details == "NCT01234567"
    assert e("Studie", "NCT01234567").details == ""
    assert e("Titel", "alle|Medizin|").details == "Medizin" and e("Titel", "Dr.").details == ""
    assert e("Beruf", "12345").details == "KldB 12345" and e("Beruf", "abc").details == ""
    assert e("Sonstiges", "12345", '{"ort": "x"}').details == ""


def test_default_knows_the_places_of_the_hospitals(fresh: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.connection.executemany(
            "INSERT INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, 'Klinik', 'merkmal', 'Kliniken', ?, ?)",
            [("Musterklinik Musterstadt", "K1", '{"ort": "Musterstadt", "land": "DE"}'),
             ("Beispielklinik", "K2", "kein JSON"), ("Fachklinik Nord", "K3", "")])
        ident.connection.commit()
    e = ident_module.standard()
    assert e.per_class == {"Klinik": 3}
    text = "Verlegt ins Krankenhaus Musterstadt, danach in die Musterklinik Musterstadt."
    assert _found(e, text) == [("Krankenhaus Musterstadt", "Klinik"),
                               ("Musterklinik Musterstadt", "Klinik")]


# -- the pipeline ----------------------------------------------------------------------------------
def test_text_file_redacts_identifiers(fresh: Path, tmp_path: Path, patient: Patient,
                                       store: PseudonymStore) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.add(["Tischler"], "Beruf")
        ident.add(["Musterklinik Nord"], "Klinik")
        ident.add(["Sportverein Musterdorf"])
    source = tmp_path / "notiz.txt"
    source.write_text(
        f"{patient.first_name} {patient.surname} arbeitet als Tischler und ist im Sportverein"
        " Musterdorf aktiv. Aufnahme aus der Musterklinik Nord am Vortag. Teilnahme an der"
        " Studie unter NCT01234567. Der Tischler aus dem Nachbarort hat geholfen.\n",
        encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    text = res.target_txt.read_text(encoding="utf-8")
    for plaintext in ("arbeitet als Tischler", "Sportverein Musterdorf", "Musterklinik Nord",
                      "NCT01234567"):
        assert plaintext not in text, text
    assert "Der Tischler aus dem Nachbarort" in text  # no cue: stays
    for prefix in ("[Beruf ", "[Identifikator ", "[Klinik ", "[Studie "):
        assert prefix in text, text
    assert any(h.startswith("Identifikatoren: 4 Treffer") for h in res.report.notes)
    assert res.report.options["identifikatoren"] is True
    z = store.mapping(patient.key())
    back = reidentify(text, z, known_codes=store.codes())
    assert "arbeitet als Tischler" in back.text and "Musterklinik Nord" in back.text
    out = pseudonymise(source, patient, PipelineOptions(identifiers=False, suffix="_aus"),
                       store=store)
    assert "Musterklinik Nord" in out.target_txt.read_text(encoding="utf-8")


def test_pdf_with_box_and_title(fresh: Path, tmp_path: Path, patient: Patient,
                                store: PseudonymStore) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.add(["Musterklinik Nord"], "Klinik")
        ident.add(["Dr. med."], "Titel")
    pdf = tmp_path / "brief.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"Patient: Herr Dr. med. {patient.first_name} {patient.surname}",
                     fontsize=11)
    page.insert_text((72, 120), "Verlegt aus der Musterklinik Nord.", fontsize=11)
    doc.save(pdf)
    doc.close()
    res = pseudonymise(pdf, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    matches = [f for f in res.hits if f.kind == Kind.IDENTIFIER]
    assert {f.rule for f in matches} == {"identifikator.klinik", "identifikator.titel"}
    assert all(f.rects for f in matches)
    with pymupdf.open(res.target_pdf) as out:
        rest = out[0].get_text()
    assert "Musterklinik" not in rest and "Dr. med." not in rest


# -- GUI and command line --------------------------------------------------------------------------
def test_page(qapp: QApplication, fresh: Path, tmp_path: Path) -> None:  # noqa: F811
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    window = MainWindow(Session())
    page = window.identifiers
    page.synchronous = True
    assert window.tabs.tabText(1) == "2 Identifikatoren"
    window.show_page(page)
    qapp.processEvents()
    assert page.model.rowCount() == 0
    assert page.add("Musterwerk AG") == 1
    assert page.add("Tischler", "Beruf") == 1
    assert page.model.rowCount() == 2 and page.count.text().startswith("2 von 2")
    page.search.setText("muster")
    assert page.proxy.rowCount() == 1
    page.search.setText("")
    line = next(i for i, e in enumerate(page.model.entries) if e.term == "Tischler")
    assert page.model.setData(page.model.index(line, 0), "Schreiner")
    assert page.model.entries[line].term == "Schreiner"
    i = page.category_filter.findData("Beruf")
    page.category_filter.setCurrentIndex(i)
    assert page.model.rowCount() == 1
    csv = tmp_path / "ident.csv"
    assert page.export_csv(csv) == 1
    page.category_filter.setCurrentIndex(0)
    assert page.delete(page.model.entries) == 2
    assert page.import_file(csv) == 1
    assert page.model.entries[0].category == "Beruf"
    window.analyse.identifiers.setChecked(False)
    window.analyse._apply_options()
    assert window.session.options.identifiers is False
    window.close()


def test_command_line(fresh: Path) -> None:
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
