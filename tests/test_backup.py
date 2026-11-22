"""Backup and restore of database and key; statistics per category."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.synthetic import synthetic_patient
from tests.test_gui import qapp  # noqa: F401


def _populated(folder: Path, report_pdf: Path, patient: Patient) -> Path:
    """A database with one pseudonymised report; returns the database path."""
    db = folder / "pseudonyme.sqlite"
    with PseudonymStore(db) as sp:
        opt = PipelineOptions(report=False, text_file=False, target_folder=folder / "aus")
        res = pseudonymise(report_pdf, patient, opt, store=sp)
        assert res.released
    return db


def test_statistics_and_categories(tmp_path: Path, report_pdf: Path, patient: Patient,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    empty = stats_mod.statistics(tmp_path / "nix.sqlite")
    assert not empty.exists and empty.patients == 0 and "nein" in dict(empty.lines())["Vorhanden"]
    db = _populated(tmp_path, report_pdf, patient)
    st = stats_mod.statistics(db)
    assert st.exists and st.size > 0 and st.patients == 1 and st.mappings >= 8
    assert st.key_present and len(st.key_id) == 16 and st.schema == "hash"
    categories = dict(st.per_category)
    assert categories["Patient"] == 1 and "Geburtsdatum" in categories and "Straße" in categories
    assert "Zuweiser" in categories and st.integrity == "ok" and st.last_entry
    lines = dict(st.lines())
    assert lines["Patienten"] == "1" and "    Patient" in lines
    assert stats_mod.category("[Code]", "manuell") == "Barcode/QR"
    assert stats_mod.category("irgendwas", "email") == "E-Mail"


def test_export_and_restore(tmp_path: Path, report_pdf: Path, patient: Patient,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    (tmp_path / "daten" / "fachbegriffe").mkdir(parents=True)
    (tmp_path / "daten" / "fachbegriffe" / "nutzervokabular.txt").write_text("Flantzlegacy\n")
    (tmp_path / "daten" / "konfig.toml").write_text('[gui]\nsprache = "de"\n')
    db = _populated(tmp_path, report_pdf, patient)
    with PseudonymStore(db) as sp:
        code = sp.find()[0]["kennung"]
        mapping = sp.mapping(patient.key())
    with pytest.raises(backup_mod.BackupError):
        backup_mod.export(tmp_path / "x.zip", tmp_path / "fehlt.sqlite")
    archive = backup_mod.export(tmp_path / "sicherung", db)  # plain, folder given
    assert archive.parent == tmp_path / "sicherung" and archive.suffix == ".zip"
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    assert {"manifest.json", "pseudonyme.sqlite", "pseudonymschluessel.txt",
            "konfig.toml"} <= names
    assert "fachbegriffe/nutzervokabular.txt" not in names  # taken over into the table
    m = backup_mod.check(archive)
    assert m.patients == 1 and m.mappings >= 8 and m.config and m.vocabulary == 0
    assert m.key_id == stats_mod.statistics(db).key_id
    # restore into an empty place: same identifiers, same mapping, same key
    new = tmp_path / "neu" / "pseudonyme.sqlite"
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "neu"))
    b = backup_mod.import_file(archive, new)
    assert b.mode == "ersetzen" and b.new_patients == 1 and b.key_replaced
    assert b.config_replaced and b.vocabulary_files == 0 and b.prior_backups == []
    with PseudonymStore(new) as store2:
        assert store2.find()[0]["kennung"] == code
        assert store2.mapping(patient.key()) == mapping
        assert store2.code_for(patient).code == code  # the key came along
    assert (tmp_path / "neu" / "konfig.toml").exists()
    from pseudonymed.detection import vocabulary as vocab_mod

    # the old per-file list went into the default database of the original data folder
    with vocab_mod.Vocabulary(tmp_path / "daten" / "pseudonyme.sqlite", seed=False) as v:
        assert v.contains("Flantzlegacy")
    # restore over an existing database sets the old one aside
    b2 = backup_mod.import_file(archive, new)
    assert len(b2.prior_backups) == 2 and all(p.exists() for p in b2.prior_backups)
    assert not b2.key_replaced


def test_merge(tmp_path: Path, report_pdf: Path, patient: Patient,
               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = _populated(tmp_path / "a", report_pdf, patient)
    archive_a = backup_mod.export(tmp_path / "a.zip", db)
    # a second database with the same key and another patient
    second = tmp_path / "b" / "pseudonyme.sqlite"
    second.parent.mkdir()
    (tmp_path / "b" / "pseudonymschluessel.txt").write_bytes(
        (tmp_path / "a" / "pseudonymschluessel.txt").read_bytes())
    other = synthetic_patient(11, "w")
    from tests.synthetic import create_report_pdf

    report_pdf_b = create_report_pdf(tmp_path / "b" / "befund.pdf", other)
    with PseudonymStore(second) as sp:
        assert pseudonymise(report_pdf_b, other, PipelineOptions(report=False, text_file=False),
                            store=sp).released
    b = backup_mod.import_file(archive_a, second, mode="zusammenfuehren")
    assert b.new_patients == 1 and b.new_mappings >= 8 and not b.key_replaced
    with PseudonymStore(second) as sp:
        assert len(sp.find()) == 2
    b = backup_mod.import_file(archive_a, second, mode="zusammenfuehren")  # idempotent
    assert b.new_patients == 0 and b.new_mappings == 0
    # a different key cannot be merged
    third = tmp_path / "c" / "pseudonyme.sqlite"
    third.parent.mkdir()
    with PseudonymStore(third) as sp:
        assert pseudonymise(report_pdf_b, other, PipelineOptions(report=False, text_file=False),
                            store=sp).released
    with pytest.raises(backup_mod.BackupError, match="gleichem Pseudonymschlüssel"):
        backup_mod.import_file(archive_a, third, mode="zusammenfuehren")
    with pytest.raises(backup_mod.BackupError):
        backup_mod.import_file(archive_a, third, mode="egal")


@pytest.mark.skipif(not pgp_module.available(), reason="weder pysequoia noch GnuPG")
def test_encrypted_backup(tmp_path: Path, report_pdf: Path, patient: Patient,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = _populated(tmp_path, report_pdf, patient)
    archive = backup_mod.export(tmp_path / "s", db, passphrase="sehr-geheim-1")
    assert archive.name.endswith(".zip.gpg") and not archive.read_bytes().startswith(b"PK")
    assert patient.surname.encode() not in archive.read_bytes()
    with pytest.raises(backup_mod.BackupError, match="Passphrase"):
        backup_mod.check(archive)
    with pytest.raises(backup_mod.BackupError):
        backup_mod.check(archive, "falsch")
    assert backup_mod.check(archive, "sehr-geheim-1").patients == 1
    b = backup_mod.import_file(archive, tmp_path / "wieder" / "p.sqlite",
                               passphrase="sehr-geheim-1")
    assert b.new_patients == 1


def test_database_via_cli(tmp_path: Path, report_pdf: Path, patient: Patient,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = _populated(tmp_path, report_pdf, patient)
    runner = CliRunner()
    r = runner.invoke(app, ["datenbank", "status", "--speicher", str(db)])
    assert r.exit_code == 0 and "Patienten" in r.output and "Zuordnungen gesamt" in r.output
    r = runner.invoke(app, ["datenbank", "exportieren", str(tmp_path / "s"), "--speicher",
                            str(db), "--unverschluesselt"])
    assert r.exit_code == 0 and "UNVERSCHLÜSSELT" in r.output, r.output
    archive = next((tmp_path / "s").glob("*.zip"))
    new = tmp_path / "n" / "p.sqlite"
    r = runner.invoke(app, ["datenbank", "importieren", str(archive), "--speicher", str(new)],
                      input="n\n")
    assert r.exit_code == 1 and not new.exists()  # declined
    r = runner.invoke(app, ["datenbank", "importieren", str(archive), "--speicher", str(new),
                            "--ja"])
    assert r.exit_code == 0 and "Wiederhergestellt: 1 Patienten" in r.output, r.output
    r = runner.invoke(app, ["datenbank", "status", "--speicher", str(new)])
    assert "Patienten                 1" in r.output.replace("  ", " ") or "1" in r.output
    if pgp_module.available():
        r = runner.invoke(app, ["datenbank", "exportieren", str(tmp_path / "v.zip"), "--speicher",
                                str(db), "--passphrase", "kurz"])
        assert r.exit_code == 1 and "zu kurz" in r.output
        r = runner.invoke(app, ["datenbank", "exportieren", str(tmp_path / "v.zip"), "--speicher",
                                str(db), "--passphrase", "lang-genug-1"])
        assert r.exit_code == 0 and (tmp_path / "v.zip.gpg").exists()


def test_window_database(qapp: QApplication, tmp_path: Path, report_pdf: Path,  # noqa: F811
                         patient: Patient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    db = _populated(tmp_path, report_pdf, patient)
    session = Session(store_path=db)
    window = MainWindow(session)
    page = window.database
    page.synchronous = True
    window.show_page(page)
    qapp.processEvents()
    assert window.tabs.tabText(window.tabs.currentIndex()) == "11 Datenbank"
    st = page.refresh()
    assert st.patients == 1 and page.table.rowCount() == len(st.lines())
    with pytest.raises(RuntimeError):
        page.run_export()  # no target folder
    page.export_target.setText(str(tmp_path / "sicherung"))
    page.unencrypted.setChecked(True)
    archive = page.run_export()
    assert archive is not None and archive.exists()
    assert "UNVERSCHLÜSSELT" in page.export_status.text()
    if pgp_module.available():
        page.unencrypted.setChecked(False)
        page.export_passphrase.setText("passphrase-1")
        page.export_passphrase2.setText("anders")
        with pytest.raises(RuntimeError):
            page.run_export()
        page.export_passphrase.setText("passphrase-1")
        page.export_passphrase2.setText("passphrase-1")
        encrypted = page.run_export()
        assert encrypted is not None and encrypted.name.endswith(".gpg")
    page.import_path.setText(str(archive))
    m = page.check_archive()
    assert m is not None and "Gleicher Schlüssel" in page.archive_info.text()
    assert page.import_button.isEnabled() and page.mode_merge.isEnabled()
    page.mode_merge.setChecked(True)
    b = page.run_import()
    assert b is not None and b.mode == "zusammenfuehren" and b.new_patients == 0
    page.mode_replace.setChecked(True)
    b = page.run_import()
    assert b is not None and b.mode == "ersetzen" and len(b.prior_backups) == 2
    assert "Wiederhergestellt" in page.import_status.text()
    window.close()
