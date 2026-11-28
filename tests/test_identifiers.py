"""Identifiers beyond the patient's data: lexident's places named by PseudonyMed's classes,
the table over lexident's store, the pipeline, GUI, CLI.  (The rules of context are tested
in lexident, where they live; the cases here guard the seam.  The take-over of the former
tables is in ``test_identifiers_move.py``.)"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import lexident
import pymupdf
import pytest
from lexcor import Change
from lexident.forms import hospital_forms
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.checks.report import as_text
from pseudonymed.cli import app
from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.detection import identifier_lists as lists
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.document.text_file import text_page
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path / "daten"))
    yield tmp_path / "daten"


def _recogniser(*lines: tuple[str, str]) -> ident_module.Recogniser:
    """lexident's recogniser over entries given with PseudonyMed's classes."""
    e = lexident.Recogniser()
    for term, category in lines:
        e.add(term, lists.cls_of(category))
    return e


def _found(e: ident_module.Recogniser, text: str) -> list[tuple[str, str]]:
    return [(text[k.start:k.end], ident_module.category_of(k.classes))
            for k in e.candidates(text)]


# -- context rules --------------------------------------------------------------------------------
def test_hospitals_with_short_forms() -> None:
    e = _recogniser(("Musterklinik Nord GmbH – Psychiatrische Tagesklinik", "hospital"),
                    ("Klinik für Geriatrische Rehabilitation", "hospital"),
                    ("Saarland Heilstätten", "hospital"))
    e.add("Halle (Saale)", lexident.HOSPITAL_TOWN)
    text = ("Verlegung aus der Musterklinik Nord, später ins Klinikum Halle (Saale) und in die"
            " Klinik für Geriatrische Rehabilitation; das Saarland ist schön. Bericht des"
            " Klinikums Halle.")
    assert _found(e, text) == [("Musterklinik Nord", "hospital"),
                               ("Klinikum Halle (Saale)", "hospital"),
                               ("Klinikums Halle", "hospital")]
    assert hospital_forms("Klinik für Geriatrische Rehabilitation") == set()
    assert hospital_forms("Caritas") == set()
    assert "Eifelklinik" in hospital_forms("Eifelklinik")
    assert _found(e, "Klinikum halle") == []  # proper nouns stay capitalised


def test_professions_only_in_context() -> None:
    e = _recogniser(*((term, "occupation") for term in (
        "Tischler", "Arzthelferin", "Hausarzt", "Bauer", "Koch", "Angehörige")))
    for sentence in ("Er arbeitet seit 2010 als Tischler.", "Er ist gelernter Tischler.",
                     "Er ist Tischler.", "Er ist Tischler von Beruf.", "Beruf: Tischler",
                     "Er war zuletzt als Tischler tätig.",
                     "Sozialanamnese:\nverheiratet, zwei Kinder, Tischler, Nichtraucher."):
        assert _found(e, sentence) == [("Tischler", "occupation")], sentence
    for sentence in ("Der Tischler hat angerufen.", "Herr Bauer ist seit Jahren tätig.",
                     "Herr Koch kam zur Kontrolle.", "Der Arbeitgeber und Angehörige wissen es.",
                     "Sozialanamnese:\nverheiratet.\nBefund:\nDer Tischler war da."):
        assert _found(e, sentence) == [], sentence
    # the first profession after a cue takes it
    assert _found(e, "Sie arbeitet als Arzthelferin beim Hausarzt.") == [
        ("Arzthelferin", "occupation")]


def test_study_acronyms_and_numbers() -> None:
    e = _recogniser(("CARE", "study"), ("HoPe", "study"), ("Dekan", "study"))
    assert "dekan" not in e.index  # plain words are no acronyms
    assert _found(e, "Teilnahme an der CARE-Studie (NCT01234567).") == [
        ("CARE", "study"), ("NCT01234567", "study")]
    assert _found(e, "Die Studie HoPe läuft.") == [("HoPe", "study")]
    assert _found(e, "Sie braucht CARE und care.") == []
    assert _found(e, "Registriert unter DRKS00012345, EudraCT-Nr. 2019-001234-56 und"
                        " EU CT 2023-501234-12-00.") == [
        ("DRKS00012345", "study"), ("2019-001234-56", "study"),
        ("2023-501234-12-00", "study")]
    assert _found(e, "Laborwert 2019-001234-56 ohne Bezeichnung") == []


def _person(text: str, word: str) -> Hit:
    a = text.index(word)
    return Hit(page=0, start=a, end=a + len(word), text=word, kind=Kind.NAME,
               rule="ner.person", pseudonym="Person ABCDEF")


def test_titles_only_next_to_people(patient: Patient) -> None:
    e = _recogniser(("Prof.", "title"), ("Dr. med.", "title"), ("Dipl.-Ing.", "title"),
                    ("MBA", "title"), ("zur", "title"))
    assignment = PseudonymAssignment(patient, "K7Q4MX", secret=bytes(range(32)))
    text = "Prof. Dr. med. Hans Meier, Dipl.-Ing., MBA, bat zur Kontrolle. Dr. med. allein."
    page = text_page(text)
    matches = ident_module.find_titles(page, e, assignment, [_person(text, "Hans Meier")])
    assert [f.text for f in matches] == ["Prof. Dr. med.", "Dipl.-Ing., MBA"]
    assert all(f.rule == "identifier.title" and f.pseudonym.startswith("[Titel ")
               for f in matches)
    assert ident_module.find_titles(page, e, assignment, []) == []
    assert "zur" not in e.index


# -- one class for a place -------------------------------------------------------------------------
def test_a_place_of_several_classes_is_named_by_one() -> None:
    """lexident gives a place every class whose rule holds there; PseudonyMed names it by
    one: hospital, profession, study, a class of the user's own (by name), the catch-all."""
    e = _recogniser(("Waldklinik Tannengrund", "hospital"), ("Waldklinik Tannengrund", "other"),
                    ("Waldklinik Tannengrund", "Arbeitgeber"), ("Tischler", "occupation"),
                    ("Tischler", "other"), ("Tischler", "Verein"), ("Tischler", "Amt"),
                    ("CARE", "study"), ("CARE", "occupation"), ("CARE", "other"))
    text = "Sie arbeitet als Tischler in der Waldklinik Tannengrund, Studie CARE."
    places = e.candidates(text)
    assert [len(k.classes) for k in places] == [4, 3, 2]  # no cue for CARE as a profession
    assert _found(e, text) == [("Tischler", "occupation"),
                               ("Waldklinik Tannengrund", "hospital"), ("CARE", "study")]
    both = "Sie arbeitet als CARE, Teilnahme an der Studie."  # a cue for either class
    assert [k.classes for k in e.candidates(both)] == [("ID", "OTHER", "PROFESSION")]
    assert _found(e, both) == [("CARE", "occupation")]
    # no cue: no occupation
    assert _found(e, "Der Tischler hat angerufen.") == [("Tischler", "Amt")]
    assert ident_module.category_of(["OTHER", "Verein"]) == "Verein"
    assert ident_module.category_of(["OTHER"]) == "other"
    assert ident_module.category_of(["NAME_TITLE"]) == "title"


# -- the table -------------------------------------------------------------------------------------
def test_table_own_entries(fresh: Path, tmp_path: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        assert ident.path == fresh / "lexident.sqlite" == ident_module.default_path()
        assert ident.add(["Musterwerk AG", "Musterwerk AG", "musterwerk ag", "x"]) == 1
        assert ident.add(["Tischler"], "Beruf") == 1  # the German name of a class is understood
        assert ident.categories() == [("occupation", 1), ("other", 1)]  # and stored in English
        own = {info.title: info for info in ident.store.packages()}
        assert set(own) == {"user: occupation", "user: other"}
        assert all(info.url == ident.store.local_url(title) and not info.redistributable
                   for title, info in own.items())
        assert sorted((e.term, e.cls, e.language) for e in ident.store.entries()) == [
            ("Musterwerk AG", "OTHER", "de"), ("Tischler", "PROFESSION", "de")]
        v0 = ident.version()
        e = ident.all_items("other")[0]
        assert e.own and e.term == "Musterwerk AG" and e.code == "" and e.source == "user"
        assert ident.all_items("Sonstiges") == [e]  # a filter by the German name as well
        new = ident.change(e.key, term="Musterwerk GmbH")
        assert new is not None and new.term == "Musterwerk GmbH" and ident.version() > v0
        assert ident.change(new.key, category="Klinik").category == "hospital"
        assert ident.change(ident.all_items("occupation")[0].key, term="ab") is None
        assert ident.change(("gibtesnicht", "occupation", ""), term="Flantzwerk") is None
        csv = tmp_path / "ident.csv"
        assert ident.export_csv(csv) == 2
        assert csv.read_text(encoding="utf-8").splitlines() == [
            "term,class,source,details", "Musterwerk GmbH,hospital,user,",
            "Tischler,occupation,user,"]
        assert ident.remove(ident.keys_for("tischler", "Beruf")) == 1
        assert "user: occupation" not in {i.title for i in ident.store.packages()}  # really gone
        assert ident.import_csv(csv) == (1, 2)  # the hospital row exists already
        listing = tmp_path / "liste.txt"
        listing.write_text("# eigene\nSportverein Musterdorf\n", encoding="utf-8")
        assert ident.import_csv(listing) == (1, 1)
        # a file with German column and class names: read as the English ones
        german = tmp_path / "klassen.csv"
        german.write_text("begriff;klasse\nFlantzhaus Nord;Klinik\n"
                          "Sportverein Musterdorf;Sonstiges\n", encoding="utf-8")
        assert ident.import_csv(german) == (1, 2)  # the club is an entry of "other" already
        assert [e.category for e in ident.all_items() if e.term == "Flantzhaus Nord"] == [
            "hospital"]
        assert ident.add(["Schmiedin"], "Handwerk") == 1  # a class of the user's own stays
        assert dict(ident.categories()) == {"hospital": 2, "occupation": 1, "other": 1,
                                            "Handwerk": 1}
        assert ident.deleted() == 0 and ident.store.overlay() == []  # own rows leave no trace
        assert ident.loaded() == []
    assert ident_module.standard().per_class == {
        "LOCATION_HOSPITAL": 2, "PROFESSION": 1, "OTHER": 1, "Handwerk": 1}


@pytest.fixture
def bundled(fresh: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The bundled lists switched on (the suite runs without them)."""
    monkeypatch.setenv("PSEUDONYMED_LEXICON", "1")
    return fresh


def test_the_bundled_lists_are_packages(bundled: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        infos = {info.title: info for info in ident.store.packages()}
        assert set(infos) == {"occupations", "hospitals", "studies", "titles", lists.TOWNS}
        assert infos["hospitals"].terms > 4000 and infos["occupations"].terms > 20000
        # a code has several names
        assert infos["occupations"].concepts < infos["occupations"].terms
        # what the file says of itself
        assert infos["hospitals"].copyright.startswith("Source:")
        assert not any(info.redistributable for info in infos.values())
        assert dict(ident.categories()).keys() == {"occupation", "hospital", "study", "title"}
        classes = {e.cls for e in ident.store.entries()}
        assert classes == {"PROFESSION", "LOCATION_HOSPITAL", "ID", "NAME_TITLE", "hospital-town"}
        # no employment status, no registry number: neither is shown or looked for
        assert not ident.keys_for("Rentnerin") and not ident.keys_for("arbeitslos")
        study = next(e for e in ident.all_items("study") if e.details)
        assert not ident.keys_for(study.details)  # the number is the concept's code, no term
        said_of_study = ident.store.properties(ident.store.local_url("studies"), study.code)
        assert "participants" in said_of_study or "countries" in said_of_study
        # what the rows of one occupation code say differs by term: a JSON object by term
        professions = ident.store.package(ident.store.local_url("occupations"))
        concept = next(c for c in professions.concepts if len(c.terms) > 1
                       and c.properties.get("base_form", "").startswith("{"))
        said = lists.unfold(concept.properties, concept.terms[0].value)
        assert set(said) <= {"base_form", "surname"} and "base_form" in said
        # a hospital says where it is, in the English keys of the bundled file
        hospital = ident.all_items("hospital")[0]
        assert {"place", "postcode", "country", "type"} <= lists.addition_of(hospital.extra).keys()
        version = ident.version()
        assert ident.seed() == {}  # the data is the one the store holds
    with ident_module.IdentifierTable() as ident:
        assert ident.version() == version  # and the next start leaves it alone


def test_deleted_bundled_entries_stay_deleted(bundled: Path) -> None:
    with ident_module.IdentifierTable() as ident:
        all_items = ident.all_items("hospital")
        assert len(all_items) > 4000 and all(e.source == "hospitals" for e in all_items)
        victim, renamed = all_items[0], all_items[1]
        assert victim.details.endswith(")")  # town and country
        url = ident.store.local_url("hospitals")
        assert ident.remove([victim.key]) == 1
        assert ident.store.overlay() == [Change(url, victim.code, "hide", victim.term, "de")]
        own = ident.change(renamed.key, term=renamed.term + " Nord")
        assert own is not None and own.own and own.code == renamed.code
        assert own.details == renamed.details  # the town came along
        assert ident.deleted() == 2
        assert ident.seed(force=True).keys() == {"occupations", "titles", "hospitals", "studies"}
        key = {e.key for e in ident.all_items("hospital")}
        assert victim.key not in key and renamed.key not in key
        assert own.key in key
        assert ident.restore() == 2
        key = {e.key for e in ident.all_items("hospital")}
        assert victim.key in key and renamed.key in key and own.key in key
        assert ident.deleted() == 0


def test_details_per_category() -> None:
    def e(category: str, code: str = "", extra: str = "") -> ident_module.Entry:
        return ident_module.Entry("Begriff", category, "source", code, extra)

    assert e("hospital", extra='{"place": "Musterstadt", "country": "DE"}').details == \
        "Musterstadt (DE)"
    assert e("hospital", extra='{"place": "Musterstadt"}').details == "Musterstadt"
    assert e("hospital", extra='{"country": "DE"}').details == "(DE)"
    assert e("hospital", extra="{").details == "" and e("hospital").details == ""
    assert e("study", "ClinicalTrials.gov:NCT01234567").details == "NCT01234567"
    assert e("study", "NCT01234567").details == ""
    assert e("title", "all|Medizin|").details == "Medizin" and e("title", "Dr.").details == ""
    # the kind of a title is stored in English and shown by its label
    title = e("title", "academic|DE|Doktor der Medizin")
    assert title.details == "academic · DE · Doktor der Medizin"
    assert title.details_in("de") == "akademisch · DE · Doktor der Medizin"
    assert title.details_in("en") == title.details
    assert e("occupation", "12345").details == "KldB 12345"
    assert e("occupation", "abc").details == ""
    assert e("other", "12345", '{"place": "x"}').details == ""


def test_the_towns_follow_the_hospitals(fresh: Path) -> None:
    """A town that has a listed hospital gives "Klinikum <town>"; the list of the towns is
    made from the hospitals in force and follows them."""
    rows = [lists.Row("Musterklinik Musterstadt", "hospital", "K1",
                      {"place": "Musterstadt", "country": "DE"}),
            lists.Row("Beispielklinik", "hospital", "K2", {"place": "Beispielhausen"}),
            lists.Row("Fachklinik Nord", "hospital", "K3")]
    with ident_module.IdentifierTable() as ident:
        assert ident.install("Probekliniken", rows, "2026-11-01 abc") == 3
        assert ident.loaded() == [("Probekliniken", 3, "2026-11-01 abc")]
        towns = ident.store.local_url(lists.TOWNS)
        assert [c.code for c in ident.store.package(towns).concepts] == [
            "Beispielhausen", "Musterstadt"]
        assert ident.count() == 3  # the towns are no identifiers of their own
    e = ident_module.standard()
    assert e.per_class == {"LOCATION_HOSPITAL": 3}
    text = "Verlegt ins Krankenhaus Musterstadt, danach in die Musterklinik Musterstadt."
    assert _found(e, text) == [("Krankenhaus Musterstadt", "hospital"),
                               ("Musterklinik Musterstadt", "hospital")]
    with ident_module.IdentifierTable() as ident:
        assert ident.remove(ident.keys_for("Musterklinik Musterstadt")) == 1
        assert [c.code for c in ident.store.package(towns).concepts] == ["Beispielhausen"]
        moved = ident.change(ident.keys_for("Beispielklinik")[0], term="Beispielklinik Süd")
        assert moved is not None and moved.own and moved.details == "Beispielhausen"
        assert [c.code for c in ident.store.package(towns).concepts] == ["Beispielhausen"]
        assert ident.remove([moved.key]) == 1  # the last hospital of the town: the town goes
        assert towns not in {info.url for info in ident.store.packages()}
    assert _found(ident_module.standard(), text) == []


def test_a_list_another_program_made_is_shown_and_can_be_hidden(fresh: Path) -> None:
    """lexident's store is shared.  A list another program put there without a URL of its
    own is one of the store's own lists: it is looked for, is shown under its title and
    class, and is never written over.  (What brings a URL of its own is another matter:
    ``test_foreign_packages.py``.)"""
    from lexcor import Concept, Package, Term

    with ident_module.IdentifierTable() as ident:
        url = ident.store.add_package(Package(
            url="", version="1", name="Vereinsliste", title="Vereinsliste", language="de",
            concepts=[Concept("V1", "Schützenverein Flantzdorf", "Verein", "", [
                Term("Schützenverein Flantzdorf", "de"), Term("SV Flantzdorf", "de")])])).url
        assert [(e.term, e.category, e.source, e.code, e.own) for e in ident.all_items()] == [
            ("Schützenverein Flantzdorf", "Verein", "Vereinsliste", "V1", False),
            ("SV Flantzdorf", "Verein", "Vereinsliste", "V1", False)]
        assert ident.loaded() == [("Vereinsliste", 2, "1")]
        assert ident.remove(ident.keys_for("sv flantzdorf")) == 1
        assert ident.store.overlay(url) == [Change(url, "V1", "hide", "SV Flantzdorf", "de")]
        assert ident.store.info(url).terms == 2 and ident.count() == 1
    found = _found(ident_module.standard(),
                   "Mitglied im Schützenverein Flantzdorf (SV Flantzdorf).")
    assert found == [("Schützenverein Flantzdorf", "Verein")]


# -- the pipeline ----------------------------------------------------------------------------------
def test_text_file_redacts_identifiers(fresh: Path, tmp_path: Path, patient: Patient,
                                       store: PseudonymStore) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.add(["Tischler"], "occupation")
        ident.add(["Musterklinik Nord"], "hospital")
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
    assert res.report.options["identifiers"] is True
    z = store.mapping(patient.key())
    back = reidentify(text, z, known_codes=store.codes())
    assert "arbeitet als Tischler" in back.text and "Musterklinik Nord" in back.text
    out = pseudonymise(source, patient, PipelineOptions(identifiers=False, suffix="_aus"),
                       store=store)
    assert "Musterklinik Nord" in out.target_txt.read_text(encoding="utf-8")


def test_pdf_with_box_and_title(fresh: Path, tmp_path: Path, patient: Patient,
                                store: PseudonymStore) -> None:
    with ident_module.IdentifierTable() as ident:
        ident.add(["Musterklinik Nord"], "hospital")
        ident.add(["Dr. med."], "title")
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
    assert {f.rule for f in matches} == {"identifier.hospital", "identifier.title"}
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
    assert page.add("Tischler", "Beruf") == 1  # the class as the German interface names it
    assert page.model.rowCount() == 2 and page.count.text().startswith("2 von 2")
    line = next(i for i, e in enumerate(page.model.entries) if e.term == "Tischler")
    assert page.model.entries[line].category == "occupation"  # stored in English
    assert page.model.data(page.model.index(line, 1)) == "Beruf"  # shown by its label
    page.search.setText("muster")
    assert page.proxy.rowCount() == 1
    page.search.setText("")
    line = next(i for i, e in enumerate(page.model.entries) if e.term == "Tischler")
    assert page.model.setData(page.model.index(line, 0), "Schreiner")
    assert page.model.entries[line].term == "Schreiner"
    i = page.category_filter.findData("occupation")
    assert page.category_filter.itemText(i) == "Beruf (1)"
    page.category_filter.setCurrentIndex(i)
    assert page.model.rowCount() == 1
    csv = tmp_path / "ident.csv"
    assert page.export_csv(csv) == 1
    page.category_filter.setCurrentIndex(0)
    assert page.delete(page.model.entries) == 2
    assert page.import_file(csv) == 1
    assert page.model.entries[0].category == "occupation"
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
    r = runner.invoke(app, ["identifikatoren", "status"])
    assert "lexident.sqlite" in r.output and "0 Identifikatoren" in r.output, r.output
