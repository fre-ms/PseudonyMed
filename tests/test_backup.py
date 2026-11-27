"""Backup and restore of database, key, vocabulary and identifiers; statistics per
category."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.detection import extra_data
from pseudonymed.detection import identifier_lists as lists
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import PseudonymStore, default_path
from tests.synthetic import synthetic_patient
from tests.test_gui import qapp  # noqa: F401
from tests.test_identifiers_move import (
    SMALL_DELETED,
    SMALL_STATES,
    former_lexicon,
    small_rows,
    tables_of,
    visible_rows,
)
from tests.test_vocabulary_move import edited_rows, former_table


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
    (tmp_path / "daten").mkdir()
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
    assert names == {"manifest.json", "pseudonyme.sqlite", "pseudonymschluessel.txt",
                     "konfig.toml", "vokabular.csv", "lexident.sqlite"}
    m = backup_mod.check(archive)
    assert m.patients == 1 and m.mappings >= 8 and m.config and m.vocabulary
    assert m.version == 3 and "· Vokabular" in m.description()
    assert m.key_id == stats_mod.statistics(db).key_id
    # restore into an empty place: same identifiers, same mapping, same key
    new = tmp_path / "neu" / "pseudonyme.sqlite"
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "neu"))
    b = backup_mod.import_file(archive, new)
    assert b.mode == "ersetzen" and b.new_patients == 1 and b.key_replaced
    assert b.config_replaced and b.vocabulary_terms > 39000 and b.prior_backups == []
    with PseudonymStore(new) as store2:
        assert store2.find()[0]["kennung"] == code
        assert store2.mapping(patient.key()) == mapping
        assert store2.code_for(patient).code == code  # the key came along
    assert (tmp_path / "neu" / "konfig.toml").exists()
    # restore over an existing database sets the old one aside (no identifiers here to keep)
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


def test_the_vocabulary_travels_as_a_csv(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive carries the vocabulary beside the database: merging adds what is new,
    restoring makes it exactly what it was, and an archive without it leaves it alone."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()  # the database and the key file the backup insists on
    with vocab_mod.Vocabulary() as v:
        v.add(["Sicherungsbegriff"], "Test")
        v.remove(["Wade"])
        saved = v.all_items()
    archive = backup_mod.export(tmp_path / "s.zip")
    without = backup_mod.export(tmp_path / "ohne.zip", vocabulary=False)
    assert backup_mod.check(archive).vocabulary and not backup_mod.check(without).vocabulary
    with zipfile.ZipFile(archive) as z:
        words = z.read("vokabular.csv").decode("utf-8")
        (tmp_path / "archiv.sqlite").write_bytes(z.read("pseudonyme.sqlite"))
    assert words.startswith("begriff,kategorie\n") and "\nSicherungsbegriff,Test\n" in words
    assert "\nWade," not in words and len(words.splitlines()) == len(saved) + 1
    assert vocab_mod.table_terms(tmp_path / "archiv.sqlite") is None  # not in the database
    with vocab_mod.Vocabulary() as v:  # the vocabulary changes after the backup
        v.remove(["Sicherungsbegriff", "Kopf"])
        v.add(["Danachbegriff"], "Danach")
        v.add(["Wade"], "Test")
        changed = v.all_items()
    for mode in backup_mod.MODES:  # an archive without it leaves it alone
        backup_mod.import_file(without, mode=mode)
        backup_mod.import_file(archive, mode=mode, vocabulary=False)
    with vocab_mod.Vocabulary() as v:
        assert v.all_items() == changed
    merged = backup_mod.import_file(archive, mode="zusammenfuehren")
    assert merged.new_vocabulary == 2  # the two terms deleted since
    assert "2 neue Vokabeln" in merged.description()
    assert "2 new vocabulary terms" in merged.description("en")
    with vocab_mod.Vocabulary() as v:
        assert v.contains("Sicherungsbegriff") and v.contains("Kopf")
        assert v.contains("Danachbegriff")
        assert [k for b, k, _a in v.all_items(search="Wade") if b == "Wade"] == ["Test"]
    restored = backup_mod.import_file(archive)
    assert restored.vocabulary_terms == len(saved)
    assert f"Vokabular mit {len(saved)} Begriffen" in restored.description()
    assert f"vocabulary of {len(saved)} terms" in restored.description("en")
    with vocab_mod.Vocabulary() as v:
        assert v.all_items() == saved and not v.contains("Danachbegriff")


def _former_archive(archive: Path, rows: list[tuple[str, str]] | None = None,
                    identifiers: bool = False) -> Path:
    """The archive as PseudonyMed wrote it before: no store of the identifiers beside the
    database, and inside the database the table of the vocabulary (``rows``; format 1, no
    CSV) or the tables of the lexicon (``identifiers``; format 2)."""
    with zipfile.ZipFile(archive) as z:
        files = {name: z.read(name) for name in z.namelist()}
    del files["lexident.sqlite"]
    database = archive.with_suffix(".sqlite")
    database.write_bytes(files["pseudonyme.sqlite"])
    if rows is not None:
        former_table(database, rows)
    if identifiers:
        former_lexicon(database, small_rows(), SMALL_STATES, SMALL_DELETED)
    files["pseudonyme.sqlite"] = database.read_bytes()
    manifest = json.loads(files.pop("manifest.json"))
    manifest["version"] = manifest[backup_mod._MARK] = 2 if rows is None else 1
    manifest["dateien"] = {name: {"sha256": backup_mod._sha(data), "bytes": len(data)}
                           for name, data in files.items()}
    former = archive.with_name("frueher.zip")
    with zipfile.ZipFile(former, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            z.writestr(name, data)
    return former


def test_an_archive_of_before_brings_its_table(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """An archive made while the vocabulary was a table of the database: merging adds the
    table's terms, restoring makes the vocabulary the table's, and the restored database
    keeps no table either way."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    with vocab_mod.Vocabulary() as v:
        v.add(["Heutebegriff"], "Heute")
    rows = edited_rows()
    former = _former_archive(backup_mod.export(tmp_path / "neu.zip", vocabulary=False), rows)
    assert not backup_mod.check(former).vocabulary and backup_mod.check(former).version == 1
    merged = backup_mod.import_file(former, mode="zusammenfuehren")
    assert merged.new_vocabulary == 4  # the four terms that are not bundled
    with vocab_mod.Vocabulary() as v:
        assert v.contains("Flantzitis") and v.contains("Heutebegriff") and v.contains("Wolf")
    restored = backup_mod.import_file(former)
    assert restored.vocabulary_terms == len(rows)
    assert vocab_mod.table_terms(default_path()) is None
    with vocab_mod.Vocabulary() as v:
        assert sorted((b, k) for b, k, _a in v.all_items()) == sorted(rows)
        assert not v.contains("Heutebegriff") and not v.contains("Wolf")
        v.add(["Nachherbegriff"], "Test")
    # left out on request: the vocabulary stays, and the table does not come back with it
    backup_mod.import_file(former, vocabulary=False)
    assert vocab_mod.table_terms(default_path()) is None
    with vocab_mod.Vocabulary() as v:
        assert v.contains("Nachherbegriff")
    # restored into another database: the vocabulary is one, whichever database it came with
    other = tmp_path / "andere" / "p.sqlite"
    assert backup_mod.import_file(former, other).vocabulary_terms == len(rows)
    assert vocab_mod.table_terms(other) is None
    with vocab_mod.Vocabulary() as v:
        assert not v.contains("Nachherbegriff") and v.count() == len(rows)


def _edited_identifiers() -> None:
    """A store as a user leaves it: a list fetched on request with one entry deleted and
    one renamed, and entries of the user's own in two classes."""
    town = {"ort": "Bergdorf", "land": "AT"}
    with ident_module.IdentifierTable() as table:
        table.install(extra_data.SOURCE_HOSPITALS_AT, [
            lists.Row("Flantzspital Bergdorf", "Klinik", "AT:K1", town),
            lists.Row("Flantzspital Talheim", "Klinik", "AT:K2", {"ort": "Talheim"}),
            lists.Row("Flantzheim Seeblick", "Klinik", "AT:K3", {"ort": "Seeblick"})],
            "2026-09-18 0123abcd", extra_data.CLINICS_AT_NOTE)
        table.add(["Flantzwerke GmbH", "Sicherungseintrag"], "Sonstiges")
        table.add(["Kunstschmiedin"], "Handwerk")
        table.remove(table.keys_for("Flantzspital Talheim"))
        assert table.change(table.keys_for("Flantzheim Seeblick")[0], "Flantzhaus Seeblick")


def _identifiers() -> tuple[list[ident_module.Entry], int, list[tuple[str, int, str]], list[str]]:
    """What the store of the identifiers holds: the entries, how many are deleted, the
    lists that were fetched and the towns of the hospitals."""
    with ident_module.IdentifierTable() as table:
        towns = sorted(e.term for e in table.store.entries() if e.cls == "hospital-town")
        return table.all_items(), table.deleted(), table.loaded(), towns


def _found(text: str) -> list[str]:
    """What the analysis finds of identifiers in a text."""
    return [text[k.start:k.end] for k in ident_module.standard().candidates(text)]


def test_the_identifiers_travel_as_their_store(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive carries the store of the identifiers beside the database: restoring puts
    it back as it was (the current one is copied aside), merging adds the user's own
    entries that are new."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    _edited_identifiers()
    saved = _identifiers()
    assert len(saved[0]) == 5 and saved[1] == 2 and saved[3] == ["Bergdorf", "Seeblick"]
    archive = backup_mod.export(tmp_path / "s.zip")
    with zipfile.ZipFile(archive) as z:
        (tmp_path / "archiv.sqlite").write_bytes(z.read("pseudonyme.sqlite"))
        (tmp_path / "archiv-lexident.sqlite").write_bytes(z.read("lexident.sqlite"))
    assert not {t for t in tables_of(tmp_path / "archiv.sqlite") if t.startswith("lexikon")}
    with ident_module.IdentifierTable(tmp_path / "archiv-lexident.sqlite",
                                      readonly=True) as archived:
        assert archived.all_items() == saved[0]
    with ident_module.IdentifierTable() as table:  # the identifiers change after the backup
        table.remove(table.keys_for("Sicherungseintrag") + table.keys_for("Kunstschmiedin"))
        table.add(["Danacheintrag"], "Sonstiges")
        table.remove(table.keys_for("Flantzspital Bergdorf"))  # one more of the fetched list
    text = "Erst der Sicherungseintrag, dann der Danacheintrag, zuletzt das Klinikum Bergdorf."
    assert _found(text) == ["Danacheintrag"]
    merged = backup_mod.import_file(archive, mode="zusammenfuehren")
    assert merged.new_identifiers == 2  # the two own entries deleted since
    assert "2 neue eigene Identifikatoren" in merged.description()
    assert "2 new identifiers of your own" in merged.description("en")
    after = _identifiers()
    assert {e.term for e in after[0] if e.own} == {
        "Flantzwerke GmbH", "Sicherungseintrag", "Kunstschmiedin", "Danacheintrag",
        "Flantzhaus Seeblick"}
    assert after[1] == 3 and after[3] == ["Seeblick"]  # merging leaves the deletions alone
    restored = backup_mod.import_file(archive)
    assert restored.identifiers == 5 and "5 Identifikatoren" in restored.description()
    assert "5 identifiers" in restored.description("en")
    assert [p.name.split(".vor-import-")[0] for p in restored.prior_backups] == [
        "pseudonyme.sqlite", "pseudonymschluessel.txt", "lexident.sqlite"]
    with ident_module.IdentifierTable(restored.prior_backups[2], readonly=True) as aside:
        assert "Danacheintrag" in [e.term for e in aside.all_items()]
    assert _identifiers() == saved
    # the analysis reads the store that came back, and the store can be worked on as before
    assert _found(text) == ["Sicherungseintrag", "Klinikum Bergdorf"]
    with ident_module.IdentifierTable() as table:
        assert table.add(["Nocheintrag"]) == 1 and table.count() == 6


def test_a_store_in_the_place_of_another_is_read_anew(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that takes the place of another may count as far as that one did: what was
    read of the one before must not answer for it."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    with ident_module.IdentifierTable() as table:
        table.add(["Erstereintrag"])
        version = table.version()
        assert [e.term for e in table.all_items()] == ["Erstereintrag"]
    text = "Der Erstereintrag und der Zweitereintrag."
    assert _found(text) == ["Erstereintrag"]
    with ident_module.IdentifierTable(tmp_path / "andere" / "lexident.sqlite") as other:
        other.add(["Zweitereintrag"])
        assert other.version() == version
    backup_mod._copy_database(tmp_path / "andere" / "lexident.sqlite",
                              ident_module.default_path())
    with ident_module.IdentifierTable() as table:
        assert table.version() == version
        assert [e.term for e in table.all_items()] == ["Zweitereintrag"]
    assert _found(text) == ["Zweitereintrag"]


def test_an_archive_of_before_brings_its_identifiers(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """An archive made while the identifiers were tables of the database: merging adds the
    user's own entries of the tables, restoring makes the store the tables', and the
    database keeps no such table either way."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    with ident_module.IdentifierTable() as table:
        table.add(["Heuteeintrag"], "Sonstiges")
    former = _former_archive(backup_mod.export(tmp_path / "neu.zip"), identifiers=True)
    assert backup_mod.check(former).version == 2 and backup_mod.check(former).vocabulary
    with zipfile.ZipFile(former) as z:
        assert "lexident.sqlite" not in z.namelist()
        (tmp_path / "archiv.sqlite").write_bytes(z.read("pseudonyme.sqlite"))
    rows = visible_rows(tmp_path / "archiv.sqlite")
    merged = backup_mod.import_file(former, mode="zusammenfuehren")
    assert merged.new_identifiers == 3  # the three entries of the user's own
    assert "lexikon" not in tables_of(default_path())
    with ident_module.IdentifierTable() as table:
        assert sorted(e.term for e in table.all_items()) == [
            "Flantzklinik Süd", "Flantzwerke GmbH", "Heuteeintrag", "Kunstschmiedin"]
    restored = backup_mod.import_file(former)
    assert restored.identifiers == len(rows) == 9
    assert restored.prior_backups[-1].name.startswith("lexident.sqlite.vor-import-")
    assert not {t for t in tables_of(default_path()) if t.startswith("lexikon")}
    with ident_module.IdentifierTable() as table:
        assert {(e.term, e.category, e.source, e.code) for e in table.all_items()} == {
            (term, category, source, code) for term, category, source, code, _extra in rows}
        assert table.deleted() == 2 and "Heuteeintrag" not in [e.term for e in table.all_items()]
        table.add(["Nachhereintrag"], "Sonstiges")
    # restored into another database: the identifiers are one, whichever database they came with
    other = tmp_path / "andere" / "p.sqlite"
    assert backup_mod.import_file(former, other).identifiers == 9
    assert not {t for t in tables_of(other) if t.startswith("lexikon")}
    with ident_module.IdentifierTable() as table:
        assert "Nachhereintrag" not in [e.term for e in table.all_items()]
        assert table.count() == 9


def test_restoring_another_database_lets_the_default_one_hand_over_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An archive is restored into another database while the default one still keeps its
    identifiers in tables: they move into the store first, so that the copy set aside holds
    them and no later start writes them over what the archive brought."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    PseudonymStore().close()
    with ident_module.IdentifierTable() as table:
        table.add(["Archiveintrag"])
    archive = backup_mod.export(tmp_path / "s.zip")
    former_lexicon(default_path(), small_rows(), SMALL_STATES, SMALL_DELETED)
    restored = backup_mod.import_file(archive, tmp_path / "andere" / "p.sqlite")
    assert "lexikon" not in tables_of(default_path())
    with ident_module.IdentifierTable(restored.prior_backups[-1], readonly=True) as aside:
        assert aside.count() == 9 and aside.deleted() == 2
    with ident_module.IdentifierTable() as table:
        assert [e.term for e in table.all_items()] == ["Archiveintrag"]


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
