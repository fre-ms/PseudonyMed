"""The vocabulary leaves the pseudonym database: a database that still keeps it in the table
of before November 2026 hands it over to lexterm's store on first use, exactly, and only a
store that passed the check costs the database its table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path

import lexterm
import pytest
from lexcor import Concept, Cover, Package, StoreError, Term

from pseudonymed.core.text import error_text
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.pseudonyms.store import PseudonymStore, default_path

MEDICINE, BODY_PARTS = vocab_mod.CATEGORY_MEDICINE, vocab_mod.CATEGORY_BODY_PARTS
FINDINGS, EPONYMS = vocab_mod.CATEGORY_FINDINGS, vocab_mod.CATEGORY_EPONYMS
# the tables as PseudonyMed created them while the vocabulary lived in the pseudonym database
FORMER_TABLES = """
CREATE TABLE vokabular (
    begriff   TEXT PRIMARY KEY COLLATE NOCASE,
    kategorie TEXT NOT NULL DEFAULT '',
    angelegt  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX vokabular_kategorie ON vokabular(kategorie);
CREATE TABLE vokabular_meta (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT NOT NULL
);
"""


def former_table(database: Path, rows: Iterable[tuple[str, str]]) -> None:
    """Give a database the vocabulary table of before, filled with (term, category) rows."""
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(FORMER_TABLES)
        connection.executemany(
            "INSERT INTO vokabular (begriff, kategorie, angelegt)"
            " VALUES (?, ?, '2026-11-20T10:00:00')", rows)
        connection.execute("INSERT INTO vokabular_meta VALUES ('version', '7')")
        connection.commit()


def edited_rows() -> list[tuple[str, str]]:
    """The bundled vocabulary as a user left it in the table: two bundled terms deleted
    ("Wolf", "Nierenlager"), a third moved to a category of the user's ("Wade"), a term
    added to each of two bundled categories, and two categories of the user's own."""
    gone = {"Wolf", "Nierenlager", "Wade"}
    rows = [(term, category) for category, terms in vocab_mod.bundled().items()
            for term in terms if term not in gone]
    return rows + [("Flantzsyndrom", MEDICINE), ("Flantzknochen", BODY_PARTS),
                   ("Wade", "Eigene"), ("Flantzitis", "Eigene"), ("Marcuflantz", "Medikament")]


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The default pseudonym database of a data folder of its own, with the edited table."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    terms_module.standard.cache_clear()
    PseudonymStore().close()
    former_table(default_path(), edited_rows())
    yield default_path()
    terms_module.standard.cache_clear()


def _tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {name for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _plain_copies(store_file: Path, rows: list[tuple[str, str]]) -> None:
    """What ``datenbank auslagern`` left in the store before PseudonyMed read it: every
    category of the table as a plain list.  One deletion was made there since."""
    with lexterm.open_store(store_file) as store:
        for category in dict.fromkeys(category for _term, category in rows):
            store.add_package(Package(
                url="", name=category, title=category, language="de",
                concepts=[Concept(t, t, "", "", [Term(t, "de")]) for t, k in rows
                          if k == category]))
        store.hide(store.local_url(MEDICINE), "Herz")


@pytest.mark.parametrize("copies", [False, True], ids=["empty store", "store with copies"])
def test_the_table_becomes_the_store(database: Path, copies: bool) -> None:
    rows = edited_rows()
    if copies:
        _plain_copies(vocab_mod.default_path(), rows)
    with vocab_mod.Vocabulary() as v:
        local = v.store.local_url
        assert sorted((term, category) for term, category, _added in v.all_items()) == \
            sorted(rows)
        titles = sorted(info.title for info in v.store.packages())
        assert titles == sorted([*vocab_mod.BUNDLED, "Eigene", "Medikament"])  # none twice
        # what the user deleted from a bundled list is hidden in it, not missing from it
        medicine = {c.code for c in v.store.package(local(MEDICINE)).concepts}
        assert "Wolf" in medicine and not v.contains("Wolf") and v.contains("Herz")
        assert {(c.url, c.code) for c in v.store.overlay()} == {
            (local(MEDICINE), "Wolf"), (local(FINDINGS), "Nierenlager"),
            (local(BODY_PARTS), "Wade")}
        # what the user added to a bundled list is a concept of it, and the list is theirs
        assert "Flantzsyndrom" in medicine and len(medicine) == \
            len(vocab_mod.bundled()[MEDICINE]) + 1
        assert not v.store.info(local(MEDICINE)).redistributable
        assert v.store.info(local(EPONYMS)).concepts == len(vocab_mod.bundled()[EPONYMS])
        # a category of the user's own is a list of its terms
        assert {c.code for c in v.store.package(local("Eigene")).concepts} == {
            "Wade", "Flantzitis"}
        version = v.version()
    assert _tables(database) >= {"patienten", "zuordnungen"}
    assert not _tables(database) & {"vokabular", "vokabular_meta"}
    fb = terms_module.standard()
    expected = terms_module.MedicalTerms()
    expected.add(term for term, _category in rows)
    assert fb.cover.single == expected.cover.single
    assert fb.cover.phrases == expected.cover.phrases
    assert fb.is_medical_term("Flantzitis") and fb.is_medical_term("Wade")
    assert not fb.is_medical_term("Wolf") and not fb.is_medical_term("Nierenlager")
    with vocab_mod.Vocabulary() as v:  # the next start has nothing to move
        assert v.version() == version
        # and the deleted bundled terms can be had back, as before the move
        assert v.restore() == {MEDICINE: 1, BODY_PARTS: 0, FINDINGS: 1, EPONYMS: 0}


def test_a_failed_check_keeps_the_table(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = edited_rows()
    with monkeypatch.context() as patch:
        patch.setattr(lexterm, "cover",
                      lambda store: Cover(["Flantzirrtum"], lexterm.EPONYM_HEADS))
        with pytest.raises(vocab_mod.VocabularyError) as refused:
            vocab_mod.Vocabulary()
        assert "stimmt nicht" in str(refused.value)
        assert "does not agree" in error_text(refused.value, "en")
        held = vocab_mod.table_terms(database)
        assert held is not None and sum(len(terms) for terms in held.values()) == len(rows)
        assert held["Eigene"] == ["Wade", "Flantzitis"]
        assert {"vokabular", "vokabular_meta"} <= _tables(database)
        # the analysis does not go on with a vocabulary that is not the user's
        with pytest.raises(vocab_mod.VocabularyError):
            terms_module.standard()
    with vocab_mod.Vocabulary() as v:  # once the store agrees, the move goes through
        assert v.count() == len(rows)
    assert vocab_mod.table_terms(database) is None


def test_a_take_over_that_cannot_finish_stops_the_analysis(
        database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store took the terms but the table could not be dropped (a database that may
    only be read, say): that is an error of its own, not a reason to fall back on the
    bundled lists, and the next start tries again."""
    def refuse(database: Path) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    with monkeypatch.context() as patch:
        patch.setattr(vocab_mod, "drop_table", refuse)
        with pytest.raises(vocab_mod.VocabularyError, match="Tabelle bleibt erhalten"):
            terms_module.standard()
    assert vocab_mod.table_terms(database) is not None
    assert not terms_module.standard().is_medical_term("Wolf")  # the user's own vocabulary
    assert vocab_mod.table_terms(database) is None


def test_a_table_never_filled_is_only_dropped(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    former_table(default_path(), [])
    with vocab_mod.Vocabulary() as v:  # the bundled lists, as on any first use
        assert v.count() == sum(len(terms) for terms in vocab_mod.bundled().values())
        assert v.store.overlay() == []
    assert vocab_mod.table_terms(default_path()) is None


def test_only_the_default_store_takes_the_table_over(database: Path, tmp_path: Path) -> None:
    with vocab_mod.Vocabulary(tmp_path / "andere" / "lexterm.sqlite") as v:
        assert not v.contains("Flantzitis") and v.contains("Wolf")
    assert vocab_mod.table_terms(database) is not None
    with vocab_mod.Vocabulary(seed=False) as v:  # opened as it is: nothing moves, nothing comes
        assert v.count() == 0
    with pytest.raises(StoreError), vocab_mod.Vocabulary(
            tmp_path / "fehlt" / "lexterm.sqlite", readonly=True):
        pass
    assert vocab_mod.table_terms(database) is not None
    assert vocab_mod.table_terms(tmp_path / "fehlt.sqlite") is None  # no database, no table
    with vocab_mod.Vocabulary() as v:
        assert v.contains("Flantzitis") and not v.contains("Wolf")
    assert vocab_mod.table_terms(database) is None
