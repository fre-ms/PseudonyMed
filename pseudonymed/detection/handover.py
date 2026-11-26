"""Hand the vocabulary and the identifiers over to the stores of lexterm and lexident.

lexterm (medical terms) and lexident (identifying information) keep their terms as packages
in stores of their own, which are lexcor's.  This module writes what PseudonyMed holds in its
pseudonym database into those stores: every category of the vocabulary as a package of
lexterm's store, the identifier rows of every source as packages of lexident's.

It is a copy.  PseudonyMed's tables are read and stay as they are, and PseudonyMed goes on
reading them until it is switched over; a store that is there already is copied aside before
anything is written into it.  What was written is read back and compared: the vocabulary by
the index its matcher builds, the identifiers term by term.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path

import lexcor
from lexcor import Concept, Package, Term

from pseudonymed.core.text import say
from pseudonymed.detection import identifiers, lexicon, vocabulary
from pseudonymed.detection import medical_terms as terms_module

HOSPITAL_TOWN = "hospital-town"  # lexident's class for a town that has a hospital
TYPES = {category: gemtex for category, gemtex in lexicon.GEMTEX.items() if gemtex} | {
    identifiers.CATEGORY_OTHER: "OTHER"}
# whose a bundled category of the vocabulary is, and whether it may be passed on
_ORIGIN = {
    vocabulary.CATEGORY_MEDICINE: ("Wikidata", "CC0 1.0", True),
    vocabulary.CATEGORY_BODY_PARTS: ("PseudonyMed", "AGPL-3.0-or-later", False),
    vocabulary.CATEGORY_FINDINGS: ("PseudonyMed", "AGPL-3.0-or-later", False),
    vocabulary.CATEGORY_EPONYMS: ("PseudonyMed", "AGPL-3.0-or-later", False),
}


class HandoverError(ValueError):
    """What was written into a store is not what PseudonyMed holds."""


@dataclass(frozen=True)
class Written:
    """One store after the hand-over: its file, the copy of the file as it was before (when
    there was one) and for every package its title, its concepts and its terms."""

    store: Path
    kept: Path | None
    packages: list[tuple[str, int, int]]


@dataclass(frozen=True)
class Report:
    """Both stores after the hand-over."""

    vocabulary: Written
    identifiers: Written


def vocabulary_packages(database: Path | None = None) -> list[Package]:
    """Every category of the vocabulary as a package: a plain list, the term its own code."""
    packages = []
    with vocabulary.Vocabulary(database) as table:
        for category, terms in table.per_category().items():
            publisher, licence, free = _ORIGIN.get(category, ("", "", False))
            packages.append(Package(
                url="", name=category, title=category, publisher=publisher, copyright=licence,
                language="de", redistributable=free,
                concepts=[Concept(term, term, "", "", [Term(term, "de")]) for term in terms]))
    return packages


def _identifier_rows(database: Path | None) -> list[tuple[str, str, str, str, str]]:
    """The visible identifier rows: term, class, source, identifier, addition."""
    with identifiers.IdentifierTable(database) as table:
        if lexicon.active():
            lexicon.ensure(table.connection)
        return [tuple(row) for row in table.connection.execute(
            "SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon"
            f" WHERE {identifiers._VISIBLE} ORDER BY quelle, klasse, begriff, kennung")]


Groups = dict[tuple[str, str], dict[str, Concept]]


def _grouped(rows: list[tuple[str, str, str, str, str]]) -> tuple[Groups, set[str]]:
    """The rows as concepts by source and class (rows with the same identifier are the terms
    of one concept), and the towns of the hospitals among them."""
    groups: Groups = {}
    towns: set[str] = set()
    for term, category, source, code, extra in rows:
        concepts = groups.setdefault((source, category), {})
        key = code or " ".join(term.split())
        concept = concepts.setdefault(key, Concept(key, "", TYPES.get(category, category)))
        concept.terms.append(Term(term, "de"))
        if category == identifiers.CATEGORY_HOSPITAL:
            towns.add(identifiers._place_from(extra))
    return groups, towns - {""}


def _titles(groups: Groups) -> dict[tuple[str, str], str]:
    """The title of every group's package: its source, with the class where a source has
    several."""
    classes: dict[str, int] = {}
    for source, _category in groups:
        classes[source] = classes.get(source, 0) + 1
    return {(source, category): f"{source}: {category}" if classes[source] > 1 else source
            for source, category in groups}


def identifier_packages(database: Path | None = None) -> list[Package]:
    """The identifier rows as packages, one for every source and class: the class is the
    GeMTeX type, and the towns of the hospitals are a package of their own (lexident forms
    "Klinikum <town>" of them)."""
    groups, towns = _grouped(_identifier_rows(database))
    titles = _titles(groups)
    packages = [Package(url="", name=titles[group], title=titles[group], language="de",
                        redistributable=False, concepts=list(concepts.values()))
                for group, concepts in groups.items()]
    if towns:
        packages.append(Package(
            url="", name="hospital towns", title="hospital towns", language="de",
            concepts=[Concept(t, t, HOSPITAL_TOWN, "", [Term(t, "de")]) for t in sorted(towns)]))
    return packages


def _write(file: Path, library: str, packages: list[Package],
           stamp: str) -> tuple[Written, list[str]]:
    """Put packages into a store (copied aside first when it exists); the URLs they got."""
    kept = None
    if file.exists():
        kept = file.with_name(f"{file.name}.before-{stamp}")
        shutil.copy2(file, kept)
    with lexcor.open_store(file, library) as store:
        infos = [store.add_package(package) for package in packages]
    return (Written(file, kept, [(i.title, i.concepts, i.terms) for i in infos]),
            [i.url for i in infos])


def _check_vocabulary(file: Path, urls: list[str], database: Path | None) -> None:
    """The matcher over the packages written must be the matcher over the table."""
    expected = terms_module.MedicalTerms()
    with vocabulary.Vocabulary(database) as table:
        for terms in table.per_category().values():
            expected.add(terms)
    with lexcor.open_store(file, "lexterm") as store:
        found = lexcor.cover(store, urls, heads=terms_module._EPONYM_HEADS)
        same = (found.single == expected.cover.single
                and found.phrases == expected.cover.phrases)
    if not same:
        raise HandoverError(say(
            "Das Fachvokabular in der Ablage von lexterm stimmt nicht mit der Tabelle überein.",
            "The vocabulary in lexterm's store does not agree with the table."))


def _check_identifiers(file: Path, urls: list[str], packages: list[Package]) -> None:
    """Every term must stand in the store under its class, and no other."""
    expected = {(" ".join(term.value.split()), concept.cls)
                for package in packages for concept in package.concepts
                for term in concept.terms}
    with lexcor.open_store(file, "lexident") as store:
        found = {(entry.term, entry.cls) for entry in store.entries(urls)}
    if found != expected:
        raise HandoverError(say(
            "Die Identifikatoren in der Ablage von lexident stimmen nicht mit der Tabelle"
            " überein.",
            "The identifiers in lexident's store do not agree with the table."))


def hand_over(target: Path | None = None, database: Path | None = None) -> Report:
    """Write the vocabulary into ``lexterm.sqlite`` and the identifiers into
    ``lexident.sqlite`` in ``target`` (default: the data folder of the two libraries), check
    both against PseudonyMed's tables and say what was written."""
    folder = target if target is not None else lexcor.store.data_folder()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    words = vocabulary_packages(database)
    written_words, urls = _write(folder / "lexterm.sqlite", "lexterm", words, stamp)
    _check_vocabulary(written_words.store, urls, database)
    rows = identifier_packages(database)
    written_rows, urls = _write(folder / "lexident.sqlite", "lexident", rows, stamp)
    _check_identifiers(written_rows.store, urls, rows)
    return Report(written_words, written_rows)
