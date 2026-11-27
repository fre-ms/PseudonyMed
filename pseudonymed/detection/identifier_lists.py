"""Identifier rows as packages of lexident's store, and the tables they were kept in.

An identifier row says a term, its class, the source's identifier of what it names (a KldB
code, the number of a hospital or of a study; none for an entry of the user's own) and what
else the source says of it.  In lexident's store the rows of one source and class are one
package: rows with the same identifier are the terms of one concept, the concept's class is
the GeMTeX type of the row's class (a class of the user's own stays as it is), and the
addition becomes properties of the concept.  Where the terms of a concept differ in what
their rows say (the base form of an occupation, the abbreviation of a title), the property
holds a JSON object from term to value (:func:`fold`, :func:`unfold`).

Until November 2026 the rows were the ``merkmal`` rows of the table ``lexikon`` of the
pseudonym database; :func:`former_tables` reads them for the one-time move
(:meth:`pseudonymed.detection.identifiers.IdentifierTable.adopt`).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from lexcor import Concept, Package, Term
from lexident.types import HOSPITAL_TOWN, OTHER

from pseudonymed.detection import lexicon

CATEGORY_HOSPITAL = "Klinik"
CATEGORY_STUDY = "Studie"
CATEGORY_OTHER = "Sonstiges"
# PseudonyMed's class of an identifier and its type in the GeMTeX guideline, lexident's class
TYPES = {category: gemtex for category, gemtex in lexicon.GEMTEX.items() if gemtex} | {
    CATEGORY_OTHER: OTHER}
_CATEGORIES = {gemtex: category for category, gemtex in TYPES.items()}
TOWNS = "hospital towns"  # the list of the towns that have a hospital, made from the hospitals
LANGUAGE = "de"
_HIDDEN = "Erwerbsstatus"  # no type of the guideline: never shown, never looked for
key = lexicon.key  # what makes two spellings one term of a row
Deletion = tuple[str, str, str]  # term, class, identifier of a bundled row the user deleted


@dataclass(frozen=True)
class Row:
    """One identifier: its term, PseudonyMed's class, the source's identifier of what it
    names ("" when it has none) and what else the source says of it."""

    term: str
    category: str
    code: str = ""
    addition: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Former:
    """What the tables of a pseudonym database held: the identifier rows by source, the
    state of each source's data and the bundled rows the user deleted."""

    rows: dict[str, list[Row]]
    states: dict[str, str]
    deleted: list[Deletion]


def cls_of(category: str) -> str:
    """lexident's class for a class of PseudonyMed's: its GeMTeX type, or the class itself."""
    return TYPES.get(category, category)


def category_of(cls: str) -> str:
    """PseudonyMed's class for a class of lexident's."""
    return _CATEGORIES.get(cls, cls)


def addition_of(extra: str) -> dict[str, str]:
    """What the JSON addition of a row says, every value as text; nothing when it is empty
    or no JSON object."""
    try:
        said = json.loads(extra) if extra.startswith("{") else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in said.items() if v not in ("", None)}


def town_of(addition: Mapping[str, str]) -> str:
    """The town a hospital's addition names ("" without one)."""
    return str(addition.get("ort") or "")


def fold(additions: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """The properties of a concept from what the rows of its terms say: a value all terms
    share as it is, a value that differs between them as a JSON object from term to value."""
    properties: dict[str, str] = {}
    for name in sorted({name for said in additions.values() for name in said}):
        values = {term: said[name] for term, said in additions.items() if name in said}
        shared = len(values) == len(additions) and len(set(values.values())) == 1
        properties[name] = next(iter(values.values())) if shared else json.dumps(
            values, ensure_ascii=False, sort_keys=True)
    return properties


def unfold(properties: Mapping[str, str], term: str) -> dict[str, str]:
    """What a concept's properties say of one of its terms (:func:`fold` read back)."""
    said: dict[str, str] = {}
    for name, value in properties.items():
        if not value.startswith("{"):
            said[name] = value
            continue
        try:
            by_term = json.loads(value)
        except ValueError:
            by_term = None
        if not isinstance(by_term, dict):
            said[name] = value
        elif term in by_term:
            said[name] = str(by_term[term])
    return said


Line = tuple[str, str, str, str]  # term, class, identifier, JSON addition


def _is_number(row: Line) -> bool:
    """Whether a row of the list of studies is the one of a registry number: it alone
    carries what the registry says of the study."""
    return row[1] == CATEGORY_STUDY and bool(row[3])


def visible(rows: Iterable[Line], studies: bool = False) -> list[Row]:
    """The rows (term, class, identifier, JSON addition) of one source that are identifiers
    to show and to look for, each once (:func:`key`).  An employment status is none.  In
    the list of studies (``studies``) the rows of the registry numbers are none either, the
    pattern finds every such number: what they say of a study goes to the rows of its
    acronyms."""
    once: dict[tuple[str, str, str], Line] = {}
    for row in rows:
        once.setdefault((key(row[0]), row[1], row[2]), row)
    kept = [row for row in once.values() if row[1] != _HIDDEN]
    if not studies:
        return [Row(term, category, code, addition_of(extra))
                for term, category, code, extra in kept]
    numbers = {row[2]: row[3] for row in kept if _is_number(row)}
    return [Row(row[0], row[1], row[2], addition_of(numbers.get(row[2], "")))
            for row in kept if not _is_number(row)]


def package(name: str, rows: Iterable[Row], version: str = "", note: str = "") -> Package:
    """The rows of one source and class as a package: no URL of its own (the store gives it
    one from its name), never to be passed on, a concept for every identifier (or, without
    one, for every term) with the rows' additions as its properties."""
    grouped: dict[str, tuple[str, dict[str, Mapping[str, str]]]] = {}
    for row in rows:
        grouped.setdefault(row.code or row.term, (cls_of(row.category), {}))[1].setdefault(
            row.term, row.addition)
    return Package(
        url="", version=version, name=name, title=name, copyright=note, language=LANGUAGE,
        concepts=[Concept(code, "", cls, "", [Term(term, LANGUAGE) for term in terms],
                          fold(terms))
                  for code, (cls, terms) in grouped.items()])


def towns_package(towns: Iterable[str]) -> Package:
    """The towns that have a hospital as a package: lexident forms "Klinikum <town>" and
    the like of them and never tags a town itself."""
    return Package(url="", name=TOWNS, title=TOWNS, language=LANGUAGE,
                   concepts=[Concept(town, town, HOSPITAL_TOWN, "", [Term(town, LANGUAGE)])
                             for town in sorted(set(towns))])


def rows_of(concepts: Iterable[Concept], category: str | None = None) -> list[Row]:
    """The rows a package's concepts stand for (:func:`package` read back); ``category``
    when the caller knows PseudonyMed's class, else the one of each concept's class."""
    return [Row(term.value, category or category_of(concept.cls),
                "" if concept.code == term.value else concept.code,
                unfold(concept.properties, term.value))
            for concept in concepts for term in concept.terms]


# -- the former tables -----------------------------------------------------------------------------
def _has(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                              (table,)).fetchone() is not None


def former_tables(database: Path) -> Former | None:
    """The identifier rows of the tables a pseudonym database kept them in until November
    2026, by source, with the state of each source and the deletions; None when the
    database is not there or has no such table."""
    if not Path(database).is_file():
        return None
    with closing(sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro",
                                 uri=True)) as connection:
        if not _has(connection, "lexikon"):
            return None
        by_source: dict[str, list[Line]] = {}
        for term, category, source, code, extra in connection.execute(
                "SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon"
                " WHERE rolle = 'merkmal'"):
            by_source.setdefault(source, []).append((term, category, code, extra))
        states = dict(connection.execute("SELECT quelle, stand FROM lexikon_meta")) \
            if _has(connection, "lexikon_meta") else {}
        deleted = [tuple(row) for row in connection.execute(
            "SELECT begriff, klasse, kennung FROM lexikon_geloescht")] \
            if _has(connection, "lexikon_geloescht") else []
    rows = {source: visible(found, studies=source == "Studien")
            for source, found in by_source.items()}
    return Former({source: found for source, found in rows.items() if found}, states, deleted)


def drop_tables(database: Path) -> None:
    """Remove the former tables of the lexicon from a pseudonym database."""
    with closing(sqlite3.connect(str(database))) as connection:
        connection.executescript(
            "DROP TABLE IF EXISTS lexikon; DROP TABLE IF EXISTS lexikon_meta;"
            " DROP TABLE IF EXISTS lexikon_geloescht; DROP TABLE IF EXISTS lexikon_version;")
