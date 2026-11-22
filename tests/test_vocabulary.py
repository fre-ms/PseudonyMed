"""The vocabulary as a database table: seeding, editing, CSV, the matcher's cache, the step."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms.store import PseudonymStore, default_path
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    terms_module.standard.cache_clear()
    yield tmp_path / "daten"
    terms_module.standard.cache_clear()


def test_base_stock_and_legacy(fresh: Path) -> None:
    folder = fresh / "fachbegriffe"
    folder.mkdir(parents=True)
    (folder / "nutzervokabular.txt").write_text("# alt\nFlantzitis\n", encoding="utf-8")
    (folder / "klinik.txt").write_text("Musterklinikum\n", encoding="utf-8")
    with vocab_mod.Vocabulary() as v:
        assert v.path == default_path() and v.count() > 39000
        categories = dict(v.categories())
        assert categories[vocab_mod.CATEGORY_MEDICINE] > 39000
        # body parts already in the Wikidata list keep that category: only the rest counts here
        assert categories[vocab_mod.CATEGORY_BODY_PARTS] > 200 and categories["klinik"] == 1
        assert categories[vocab_mod.CATEGORY_USER] == 1 and categories[vocab_mod.CATEGORY_EPONYMS]
        assert v.contains("Flantzitis") and v.contains("Nierenlager") and v.version() >= 1
    assert not (folder / "nutzervokabular.txt").exists()
    assert (folder / "nutzervokabular.txt.uebernommen").exists()
    fb = terms_module.standard()
    assert fb.is_medical_term("Flantzitis") and fb.is_medical_term("Musterklinikum")
    assert any(q == "klinik" for q, _n in fb.sources)
    # the store schema carries the table as well
    with PseudonymStore() as sp:
        assert sp.connection.execute("SELECT COUNT(*) FROM vokabular").fetchone()[0] > 39000


def test_edit_and_cache(fresh: Path) -> None:
    fb = terms_module.standard()
    assert fb.is_medical_term("Wolf") and not fb.is_medical_term("Flantzose")
    with vocab_mod.Vocabulary() as v:
        v1 = v.version()
        assert v.add(["Flantzose", "  flantzose ", "ab"], "Test") == 1  # case, length
        assert v.remove(["Wolf", "gibtesnicht"]) == 1
        assert v.rename("Flantzose", "Flantzosis") and not v.rename("Flantzosis", "Wade")
        assert v.set_category(["Flantzosis"], "Neu") == 1
        assert v.version() > v1
        assert [z[1] for z in v.all_items(search="flantzos")] == ["Neu"]
        assert v.all_items(search="100%") == []
        assert v.all_items(category="Neu")[0][0] == "Flantzosis"
    terms2 = terms_module.standard()  # rebuilt because the version changed
    assert terms2 is not fb and terms2.is_medical_term("Flantzosis")
    assert not terms2.is_medical_term("Wolf")
    assert terms_module.standard() is terms2  # unchanged table: cached


def test_csv_round_trip(fresh: Path, tmp_path: Path) -> None:
    with vocab_mod.Vocabulary() as v:
        v.add(["Alpha-Test", "Beta Begriff"], "Kat A")
        n = v.export_csv(tmp_path / "a.csv", category="Kat A")
    text = (tmp_path / "a.csv").read_text(encoding="utf-8")
    assert n == 2 and text == "begriff,kategorie\nAlpha-Test,Kat A\nBeta Begriff,Kat A\n"
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


def test_backup_with_vocabulary(fresh: Path, tmp_path: Path) -> None:
    with PseudonymStore():  # creates the key file the backup insists on
        pass
    with vocab_mod.Vocabulary() as v:
        v.add(["Sicherungsbegriff"], "Test")
    st = backup_mod.statistics()
    assert st.vocabulary_terms > 39000 and dict(st.vocabulary_categories)["Test"] == 1
    assert "Vokabular (Begriffe)" in dict(st.lines())
    archive = backup_mod.export(tmp_path / "s.zip")
    other = tmp_path / "andere" / "p.sqlite"
    with vocab_mod.Vocabulary(other) as v2:
        assert not v2.contains("Sicherungsbegriff")
    b = backup_mod.import_file(archive, other, mode="zusammenfuehren")
    assert b.new_vocabulary >= 1
    with vocab_mod.Vocabulary(other) as v2:
        assert v2.contains("Sicherungsbegriff")


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
    # editing through the model writes to the table
    source = next(r for r, z in enumerate(page.model.lines) if z[0] == "Testbegriff Neu")
    idx = page.model.index(source, 1)
    assert page.model.setData(idx, "Andere") and page.model.data(idx) == "Andere"
    with vocab_mod.Vocabulary() as v:
        assert [k for _b, k, _a in v.all_items(search="Testbegriff Neu")] == ["Andere"]
    assert not page.model.setData(page.model.index(source, 0), "Wade")  # exists already
    # delete, export, import, reseed
    assert page.delete(["Testbegriff Neu", "Wolf"]) == 2
    assert not terms_module.standard().is_medical_term("Wolf")
    assert page.export_csv(tmp_path / "alles.csv") == page.model.rowCount()
    (tmp_path / "neu.txt").write_text("Importbegriff\n", encoding="utf-8")
    assert page.import_file(tmp_path / "neu.txt", "Liste") == 1
    assert page.restore_base_set() >= 1  # "Wolf" comes back
    assert terms_module.standard().is_medical_term("Wolf")
    assert "Nutzervokabular" in window.analyse.vocabulary_info.text() or \
        window.analyse.refresh() is None
    window.close()
