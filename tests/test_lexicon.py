"""The semantic lexicon: loading, reloading, user rows, statistics, and the protection it
leaves alone."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from pseudonymed.detection import lexicon
from pseudonymed.detection.vocabulary import Vocabulary


@pytest.fixture
def lexicon_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")


def test_loads_bundled_categories(tmp_path: Path, lexicon_on: None) -> None:
    with Vocabulary(tmp_path / "p.sqlite") as v:
        categories = {k: (r, n) for k, r, n in lexicon.per_class(v.connection)}
        for category in ("Anatomie", "Medikament", "Erkrankung", "Laborwert", "Befund", "Eponym"):
            role, count = categories[category]
            assert role == lexicon.MEDICAL and count > 0, category
        assert ("Laborwert", lexicon.MEDICAL) in lexicon.categories_of(v.connection, "NfL")
        assert ("Medikament", lexicon.MEDICAL) in lexicon.categories_of(v.connection,
                                                                        "Levetiracetam")
        meta = dict(v.connection.execute("SELECT quelle, anzahl FROM lexikon_meta"))
        assert meta["Wikidata"] > 30000 and meta["Laborwerte"] > 200
        for category in ("Beruf", "Erwerbsstatus", "Titel", "Klinik", "Studie"):
            role, count = categories[category]
            assert role == lexicon.FEATURE and count > 0, category
        assert ("Titel", lexicon.FEATURE) in lexicon.categories_of(v.connection, "Dr. med.")
        assert ("Erwerbsstatus", lexicon.FEATURE) in lexicon.categories_of(v.connection,
                                                                           "Rentnerin")
        extra = v.connection.execute("SELECT zusatz FROM lexikon WHERE begriff = 'Bauer'"
                                     " AND klasse = 'Beruf'").fetchone()[0]
        assert '"nachname":1' in extra


def test_reloaded_only_when_data_changed(tmp_path: Path, lexicon_on: None,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "berufe.csv.gz"
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Quelle: synthetisch\nbegriff,klasse,kennung,grundform,nachname\n"
                "Bäckerin,Beruf,29202,Bäcker,\nBauer,Beruf,11104,Bauer,1\n")
    sources = (lexicon.Source("Berufe", file, lexicon._professions),)
    monkeypatch.setattr(lexicon, "SOURCES", sources)
    with Vocabulary(tmp_path / "p.sqlite") as v:
        v.connection.execute("INSERT INTO lexikon VALUES ('Eigene Klinik', 'Klinik', 'merkmal',"
                             " 'Nutzer', '', '')")
        v.connection.commit()
        assert lexicon.ensure(v.connection) == {}
        assert ("Beruf", lexicon.FEATURE) in lexicon.categories_of(v.connection, "bäckerin")
        extra = v.connection.execute("SELECT zusatz FROM lexikon WHERE begriff = 'Bauer'"
                                     ).fetchone()[0]
        assert '"nachname":1' in extra
    with gzip.open(file, "wt", encoding="utf-8") as f:
        f.write("# Quelle: synthetisch\nbegriff,klasse,kennung,grundform,nachname\n"
                "Rentnerin,Erwerbsstatus,,Rentner,\n")
    with Vocabulary(tmp_path / "p.sqlite") as v:
        assert lexicon.categories_of(v.connection, "Bäckerin") == []
        assert lexicon.categories_of(v.connection, "Rentnerin") == [("Erwerbsstatus", "merkmal")]
        assert lexicon.categories_of(v.connection, "Eigene Klinik") == [("Klinik", "merkmal")]


def test_disabled_without_table(tmp_path: Path) -> None:
    with Vocabulary(tmp_path / "p.sqlite") as v:
        assert lexicon.per_class(v.connection) == []


def test_does_not_change_the_protection(tmp_path: Path, lexicon_on: None,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Professions, titles, hospitals and studies must never become protected terms."""
    from pseudonymed.detection import medical_terms

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    medical_terms.standard.cache_clear()
    with Vocabulary() as v:
        v.connection.execute("INSERT INTO lexikon VALUES ('Bauer', 'Beruf', 'merkmal', 'Nutzer',"
                             " '', '')")
        v.connection.commit()
    try:
        matcher = medical_terms.standard()
        text = "Herr Bauer berichtet über Kopfschmerz."
        is_protected = {text[a:b] for a, b in matcher.spans(text)}
        assert "Bauer" not in is_protected and "Kopfschmerz" in is_protected
    finally:
        medical_terms.standard.cache_clear()


def test_statistics_show_lexicon(tmp_path: Path, lexicon_on: None) -> None:
    from pseudonymed.pseudonyms.backup import statistics

    path = tmp_path / "p.sqlite"
    Vocabulary(path).close()
    lines = dict(statistics(path).lines())
    assert int(lines["Lexikon (Einträge)"]) > 30000
    assert "    Anatomie (Fachklasse)" in lines and "    Klinik (Identifikator)" in lines


def test_gemtex_types() -> None:
    assert lexicon.gemtex_type("Klinik") == "LOCATION_HOSPITAL"
    assert lexicon.gemtex_type("Studie") == "ID"
    assert lexicon.gemtex_type("Beruf") == "PROFESSION"
    assert lexicon.gemtex_type("Erwerbsstatus") == ""
    assert lexicon.gemtex_type("Titel", '{"art":"akademisch"}') == "NAME_TITLE"
    assert lexicon.gemtex_type("Titel", '{"art":"adel"}') == ""
    assert lexicon.gemtex_type("Anatomie") == ""
