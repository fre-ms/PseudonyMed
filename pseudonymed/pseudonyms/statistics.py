"""What the pseudonym store holds: the statistics the database page and ``datenbank status``
show, the labels of the pseudonym categories, and the public handle of a key."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pseudonymed.config import config_path
from pseudonymed.core.text import say
from pseudonymed.detection.medical_terms import data_folder as vocabulary_folder
from pseudonymed.pseudonyms.store import default_key_path, default_path, key_path_for

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
_YES = say("ja", "yes")
_NO = say("nein", "no")
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
                + [(say("Gemerkte Verwerfungen", "Remembered discards"), str(self.discarded))]
                + _indented(self.discarded_per_rule)
                + self._status_lines()
                + _indented(self.vocabulary_categories)
                + self._lexicon_lines()
                + [(say("Konfiguration", "Configuration"),
                    say("vorhanden", "present") if self.config_present else say("keine", "none"))])

    def _header_lines(self) -> list[tuple[str, str]]:
        """Where database and key live, and the headline counts."""
        return [
            (say("Datenbank", "Database"), str(self.path)),
            (say("Vorhanden", "Present"),
             _YES if self.exists else say("nein (noch nicht angelegt)", "no (not created yet)")),
            (say("Größe", "Size"), _mb(self.size)),
            (say("Schema", "Scheme"), self.schema or "–"),
            (say("Schlüsseldatei", "Key file"), str(self.key_path or "–")),
            (say("Schlüssel vorhanden", "Key present"), _YES if self.key_present else _NO),
            (say("Schlüssel-Kennung", "Key ID"), self.key_id or "–"),
            (say("Patienten", "Patients"), str(self.patients)),
            (say("Verknüpfte Schreibweisen", "Linked spellings"), str(self.aliases)),
            (say("Zuordnungen gesamt", "Mappings in total"), str(self.mappings)),
        ]

    def _status_lines(self) -> list[tuple[str, str]]:
        """Time span of the entries, integrity, page usage and the vocabulary total."""
        return [
            (say("Erster Eintrag", "First entry"), self.first_entry or "–"),
            (say("Letzter Eintrag", "Last entry"), self.last_entry or "–"),
            (say("Integrität", "Integrity"), self.integrity or "–"),
            (say("Seiten / frei", "Pages / free"), f"{self.pages} / {self.free_pages}"),
            (say("Vokabular (Begriffe)", "Vocabulary (terms)"), str(self.vocabulary_terms)),
        ]

    def _lexicon_lines(self) -> list[tuple[str, str]]:
        """The bundled lexicon per class (only when it is loaded) and the data sets the
        user fetched themselves."""
        out: list[tuple[str, str]] = []
        if self.lexicon_categories:
            out.append((say("Lexikon (Einträge)", "Lexicon (entries)"),
                        str(sum(n for _k, _r, n in self.lexicon_categories))))
            out += [(say(f"    {k} ({'Identifikator' if r == 'merkmal' else 'Fachklasse'})",
                         f"    {k} ({'identifier' if r == 'merkmal' else 'medical class'})"),
                     str(n))
                    for k, r, n in self.lexicon_categories]
        out += [(say("Zusatzdaten (selbst geladen)", "Extra data (fetched yourself)"),
                 say(f"{q}: {n} Einträge, {g[:10]}", f"{q}: {n} entries, {g[:10]}"))
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


def store_paths(store_path: Path | None) -> tuple[Path, Path]:
    """Database and key file: the configured ones for the default database, the key
    next to the database when a database path was given explicitly."""
    if store_path:
        db = Path(store_path)
        return db, key_path_for(db)
    return default_path(), default_key_path()


def statistics(store_path: Path | None = None) -> Statistics:
    """Describe the pseudonym store at ``store_path`` (default: the configured one)
    without changing it; a database that cannot be read reports the error as integrity."""
    db, key = store_paths(store_path)
    st = Statistics(path=db, key_path=key, exists=db.exists(),
                    config_present=config_path().exists())
    if key.exists():
        st.key_present = True
        st.key_id = _key_file_code(key)
    st.vocabulary_files = len(vocabulary_files(vocabulary_folder()))
    if not db.exists():
        return st
    st.size = db.stat().st_size
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        _count_contents(v, st)
    except sqlite3.DatabaseError as e:
        st.integrity = say(f"Fehler: {e}", f"Error: {e}")
    finally:
        v.close()
    return st


def _key_file_code(key: Path) -> str:
    """The public handle of the key in a key file, or ``unreadable`` when it is not hex digits."""
    try:
        return key_id(bytes.fromhex(key.read_text(encoding="utf-8").strip()))
    except ValueError:
        return "unlesbar"


def vocabulary_files(folder: Path) -> list[Path]:
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
