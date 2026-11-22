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
transliteration; multi-word terms are matched as a whole.
"""

from __future__ import annotations

import csv
import gzip
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from pseudonymed import environment
from pseudonymed.core.text import say

BASE = Path(__file__).resolve().parent.parent / "data" / "fachbegriffe_de.txt.gz"
BODY_PARTS = Path(__file__).resolve().parent.parent / "data" / "koerperteile_de.txt"
FINDINGS_TERMS = Path(__file__).resolve().parent.parent / "data" / "befundbegriffe_de.txt"
_TOKEN = re.compile(r"[^\W\d_][\w\-]*|\d+[\w\-]*")
_SUFFIXES = ("ern", "en", "es", "er", "em", "e", "n", "s")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_BRACKET = re.compile(r"\s*\([^)]*\)\s*$")
MAX_WORDS = 5
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


def _norm(word: str) -> str:
    """Case-folded word without surrounding punctuation."""
    return word.casefold().strip(" .,;:!?\"'()[]")


def _forms(term: str) -> set[str]:
    """The normalised term as written and with umlauts transliterated (never the empty string)."""
    n = _norm(term)
    return {n, n.translate(_TRANS)} - {""}


def _leading_eponym(word: str) -> str | None:
    """The eponym in front of an eponym head ("Babinski" of "Babinski-Zeichen"), else None.
    Only two-part terms with a first part of four or more characters qualify: "Berlin-Blau"
    has a chemistry head, and in "Konigsmark-Hollander-Berlin-Syndrom" the town matters more."""
    parts = word.split("-")
    if len(parts) == 2 and parts[1] in _EPONYM_HEADS and len(parts[0]) >= 4:
        return parts[0]
    return None


def _follows(normed: list[str], i: int, sequence: tuple[str, ...]) -> bool:
    """Do the normalised tokens from ``i`` on spell the phrase ``sequence``, each token as written
    or with umlauts transliterated?"""
    if i + len(sequence) > len(normed):
        return False
    return all(n == f or n.translate(_TRANS) == f
               for n, f in zip(normed[i:i + len(sequence)], sequence, strict=True))


def _fully_covered(text: str, a: int, b: int, spans: list[tuple[int, int]]) -> bool:
    """Does every token of ``text[a:b]`` lie inside one of ``spans`` (phrases count as a
    whole)?"""
    return all(any(s[0] <= m.start() and m.end() <= s[1] for s in spans)
               for m in _TOKEN.finditer(text, a, b))


@dataclass
class MedicalTerms:
    """The merged vocabulary with its matcher."""

    single_words: set[str] = field(default_factory=set)
    multi_words: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    sources: list[tuple[str, int]] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of terms held: single words plus whole phrases."""
        return len(self.single_words) + sum(len(v) for v in self.multi_words.values())

    def add(self, terms: Iterable[str], source: str = "") -> int:
        """Adds raw terms in both umlaut spellings (comments and entries under three characters
        skipped, a trailing bracket removed); returns how many were taken and records that
        count under ``source`` when one is given."""
        n = 0
        for raw in terms:
            term = _BRACKET.sub("", raw.strip())
            if len(term) < 3 or term.startswith("#"):
                continue
            for form in _forms(term):
                self._remember(form)
            n += 1
        if source:
            self.sources.append((source, n))
        return n

    def _remember(self, form: str) -> None:
        """Files one normalised form as a single word or, up to ``MAX_WORDS`` tokens, as a
        whole phrase; parts of "Berliner Blau" must not protect "Berlin"."""
        tokens = tuple(t for t in re.split(r"[\s/]+", form) if t)
        if not tokens:
            return
        if len(tokens) == 1:
            self._remember_single_word(tokens[0])
        elif len(tokens) <= MAX_WORDS:
            self.multi_words.setdefault(tokens[0], set()).add(tokens)

    def _remember_single_word(self, word: str) -> None:
        """Adds a single word and, for "Babinski-Zeichen", the eponym "Babinski" that reports
        use on its own."""
        self.single_words.add(word)
        eponym = _leading_eponym(word)
        if eponym:
            self.single_words.add(eponym)

    def is_medical_term(self, word: str) -> bool:
        """Single word, inflected forms and hyphen parts included."""
        return any(self._form_known(form) for form in _forms(word))

    def _form_known(self, form: str) -> bool:
        """Is one normalised form listed, an inflected form of a listed word, or a hyphenated
        compound with a listed part ("Parkinson-Syndrom", "Herz-")?"""
        if form in self.single_words or self._stemmed(form):
            return True
        if "-" in form and any(self._part_known(t) for t in form.split("-")):
            return True
        return form.endswith("-") and form[:-1] in self.single_words

    def _part_known(self, part: str) -> bool:
        """A hyphen part of at least three characters that is listed, inflected or not."""
        return len(part) >= 3 and (part in self.single_words or self._stemmed(part))

    def _stemmed(self, t: str) -> bool:
        """Is ``t`` a listed word with a German inflection suffix on a stem of four or more
        characters?"""
        return any(t.endswith(s) and len(t) - len(s) >= 4 and t[: -len(s)] in self.single_words
                   for s in _SUFFIXES)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans of medical terms in ``text`` (multi-word first, then single)."""
        tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]
        normed = [_norm(w) for _a, _b, w in tokens]
        occupied = [False] * len(tokens)
        result: list[tuple[int, int]] = []
        for i, (a, _b, _w) in enumerate(tokens):
            n = self._phrase_length(normed, i)
            if n:
                result.append((a, tokens[i + n - 1][1]))
                occupied[i:i + n] = [True] * n
        result += [(a, b) for i, (a, b, w) in enumerate(tokens)
                   if not occupied[i] and self._counts_alone(w)]
        result.sort()
        return result

    def _phrase_length(self, normed: list[str], i: int) -> int:
        """Token count of the longest listed phrase that starts at token ``i`` (0 when none);
        the first token is looked up as written and with umlauts transliterated."""
        candidates = self.multi_words.get(normed[i]) or self.multi_words.get(
            normed[i].translate(_TRANS))
        if not candidates:
            return 0
        for sequence in sorted(candidates, key=len, reverse=True):
            if _follows(normed, i, sequence):
                return len(sequence)
        return 0

    def _counts_alone(self, word: str) -> bool:
        """Is a lone token a protected term?  Words of up to three letters count only when
        they start with a capital: "hat" is a verb, "Ohr", "Arm" and "HAT" are terms."""
        if len(word) <= 3 and not word[:1].isupper():
            return False
        return self.is_medical_term(word)

    def is_protected(self, text: str, a: int, b: int, spans: list[tuple[int, int]] | None = None,
                     whole: bool = False) -> bool:
        """Does ``text[a:b]`` touch a protected term (or, with ``whole``, consist of them)?"""
        spans = self.spans(text) if spans is None else spans
        touched = [s for s in spans if a < s[1] and b > s[0]]
        if not touched:
            return False
        return not whole or _fully_covered(text, a, b, touched)


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
                             f"Unknown format: {format_!r} (liste, csv, mesh-xml)"))
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        new = v.add(terms, name)
        target = v.path
    standard.cache_clear()
    return target, new
