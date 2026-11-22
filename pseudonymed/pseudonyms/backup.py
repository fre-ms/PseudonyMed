"""Backup, restore and statistics of everything re-identification depends on.

The pseudonym database (``pseudonyme.sqlite``) holds the identities and the mapping
of every pseudonym back to its clear text; the key file (``pseudonymschluessel.txt``)
is the secret the codes are derived from.  Without both, a pseudonymised text cannot
be read in clear again, and with both anyone can.  A backup is therefore a ZIP of the
two (a consistent copy of the database through SQLite's backup API, never a raw copy
of an open file), optionally the configuration and the user's vocabulary lists, plus
a manifest with checksums, and it is protected by a passphrase (OpenPGP symmetric
encryption, readable by ``gpg --decrypt`` as well) unless the user insists on plain.

Restore replaces the database and the key after setting aside the current ones, or
merges an archive made with the *same* key into the local database (same key, same
codes for the same people); merging two different pseudonym spaces is refused.
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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pseudonymed import TOOL
from pseudonymed.config import config_path
from pseudonymed.core.stored import as_stored, field_values
from pseudonymed.detection.medical_terms import data_folder as vocabulary_folder
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.store import default_key_path, default_path, key_path_for

VERSION = 1
NAME_DB = "pseudonyme.sqlite"
NAME_KEY = "pseudonymschluessel.txt"
NAME_CONFIG = "konfig.toml"
VOCABULARY_FOLDER = "fachbegriffe"
NAME_MANIFEST = "manifest.json"
_MARK = "pseudonymed_sicherung"
_FORMER_MARK = "anonymed_sicherung"  # backups written before the project was renamed
MODES = ("ersetzen", "zusammenfuehren")

CATEGORIES = [  # pseudonym prefix -> label (first match wins)
    ("Patientin ", "Patient"), ("Patient ", "Patient"), ("Angehörige", "Angehörige"),
    ("Dr. Z", "Zuweiser"), ("[geb. ", "Geburtsdatum"), ("[Alter ", "Geburtsdatum"),
    ("[Straße ", "Straße"), ("[PLZ Ort ", "PLZ und Ort"), ("[Ort ", "Ort"),
    ("[Wohnort]", "Ort"), ("Person ", "Person (NER)"), ("[Telefon", "Telefon"),
    ("[E-Mail", "E-Mail"), ("[Versicherten", "Versichertennummer"), ("[Pat", "Patienten-ID"),
    ("[Aktenzeichen", "Aktenzeichen"), ("[IBAN", "IBAN"), ("[Code]", "Barcode/QR"),
    ("[Beruf", "Beruf"), ("[Klinik", "Klinik"), ("[Studie", "Studie"), ("[Titel", "Titel"),
    ("[Identifikator", "Identifikator"), ("[Medikament", "Medikament"),
    ("[Dosierung", "Dosierung"), ("[Laborwert", "Laborwert"), ("[Messwert", "Messwert"),
]
_KIND_LABELS = {
    "name": "Name", "vorname": "Name", "nachname": "Name", "geburtsname": "Geburtsname",
    "geburtsdatum": "Geburtsdatum", "adresse": "Adresse", "ort": "Ort", "telefon": "Telefon",
    "email": "E-Mail", "versichertennummer": "Versichertennummer",
    "patienten_id": "Patienten-ID", "aktenzeichen": "Aktenzeichen", "iban": "IBAN",
    "begriff": "Begriff", "manuell": "Manuell", "identifikator": "Identifikator",
    "medizin": "Medizinischer Inhalt",
}
_TABLES = ("patienten", "aliasse", "zuordnungen", "verworfen", "vokabular",  # merged in
           "restrisiko", "restrisiko_dokumente")  # this order; older archives may lack some


class BackupError(Exception):
    """Anything that stops a backup, restore or relocation; the message is for the user."""


def category(pseudonym: str, kind: str = "") -> str:
    """Human label for the statistics: by the pseudonym's shape, else by its kind."""
    for begin, label in CATEGORIES:
        if pseudonym.startswith(begin):
            return label
    return _KIND_LABELS.get(kind.lower(), kind or "Sonstige")


def key_id(secret: bytes) -> str:
    """A public handle for a secret key: 16 hex digits of its SHA-256, never the key."""
    return hashlib.sha256(secret).hexdigest()[:16]


# -- statistics ------------------------------------------------------------------------------------
@dataclass
class Statistics:
    """What ``datenbank status`` and the database page show about the local pseudonym store."""
    path: Path
    exists: bool = False
    size: int = 0
    schema: str = ""
    key_path: Path | None = None
    key_present: bool = False
    key_id: str = ""
    patients: int = 0
    aliases: int = 0
    mappings: int = 0
    discarded: int = 0
    per_category: list[tuple[str, int]] = field(default_factory=list)
    discarded_per_rule: list[tuple[str, int]] = field(default_factory=list)
    first_entry: str = ""
    last_entry: str = ""
    integrity: str = ""
    pages: int = 0
    free_pages: int = 0
    vocabulary_files: int = 0
    vocabulary_terms: int = 0
    vocabulary_categories: list[tuple[str, int]] = field(default_factory=list)
    lexicon_categories: list[tuple[str, str, int]] = field(default_factory=list)
    lexicon_extra: list[tuple[str, int, str]] = field(default_factory=list)
    config_present: bool = False

    def lines(self) -> list[tuple[str, str]]:
        """Label/value pairs for display (CLI and desktop)."""
        return (self._header_lines()
                + _indented(self.per_category)
                + [("Gemerkte Verwerfungen", str(self.discarded))]
                + _indented(self.discarded_per_rule)
                + self._status_lines()
                + _indented(self.vocabulary_categories)
                + self._lexicon_lines()
                + [("Konfiguration", "vorhanden" if self.config_present else "keine")])

    def _header_lines(self) -> list[tuple[str, str]]:
        """Where database and key live, and the headline counts."""
        return [
            ("Datenbank", str(self.path)),
            ("Vorhanden", "ja" if self.exists else "nein (noch nicht angelegt)"),
            ("Größe", _mb(self.size)),
            ("Schema", self.schema or "–"),
            ("Schlüsseldatei", str(self.key_path or "–")),
            ("Schlüssel vorhanden", "ja" if self.key_present else "nein"),
            ("Schlüssel-Kennung", self.key_id or "–"),
            ("Patienten", str(self.patients)),
            ("Verknüpfte Schreibweisen", str(self.aliases)),
            ("Zuordnungen gesamt", str(self.mappings)),
        ]

    def _status_lines(self) -> list[tuple[str, str]]:
        """Time span of the entries, integrity, page usage and the vocabulary total."""
        return [
            ("Erster Eintrag", self.first_entry or "–"),
            ("Letzter Eintrag", self.last_entry or "–"),
            ("Integrität", self.integrity or "–"),
            ("Seiten / frei", f"{self.pages} / {self.free_pages}"),
            ("Vokabular (Begriffe)", str(self.vocabulary_terms)),
        ]

    def _lexicon_lines(self) -> list[tuple[str, str]]:
        """The bundled lexicon per class (only when it is loaded) and the data sets the
        user fetched themselves."""
        out: list[tuple[str, str]] = []
        if self.lexicon_categories:
            out.append(("Lexikon (Einträge)", str(sum(n for _k, _r, n in self.lexicon_categories))))
            out += [(f"    {k} ({'Identifikator' if r == 'merkmal' else 'Fachklasse'})", str(n))
                    for k, r, n in self.lexicon_categories]
        out += [("Zusatzdaten (selbst geladen)", f"{q}: {n} Einträge, {g[:10]}")
                for q, n, g in self.lexicon_extra]
        return out


def _indented(pairs: Iterable[tuple[str, int]]) -> list[tuple[str, str]]:
    """Sub-rows of a count, indented under their total."""
    return [(f"    {k}", str(n)) for k, n in pairs]


def _mb(n: int) -> str:
    """A byte count as B, KB or MB with one decimal."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _paths(store_path: Path | None) -> tuple[Path, Path]:
    """Database and key file: the configured ones for the default database, the key
    next to the database when a database path was given explicitly."""
    if store_path:
        db = Path(store_path)
        return db, key_path_for(db)
    return default_path(), default_key_path()


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

    def description(self) -> str:
        """One sentence for the user about the new location and what moved."""
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
        raise BackupError("Bitte absolute Pfade angeben.")
    if database == key:
        raise BackupError("Datenbank und Schlüsseldatei brauchen verschiedene Dateinamen.")
    old_db, old_key = _paths(None)
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
        raise BackupError(f"Am Ziel gibt es schon eine Datenbank: {new}."
                          " Anderen Pfad wählen oder sie über den Import ersetzen.")
    new.parent.mkdir(parents=True, exist_ok=True)
    _copy_database(old, new)
    return _set_aside(old, stamp)


def _relocate_key(old: Path, new: Path, stamp: str) -> Path | None:
    """Copy the key file to ``new`` and rename the old one; None when there is nothing to move.
    A different key at the target is refused, the same key (whitespace aside) is overwritten."""
    if not old.exists() or old == new:
        return None
    if new.exists() and new.read_bytes().strip() != old.read_bytes().strip():
        raise BackupError(f"Am Ziel gibt es schon einen anderen Schlüssel: {new}.")
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, new)
    return _set_aside(old, stamp)


def statistics(store_path: Path | None = None) -> Statistics:
    """Describe the pseudonym store at ``store_path`` (default: the configured one)
    without changing it; a database that cannot be read reports the error as integrity."""
    db, key = _paths(store_path)
    st = Statistics(path=db, key_path=key, exists=db.exists(),
                    config_present=config_path().exists())
    if key.exists():
        st.key_present = True
        st.key_id = _key_file_code(key)
    st.vocabulary_files = len(_vocabulary_files(vocabulary_folder()))
    if not db.exists():
        return st
    st.size = db.stat().st_size
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        _count_contents(v, st)
    except sqlite3.DatabaseError as e:
        st.integrity = f"Fehler: {e}"
    finally:
        v.close()
    return st


def _key_file_code(key: Path) -> str:
    """The public handle of the key in a key file, or ``unreadable`` when it is not hex digits."""
    try:
        return key_id(bytes.fromhex(key.read_text(encoding="utf-8").strip()))
    except ValueError:
        return "unlesbar"


def _vocabulary_files(folder: Path) -> list[Path]:
    """Vocabulary list files (``.txt``, ``.gz``) of a folder, sorted; none without the folder."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix in (".txt", ".gz"))


def _one(v: sqlite3.Connection, sql: str, default: Any = 0) -> Any:
    """One value; a table that does not exist yet counts as empty."""
    try:
        return v.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return default


def _all_rows(v: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    """All rows; a table that does not exist yet has none."""
    try:
        return list(v.execute(sql))
    except sqlite3.OperationalError:
        return []


def _count_contents(v: sqlite3.Connection, st: Statistics) -> None:
    """Fill counts, categories, time span, integrity and lexicon figures from an open connection."""
    st.patients = _one(v, "SELECT COUNT(*) FROM patienten")
    st.aliases = _one(v, "SELECT COUNT(*) FROM aliasse")
    st.mappings = _one(v, "SELECT COUNT(*) FROM zuordnungen")
    st.discarded = _one(v, "SELECT COUNT(*) FROM verworfen")
    st.schema = ", ".join(sorted(r[0] for r in _all_rows(v,
                                                         "SELECT DISTINCT schema FROM patienten")))
    st.per_category = _per_category(v)
    st.discarded_per_rule = [(r, n) for r, n in _all_rows(
        v, "SELECT regel, COUNT(*) FROM verworfen GROUP BY regel ORDER BY COUNT(*) DESC, regel")]
    st.first_entry, st.last_entry = _time_span(v)
    st.integrity = str(_one(v, "PRAGMA integrity_check", "?"))
    st.pages = _one(v, "PRAGMA page_count")
    st.free_pages = _one(v, "PRAGMA freelist_count")
    st.vocabulary_terms = _one(v, "SELECT COUNT(*) FROM vokabular")
    st.vocabulary_categories = [(k, n) for k, n in _all_rows(
        v, "SELECT kategorie, COUNT(*) FROM vokabular GROUP BY kategorie"
        " ORDER BY COUNT(*) DESC, kategorie")]
    from pseudonymed.detection.extra_data import loaded_sources
    from pseudonymed.detection.lexicon import per_class

    st.lexicon_categories = per_class(v)
    st.lexicon_extra = loaded_sources(v)


def _per_category(v: sqlite3.Connection) -> list[tuple[str, int]]:
    """Mappings per human category, the most frequent first, ties by name."""
    counter = Counter(category(pseudonym, kind or "")
                      for pseudonym, kind in _all_rows(v, "SELECT pseudonym, art FROM zuordnungen"))
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _time_span(v: sqlite3.Connection) -> tuple[str, str]:
    """First and last entry as ``YYYY-MM-DD HH:MM:SS``; two empty strings for an empty store."""
    times = [t for t in (_one(v, "SELECT MIN(angelegt) FROM patienten", None),
                         _one(v, "SELECT MAX(geaendert) FROM zuordnungen", None),
                         _one(v, "SELECT MAX(angelegt) FROM patienten", None)) if t]
    if not times:
        return "", ""
    return min(times)[:19].replace("T", " "), max(times)[:19].replace("T", " ")


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
    def vocabulary(self) -> int:
        """Number of vocabulary list files in the archive."""
        return sum(1 for n in self.files if n.startswith(VOCABULARY_FOLDER + "/"))

    def description(self) -> str:
        """One line for the user: date, tool, counts, key handle and the optional parts."""
        return (f"{self.created[:16].replace('T', ' ')} · {self.tool} · {self.patients}"
                f" Patienten, {self.mappings} Zuordnungen, {self.discarded} Verwerfungen"
                f" · Schlüssel {self.key_id}"
                + (" · Konfiguration" if self.config else "")
                + (f" · {self.vocabulary} Vokabulardatei(en)" if self.vocabulary else ""))


def _sha(data: bytes) -> str:
    """SHA-256 of a file's bytes as hex, the checksum the manifest records."""
    return hashlib.sha256(data).hexdigest()


def _db_copy(db: Path) -> bytes:
    """A consistent snapshot of the database through SQLite's backup API."""
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / NAME_DB
        _copy_database(db, target)
        return target.read_bytes()


def export(target: Path, store_path: Path | None = None, config: bool = True,
           vocabulary: bool = True, passphrase: str | None = None) -> Path:
    """Write the backup archive; ``target`` is a folder (file name chosen) or a file.
    With a passphrase the ZIP is wrapped in an OpenPGP message (``.zip.gpg``)."""
    db, key = _paths(store_path)
    if not db.exists():
        raise BackupError(f"Keine Datenbank unter {db}.")
    if not key.exists():
        raise BackupError(f"Keine Schlüsseldatei unter {key}; ohne sie lässt sich"
                          " nichts rekonstruieren.")
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
    configuration and the vocabulary lists."""
    contents: dict[str, bytes] = {NAME_DB: _db_copy(db), NAME_KEY: key.read_bytes()}
    if config and config_path().exists():
        contents[NAME_CONFIG] = config_path().read_bytes()
    if vocabulary:
        for p in _vocabulary_files(vocabulary_folder()):
            contents[f"{VOCABULARY_FOLDER}/{p.name}"] = p.read_bytes()
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
        raise BackupError("Das Archiv ist verschlüsselt; Passphrase nötig.")
    try:
        decrypted = pgp_module.decrypt_with_passphrase(raw, passphrase)
    except pgp_module.PGPError as e:
        raise BackupError(str(e)) from e
    if not decrypted.startswith(b"PK"):
        raise BackupError("Entschlüsselt, aber kein ZIP-Archiv.")
    return decrypted


def _read_archive(raw: bytes) -> tuple[Manifest, dict[str, bytes]]:
    """Manifest and files of a ZIP archive; a torn ZIP is reported as damaged."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
            if NAME_MANIFEST not in names:
                raise BackupError("Kein PseudonyMed-Archiv (manifest.json fehlt).")
            manifest = _manifest_from(z.read(NAME_MANIFEST))
            contents = {n: z.read(n) for n in names if n != NAME_MANIFEST}
    except zipfile.BadZipFile as e:
        raise BackupError("Beschädigtes Archiv.") from e
    return manifest, contents


def _manifest_from(raw: bytes) -> Manifest:
    """The manifest from its JSON: it must carry the project mark (the one of the earlier
    name is still accepted) and must not come from a newer PseudonyMed."""
    m = json.loads(raw.decode("utf-8"))
    marks = [m.pop(k, None) for k in (_MARK, _FORMER_MARK)]
    if all(x is None for x in marks):
        raise BackupError("Kein PseudonyMed-Archiv.")
    if int(m.get("version", 0)) > VERSION:
        raise BackupError("Das Archiv stammt von einem neueren PseudonyMed.")
    return Manifest(**field_values(Manifest, m))


def _check_completeness(manifest: Manifest, contents: dict[str, bytes]) -> None:
    """Every listed file must be present with its checksum, and database and key among them."""
    for name, info in manifest.files.items():
        if name not in contents:
            raise BackupError(f"Datei {name} fehlt im Archiv.")
        if _sha(contents[name]) != info.get("sha256"):
            raise BackupError(f"Prüfsumme von {name} stimmt nicht; Archiv beschädigt.")
    if NAME_DB not in contents or NAME_KEY not in contents:
        raise BackupError("Archiv ohne Datenbank oder Schlüssel.")


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
    vocabulary_files: int = 0

    new_vocabulary: int = 0

    def description(self) -> str:
        """One sentence per mode: what was restored, or how many entries were new."""
        if self.mode == "ersetzen":
            return (f"Wiederhergestellt: {self.new_patients} Patienten, {self.new_mappings}"
                    f" Zuordnungen, {self.new_discarded} Verwerfungen; Schlüssel"
                    f" {'ersetzt' if self.key_replaced else 'unverändert'}"
                    + (", Konfiguration ersetzt" if self.config_replaced else "")
                    + (f", {self.vocabulary_files} Vokabulardatei(en)" if self.vocabulary_files
                       else "")
                    + (". Vorherige Fassung gesichert: "
                       + ", ".join(p.name for p in self.prior_backups)
                       if self.prior_backups else "."))
        return (f"Zusammengeführt: {self.new_patients} neue Patienten, {self.new_aliases} neue"
                f" Schreibweisen, {self.new_mappings} neue Zuordnungen, {self.new_discarded}"
                f" neue Verwerfungen, {self.new_vocabulary} neue Vokabeln"
                + (f", {self.vocabulary_files} neue Vokabulardatei(en)" if self.vocabulary_files
                   else "") + ".")


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
        raise BackupError(f"Modus muss einer von {', '.join(MODES)} sein.")
    manifest, contents = _unpack(archive, passphrase)
    db, key = _paths(store_path)
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
        if vocabulary:
            _adopt_vocabulary_files(contents, mode, report)
    return report


def _key_from(contents: dict[str, bytes]) -> bytes:
    """The archived key, stripped; it must be hex digits like the key file."""
    key = contents[NAME_KEY].strip()
    try:
        bytes.fromhex(key.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BackupError("Der Schlüssel im Archiv ist nicht lesbar.") from e
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
    report.new_vocabulary = counter.get("vokabular", 0)
    if report.new_vocabulary:
        _forget_vocabulary()


def _require_same_key(e: _ImportContext, local_empty: bool) -> None:
    """Two pseudonym spaces are never mixed: the same person would carry two codes.  A
    missing local key or an empty local database is fine, the archive's key takes over."""
    if e.local_key is None or e.local_key == e.new_key or local_empty:
        return
    k_archive = key_id(bytes.fromhex(e.new_key.decode()))
    k_local = key_id(bytes.fromhex(e.local_key.decode()))
    raise BackupError(
        "Zusammenführen geht nur bei gleichem Pseudonymschlüssel: das Archiv hat"
        f" den Schlüssel {k_archive}, die lokale Datenbank {k_local}. Zwei"
        " Pseudonymräume lassen sich nicht mischen; stattdessen Ersetzen.")


def _adopt_entries(db: Path, archive_db: Path) -> dict[str, int]:
    """Copy the archive's rows into the local database table by table (INSERT OR IGNORE) and
    count the newcomers; new words bump the vocabulary version so cached matchers notice."""
    target = sqlite3.connect(db)
    try:
        from pseudonymed.pseudonyms.store import _SCHEMA

        target.executescript(_SCHEMA)
        target.execute("ATTACH DATABASE ? AS archiv", (str(archive_db),))
        counter = {table: _adopt_table(target, table) for table in _TABLES}
        if counter.get("vokabular"):
            target.execute("INSERT INTO vokabular_meta (schluessel, wert) VALUES"
                           " ('version', '1') ON CONFLICT(schluessel) DO UPDATE SET"
                           " wert = CAST(CAST(wert AS INTEGER) + 1 AS TEXT)")
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


def _adopt_vocabulary_files(contents: dict[str, bytes], mode: str,
                            report: ImportReport) -> None:
    """Write the archive's vocabulary list files into the data folder: all of them when
    restoring, only the missing ones when merging."""
    folder_v = vocabulary_folder()
    for name, data in contents.items():
        if not name.startswith(VOCABULARY_FOLDER + "/"):
            continue
        target_v = folder_v / Path(name).name
        if mode == "ersetzen" or not target_v.exists():
            folder_v.mkdir(parents=True, exist_ok=True)
            target_v.write_bytes(data)
            report.vocabulary_files += 1
    if report.vocabulary_files:
        _forget_vocabulary()


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
