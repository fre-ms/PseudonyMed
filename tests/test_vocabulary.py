"""The vocabulary in lexterm's store: seeding, editing, the overlay, CSV, the matcher's cache,
the step."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from lexcor import Change, Concept, Package, Term
from PySide6.QtWidgets import QApplication

from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import overlay
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.test_gui import qapp  # noqa: F401

MEDICINE, BODY_PARTS = vocab_mod.CATEGORY_MEDICINE, vocab_mod.CATEGORY_BODY_PARTS
FINDINGS, EPONYMS = vocab_mod.CATEGORY_FINDINGS, vocab_mod.CATEGORY_EPONYMS


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path / "daten"))
    terms_module.standard.cache_clear()
    yield tmp_path / "daten"
    terms_module.standard.cache_clear()


def _concepts(v: vocab_mod.Vocabulary, category: str) -> set[str]:
    """The codes of the store's own list of a category, hidden ones included."""
    return {c.code for c in v.store.package(v.store.local_url(category)).concepts}


def _terms(v: vocab_mod.Vocabulary, category: str) -> list[str]:
    return [term for term, _category, _added in v.all_items(category=category)]


def test_base_stock(fresh: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        assert v.path == fresh / "lexterm.sqlite" == vocab_mod.default_path()
        assert v.count() > 39000 and v.version() >= 1
        categories = dict(v.categories())
        assert categories[MEDICINE] > 39000
        # body parts already in the Wikidata list keep that category: only the rest counts here
        assert categories[BODY_PARTS] > 200 and categories[FINDINGS] > 200 and categories[EPONYMS]
        assert v.contains("Nierenlager") and v.contains("nierenlager")
        assert not v.contains("Flantzitis")
        infos = {info.title: info for info in v.store.packages()}
        assert set(infos) == set(vocab_mod.BUNDLED)
        assert all(info.url == v.store.local_url(title) for title, info in infos.items())
        assert infos[MEDICINE].redistributable and infos[MEDICINE].publisher == "Wikidata"
        assert infos[MEDICINE].copyright == "CC0 1.0"
        assert not any(infos[k].redistributable for k in (BODY_PARTS, FINDINGS, EPONYMS))
        assert infos[BODY_PARTS].copyright == "AGPL-3.0-or-later"
        entry = next(e for e in v.store.entries() if e.term == "Nierenlager")
        assert (entry.code, entry.language, entry.system) == (
            "Nierenlager", "de", v.store.local_url(FINDINGS))
        assert v.seed() == {} and v.restore() == dict.fromkeys(vocab_mod.BUNDLED, 0)
        assert v.all_items(search="nierenlager", category=FINDINGS)[0] == (
            "Nierenlager", FINDINGS, "")
        assert v.all_items(search="Nierenlager", category=MEDICINE) == []
    fb = terms_module.standard()
    assert fb.is_medical_term("Nierenlager")
    assert [(q, n) for q, n in fb.sources] == [(k, categories[k]) for k in vocab_mod.BUNDLED]
    # the pseudonym database keeps no table of the vocabulary
    with PseudonymStore() as sp:
        tables = {r[0] for r in sp.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"patients", "aliases", "mappings", "discarded", "residual_risk",
                      "residual_risk_documents"}


def test_edit_and_cache(fresh: Path) -> None:
    fb = terms_module.standard()
    assert fb.is_medical_term("Wolf") and not fb.is_medical_term("Flantzose")
    with vocab_mod.Vocabulary() as v:
        v1 = v.version()
        assert v.add(["Flantzose", "  flantzose ", "ab"], "Test") == 1  # case, length
        assert v.remove(["Wolf", "gibtesnicht"]) == 1
        assert v.rename("Flantzose", "Flantzosis") and not v.rename("Flantzosis", "Wade")
        assert not v.rename("gibtesnicht", "Flantzneu") and not v.rename("Flantzosis", "ab")
        assert v.set_category(["Flantzosis"], "Neu") == 1
        assert v.version() > v1
        assert [z[1] for z in v.all_items(search="flantzos")] == ["Neu"]
        assert v.all_items(search="100%") == []
        assert v.all_items(category="Neu") == [("Flantzosis", "Neu", "")]
        assert "Test" not in dict(v.categories())  # its one term went to another category
    terms2 = terms_module.standard()  # rebuilt because the version changed
    assert terms2 is not fb and terms2.is_medical_term("Flantzosis")
    assert not terms2.is_medical_term("Wolf")
    assert terms_module.standard() is terms2  # unchanged store: cached


def test_a_deleted_term_is_hidden_and_stays_away(fresh: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        url = v.store.local_url(BODY_PARTS)
        whole = len(vocab_mod.bundled()[BODY_PARTS])
        assert v.remove(["Wade"]) == 1 and not v.contains("Wade")
        assert v.store.overlay(url) == [Change(url, "Wade", "hide")]
        assert "Wade" in _concepts(v, BODY_PARTS)  # hidden, not taken out of the list
        assert dict(v.categories())[BODY_PARTS] == whole - 1
        # the list installed again (a new version of it, say) leaves the term away
        assert v.store.remove_package(url) and BODY_PARTS not in dict(v.categories())
        assert v.seed() == {BODY_PARTS: whole}
        assert not v.contains("Wade") and v.contains("Kopf")
        assert dict(v.categories())[BODY_PARTS] == whole - 1
    with vocab_mod.Vocabulary() as v:  # and so does the next start
        assert not v.contains("Wade")
        # added to its category again, it is shown again instead of being written twice
        assert v.add(["Wade"], BODY_PARTS) == 1 and v.contains("Wade")
        assert v.store.overlay(url) == [] and len(_concepts(v, BODY_PARTS)) == whole
        assert v.store.info(url).redistributable is False


def test_many_terms_are_hidden_in_one_step(fresh: Path) -> None:
    """A whole category deleted at once gets the same lines in the overlay as its terms
    deleted one by one would, written together."""
    with vocab_mod.Vocabulary() as v:
        findings, url = vocab_mod.bundled()[FINDINGS], v.store.local_url(FINDINGS)
        assert len(findings) >= overlay.MANY
        version = v.version()
        assert v.remove(findings) == len(findings) and v.version() == version + 1
        assert v.store.overlay(url) == [Change(url, e, "hide") for e in sorted(findings)]
        assert FINDINGS not in dict(v.categories())
        assert v.remove(["Wade"]) == 1 and v.version() == version + 2  # a single one as ever
        assert v.restore()[FINDINGS] == len(findings) and v.store.overlay(url) == []


def test_the_bundled_lists_come_back_on_request(fresh: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        assert v.remove(["Wade", "Kopf", "Wolf"]) == 3
        assert v.add(["Kopf"], "Meine") == 1  # stands in another category now
        assert v.restore() == {MEDICINE: 1, BODY_PARTS: 1, FINDINGS: 0, EPONYMS: 0}
        assert v.contains("Wade") and v.contains("Wolf")
        assert [k for b, k, _a in v.all_items(search="Kopf") if b == "Kopf"] == ["Meine"]
        assert v.store.info(v.store.local_url(MEDICINE)).redistributable  # still the list


def test_what_is_added_goes_into_the_list_of_its_category(fresh: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        assert v.add(["Flantzitis"], "Eigene") == 1
        info = next(i for i in v.store.packages() if i.title == "Eigene")
        assert info.url == v.store.local_url("Eigene") and info.url.startswith("urn:uuid:")
        assert (info.concepts, info.redistributable, info.publisher) == (1, False, "")
        assert v.add(["Flantzom", "Flantzitis"], "Eigene") == 1
        assert _concepts(v, "Eigene") == {"Flantzitis", "Flantzom"}
        assert len(v.store.packages()) == 5  # written again, not beside itself
        # a bundled list with a term of the user's is no longer the list that may be passed on
        assert v.add(["Flantzsyndrom"], MEDICINE) == 1
        medicine = next(i for i in v.store.packages() if i.title == MEDICINE)
        assert not medicine.redistributable and medicine.publisher == "Wikidata"
        assert dict(v.categories())[MEDICINE] == len(vocab_mod.bundled()[MEDICINE]) + 1
        # a change of case is a rename: the old spelling is hidden, the new one is a concept
        assert v.rename("Flantzitis", "FLANTZITIS")
        assert _terms(v, "Eigene") == ["FLANTZITIS", "Flantzom"]
        assert _concepts(v, "Eigene") == {"Flantzitis", "FLANTZITIS", "Flantzom"}
        # moved to another category and back: found whatever its case, kept as it is spelt
        assert v.set_category(["Flantzom"], BODY_PARTS) == 1
        assert "Flantzom" in _terms(v, BODY_PARTS) and _terms(v, "Eigene") == ["FLANTZITIS"]
        assert v.set_category(["flantzom"], "Eigene") == 1
        assert _terms(v, "Eigene") == ["FLANTZITIS", "Flantzom"]
        assert "Flantzom" not in _terms(v, BODY_PARTS)
        assert v.set_category(["Flantzom"], "Eigene") == 1  # found; nothing to move
        assert v.set_category(["gibtesnicht"], "Eigene") == 0
        assert v.set_category(["Flantzom"], " ") == 1  # no category: the user's own
        assert _terms(v, vocab_mod.CATEGORY_USER) == ["Flantzom"]


def test_a_list_another_program_made_is_a_category_too(fresh: Path) -> None:
    """lexterm's store is shared.  A list another program put there without a URL of its
    own is one of the store's own lists, however that program named and coded it: it
    protects, is shown and can be deleted, and is never written over.  (What brings a URL
    of its own is another matter: ``test_foreign_packages.py``.)"""
    with vocab_mod.Vocabulary() as v:
        url = v.store.add_package(Package(
            url="", version="1", name="Flantzliste", title="Flantzliste", language="de",
            concepts=[Concept("F1", "Flantzfieber", "", "", [
                Term("Flantzfieber", "de"), Term("Flantz fever", "en"),
                Term("Morbus Flantz", "de")]),
                Concept("Flantzgrippe", "", "", "", [Term("Flantzgrippe", "de")])])).url
        assert url == v.store.local_url("flantzliste")
        assert _terms(v, "Flantzliste") == ["Flantzfieber", "Flantzgrippe", "Morbus Flantz"]
        assert not v.contains("Flantz fever")  # German text is searched for German terms
        assert v.remove(["Morbus Flantz", "Flantzgrippe"]) == 2
        assert v.store.overlay(url) == [Change(url, "F1", "hide", "Morbus Flantz", "de"),
                                        Change(url, "Flantzgrippe", "hide")]
        assert _terms(v, "Flantzliste") == ["Flantzfieber"]  # the concept's other name stays
        assert v.add(["Flantzhusten"], "Flantzliste") == 1
        assert _concepts(v, "Flantzliste") == {"F1", "Flantzgrippe", "Flantzhusten"}
        assert _terms(v, "Flantzliste") == ["Flantzfieber", "Flantzhusten"]
        # a name that program's user gave a concept in the overlay goes by being taken back
        v.store.add_term(url, "F1", Term("Flantzleiden", "de"))
        assert v.contains("Flantzleiden") and v.remove(["Flantzleiden"]) == 1
        assert not v.contains("Flantzleiden")
        assert all(change.action == "hide" for change in v.store.overlay(url))
    fb = terms_module.standard()
    assert fb.is_medical_term("Flantzfieber") and fb.is_medical_term("Flantzhusten")
    assert not fb.is_medical_term("Flantzgrippe")
    assert not fb.is_protected("Morbus Flantz", 0, 13, whole=True)


def test_csv_round_trip(fresh: Path, tmp_path: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        v.add(["Alpha-Test", "Beta Begriff"], "Kat A")
        n = v.export_csv(tmp_path / "a.csv", category="Kat A")
    text = (tmp_path / "a.csv").read_text(encoding="utf-8")
    assert n == 2 and text == "term,category\nAlpha-Test,Kat A\nBeta Begriff,Kat A\n"
    (tmp_path / "b.csv").write_text("Begriff;Kategorie\nGamma;Kat B\nAlpha-Test;Kat A\nDelta;\n",
                                    encoding="utf-8")
    (tmp_path / "c.txt").write_text("# Liste\nEpsilon\n\nZeta Zwei\n", encoding="utf-8")
    with vocab_mod.Vocabulary() as v:
        assert v.import_csv(tmp_path / "b.csv", "Standard") == (2, 3)
        assert v.import_csv(tmp_path / "c.txt", "Liste") == (2, 2)
        assert {b: k for b, k, _a in v.all_items(search="Delta")}["Delta"] == "Standard"
        assert [k for _b, k, _a in v.all_items(search="Zeta Zwei")] == ["Liste"]
        assert v.import_csv(tmp_path / "c.txt", "Liste") == (0, 2)
    fb = terms_module.standard()
    assert fb.is_protected("Zeta Zwei", 0, 9, whole=True)


def test_a_csv_can_say_the_whole_vocabulary(fresh: Path, tmp_path: Path) -> None:
    """What the export wrote makes the vocabulary exactly what it was: deleted terms come
    back, later ones go, each in its category."""
    with vocab_mod.Vocabulary() as v:
        v.add(["Flantzitis", "Alpha, Beta"], "Eigene")
        v.add(["Flantzsyndrom"], MEDICINE)
        v.remove(["Wade"])
        before = v.all_items()
        assert v.export_csv(tmp_path / "alles.csv") == len(before)
        v.add(["Flantzom"], "Andere")
        v.remove(["Flantzitis", "Kopf", "Flantzsyndrom"])
        v.add(["Wade"], "Eigene")
        assert v.all_items() != before
        assert v.replace_from_csv(tmp_path / "alles.csv") == len(before)
        assert v.all_items() == before
        assert "Andere" not in dict(v.categories()) and not v.contains("Wade")
        assert "Wade" in _concepts(v, BODY_PARTS)  # hidden in its list, as a deletion is
        assert len(_concepts(v, MEDICINE)) == len(vocab_mod.bundled()[MEDICINE]) + 1
    fb = terms_module.standard()
    assert fb.is_medical_term("Flantzitis") and fb.is_medical_term("Kopf")
    assert not fb.is_medical_term("Flantzom") and not fb.is_medical_term("Wade")


def test_categories_that_differ_in_case_only_are_one_list(fresh: Path) -> None:
    """The store names its own lists without case: no list takes the place of another."""
    with vocab_mod.Vocabulary(seed=False) as v:
        assert v.add(["Flantzbein"], "Body Parts") == 1  # before the bundled list is there
        assert v.seed().keys() == {MEDICINE, FINDINGS, EPONYMS}  # its place is taken
        assert v.contains("Flantzbein")
    with vocab_mod.Vocabulary() as v:
        assert v.add(["Marcuflantz"], "Medikament") == 1
        assert v.add(["Flantzoprazol"], "medikament") == 1
        assert _terms(v, "Medikament") == ["Flantzoprazol", "Marcuflantz"]
        v.replace({"medikament": ["Flantzoprazol"], "Medikament": ["Marcuflantz", "Flantzin"],
                   "KÖRPERTEILE": ["Kopf"], "": ["Flantzwort"]})  # German: the English list
        assert dict(v.categories()) == {BODY_PARTS: 1, "medikament": 3,
                                        vocab_mod.CATEGORY_USER: 1}
        assert _terms(v, "medikament") == ["Flantzin", "Flantzoprazol", "Marcuflantz"]


def test_statistics_count_the_store(fresh: Path) -> None:
    empty = stats_mod.statistics()  # no store yet: nothing is created to count it
    assert empty.vocabulary_terms == 0 and not (fresh / "lexterm.sqlite").exists()
    with vocab_mod.Vocabulary() as v:
        v.add(["Sicherungsbegriff"], "Test")
    st = stats_mod.statistics()
    assert st.vocabulary_terms > 39000 and dict(st.vocabulary_categories)["Test"] == 1
    assert dict(st.lines())["Vokabular (Begriffe)"] == str(st.vocabulary_terms)


def test_vocabulary_page(qapp: QApplication, fresh: Path, tmp_path: Path) -> None:  # noqa: F811
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    window = MainWindow(Session())
    assert [window.tabs.tabText(i) for i in range(4)] == [
        "1 Patientendaten", "2 Identifikatoren", "3 Fachvokabular", "4 Dokumente"]
    page = window.vocabulary
    page.synchronous = True
    window.show_page(page)
    qapp.processEvents()
    total = page.model.rowCount()
    assert total > 39000 and page.proxy.rowCount() == total
    assert page.model.columnCount() == 2  # term and category: the store keeps no day
    page.search.setText("nierenlag")
    assert 1 <= page.proxy.rowCount() <= 5
    page.search.clear()
    assert page.add("Testbegriff Neu", "Meine") == 1
    assert page.add("Testbegriff Neu") == 0
    assert page.add("ab") == 0 and "drei Zeichen" in page.status.text()
    assert terms_module.standard().is_protected("Testbegriff Neu", 0, 15, whole=True)
    # the category filter narrows the table; sorting by the header works on the proxy
    i = page.category_filter.findData("Meine")
    page.category_filter.setCurrentIndex(i)
    assert page.model.rowCount() == 1 and page.model.lines[0][0] == "Testbegriff Neu"
    from PySide6.QtCore import Qt

    page.category_filter.setCurrentIndex(0)
    page.view.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    first = page.proxy.index(0, 1).data()
    assert first.casefold() >= page.proxy.index(page.proxy.rowCount() - 1, 1).data().casefold()
    # editing through the model writes to the store
    source = next(r for r, z in enumerate(page.model.lines) if z[0] == "Testbegriff Neu")
    idx = page.model.index(source, 1)
    assert page.model.setData(idx, "Andere") and page.model.data(idx) == "Andere"
    with vocab_mod.Vocabulary() as v:
        assert [k for _b, k, _a in v.all_items(search="Testbegriff Neu")] == ["Andere"]
    assert not page.model.setData(page.model.index(source, 0), "Wade")  # exists already
    # delete, export, import, bring the bundled terms back
    assert page.delete(["Testbegriff Neu", "Wolf"]) == 2
    assert not terms_module.standard().is_medical_term("Wolf")
    assert page.export_csv(tmp_path / "alles.csv") == page.model.rowCount()
    (tmp_path / "neu.txt").write_text("Importbegriff\n", encoding="utf-8")
    assert page.import_file(tmp_path / "neu.txt", "Liste") == 1
    assert page.restore_base_set() == 1  # "Wolf" comes back
    assert terms_module.standard().is_medical_term("Wolf")
    window.analyse.refresh()
    info = window.analyse.vocabulary_info.text()  # the sources by their German labels
    assert "Medizin (Wikidata)" in info and "Körperteile" in info and "body parts" not in info
    window.close()


def test_a_store_hides_the_eponyms_struck_from_the_list(fresh: Path) -> None:
    """A store that still holds a common name in its list of eponyms hides it, as a
    deletion would, and keeps what the user added to that list."""
    with vocab_mod.Vocabulary() as v:
        url = v.store.local_url(vocab_mod.CATEGORY_EPONYMS)
        assert "becker" not in {e.term for e in v.store.entries([url])}  # a new store lacks it
        v.store.add_package(Package(url=url, name=vocab_mod.CATEGORY_EPONYMS,
                                    title=vocab_mod.CATEGORY_EPONYMS, language="de", concepts=[
            Concept(term, terms=[Term(term, "de")])
            for term in ("babinski", "becker", "hoffmann", "flantz")]))
    with vocab_mod.Vocabulary() as v:  # the store as an older PseudonyMed left it
        assert sorted(e.term for e in v.store.entries([url])) == ["babinski", "flantz"]
        assert len(v.store.overlay(url)) == 2
    with vocab_mod.Vocabulary() as v:  # nothing more to do a second time
        assert len(v.store.overlay(url)) == 2
