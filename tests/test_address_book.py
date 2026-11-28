"""Address books (local vCards and CardDAV together), storage paths and the patient step."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed import config as config_module
from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import (
    PseudonymStore,
    default_key_path,
    default_path,
)
from pseudonymed.sources import address_book
from tests.carddav_fake import FakeCardDAV
from tests.synthetic import synthetic_patient, vcard_from_patient
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "daten"
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(folder))
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_URL", raising=False)
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_PASSWORD", raising=False)
    return folder


def _vcf(path: Path, *patients: Patient) -> Path:
    path.write_text("".join(vcard_from_patient(p, f"uid-{i}") for i, p in enumerate(patients)),
                    encoding="utf-8")
    return path


def test_local_address_book(data_folder: Path, tmp_path: Path, patient: Patient,
                            female_patient: Patient) -> None:
    file = _vcf(tmp_path / "kontakte.vcf", patient, female_patient)
    assert address_book.search_local(file, patient.surname[:4]) and \
        address_book.search_local(file, "")[1].surname == female_patient.surname
    folder = tmp_path / "buch"
    folder.mkdir()
    _vcf(folder / "a.vcf", patient)
    _vcf(folder / "b.vcf", female_patient)
    (folder / "notiz.txt").write_text("kein vcf")
    assert len(address_book.local_files(folder)) == 2
    assert len(address_book.search_local(folder, str(female_patient.birth_date.year))) >= 1
    assert not address_book.is_set_up()
    e = config_module.load_settings()
    e.local_address_book = str(folder)
    config_module.save_settings(e)
    assert address_book.is_set_up()
    result = address_book.search(patient.surname)
    assert [p.surname for p in result.patients] == [patient.surname]
    assert result.sources and not result.error


def test_both_sources_and_password_in_file(data_folder: Path, tmp_path: Path, patient: Patient,
                                            female_patient: Patient,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Local book and CardDAV answer together; without a keyring the password may live in
    the configuration file."""
    server = FakeCardDAV(books={"patienten": ("Patienten", [vcard_from_patient(female_patient,
                                                                                  "u1")])}).start()
    try:
        e = config_module.load_settings()
        e.local_address_book = str(_vcf(tmp_path / "lokal.vcf", patient))
        config_module.save_settings(e)
        k = config_module.CardDAVConfig(url=server.url, user=server.user,
                                        address_book="patienten", password_in_file=server.password)
        monkeypatch.setattr(config_module, "save_password", lambda *_a, **_k: False)
        config_module.save_config(k)
        loaded = config_module.load_config()
        assert loaded.password_in_file == server.password
        assert config_module.load_password(loaded) == server.password
        result = address_book.search("")  # everything from both
        names = {p.surname for p in result.patients}
        assert {patient.surname, female_patient.surname} <= names and not result.error
        assert len(result.sources) == 2
        # a wrong password fails only the CardDAV source
        k.password_in_file = "falsch"
        config_module.save_config(k)
        result = address_book.search(patient.surname)
        assert result.patients and len(result.error) == 1 and "CardDAV" in result.error[0]
        # CLI: contacts come from both books
        runner = CliRunner()
        k.password_in_file = server.password
        config_module.save_config(k)
        r = runner.invoke(app, ["kontakte", "suchen", female_patient.surname[:4]])
        assert r.exit_code == 0 and female_patient.surname in r.output, r.output
        r = runner.invoke(app, ["kontakte", "lokal"])
        assert r.exit_code == 0 and "lokal.vcf" in r.output
        r = runner.invoke(app, ["kontakte", "lokal", "--entfernen"])
        assert r.exit_code == 0 and not config_module.load_settings().local_address_book
        r = runner.invoke(app, ["kontakte", "lokal", str(tmp_path / "fehlt.vcf")])
        assert r.exit_code == 1
    finally:
        server.stop()


def test_storage_location_configurable(data_folder: Path, tmp_path: Path, report_pdf: Path,
                                       patient: Patient) -> None:
    from pseudonymed.pipeline import PipelineOptions, pseudonymise

    assert default_path() == data_folder / "pseudonyms.sqlite"
    with PseudonymStore() as sp:
        res = pseudonymise(report_pdf, patient, PipelineOptions(report=False, text_file=False,
                                                           target_folder=tmp_path / "aus"),
                           store=sp)
        code = res.code
    assert (data_folder / "pseudonym-key.txt").exists()
    new_db, new_key = tmp_path / "tresor" / "pseudonymed.sqlite", tmp_path / "keys" / "k.txt"
    with pytest.raises(backup_mod.BackupError):
        backup_mod.relocate(Path("relativ.sqlite"), new_key)
    v = backup_mod.relocate(new_db, new_key)
    assert v.copied and len(v.renamed) == 2 and all(p.exists() for p in v.renamed)
    assert default_path() == new_db and default_key_path() == new_key
    assert not (data_folder / "pseudonyms.sqlite").exists()
    with PseudonymStore() as sp:  # default store now lives there, same key, same code
        assert sp.path == new_db and sp.key_path == new_key
        assert sp.code_for(patient).code == code
    st = stats_mod.statistics()
    assert st.path == new_db and st.key_path == new_key and st.patients == 1
    # an explicit database path keeps its key next to it (tests, --speicher)
    with PseudonymStore(tmp_path / "anders" / "x.sqlite") as sp:
        assert sp.key_path == tmp_path / "anders" / "pseudonym-key.txt"
    # a second move with another database already at the target is refused
    foreign = tmp_path / "fremd" / "p.sqlite"
    with PseudonymStore(foreign) as sp:
        sp.code_for(synthetic_patient(3, "f"))
    with pytest.raises(backup_mod.BackupError, match="schon eine Datenbank"):
        backup_mod.relocate(foreign, new_key)
    runner = CliRunner()
    r = runner.invoke(app, ["datenbank", "speicherort"])
    assert r.exit_code == 0 and str(new_db) in r.output and str(new_key) in r.output


def test_patient_page(qapp: QApplication, data_folder: Path, tmp_path: Path,  # noqa: F811
                      patient: Patient, female_patient: Patient) -> None:
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.patient_page import ListField, lines_from_file
    from pseudonymed.gui.session import Session

    window = MainWindow(Session(store_path=tmp_path / "s.sqlite"))
    page = window.patient
    page.synchronous = True
    assert not page.match_group.isVisibleTo(window)  # nothing to show yet
    page.search.setText(patient.surname)
    assert page.run_search() is None and "Kein Adressbuch" in page.note.text()
    e = config_module.load_settings()
    e.local_address_book = str(_vcf(tmp_path / "k.vcf", patient, female_patient))
    config_module.save_settings(e)
    result = page.run_search()
    assert result is not None and len(result.patients) == 1
    assert page.match_group.isVisibleTo(window) and page.listing.count() == 1
    assert page.f["surname"].text() == patient.surname  # a single hit fills the form
    page.search.setText("gibtesnicht")
    page.run_search()
    assert not page.match_group.isVisibleTo(window) and "Kein Treffer" in page.note.text()
    # a dropped text file adds its lines to a list field
    listing = tmp_path / "zuweiser.txt"
    listing.write_text("# Zuweiser\nDr. med. Anna Beispiel\n\nDr. Max Muster\n", encoding="utf-8")
    assert lines_from_file(listing) == ["Dr. med. Anna Beispiel", "Dr. Max Muster"]
    field_name: ListField = page.referrers
    field_name.setPlainText("Dr. Max Muster")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(listing))])
    assert field_name.canInsertFromMimeData(mime)
    field_name.insertFromMimeData(mime)
    assert field_name.toPlainText().splitlines() == ["Dr. Max Muster", "Dr. med. Anna Beispiel"]
    assert [z.surname for z in page.patient().referrers] == ["Muster", "Beispiel"]
    window.close()


def test_address_book_dialog(qapp: QApplication, data_folder: Path, tmp_path: Path,  # noqa: F811
                             patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.address_book_dialog import AddressBookDialog

    server = FakeCardDAV(books={"patienten": ("Patienten", [vcard_from_patient(patient,
                                                                                  "u1")])}).start()
    try:
        monkeypatch.setattr(config_module, "save_password", lambda *_a, **_k: False)
        import pseudonymed.gui.address_book_dialog as dialog_module

        monkeypatch.setattr(dialog_module, "save_password", lambda *_a, **_k: False)
        dialog = AddressBookDialog()
        dialog.local.setText(str(tmp_path / "fehlt.vcf"))
        assert not dialog.save() and "keine vCard" in dialog.status.text()
        dialog.local.setText(str(_vcf(tmp_path / "k.vcf", patient)))
        dialog.url.setText(server.url)
        dialog.user.setText(server.user)
        assert dialog.load_address_books() == [] and "App-Passwort" in dialog.status.text()
        dialog.password.setText(server.password)
        books = dialog.load_address_books()
        assert "patienten" in books and dialog.address_book.count() >= 1
        assert dialog.save()
        e = config_module.load_settings()
        k = config_module.load_config()
        assert e.local_address_book.endswith("k.vcf") and k.url == server.url
        assert k.password_in_file == server.password  # no keyring: kept in the 0600 file
        assert address_book.search(patient.surname).patients
    finally:
        server.stop()


def test_database_page_storage_location(qapp: QApplication, data_folder: Path,  # noqa: F811
                                        tmp_path: Path) -> None:
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    with PseudonymStore() as sp:
        sp.code_for(synthetic_patient(5, "m"))
    window = MainWindow(Session())  # default paths: the location is editable
    page = window.database
    page.synchronous = True
    page.refresh()
    assert page.database_location.text() == str(data_folder / "pseudonyms.sqlite")
    page.database_location.setText(str(tmp_path / "woanders" / "db.sqlite"))
    page.key_location.setText(str(tmp_path / "woanders" / "schluessel.txt"))
    v = page.set_storage_location()
    assert v is not None and v.copied and "Speicherort" in page.location_status.text()
    assert default_path() == tmp_path / "woanders" / "db.sqlite"
    assert stats_mod.statistics().patients == 1
    window.close()
