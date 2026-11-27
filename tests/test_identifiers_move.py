"""The identifiers leave the pseudonym database: a database that still keeps them in the
tables of before November 2026 hands them over to lexident's store on first use, exactly,
and only a store that passed the check costs the database its tables."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path

import lexident
import pytest
from lexcor import Concept, Package, Term

from pseudonymed.core.text import error_text
from pseudonymed.detection import extra_data, lexicon
from pseudonymed.detection import identifier_lists as lists
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.pseudonyms.store import PseudonymStore, default_path

# the tables as PseudonyMed created them while the lexicon lived in the pseudonym database
FORMER_TABLES = """
CREATE TABLE lexikon (
    begriff TEXT NOT NULL COLLATE NOCASE,
    klasse  TEXT NOT NULL,
    rolle   TEXT NOT NULL,
    quelle  TEXT NOT NULL,
    kennung TEXT NOT NULL DEFAULT '',
    zusatz  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (begriff, klasse, kennung)
) WITHOUT ROWID;
CREATE TABLE lexikon_meta (
    quelle  TEXT PRIMARY KEY,
    stand   TEXT NOT NULL,
    anzahl  INTEGER NOT NULL,
    geladen TEXT NOT NULL
);
CREATE TABLE lexikon_geloescht (
    begriff TEXT NOT NULL COLLATE NOCASE,
    klasse  TEXT NOT NULL,
    kennung TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (begriff, klasse, kennung)
) WITHOUT ROWID;
CREATE TABLE lexikon_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
"""
# what the identifiers step showed and the analysis looked for, as the program of then said it
VISIBLE = ("rolle = 'merkmal' AND klasse != 'Erwerbsstatus'"
           " AND NOT (klasse = 'Studie' AND quelle = 'Studien' AND zusatz != '')")
AT = extra_data.SOURCE_HOSPITALS_AT
Line = tuple[str, str, str, str, str, str]  # begriff, klasse, rolle, quelle, kennung, zusatz


def former_lexicon(database: Path, rows: Iterable[Line], states: dict[str, str] | None = None,
                   deleted: Iterable[tuple[str, str, str]] = ()) -> None:
    """Give a database the lexicon tables of before: rows, the state of each source and
    the bundled rows the user deleted."""
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(FORMER_TABLES)
        connection.executemany("INSERT OR IGNORE INTO lexikon VALUES (?, ?, ?, ?, ?, ?)", rows)
        connection.executemany(
            "INSERT INTO lexikon_meta VALUES (?, ?, 0, '2026-11-20T10:00:00')",
            (states or {}).items())
        connection.executemany("INSERT INTO lexikon_geloescht VALUES (?, ?, ?)", deleted)
        connection.execute("DELETE FROM lexikon WHERE quelle != 'Nutzer' AND EXISTS (SELECT 1"
                           " FROM lexikon_geloescht g WHERE g.begriff = lexikon.begriff"
                           " AND g.klasse = lexikon.klasse AND g.kennung = lexikon.kennung)")
        connection.execute("INSERT INTO lexikon_version VALUES (1, 5)")
        connection.commit()


def visible_rows(database: Path) -> list[tuple[str, str, str, str, str]]:
    """The rows of the former tables that were identifiers to show and to look for."""
    with closing(sqlite3.connect(database)) as connection:
        return list(connection.execute(
            f"SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon WHERE {VISIBLE}"))


def tables_of(database: Path) -> set[str]:
    """The names of the tables of a database."""
    with closing(sqlite3.connect(database)) as connection:
        return {name for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


def small_rows() -> list[Line]:
    """A lexicon of invented rows: two bundled lists, a list fetched on request and entries
    of the user's own, one of them a bundled hospital the user renamed."""
    town = json.dumps({"ort": "Flantzstadt", "land": "DE"})
    return [
        ("Flantzklinik Nord", "Klinik", "merkmal", "Kliniken", "DE:1", town),
        ("Klinikum Tannengrund", "Klinik", "merkmal", "Kliniken", "DE:2",
         json.dumps({"ort": "Tannengrund", "land": "DE", "betten": 120})),
        ("Flantzbäckerin", "Beruf", "merkmal", "Berufe", "29202", '{"grundform":"Flantzbäcker"}'),
        ("Flantzbäcker", "Beruf", "merkmal", "Berufe", "29202",
         '{"grundform":"Flantzbäcker","nachname":1}'),
        ("Flantzrentnerin", "Erwerbsstatus", "merkmal", "Berufe", "", "{}"),
        ("NCT01234567", "Studie", "merkmal", "Studien", "ClinicalTrials.gov:NCT01234567",
         '{"teilnehmer":120,"laender":"DE"}'),
        ("FLANTZ-1", "Studie", "merkmal", "Studien", "ClinicalTrials.gov:NCT01234567", ""),
        ("Flantzspital Bergdorf", "Klinik", "merkmal", AT, "AT:K1",
         json.dumps({"ort": "Bergdorf", "land": "AT"})),
        ("Flantzwerke GmbH", "Sonstiges", "merkmal", "Nutzer", "", ""),
        ("Kunstschmiedin", "Handwerk", "merkmal", "Nutzer", "", ""),
        ("Flantzklinik Süd", "Klinik", "merkmal", "Nutzer", "DE:3",
         json.dumps({"ort": "Südstadt", "land": "DE"})),
        ("Flantzitis", "Erkrankung", "fach", "Wikidata", "Q1", ""),
    ]


SMALL_DELETED = [("Flantzklinik Süd alt", "Klinik", "DE:3"), ("Flantzmüller", "Beruf", "29203")]
SMALL_STATES = {"Kliniken": "k-stand", "Berufe": "b-stand", "Studien": "s-stand",
                AT: "2026-09-18 0123abcd"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The default pseudonym database of a data folder of its own, with the small lexicon;
    the bundled lists stay switched off, so that the store holds what the tables held."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    former_lexicon(default_path(), small_rows(), SMALL_STATES, SMALL_DELETED)
    yield default_path()


def _copies(store_file: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    """What ``datenbank auslagern`` left in the store before PseudonyMed read it: the rows of
    every source as a package without properties, the towns of the hospitals, and under the
    name it gave a single class of the user's own entries a list that has no place now."""
    by_source: dict[str, dict[str, Concept]] = {}
    for term, category, source, code, _extra in rows:
        concept = by_source.setdefault(source, {}).setdefault(
            code or term, Concept(code or term, "", lists.cls_of(category)))
        concept.terms.append(Term(term, "de"))
    with lexident.open_store(store_file) as store:
        for source, concepts in by_source.items():
            store.add_package(Package(url="", name=source, title=source, language="de",
                                      concepts=list(concepts.values())))
        store.add_package(Package(url="", name=lists.TOWNS, title=lists.TOWNS, language="de",
                                  concepts=[Concept("Altstadt", "Altstadt", "hospital-town", "",
                                                    [Term("Altstadt", "de")])]))
        store.hide(store.local_url("Kliniken"), "DE:1", "Flantzklinik Nord", "de")


@pytest.mark.parametrize("copies", [False, True], ids=["empty store", "store with copies"])
def test_the_tables_become_the_store(database: Path, copies: bool) -> None:
    rows = visible_rows(database)
    if copies:
        _copies(ident_module.default_path(), rows)
    with ident_module.IdentifierTable() as table:
        found = {(e.term, e.category, e.source, e.code) for e in table.all_items()}
        assert found == {(term, category, source, code)
                         for term, category, source, code, _extra in rows}
        assert table.count() == len(rows) == 9  # no employment status, no registry number
        # the lists the hand-over left are replaced, its list of the user's entries is gone
        assert {info.title for info in table.store.packages()} == {
            "Kliniken", "Berufe", "Studien", AT, "Nutzer: Sonstiges", "Nutzer: Handwerk",
            "Nutzer: Klinik", lists.TOWNS}
        local = table.store.local_url
        # the class is the GeMTeX type, a class of the user's own stays; the state came along
        assert {e.cls for e in table.store.entries()} == {
            "LOCATION_HOSPITAL", "PROFESSION", "ID", "OTHER", "Handwerk", "hospital-town"}
        assert table.store.info(local("Kliniken")).version == "k-stand"
        assert table.loaded() == [(AT, 1, "2026-09-18 0123abcd")]
        assert table.store.info(local(AT)).copyright == extra_data.CLINICS_AT_NOTE
        # what a row said is a property of its concept; where the terms of a concept differ,
        # each term has its own; a study has what the row of its number said
        assert table.store.properties(local("Kliniken"), "DE:2") == {
            "betten": "120", "land": "DE", "ort": "Tannengrund"}
        bakers = table.store.properties(local("Berufe"), "29202")
        assert lists.unfold(bakers, "Flantzbäcker") == {"grundform": "Flantzbäcker",
                                                         "nachname": "1"}
        assert lists.unfold(bakers, "Flantzbäckerin") == {"grundform": "Flantzbäcker"}
        assert table.store.properties(local("Studien"), "ClinicalTrials.gov:NCT01234567") == {
            "laender": "DE", "teilnehmer": "120"}
        # the user's own entries, the renamed hospital with its number and its town
        own = {e.term: e for e in table.all_items() if e.own}
        assert set(own) == {"Flantzwerke GmbH", "Kunstschmiedin", "Flantzklinik Süd"}
        assert own["Flantzklinik Süd"].code == "DE:3"
        assert own["Flantzklinik Süd"].details == "Südstadt (DE)"
        # the towns of the hospitals in force, the user's and the fetched list's among them
        assert sorted(e.term for e in table.store.entries() if e.cls == "hospital-town") == [
            "Bergdorf", "Flantzstadt", "Südstadt", "Tannengrund"]
        # the deletions are hidden terms: they stay away when a list comes again
        hidden = {(line.url, line.code, line.value) for line in table.store.overlay()
                  if line.url in (local("Kliniken"), local(AT), local("Berufe"))}
        assert hidden == {(local("Kliniken"), "DE:3", "Flantzklinik Süd alt"),
                          (local(AT), "DE:3", "Flantzklinik Süd alt"),
                          (local("Berufe"), "29203", "Flantzmüller")}
        assert table.deleted() == 2
        version = table.version()
    assert tables_of(database) >= {"patienten", "zuordnungen"}
    assert not {t for t in tables_of(database) if t.startswith("lexikon")}
    e = ident_module.standard()
    text = ("Arbeitet als Flantzbäckerin bei der Flantzwerke GmbH, war im Klinikum Bergdorf und"
            " in der Flantzklinik Süd; Teilnahme an der Studie FLANTZ-1. Krankenhaus Altstadt.")
    assert [(text[k.start:k.end], ident_module.category_of(k.classes))
            for k in e.candidates(text)] == [
        ("Flantzbäckerin", "Beruf"), ("Flantzwerke GmbH", "Sonstiges"),
        ("Klinikum Bergdorf", "Klinik"), ("Flantzklinik Süd", "Klinik"), ("FLANTZ-1", "Studie")]
    with ident_module.IdentifierTable() as table:  # the next start has nothing to move
        assert table.version() == version


def test_a_failed_check_keeps_the_tables(database: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    whole = lists.towns_package
    with monkeypatch.context() as patch:
        patch.setattr(lists, "towns_package", lambda towns: whole(list(towns)[1:]))
        with pytest.raises(ident_module.IdentifierError) as refused:
            ident_module.IdentifierTable()
        assert "stimmen nicht" in str(refused.value)
        assert "do not agree" in error_text(refused.value, "en")
        assert len(visible_rows(database)) == 9
        assert {"lexikon", "lexikon_meta", "lexikon_geloescht", "lexikon_version"} <= \
            tables_of(database)
        # the analysis does not go on with identifiers that are not the user's
        with pytest.raises(ident_module.IdentifierError):
            ident_module.standard()
    with ident_module.IdentifierTable() as table:  # once the store agrees, the move goes through
        assert table.count() == 9
    assert "lexikon" not in tables_of(database)


def test_a_take_over_that_cannot_finish_stops_the_analysis(
        database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(database: Path) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    with monkeypatch.context() as patch:
        patch.setattr(lists, "drop_tables", refuse)
        with pytest.raises(ident_module.IdentifierError, match="Tabellen bleiben erhalten"):
            ident_module.standard()
    assert "lexikon" in tables_of(database)
    assert ident_module.standard().per_class["OTHER"] == 1  # the next start tries again
    assert "lexikon" not in tables_of(database)


def test_tables_without_identifiers_are_only_dropped(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    former_lexicon(default_path(), [row for row in small_rows() if row[2] == "fach"])
    with ident_module.IdentifierTable() as table:
        assert table.count() == 0 and table.store.packages() == []
    assert "lexikon" not in tables_of(default_path())


def test_only_the_default_store_takes_the_tables_over(database: Path, tmp_path: Path) -> None:
    with ident_module.IdentifierTable(tmp_path / "andere" / "lexident.sqlite") as table:
        assert table.count() == 0
    with ident_module.IdentifierTable(seed=False) as table:  # as it is: nothing moves
        assert table.count() == 0
    assert "lexikon" in tables_of(database)
    assert lists.former_tables(tmp_path / "fehlt.sqlite") is None  # no database, no tables
    with ident_module.IdentifierTable() as table:
        assert table.count() == 9
    assert lists.former_tables(database) is None


def test_bundled_lists_of_another_day_are_installed_anew(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The tables hold a bundled list as an earlier version of the program brought it.  They
    are taken over as they are, with their state; the list of today then takes their place,
    as it did whenever the data changed, and what the user deleted stays away."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")
    file = tmp_path / "berufe.csv"
    file.write_text("# Quelle: synthetisch\nbegriff,klasse,kennung,grundform,nachname\n"
                    "Flantzbäcker,Beruf,29202,Flantzbäcker,\nFlantzmüller,Beruf,29203,,\n"
                    "Flantzweberin,Beruf,28102,,\n", encoding="utf-8")
    monkeypatch.setattr(lexicon, "SOURCES", (lexicon.Source("Berufe", file,
                                                            lexicon._professions),))
    PseudonymStore().close()
    former_lexicon(default_path(), small_rows(), SMALL_STATES, SMALL_DELETED)
    with ident_module.IdentifierTable() as table:
        professions = [e.term for e in table.all_items("Beruf")]
        assert professions == ["Flantzbäcker", "Flantzweberin"]  # "Flantzmüller" was deleted
        assert table.store.info(table.store.local_url("Berufe")).version == lexicon.state(
            lexicon.SOURCES[0])
        assert table.count() == 9  # the other lists are as the tables held them
        assert table.restore() == 2 and "Flantzmüller" in [
            e.term for e in table.all_items("Beruf")]


def test_the_bundled_lists_move_as_they_are(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """The lexicon as the program of before loaded it from the bundled files, with two
    deletions and an entry of the user's own, as on a machine that used it."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")
    PseudonymStore().close()
    bundled = [(b, k, r, q.name, code, extra) for q in lexicon.SOURCES
               for b, k, r, code, extra in lexicon.rows_of(q)]
    hospital = next(row for row in bundled if row[3] == "Kliniken")
    profession = next(row for row in bundled if row[1] == "Beruf")
    former_lexicon(default_path(), [*bundled, ("Flantzwerke GmbH", "Sonstiges", "merkmal",
                                               "Nutzer", "", "")],
                   {q.name: lexicon.state(q) for q in lexicon.SOURCES},
                   [(hospital[0], "Klinik", hospital[4]), (profession[0], "Beruf", profession[4])])
    rows = visible_rows(default_path())
    assert len(rows) > 49000
    with ident_module.IdentifierTable() as table:
        found = {(e.term, e.category, e.source, e.code) for e in table.all_items()}
        assert found == {(term, category, source, code)
                         for term, category, source, code, _extra in rows}
        assert table.deleted() == 2
        titles = sorted(info.title for info in table.store.packages())
        assert titles == ["Berufe", "Kliniken", "Nutzer: Sonstiges", "Studien", "Titel",
                          lists.TOWNS]
        version = table.version()
        assert table.seed() == {}  # the state is today's: nothing is installed again
        assert table.version() == version
    assert "lexikon" not in tables_of(default_path())
