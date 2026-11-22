"""Unit tests for the helpers behind backup, restore and relocation.  The round trips
live in ``test_backup.py``; here are the branches those only reach by accident:
archive naming, decryption guards, manifest checks, key validation, relocation refusals
and merging an archive that predates a table."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms import statistics as stats_mod
from pseudonymed.pseudonyms.store import _SCHEMA


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _manifest(files: dict[str, bytes]) -> bytes:
    return json.dumps({backup_mod._MARK: backup_mod.VERSION, "version": backup_mod.VERSION,
                       "dateien": {n: {"sha256": backup_mod._sha(d), "bytes": len(d)}
                                   for n, d in files.items()}}).encode()


def test_statistics_lines_order() -> None:
    st = stats_mod.Statistics(
        path=Path("/x/p.sqlite"), exists=True, per_category=[("Patient", 1)], discarded=2,
        discarded_per_rule=[("ner.person", 2)], vocabulary_categories=[("Test", 1)],
        lexicon_categories=[("Klinik", "merkmal", 5), ("Anatomie", "fach", 7)],
        lexicon_extra=[("Kliniken AT", 3, "2026-11-12T10:00:00")], config_present=True)
    assert [k for k, _ in st.lines()] == [
        "Datenbank", "Vorhanden", "Größe", "Schema", "Schlüsseldatei", "Schlüssel vorhanden",
        "Schlüssel-Kennung", "Patienten", "Verknüpfte Schreibweisen", "Zuordnungen gesamt",
        "    Patient", "Gemerkte Verwerfungen", "    ner.person", "Erster Eintrag",
        "Letzter Eintrag", "Integrität", "Seiten / frei", "Vokabular (Begriffe)", "    Test",
        "Lexikon (Einträge)", "    Klinik (Identifikator)", "    Anatomie (Fachklasse)",
        "Zusatzdaten (selbst geladen)", "Konfiguration"]
    values = dict(st.lines())
    assert values["Lexikon (Einträge)"] == "12" and values["Schema"] == "–"
    assert values["Zusatzdaten (selbst geladen)"] == "Kliniken AT: 3 Einträge, 2026-11-12"
    assert "Lexikon (Einträge)" not in dict(stats_mod.Statistics(path=Path("/x")).lines())


def test_archive_path(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    p = backup_mod._archive_path(folder, encrypted=False)
    assert p.parent == folder and p.name.startswith("pseudonymed_sicherung_") and p.suffix == ".zip"
    assert backup_mod._archive_path(tmp_path / "ohne", encrypted=True).name.endswith(".zip.gpg")
    gpg = tmp_path / "a.zip.gpg"
    assert backup_mod._archive_path(tmp_path / "a.zip", encrypted=True) == gpg
    assert backup_mod._archive_path(gpg, encrypted=True) == gpg  # never a second suffix
    assert backup_mod._archive_path(tmp_path / "a.zip", encrypted=False) == tmp_path / "a.zip"


def test_as_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert backup_mod._as_zip(b"PK\x03\x04rest", None) == b"PK\x03\x04rest"
    with pytest.raises(backup_mod.BackupError, match="Passphrase"):
        backup_mod._as_zip(b"\x85\x02", None)
    monkeypatch.setattr(pgp_module, "decrypt_with_passphrase", lambda data, pw: b"kein zip")
    with pytest.raises(backup_mod.BackupError, match="kein ZIP"):
        backup_mod._as_zip(b"\x85\x02", "pw")

    def fails(data: bytes, pw: str) -> bytes:
        raise pgp_module.PGPError("falsche Passphrase")

    monkeypatch.setattr(pgp_module, "decrypt_with_passphrase", fails)
    with pytest.raises(backup_mod.BackupError, match="falsche Passphrase"):
        backup_mod._as_zip(b"\x85\x02", "pw")
    monkeypatch.setattr(pgp_module, "decrypt_with_passphrase", lambda data, pw: b"PK ok")
    assert backup_mod._as_zip(b"\x85\x02", "pw") == b"PK ok"


def test_manifest_from() -> None:
    with pytest.raises(backup_mod.BackupError, match="Kein PseudonyMed"):
        backup_mod._manifest_from(b'{"version": 1}')
    newer = json.dumps({backup_mod._MARK: 1, "version": backup_mod.VERSION + 1}).encode()
    with pytest.raises(backup_mod.BackupError, match="neueren"):
        backup_mod._manifest_from(newer)
    old = json.dumps({backup_mod._FORMER_MARK: 1, "version": 1, "patienten": 3,
                      "unbekannt": "x"}).encode()
    m = backup_mod._manifest_from(old)
    assert m.patients == 3 and m.version == 1 and not m.config


def test_read_archive_and_completeness() -> None:
    with pytest.raises(backup_mod.BackupError, match="Beschädigtes"):
        backup_mod._read_archive(b"PK kein zip")
    with pytest.raises(backup_mod.BackupError, match="manifest.json fehlt"):
        backup_mod._read_archive(_zip({"a.txt": b"x"}))
    files = {backup_mod.NAME_DB: b"db", backup_mod.NAME_KEY: b"00ff\n"}
    manifest, contents = backup_mod._read_archive(
        _zip({backup_mod.NAME_MANIFEST: _manifest(files), **files}))
    assert contents == files and set(manifest.files) == set(files)
    backup_mod._check_completeness(manifest, contents)
    with pytest.raises(backup_mod.BackupError, match="fehlt im Archiv"):
        backup_mod._check_completeness(manifest, {backup_mod.NAME_DB: b"db"})
    with pytest.raises(backup_mod.BackupError, match="Prüfsumme"):
        backup_mod._check_completeness(manifest, {**contents, backup_mod.NAME_DB: b"anders"})
    with pytest.raises(backup_mod.BackupError, match="ohne Datenbank"):
        backup_mod._check_completeness(backup_mod.Manifest(), {backup_mod.NAME_CONFIG: b""})


def test_key_from() -> None:
    assert backup_mod._key_from({backup_mod.NAME_KEY: b" 00ff \n"}) == b"00ff"
    for unreadable in (b"kein hex", "ü".encode()):
        with pytest.raises(backup_mod.BackupError, match="nicht lesbar"):
            backup_mod._key_from({backup_mod.NAME_KEY: unreadable})


def test_relocate_key(tmp_path: Path) -> None:
    old = tmp_path / "alt" / "k.txt"
    old.parent.mkdir()
    old.write_bytes(b"00ff\n")
    new = tmp_path / "neu" / "k.txt"
    assert backup_mod._relocate_key(old, old, "s") is None  # same place: nothing to do
    assert backup_mod._relocate_key(tmp_path / "fehlt", new, "s") is None
    new.parent.mkdir()
    new.write_bytes(b"ffff\n")
    with pytest.raises(backup_mod.BackupError, match="anderen Schlüssel"):
        backup_mod._relocate_key(old, new, "s")
    new.write_bytes(b"00ff")  # the same key, other whitespace: accepted
    aside = backup_mod._relocate_key(old, new, "s")
    assert aside == tmp_path / "alt" / "k.txt.verlegt-s" and aside.exists()
    assert not old.exists() and new.read_bytes() == b"00ff\n"


def _database_with_patient(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    v = sqlite3.connect(path)
    v.executescript(_SCHEMA)
    v.execute("INSERT INTO patienten (schluessel, kennung, angelegt) VALUES ('s1', 'K1', 't')")
    v.commit()
    v.close()


def _count(path: Path, table: str) -> int:
    v = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(v.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        v.close()


def test_relocate_database(tmp_path: Path) -> None:
    old = tmp_path / "alt" / "p.sqlite"
    _database_with_patient(old)
    assert backup_mod._relocate_database(old, old, "s") is None
    assert backup_mod._relocate_database(tmp_path / "fehlt.sqlite", tmp_path / "x", "s") is None
    full = tmp_path / "voll.sqlite"
    shutil.copy2(old, full)
    with pytest.raises(backup_mod.BackupError, match="schon eine Datenbank"):
        backup_mod._relocate_database(old, full, "s")
    assert old.exists()  # a refusal moves nothing
    new = tmp_path / "neu" / "tief" / "p.sqlite"
    aside = backup_mod._relocate_database(old, new, "s")
    assert aside == old.with_name("p.sqlite.verlegt-s") and aside.exists()
    assert not old.exists() and _count(new, "patienten") == 1


def test_adopt_entries_tolerates_missing_tables(tmp_path: Path) -> None:
    archive = tmp_path / "archiv.sqlite"
    _database_with_patient(archive)
    v = sqlite3.connect(archive)
    v.executescript("DROP TABLE vokabular; DROP TABLE restrisiko_dokumente;")
    v.close()
    local = tmp_path / "lokal.sqlite"
    counter = backup_mod._adopt_entries(local, archive)
    assert counter["patienten"] == 1 and counter["vokabular"] == 0
    assert counter["restrisiko_dokumente"] == 0 and _count(local, "patienten") == 1
    assert backup_mod._adopt_entries(local, archive)["patienten"] == 0  # idempotent
    assert _count(local, "vokabular_meta") == 0  # no words, no version bump
