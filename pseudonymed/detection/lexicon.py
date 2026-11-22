"""Semantic lexicon: what a term *is*, from bundled reference data.

The protected vocabulary (:mod:`pseudonymed.detection.vocabulary`) only says that a word must
not be redacted by the guesswork.  The lexicon adds the class of a term and its role:

* role ``terms`` - medical terms by class (Anatomie, Medikament, Erkrankung, Symptom,
  Substanz, Verfahren, Psyche, Physiologie, Organismus, Laborwert, Befund, Eponym);
* role ``feature`` - quasi-identifying attributes (Beruf, Erwerbsstatus, Titel, Klinik,
  Studie) that a residual-risk estimate should weigh and that must never be protected.

The table ``lexicon`` lives in the pseudonym database next to ``vocabulary``.  Bundled sources
are (re)loaded whenever their file changes (a hash per source in ``lexikon_meta``); rows of
the source ``Nutzer`` are left alone, and bundled rows the user deleted stay deleted
(``lexikon_geloescht``).  The ``feature`` rows are the identifiers of the identifiers step
(:mod:`pseudonymed.detection.identifiers`), which redacts them by default; the ``terms``
rows and the control step do not use the lexicon yet.  ``PSEUDONYMED_LEXIKON=0`` switches the
loading off (the test suite does so, except in the lexicon tests).
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from pseudonymed import environment

DATA = Path(__file__).resolve().parent.parent / "data"
MEDICAL = "fach"
FEATURE = "merkmal"
USER = "Nutzer"
SCHEMA = """
CREATE TABLE IF NOT EXISTS lexikon (
    begriff TEXT NOT NULL COLLATE NOCASE,
    klasse  TEXT NOT NULL,
    rolle   TEXT NOT NULL,
    quelle  TEXT NOT NULL,
    kennung TEXT NOT NULL DEFAULT '',
    zusatz  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (begriff, klasse, kennung)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lexikon_meta (
    quelle  TEXT PRIMARY KEY,
    stand   TEXT NOT NULL,
    anzahl  INTEGER NOT NULL,
    geladen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lexikon_geloescht (
    begriff TEXT NOT NULL COLLATE NOCASE,
    klasse  TEXT NOT NULL,
    kennung TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (begriff, klasse, kennung)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lexikon_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
"""
MIN_LENGTH = 2

# Correspondence with the GeMTeX type system for identifying information (annotation
# guideline, 2nd version, 7 September 2025, doi:10.5281/zenodo.15747389).  Studies are IDs
# there, only academic titles are NAME_TITLE (functions such as Chefarzt are not annotated),
# and an employment status is explicitly no PROFESSION: it keeps no GeMTeX type and serves
# the residual-risk estimate only.
GEMTEX = {"Beruf": "PROFESSION", "Titel": "NAME_TITLE", "Klinik": "LOCATION_HOSPITAL",
          "Studie": "ID", "Erwerbsstatus": ""}

Row = tuple[str, str, str, str, str]  # begriff, klasse, rolle, kennung, zusatz (JSON)


def _open_text(path: Path) -> TextIO:
    """A UTF-8 text handle on a plain or gzip-compressed data file."""
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else \
        path.open(encoding="utf-8")


def _csv(path: Path) -> Iterator[dict[str, str]]:
    """The records of a CSV data file with a header line, comment lines skipped."""
    with _open_text(path) as f:
        yield from csv.DictReader(z for z in f if not z.startswith("#"))


def _list_lines(path: Path) -> Iterator[str]:
    """Non-empty, non-comment lines of a plain list file, stripped."""
    with _open_text(path) as f:
        for z in f:
            z = z.strip()
            if z and not z.startswith("#"):
                yield z


def _json(**values: object) -> str:
    """Compact JSON of the given details without empty values ("" when nothing remains)."""
    clean = {k: v for k, v in values.items() if v not in ("", None)}
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":")) if clean else ""


def _number(text: str | None) -> int | None:
    """The integer in a CSV cell (``"12.0"`` included), None when empty or not a number."""
    try:
        return int(float(text)) if text not in (None, "") else None
    except ValueError:
        return None


# -- sources ---------------------------------------------------------------------------------------
def _medical_categories(path: Path) -> Iterator[Row]:
    """Rows of the Wikidata class table: term, medical class and the item as ``code``."""
    for z in _csv(path):
        yield z["begriff"], z["klasse"], MEDICAL, z.get("kennung", ""), ""


def _list_as(category: str) -> Callable[[Path], Iterator[Row]]:
    """A reader that files every line of a plain list under the medical class ``category``."""
    def reader(path: Path) -> Iterator[Row]:
        """One ``terms`` row per list line, without identifier or details."""
        for b in _list_lines(path):
            yield b, category, MEDICAL, "", ""
    return reader


def _professions(path: Path) -> Iterator[Row]:
    """Professions (KldB) and employment statuses with their base form; ``surname`` marks
    the professions that are also common surnames."""
    for z in _csv(path):
        yield (z["begriff"], z.get("klasse") or "Beruf", FEATURE, z.get("kennung", ""),
               _json(grundform=z.get("grundform", ""),
                     nachname=1 if z.get("nachname", "").strip() in ("1", "ja", "true") else None))


def _titles(path: Path) -> Iterator[Row]:
    """One row for the full designation and one for its abbreviation (``Dr. med.``), which is
    what letters actually contain; art and country keep rows of the same word apart."""
    for z in _csv(path):
        kind, country, abbr = z.get("art", ""), z.get("land", ""), z.get("abkuerzung", "").strip()
        yield z["begriff"], "Titel", FEATURE, f"{kind}|{country}", _json(
            art=kind, land=country, abkuerzung=abbr)
        if abbr:
            yield abbr, "Titel", FEATURE, f"{kind}|{country}|{z['begriff']}", _json(
                art=kind, land=country, bedeutung=z["begriff"])


def _hospitals(path: Path) -> Iterator[Row]:
    """Hospitals of the DACH directories, keyed by country and directory number, with place,
    beds and provider as details."""
    for z in _csv(path):
        country = z.get("land", "")
        yield (z["name"], "Klinik", FEATURE, f"{country}:{z.get('kennung', '')}",
               _json(typ=z.get("typ", ""), ort=z.get("ort", ""), plz=z.get("plz", ""),
                     gemeinde=z.get("gemeinde_kennung", ""), land=country,
                     betten=_number(z.get("betten")), traeger=z.get("traeger", "")))


def _studies(path: Path) -> Iterator[Row]:
    """The registry number carries the details; the acronym row only points to it."""
    for z in _csv(path):
        code = f"{z.get('register', '')}:{z.get('kennung', '')}"
        extra = _json(teilnehmer=_number(z.get("teilnehmer")),
                      teilnehmer_art=z.get("teilnehmer_art", ""),
                      zentren_dach=_number(z.get("zentren_dach")),
                      laender=z.get("laender_dach", ""),
                      orte=" | ".join((z.get("orte_dach") or "").split(" | ")[:5]))
        if z.get("kennung"):
            yield z["kennung"], "Studie", FEATURE, code, extra
        acronym = (z.get("akronym") or "").strip()
        if len(acronym) >= 3:
            yield acronym, "Studie", FEATURE, code, ""


def _eponyms() -> Iterator[Row]:
    """The eponyms of the NER step (four characters or more) as ``terms`` rows."""
    from pseudonymed.detection.ner import EPONYMS

    for e in sorted(EPONYMS):
        if len(e) >= 4:
            yield e, "Eponym", MEDICAL, "", ""


@dataclass(frozen=True)
class Source:
    """A bundled source: its name in ``lexikon_meta``, its file (None for a source computed
    in code) and the reader that yields its rows."""

    name: str
    file: Path | None
    reader: Callable[..., Iterator[Row]]


SOURCES: tuple[Source, ...] = (
    Source("Wikidata", DATA / "fachklassen_de.csv.gz", _medical_categories),
    Source("Körperteile", DATA / "koerperteile_de.txt", _list_as("Anatomie")),
    Source("Befundbegriffe", DATA / "befundbegriffe_de.txt", _list_as("Befund")),
    Source("Laborwerte", DATA / "laborwerte_de.txt", _list_as("Laborwert")),
    Source("Eponyme", None, _eponyms),
    Source("Berufe", DATA / "berufe_de.csv.gz", _professions),
    Source("Titel", DATA / "titel_de.csv.gz", _titles),
    Source("Kliniken", DATA / "kliniken_dach.csv.gz", _hospitals),
    Source("Studien", DATA / "studien_dach.csv.gz", _studies),
)


def _state(q: Source) -> str:
    """Short hash of a source's current content: its file bytes, or the rows it computes."""
    if q.file is None:
        lines = "\n".join("\t".join(z) for z in q.reader())
        return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(q.file.read_bytes()).hexdigest()[:16]


def active() -> bool:
    """Is loading switched on (``PSEUDONYMED_LEXIKON`` is not ``0``)?"""
    return environment.variable("LEXIKON", "1") != "0"


def _should_load(q: Source, names: set[str] | None) -> bool:
    """Is a bundled source present on disk and, when names were given, among them?"""
    if q.file is not None and not q.file.exists():
        return False
    return not names or q.name in names


def _rows_of(q: Source) -> Iterator[Row]:
    """The rows of a source with inner whitespace collapsed; terms shorter than
    ``MIN_LENGTH`` are left out."""
    args = () if q.file is None else (q.file,)
    for b, k, r, code, extra in q.reader(*args):
        if len(b.strip()) >= MIN_LENGTH:
            yield " ".join(b.split()), k, r, code, extra


def _load_source(connection: sqlite3.Connection, q: Source, state: str) -> int:
    """Replaces the rows of one source and records ``state`` in ``lexikon_meta``; returns
    the number of rows inserted."""
    connection.execute("DELETE FROM lexikon WHERE quelle = ?", (q.name,))
    before = connection.total_changes
    connection.executemany(
        "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ((b, k, r, q.name, code, extra) for b, k, r, code, extra in _rows_of(q)))
    n = connection.total_changes - before
    connection.execute(
        "INSERT OR REPLACE INTO lexikon_meta (quelle, stand, anzahl, geladen)"
        " VALUES (?, ?, ?, ?)",
        (q.name, state, n, dt.datetime.now().isoformat(timespec="seconds")))
    return n


def _remove_deleted(connection: sqlite3.Connection) -> None:
    """Removes bundled rows the user deleted earlier (``lexikon_geloescht``) after a reload
    brought them back; rows of the source ``Nutzer`` stay."""
    connection.execute(
        "DELETE FROM lexikon WHERE quelle != ? AND EXISTS (SELECT 1 FROM lexikon_geloescht g"
        " WHERE g.begriff = lexikon.begriff AND g.klasse = lexikon.klasse"
        " AND g.kennung = lexikon.kennung)", (USER,))


def ensure(connection: sqlite3.Connection, force: bool = False,
           sources: Iterable[str] | None = None) -> dict[str, int]:
    """Create the tables and (re)load every bundled source whose data changed (only the
    sources named in ``sources``, when given).  Returns the number of rows loaded per
    reloaded source (empty when all is current)."""
    if not active() and not force:
        return {}
    connection.executescript(SCHEMA)
    old = dict(connection.execute("SELECT quelle, stand FROM lexikon_meta"))
    names = set(sources) if sources is not None else None
    loaded: dict[str, int] = {}
    for q in SOURCES:
        if not _should_load(q, names):
            continue
        state = _state(q)
        if force or old.get(q.name) != state:
            loaded[q.name] = _load_source(connection, q, state)
    if loaded:
        _remove_deleted(connection)
        increment_version(connection)
    connection.commit()
    return loaded


def version(connection: sqlite3.Connection) -> int:
    """Counter that changes with every change of the lexicon (for cached matchers)."""
    try:
        r = connection.execute("SELECT version FROM lexikon_version WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(r[0]) if r else 0


def increment_version(connection: sqlite3.Connection) -> None:
    """Increments the lexicon's version counter (creating it at 1); not committed here."""
    connection.execute(
        "INSERT INTO lexikon_version (id, version) VALUES (1, 1)"
        " ON CONFLICT(id) DO UPDATE SET version = version + 1")


def per_class(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(class, role, rows), merkmal classes first; empty when the table does not exist."""
    try:
        return [tuple(r) for r in connection.execute(
            "SELECT klasse, rolle, COUNT(*) FROM lexikon GROUP BY klasse, rolle"
            " ORDER BY rolle DESC, COUNT(*) DESC, klasse")]
    except sqlite3.OperationalError:
        return []


def gemtex_type(category: str, extra: str = "") -> str:
    """GeMTeX type of a lexicon row ("" where the guideline annotates nothing)."""
    if category == "Titel":
        try:
            kind = json.loads(extra).get("art", "") if extra else ""
        except ValueError:
            kind = ""
        return GEMTEX["Titel"] if kind in ("", "akademisch") else ""
    return GEMTEX.get(category, "")


def categories_of(connection: sqlite3.Connection, term: str) -> list[tuple[str, str]]:
    """All (class, role) pairs of a term, case-insensitively."""
    return [tuple(r) for r in connection.execute(
        "SELECT DISTINCT klasse, rolle FROM lexikon WHERE begriff = ? ORDER BY rolle, klasse",
        (" ".join(term.split()),))]
