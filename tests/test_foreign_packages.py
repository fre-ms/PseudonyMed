"""The stores of the lex libraries are shared: another program may keep a whole terminology
in them.  PseudonyMed's are only the lists a store gave a URL itself; a package that brought
a URL of its own is no category and no list of identifiers, and is neither hidden, rewritten
nor removed, whatever PseudonyMed does.  It protects, or is looked for, while the store has
it ticked, as every list does."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import lexident
import lexterm
import pytest
from lexcor import Change, Concept, Package, Store, Term

from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import lexicon
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.detection.store_table import own_lists
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import PseudonymStore

TERMINOLOGY = "http://example.org/fhir/CodeSystem/flantz-10"
DIRECTORY = "http://example.org/fhir/CodeSystem/flantz-hospitals"
What = tuple[Package, list[Change]]


def terminology() -> Package:
    """A terminology as another program installs it: a URL of its own, codes, several names
    of a concept, and a title that is also the name of a bundled category."""
    return Package(
        url=TERMINOLOGY, version="2026", name="Flantz10", title=vocab_mod.CATEGORY_BODY_PARTS,
        publisher="Flantz-Institut", language="de",
        concepts=[Concept("F00", "Flantzfieber", "", "", [
            Term("Flantzfieber", "de"), Term("Morbus Flantz", "de"), Term("Flantz fever", "en")]),
            Concept("F01", "Flantzgrippe", "", "", [Term("Flantzgrippe", "de")]),
            Concept("F02", "Mustermann", "", "", [Term("Mustermann", "de")])])


def directory() -> Package:
    """A directory of hospitals as another program installs it in lexident's store, with a
    town that has a hospital and the title of the bundled list of hospitals."""
    return Package(
        url=DIRECTORY, version="7", name="FlantzHospitals", title=lexicon.HOSPITALS,
        language="de",
        concepts=[Concept("H1", "Flantzklinikum Fremdstadt", "LOCATION_HOSPITAL", "", [
            Term("Flantzklinikum Fremdstadt", "de"), Term("Fremdklinik Flantz", "de")]),
            Concept("Fremdstadt", "Fremdstadt", "hospital-town", "", [Term("Fremdstadt", "de")]),
            Concept("V1", "Fremdverein Flantzdorf", "OTHER", "", [
                Term("Fremdverein Flantzdorf", "de")])])


def _install(store: Store, package: Package, code: str, term: str) -> What:
    """Put a package of another program into a store, with a line that program's user wrote
    into the overlay; what the store then holds of it."""
    url = store.add_package(package).url
    store.hide(url, code, term, "de")
    return _held(store, url)


def _held(store: Store, url: str) -> What:
    """What a store holds of one package: the package as it came, and its overlay."""
    return store.package(url), store.overlay(url)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A data folder of its own with a pseudonym database."""
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path / "daten"))
    terms_module.standard.cache_clear()
    PseudonymStore().close()
    yield tmp_path / "daten"
    terms_module.standard.cache_clear()


def _vocabulary() -> tuple[int, list[tuple[str, int]], list[tuple[str, str, str]]]:
    with vocab_mod.Vocabulary() as v:
        return v.count(), v.categories(), v.all_items()


def _cover() -> tuple[frozenset[str], frozenset[tuple[str, ...]]]:
    found = terms_module.standard().cover
    return frozenset(found.single), frozenset(found.phrases)


# -- which lists are the store's own ---------------------------------------------------------------
def test_the_lists_of_a_store_are_told_by_their_url(tmp_path: Path) -> None:
    with lexterm.open_store(tmp_path / "s.sqlite") as store, \
            lexterm.open_store(tmp_path / "anderswo.sqlite") as elsewhere:
        plain = [Concept("Flantzwort", "", "", "", [Term("Flantzwort", "de")])]
        own = store.add_package(Package(url="", name="Eigene", title="Eigene", concepts=plain))
        renamed = store.add_package(Package(url="", name="liste-7", title="Siebte Liste",
                                            concepts=plain))
        # a list another store gave its URL is not this store's, although it looks the same
        moved = Package(url=elsewhere.local_url("Eigene"), name="Eigene", title="Eigene",
                        concepts=plain)
        store.add_package(moved)
        store.add_package(terminology())
        assert own.url == store.local_url("Eigene") and moved.url != own.url
        assert [info.url for info in own_lists(store)] == [own.url, renamed.url]
        assert len(store.packages()) == 4


# -- the vocabulary --------------------------------------------------------------------------------
def test_a_terminology_is_no_vocabulary(folder: Path) -> None:
    before = _vocabulary()
    cover = _cover()
    with vocab_mod.Vocabulary() as v:
        held = _install(v.store, terminology(), "F00", "Morbus Flantz")
    assert _vocabulary() == before
    # ticked as it came into the store, it protects; unticked, the vocabulary is as before
    assert terms_module.standard().is_medical_term("Flantzfieber")
    with vocab_mod.Vocabulary() as v:
        v.store.switch(TERMINOLOGY, False)
    assert _cover() == cover
    fb = terms_module.standard()
    assert not fb.is_medical_term("Flantzfieber") and not fb.is_medical_term("Mustermann")
    assert dict(stats_mod.statistics().vocabulary_categories) == dict(before[1])
    with vocab_mod.Vocabulary() as v:
        assert not v.contains("Flantzfieber") and v.all_items(search="Flantz") == []
        # what PseudonyMed does with a term of that name goes into the store's own lists
        assert v.remove(["Flantzgrippe", "Flantzfieber"]) == 0
        assert not v.rename("Flantzgrippe", "Flantzschnupfen")
        assert v.set_category(["Flantzgrippe"], "Eigene") == 0
        assert v.add(["Flantzgrippe"], vocab_mod.CATEGORY_BODY_PARTS) == 1
        assert v.remove(["Flantzgrippe"]) == 1
        assert v.restore() == {category: 0 for category in vocab_mod.BUNDLED}
        assert _held(v.store, TERMINOLOGY) == held
        own = v.store.local_url(vocab_mod.CATEGORY_BODY_PARTS)
        assert v.store.overlay(own) == [Change(own, "Flantzgrippe", "hide")]


def test_a_terminology_does_not_stand_for_a_bundled_list(folder: Path) -> None:
    """A package of another program that is called like a bundled category is not that
    category: the bundled list is installed beside it."""
    with lexterm.open_store(vocab_mod.default_path()) as store:
        held = _install(store, terminology(), "F00", "Morbus Flantz")
    with vocab_mod.Vocabulary() as v:
        assert {info.title for info in own_lists(v.store)} == set(vocab_mod.BUNDLED)
        assert dict(v.categories())[vocab_mod.CATEGORY_BODY_PARTS] == len(
            vocab_mod.bundled()[vocab_mod.CATEGORY_BODY_PARTS])
        assert _held(v.store, TERMINOLOGY) == held


def test_replacing_the_vocabulary_leaves_a_terminology_alone(folder: Path, tmp_path: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        held = _install(v.store, terminology(), "F00", "Morbus Flantz")
        v.add(["Sicherungsbegriff"], "Test")
        saved = v.all_items()
    archive = backup_mod.export(tmp_path / "s.zip")
    with zipfile.ZipFile(archive) as z:
        words = z.read("vocabulary.csv").decode("utf-8")
    assert "Flantzfieber" not in words and len(words.splitlines()) == len(saved) + 1
    with vocab_mod.Vocabulary() as v:
        v.remove(["Sicherungsbegriff", "Kopf"])
        v.add(["Flantzfieber"], "Danach")
    for mode in reversed(backup_mod.MODES):
        backup_mod.import_file(archive, mode=mode)
        with vocab_mod.Vocabulary() as v:
            assert _held(v.store, TERMINOLOGY) == held
    with vocab_mod.Vocabulary() as v:
        assert v.all_items() == saved
        v.replace({"Nur": ["Einzigbegriff"]})
        assert [term for term, _category, _added in v.all_items()] == ["Einzigbegriff"]
        assert _held(v.store, TERMINOLOGY) == held


# -- the identifiers -------------------------------------------------------------------------------
def _identifiers() -> tuple[list[ident_module.Entry], int, list[tuple[str, int, str]]]:
    with ident_module.IdentifierTable() as table:
        return table.all_items(), table.deleted(), table.loaded()


def _found(text: str) -> list[str]:
    return [text[k.start:k.end] for k in ident_module.standard().candidates(text)]


TEXT = ("War im Flantzklinikum Fremdstadt, dann im Klinikum Fremdstadt, Mitglied im Fremdverein"
        " Flantzdorf und bei der Flantzwerke GmbH.")


def test_a_directory_of_another_program_holds_no_identifiers(folder: Path) -> None:
    with ident_module.IdentifierTable() as table:
        table.add(["Flantzwerke GmbH"])
        table.add(["Eigenklinik Flantz"], "hospital")
    before = _identifiers()
    assert _found(TEXT) == ["Flantzwerke GmbH"]
    with ident_module.IdentifierTable() as table:
        held = _install(table.store, directory(), "H1", "Fremdklinik Flantz")
    assert _identifiers() == before and "Flantzklinikum Fremdstadt" in _found(TEXT)
    with ident_module.IdentifierTable() as table:
        table.store.switch(DIRECTORY, False)  # unticked, it is no longer looked for
    assert _found(TEXT) == ["Flantzwerke GmbH"]
    assert stats_mod.statistics().identifier_lists == []
    with ident_module.IdentifierTable() as table:
        assert table.keys_for("Flantzklinikum Fremdstadt") == []
        assert table.remove([("Flantzklinikum Fremdstadt", "hospital", "H1")]) == 0
        assert table.change(("Flantzklinikum Fremdstadt", "hospital", "H1"), "Anders") is None
        assert table.remove(table.keys_for("Eigenklinik Flantz")) == 1
        # the towns that form names of hospitals are those of PseudonyMed's own hospitals
        assert [info.title for info in own_lists(table.store)] == ["user: other"]
        assert table.restore() == 0 and table.deleted() == 0
        assert _held(table.store, DIRECTORY) == held


def test_a_backup_of_the_identifiers_leaves_a_directory_alone(folder: Path,
                                                              tmp_path: Path) -> None:
    with ident_module.IdentifierTable() as table:
        table.add(["Flantzwerke GmbH", "Sicherungseintrag"])
        held = _install(table.store, directory(), "H1", "Fremdklinik Flantz")
    saved = _identifiers()
    archive = backup_mod.export(tmp_path / "s.zip")
    with zipfile.ZipFile(archive) as z:
        (tmp_path / "archiv.sqlite").write_bytes(z.read("lexident.sqlite"))
    with lexident.open_store(tmp_path / "archiv.sqlite", readonly=True) as archived:
        assert [info.title for info in archived.packages()] == ["user: other"]
    with ident_module.IdentifierTable() as table:
        table.remove(table.keys_for("Sicherungseintrag"))
        table.add(["Danacheintrag"])
        table.store.hide(DIRECTORY, "V1", "Fremdverein Flantzdorf", "de")  # the other program
        held = _held(table.store, DIRECTORY)
    merged = backup_mod.import_file(archive, mode="merge")
    assert merged.new_identifiers == 1
    with ident_module.IdentifierTable() as table:
        assert _held(table.store, DIRECTORY) == held
    restored = backup_mod.import_file(archive)
    assert restored.identifiers == 2 and _identifiers() == saved
    with lexident.open_store(restored.prior_backups[-1], readonly=True) as aside:
        assert [info.title for info in aside.packages()] == ["user: other"]
    with ident_module.IdentifierTable() as table:
        assert _held(table.store, DIRECTORY) == held
