"""The protected vocabulary as packages of lexterm's store.

Every term the guesswork (NER, fuzzy search) must leave alone lives in the store of lexterm
(``lexterm.sqlite``, a store of lexcor's): a category is a package of that store, known by
its title, and a term is a concept whose code is the term itself.  On first use the bundled
lists are installed as packages (Wikidata medicine, body parts, findings terms, eponyms);
from then on the store is the truth: the vocabulary step shows, sorts, edits, adds and
deletes terms and exchanges CSV.  What a user deletes is hidden in the store's overlay and
stays hidden when the package is installed again; what a user adds goes into the store's own
list of the category.  The store's version counter tells
:func:`pseudonymed.detection.medical_terms.standard` when to rebuild its matcher.

Until November 2026 the vocabulary was a table of the pseudonym database.  A database that
still has that table hands its content over on first use (:meth:`Vocabulary.adopt`): the
store is made to hold exactly the table's terms, the matcher's index over the store is
compared with the one over the table, and only then is the table dropped.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import string
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import lexterm
from lexcor import Change, Concept, Cover, Entry, Package, Store, StoreError, Term
from lexcor.store import OVERLAY_FORMAT

from pseudonymed import environment
from pseudonymed.core.text import say
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
BUNDLED = (CATEGORY_MEDICINE, CATEGORY_BODY_PARTS, CATEGORY_FINDINGS, CATEGORY_EPONYMS)
# whose a bundled category is, and whether it may be passed on
_ORIGIN = {
    CATEGORY_MEDICINE: ("Wikidata", "CC0 1.0", True),
    CATEGORY_BODY_PARTS: ("PseudonyMed", "AGPL-3.0-or-later", False),
    CATEGORY_FINDINGS: ("PseudonyMed", "AGPL-3.0-or-later", False),
    CATEGORY_EPONYMS: ("PseudonyMed", "AGPL-3.0-or-later", False),
}
STORE_NAME = "lexterm.sqlite"
LANGUAGE = "de"
MIN_LENGTH = 3
MANY = 100  # from this many terms on, hiding them is written to the overlay in one step
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
Item = tuple[str, str]  # category, term


class VocabularyError(ValueError):
    """The store does not hold the vocabulary it was to take over; the message is for the
    user."""


def default_path() -> Path:
    """The store that holds the vocabulary: ``lexterm.sqlite`` in the folder of the lex
    stores (:func:`pseudonymed.environment.lex_folder`)."""
    return environment.lex_folder() / STORE_NAME


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


def _key(term: str) -> str:
    """What makes two spellings one term of the vocabulary: the letters A to Z without
    their case, as the table the vocabulary came from compared them ("Gesäss" and "Gesäß"
    stay two terms)."""
    return term.translate(_ASCII_LOWER)


@cache
def bundled() -> dict[str, tuple[str, ...]]:
    """The bundled categories with their terms, as first use installs them: the lists in
    their order (Wikidata medicine, body parts, findings terms, eponyms), a term that an
    earlier list has left out of the later ones."""
    from pseudonymed.detection.ner import EPONYMS

    lists = [(_rows(path), category) for path, category in SEED_LISTS if path.exists()]
    lists.append((iter(sorted(e for e in EPONYMS if len(e) >= 4)), CATEGORY_EPONYMS))
    held: set[str] = set()
    out: dict[str, tuple[str, ...]] = {}
    for terms, category in lists:
        new = [t for t in _clean(terms) if _key(t) not in held]
        held.update(_key(t) for t in new)
        out[category] = tuple(new)
    return out


def _exact(terms: Iterable[str]) -> list[str]:
    """Terms as they are, each once, with their spaces collapsed and none empty."""
    return list(dict.fromkeys(t for t in (" ".join(str(b).split()) for b in terms) if t))


def _package(category: str, terms: Iterable[str]) -> Package:
    """A category as a package of the store: a plain list in which a term is its own code.
    It has no URL of its own (the store gives it one from its name), and it may be passed
    on only while it is a bundled list that may and holds nothing but that list's terms."""
    publisher, licence, free = _ORIGIN.get(category, ("", "", False))
    codes = list(terms)
    return Package(
        url="", name=category, title=category, publisher=publisher, copyright=licence,
        language=LANGUAGE,
        redistributable=free and set(codes) <= set(bundled().get(category, ())),
        concepts=[Concept(t, t, "", "", [Term(t, LANGUAGE)]) for t in codes])


def table_terms(database: Path) -> dict[str, list[str]] | None:
    """The terms by category of the table a pseudonym database kept the vocabulary in until
    November 2026 (a row without a category is the user's own); None when the database is
    not there or has no such table."""
    if not Path(database).is_file():
        return None
    with closing(sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro",
                                 uri=True)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                              " AND name = 'vokabular'").fetchone() is None:
            return None
        groups: dict[str, list[str]] = {}
        for term, category in connection.execute(
                "SELECT begriff, kategorie FROM vokabular ORDER BY rowid"):
            groups.setdefault(category.strip() or CATEGORY_USER, []).append(term)
    return groups


def drop_table(database: Path) -> None:
    """Remove the former vocabulary table and its counter from a pseudonym database."""
    with closing(sqlite3.connect(str(database))) as connection:
        connection.executescript(
            "DROP TABLE IF EXISTS vokabular; DROP TABLE IF EXISTS vokabular_meta;")


@dataclass(frozen=True)
class _View:
    """The vocabulary as the store shows it at one version: every term of every category
    with the changes of the overlay that take it out of force, the terms by their key, and
    the rows (key, term, category) in the order of the alphabet."""

    version: int
    origins: dict[Item, list[Change]]
    by_key: dict[str, list[Item]]
    rows: list[tuple[str, str, str]]


def _origin(entry: Entry, only_term: bool, added: bool) -> Change:
    """The change of the overlay that takes a term out of force: for a term the overlay
    itself added, that line (to be taken back); else a line that hides it, its whole concept
    when the term is the concept's code and its only term, the term alone otherwise."""
    if added:
        return Change(entry.system, entry.code, "add", entry.term, entry.language)
    if only_term and entry.code == entry.term:
        return Change(entry.system, entry.code, "hide")
    return Change(entry.system, entry.code, "hide", entry.term, entry.language)


def _read(store: Store) -> _View:
    """The terms in force in a store: German ones and those that name no language."""
    titles = {info.url: info.title or info.url for info in store.packages()}
    entries = list(store.entries())
    terms = Counter((e.system, e.code) for e in entries)
    added = {(c.url, c.code, c.value, c.language) for c in store.overlay() if c.action == "add"}
    origins: dict[Item, list[Change]] = {}
    for e in entries:
        if e.language in (LANGUAGE, ""):
            origins.setdefault((titles[e.system], e.term), []).append(_origin(
                e, terms[(e.system, e.code)] == 1, (e.system, e.code, e.term, e.language) in added))
    rows = sorted((_key(term), term, category) for category, term in origins)
    by_key: dict[str, list[Item]] = {}
    for key, term, category in rows:
        by_key.setdefault(key, []).append((category, term))
    return _View(store.version(), origins, by_key, rows)


_views: dict[str, _View] = {}  # the store read last, by its key: a window asks many times


def _view(store: Store) -> _View:
    """The terms in force in a store, read again only when the store has changed."""
    seen = _views.get(store.key)
    if seen is None or seen.version != store.version():
        _views.clear()
        seen = _views[store.key] = _read(store)
    return seen


class Vocabulary:
    """Reads and writes the protected vocabulary in lexterm's store."""

    def __init__(self, path: str | Path | None = None, seed: bool = True,
                 readonly: bool = False) -> None:
        """Open the store in the file ``path`` (default: ``lexterm.sqlite`` in the folder
        of the lex stores).  With ``seed`` the bundled lists the store lacks are installed,
        and before that the default store takes over a vocabulary that the pseudonym
        database still keeps in a table.  ``readonly`` reads a store that exists and writes
        nothing; lexcor's ``StoreError`` says that there is none."""
        self.path = Path(path) if path else default_path()
        self.store = lexterm.open_store(self.path, readonly=readonly)
        if readonly:
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if seed:
            try:
                self._prepare(default=path is None)
            except BaseException:
                self.close()
                raise

    def _prepare(self, default: bool) -> None:
        """What first use owes the store: the vocabulary a pseudonym database still keeps
        (the default store takes it over from the default database), then the bundled
        lists the store lacks."""
        if default:
            from pseudonymed.pseudonyms.store import default_path as database

            self.adopt(database())
        self.seed()

    def close(self) -> None:
        """Close the store."""
        self.store.close()

    def __enter__(self) -> Vocabulary:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -----------------------------------------------------------------------------------
    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        return self.store.version()

    def _view(self) -> _View:
        """The terms in force."""
        return _view(self.store)

    def count(self) -> int:
        """How many terms the vocabulary holds."""
        return len(self._view().rows)

    def categories(self) -> list[tuple[str, int]]:
        """Every category with its number of terms, the largest first."""
        counts = Counter(category for _sort, _term, category in self._view().rows)
        return sorted(counts.items(), key=lambda kn: (-kn[1], kn[0]))

    def all_items(self, search: str = "", category: str = "") -> list[tuple[str, str, str]]:
        """Rows (term, category, added) in alphabetical order, narrowed to terms that
        contain ``search`` and to one category when given.  The store keeps no day for a
        term: ``added`` is empty."""
        wanted = _key(search)
        return [(term, k, "") for key, term, k in self._view().rows
                if (not category or k == category) and wanted in key]

    def per_category(self) -> dict[str, list[str]]:
        """Terms grouped by category, bundled categories first (for the matcher's sources)."""
        groups: dict[str, list[str]] = {}
        for _sort, term, category in self._view().rows:
            groups.setdefault(category, []).append(term)
        order = [k for _p, k in SEED_LISTS] + [CATEGORY_EPONYMS, CATEGORY_USER]
        return {k: groups[k] for k in order if k in groups} | {
            k: v for k, v in groups.items() if k not in order}

    def contains(self, term: str) -> bool:
        """Whether the term is in the vocabulary (case-insensitively)."""
        return _key(term.strip()) in self._view().by_key

    def _found(self, terms: Iterable[str]) -> list[Item]:
        """The terms in force that these spellings name, each once."""
        by_key = self._view().by_key
        return list(dict.fromkeys(
            item for term in terms for item in by_key.get(_key(term.strip()), [])))

    # -- writing -----------------------------------------------------------------------------------
    def _hides(self, url: str) -> dict[str, list[Change]]:
        """The overlay's lines that hide a concept of a package or the term that is its
        code, by code."""
        hides: dict[str, list[Change]] = {}
        for change in self.store.overlay(url):
            if change.action == "hide" and change.value in ("", change.code):
                hides.setdefault(change.code, []).append(change)
        return hides

    def _hide(self, items: Iterable[Item]) -> None:
        """Take terms out of force through the overlay, wherever they come from: hidden,
        or, where the overlay had added them, taken back."""
        origins = self._view().origins
        lines = [origin for item in items for origin in origins[item]]
        for line in lines:
            if line.action == "add":
                self.store.undo(line)
        self._hide_lines([line for line in lines if line.action == "hide"])

    def _hide_lines(self, lines: list[Change]) -> None:
        """Put hiding lines into the overlay: one by one, or from :data:`MANY` on as one
        overlay taken in at once.  The lines are the same; but the store writes every
        single change to the disk on its own, and a whole category would take minutes."""
        if len(lines) < MANY:
            for line in lines:
                self.store.hide(line.url, line.code, line.value, line.language)
            return
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "overlay.json"
            file.write_text(json.dumps({"lexcor-overlay": OVERLAY_FORMAT,
                                        "changes": [asdict(line) for line in lines]}),
                            encoding="utf-8")
            self.store.import_overlay(file)

    def _put(self, category: str, terms: list[str]) -> None:
        """Bring terms into force in the store's own list of a category: the list is
        written again with the terms it lacks (a new category is a new list), and a term
        that is merely hidden there is shown again."""
        url = self.store.local_url(category)
        if any(info.url == url for info in self.store.packages()):
            self._extend(url, category, terms)
        else:
            self.store.add_package(_package(category, terms))
        hides = self._hides(url)
        for change in (c for term in terms for c in hides.get(term, [])):
            self.store.undo(change)

    def _extend(self, url: str, category: str, terms: list[str]) -> None:
        """Write the store's own list of a category again with the terms it lacks; it may
        be passed on only while nothing but its bundled terms is in it."""
        package = self.store.package(url)
        held = {concept.code for concept in package.concepts}
        missing = [t for t in terms if t not in held]
        if not missing:
            return
        package.concepts += [Concept(t, t, "", "", [Term(t, LANGUAGE)]) for t in missing]
        package.redistributable = package.redistributable and \
            set(missing) <= set(bundled().get(category, ()))
        self.store.add_package(package)

    def add(self, terms: Iterable[str], category: str = CATEGORY_USER) -> int:
        """Insert what is new (case-insensitively); returns how many were new."""
        by_key = self._view().by_key
        new = [t for t in _clean(terms) if _key(t) not in by_key]
        if new:
            self._put(category.strip() or CATEGORY_USER, new)
        return len(new)

    def remove(self, terms: Iterable[str]) -> int:
        """Delete the given terms: they are hidden in the overlay and stay away when their
        list is installed again.  Returns how many went."""
        found = self._found(terms)
        self._hide(found)
        return len(found)

    def rename(self, old: str, new: str) -> bool:
        """Rename one term; False when the old one is unknown or the new spelling is too
        short or taken by another term (a change of case only is allowed)."""
        new = " ".join(new.split())
        found = self._found([old])
        if not found or len(new) < MIN_LENGTH or (
                new.casefold() != old.casefold() and self.contains(new)):
            return False
        self._hide(found)
        self._put(found[0][0], [new])
        return True

    def set_category(self, terms: Iterable[str], category: str) -> int:
        """Move the given terms to a category (the user's when empty); returns how many
        terms were found."""
        category = category.strip() or CATEGORY_USER
        found = self._found(terms)
        moving = [item for item in found if item[0] != category]
        if moving:
            self._hide(moving)
            self._put(category, list(dict.fromkeys(term for _k, term in moving)))
        return len(found)

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

    def replace_from_csv(self, path: Path) -> int:
        """Make the vocabulary exactly what a CSV written by :meth:`export_csv` says
        (:meth:`replace`); returns how many terms it holds."""
        read_in = list_import.read_list(path, "kategorie", CATEGORY_USER)
        self.replace(read_in.groups)
        return read_in.lines

    # -- bundled lists ---------------------------------------------------------------------------
    def seed(self) -> dict[str, int]:
        """Install every bundled list the store has no package of (none of its title, and
        none in the place of the store's own list of that name); what the overlay hides in
        one stays hidden.  Returns the number of terms of each list installed."""
        held = {key for info in self.store.packages() for key in (info.title, info.url)}
        missing = [k for k in BUNDLED
                   if k not in held and self.store.local_url(k) not in held]
        return {k: self.store.add_package(_package(k, bundled()[k])).concepts
                for k in missing if k in bundled()}

    def restore(self) -> dict[str, int]:
        """Bring back what is missing of the bundled lists: the terms deleted from them,
        unless they stand in another category now.  Returns how many came back per list."""
        return {category: self.add(terms, category) for category, terms in bundled().items()}

    # -- the vocabulary as a whole ---------------------------------------------------------------
    def replace(self, groups: Mapping[str, Iterable[str]]) -> None:
        """Make the vocabulary exactly these terms per category, then compare the matcher's
        index over the store with the one over these terms (:class:`VocabularyError` when
        they differ).  A bundled category is its bundled list with what the list lacks
        added and what is not wanted hidden; any other category is a list of its terms;
        whatever else is in force in the store is hidden.  Categories whose names differ
        in case only are one list of the store, under the bundled name or the first."""
        wanted = self._by_list(groups)
        for category in dict.fromkeys([*bundled(), *wanted]):
            self._settle(category, wanted.get(category, []))
        keep = {(category, term) for category, terms in wanted.items() for term in terms}
        self._hide([item for item in self._view().origins if item not in keep])
        self._check(keep)

    def _by_list(self, groups: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
        """Terms by the name of the store's own list they go to: the store tells lists
        apart by their names without case, and a category without a name is the user's."""
        names = {self.store.local_url(category): category for category in bundled()}
        wanted: dict[str, list[str]] = {}
        for category, terms in groups.items():
            category = category.strip() or CATEGORY_USER
            name = names.setdefault(self.store.local_url(category), category)
            wanted[name] = _exact([*wanted.get(name, []), *terms])
        return wanted

    def _check(self, wanted: set[Item]) -> None:
        """The matcher over the store must be the matcher over the terms wanted."""
        expected = Cover((term for _category, term in wanted), lexterm.EPONYM_HEADS)
        found = lexterm.cover(self.store)
        if found.single != expected.single or found.phrases != expected.phrases:
            raise VocabularyError(say(
                "Das Fachvokabular in der Ablage von lexterm stimmt nicht mit dem überein, was"
                " sie übernehmen sollte. Die Quelle (Tabelle oder Sicherung) ist unverändert.",
                "The vocabulary in lexterm's store does not agree with what it was to take"
                " over. The source (table or backup) is unchanged."))

    def _settle(self, category: str, wanted: list[str]) -> None:
        """The store's own list of a category with exactly these terms in force."""
        base = bundled().get(category, ())
        known = set(base)
        codes = [*base, *(t for t in wanted if t not in known)]
        url = self.store.add_package(_package(category, codes)).url
        hides = self._hides(url)
        keep = set(wanted)
        for change in (c for code in codes if code in keep for c in hides.get(code, [])):
            self.store.undo(change)
        self._hide_lines([Change(url, code, "hide") for code in codes
                          if code not in keep and code not in hides])

    def adopt(self, database: Path) -> int | None:
        """Take over the vocabulary of a pseudonym database that still keeps it in a table:
        the store is made to hold exactly the table's terms (:meth:`replace`, which checks
        the result), then the table is dropped.  An empty table was never filled and is
        only dropped.  Returns how many terms moved, None when there is no table.  Whatever
        stops the take-over is a :class:`VocabularyError`: nobody is to go on with another
        vocabulary while the user's own waits in the table."""
        groups = table_terms(database)
        if groups is None:
            return None
        try:
            if groups:
                self.replace(groups)
            drop_table(database)
        except (StoreError, sqlite3.Error, OSError) as error:
            raise VocabularyError(say(
                "Das Fachvokabular ließ sich nicht aus der Pseudonym-Datenbank in die Ablage"
                f" von lexterm übernehmen ({error}). Die Tabelle bleibt erhalten.",
                "The vocabulary could not be taken over from the pseudonym database into"
                f" lexterm's store ({error}). The table stays as it is.")) from error
        return sum(len(terms) for terms in groups.values())
