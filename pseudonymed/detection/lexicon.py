"""Bundled reference data: what a term *is*.

The protected vocabulary (:mod:`pseudonymed.detection.vocabulary`) only says that a word must
not be redacted by the guesswork.  The bundled lists read here add the class of a term and
its role:

* role ``medical`` - medical terms by class (anatomy, drug, disease, symptom, substance,
  procedure, psyche, physiology, organism, lab_test, finding, eponym);
* role ``feature`` - quasi-identifying attributes (occupation, employment_status, title,
  hospital, study) that must never be protected.

Classes, columns and the names of the lists are English (:mod:`pseudonymed.core.names`
gives their labels); the terms are the German words of the documents.  The ``feature`` rows
are the bundled identifiers: :mod:`pseudonymed.detection.identifiers` installs them as
packages of lexident's store, where the identifiers step shows them and the analysis
redacts them by default.  The ``medical`` rows nobody edits and one reader needs: they are
read from the bundled files when a medical option of the analysis asks for drug or lab
names (:func:`medical_rows`).  They are
kept out of lexterm's store on purpose: every list PseudonyMed keeps there is protected
vocabulary, and these rows hold words that must not be ("Zuname" is a lab term of Wikidata).
``PSEUDONYMED_LEXICON=0`` switches the bundled lists off (the test suite does so, except in
the tests that need them).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import string
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from pseudonymed import environment

DATA = Path(__file__).resolve().parent.parent / "data"
MEDICAL = "medical"
FEATURE = "feature"
USER = "user"  # the source of the user's own entries
OCCUPATIONS, TITLES, HOSPITALS, STUDIES = "occupations", "titles", "hospitals", "studies"
FEATURE_SOURCES = (OCCUPATIONS, TITLES, HOSPITALS, STUDIES)  # the bundled identifier lists
LAB_TESTS = "lab tests"  # the curated list of lab tests
MIN_LENGTH = 2
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# Correspondence with the GeMTeX type system for identifying information (annotation
# guideline, 2nd version, 7 September 2025, doi:10.5281/zenodo.15747389).  Studies are IDs
# there, only academic titles are NAME_TITLE (functions such as Chefarzt are not annotated),
# and an employment status is explicitly no PROFESSION: it keeps no GeMTeX type and serves
# the residual-risk estimate only.
GEMTEX = {"occupation": "PROFESSION", "title": "NAME_TITLE", "hospital": "LOCATION_HOSPITAL",
          "study": "ID", "employment_status": ""}

Row = tuple[str, str, str, str, str]  # term, class, role, code, addition (JSON)


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
        yield z["term"], z["class"], MEDICAL, z.get("code", ""), ""


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
        yield (z["term"], z.get("class") or "occupation", FEATURE, z.get("code", ""),
               _json(base_form=z.get("base_form", ""),
                     surname=1 if z.get("surname", "").strip() in ("1", "true") else None))


def _titles(path: Path) -> Iterator[Row]:
    """One row for the full designation and one for its abbreviation (``Dr. med.``), which is
    what letters actually contain; kind and country keep rows of the same word apart."""
    for z in _csv(path):
        kind, country = z.get("kind", ""), z.get("country", "")
        abbr = z.get("abbreviation", "").strip()
        yield z["term"], "title", FEATURE, f"{kind}|{country}", _json(
            kind=kind, country=country, abbreviation=abbr)
        if abbr:
            yield abbr, "title", FEATURE, f"{kind}|{country}|{z['term']}", _json(
                kind=kind, country=country, meaning=z["term"])


def _hospitals(path: Path) -> Iterator[Row]:
    """Hospitals of the DACH directories, keyed by country and directory number, with place,
    beds and provider as details."""
    for z in _csv(path):
        country = z.get("country", "")
        yield (z["name"], "hospital", FEATURE, f"{country}:{z.get('code', '')}",
               _json(type=z.get("type", ""), place=z.get("place", ""),
                     postcode=z.get("postcode", ""), municipality=z.get("municipality_code", ""),
                     country=country, beds=_number(z.get("beds")),
                     provider=z.get("provider", "")))


def _studies(path: Path) -> Iterator[Row]:
    """The registry number carries the details; the acronym row only points to it."""
    for z in _csv(path):
        code = f"{z.get('registry', '')}:{z.get('code', '')}"
        extra = _json(participants=_number(z.get("participants")),
                      participants_kind=z.get("participants_kind", ""),
                      centres_dach=_number(z.get("centres_dach")),
                      countries=z.get("countries_dach", ""),
                      places=" | ".join((z.get("places_dach") or "").split(" | ")[:5]))
        if z.get("code"):
            yield z["code"], "study", FEATURE, code, extra
        acronym = (z.get("acronym") or "").strip()
        if len(acronym) >= 3:
            yield acronym, "study", FEATURE, code, ""


def _eponyms() -> Iterator[Row]:
    """The eponyms of the NER step (four characters or more) as ``terms`` rows."""
    from pseudonymed.detection.ner import EPONYMS

    for e in sorted(EPONYMS):
        if len(e) >= 4:
            yield e, "eponym", MEDICAL, "", ""


@dataclass(frozen=True)
class Source:
    """A bundled source: its name (a list of identifiers is installed under it in lexident's
    store), its file (None for a source computed in code) and the reader that yields its
    rows."""

    name: str
    file: Path | None
    reader: Callable[..., Iterator[Row]]


SOURCES: tuple[Source, ...] = (
    Source("wikidata", DATA / "medical_classes_de.csv.gz", _medical_categories),
    Source("body parts", DATA / "body_parts_de.txt", _list_as("anatomy")),
    Source("findings terms", DATA / "findings_terms_de.txt", _list_as("finding")),
    Source(LAB_TESTS, DATA / "lab_tests_de.txt", _list_as("lab_test")),
    Source("eponyms", None, _eponyms),
    Source(OCCUPATIONS, DATA / "occupations_de.csv.gz", _professions),
    Source(TITLES, DATA / "titles_de.csv.gz", _titles),
    Source(HOSPITALS, DATA / "hospitals_dach.csv.gz", _hospitals),
    Source(STUDIES, DATA / "studies_dach.csv.gz", _studies),
)


_states: dict[tuple[Path, int, int], str] = {}  # a file's hash while the file is the same


def state(q: Source) -> str:
    """Short hash of a source's current content: its file bytes, or the rows it computes."""
    if q.file is None:
        lines = "\n".join("\t".join(z) for z in q.reader())
        return hashlib.sha256(lines.encode("utf-8")).hexdigest()[:16]
    stat = q.file.stat()
    known = (q.file, stat.st_mtime_ns, stat.st_size)
    if known not in _states:
        _states[known] = hashlib.sha256(q.file.read_bytes()).hexdigest()[:16]
    return _states[known]


def note(q: Source) -> str:
    """What a bundled file says of itself in its first line: source, licence and date."""
    if q.file is None:
        return ""
    with _open_text(q.file) as f:
        first = f.readline().strip()
    return first.lstrip("# ") if first.startswith("#") else ""


def active() -> bool:
    """Are the bundled lists switched on (``PSEUDONYMED_LEXICON`` is not ``0``)?"""
    return environment.variable("LEXICON", "1") != "0"


def available(names: tuple[str, ...] | None = None) -> list[Source]:
    """The bundled sources that are present on disk: the identifier lists, or with
    ``names`` those of these names."""
    wanted = FEATURE_SOURCES if names is None else names
    return [q for q in SOURCES if q.name in wanted and (q.file is None or q.file.exists())]


def rows_of(q: Source) -> Iterator[Row]:
    """The rows of a source with inner whitespace collapsed; terms shorter than
    ``MIN_LENGTH`` are left out."""
    args = () if q.file is None else (q.file,)
    for b, k, r, code, extra in q.reader(*args):
        if len(b.strip()) >= MIN_LENGTH:
            yield " ".join(b.split()), k, r, code, extra


# -- the medical classes ---------------------------------------------------------------------------
_medical: dict[tuple[tuple[str, str], ...], list[tuple[str, str, str]]] = {}


def key(term: str) -> str:
    """What makes two spellings one term of a list: the letters A to Z without their case,
    as the table compared them that the rows were kept in."""
    return term.translate(_ASCII_LOWER)


def _medical_sources() -> list[Source]:
    """The bundled sources of medical classes that are present on disk; none when the
    bundled lists are switched off."""
    return [q for q in SOURCES if active() and q.name not in FEATURE_SOURCES
            and (q.file is None or q.file.exists())]


def medical_state() -> tuple[tuple[str, str], ...]:
    """The state of the bundled medical lists: every source with the hash of its data."""
    return tuple((q.name, state(q)) for q in _medical_sources())


def medical_rows() -> list[tuple[str, str, str]]:
    """Term, medical class and source of every row of the bundled medical lists, read once
    for the state the files are in; none when the bundled lists are switched off.  A term
    stands once per class and identifier (:func:`key`), in its first spelling, as the table
    held it that these rows were kept in."""
    held = medical_state()
    if held not in _medical:
        seen: set[tuple[str, str, str]] = set()
        rows: list[tuple[str, str, str]] = []
        for q in _medical_sources():
            for term, category, _role, code, _extra in rows_of(q):
                if (key(term), category, code) not in seen:
                    seen.add((key(term), category, code))
                    rows.append((term, category, q.name))
        _medical.clear()
        _medical[held] = rows
    return _medical[held]


def gemtex_type(category: str, extra: str = "") -> str:
    """GeMTeX type of a lexicon row ("" where the guideline annotates nothing)."""
    if category == "title":
        try:
            kind = json.loads(extra).get("kind", "") if extra else ""
        except ValueError:
            kind = ""
        return GEMTEX["title"] if kind in ("", "academic") else ""
    return GEMTEX.get(category, "")
