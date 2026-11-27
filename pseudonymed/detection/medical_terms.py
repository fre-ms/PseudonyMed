"""Medical vocabulary that must never be redacted by guesswork.

Before the NER model and the fuzzy OCR search may touch a word, the page is
scanned for medical terms: diseases, symptoms, anatomy, drugs, procedures,
scores, eponyms.  Spans covered by such a term are protected from those two
heuristics (the deterministic rules for the patient's own identifiers are
not affected, a patient really called "Herz" is still redacted).

The terms live in lexterm's store (:mod:`pseudonymed.detection.vocabulary`): the bundled
lists (German labels and aliases from Wikidata for items with MeSH, ICD-10, ATC or INN
identifiers, CC0; body parts; findings terms; the eponyms of
:mod:`pseudonymed.detection.ner`) are installed there on first use, and the user adds,
deletes and imports terms there (vocabulary step, ``pseudonymed fachbegriffe``).
:func:`load` reads the bundled lists directly, as the fallback when the store cannot be
opened.

Matching is case-insensitive, tolerates German inflection (-s, -n, -en, -e,
-es, -er, -em, -ern), hyphenated compounds ("Parkinson-Syndrom") and umlaut
transliteration; multi-word terms are matched as a whole.  The matcher was written here and
lives in lexcor now (:class:`lexcor.cover.Cover`), and the head nouns after which a name is
an eponym are lexterm's (``lexterm.EPONYM_HEADS``); this module keeps what is PseudonyMed's:
which store is read, the record of the lists, and the import of term files.
"""

from __future__ import annotations

import csv
import gzip
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from pathlib import Path

import lexterm
from lexcor import Cover, StoreError

from pseudonymed.core.text import say


class MedicalTerms:
    """The merged vocabulary with its matcher: lexcor's cover with lexterm's eponym heads,
    and the record of which list brought how many terms."""

    def __init__(self, cover: Cover | None = None,
                 sources: Iterable[tuple[str, int]] = ()) -> None:
        """Starts empty, or over a cover built elsewhere with the record of its lists."""
        self.cover = cover if cover is not None else Cover(heads=lexterm.EPONYM_HEADS)
        self.sources: list[tuple[str, int]] = list(sources)

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


def load() -> MedicalTerms:
    """The vocabulary straight from the bundled lists, each under the name of its category:
    the fallback of :func:`standard` when the store is unusable."""
    from pseudonymed.detection import vocabulary as vocab_mod

    fb = MedicalTerms()
    for category, terms in vocab_mod.bundled().items():
        fb.add(terms, category)
    return fb


class _DefaultVocabulary:
    """The vocabulary used by the pipeline: lexterm's cover over the store, kept until the
    store's version changes; ``cache_clear`` makes the next call build it again.  A callable
    object rather than a function with a bolted-on attribute, so that ``cache_clear`` is
    typed."""

    def __init__(self) -> None:
        """Starts without a cached vocabulary."""
        self._cache: tuple[tuple[Path, int, tuple[str, ...]], MedicalTerms] | None = None

    def __call__(self) -> MedicalTerms:
        """The vocabulary used by the pipeline: the lists the store has ticked, built again
        only when the store or the ticks changed; the bundled lists when the store is
        unusable.  A
        vocabulary that could not be taken over from the pseudonym database is no such
        case: that error is passed on."""
        from pseudonymed.detection import vocabulary as vocab_mod

        try:
            with vocab_mod.Vocabulary() as v:
                state = (v.path, v.version(), tuple(v.applied()))
                if self._cache is not None and self._cache[0] == state:
                    return self._cache[1]
                fb = MedicalTerms(v.cover(), v.applied_sources())
        except (sqlite3.Error, OSError, StoreError):
            return load()
        self._cache = (state, fb)
        return fb

    def cache_clear(self) -> None:
        """Forgets the cached vocabulary; the next call builds it again from the store."""
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
    """Merge the terms of ``path`` (one per line or CSV) into the vocabulary under the
    category ``name`` ("nutzervokabular" = Nutzervokabular); returns the store and how
    many were new.  This is what the exported ``.vokabular.txt`` files are for: the
    vocabulary grows with every review."""
    from pseudonymed.detection import vocabulary as vocab_mod

    category = vocab_mod.CATEGORY_USER if name == USER_LIST else name
    with vocab_mod.Vocabulary() as v:
        new, _read = v.import_csv(path, category)
        target = v.path
    standard.cache_clear()
    return target, new


def import_file(path: Path, name: str, format_: str = "liste", column: int = 0,
                delimiter: str = ";") -> tuple[Path, int]:
    """Add the terms of ``path`` to the vocabulary under the category ``name``; returns
    the store and how many were new."""
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
