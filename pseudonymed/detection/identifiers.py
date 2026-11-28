"""Identifiers beyond the patient's own data: professions, hospitals, studies, titles.

The GeMTeX annotation guideline for identifying information (2nd version, 7 September
2025, doi:10.5281/zenodo.15747389) marks more than the patient's names and numbers: the
patient's PROFESSION, hospitals and practices (LOCATION_HOSPITAL), studies the patient
takes part in (ID) and academic titles (NAME_TITLE).  Such identifiers live in the store of
lexident, the library that recognises them (``lexident.sqlite``, a store of lexcor's): the
bundled lists are installed there as packages, the user's own entries are lists of the
store's own, and a bundled entry the user deletes is hidden in the store's overlay.  All of
them are lists the store itself gave a URL
(:func:`pseudonymed.detection.store_table.own_lists`); a package another program installed
there under a URL of its own is not PseudonyMed's and is neither shown, looked for nor
changed.  The identifiers step shows them and lets the user add, change and delete entries
(:class:`IdentifierTable`), and the analysis redacts what lexident finds by default.  Every
hit is a suggestion that the visual review may discard.

A bare dictionary match would redact half of every letter ("Bauer", "Leiter", "CARE"),
so each class has a rule of context, which is lexident's (it was written here):

* hospitals (``hospital``): the official name, or its main part, wherever it stands, and
  "Klinikum <town>" for a town that has a listed hospital;
* professions (``occupation``): only next to a cue such as "arbeitet als", "von Beruf",
  "gelernte", or in a social-history paragraph;
* studies (``study``): registry numbers (NCT, DRKS, ISRCTN, EU CT, EudraCT) always;
  acronyms only in a registered spelling and with a study cue nearby;
* titles (``title``): only directly before or after a person the analysis found already
  (patient, relatives, referrers, NER).  This pass runs after the NER;
* every other class, including the user's own (``other``): wherever it stands.

lexident gives a place every class whose rule holds there.  PseudonyMed names a place by
one class, for its pseudonym and its rule (:func:`category_of`): a hospital before a
profession before a study before a class of the user's own (by name) before ``other``.
The place is redacted whichever class names it.  An
employment status (berentet, arbeitslos) is no PROFESSION in GeMTeX and stays.  Identifier
hits are guesses like the NER's: they are dashed in the review and are not part of the
final re-check.

Classes are stored in English (:mod:`pseudonymed.core.names` gives their labels and reads
a German class name typed or imported); the word in a pseudonym is in the language of the
document ("[Klinik <code>]").
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import lexident
from lexcor import Change, Store, StoreError
from lexcor import Entry as Listed
from lexident.forms import norm
from lexident.types import HOSPITAL_TOWN, ID, LOCATION_HOSPITAL, OTHER, PROFESSION
from ocrdoc.pagetext import PageText

from pseudonymed import environment
from pseudonymed.core import names
from pseudonymed.core.model import Hit, Kind
from pseudonymed.detection import identifier_lists as lists
from pseudonymed.detection import lexicon, list_import, overlay
from pseudonymed.detection.choice import applied
from pseudonymed.detection.identifier_lists import CATEGORY_HOSPITAL, CATEGORY_OTHER, Row
from pseudonymed.detection.store_table import StoreTable, own_lists
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

CATEGORY_PROFESSION = "occupation"
CATEGORY_STUDY = lists.CATEGORY_STUDY
CATEGORY_TITLE = "title"
CATEGORIES = (CATEGORY_HOSPITAL, CATEGORY_PROFESSION, CATEGORY_STUDY, CATEGORY_TITLE,
              CATEGORY_OTHER)
# the word in the pseudonym of a bundled class, in the language of the documents
PREFIX = {CATEGORY_HOSPITAL: "Klinik", CATEGORY_PROFESSION: "Beruf", CATEGORY_STUDY: "Studie",
          CATEGORY_TITLE: "Titel", CATEGORY_OTHER: "Identifikator"}
STORE_NAME = "lexident.sqlite"
MIN_LENGTH = 3
# the class a place is named by when it carries several: these first, in this order (the
# name of a hospital stands for itself, a profession has its cue right beside it, a study
# only somewhere near), then a class of the user's own by its name, and the catch-all last
_RANK = (LOCATION_HOSPITAL, PROFESSION, ID)
_PERSON_KINDS = (Kind.NAME, Kind.FIRST_NAME, Kind.SURNAME, Kind.BIRTH_NAME)
Recogniser = lexident.Recogniser
Key = tuple[str, str, str]  # term, class, identifier


def default_path() -> Path:
    """The store that holds the identifiers: ``lexident.sqlite`` in the folder of the lex
    stores (:func:`pseudonymed.environment.lex_folder`)."""
    return environment.lex_folder() / STORE_NAME


def rule_name(category: str) -> str:
    """Rule id of a class: ``identifier.hospital``, ``identifier.other`` …; a class of the
    user's own by its name."""
    word = category if category in CATEGORIES else re.sub(r"\W+", "_", norm(category)).strip("_")
    return "identifier." + (word or CATEGORY_OTHER)


def prefix(category: str) -> str:
    """The word in the pseudonym of a class: ``[Klinik <code>]`` for a hospital, a class of
    the user's own by its name, ``[Identifikator <code>]`` for one without a name."""
    return PREFIX.get(category, category.strip() or PREFIX[CATEGORY_OTHER])


def category_of(classes: Iterable[str]) -> str:
    """The one class PseudonyMed names a place by that carries these classes of lexident's:
    a hospital before a profession before a study before a class of the user's own (the
    first by name) before the catch-all."""
    first = min(classes, key=lambda cls: (
        _RANK.index(cls) if cls in _RANK else len(_RANK) + (cls == OTHER), cls))
    return lists.category_of(first)


# -- hits -----------------------------------------------------------------------------------------
def _hit(page: PageText, a: int, b: int, category: str, assignment: PseudonymAssignment,
         source: str) -> Hit:
    """The hit for ``a:b`` with its pseudonym, rectangles and font size."""
    text = page.text[a:b]
    return Hit(
        page=page.number, start=a, end=b, text=text, kind=Kind.IDENTIFIER,
        rule=rule_name(category), pseudonym=assignment.identifier(prefix(category), text),
        rects=[tuple(r) for r in page.rects(a, b)], font_size=page.font_size(a, b),
        source=source)


def _free(a: int, b: int, occupied: list[Hit]) -> bool:
    """Whether no existing hit overlaps ``a:b``."""
    return not any(f.end > f.start and a < f.end and b > f.start for f in occupied)


def find_identifiers(page: PageText, recogniser: Recogniser, assignment: PseudonymAssignment,
                     existing: list[Hit], source: str = "text") -> list[Hit]:
    """Hospitals, professions, studies and the user's own identifiers on one page; what
    overlaps an existing hit is left to that hit."""
    matches: list[Hit] = []
    for k in recogniser.candidates(page.text):
        if _free(k.start, k.end, existing + matches):
            matches.append(_hit(page, k.start, k.end, category_of(k.classes), assignment,
                                source))
    return matches


def find_titles(page: PageText, recogniser: Recogniser, assignment: PseudonymAssignment,
                existing: list[Hit], source: str = "text") -> list[Hit]:
    """Titles directly before a person ("Prof. Dr. med. Person K7Q4MX") or after it
    ("…, Dipl.-Ing."), run after the NER; adjacent titles become one hit, and a chain that
    overlaps an existing hit is left out."""
    persons = [(f.start, f.end) for f in existing if f.end > f.start
               and (f.kind in _PERSON_KINDS or f.rule == "ner.person")]
    if not persons:
        return []
    chains = lexident.titles(page.text, recogniser.candidates(page.text, title=True), persons,
                             [(f.start, f.end) for f in existing])
    return [_hit(page, chain.start, chain.end, CATEGORY_TITLE, assignment, source)
            for chain in chains]


# -- the table behind the identifiers step ------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One identifier of the step: the term, its class, where it came from, the source's
    id of what it names and, for a hospital, the JSON addition of the reference data."""

    term: str
    category: str
    source: str
    code: str
    extra: str

    @property
    def key(self) -> tuple[str, str, str]:
        """What identifies the row: (term, class, id)."""
        return self.term, self.category, self.code

    @property
    def own(self) -> bool:
        """Whether the user entered or changed the row (bundled rows are hidden when
        deleted and can be brought back, the user's own are gone)."""
        return self.source == lexicon.USER

    @property
    def details(self) -> str:
        """Short description from the reference data (place, registry number, meaning)."""
        description = _DETAILS.get(self.category)
        return description(self) if description else ""

    def details_in(self, language: str) -> str:
        """The description in the interface language: the kind of a title by its label."""
        if self.category != CATEGORY_TITLE:
            return self.details
        return " · ".join(names.label(part, language) for part in self.details.split(" · "))


def _hospital_details(e: Entry) -> str:
    """Town and country from a hospital's JSON addition; empty without one."""
    if not e.extra:
        return ""
    try:
        z = json.loads(e.extra)
    except ValueError:
        return ""
    return f"{z.get('place', '')} ({z.get('country', '')})".replace(" ()", "").strip()


def _study_details(e: Entry) -> str:
    """The registry number behind the registry's name ("ClinicalTrials.gov:NCT…")."""
    return e.code.split(":", 1)[1] if ":" in e.code else ""


def _title_details(e: Entry) -> str:
    """The fields of a title from its ``|``-separated id, "all" (every country) left out."""
    if "|" not in e.code:
        return ""
    return " · ".join(t for t in e.code.split("|") if t and t != "all")


def _profession_details(e: Entry) -> str:
    """The KldB 2010 code of a profession when the id is one."""
    return f"KldB {e.code}" if e.code.isdigit() else ""


_DETAILS: dict[str, Callable[[Entry], str]] = {
    CATEGORY_HOSPITAL: _hospital_details, CATEGORY_STUDY: _study_details,
    CATEGORY_TITLE: _title_details, CATEGORY_PROFESSION: _profession_details,
}



def _place_from(extra: str) -> str:
    """The town in a hospital's JSON addition; empty without one or when unreadable."""
    return lists.town_of(lists.addition_of(extra))


@dataclass(frozen=True)
class _Place:
    """Where an identifier stands in the store: package, concept, term and its language,
    and whether the package is a list of the user's own."""

    url: str
    code: str
    term: str
    language: str
    own: bool


@dataclass(frozen=True)
class _View:
    """The identifiers as the store shows them in one state: the entries in the order of
    the alphabet, and the places behind each key."""

    state: tuple[int, int]
    entries: list[Entry]
    places: dict[Key, list[_Place]]


def _own_name(category: str) -> str:
    """The name of the user's own list of a class."""
    return f"{lexicon.USER}: {category}"


def _urls(store: Store) -> list[str]:
    """The URLs of the store's own lists: only these are PseudonyMed's identifiers."""
    return [info.url for info in own_lists(store)]


def _user_urls(store: Store) -> set[str]:
    """The lists of the user's own entries among the store's own lists."""
    return {info.url for info in own_lists(store)
            if info.title.startswith(f"{lexicon.USER}: ")}


def _normal(key: Key) -> Key:
    """A key as the view files it: the letters A to Z of its term without their case."""
    return lists.key(" ".join(key[0].split())), key[1], key[2]


def _state(store: Store) -> tuple[int, int]:
    """What tells one state of a store's file from another: the counter every change
    raises, and when the file was written last.  A file put in a store's place, as a
    restored backup is, may well carry the counter of the one before."""
    return store.version(), store.path.stat().st_mtime_ns


Said = dict[tuple[str, str], Mapping[str, str]]  # (package, concept): its properties


def _said(store: Store, urls: Iterable[str]) -> Said:
    """What some packages of a store say of each of their concepts."""
    return {(url, concept.code): concept.properties
            for url in urls for concept in store.package(url).concepts}


def _entry(e: Listed, source: str, said: Mapping[str, str]) -> Entry:
    """A term in force as an entry of the table, with what its package says of it."""
    addition = lists.unfold(said, e.term)
    return Entry(e.term, lists.category_of(e.cls), source, "" if e.code == e.term else e.code,
                 json.dumps(addition, ensure_ascii=False) if addition else "")


def _read(store: Store) -> _View:
    """The identifiers in force in the store's own lists.  The towns that have a hospital
    are none: they only form names of hospitals.  A hospital carries what its package says
    of it."""
    titles = {info.url: info.title or info.url for info in own_lists(store)}
    own = _user_urls(store)
    found = [e for e in store.entries(titles) if e.cls != HOSPITAL_TOWN]
    said = _said(store, sorted({e.system for e in found if e.cls == LOCATION_HOSPITAL}))
    entries: list[Entry] = []
    places: dict[Key, list[_Place]] = {}
    for e in found:
        entry = _entry(e, lexicon.USER if e.system in own else titles[e.system],
                       said.get((e.system, e.code), {}))
        entries.append(entry)
        places.setdefault(_normal(entry.key), []).append(
            _Place(e.system, e.code, e.term, e.language, e.system in own))
    entries.sort(key=lambda entry: (lists.key(entry.term), entry.term, entry.category,
                                    entry.code))
    return _View(_state(store), entries, places)


_views: dict[str, _View] = {}  # the store read last, by its key: a window asks many times


def _view(store: Store) -> _View:
    """The identifiers in force in a store, read again only when the store has changed."""
    seen = _views.get(store.key)
    if seen is None or seen.state != _state(store):
        _views.clear()
        seen = _views[store.key] = _read(store)
    return seen


class IdentifierTable(StoreTable):
    """Reads and writes the identifiers in lexident's store.  A bundled entry that is
    deleted or changed is hidden in the store's overlay, so that the next update of the
    bundled data does not bring it back; a changed entry becomes the user's own."""

    def __init__(self, path: str | Path | None = None, seed: bool = True,
                 readonly: bool = False) -> None:
        """Open the store in the file ``path`` (default: ``lexident.sqlite`` in the folder
        of the lex stores).  With ``seed`` the bundled lists whose data changed are
        installed.  ``readonly`` reads a store that exists and writes
        nothing; lexcor's ``StoreError`` says that there is none."""
        self.path = Path(path) if path else default_path()
        self._open(lexident.open_store(self.path, readonly=readonly), seed)

    # -- reading -------------------------------------------------------------------------------
    def count(self) -> int:
        """How many entries the step shows."""
        return len(_view(self.store).entries)

    def categories(self) -> list[tuple[str, int]]:
        """Every class of the entries with its number of entries, the largest first."""
        counts = Counter(entry.category for entry in _view(self.store).entries)
        return sorted(counts.items(), key=lambda kn: (-kn[1], kn[0]))

    def all_items(self, category: str = "") -> list[Entry]:
        """The entries in alphabetical order, of one class when given (its German name
        understood)."""
        category = names.class_name(category) if category else ""
        return [entry for entry in _view(self.store).entries
                if not category or entry.category == category]

    def deleted(self) -> int:
        """How many entries the user deleted or changed that can be brought back (those
        the overlay hides in the store's own lists)."""
        return len({(line.code, line.value) for line in self._hides()})

    def _hides(self) -> list[Change]:
        """The lines of the overlay that hide something in the store's own lists."""
        mine = set(_urls(self.store))
        return [line for line in self.store.overlay()
                if line.action == "hide" and line.url in mine]

    def keys_for(self, term: str, category: str = "") -> list[Key]:
        """Keys of the entries with this term (case-insensitive), in one class or all."""
        wanted = lists.key(" ".join(term.split()))
        category = names.class_name(category) if category else ""
        return [entry.key for entry in _view(self.store).entries
                if lists.key(entry.term) == wanted and (not category
                                                        or entry.category == category)]

    def loaded(self) -> list[tuple[str, int, str]]:
        """The lists that are neither bundled nor the user's own entries, such as the data
        fetched on request: (list, terms, state of its data)."""
        own = _user_urls(self.store)
        return [(info.title, info.terms, info.version) for info in own_lists(self.store)
                if info.url not in own and info.title != lists.TOWNS
                and info.title not in lexicon.FEATURE_SOURCES]

    def _row(self, key: Key) -> Entry | None:
        """The entry with this key; None when there is none in force."""
        wanted = _normal(key)
        return next((entry for entry in _view(self.store).entries
                     if _normal(entry.key) == wanted), None)

    # -- writing -------------------------------------------------------------------------------
    def _rewrite(self, category: str, add: Iterable[Row] = (),
                 drop: Iterable[tuple[str, str]] = ()) -> None:
        """Write the user's own list of a class again with rows added and entries (term
        and identifier) taken out; a list left without a term goes."""
        name = _own_name(category)
        url = self.store.local_url(name)
        held = [info for info in self.store.packages() if info.url == url]
        gone = set(drop)
        rows = [row for row in (lists.rows_of(self.store.package(url).concepts, category)
                                if held else []) if (row.term, row.code) not in gone]
        rows += list(add)
        if rows:
            self.store.add_package(lists.package(name, rows))
        elif held:
            self.store.remove_package(url)
        if category == CATEGORY_HOSPITAL:
            self._settle_towns()

    def _settle_towns(self) -> None:
        """Keep the list of the towns that have a hospital in step with the hospitals in
        force: lexident forms "Klinikum <town>" of them."""
        towns = sorted({_place_from(entry.extra) for entry in _view(self.store).entries
                        if entry.category == CATEGORY_HOSPITAL} - {""})
        url = self.store.local_url(lists.TOWNS)
        held = any(info.url == url for info in self.store.packages())
        if not towns:
            if held:
                self.store.remove_package(url)
        elif not held or [c.code for c in self.store.package(url).concepts] != towns:
            self.store.add_package(lists.towns_package(towns))

    def add(self, terms: Iterable[str], category: str = CATEGORY_OTHER) -> int:
        """Insert the user's own identifiers; returns how many were new.  A class typed in
        German or as its label is the English one (``Klinik`` is ``hospital``)."""
        category = names.class_name(category) or CATEGORY_OTHER
        return self.add_rows(Row(" ".join(str(b).split()), category) for b in terms)

    def add_rows(self, rows: Iterable[Row]) -> int:
        """Insert rows as entries of the user's own, with the source's identifier and what
        the source says where a row carries them (an entry that was a bundled one before
        the user changed it); returns how many were new."""
        places = _view(self.store).places
        new: dict[Key, Row] = {}
        for row in rows:
            key = _normal((row.term, row.category, row.code))
            if len(row.term) >= MIN_LENGTH and not row.term.startswith("#") \
                    and key not in places:
                new.setdefault(key, row)
        for category in dict.fromkeys(row.category for row in new.values()):
            self._rewrite(category, add=[row for row in new.values()
                                         if row.category == category])
        return len(new)

    def own_rows(self) -> list[Row]:
        """The user's own entries as rows, each with the source's identifier and what the
        source says of it when it has them."""
        return [row for url in sorted(_user_urls(self.store))
                for row in lists.rows_of(self.store.package(url).concepts)]

    def remove(self, key: Iterable[Key]) -> int:
        """Delete the entries with these keys: one of the user's own is taken out of its
        list, any other is hidden in the overlay.  Returns how many went."""
        places = _view(self.store).places
        found = list(dict.fromkeys((k, place) for k in map(_normal, key)
                                   for place in places.get(k, [])))
        listed = [(k, place) for k, place in found if not place.own]
        overlay.hide(self.store, [Change(place.url, place.code, "hide", place.term,
                                         place.language) for _k, place in listed])
        self._drop_own([(k, place) for k, place in found if place.own])
        if any(k[1] == CATEGORY_HOSPITAL for k, _place in listed):
            self._settle_towns()
        return len(found)

    def _drop_own(self, found: list[tuple[Key, _Place]]) -> None:
        """Take entries of the user's own out of their lists."""
        own: dict[str, list[tuple[str, str]]] = {}
        for k, place in found:
            own.setdefault(k[1], []).append((place.term, k[2]))
        for category, gone in own.items():
            self._rewrite(category, drop=gone)

    def _target(self, e: Entry, term: str | None, category: str | None) -> tuple[str, str] | None:
        """Term and class an entry is to have after a change; None when the result is too
        short, is the entry as it stands or exists already."""
        new_term = " ".join((e.term if term is None else term).split())
        new_category = (e.category if category is None
                        else names.class_name(category)) or CATEGORY_OTHER
        if len(new_term) < MIN_LENGTH or (new_term, new_category) == (e.term, e.category):
            return None
        same_row = (new_term.casefold(), new_category) == (e.term.casefold(), e.category)
        if not same_row and self._row((new_term, new_category, e.code)) is not None:
            return None
        return new_term, new_category

    def change(self, key: Key, term: str | None = None,
               category: str | None = None) -> Entry | None:
        """Change term or class of one entry and return it as it is now; None when the
        result is too short or exists already.  The entry becomes the user's own and keeps
        the source's identifier and what the source says of it."""
        e = self._row(key)
        target = self._target(e, term, category) if e is not None else None
        if e is None or target is None:
            return None
        new_term, new_category = target
        place = _view(self.store).places[_normal(e.key)][0]
        addition = lists.unfold(self.store.properties(place.url, place.code), place.term)
        self.remove([e.key])
        self._rewrite(new_category, add=[Row(new_term, new_category, e.code, addition)])
        shown = addition if new_category == CATEGORY_HOSPITAL else {}
        return Entry(new_term, new_category, lexicon.USER, e.code,
                     json.dumps(shown, ensure_ascii=False) if shown else "")

    def restore(self) -> int:
        """Bring back what is hidden and install the bundled lists again; returns how many
        deletions were forgotten."""
        lines = self._hides()
        for line in lines:
            self.store.undo(line)
        self.seed(force=True)
        return len({(line.code, line.value) for line in lines})

    def install(self, source: str, rows: Iterable[Row], state: str = "",
                note: str = "") -> int:
        """Take a list in under the name of its source, in place of the one of that name
        (data fetched on the user's request); returns how many terms it holds."""
        terms = self.store.add_package(lists.package(source, rows, state, note)).terms
        self._settle_towns()
        return terms

    # -- the lists as a whole --------------------------------------------------------------------
    def export_lists(self, path: Path) -> int:
        """Write the store's own lists, with what the overlay says of them, into a store of
        their own in the file ``path`` (what a backup carries: no package of another
        program travels); returns how many lists."""
        with lexident.open_store(path) as copy:
            return copy_lists(self.store, copy)

    def take_lists(self, path: Path) -> int:
        """Make the store's own lists exactly those of the store in the file ``path``
        (written by :meth:`export_lists`); packages of other programs stay as they are.
        Returns how many lists came."""
        with lexident.open_store(path, readonly=True) as source:
            return copy_lists(source, self.store)

    # -- CSV -------------------------------------------------------------------------------------
    def export_csv(self, path: Path, category: str = "") -> int:
        """Write ``term,class,source,details`` rows (comma, UTF-8, header line)."""
        lines = self.all_items(category)
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["term", "class", "source", "details"])
            w.writerows((e.term, e.category, e.source, e.details) for e in lines)
        return len(lines)

    def import_csv(self, path: Path, category: str = CATEGORY_OTHER) -> tuple[int, int]:
        """Read a CSV (``term[,class]``, the German ``begriff[,klasse]`` as well; header
        optional, comma, semicolon or tab) or a plain list (one term per line); returns
        (new, read).  A class column in the file wins over ``category``; a German class
        name in it is read as the English one."""
        read_in = list_import.read_list(path, ("class", "klasse"), category)
        return sum(self.add(b, k) for k, b in read_in.groups.items()), read_in.lines

    # -- bundled lists ---------------------------------------------------------------------------
    def seed(self, force: bool = False) -> dict[str, int]:
        """Install every bundled list whose data is not the one the store holds (all of
        them with ``force``); what the overlay hides in one stays hidden.  Returns the
        number of terms of each list installed; nothing while the bundled lists are
        switched off."""
        if not lexicon.active() and not force:
            return {}
        held = {info.url: info.version for info in self.store.packages()}
        loaded: dict[str, int] = {}
        for q in lexicon.available():
            state = lexicon.state(q)
            if force or held.get(self.store.local_url(q.name)) != state:
                rows = lists.visible(((b, k, code, extra)
                                      for b, k, _r, code, extra in lexicon.rows_of(q)),
                                     studies=q.name == lexicon.STUDIES)
                loaded[q.name] = self.store.add_package(
                    lists.package(q.name, rows, state, lexicon.note(q))).terms
        if loaded:
            self._settle_towns()
        return loaded



def copy_lists(source: Store, target: Store) -> int:
    """Make the own lists of ``target`` exactly those of ``source``: every list is written
    under the URL the target gives its name, with the lines of the overlay that speak of
    it; an own list of the target that the source lacks goes, with its lines.  Packages of
    other programs are left alone in both stores.  Returns how many lists were copied."""
    urls: dict[str, str] = {}
    for info in own_lists(source):
        package = source.package(info.url)
        package.url = ""
        urls[info.url] = target.add_package(package).url
    kept = set(urls.values())
    gone = set(_urls(target)) - kept
    for url in gone:
        target.remove_package(url)
    for line in target.overlay():
        if line.url in kept | gone:
            target.undo(line)
    overlay.take(target, [replace(line, url=urls[line.url]) for line in source.overlay()
                          if line.url in urls])
    return len(urls)


# of the store read last: its state with the lists searched, and the recogniser
_recognisers: dict[str, tuple[tuple[int, int, tuple[str, ...]], Recogniser]] = {}


def _recogniser(store: Store) -> Recogniser:
    """lexident's recogniser over what is in force in the lists the store has ticked, built
    again only when the store or the ticks have changed."""
    urls = applied(store)
    state = (*_state(store), tuple(urls))
    built = _recognisers.get(store.key)
    if built is None or built[0] != state:
        _recognisers.clear()
        built = _recognisers[store.key] = (state, lexident.recogniser_of(store.entries(urls)))
    return built[1]


def standard() -> Recogniser:
    """lexident's recogniser over the lists the store has ticked, built again only when
    the store or the ticks have changed; without a usable store it knows the registry numbers
    only."""
    try:
        with IdentifierTable() as table:
            return _recogniser(table.store)
    except (sqlite3.Error, OSError, StoreError):
        return Recogniser()
