"""Semantic lexicon: what a term *is*, from bundled reference data.

The protected vocabulary (:mod:`pseudonymed.erkennung.vokabular`) only says that a word must
not be redacted by the guesswork.  The lexicon adds the class of a term and its role:

* role ``fach`` - medical terms by class (Anatomie, Medikament, Erkrankung, Symptom,
  Substanz, Verfahren, Psyche, Physiologie, Organismus, Laborwert, Befund, Eponym);
* role ``merkmal`` - quasi-identifying attributes (Beruf, Erwerbsstatus, Titel, Klinik,
  Studie) that a residual-risk estimate should weigh and that must never be protected.

The table ``lexikon`` lives in the pseudonym database next to ``vokabular``.  Bundled sources
are (re)loaded whenever their file changes (a hash per source in ``lexikon_meta``); rows of
the source ``Nutzer`` are left alone, and bundled rows the user deleted stay deleted
(``lexikon_geloescht``).  The ``merkmal`` rows are the identifiers of the identifiers step
(:mod:`pseudonymed.erkennung.identifikatoren`), which redacts them by default; the ``fach``
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

from pseudonymed import umgebung

DATEN = Path(__file__).resolve().parent.parent / "daten"
FACH = "fach"
MERKMAL = "merkmal"
NUTZER = "Nutzer"
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
MIN_LAENGE = 2

# Correspondence with the GeMTeX type system for identifying information (annotation
# guideline, 2nd version, 7 September 2025, doi:10.5281/zenodo.15747389).  Studies are IDs
# there, only academic titles are NAME_TITLE (functions such as Chefarzt are not annotated),
# and an employment status is explicitly no PROFESSION: it keeps no GeMTeX type and serves
# the residual-risk estimate only.
GEMTEX = {"Beruf": "PROFESSION", "Titel": "NAME_TITLE", "Klinik": "LOCATION_HOSPITAL",
          "Studie": "ID", "Erwerbsstatus": ""}

Zeile = tuple[str, str, str, str, str]  # begriff, klasse, rolle, kennung, zusatz (JSON)


def _offen(pfad: Path) -> TextIO:
    """A UTF-8 text handle on a plain or gzip-compressed data file."""
    return gzip.open(pfad, "rt", encoding="utf-8") if pfad.suffix == ".gz" else \
        pfad.open(encoding="utf-8")


def _csv(pfad: Path) -> Iterator[dict[str, str]]:
    """The records of a CSV data file with a header line, comment lines skipped."""
    with _offen(pfad) as f:
        yield from csv.DictReader(z for z in f if not z.startswith("#"))


def _liste(pfad: Path) -> Iterator[str]:
    """Non-empty, non-comment lines of a plain list file, stripped."""
    with _offen(pfad) as f:
        for z in f:
            z = z.strip()
            if z and not z.startswith("#"):
                yield z


def _json(**werte: object) -> str:
    """Compact JSON of the given details without empty values ("" when nothing remains)."""
    sauber = {k: v for k, v in werte.items() if v not in ("", None)}
    return json.dumps(sauber, ensure_ascii=False, separators=(",", ":")) if sauber else ""


def _zahl(text: str | None) -> int | None:
    """The integer in a CSV cell (``"12.0"`` included), None when empty or not a number."""
    try:
        return int(float(text)) if text not in (None, "") else None
    except ValueError:
        return None


# -- sources ---------------------------------------------------------------------------------------
def _fachklassen(pfad: Path) -> Iterator[Zeile]:
    """Rows of the Wikidata class table: term, medical class and the item as ``kennung``."""
    for z in _csv(pfad):
        yield z["begriff"], z["klasse"], FACH, z.get("kennung", ""), ""


def _liste_als(klasse: str) -> Callable[[Path], Iterator[Zeile]]:
    """A reader that files every line of a plain list under the medical class ``klasse``."""
    def lesen(pfad: Path) -> Iterator[Zeile]:
        """One ``fach`` row per list line, without identifier or details."""
        for b in _liste(pfad):
            yield b, klasse, FACH, "", ""
    return lesen


def _berufe(pfad: Path) -> Iterator[Zeile]:
    """Professions (KldB) and employment statuses with their base form; ``nachname`` marks
    the professions that are also common surnames."""
    for z in _csv(pfad):
        yield (z["begriff"], z.get("klasse") or "Beruf", MERKMAL, z.get("kennung", ""),
               _json(grundform=z.get("grundform", ""),
                     nachname=1 if z.get("nachname", "").strip() in ("1", "ja", "true") else None))


def _titel(pfad: Path) -> Iterator[Zeile]:
    """One row for the full designation and one for its abbreviation (``Dr. med.``), which is
    what letters actually contain; art and country keep rows of the same word apart."""
    for z in _csv(pfad):
        art, land, abk = z.get("art", ""), z.get("land", ""), z.get("abkuerzung", "").strip()
        yield z["begriff"], "Titel", MERKMAL, f"{art}|{land}", _json(art=art, land=land,
                                                                     abkuerzung=abk)
        if abk:
            yield abk, "Titel", MERKMAL, f"{art}|{land}|{z['begriff']}", _json(
                art=art, land=land, bedeutung=z["begriff"])


def _kliniken(pfad: Path) -> Iterator[Zeile]:
    """Hospitals of the DACH directories, keyed by country and directory number, with place,
    beds and provider as details."""
    for z in _csv(pfad):
        land = z.get("land", "")
        yield (z["name"], "Klinik", MERKMAL, f"{land}:{z.get('kennung', '')}",
               _json(typ=z.get("typ", ""), ort=z.get("ort", ""), plz=z.get("plz", ""),
                     gemeinde=z.get("gemeinde_kennung", ""), land=land,
                     betten=_zahl(z.get("betten")), traeger=z.get("traeger", "")))


def _studien(pfad: Path) -> Iterator[Zeile]:
    """The registry number carries the details; the acronym row only points to it."""
    for z in _csv(pfad):
        kennung = f"{z.get('register', '')}:{z.get('kennung', '')}"
        zusatz = _json(teilnehmer=_zahl(z.get("teilnehmer")),
                       teilnehmer_art=z.get("teilnehmer_art", ""),
                       zentren_dach=_zahl(z.get("zentren_dach")),
                       laender=z.get("laender_dach", ""),
                       orte=" | ".join((z.get("orte_dach") or "").split(" | ")[:5]))
        if z.get("kennung"):
            yield z["kennung"], "Studie", MERKMAL, kennung, zusatz
        akronym = (z.get("akronym") or "").strip()
        if len(akronym) >= 3:
            yield akronym, "Studie", MERKMAL, kennung, ""


def _eponyme() -> Iterator[Zeile]:
    """The eponyms of the NER step (four characters or more) as ``fach`` rows."""
    from pseudonymed.erkennung.ner import EPONYME

    for e in sorted(EPONYME):
        if len(e) >= 4:
            yield e, "Eponym", FACH, "", ""


@dataclass(frozen=True)
class Quelle:
    """A bundled source: its name in ``lexikon_meta``, its file (None for a source computed
    in code) and the reader that yields its rows."""

    name: str
    datei: Path | None
    lesen: Callable[..., Iterator[Zeile]]


QUELLEN: tuple[Quelle, ...] = (
    Quelle("Wikidata", DATEN / "fachklassen_de.csv.gz", _fachklassen),
    Quelle("Körperteile", DATEN / "koerperteile_de.txt", _liste_als("Anatomie")),
    Quelle("Befundbegriffe", DATEN / "befundbegriffe_de.txt", _liste_als("Befund")),
    Quelle("Laborwerte", DATEN / "laborwerte_de.txt", _liste_als("Laborwert")),
    Quelle("Eponyme", None, _eponyme),
    Quelle("Berufe", DATEN / "berufe_de.csv.gz", _berufe),
    Quelle("Titel", DATEN / "titel_de.csv.gz", _titel),
    Quelle("Kliniken", DATEN / "kliniken_dach.csv.gz", _kliniken),
    Quelle("Studien", DATEN / "studien_dach.csv.gz", _studien),
)


def _stand(q: Quelle) -> str:
    """Short hash of a source's current content: its file bytes, or the rows it computes."""
    if q.datei is None:
        zeilen = "\n".join("\t".join(z) for z in q.lesen())
        return hashlib.sha256(zeilen.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(q.datei.read_bytes()).hexdigest()[:16]


def aktiv() -> bool:
    """Is loading switched on (``PSEUDONYMED_LEXIKON`` is not ``0``)?"""
    return umgebung.variable("LEXIKON", "1") != "0"


def _zu_laden(q: Quelle, namen: set[str] | None) -> bool:
    """Is a bundled source present on disk and, when names were given, among them?"""
    if q.datei is not None and not q.datei.exists():
        return False
    return not namen or q.name in namen


def _zeilen_von(q: Quelle) -> Iterator[Zeile]:
    """The rows of a source with inner whitespace collapsed; terms shorter than
    ``MIN_LAENGE`` are left out."""
    args = () if q.datei is None else (q.datei,)
    for b, k, r, ke, zu in q.lesen(*args):
        if len(b.strip()) >= MIN_LAENGE:
            yield " ".join(b.split()), k, r, ke, zu


def _lade_quelle(verbindung: sqlite3.Connection, q: Quelle, stand: str) -> int:
    """Replaces the rows of one source and records ``stand`` in ``lexikon_meta``; returns
    the number of rows inserted."""
    verbindung.execute("DELETE FROM lexikon WHERE quelle = ?", (q.name,))
    vorher = verbindung.total_changes
    verbindung.executemany(
        "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ((b, k, r, q.name, ke, zu) for b, k, r, ke, zu in _zeilen_von(q)))
    n = verbindung.total_changes - vorher
    verbindung.execute(
        "INSERT OR REPLACE INTO lexikon_meta (quelle, stand, anzahl, geladen)"
        " VALUES (?, ?, ?, ?)",
        (q.name, stand, n, dt.datetime.now().isoformat(timespec="seconds")))
    return n


def _geloeschte_entfernen(verbindung: sqlite3.Connection) -> None:
    """Removes bundled rows the user deleted earlier (``lexikon_geloescht``) after a reload
    brought them back; rows of the source ``Nutzer`` stay."""
    verbindung.execute(
        "DELETE FROM lexikon WHERE quelle != ? AND EXISTS (SELECT 1 FROM lexikon_geloescht g"
        " WHERE g.begriff = lexikon.begriff AND g.klasse = lexikon.klasse"
        " AND g.kennung = lexikon.kennung)", (NUTZER,))


def sicherstellen(verbindung: sqlite3.Connection, erzwingen: bool = False,
                  quellen: Iterable[str] | None = None) -> dict[str, int]:
    """Create the tables and (re)load every bundled source whose data changed (only the
    sources named in ``quellen``, when given).  Returns the number of rows loaded per
    reloaded source (empty when all is current)."""
    if not aktiv() and not erzwingen:
        return {}
    verbindung.executescript(SCHEMA)
    alt = dict(verbindung.execute("SELECT quelle, stand FROM lexikon_meta"))
    namen = set(quellen) if quellen is not None else None
    geladen: dict[str, int] = {}
    for q in QUELLEN:
        if not _zu_laden(q, namen):
            continue
        stand = _stand(q)
        if erzwingen or alt.get(q.name) != stand:
            geladen[q.name] = _lade_quelle(verbindung, q, stand)
    if geladen:
        _geloeschte_entfernen(verbindung)
        erhoehe_version(verbindung)
    verbindung.commit()
    return geladen


def version(verbindung: sqlite3.Connection) -> int:
    """Counter that changes with every change of the lexicon (for cached matchers)."""
    try:
        r = verbindung.execute("SELECT version FROM lexikon_version WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(r[0]) if r else 0


def erhoehe_version(verbindung: sqlite3.Connection) -> None:
    """Increments the lexicon's version counter (creating it at 1); not committed here."""
    verbindung.execute(
        "INSERT INTO lexikon_version (id, version) VALUES (1, 1)"
        " ON CONFLICT(id) DO UPDATE SET version = version + 1")


def je_klasse(verbindung: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(class, role, rows), merkmal classes first; empty when the table does not exist."""
    try:
        return [tuple(r) for r in verbindung.execute(
            "SELECT klasse, rolle, COUNT(*) FROM lexikon GROUP BY klasse, rolle"
            " ORDER BY rolle DESC, COUNT(*) DESC, klasse")]
    except sqlite3.OperationalError:
        return []


def gemtex_typ(klasse: str, zusatz: str = "") -> str:
    """GeMTeX type of a lexicon row ("" where the guideline annotates nothing)."""
    if klasse == "Titel":
        try:
            art = json.loads(zusatz).get("art", "") if zusatz else ""
        except ValueError:
            art = ""
        return GEMTEX["Titel"] if art in ("", "akademisch") else ""
    return GEMTEX.get(klasse, "")


def klassen_von(verbindung: sqlite3.Connection, begriff: str) -> list[tuple[str, str]]:
    """All (class, role) pairs of a term, case-insensitively."""
    return [tuple(r) for r in verbindung.execute(
        "SELECT DISTINCT klasse, rolle FROM lexikon WHERE begriff = ? ORDER BY rolle, klasse",
        (" ".join(begriff.split()),))]
