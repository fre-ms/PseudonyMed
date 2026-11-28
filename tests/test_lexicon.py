"""The bundled reference data: the medical classes read from the files, the identifier lists
installed as packages and installed again when their data changed, and the protection they
leave alone."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pseudonymed.detection import lexicon
from pseudonymed.detection.identifiers import IdentifierTable


@pytest.fixture
def lexicon_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_LEXICON", "1")


def test_medical_classes_come_from_the_bundled_files(lexicon_on: None) -> None:
    rows = lexicon.medical_rows()
    classes = {category for _term, category, _source in rows}
    assert {"anatomy", "drug", "disease", "lab_test", "finding", "eponym"} <= classes
    assert ("NfL", "lab_test", "lab tests") in rows
    assert any(term == "Levetiracetam" and category == "drug"
               for term, category, _source in rows)
    assert {source for _t, _k, source in rows} == {
        "wikidata", "body parts", "findings terms", "lab tests", "eponyms"}
    assert not any(category in ("occupation", "hospital", "title", "study")
                   for _t, category, _s in rows)
    assert lexicon.medical_rows() is rows  # read once while the files are the same
    assert len(lexicon.medical_state()) == 5


def test_switched_off_there_are_no_bundled_lists(tmp_path: Path) -> None:
    assert not lexicon.active()
    assert lexicon.medical_rows() == [] and lexicon.medical_state() == ()
    with IdentifierTable(tmp_path / "lexident.sqlite") as table:
        assert table.seed() == {} and table.count() == 0 and table.store.packages() == []


def test_identifier_rows_of_the_bundled_files() -> None:
    by_name = {q.name: q for q in lexicon.available()}
    assert set(by_name) == set(lexicon.FEATURE_SOURCES)
    assert set(by_name) == {"occupations", "titles", "hospitals", "studies"}
    professions = list(lexicon.rows_of(by_name["occupations"]))
    assert {category for _b, category, *_rest in professions} == {
        "occupation", "employment_status"}
    surname = next(extra for term, category, _r, _c, extra in professions
                   if term == "Bauer" and category == "occupation")
    assert '"surname":1' in surname and '"base_form":"Bauer"' in surname
    titles = {term: extra for term, _k, _r, _c, extra in lexicon.rows_of(by_name["titles"])}
    assert "Dr. med." in titles and '"kind":"academic"' in titles["Dr. med."]
    assert all(role == lexicon.FEATURE for _b, _k, role, _c, _e in professions)
    assert lexicon.note(by_name["hospitals"]).startswith("Source:")


def test_installed_again_only_when_data_changed(tmp_path: Path, lexicon_on: None,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "occupations.csv.gz"
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Source: synthetic\nterm,class,code,base_form,surname\n"
                "Bäckerin,occupation,29202,Bäcker,\nBauer,occupation,11104,Bauer,1\n"
                "Rentnerin,employment_status,,Rentner,\n")
    monkeypatch.setattr(lexicon, "SOURCES", (lexicon.Source("occupations", file,
                                                            lexicon._professions),))
    store = tmp_path / "lexident.sqlite"
    with IdentifierTable(store) as table:
        assert [e.term for e in table.all_items()] == ["Bauer", "Bäckerin"]
        assert {e.category for e in table.all_items()} == {"occupation"}
        assert table.add(["Eigene Klinik"], "hospital") == 1
        assert table.remove(table.keys_for("Bauer")) == 1
        url = table.store.local_url("occupations")
        assert table.store.properties(url, "11104") == {"base_form": "Bauer", "surname": "1"}
        assert table.store.info(url).copyright == "Source: synthetic"
        version = table.version()
        assert table.seed() == {}
    with IdentifierTable(store) as table:
        assert table.version() == version  # nothing changed: nothing written
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Source: synthetic\nterm,class,code,base_form,surname\n"
                "Bauer,occupation,11104,Bauer,1\nMüllerin,occupation,29232,Müller,\n")
    with IdentifierTable(store) as table:  # the file changed: the list is installed again
        assert [(e.term, e.source) for e in table.all_items()] == [
            ("Eigene Klinik", "user"), ("Müllerin", "occupations")]  # "Bauer" stays deleted
        assert table.deleted() == 1 and table.restore() == 1
        assert [e.term for e in table.all_items("occupation")] == ["Bauer", "Müllerin"]


def test_does_not_change_the_protection(tmp_path: Path, lexicon_on: None,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Professions, titles, hospitals and studies must never become protected terms."""
    from pseudonymed.detection import medical_terms

    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path))
    medical_terms.standard.cache_clear()
    with IdentifierTable() as table:
        assert dict(table.categories())["occupation"] > 20000
        table.add(["Flantzbauer"], "occupation")
    try:
        matcher = medical_terms.standard()
        text = "Herr Bauer berichtet über Kopfschmerz."
        is_protected = {text[a:b] for a, b in matcher.spans(text)}
        assert "Bauer" not in is_protected and "Kopfschmerz" in is_protected
        assert not matcher.is_medical_term("Flantzbauer")
    finally:
        medical_terms.standard.cache_clear()


def test_statistics_show_the_identifiers(tmp_path: Path, lexicon_on: None,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.pseudonyms.statistics import statistics

    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path))
    empty = dict(statistics().lines())  # no store yet: nothing is created to count it
    assert "Identifikatoren (Einträge)" not in empty and not (tmp_path / "lexident.sqlite").exists()
    IdentifierTable().close()
    st = statistics()
    assert dict(st.identifier_categories)["hospital"] > 4000  # counted by the stored class
    lines = dict(st.lines())
    assert int(lines["Identifikatoren (Einträge)"]) > 40000
    # the lines to display name each class by its label
    assert int(lines["    Klinik"]) > 4000 and "    Beruf" in lines


def test_gemtex_types() -> None:
    assert lexicon.gemtex_type("hospital") == "LOCATION_HOSPITAL"
    assert lexicon.gemtex_type("study") == "ID"
    assert lexicon.gemtex_type("occupation") == "PROFESSION"
    assert lexicon.gemtex_type("employment_status") == ""
    assert lexicon.gemtex_type("title", '{"kind":"academic"}') == "NAME_TITLE"
    assert lexicon.gemtex_type("title") == "NAME_TITLE"
    assert lexicon.gemtex_type("title", '{"kind":"nobility"}') == ""
    assert lexicon.gemtex_type("anatomy") == ""
