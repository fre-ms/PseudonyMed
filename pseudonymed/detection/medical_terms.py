"""Medical vocabulary that must never be redacted by guesswork.

Before the NER model and the fuzzy OCR search may touch a word, the page is
scanned for medical terms: diseases, symptoms, anatomy, drugs, procedures,
scores, eponyms.  Spans covered by such a term are protected from those two
heuristics (the deterministic rules for the patient's own identifiers are
not affected, a patient really called "Herz" is still redacted).

The terms live in the ``vocabulary`` table of the pseudonym database
(:mod:`pseudonymed.detection.vocabulary`): the bundled lists (German labels and aliases
from Wikidata for items with MeSH, ICD-10, ATC or INN identifiers, CC0; body parts;
findings terms; the eponyms of :mod:`pseudonymed.detection.ner`) are loaded into it on
first use, and the user adds, deletes and imports terms there (vocabulary step,
``pseudonymed fachbegriffe``).  :func:`load` still reads the bundled files directly, as
the fallback when the database cannot be opened.

Matching is case-insensitive, tolerates German inflection (-s, -n, -en, -e,
-es, -er, -em, -ern), hyphenated compounds ("Parkinson-Syndrom") and umlaut
transliteration; multi-word terms are matched as a whole.  The matcher itself was written
here and lives in lexcor now (:class:`lexcor.cover.Cover`), which other programs share;
this module keeps what is PseudonyMed's: the lists, where they are stored, and the head
nouns after which a name is an eponym.
"""

from __future__ import annotations

import csv
import gzip
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from pathlib import Path

from lexcor.cover import Cover

from pseudonymed import environment
from pseudonymed.core.text import say

BASE = Path(__file__).resolve().parent.parent / "data" / "fachbegriffe_de.txt.gz"
BODY_PARTS = Path(__file__).resolve().parent.parent / "data" / "koerperteile_de.txt"
FINDINGS_TERMS = Path(__file__).resolve().parent.parent / "data" / "befundbegriffe_de.txt"
# Head nouns after which the first part of a hyphenated term is an eponym that appears on
# its own in reports ("Babinski-Zeichen" -> "Babinski positiv").  Anatomy and chemistry heads
# are deliberately absent: "Müller-Gang" or "Berlin-Blau" must not protect the surname Müller
# or the town Berlin from the name recognition.
_EPONYM_HEADS = {
    "zeichen", "syndrom", "test", "probe", "manöver", "manoever", "reflex", "phänomen",
    "phaenomen", "skala", "score", "index", "trias", "kriterien", "klassifikation", "lagerung",
    "griff", "punkt", "position", "stadium", "formel", "gleichung", "koeffizient", "regel",
    "gesetz", "effekt", "reaktion", "methode", "verfahren", "technik", "operation", "naht",
    "schnitt", "zugang",
}


def data_folder() -> Path:
    """The folder of the user's own list files (``medical_terms`` in the data folder)."""
    return environment.data_folder() / "fachbegriffe"


class MedicalTerms:
    """The merged vocabulary with its matcher: lexcor's cover with PseudonyMed's eponym heads,
    and the record of which list brought how many terms."""

    def __init__(self) -> None:
        """Starts empty."""
        self.cover = Cover(heads=_EPONYM_HEADS)
        self.sources: list[tuple[str, int]] = []

    @property
    def count(self) -> int:
        """Number of terms held: single words plus whole phrases."""
        return len(self.cover.single) + sum(len(v) for v in self.cover.phrases.values())

    def add(self, terms: Iterable[str], source: str = "") -> int:
        """Adds raw terms in both umlaut spellings (comments and entries under three characters
        skipped, a trailing bracket removed); returns how many were taken and records that
        count under ``source`` when one is given."""
        n = self.cover.add(t for t in terms if not t.strip().startswith("#"))
        if source:
            self.sources.append((source, n))
        return n

    def is_medical_term(self, word: str) -> bool:
        """Single word, inflected forms and hyphen parts included."""
        return self.cover.is_term(word)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans of medical terms in ``text`` (multi-word first, then single)."""
        return self.cover.spans(text)

    def is_protected(self, text: str, a: int, b: int, spans: list[tuple[int, int]] | None = None,
                     whole: bool = False) -> bool:
        """Does ``text[a:b]`` touch a protected term (or, with ``whole``, consist of them)?"""
        return self.cover.covers(text, a, b, spans, whole)


# -- loading ---------------------------------------------------------------------------------------
def _read_list(path: Path) -> Iterator[str]:
    """Non-empty, non-comment lines of a plain or gzip-compressed list file, stripped."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def _load_bundled(fb: MedicalTerms) -> None:
    """Adds the bundled lists that exist on disk, each under its source name, in the order
    base vocabulary, body parts, findings terms."""
    for path, source in ((BASE, f"Basis ({BASE.name})"), (BODY_PARTS, "Körperteile"),
                         (FINDINGS_TERMS, "Befundbegriffe")):
        if path.exists():
            fb.add(_read_list(path), source)


def _user_lists() -> list[Path]:
    """The user's own list files (``.txt``, ``.gz``) in the data folder, sorted by name;
    empty when the folder does not exist."""
    folder = data_folder()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix in (".txt", ".gz") and p.is_file())


def load(extra: Iterable[Path] = (), with_base: bool = True,
         with_user_data: bool = True) -> MedicalTerms:
    """Builds the vocabulary from the files: the bundled lists (``with_base``), the eponyms of
    the NER step, the files in ``extra`` and the user's own lists (``with_user_data``), in
    that order.  The fallback of :func:`standard` when the database is unusable."""
    from pseudonymed.detection.ner import EPONYMS

    fb = MedicalTerms()
    if with_base:
        _load_bundled(fb)
    fb.add((e for e in EPONYMS if len(e) >= 4), "Eponyme")
    paths = list(extra) + (_user_lists() if with_user_data else [])
    for p in paths:
        fb.add(_read_list(p), p.name)
    return fb


class _DefaultVocabulary:
    """The vocabulary used by the pipeline, built from the database table and kept until the
    table's version changes; ``cache_clear`` makes the next call rebuild it.  A callable
    object rather than a function with a bolted-on attribute, so that ``cache_clear`` is
    typed."""

    def __init__(self) -> None:
        """Starts without a cached vocabulary."""
        self._cache: tuple[Path, int, MedicalTerms] | None = None

    def __call__(self) -> MedicalTerms:
        """The vocabulary used by the pipeline: built from the database table, rebuilt only
        when the table's version changed; the bundled files when the database is unusable."""
        import sqlite3

        from pseudonymed.detection import vocabulary as vocab_mod

        try:
            path = vocab_mod.default_path()
            with vocab_mod.Vocabulary(path) as v:
                version = v.version()
                if self._cache is not None and self._cache[0] == path \
                        and self._cache[1] == version:
                    return self._cache[2]
                fb = MedicalTerms()
                for category, terms in v.per_category().items():
                    fb.add(terms, category)
        except (sqlite3.Error, OSError):
            return load()
        self._cache = (path, version, fb)
        return fb

    def cache_clear(self) -> None:
        """Forgets the cached vocabulary; the next call rebuilds it from the table."""
        self._cache = None


standard = _DefaultVocabulary()


# -- import ----------------------------------------------------------------------------------
def terms_from_mesh_xml(path: Path) -> Iterator[str]:
    """Descriptor names and entry terms of a MeSH XML file (NLM or a translation)."""
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag in ("DescriptorRecord", "SupplementalRecord"):
            for s in element.iter("String"):
                if s.text and s.text.strip():
                    yield s.text.strip()
            element.clear()


def terms_from_csv(path: Path, column: int, delimiter: str = ";") -> Iterator[str]:
    """One column of a CSV file, for ICD-10-GM, ATC or any other table."""
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        for line in csv.reader(f, delimiter=delimiter):
            if len(line) > column and line[column].strip():
                yield line[column].strip()


USER_LIST = "nutzervokabular"


def merge_terms(path: Path, name: str = USER_LIST) -> tuple[Path, int]:
    """Merge the terms of ``path`` (one per line or CSV) into the vocabulary table under
    the category ``name`` ("nutzervokabular" = Nutzervokabular); returns the database
    and how many were new.  This is what the exported ``.vokabular.txt`` files are for:
    the vocabulary grows with every review."""
    from pseudonymed.detection import vocabulary as vocab_mod

    category = vocab_mod.CATEGORY_USER if name == USER_LIST else name
    with vocab_mod.Vocabulary() as v:
        new, _read = v.import_csv(path, category)
        target = v.path
    standard.cache_clear()
    return target, new


def import_file(path: Path, name: str, format_: str = "liste", column: int = 0,
                delimiter: str = ";") -> tuple[Path, int]:
    """Add the terms of ``path`` to the vocabulary table under the category ``name``;
    returns the database and how many were new."""
    if format_ == "mesh-xml":
        terms = terms_from_mesh_xml(path)
    elif format_ == "csv":
        terms = terms_from_csv(path, column, delimiter)
    elif format_ == "liste":
        terms = _read_list(path)
    else:
        raise ValueError(say(f"Unbekanntes Format: {format_!r} (liste, csv, mesh-xml)",
                             f"Unknown format: {format_!r} (list, csv, mesh-xml)"))
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        new = v.add(terms, name)
        target = v.path
    standard.cache_clear()
    return target, new
