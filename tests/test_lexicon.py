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
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")


def test_medical_classes_come_from_the_bundled_files(lexicon_on: None) -> None:
    rows = lexicon.medical_rows()
    classes = {category for _term, category, _source in rows}
    assert {"Anatomie", "Medikament", "Erkrankung", "Laborwert", "Befund", "Eponym"} <= classes
    assert ("NfL", "Laborwert", "Laborwerte") in rows
    assert any(term == "Levetiracetam" and category == "Medikament"
               for term, category, _source in rows)
    assert {source for _t, _k, source in rows} == {
        "Wikidata", "Körperteile", "Befundbegriffe", "Laborwerte", "Eponyme"}
    assert not any(category in ("Beruf", "Klinik", "Titel", "Studie") for _t, category, _s in rows)
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
    professions = list(lexicon.rows_of(by_name["Berufe"]))
    assert {category for _b, category, *_rest in professions} == {"Beruf", "Erwerbsstatus"}
    surname = next(extra for term, category, _r, _c, extra in professions
                   if term == "Bauer" and category == "Beruf")
    assert '"nachname":1' in surname
    titles = {term for term, *_rest in lexicon.rows_of(by_name["Titel"])}
    assert "Dr. med." in titles
    assert all(role == lexicon.FEATURE for _b, _k, role, _c, _e in professions)
    assert lexicon.note(by_name["Kliniken"]).startswith("Quelle:")


def test_installed_again_only_when_data_changed(tmp_path: Path, lexicon_on: None,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "berufe.csv.gz"
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Quelle: synthetisch\nbegriff,klasse,kennung,grundform,nachname\n"
                "Bäckerin,Beruf,29202,Bäcker,\nBauer,Beruf,11104,Bauer,1\n"
                "Rentnerin,Erwerbsstatus,,Rentner,\n")
    monkeypatch.setattr(lexicon, "SOURCES", (lexicon.Source("Berufe", file,
                                                            lexicon._professions),))
    store = tmp_path / "lexident.sqlite"
    with IdentifierTable(store) as table:
        assert [e.term for e in table.all_items()] == ["Bauer", "Bäckerin"]
        assert table.add(["Eigene Klinik"], "Klinik") == 1
        assert table.remove(table.keys_for("Bauer")) == 1
        url = table.store.local_url("Berufe")
        assert table.store.properties(url, "11104") == {"grundform": "Bauer", "nachname": "1"}
        assert table.store.info(url).copyright == "Quelle: synthetisch"
        version = table.version()
        assert table.seed() == {}
    with IdentifierTable(store) as table:
        assert table.version() == version  # nothing changed: nothing written
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Quelle: synthetisch\nbegriff,klasse,kennung,grundform,nachname\n"
                "Bauer,Beruf,11104,Bauer,1\nMüllerin,Beruf,29232,Müller,\n")
    with IdentifierTable(store) as table:  # the file changed: the list is installed again
        assert [(e.term, e.source) for e in table.all_items()] == [
            ("Eigene Klinik", "Nutzer"), ("Müllerin", "Berufe")]  # "Bauer" stays deleted
        assert table.deleted() == 1 and table.restore() == 1
        assert [e.term for e in table.all_items("Beruf")] == ["Bauer", "Müllerin"]


def test_does_not_change_the_protection(tmp_path: Path, lexicon_on: None,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Professions, titles, hospitals and studies must never become protected terms."""
    from pseudonymed.detection import medical_terms

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    medical_terms.standard.cache_clear()
    with IdentifierTable() as table:
        assert dict(table.categories())["Beruf"] > 20000
        table.add(["Flantzbauer"], "Beruf")
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

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    empty = dict(statistics().lines())  # no store yet: nothing is created to count it
    assert "Identifikatoren (Einträge)" not in empty and not (tmp_path / "lexident.sqlite").exists()
    IdentifierTable().close()
    lines = dict(statistics().lines())
    assert int(lines["Identifikatoren (Einträge)"]) > 40000
    assert int(lines["    Klinik"]) > 4000 and "    Beruf" in lines


def test_gemtex_types() -> None:
    assert lexicon.gemtex_type("Klinik") == "LOCATION_HOSPITAL"
    assert lexicon.gemtex_type("Studie") == "ID"
    assert lexicon.gemtex_type("Beruf") == "PROFESSION"
    assert lexicon.gemtex_type("Erwerbsstatus") == ""
    assert lexicon.gemtex_type("Titel", '{"art":"akademisch"}') == "NAME_TITLE"
    assert lexicon.gemtex_type("Titel", '{"art":"adel"}') == ""
    assert lexicon.gemtex_type("Anatomie") == ""
