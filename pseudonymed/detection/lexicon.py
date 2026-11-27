"""Bundled reference data: what a term *is*.

The protected vocabulary (:mod:`pseudonymed.detection.vocabulary`) only says that a word must
not be redacted by the guesswork.  The bundled lists read here add the class of a term and
its role:

* role ``fach`` - medical terms by class (Anatomie, Medikament, Erkrankung, Symptom,
  Substanz, Verfahren, Psyche, Physiologie, Organismus, Laborwert, Befund, Eponym);
* role ``merkmal`` - quasi-identifying attributes (Beruf, Erwerbsstatus, Titel, Klinik,
  Studie) that must never be protected.

Neither lives in the pseudonym database any more.  The ``merkmal`` rows are the bundled
identifiers: :mod:`pseudonymed.detection.identifiers` installs them as packages of lexident's
store, where the identifiers step shows them and the analysis redacts them by default.  The
``fach`` rows nobody edits and one reader needs: they are read from the bundled files when a
medical option of the analysis asks for drug or lab names (:func:`medical_rows`).  They are
kept out of lexterm's store on purpose: every list PseudonyMed keeps there is protected
vocabulary, and these rows hold words that must not be ("Zuname" is a lab term of Wikidata).
``PSEUDONYMED_LEXIKON=0`` switches the bundled lists off (the test suite does so, except in
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
MEDICAL = "fach"
FEATURE = "merkmal"
USER = "Nutzer"
FEATURE_SOURCES = ("Berufe", "Titel", "Kliniken", "Studien")  # the bundled identifier lists
MIN_LENGTH = 2
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

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
    """A bundled source: its name (a list of identifiers is installed under it in lexident's
    store), its file (None for a source computed in code) and the reader that yields its
    rows."""

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
    """Are the bundled lists switched on (``PSEUDONYMED_LEXIKON`` is not ``0``)?"""
    return environment.variable("LEXIKON", "1") != "0"


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
    if category == "Titel":
        try:
            kind = json.loads(extra).get("art", "") if extra else ""
        except ValueError:
            kind = ""
        return GEMTEX["Titel"] if kind in ("", "akademisch") else ""
    return GEMTEX.get(category, "")
