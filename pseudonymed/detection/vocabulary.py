"""The protected vocabulary as a table of the pseudonym database.

Every term the guesswork (NER, fuzzy search) must leave alone lives in the table
``vocabulary`` (term, category, when added).  On first use the bundled lists are loaded
into it (Wikidata medicine, body parts, findings terms, eponyms) and lists from the old
``fachbegriffe/`` folder are taken over once; from then on the table is the truth: the
vocabulary step shows, sorts, edits, adds and deletes rows and exchanges CSV.  A
version counter in ``vokabular_meta`` tells :func:`pseudonymed.detection.medical_terms.standard`
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

from pseudonymed import environment
from pseudonymed.detection import list_import

DATA = Path(__file__).resolve().parent.parent / "data"
CATEGORY_MEDICINE = "Medizin (Wikidata)"
CATEGORY_BODY_PARTS = "Körperteile"
CATEGORY_FINDINGS = "Befundbegriffe"
CATEGORY_EPONYMS = "Eponyme"
CATEGORY_USER = "Nutzervokabular"
SEED_LISTS = (
    (DATA / "fachbegriffe_de.txt.gz", CATEGORY_MEDICINE),
    (DATA / "koerperteile_de.txt", CATEGORY_BODY_PARTS),
    (DATA / "befundbegriffe_de.txt", CATEGORY_FINDINGS),
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
MIN_LENGTH = 3


def default_path() -> Path:
    """The pseudonym database that holds the table (the store decides where it lives)."""
    from pseudonymed.pseudonyms.store import default_path as _path

    return _path()


def legacy_folder() -> Path:
    """The folder of the former per-file user lists (taken over once)."""
    return environment.data_folder() / "fachbegriffe"


def _rows(path: Path) -> Iterator[str]:
    """The non-empty, non-comment lines of a plain or gzipped list, stripped."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def _clean(terms: Iterable[str]) -> list[str]:
    """Terms with their spaces collapsed, long enough, no comments, first spelling of
    each (case-insensitively), in the given order."""
    out: list[str] = []
    seen: set[str] = set()
    for b in terms:
        b = " ".join(str(b).split())
        if len(b) < MIN_LENGTH or b.startswith("#") or b.casefold() in seen:
            continue
        seen.add(b.casefold())
        out.append(b)
    return out


def _legacy_files() -> list[Path]:
    """The list files of the former per-file folder in name order; none without it."""
    folder = legacy_folder()
    if not folder.is_dir():
        return []
    return [d for d in sorted(folder.iterdir()) if d.is_file() and d.suffix in (".txt", ".gz")]


def _legacy_category(file: Path) -> str:
    """The category a former list file goes to: the user's for ``nutzervokabular*``,
    else the file's stem."""
    return CATEGORY_USER if file.stem.lower().startswith("nutzervokabular") else file.stem


def _mark_adopted(file: Path) -> None:
    """Rename a taken-over list to ``*.uebernommen`` so that it is not read twice; a file
    that cannot be renamed stays (its terms are in the table already)."""
    try:
        file.rename(file.with_name(file.name + ".uebernommen"))
    except OSError:
        pass


class Vocabulary:
    """Reads and writes the vocabulary table of the pseudonym database."""

    def __init__(self, path: str | Path | None = None, seed: bool = True) -> None:
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
        from pseudonymed.pseudonyms.store import _SCHEMA  # the whole store, not only our tables

        self.connection.executescript(_SCHEMA)
        self.connection.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if seed and self.count() == 0:
            self.seed()
        if seed:
            from pseudonymed.detection import lexicon  # classes and the identifiers step

            lexicon.ensure(self.connection)

    def close(self) -> None:
        """Close the database connection."""
        self.connection.close()

    def __enter__(self) -> Vocabulary:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -----------------------------------------------------------------------------------
    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        r = self.connection.execute(
            "SELECT wert FROM vokabular_meta WHERE schluessel = 'version'").fetchone()
        return int(r[0]) if r else 0

    def count(self) -> int:
        """How many terms the table holds."""
        return int(self.connection.execute("SELECT COUNT(*) FROM vokabular").fetchone()[0])

    def categories(self) -> list[tuple[str, int]]:
        """Every category with its number of terms, the largest first."""
        return [(k, n) for k, n in self.connection.execute(
            "SELECT kategorie, COUNT(*) FROM vokabular GROUP BY kategorie"
            " ORDER BY COUNT(*) DESC, kategorie")]

    def all_items(self, search: str = "", category: str = "") -> list[tuple[str, str, str]]:
        """Rows (term, category, added) in alphabetical order, narrowed to terms that
        contain ``search`` and to one category when given."""
        sql, values = "SELECT begriff, kategorie, angelegt FROM vokabular", []
        conditions = []
        if search:
            conditions.append("begriff LIKE ? ESCAPE '\\'")
            values.append("%" + search.replace("\\", "\\\\").replace("%", "\\%")
                          .replace("_", "\\_") + "%")
        if category:
            conditions.append("kategorie = ?")
            values.append(category)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY begriff COLLATE NOCASE"
        return [tuple(r) for r in self.connection.execute(sql, values)]

    def per_category(self) -> dict[str, list[str]]:
        """Terms grouped by category, bundled categories first (for the matcher's sources)."""
        groups: dict[str, list[str]] = {}
        for term, category in self.connection.execute(
                "SELECT begriff, kategorie FROM vokabular"):
            groups.setdefault(category, []).append(term)
        order = [k for _p, k in SEED_LISTS] + [CATEGORY_EPONYMS, CATEGORY_USER]
        return {k: groups[k] for k in order if k in groups} | {
            k: v for k, v in sorted(groups.items()) if k not in order}

    def contains(self, term: str) -> bool:
        """Whether the term is in the table (case-insensitively)."""
        return self.connection.execute("SELECT 1 FROM vokabular WHERE begriff = ?",
                                       (term.strip(),)).fetchone() is not None

    # -- writing -----------------------------------------------------------------------------------
    def _bump(self) -> None:
        self.connection.execute(
            "INSERT INTO vokabular_meta (schluessel, wert) VALUES ('version', '1')"
            " ON CONFLICT(schluessel) DO UPDATE SET wert = CAST(CAST(wert AS INTEGER) + 1 AS TEXT)")

    def add(self, terms: Iterable[str], category: str = CATEGORY_USER) -> int:
        """Insert what is new (case-insensitively); returns how many were new."""
        cleaned = _clean(terms)
        if not cleaned:
            return 0
        now = dt.datetime.now().isoformat(timespec="seconds")
        before = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO vokabular (begriff, kategorie, angelegt) VALUES (?, ?, ?)",
            [(b, category.strip() or CATEGORY_USER, now) for b in cleaned])
        new = self.connection.total_changes - before
        if new:
            self._bump()
        self.connection.commit()
        return new

    def remove(self, terms: Iterable[str]) -> int:
        """Delete the given terms; returns how many rows went."""
        before = self.connection.total_changes
        self.connection.executemany("DELETE FROM vokabular WHERE begriff = ?",
                                    [(b.strip(),) for b in terms if b.strip()])
        n = self.connection.total_changes - before
        if n:
            self._bump()
        self.connection.commit()
        return n

    def rename(self, old: str, new: str) -> bool:
        """Rename one term; False when the new spelling is too short or taken by another
        term (a change of case only is allowed)."""
        new = " ".join(new.split())
        if len(new) < MIN_LENGTH or (new.casefold() != old.casefold() and self.contains(new)):
            return False
        self.connection.execute("UPDATE vokabular SET begriff = ? WHERE begriff = ?", (new, old))
        self._bump()
        self.connection.commit()
        return True

    def set_category(self, terms: Iterable[str], category: str) -> int:
        """Move the given terms to a category (the user's when empty); returns how many
        rows changed."""
        category = category.strip() or CATEGORY_USER
        before = self.connection.total_changes
        self.connection.executemany("UPDATE vokabular SET kategorie = ? WHERE begriff = ?",
                                    [(category, b) for b in terms])
        n = self.connection.total_changes - before
        if n:
            self._bump()
        self.connection.commit()
        return n

    # -- CSV ---------------------------------------------------------------------------------------
    def export_csv(self, path: Path, category: str = "") -> int:
        """Write ``begriff,kategorie`` rows (comma, UTF-8, header line)."""
        lines = self.all_items(category=category)
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["begriff", "kategorie"])
            w.writerows((b, k) for b, k, _a in lines)
        return len(lines)

    def import_csv(self, path: Path, category: str = CATEGORY_USER) -> tuple[int, int]:
        """Read a CSV (``begriff[,kategorie]``, header optional, comma, semicolon or tab) or a
        plain list (one term per line); returns (new, read).  A category column in the
        file wins over ``category``."""
        read_in = list_import.read_list(path, "kategorie", category)
        new = sum(self.add(terms, cat) for cat, terms in read_in.groups.items())
        return new, read_in.lines

    # -- bundled lists ---------------------------------------------------------------------------
    def seed(self) -> dict[str, int]:
        """Load the bundled lists and the eponyms, take over lists of the old folder once."""
        from pseudonymed.detection.ner import EPONYMS

        counts: dict[str, int] = {}
        for path, category in SEED_LISTS:
            if path.exists():
                counts[category] = self.add(_rows(path), category)
        counts[CATEGORY_EPONYMS] = self.add((e for e in EPONYMS if len(e) >= 4), CATEGORY_EPONYMS)
        for file in _legacy_files():
            category = _legacy_category(file)
            counts[category] = counts.get(category, 0) + \
                self.add(_rows(file), category)
            _mark_adopted(file)
        self.connection.execute(
            "INSERT OR REPLACE INTO vokabular_meta (schluessel, wert) VALUES ('grundbestand', ?)",
            (dt.datetime.now().isoformat(timespec="seconds"),))
        self._bump()
        self.connection.commit()
        return counts
