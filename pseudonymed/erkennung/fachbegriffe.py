"""Medical vocabulary that must never be redacted by guesswork.

Before the NER model and the fuzzy OCR search may touch a word, the page is
scanned for medical terms: diseases, symptoms, anatomy, drugs, procedures,
scores, eponyms.  Spans covered by such a term are protected from those two
heuristics (the deterministic rules for the patient's own identifiers are
not affected, a patient really called "Herz" is still redacted).

The terms live in the ``vokabular`` table of the pseudonym database
(:mod:`pseudonymed.erkennung.vokabular`): the bundled lists (German labels and aliases
from Wikidata for items with MeSH, ICD-10, ATC or INN identifiers, CC0; body parts;
findings terms; the eponyms of :mod:`pseudonymed.erkennung.ner`) are loaded into it on
first use, and the user adds, deletes and imports terms there (vocabulary step,
``pseudonymed fachbegriffe``).  :func:`laden` still reads the bundled files directly, as
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

from pseudonymed import umgebung

BASIS = Path(__file__).resolve().parent.parent / "daten" / "fachbegriffe_de.txt.gz"
KOERPERTEILE = Path(__file__).resolve().parent.parent / "daten" / "koerperteile_de.txt"
BEFUNDBEGRIFFE = Path(__file__).resolve().parent.parent / "daten" / "befundbegriffe_de.txt"
_TOKEN = re.compile(r"[^\W\d_][\w\-]*|\d+[\w\-]*")
_SUFFIXE = ("ern", "en", "es", "er", "em", "e", "n", "s")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_KLAMMER = re.compile(r"\s*\([^)]*\)\s*$")
MAX_WOERTER = 5
# Head nouns after which the first part of a hyphenated term is an eponym that appears on
# its own in reports ("Babinski-Zeichen" -> "Babinski positiv").  Anatomy and chemistry heads
# are deliberately absent: "Müller-Gang" or "Berlin-Blau" must not protect the surname Müller
# or the town Berlin from the name recognition.
_EPONYM_KOEPFE = {
    "zeichen", "syndrom", "test", "probe", "manöver", "manoever", "reflex", "phänomen",
    "phaenomen", "skala", "score", "index", "trias", "kriterien", "klassifikation", "lagerung",
    "griff", "punkt", "position", "stadium", "formel", "gleichung", "koeffizient", "regel",
    "gesetz", "effekt", "reaktion", "methode", "verfahren", "technik", "operation", "naht",
    "schnitt", "zugang",
}


def datenordner() -> Path:
    """The folder of the user's own list files (``fachbegriffe`` in the data folder)."""
    return umgebung.datenordner() / "fachbegriffe"


def _norm(wort: str) -> str:
    """Case-folded word without surrounding punctuation."""
    return wort.casefold().strip(" .,;:!?\"'()[]")


def _formen(begriff: str) -> set[str]:
    """The normalised term as written and with umlauts transliterated (never the empty string)."""
    n = _norm(begriff)
    return {n, n.translate(_TRANS)} - {""}


def _eponym_vorderteil(wort: str) -> str | None:
    """The eponym in front of an eponym head ("Babinski" of "Babinski-Zeichen"), else None.
    Only two-part terms with a first part of four or more characters qualify: "Berlin-Blau"
    has a chemistry head, and in "Konigsmark-Hollander-Berlin-Syndrom" the town matters more."""
    teile = wort.split("-")
    if len(teile) == 2 and teile[1] in _EPONYM_KOEPFE and len(teile[0]) >= 4:
        return teile[0]
    return None


def _folgt(normen: list[str], i: int, folge: tuple[str, ...]) -> bool:
    """Do the normalised tokens from ``i`` on spell the phrase ``folge``, each token as written
    or with umlauts transliterated?"""
    if i + len(folge) > len(normen):
        return False
    return all(n == f or n.translate(_TRANS) == f
               for n, f in zip(normen[i:i + len(folge)], folge, strict=True))


def _ganz_bedeckt(text: str, a: int, b: int, spannen: list[tuple[int, int]]) -> bool:
    """Does every token of ``text[a:b]`` lie inside one of ``spannen`` (phrases count as a
    whole)?"""
    return all(any(s[0] <= m.start() and m.end() <= s[1] for s in spannen)
               for m in _TOKEN.finditer(text, a, b))


@dataclass
class Fachbegriffe:
    """The merged vocabulary with its matcher."""

    einzel: set[str] = field(default_factory=set)
    mehrwort: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    quellen: list[tuple[str, int]] = field(default_factory=list)

    @property
    def anzahl(self) -> int:
        """Number of terms held: single words plus whole phrases."""
        return len(self.einzel) + sum(len(v) for v in self.mehrwort.values())

    def fuege_hinzu(self, begriffe: Iterable[str], quelle: str = "") -> int:
        """Adds raw terms in both umlaut spellings (comments and entries under three characters
        skipped, a trailing bracket removed); returns how many were taken and records that
        count under ``quelle`` when one is given."""
        n = 0
        for roh in begriffe:
            begriff = _KLAMMER.sub("", roh.strip())
            if len(begriff) < 3 or begriff.startswith("#"):
                continue
            for form in _formen(begriff):
                self._merke(form)
            n += 1
        if quelle:
            self.quellen.append((quelle, n))
        return n

    def _merke(self, form: str) -> None:
        """Files one normalised form as a single word or, up to ``MAX_WOERTER`` tokens, as a
        whole phrase; parts of "Berliner Blau" must not protect "Berlin"."""
        tokens = tuple(t for t in re.split(r"[\s/]+", form) if t)
        if not tokens:
            return
        if len(tokens) == 1:
            self._merke_einzelwort(tokens[0])
        elif len(tokens) <= MAX_WOERTER:
            self.mehrwort.setdefault(tokens[0], set()).add(tokens)

    def _merke_einzelwort(self, wort: str) -> None:
        """Adds a single word and, for "Babinski-Zeichen", the eponym "Babinski" that reports
        use on its own."""
        self.einzel.add(wort)
        eponym = _eponym_vorderteil(wort)
        if eponym:
            self.einzel.add(eponym)

    def ist_fachbegriff(self, wort: str) -> bool:
        """Single word, inflected forms and hyphen parts included."""
        return any(self._form_bekannt(form) for form in _formen(wort))

    def _form_bekannt(self, form: str) -> bool:
        """Is one normalised form listed, an inflected form of a listed word, or a hyphenated
        compound with a listed part ("Parkinson-Syndrom", "Herz-")?"""
        if form in self.einzel or self._gestammt(form):
            return True
        if "-" in form and any(self._teil_bekannt(t) for t in form.split("-")):
            return True
        return form.endswith("-") and form[:-1] in self.einzel

    def _teil_bekannt(self, teil: str) -> bool:
        """A hyphen part of at least three characters that is listed, inflected or not."""
        return len(teil) >= 3 and (teil in self.einzel or self._gestammt(teil))

    def _gestammt(self, t: str) -> bool:
        """Is ``t`` a listed word with a German inflection suffix on a stem of four or more
        characters?"""
        return any(t.endswith(s) and len(t) - len(s) >= 4 and t[: -len(s)] in self.einzel
                   for s in _SUFFIXE)

    def spannen(self, text: str) -> list[tuple[int, int]]:
        """Character spans of medical terms in ``text`` (multi-word first, then single)."""
        tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]
        normen = [_norm(w) for _a, _b, w in tokens]
        belegt = [False] * len(tokens)
        ergebnis: list[tuple[int, int]] = []
        for i, (a, _b, _w) in enumerate(tokens):
            n = self._phrasenlaenge(normen, i)
            if n:
                ergebnis.append((a, tokens[i + n - 1][1]))
                belegt[i:i + n] = [True] * n
        ergebnis += [(a, b) for i, (a, b, w) in enumerate(tokens)
                     if not belegt[i] and self._zaehlt_einzeln(w)]
        ergebnis.sort()
        return ergebnis

    def _phrasenlaenge(self, normen: list[str], i: int) -> int:
        """Token count of the longest listed phrase that starts at token ``i`` (0 when none);
        the first token is looked up as written and with umlauts transliterated."""
        kandidaten = self.mehrwort.get(normen[i]) or self.mehrwort.get(
            normen[i].translate(_TRANS))
        if not kandidaten:
            return 0
        for folge in sorted(kandidaten, key=len, reverse=True):
            if _folgt(normen, i, folge):
                return len(folge)
        return 0

    def _zaehlt_einzeln(self, wort: str) -> bool:
        """Is a lone token a protected term?  Words of up to three letters count only when
        they start with a capital: "hat" is a verb, "Ohr", "Arm" and "HAT" are terms."""
        if len(wort) <= 3 and not wort[:1].isupper():
            return False
        return self.ist_fachbegriff(wort)

    def geschuetzt(self, text: str, a: int, b: int, spannen: list[tuple[int, int]] | None = None,
                   ganz: bool = False) -> bool:
        """Does ``text[a:b]`` touch a protected term (or, with ``ganz``, consist of them)?"""
        spannen = self.spannen(text) if spannen is None else spannen
        beruehrt = [s for s in spannen if a < s[1] and b > s[0]]
        if not beruehrt:
            return False
        return not ganz or _ganz_bedeckt(text, a, b, beruehrt)


# -- loading ---------------------------------------------------------------------------------------
def _lies_liste(pfad: Path) -> Iterator[str]:
    """Non-empty, non-comment lines of a plain or gzip-compressed list file, stripped."""
    oeffnen = gzip.open if pfad.suffix == ".gz" else open
    with oeffnen(pfad, "rt", encoding="utf-8", errors="replace") as f:
        for zeile in f:
            zeile = zeile.strip()
            if zeile and not zeile.startswith("#"):
                yield zeile


def _gebuendelt_laden(fb: Fachbegriffe) -> None:
    """Adds the bundled lists that exist on disk, each under its source name, in the order
    base vocabulary, body parts, findings terms."""
    for pfad, quelle in ((BASIS, f"Basis ({BASIS.name})"), (KOERPERTEILE, "Körperteile"),
                         (BEFUNDBEGRIFFE, "Befundbegriffe")):
        if pfad.exists():
            fb.fuege_hinzu(_lies_liste(pfad), quelle)


def _nutzerlisten() -> list[Path]:
    """The user's own list files (``.txt``, ``.gz``) in the data folder, sorted by name;
    empty when the folder does not exist."""
    ordner = datenordner()
    if not ordner.is_dir():
        return []
    return sorted(p for p in ordner.iterdir() if p.suffix in (".txt", ".gz") and p.is_file())


def laden(zusatz: Iterable[Path] = (), mit_basis: bool = True,
          mit_nutzerdaten: bool = True) -> Fachbegriffe:
    """Builds the vocabulary from the files: the bundled lists (``mit_basis``), the eponyms of
    the NER step, the files in ``zusatz`` and the user's own lists (``mit_nutzerdaten``), in
    that order.  The fallback of :func:`standard` when the database is unusable."""
    from pseudonymed.erkennung.ner import EPONYME

    fb = Fachbegriffe()
    if mit_basis:
        _gebuendelt_laden(fb)
    fb.fuege_hinzu((e for e in EPONYME if len(e) >= 4), "Eponyme")
    pfade = list(zusatz) + (_nutzerlisten() if mit_nutzerdaten else [])
    for p in pfade:
        fb.fuege_hinzu(_lies_liste(p), p.name)
    return fb


class _Standardvokabular:
    """The vocabulary used by the pipeline, built from the database table and kept until the
    table's version changes; ``cache_clear`` makes the next call rebuild it.  A callable
    object rather than a function with a bolted-on attribute, so that ``cache_clear`` is
    typed."""

    def __init__(self) -> None:
        """Starts without a cached vocabulary."""
        self._cache: tuple[Path, int, Fachbegriffe] | None = None

    def __call__(self) -> Fachbegriffe:
        """The vocabulary used by the pipeline: built from the database table, rebuilt only
        when the table's version changed; the bundled files when the database is unusable."""
        import sqlite3

        from pseudonymed.erkennung import vokabular as vokmodul

        try:
            pfad = vokmodul.standardpfad()
            with vokmodul.Vokabular(pfad) as v:
                version = v.version()
                if self._cache is not None and self._cache[0] == pfad \
                        and self._cache[1] == version:
                    return self._cache[2]
                fb = Fachbegriffe()
                for kategorie, begriffe in v.je_kategorie().items():
                    fb.fuege_hinzu(begriffe, kategorie)
        except (sqlite3.Error, OSError):
            return laden()
        self._cache = (pfad, version, fb)
        return fb

    def cache_clear(self) -> None:
        """Forgets the cached vocabulary; the next call rebuilds it from the table."""
        self._cache = None


standard = _Standardvokabular()


# -- import ----------------------------------------------------------------------------------
def begriffe_aus_mesh_xml(pfad: Path) -> Iterator[str]:
    """Descriptor names and entry terms of a MeSH XML file (NLM or a translation)."""
    for _ereignis, element in ET.iterparse(pfad, events=("end",)):
        if element.tag in ("DescriptorRecord", "SupplementalRecord"):
            for s in element.iter("String"):
                if s.text and s.text.strip():
                    yield s.text.strip()
            element.clear()


def begriffe_aus_csv(pfad: Path, spalte: int, trennzeichen: str = ";") -> Iterator[str]:
    """One column of a CSV file, for ICD-10-GM, ATC or any other table."""
    with pfad.open(encoding="utf-8", errors="replace", newline="") as f:
        for zeile in csv.reader(f, delimiter=trennzeichen):
            if len(zeile) > spalte and zeile[spalte].strip():
                yield zeile[spalte].strip()


NUTZERLISTE = "nutzervokabular"


def ergaenze(pfad: Path, name: str = NUTZERLISTE) -> tuple[Path, int]:
    """Merge the terms of ``pfad`` (one per line or CSV) into the vocabulary table under
    the category ``name`` ("nutzervokabular" = Nutzervokabular); returns the database
    and how many were new.  This is what the exported ``.vokabular.txt`` files are for:
    the vocabulary grows with every review."""
    from pseudonymed.erkennung import vokabular as vokmodul

    kategorie = vokmodul.KATEGORIE_NUTZER if name == NUTZERLISTE else name
    with vokmodul.Vokabular() as v:
        neu, _gelesen = v.importiere_csv(pfad, kategorie)
        ziel = v.pfad
    standard.cache_clear()
    return ziel, neu


def importiere(pfad: Path, name: str, format_: str = "liste", spalte: int = 0,
               trennzeichen: str = ";") -> tuple[Path, int]:
    """Add the terms of ``pfad`` to the vocabulary table under the category ``name``;
    returns the database and how many were new."""
    if format_ == "mesh-xml":
        begriffe = begriffe_aus_mesh_xml(pfad)
    elif format_ == "csv":
        begriffe = begriffe_aus_csv(pfad, spalte, trennzeichen)
    elif format_ == "liste":
        begriffe = _lies_liste(pfad)
    else:
        raise ValueError(f"Unbekanntes Format: {format_!r} (liste, csv, mesh-xml)")
    from pseudonymed.erkennung import vokabular as vokmodul

    with vokmodul.Vokabular() as v:
        neu = v.fuege_hinzu(begriffe, name)
        ziel = v.pfad
    standard.cache_clear()
    return ziel, neu
