"""The protected vocabulary as a table of the pseudonym database.

Every term the guesswork (NER, fuzzy search) must leave alone lives in the table
``vokabular`` (term, category, when added).  On first use the bundled lists are loaded
into it (Wikidata medicine, body parts, findings terms, eponyms) and lists from the old
``fachbegriffe/`` folder are taken over once; from then on the table is the truth: the
vocabulary step shows, sorts, edits, adds and deletes rows and exchanges CSV.  A
version counter in ``vokabular_meta`` tells :func:`pseudonymed.erkennung.fachbegriffe.standard`
when to rebuild its matcher.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import os
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from pseudonymed import umgebung
from pseudonymed.erkennung import listenimport

DATEN = Path(__file__).resolve().parent.parent / "daten"
KATEGORIE_MEDIZIN = "Medizin (Wikidata)"
KATEGORIE_KOERPER = "Körperteile"
KATEGORIE_BEFUND = "Befundbegriffe"
KATEGORIE_EPONYME = "Eponyme"
KATEGORIE_NUTZER = "Nutzervokabular"
GRUNDBESTAND = (
    (DATEN / "fachbegriffe_de.txt.gz", KATEGORIE_MEDIZIN),
    (DATEN / "koerperteile_de.txt", KATEGORIE_KOERPER),
    (DATEN / "befundbegriffe_de.txt", KATEGORIE_BEFUND),
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS vokabular (
    begriff   TEXT PRIMARY KEY COLLATE NOCASE,
    kategorie TEXT NOT NULL DEFAULT '',
    angelegt  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS vokabular_kategorie ON vokabular(kategorie);
CREATE TABLE IF NOT EXISTS vokabular_meta (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT NOT NULL
);
"""
MIN_LAENGE = 3


def standardpfad() -> Path:
    """The pseudonym database that holds the table (the store decides where it lives)."""
    from pseudonymed.pseudonyme.speicher import standardpfad as _pfad

    return _pfad()


def legacy_ordner() -> Path:
    """The folder of the former per-file user lists (taken over once)."""
    return umgebung.datenordner() / "fachbegriffe"


def _zeilen(pfad: Path) -> Iterator[str]:
    """The non-empty, non-comment lines of a plain or gzipped list, stripped."""
    oeffnen = gzip.open if pfad.suffix == ".gz" else open
    with oeffnen(pfad, "rt", encoding="utf-8", errors="replace") as f:
        for zeile in f:
            zeile = zeile.strip()
            if zeile and not zeile.startswith("#"):
                yield zeile


def _sauber(begriffe: Iterable[str]) -> list[str]:
    """Terms with their spaces collapsed, long enough, no comments, first spelling of
    each (case-insensitively), in the given order."""
    aus: list[str] = []
    gesehen: set[str] = set()
    for b in begriffe:
        b = " ".join(str(b).split())
        if len(b) < MIN_LAENGE or b.startswith("#") or b.casefold() in gesehen:
            continue
        gesehen.add(b.casefold())
        aus.append(b)
    return aus


def _legacy_dateien() -> list[Path]:
    """The list files of the former per-file folder in name order; none without it."""
    ordner = legacy_ordner()
    if not ordner.is_dir():
        return []
    return [d for d in sorted(ordner.iterdir()) if d.is_file() and d.suffix in (".txt", ".gz")]


def _legacy_kategorie(datei: Path) -> str:
    """The category a former list file goes to: the user's for ``nutzervokabular*``,
    else the file's stem."""
    return KATEGORIE_NUTZER if datei.stem.lower().startswith("nutzervokabular") else datei.stem


def _markiere_uebernommen(datei: Path) -> None:
    """Rename a taken-over list to ``*.uebernommen`` so that it is not read twice; a file
    that cannot be renamed stays (its terms are in the table already)."""
    try:
        datei.rename(datei.with_name(datei.name + ".uebernommen"))
    except OSError:
        pass


class Vokabular:
    """Reads and writes the vocabulary table of the pseudonym database."""

    def __init__(self, pfad: str | Path | None = None, grundbestand: bool = True) -> None:
        self.pfad = Path(pfad) if pfad else standardpfad()
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        self.verbindung = sqlite3.connect(str(self.pfad), check_same_thread=False)
        from pseudonymed.pseudonyme.speicher import _SCHEMA  # the whole store, not only our tables

        self.verbindung.executescript(_SCHEMA)
        self.verbindung.executescript(SCHEMA)
        try:
            os.chmod(self.pfad, 0o600)
        except OSError:
            pass
        if grundbestand and self.anzahl() == 0:
            self.grundbestand()
        if grundbestand:
            from pseudonymed.erkennung import lexikon  # classes and the identifiers step

            lexikon.sicherstellen(self.verbindung)

    def close(self) -> None:
        """Close the database connection."""
        self.verbindung.close()

    def __enter__(self) -> Vokabular:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -----------------------------------------------------------------------------------
    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        r = self.verbindung.execute(
            "SELECT wert FROM vokabular_meta WHERE schluessel = 'version'").fetchone()
        return int(r[0]) if r else 0

    def anzahl(self) -> int:
        """How many terms the table holds."""
        return int(self.verbindung.execute("SELECT COUNT(*) FROM vokabular").fetchone()[0])

    def kategorien(self) -> list[tuple[str, int]]:
        """Every category with its number of terms, the largest first."""
        return [(k, n) for k, n in self.verbindung.execute(
            "SELECT kategorie, COUNT(*) FROM vokabular GROUP BY kategorie"
            " ORDER BY COUNT(*) DESC, kategorie")]

    def alle(self, suche: str = "", kategorie: str = "") -> list[tuple[str, str, str]]:
        """Rows (term, category, added) in alphabetical order, narrowed to terms that
        contain ``suche`` and to one category when given."""
        sql, werte = "SELECT begriff, kategorie, angelegt FROM vokabular", []
        bedingungen = []
        if suche:
            bedingungen.append("begriff LIKE ? ESCAPE '\\'")
            werte.append("%" + suche.replace("\\", "\\\\").replace("%", "\\%")
                         .replace("_", "\\_") + "%")
        if kategorie:
            bedingungen.append("kategorie = ?")
            werte.append(kategorie)
        if bedingungen:
            sql += " WHERE " + " AND ".join(bedingungen)
        sql += " ORDER BY begriff COLLATE NOCASE"
        return [tuple(r) for r in self.verbindung.execute(sql, werte)]

    def je_kategorie(self) -> dict[str, list[str]]:
        """Terms grouped by category, bundled categories first (for the matcher's sources)."""
        gruppen: dict[str, list[str]] = {}
        for begriff, kategorie in self.verbindung.execute(
                "SELECT begriff, kategorie FROM vokabular"):
            gruppen.setdefault(kategorie, []).append(begriff)
        reihenfolge = [k for _p, k in GRUNDBESTAND] + [KATEGORIE_EPONYME, KATEGORIE_NUTZER]
        return {k: gruppen[k] for k in reihenfolge if k in gruppen} | {
            k: v for k, v in sorted(gruppen.items()) if k not in reihenfolge}

    def enthaelt(self, begriff: str) -> bool:
        """Whether the term is in the table (case-insensitively)."""
        return self.verbindung.execute("SELECT 1 FROM vokabular WHERE begriff = ?",
                                       (begriff.strip(),)).fetchone() is not None

    # -- writing -----------------------------------------------------------------------------------
    def _bump(self) -> None:
        self.verbindung.execute(
            "INSERT INTO vokabular_meta (schluessel, wert) VALUES ('version', '1')"
            " ON CONFLICT(schluessel) DO UPDATE SET wert = CAST(CAST(wert AS INTEGER) + 1 AS TEXT)")

    def fuege_hinzu(self, begriffe: Iterable[str], kategorie: str = KATEGORIE_NUTZER) -> int:
        """Insert what is new (case-insensitively); returns how many were new."""
        saubere = _sauber(begriffe)
        if not saubere:
            return 0
        jetzt = dt.datetime.now().isoformat(timespec="seconds")
        vorher = self.verbindung.total_changes
        self.verbindung.executemany(
            "INSERT OR IGNORE INTO vokabular (begriff, kategorie, angelegt) VALUES (?, ?, ?)",
            [(b, kategorie.strip() or KATEGORIE_NUTZER, jetzt) for b in saubere])
        neu = self.verbindung.total_changes - vorher
        if neu:
            self._bump()
        self.verbindung.commit()
        return neu

    def entferne(self, begriffe: Iterable[str]) -> int:
        """Delete the given terms; returns how many rows went."""
        vorher = self.verbindung.total_changes
        self.verbindung.executemany("DELETE FROM vokabular WHERE begriff = ?",
                                    [(b.strip(),) for b in begriffe if b.strip()])
        n = self.verbindung.total_changes - vorher
        if n:
            self._bump()
        self.verbindung.commit()
        return n

    def benenne_um(self, alt: str, neu: str) -> bool:
        """Rename one term; False when the new spelling is too short or taken by another
        term (a change of case only is allowed)."""
        neu = " ".join(neu.split())
        if len(neu) < MIN_LAENGE or (neu.casefold() != alt.casefold() and self.enthaelt(neu)):
            return False
        self.verbindung.execute("UPDATE vokabular SET begriff = ? WHERE begriff = ?", (neu, alt))
        self._bump()
        self.verbindung.commit()
        return True

    def setze_kategorie(self, begriffe: Iterable[str], kategorie: str) -> int:
        """Move the given terms to a category (the user's when empty); returns how many
        rows changed."""
        kategorie = kategorie.strip() or KATEGORIE_NUTZER
        vorher = self.verbindung.total_changes
        self.verbindung.executemany("UPDATE vokabular SET kategorie = ? WHERE begriff = ?",
                                    [(kategorie, b) for b in begriffe])
        n = self.verbindung.total_changes - vorher
        if n:
            self._bump()
        self.verbindung.commit()
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def exportiere_csv(self, pfad: Path, kategorie: str = "") -> int:
        """Write ``begriff,kategorie`` rows (comma, UTF-8, header line)."""
        zeilen = self.alle(kategorie=kategorie)
        with Path(pfad).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["begriff", "kategorie"])
            w.writerows((b, k) for b, k, _a in zeilen)
        return len(zeilen)

    def importiere_csv(self, pfad: Path, kategorie: str = KATEGORIE_NUTZER) -> tuple[int, int]:
        """Read a CSV (``begriff[,kategorie]``, header optional, comma, semicolon or tab) or a
        plain list (one term per line); returns (new, read).  A category column in the
        file wins over ``kategorie``."""
        gelesen = listenimport.lies_liste(pfad, "kategorie", kategorie)
        neu = sum(self.fuege_hinzu(begriffe, kat) for kat, begriffe in gelesen.gruppen.items())
        return neu, gelesen.zeilen

    # -- bundled lists ---------------------------------------------------------------------------
    def grundbestand(self) -> dict[str, int]:
        """Load the bundled lists and the eponyms, take over lists of the old folder once."""
        from pseudonymed.erkennung.ner import EPONYME

        zaehlung: dict[str, int] = {}
        for pfad, kategorie in GRUNDBESTAND:
            if pfad.exists():
                zaehlung[kategorie] = self.fuege_hinzu(_zeilen(pfad), kategorie)
        zaehlung[KATEGORIE_EPONYME] = self.fuege_hinzu((e for e in EPONYME if len(e) >= 4),
                                                       KATEGORIE_EPONYME)
        for datei in _legacy_dateien():
            kategorie = _legacy_kategorie(datei)
            zaehlung[kategorie] = zaehlung.get(kategorie, 0) + \
                self.fuege_hinzu(_zeilen(datei), kategorie)
            _markiere_uebernommen(datei)
        self.verbindung.execute(
            "INSERT OR REPLACE INTO vokabular_meta (schluessel, wert) VALUES ('grundbestand', ?)",
            (dt.datetime.now().isoformat(timespec="seconds"),))
        self._bump()
        self.verbindung.commit()
        return zaehlung
