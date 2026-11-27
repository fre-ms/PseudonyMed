"""Backup and restore of everything re-identification depends on (the statistics of the
store live in :mod:`pseudonymed.pseudonyms.statistics`).

The pseudonym database (``pseudonyme.sqlite``) holds the identities and the mapping
of every pseudonym back to its clear text; the key file (``pseudonymschluessel.txt``)
is the secret the codes are derived from.  Without both, a pseudonymised text cannot
be read in clear again, and with both anyone can.  A backup is therefore a ZIP of the
two (a consistent copy of the database through SQLite's backup API, never a raw copy
of an open file), optionally the configuration and the protected vocabulary (one CSV,
``begriff,kategorie``: the vocabulary lives in lexterm's store, not in the database),
plus a manifest with checksums, and it is protected by a passphrase (OpenPGP symmetric
encryption, readable by ``gpg --decrypt`` as well) unless the user insists on plain.

Restore replaces the database and the key after setting aside the current ones, or
merges an archive made with the *same* key into the local database (same key, same
codes for the same people); merging two different pseudonym spaces is refused.  The
vocabulary follows the mode: restoring makes it exactly what the archive holds, merging
adds what is new.  An archive made before the vocabulary left the database carries it as
a table of that database, which is taken over in the same way.

The identifiers (the bundled lists with what the user deleted from them, the user's own
entries, lists fetched on request) live in lexident's store and travel as a store of their
own that holds these lists and nothing else: a terminology another program keeps in the
same store is no part of a backup.  Restoring makes the store's own lists exactly those of
the archive after copying the current ones aside, and leaves the packages of other
programs alone; merging adds the user's own entries that are new.  An archive made while
the identifiers were tables of the database brings those tables, which are taken over as
on first use.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from lexcor import StoreError

from pseudonymed import TOOL
from pseudonymed.config import config_path
from pseudonymed.core.stored import as_stored, field_values
from pseudonymed.core.text import english, say
from pseudonymed.detection import identifier_lists, identifiers, lexicon
from pseudonymed.detection import vocabulary as vocab_mod
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.statistics import key_id, statistics, store_paths

# 2: the vocabulary travels as a CSV beside the database, no longer inside it
# 3: the identifiers travel as a store of their lists, no longer inside the database either
VERSION = 3
NAME_DB = "pseudonyme.sqlite"
NAME_KEY = "pseudonymschluessel.txt"
NAME_CONFIG = "konfig.toml"
NAME_VOCABULARY = "vokabular.csv"
NAME_IDENTIFIERS = "lexident.sqlite"
NAME_MANIFEST = "manifest.json"
_MARK = "pseudonymed_sicherung"
_FORMER_MARK = "anonymed_sicherung"  # backups written before the project was renamed
MODES = ("ersetzen", "zusammenfuehren")

_TABLES = ("patienten", "aliasse", "zuordnungen", "verworfen",  # merged in this order;
           "restrisiko", "restrisiko_dokumente")  # older archives may lack some


class BackupError(Exception):
    """Anything that stops a backup, restore or relocation; the message is for the user."""


def _timestamp() -> str:
    """The stamp that names files set aside (``*.verlegt-<stamp>``, ``*.vor-import-<stamp>``)."""
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _protect(*paths: Path) -> None:
    """Restrict the files to their owner (0600); file systems without POSIX modes are tolerated."""
    try:
        for p in paths:
            os.chmod(p, 0o600)
    except OSError:
        pass


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy of a SQLite database through the backup API, which reads the
    source inside a transaction; a plain file copy of an open database could be torn."""
    begin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    last = sqlite3.connect(target)
    try:
        begin.backup(last)
    finally:
        last.close()
        begin.close()


def _set_aside(path: Path, stamp: str) -> Path:
    """Rename a file to ``<name>.verlegt-<stamp>`` and return the new path."""
    aside = path.with_name(f"{path.name}.verlegt-{stamp}")
    path.rename(aside)
    return aside


@dataclass
class Relocation:
    """Result of ``relocate``: the new paths, whether files were copied and which were set aside."""
    database: Path
    key: Path
    copied: bool = False
    renamed: list[Path] = field(default_factory=list)

    def description(self, language: str = "de") -> str:
        """One sentence for the user about the new location and what moved."""
        if english(language):
            text = f"Location: database {self.database}, key {self.key}."
            if self.copied:
                text += " Existing files were copied there"
                if self.renamed:
                    text += "; the old ones are now called " + ", ".join(
                        p.name for p in self.renamed)
                text += "."
            return text
        text = f"Speicherort: Datenbank {self.database}, Schlüssel {self.key}."
        if self.copied:
            text += " Vorhandene Dateien wurden dorthin kopiert"
            if self.renamed:
                text += "; die alten heißen jetzt " + ", ".join(p.name for p in self.renamed)
            text += "."
        return text


def relocate(database: Path, key: Path, copy: bool = True) -> Relocation:
    """Store database and key at the given paths from now on (``[speicher]`` in the
    configuration).  With ``copy`` the current files are copied there first (the
    database through the backup API) and the originals renamed ``*.verlegt-<stamp>``,
    so that exactly one database stays live."""
    from pseudonymed.config import load_settings, save_settings

    database, key = Path(database).expanduser(), Path(key).expanduser()
    if not database.is_absolute() or not key.is_absolute():
        raise BackupError(say("Bitte absolute Pfade angeben.", "Please give absolute paths."))
    if database == key:
        raise BackupError(say("Datenbank und Schlüsseldatei brauchen verschiedene Dateinamen.",
                              "Database and key file need different file names."))
    old_db, old_key = store_paths(None)
    v = Relocation(database=database, key=key)
    if copy and (old_db != database or old_key != key):
        v.renamed = _move_store(old_db, old_key, database, key)
        v.copied = bool(v.renamed)
    e = load_settings()
    e.store_database, e.store_key = str(database), str(key)
    save_settings(e)
    return v


def _move_store(old_db: Path, old_key: Path, database: Path, key: Path) -> list[Path]:
    """Copy the live database and key to their new paths and set the old files aside; returns
    the renamed originals.  The database goes first, so a refused target leaves the key put."""
    stamp = _timestamp()
    relocated = [_relocate_database(old_db, database, stamp),
                 _relocate_key(old_key, key, stamp)]
    for p in (database, key):
        if p.exists():
            _protect(p)
    return [p for p in relocated if p]


def _relocate_database(old: Path, new: Path, stamp: str) -> Path | None:
    """Copy the live database to ``new`` through the backup API and rename the old file; None
    when there is nothing to move.  A filled target is never overwritten: that is the import's
    job."""
    if not old.exists() or old == new:
        return None
    if new.exists() and not _is_empty(new):
        raise BackupError(say(f"Am Ziel gibt es schon eine Datenbank: {new}."
                              " Anderen Pfad wählen oder sie über den Import ersetzen.",
                              f"There is a database at the target already: {new}."
                              " Choose another path or replace it through the import."))
    new.parent.mkdir(parents=True, exist_ok=True)
    _copy_database(old, new)
    return _set_aside(old, stamp)


def _relocate_key(old: Path, new: Path, stamp: str) -> Path | None:
    """Copy the key file to ``new`` and rename the old one; None when there is nothing to move.
    A different key at the target is refused, the same key (whitespace aside) is overwritten."""
    if not old.exists() or old == new:
        return None
    if new.exists() and new.read_bytes().strip() != old.read_bytes().strip():
        raise BackupError(say(f"Am Ziel gibt es schon einen anderen Schlüssel: {new}.",
                              f"There is another key at the target already: {new}."))
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, new)
    return _set_aside(old, stamp)


# -- backup ----------------------------------------------------------------------------------------
@dataclass
class Manifest:
    """What ``manifest.json`` records about an archive: who wrote it and when, the counts
    at that moment, the key's public handle and a checksum per file."""

    STORED_KEYS: ClassVar[dict[str, str]] = {
        "created": "erstellt", "tool": "werkzeug", "key_id": "schluessel_kennung",
        "patients": "patienten", "aliases": "aliasse", "mappings": "zuordnungen",
        "discarded": "verworfen", "files": "dateien"}

    version: int = VERSION
    created: str = ""
    tool: str = TOOL
    schema: str = ""
    key_id: str = ""
    patients: int = 0
    aliases: int = 0
    mappings: int = 0
    discarded: int = 0
    files: dict[str, dict[str, int | str]] = field(default_factory=dict)

    @property
    def config(self) -> bool:
        """Whether the configuration file travels in the archive."""
        return NAME_CONFIG in self.files

    @property
    def vocabulary(self) -> bool:
        """Whether the vocabulary travels in the archive."""
        return NAME_VOCABULARY in self.files

    def description(self, language: str = "de") -> str:
        """One line for the user: date, tool, counts, key handle and the optional parts."""
        when = self.created[:16].replace("T", " ")
        if english(language):
            return (f"{when} · {self.tool} · {self.patients} patients, {self.mappings} mappings,"
                    f" {self.discarded} discards · key {self.key_id}"
                    + (" · configuration" if self.config else "")
                    + (" · vocabulary" if self.vocabulary else ""))
        return (f"{when} · {self.tool} · {self.patients}"
                f" Patienten, {self.mappings} Zuordnungen, {self.discarded} Verwerfungen"
                f" · Schlüssel {self.key_id}"
                + (" · Konfiguration" if self.config else "")
                + (" · Vokabular" if self.vocabulary else ""))


def _sha(data: bytes) -> str:
    """SHA-256 of a file's bytes as hex, the checksum the manifest records."""
    return hashlib.sha256(data).hexdigest()


def _db_copy(db: Path) -> bytes:
    """A consistent snapshot of the database through SQLite's backup API, without the
    tables a database may still have from the time it kept the vocabulary and the
    identifiers."""
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / NAME_DB
        _copy_database(db, target)
        vocab_mod.drop_table(target)
        identifier_lists.drop_tables(target)
        return target.read_bytes()


def _identifiers_copy() -> bytes:
    """The lists of the identifiers as a store of their own.  Opening the store first takes
    over what the pseudonym database still holds and installs the bundled lists."""
    try:
        with tempfile.TemporaryDirectory() as folder:
            with identifiers.IdentifierTable() as table:
                table.export_lists(Path(folder) / NAME_IDENTIFIERS)
            return (Path(folder) / NAME_IDENTIFIERS).read_bytes()
    except (identifiers.IdentifierError, StoreError) as error:
        raise _vocabulary_error(error) from error


def _vocabulary_csv() -> bytes:
    """The protected vocabulary as the CSV its own export writes."""
    try:
        with tempfile.TemporaryDirectory() as folder, vocab_mod.Vocabulary() as vocabulary:
            vocabulary.export_csv(Path(folder) / NAME_VOCABULARY)
            return (Path(folder) / NAME_VOCABULARY).read_bytes()
    except (vocab_mod.VocabularyError, StoreError) as error:
        raise _vocabulary_error(error) from error


def _vocabulary_error(error: ValueError) -> BackupError:
    """An error of the vocabulary, of the identifiers or of their stores as an error of the
    backup, with both of its languages."""
    if isinstance(error, StoreError):
        return BackupError(say(error.message("de"), error.message("en")))
    return BackupError(*error.args)


def export(target: Path, store_path: Path | None = None, config: bool = True,
           vocabulary: bool = True, passphrase: str | None = None) -> Path:
    """Write the backup archive; ``target`` is a folder (file name chosen) or a file.
    With a passphrase the ZIP is wrapped in an OpenPGP message (``.zip.gpg``)."""
    db, key = store_paths(store_path)
    if not db.exists():
        raise BackupError(say(f"Keine Datenbank unter {db}.", f"No database at {db}."))
    if not key.exists():
        raise BackupError(say(f"Keine Schlüsseldatei unter {key}; ohne sie lässt sich"
                              " nichts rekonstruieren.",
                              f"No key file at {key}; without it nothing can be reconstructed."))
    st = statistics(db)
    manifest = Manifest(created=dt.datetime.now().isoformat(timespec="seconds"),
                        schema=st.schema, key_id=st.key_id,
                        patients=st.patients, aliases=st.aliases, mappings=st.mappings,
                        discarded=st.discarded)
    contents = _collect_contents(db, key, config, vocabulary)
    manifest.files = {name: {"sha256": _sha(data), "bytes": len(data)}
                      for name, data in contents.items()}
    raw = _pack(manifest, contents)
    target = _archive_path(Path(target), encrypted=bool(passphrase))
    if passphrase:
        raw = pgp_module.encrypt_with_passphrase(raw, passphrase)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    _protect(target)
    return target


def _collect_contents(db: Path, key: Path, config: bool, vocabulary: bool) -> dict[str, bytes]:
    """The archive's files by name: a database snapshot, the key, and on request the
    configuration and the vocabulary, and always the identifiers.  Vocabulary and
    identifiers are read first: opening them takes over what the database still holds, and
    the snapshot is to follow that."""
    words = _vocabulary_csv() if vocabulary else None
    listed = _identifiers_copy()
    contents: dict[str, bytes] = {NAME_DB: _db_copy(db), NAME_KEY: key.read_bytes(),
                                  NAME_IDENTIFIERS: listed}
    if config and config_path().exists():
        contents[NAME_CONFIG] = config_path().read_bytes()
    if words is not None:
        contents[NAME_VOCABULARY] = words
    return contents


def _pack(manifest: Manifest, contents: dict[str, bytes]) -> bytes:
    """The ZIP archive as bytes: the manifest (with the project mark) first, then the files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(NAME_MANIFEST, json.dumps({_MARK: VERSION, **as_stored(manifest)},
                                              ensure_ascii=False, indent=1))
        for name, data in contents.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _archive_path(target: Path, encrypted: bool) -> Path:
    """The file the archive goes to: in a folder (or at a path without suffix) a stamped
    ``pseudonymed_sicherung_<stamp>.zip``; an encrypted one ends in ``.gpg`` for ``gpg``'s sake."""
    if target.is_dir() or not target.suffix:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        target = target / f"pseudonymed_sicherung_{stamp}.zip"
    if encrypted and target.suffix != ".gpg":
        target = target.with_name(target.name + ".gpg")
    return target


def _unpack(archive: Path, passphrase: str | None) -> tuple[Manifest, dict[str, bytes]]:
    """Read an archive and verify it: decrypt when needed, read manifest and files, check
    every checksum and that database and key are present."""
    raw = _as_zip(Path(archive).read_bytes(), passphrase)
    manifest, contents = _read_archive(raw)
    _check_completeness(manifest, contents)
    return manifest, contents


def _as_zip(raw: bytes, passphrase: str | None) -> bytes:
    """The ZIP bytes: the file itself when it is one, else the OpenPGP message decrypted."""
    if raw.startswith(b"PK"):
        return raw
    if not passphrase:
        raise BackupError(say("Das Archiv ist verschlüsselt; Passphrase nötig.",
                              "The archive is encrypted; a passphrase is needed."))
    try:
        decrypted = pgp_module.decrypt_with_passphrase(raw, passphrase)
    except pgp_module.PGPError as e:
        raise BackupError(*e.args) from e  # keeps the English form
    if not decrypted.startswith(b"PK"):
        raise BackupError(say("Entschlüsselt, aber kein ZIP-Archiv.",
                              "Decrypted, but not a ZIP archive."))
    return decrypted


def _read_archive(raw: bytes) -> tuple[Manifest, dict[str, bytes]]:
    """Manifest and files of a ZIP archive; a torn ZIP is reported as damaged."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
            if NAME_MANIFEST not in names:
                raise BackupError(say("Kein PseudonyMed-Archiv (manifest.json fehlt).",
                                      "Not a PseudonyMed archive (manifest.json is missing)."))
            manifest = _manifest_from(z.read(NAME_MANIFEST))
            contents = {n: z.read(n) for n in names if n != NAME_MANIFEST}
    except zipfile.BadZipFile as e:
        raise BackupError(say("Beschädigtes Archiv.", "Damaged archive.")) from e
    return manifest, contents


def _manifest_from(raw: bytes) -> Manifest:
    """The manifest from its JSON: it must carry the project mark (the one of the earlier
    name is still accepted) and must not come from a newer PseudonyMed."""
    m = json.loads(raw.decode("utf-8"))
    marks = [m.pop(k, None) for k in (_MARK, _FORMER_MARK)]
    if all(x is None for x in marks):
        raise BackupError(say("Kein PseudonyMed-Archiv.", "Not a PseudonyMed archive."))
    if int(m.get("version", 0)) > VERSION:
        raise BackupError(say("Das Archiv stammt von einem neueren PseudonyMed.",
                              "The archive comes from a newer PseudonyMed."))
    return Manifest(**field_values(Manifest, m))


def _check_completeness(manifest: Manifest, contents: dict[str, bytes]) -> None:
    """Every listed file must be present with its checksum, and database and key among them."""
    for name, info in manifest.files.items():
        if name not in contents:
            raise BackupError(say(f"Datei {name} fehlt im Archiv.",
                                  f"File {name} is missing from the archive."))
        if _sha(contents[name]) != info.get("sha256"):
            raise BackupError(say(f"Prüfsumme von {name} stimmt nicht; Archiv beschädigt.",
                                  f"The checksum of {name} does not match; the archive is"
                                  " damaged."))
    if NAME_DB not in contents or NAME_KEY not in contents:
        raise BackupError(say("Archiv ohne Datenbank oder Schlüssel.",
                              "Archive without database or key."))


def check(archive: Path, passphrase: str | None = None) -> Manifest:
    """Read and verify an archive without changing anything."""
    return _unpack(archive, passphrase)[0]


@dataclass
class ImportReport:
    """What a restore or merge changed, for the message to the user."""
    mode: str
    prior_backups: list[Path] = field(default_factory=list)
    new_patients: int = 0
    new_aliases: int = 0
    new_mappings: int = 0
    new_discarded: int = 0
    key_replaced: bool = False
    config_replaced: bool = False
    vocabulary_terms: int = 0  # restoring: the terms of the vocabulary the archive brought
    new_vocabulary: int = 0  # merging: the terms that were new
    identifiers: int = 0  # restoring: the identifiers the archive brought
    new_identifiers: int = 0  # merging: the user's own identifiers that were new

    def description(self, language: str = "de") -> str:
        """One sentence per mode: what was restored, or how many entries were new."""
        if english(language):
            return self._description_en()
        if self.mode == "ersetzen":
            return (f"Wiederhergestellt: {self.new_patients} Patienten, {self.new_mappings}"
                    f" Zuordnungen, {self.new_discarded} Verwerfungen; Schlüssel"
                    f" {'ersetzt' if self.key_replaced else 'unverändert'}"
                    + (", Konfiguration ersetzt" if self.config_replaced else "")
                    + (f", Vokabular mit {self.vocabulary_terms} Begriffen"
                       if self.vocabulary_terms else "")
                    + (f", {self.identifiers} Identifikatoren" if self.identifiers else "")
                    + (". Vorherige Fassung gesichert: "
                       + ", ".join(p.name for p in self.prior_backups)
                       if self.prior_backups else "."))
        return (f"Zusammengeführt: {self.new_patients} neue Patienten, {self.new_aliases} neue"
                f" Schreibweisen, {self.new_mappings} neue Zuordnungen, {self.new_discarded}"
                f" neue Verwerfungen, {self.new_vocabulary} neue Vokabeln,"
                f" {self.new_identifiers} neue eigene Identifikatoren.")

    def _description_en(self) -> str:
        """The English sentence of :meth:`description`."""
        words = (f", vocabulary of {self.vocabulary_terms} terms" if self.vocabulary_terms
                 else "") + (f", {self.identifiers} identifiers" if self.identifiers else "")
        if self.mode == "ersetzen":
            return (f"Restored: {self.new_patients} patients, {self.new_mappings} mappings,"
                    f" {self.new_discarded} discards; key"
                    f" {'replaced' if self.key_replaced else 'unchanged'}"
                    + (", configuration replaced" if self.config_replaced else "") + words
                    + (". Previous version kept: "
                       + ", ".join(p.name for p in self.prior_backups)
                       if self.prior_backups else "."))
        return (f"Merged: {self.new_patients} new patients, {self.new_aliases} new spellings,"
                f" {self.new_mappings} new mappings, {self.new_discarded} new discards,"
                f" {self.new_vocabulary} new vocabulary terms,"
                f" {self.new_identifiers} new identifiers of your own.")


def _safety_copy(path: Path, stamp: str) -> Path | None:
    """Copy a file to ``<name>.vor-import-<stamp>`` before the import touches it, or None."""
    if not path.exists():
        return None
    copy = path.with_name(f"{path.name}.vor-import-{stamp}")
    shutil.copy2(path, copy)
    _protect(copy)
    return copy


@dataclass(frozen=True)
class _ImportContext:
    """The local files an import works on and the two keys it compares: the one from the
    archive and the one on disk (None without one), plus the stamp naming the safety copies."""
    db: Path
    key: Path
    archive_db: Path
    new_key: bytes
    local_key: bytes | None
    stamp: str

    def write_key(self) -> bool:
        """Put the archive's key in place; True when it differs from the one there before."""
        self.key.write_bytes(self.new_key + b"\n")
        return self.local_key != self.new_key


def import_file(archive: Path, store_path: Path | None = None, passphrase: str | None = None,
                mode: str = "ersetzen", config: bool = True, vocabulary: bool = True,
                ) -> ImportReport:
    """Restore (``ersetzen``) or merge (``zusammenfuehren``) an archive."""
    if mode not in MODES:
        raise BackupError(say(f"Modus muss einer von {', '.join(MODES)} sein.",
                              f"The mode must be one of {', '.join(MODES)}."))
    manifest, contents = _unpack(archive, passphrase)
    db, key = store_paths(store_path)
    new_key = _key_from(contents)
    local_key = key.read_bytes().strip() if key.exists() else None
    report = ImportReport(mode=mode)
    stamp = _timestamp()
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        archive_db = Path(folder) / NAME_DB
        archive_db.write_bytes(contents[NAME_DB])
        e = _ImportContext(db, key, archive_db, new_key, local_key, stamp)
        if mode == "ersetzen":
            _restore(e, manifest, report, contents.get(NAME_CONFIG) if config else None)
        else:
            _merge(e, report)
        _protect(key, db)
        _take_vocabulary(e, contents.get(NAME_VOCABULARY), vocabulary, report)
        _take_identifiers(e, contents.get(NAME_IDENTIFIERS), report)
    return report


def _key_from(contents: dict[str, bytes]) -> bytes:
    """The archived key, stripped; it must be hex digits like the key file."""
    key = contents[NAME_KEY].strip()
    try:
        bytes.fromhex(key.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BackupError(say("Der Schlüssel im Archiv ist nicht lesbar.",
                              "The key in the archive is not readable.")) from e
    return key


def _restore(e: _ImportContext, manifest: Manifest, report: ImportReport,
             new_config: bytes | None) -> None:
    """Restore: set the current database, key and configuration aside, then put the
    archive's copies in their place; the counts are the manifest's."""
    report.prior_backups = [p for p in (_safety_copy(e.db, e.stamp),
                                        _safety_copy(e.key, e.stamp)) if p]
    _copy_database(e.archive_db, e.db)
    report.key_replaced = e.write_key()
    report.new_patients, report.new_mappings, report.new_discarded = (
        manifest.patients, manifest.mappings, manifest.discarded)
    if new_config is not None:
        _safety_copy(config_path(), e.stamp)
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_bytes(new_config)
        report.config_replaced = True


def _merge(e: _ImportContext, report: ImportReport) -> None:
    """Merge: refuse a foreign key unless the local store is empty, set the database
    aside, adopt the archive's key where there was none, then copy the entries over."""
    local_empty = not e.db.exists() or _is_empty(e.db)
    _require_same_key(e, local_empty)
    report.prior_backups = [p for p in (_safety_copy(e.db, e.stamp),) if p]
    if e.local_key is None or local_empty:
        _safety_copy(e.key, e.stamp)
        report.key_replaced = e.write_key()
    counter = _adopt_entries(e.db, e.archive_db)
    report.new_patients, report.new_aliases = counter["patienten"], counter["aliasse"]
    report.new_mappings, report.new_discarded = (counter["zuordnungen"], counter["verworfen"])


def _require_same_key(e: _ImportContext, local_empty: bool) -> None:
    """Two pseudonym spaces are never mixed: the same person would carry two codes.  A
    missing local key or an empty local database is fine, the archive's key takes over."""
    if e.local_key is None or e.local_key == e.new_key or local_empty:
        return
    k_archive = key_id(bytes.fromhex(e.new_key.decode()))
    k_local = key_id(bytes.fromhex(e.local_key.decode()))
    raise BackupError(say(
        "Zusammenführen geht nur bei gleichem Pseudonymschlüssel: das Archiv hat"
        f" den Schlüssel {k_archive}, die lokale Datenbank {k_local}. Zwei"
        " Pseudonymräume lassen sich nicht mischen; stattdessen Ersetzen.",
        f"Merging only works with the same pseudonym key: the archive has the key {k_archive},"
        f" the local database {k_local}. Two pseudonym spaces cannot be mixed; replace"
        " instead."))


def _adopt_entries(db: Path, archive_db: Path) -> dict[str, int]:
    """Copy the archive's rows into the local database table by table (INSERT OR IGNORE) and
    count the newcomers."""
    target = sqlite3.connect(db)
    try:
        from pseudonymed.pseudonyms.store import _SCHEMA

        target.executescript(_SCHEMA)
        target.execute("ATTACH DATABASE ? AS archiv", (str(archive_db),))
        counter = {table: _adopt_table(target, table) for table in _TABLES}
        target.commit()
        target.execute("DETACH DATABASE archiv")
    finally:
        target.close()
    return counter


def _adopt_table(target: sqlite3.Connection, table: str) -> int:
    """INSERT OR IGNORE the attached archive's rows of one table; returns how many were
    new.  An archive written before the table existed contributes nothing."""
    before = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    try:
        target.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM archiv.{table}")
    except sqlite3.OperationalError:
        pass
    after = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return after - before


def _take_vocabulary(e: _ImportContext, words: bytes | None, take: bool,
                     report: ImportReport) -> None:
    """The archive's vocabulary: its CSV, or for an archive made before the vocabulary left
    the database the table in its database.  Restoring makes the vocabulary exactly that,
    merging adds what is new; without ``take`` the vocabulary stays as it is.  A restored
    database never keeps the table."""
    file = e.archive_db.with_name(NAME_VOCABULARY)
    if words is not None:
        file.write_bytes(words)
    try:
        if report.mode == "ersetzen":
            _restore_vocabulary(e, file, take, report)
        elif take:
            _merge_vocabulary(e, file, report)
    except (vocab_mod.VocabularyError, StoreError) as error:
        raise _vocabulary_error(error) from error
    _forget_vocabulary()


def _restore_vocabulary(e: _ImportContext, file: Path, take: bool,
                        report: ImportReport) -> None:
    """Make the vocabulary exactly what the archive holds (when it is to be taken), then
    drop the table an archive of the former kind restored with the database.  Opening the
    vocabulary takes over what the default database still holds: the restored database is
    that one, or is taken over after it."""
    if take and file.exists():
        with vocab_mod.Vocabulary() as vocabulary:
            report.vocabulary_terms = vocabulary.replace_from_csv(file)
    elif take:
        held = vocab_mod.table_terms(e.db) or {}
        with vocab_mod.Vocabulary() as vocabulary:
            vocabulary.adopt(e.db)
        report.vocabulary_terms = sum(len(terms) for terms in held.values())
    vocab_mod.drop_table(e.db)


def _merge_vocabulary(e: _ImportContext, file: Path, report: ImportReport) -> None:
    """Add the terms of the archive that the vocabulary lacks."""
    with vocab_mod.Vocabulary() as vocabulary:
        if file.exists():
            report.new_vocabulary = vocabulary.import_csv(file)[0]
            return
        groups = vocab_mod.table_terms(e.archive_db) or {}
        report.new_vocabulary = sum(vocabulary.add(terms, category)
                                    for category, terms in groups.items())


def _take_identifiers(e: _ImportContext, listed: bytes | None, report: ImportReport) -> None:
    """The archive's identifiers: the snapshot of their store, or for an archive made while
    they were tables of the database those tables.  Restoring makes the store what the
    archive holds, merging adds the user's own entries that are new.  A restored database
    never keeps the tables."""
    snapshot = e.archive_db.with_name(NAME_IDENTIFIERS)
    if listed is not None:
        snapshot.write_bytes(listed)
    try:
        if report.mode == "ersetzen":
            _restore_identifiers(e, snapshot, report)
        else:
            _merge_identifiers(e, snapshot, report)
    except (identifiers.IdentifierError, StoreError) as error:
        raise _vocabulary_error(error) from error


def _restore_identifiers(e: _ImportContext, snapshot: Path, report: ImportReport) -> None:
    """Make the identifiers what the archive holds, after copying the current lists aside:
    the lists of its store take the place of the store's own, or the tables an archive of
    the former kind restored with the database are taken over.  Either way the restored
    database keeps no such tables, the bundled lists are brought up to date afterwards, and
    packages of other programs in the store stay as they are."""
    from pseudonymed.pseudonyms.store import default_path

    if e.db.resolve() != default_path().resolve() \
            and identifier_lists.former_tables(default_path()) is not None:
        # another database is restored while the default one still waits to hand its
        # identifiers over: they move first, so that the copy set aside holds them
        identifiers.IdentifierTable().close()
    report.prior_backups += [p for p in (_identifiers_aside(e.stamp),) if p]
    with identifiers.IdentifierTable(seed=False) as table:
        if snapshot.exists():
            table.take_lists(snapshot)
        table.adopt(e.db)
        report.identifiers = table.count()
        table.seed()
    identifier_lists.drop_tables(e.db)


def _identifiers_aside(stamp: str) -> Path | None:
    """Copy the store's own lists of identifiers into ``lexident.sqlite.vor-import-<stamp>``
    before a restore replaces them; None when there are none."""
    target = identifiers.default_path()
    if not target.exists():
        return None
    copy = target.with_name(f"{target.name}.vor-import-{stamp}")
    with identifiers.IdentifierTable(seed=False) as table:
        if not table.export_lists(copy):
            copy.unlink()
            return None
    _protect(copy)
    return copy


def _merge_identifiers(e: _ImportContext, snapshot: Path, report: ImportReport) -> None:
    """Add the user's own identifiers of the archive that the store lacks, each with the
    source's identifier and what the source says of it when it has them."""
    if snapshot.exists():
        with identifiers.IdentifierTable(snapshot, readonly=True) as archived:
            own = archived.own_rows()
    else:
        former = identifier_lists.former_tables(e.archive_db)
        own = list(former.rows.get(lexicon.USER, [])) if former else []
    with identifiers.IdentifierTable() as table:
        report.new_identifiers = table.add_rows(own)


def _forget_vocabulary() -> None:
    """Drop the cached vocabulary matcher so the next analysis sees the imported words."""
    from pseudonymed.detection.medical_terms import standard

    standard.cache_clear()


def _is_empty(db: Path) -> bool:
    """True when the database holds neither patients nor mappings (or none of the tables yet)."""
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table in ("patienten", "zuordnungen"):
            try:
                if v.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    return False
            except sqlite3.DatabaseError:
                continue
        return True
    finally:
        v.close()
