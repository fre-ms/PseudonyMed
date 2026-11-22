"""Identifiers beyond the patient's own data: professions, hospitals, studies, titles.

The GeMTeX annotation guideline for identifying information (2nd version, 7 September
2025, doi:10.5281/zenodo.15747389) marks more than the patient's names and numbers: the
patient's PROFESSION, hospitals and practices (LOCATION_HOSPITAL), studies the patient
takes part in (ID) and academic titles (NAME_TITLE).  The ``feature`` rows of the lexicon
(:mod:`pseudonymed.detection.lexicon`) list such identifiers.  The identifiers step shows
them and lets the user add, change and delete rows, and the analysis redacts what it
finds by default.  Every hit is a suggestion that the visual review may discard.

A bare dictionary match would redact half of every letter ("Bauer", "Leiter", "CARE"),
so each class has a context rule:

* hospitals (``Klinik``): the official name, or its main part (before " – ", without the
  legal form), wherever it stands.  Names made only of generic words ("Klinik für
  Geriatrische Rehabilitation") are skipped;
* professions (``Beruf``): only next to a cue such as "arbeitet als", "von Beruf",
  "gelernte", "Ausbildung zur" or "ist", or in a social-history paragraph.  The first
  profession after a cue takes it ("arbeitet als Arzthelferin beim Hausarzt");
* studies (``Studie``): registry numbers (NCT, DRKS, ISRCTN, EU CT, EudraCT) always;
  acronyms only in exactly the registered spelling and with a study cue nearby;
* titles (``Titel``): only directly before or after a person the analysis found already
  (patient, relatives, referrers, NER).  This pass runs after the NER;
* every other class, including the user's own (``Sonstiges``): wherever it stands.

An employment status (berentet, arbeitslos) is no PROFESSION in GeMTeX and stays.
Identifier hits are guesses like the NER's: they are dashed in the review and are not
part of the final re-check.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from optdoc.pagetext import PageText

from pseudonymed.core.model import Hit, Kind
from pseudonymed.detection import lexicon, list_import
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

CATEGORY_HOSPITAL = "Klinik"
CATEGORY_PROFESSION = "Beruf"
CATEGORY_STUDY = "Studie"
CATEGORY_TITLE = "Titel"
CATEGORY_OTHER = "Sonstiges"
CATEGORIES = (CATEGORY_HOSPITAL, CATEGORY_PROFESSION, CATEGORY_STUDY, CATEGORY_TITLE,
              CATEGORY_OTHER)
PREFIX = {CATEGORY_OTHER: "Identifikator"}
FEATURE_SOURCES = ("Berufe", "Titel", "Kliniken", "Studien")
MIN_LENGTH = 3
MAX_WORDS = 16

# What the step shows and the analysis redacts: identifier rows, without the employment
# status (no GeMTeX type) and without the registry numbers of the studies (the pattern
# below finds every registry number, listed or not; the acronym rows carry the number).
_VISIBLE = ("rolle = 'merkmal' AND klasse != 'Erwerbsstatus'"
            " AND NOT (klasse = 'Studie' AND quelle = 'Studien' AND zusatz != '')")

_TOKEN = re.compile(r"[^\W\d_]\w*|\d+\w*")  # parts of hyphenated words are tokens of their own
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_SUFFIXES = ("ern", "en", "es", "er", "em", "e", "n", "s")
_GAP = re.compile(r"[\s.,'’&/()+:–—-]{0,6}")

# -- hospital names -----------------------------------------------------------------------------
_LEGAL_FORM = re.compile(
    r"[\s,]+(?:gemeinnützige\s+)?(?:g?GmbH(?:\s*&\s*Co\.?\s*KG)?|g?AG|KG|mbH|e\.\s?V\.|eV|SE"
    r"|KdöR|AöR|gGmbH)\.?\s*$", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s+[–—-]\s+")
# "Klinikum Augsburg": a generic hospital word before a town that has a hospital in the list.
_HOSPITAL_WORDS = (
    "Klinikum", "Klinik", "Kliniken", "Krankenhaus", "Kreisklinik", "Kreisklinikum",
    "Kreiskrankenhaus", "Stadtklinik", "Städtisches Klinikum", "Uniklinik", "Uniklinikum",
    "Universitätsklinik", "Universitätsklinikum", "Bezirksklinikum", "Bezirkskrankenhaus",
    "Spital", "Kantonsspital", "Landesklinikum", "Landeskrankenhaus", "Fachklinik",
)
_BRACKET = re.compile(r"\s*\([^)]*\)")
# A one-word name must say that it is a hospital ("Eifelklinik", not "Saarland" or "Caritas").
_HOSPITAL_WORD = re.compile(
    r"(?i)klinik|clinic|spital|krankenhaus|sanatorium|hospital|zentrum|centrum")
_GENERIC = {
    "klinik", "kliniken", "klinikum", "klinika", "krankenhaus", "krankenhaeuser", "fachklinik",
    "fachkliniken", "fachklinikum", "fachkrankenhaus", "tagesklinik", "nachtklinik",
    "rehaklinik", "rehabilitationsklinik", "reha", "rehabilitation", "rehabilitationszentrum",
    "fruehrehabilitation", "zentrum", "zentren", "centrum", "center", "abteilung", "ambulanz",
    "institutsambulanz", "praxis", "mvz", "medizinisches", "versorgungszentrum", "hospital",
    "spital", "spitaeler", "sanatorium", "kurklinik", "akutklinik", "privatklinik",
    "psychiatrie", "psychotherapie", "psychosomatik", "neurologie", "geriatrie", "orthopaedie",
    "chirurgie", "innere", "medizin", "kinder", "jugend", "jugendliche", "erwachsene",
    "kinder-", "jugendpsychiatrie", "gesundheit", "suchtmedizin", "sucht", "entwoehnung",
    "standort", "haus", "station", "tagesstaette", "wohnheim", "heim", "pflegeheim",
    "fuer", "und", "der", "die", "das", "des", "dem", "den", "am", "an", "im", "in", "zum",
    "zur", "st", "sankt", "gmbh", "ggmbh", "ag", "kg", "co", "ev", "mbh", "uniklinik",
    "universitaetsklinikum", "universitaetsklinik", "kreisklinik", "kreiskrankenhaus",
    "stadtklinik", "bezirksklinikum", "bezirkskrankenhaus", "landesklinik", "landeskrankenhaus",
    "allgemeines", "allgemeinkrankenhaus", "evangelisches", "katholisches", "staedtisches",
    "akademisches", "lehrkrankenhaus", "therapie", "therapiezentrum", "psychische",
    "psychischen", "psychiatrisch", "seelische", "seelischen", "behandlung", "pflege",
    "akutgeriatrie", "geriatrische", "neurologische", "psychiatrische", "psychosomatische",
}

# -- professions ----------------------------------------------------------------------------------
# The cue has to stand right before the profession ("gelernter Tischler", "arbeitet seit 2010
# als Busfahrer", "ist Polizist", "Beruf: Lehrerin") or right after it ("Lehrerin von Beruf",
# "als Koch tätig", "Landwirt im Ruhestand").
_PROFESSION_BEFORE = re.compile(
    r"(?i)(?:\b(?:gelernte?[nrms]?|ehemalige?[nrms]?|frühere?[nrms]?|pensionierte?[nrms]?"
    r"|berentete?[nrms]?|verrentete?[nrms]?|selbstst?ändige?[nrms]?|freiberufliche?[nrms]?"
    r"|ausgebildete?[nrms]?|angehende?[nrms]?|arbeitslose?[nrms]?)"
    r"|\b(?:arbeite[nt]?e?[nt]?|gearbeitet|tätig|beschäftigt|angestellt|jobbt|Tätigkeit|Arbeit"
    r"|Job|Stelle|Anstellung|zuletzt|Ausbildung|Umschulung|Lehre|Weiterbildung|Beruf|beruflich)"
    r"\b(?:[ \t]+\S+){0,4}?[ \t]+(?:als|zum|zur)"
    r"|\b(?:von[ \t]+Beruf|Beruf(?:sbezeichnung)?[ \t]*:|beruflich)"
    r"|\b(?:ist|war|sei|wäre|wurde|wird|bin)(?:[ \t]+(?:von[ \t]+Beruf|seit[ \t]+\S+|jetzt|früher"
    r"|dort|hier|eine?r?|gelernte?r?|ehemalige?r?))?)\s+$")
_PROFESSION_AFTER = re.compile(
    r"(?i)^[ \t]*,?[ \t]*(?:\S+[ \t]+){0,3}?(?:von[ \t]+Beruf|tätig|beschäftigt|angestellt"
    r"|gearbeitet|im[ \t]+Ruhestand|in[ \t]+Rente|berentet|a\.[ \t]?D\.)")
_SALUTATION = re.compile(r"(?i)\b(?:Herr|Herrn|Frau|Hr|Fr|Dr|Prof|Familie|Fam)\.?\s+$")
# Words of the classification that rarely name the patient's own profession.
_NOT_PROFESSION = {"angehoerige", "angehoeriger", "ordner", "ordnerin", "mitglied", "partner",
                   "partnerin", "spender", "spenderin", "halter", "halterin", "betreuer",
                   "betreuerin", "patient", "patientin", "pate", "patin"}
_SOCIAL_HISTORY = re.compile(
    r"(?i)\b(?:Sozialanamnese|Berufsanamnese|Sozial-\s*und\s*Berufsanamnese|Soziale\s+Situation"
    r"|Sozialanamnestisch|Beruf\s*:)")
_HEADING = re.compile(r"\n[A-ZÄÖÜ][\w /-]{2,40}:\s*(?:\n|$)")
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-ZÄÖÜ])|\n\s*\n")

# -- studies --------------------------------------------------------------------------------------
_ACRONYM = re.compile(r"(?=(?:.*[A-ZÄÖÜ]){2}|.*\d)(?=(?:.*[^\W\d_]){2})")  # "CARE", "HoPe", "POEM3"
_STUDY_CUE = re.compile(
    r"(?i)\b(?:Studie\w*|Studien\w*|Trial|Register\w*|Kohorte\w*|randomisiert\w*|eingeschlossen"
    r"|Einschluss|Teilnahme|teilnehm\w*|Proband\w*|Prüfzentrum|Prüfärzt\w*|Prüfarzt|Screening)\b")
_STUDY_ID = re.compile(r"\b(?:NCT\d{8}|DRKS\d{8}|ISRCTN\d{8}|20\d{2}-5\d{5}-\d{2}-\d{2})\b")
_EUDRACT = re.compile(r"(?i)\bEudra\s?CT(?:[\s-]*(?:Nr\.?|No\.?|Nummer|number))?[\s:]*"
                      r"((?:19|20)\d{2}-\d{6}-\d{2})\b")

_PERSON_KINDS = (Kind.NAME, Kind.FIRST_NAME, Kind.SURNAME, Kind.BIRTH_NAME)


def _norm(token: str) -> str:
    """Case-folded token with the umlauts transliterated, the key of the index."""
    return token.casefold().translate(_TRANS)


def rule_name(category: str) -> str:
    """Rule id of a class: ``identifier.hospital``, ``identifier.sonstiges`` …"""
    return "identifikator." + (re.sub(r"\W+", "_", _norm(category)).strip("_") or "sonstiges")


def prefix(category: str) -> str:
    """The word in the pseudonym of a class: ``[Klinik <code>]``, ``[Identifikator <code>]``
    for the user's own class."""
    return PREFIX.get(category, category.strip() or PREFIX[CATEGORY_OTHER])


def _specific(name: str) -> bool:
    """Whether a hospital name holds a word of its own (a place, a saint, a brand)
    rather than generic words only."""
    for token in _TOKEN.findall(name):
        part = _norm(token)
        if (len(part) >= 3 and part.isalpha() and part not in _GENERIC
                and not re.search(r"(?:isch|lich)(?:e[nrms]?)?$", part)):
            return True
    return False


def hospital_forms(name: str) -> set[str]:
    """The official name, its main part before a dash and both without the legal form or
    parentheses; only forms with a word of their own."""
    forms = {name, _SEPARATOR.split(name)[0]}
    forms |= {_LEGAL_FORM.sub("", f).strip(" ,") for f in forms}
    forms |= {_BRACKET.sub("", f).strip(" ,") for f in forms}
    return {f for f in forms if len(f) >= 6 and _specific(f)
            and (len(_TOKEN.findall(f)) > 1 or _HOSPITAL_WORD.search(f))}


def _title_forms(term: str) -> set[str]:
    """A title as entered; nothing for a nobiliary particle ("zur", "von")."""
    if len(term) < 2 or (term.islower() and len(term) <= 4):
        return set()
    return {term}


def _study_forms(term: str) -> set[str]:
    """A study acronym as registered; nothing for a plain word ("Dekan", "Radix")."""
    if len(term) < MIN_LENGTH or not _ACRONYM.match(term):
        return set()
    return {term}


def _word_forms(term: str, exact: bool | None) -> set[str]:
    """Any other entry as entered; one matched only in its exact spelling may be short ("Hb")."""
    return {term} if len(term) >= (2 if exact else MIN_LENGTH) else set()


def _forms_for(term: str, category: str, exact: bool | None) -> set[str]:
    """The forms under which an entry of a class goes into the index: hospitals with their
    short forms, professions without words that rarely name the patient's own."""
    if category == CATEGORY_TITLE:
        return _title_forms(term)
    if category == CATEGORY_STUDY:
        return _study_forms(term)
    if category == CATEGORY_HOSPITAL:
        return hospital_forms(term)
    forms = _word_forms(term, exact)
    if category == CATEGORY_PROFESSION:
        return {f for f in forms if _norm(f) not in _NOT_PROFESSION}
    return forms


# -- matching -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Pattern:
    """One indexed form of an entry: its tokens as typed and normalised."""

    normal: tuple[str, ...]
    original: tuple[str, ...]
    category: str
    exact: bool  # registered spelling only (acronyms, titles)
    closing: str  # closing full stop or parenthesis of the entry ("Dr. med.", "Halle (Saale)")


@dataclass(frozen=True)
class Candidate:
    """A span of the text that an entry of the class matched."""

    start: int
    end: int
    category: str


class Recogniser:
    """The identifier rows with their matcher."""

    def __init__(self, registry_numbers: bool = True) -> None:
        self.registry_numbers = registry_numbers  # NCT, DRKS … as study hits
        self.index: dict[str, list[_Pattern]] = {}
        self.per_class: dict[str, int] = {}
        self._seen: set[tuple[tuple[str, ...], str]] = set()

    @property
    def count(self) -> int:
        """How many rows the matcher took on, over all classes."""
        return sum(self.per_class.values())

    def add(self, term: str, category: str, exact: bool | None = None) -> int:
        """Add one row (hospitals with their short forms); returns how many forms.
        ``exact`` asks for the registered spelling (default: titles and studies only)."""
        term = " ".join(term.split())
        new = sum(self._form(form, category, exact) for form in _forms_for(term, category, exact))
        if new:
            self.per_class[category] = self.per_class.get(category, 0) + 1
        return new

    def add_place(self, place: str) -> int:
        """Short hospital names for a town with a hospital: "Klinikum <town>",
        "Krankenhaus <town>" …, also without a parenthesised addition ("Halle (Saale)")."""
        places = {" ".join(place.split()), _BRACKET.sub("", place).strip()}
        return sum(self._form(f"{w} {o}", CATEGORY_HOSPITAL)
                   for o in places if len(o) >= 3 for w in _HOSPITAL_WORDS)

    def _form(self, form: str, category: str, exact: bool | None = None) -> int:
        """Index one form (1) unless it is empty, too long or known already (0)."""
        original = tuple(_TOKEN.findall(form))
        if not original or len(original) > MAX_WORDS:
            return 0
        normal = tuple(_norm(t) for t in original)
        if (normal, category) in self._seen:
            return 0
        self._seen.add((normal, category))
        if exact is None:
            exact = category in (CATEGORY_TITLE, CATEGORY_STUDY)
        self.index.setdefault(normal[0], []).append(
            _Pattern(normal, original, category, exact, form[-1] if form[-1] in ".)" else ""))
        return 1

    @staticmethod
    def _matches(muster_original: str, muster_normal: str, token: str, exact: bool) -> bool:
        """Whether a token is one word of an entry: exactly, or normalised with an
        inflection ending, keeping a capital where the entry has one."""
        if exact:
            return token == muster_original
        if muster_original[:1].isupper() and not token[:1].isupper():
            return False
        n = _norm(token)
        if n == muster_normal:
            return True
        return len(muster_normal) >= 4 and any(
            n.endswith(s) and n[:-len(s)] == muster_normal for s in _SUFFIXES)

    def _starting_at(self, text: str, tokens: list[re.Match[str]],
                     i: int) -> list[tuple[int, _Pattern]]:
        """Every entry that matches from token ``i`` on: (end offset, entry)."""
        n = _norm(tokens[i].group())
        key = {n} | {n[:-len(s)] for s in _SUFFIXES if n.endswith(s) and len(n) > len(s) + 3}
        matches: list[tuple[int, _Pattern]] = []
        for index_key in key:
            for m in self.index.get(index_key, ()):
                end = self._end_of(text, tokens, i, m)
                if end is not None:
                    matches.append((end, m))
        return matches

    def _end_of(self, text: str, tokens: list[re.Match[str]], i: int,
                m: _Pattern) -> int | None:
        """End offset of entry ``m`` when its words match the tokens from ``i`` on, each
        joined to the previous one; None otherwise."""
        if i + len(m.normal) > len(tokens):
            return None
        for k, (orig, norm) in enumerate(zip(m.original, m.normal, strict=True)):
            t = tokens[i + k]
            if k and not _connected(text, tokens[i + k - 1], t):
                return None
            if not self._matches(orig, norm, t.group(), m.exact):
                return None
        end = tokens[i + len(m.normal) - 1].end()
        if m.closing and text[end:end + 1] == m.closing:
            end += 1
        return end

    def candidates(self, text: str, title: bool = False) -> list[Candidate]:
        """Non-overlapping hits in reading order that pass their class's context rule;
        titles only when asked (they need the people found)."""
        tokens = list(_TOKEN.finditer(text))
        out: list[Candidate] = []
        profession_end = 0
        i = 0
        while i < len(tokens):
            selected = self._match_at(text, tokens, i, title, profession_end)
            if selected is None:
                i += 1
                continue
            out.append(selected)
            if selected.category == CATEGORY_PROFESSION:
                profession_end = selected.end
            while i < len(tokens) and tokens[i].start() < selected.end:
                i += 1
        if not title and self.registry_numbers:
            out += _study_numbers(text, out)
            out.sort(key=lambda k: k.start)
        return out

    def _match_at(self, text: str, tokens: list[re.Match[str]], i: int, title: bool,
                  profession_end: int) -> Candidate | None:
        """The longest entry at token ``i`` of the wanted kind (titles or the rest) that
        passes its class's context rule; None when none does."""
        a = tokens[i].start()
        for end, m in sorted(self._starting_at(text, tokens, i), key=lambda t: -(t[0] - a)):
            if (m.category == CATEGORY_TITLE) == title \
                    and _in_context(m.category, text, a, end, profession_end):
                return Candidate(a, end, m.category)
        return None


def _connected(text: str, before: re.Match[str], token: re.Match[str]) -> bool:
    """Whether two neighbouring tokens belong to one entry: at most six characters of
    punctuation or space between them and no paragraph break."""
    between = text[before.end():token.start()]
    return _GAP.fullmatch(between) is not None and "\n\n" not in between


def _in_context(category: str, text: str, a: int, b: int, profession_end: int) -> bool:
    """Whether a hit passes the context rule of its class: a profession needs a cue, a study
    acronym a study word nearby; every other class matches wherever it stands."""
    if category == CATEGORY_PROFESSION:
        return _profession_in_context(text, a, b, profession_end)
    if category == CATEGORY_STUDY:
        return _study_in_context(text, a, b)
    return True


def _sentence_start(text: str, a: int, boundary: int) -> int:
    """Offset where the sentence around ``a`` begins: after the last sentence end within
    80 characters, never before ``boundary``."""
    begin = max(0, a - 80, boundary)
    last = None
    for m in _SENTENCE_END.finditer(text, begin, a):
        last = m
    return last.end() if last is not None else begin


def _profession_in_context(text: str, a: int, b: int, profession_end: int) -> bool:
    """Whether a profession at ``a:b`` has a cue right before or after it, or stands in a
    social-history paragraph; a salutation before it makes it a name."""
    before = text[_sentence_start(text, a, profession_end):a]
    if _SALUTATION.search(before):
        return False  # "Herr Bauer": a name
    if _PROFESSION_BEFORE.search(before):
        return True
    sentence_end = _SENTENCE_END.search(text, b)
    if _PROFESSION_AFTER.search(text[b:min(b + 60,
                                           sentence_end.start() if sentence_end else len(text))]):
        return True
    window = text[max(0, a - 300, profession_end):a]
    header = None
    for m in _SOCIAL_HISTORY.finditer(window):
        header = m
    return header is not None and not _HEADING.search(window, header.end())


def _study_in_context(text: str, a: int, b: int) -> bool:
    """Whether a study word stands within 40 characters on the same line."""
    before = text[max(0, a - 40):a].rsplit("\n", 1)[-1]
    after = text[b:b + 40].split("\n", 1)[0]
    return bool(_STUDY_CUE.search(before) or _STUDY_CUE.search(after))


def _study_numbers(text: str, occupied: list[Candidate]) -> list[Candidate]:
    """Registry numbers (NCT, DRKS, ISRCTN, EU CT, labelled EudraCT) outside the hits."""
    spans = [(m.start(), m.end()) for m in _STUDY_ID.finditer(text)]
    spans += [(m.start(1), m.end(1)) for m in _EUDRACT.finditer(text)]
    return [Candidate(x, y, CATEGORY_STUDY) for x, y in spans
            if not any(x < k.end and y > k.start for k in occupied)]


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
            matches.append(_hit(page, k.start, k.end, k.category, assignment, source))
    return matches


def find_titles(page: PageText, recogniser: Recogniser, assignment: PseudonymAssignment,
                existing: list[Hit], source: str = "text") -> list[Hit]:
    """Titles directly before a person ("Prof. Dr. med. Person K7Q4MX") or after it
    ("…, Dipl.-Ing."), run after the NER; adjacent titles become one hit."""
    persons = [f for f in existing if f.end > f.start
               and (f.kind in _PERSON_KINDS or f.rule == "ner.person")]
    if not persons:
        return []
    candidates = recogniser.candidates(page.text, title=True)
    matches = [
        _hit(page, chain[0].start, chain[-1].end, CATEGORY_TITLE, assignment, source)
        for p in persons
        for chain in (_titles_before(page.text, candidates, p.start),
                      _titles_after(page.text, candidates, p.end))
        if chain]
    return _without_overlap(matches, existing)


def _adjacent(between: str) -> bool:
    """Whether a title and its neighbour are separated by spaces, one comma and at most
    one line break only."""
    return re.fullmatch(r"[ \t]*,?[ \t]*\n?[ \t]*", between) is not None


def _titles_before(text: str, candidates: list[Candidate], boundary: int) -> list[Candidate]:
    """The unbroken chain of titles that ends right before ``boundary``, a person's start."""
    chain: list[Candidate] = []
    for k in reversed([k for k in candidates if k.end <= boundary]):
        if not _adjacent(text[k.end:boundary]):
            break
        chain.insert(0, k)
        boundary = k.start
    return chain


def _titles_after(text: str, candidates: list[Candidate], boundary: int) -> list[Candidate]:
    """The unbroken chain of titles that starts right after ``boundary``, a person's end."""
    chain: list[Candidate] = []
    for k in [k for k in candidates if k.start >= boundary]:
        if not _adjacent(text[boundary:k.start]):
            break
        chain.append(k)
        boundary = k.end
    return chain


def _without_overlap(matches: list[Hit], existing: list[Hit]) -> list[Hit]:
    """The hits in reading order, the longer first at the same start, each one only where
    neither an existing hit nor an accepted one lies."""
    unique: list[Hit] = []
    for f in sorted(matches, key=lambda f: (f.start, -f.end)):
        if _free(f.start, f.end, existing + unique):
            unique.append(f)
    return unique


# -- the table behind the identifiers step ------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One visible row of the lexicon: the identifier, its class, where it came from, the
    source's id and the JSON addition of the reference data."""

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
        """Whether the user entered or changed the row (bundled rows leave a trace when
        deleted, the user's own do not)."""
        return self.source == lexicon.USER

    @property
    def details(self) -> str:
        """Short description from the reference data (place, registry number, meaning)."""
        description = _DETAILS.get(self.category)
        return description(self) if description else ""


def _hospital_details(e: Entry) -> str:
    """Town and country from a hospital's JSON addition; empty without one."""
    if not e.extra:
        return ""
    try:
        z = json.loads(e.extra)
    except ValueError:
        return ""
    return f"{z.get('ort', '')} ({z.get('land', '')})".replace(" ()", "").strip()


def _study_details(e: Entry) -> str:
    """The registry number behind the registry's name ("ClinicalTrials.gov:NCT…")."""
    return e.code.split(":", 1)[1] if ":" in e.code else ""


def _title_details(e: Entry) -> str:
    """The fields of a title from its ``|``-separated id, "alle" (every field) left out."""
    if "|" not in e.code:
        return ""
    return " · ".join(t for t in e.code.split("|") if t and t != "alle")


def _profession_details(e: Entry) -> str:
    """The KldB 2010 code of a profession when the id is one."""
    return f"KldB {e.code}" if e.code.isdigit() else ""


_DETAILS: dict[str, Callable[[Entry], str]] = {
    CATEGORY_HOSPITAL: _hospital_details, CATEGORY_STUDY: _study_details,
    CATEGORY_TITLE: _title_details, CATEGORY_PROFESSION: _profession_details,
}


class IdentifierTable:
    """Reads and writes the identifier rows of the lexicon in the pseudonym database.
    Deleting or changing a bundled row records the deletion, so that the next update of
    the bundled data does not bring it back; a changed bundled row becomes the user's."""

    def __init__(self, path: str | Path | None = None) -> None:
        from pseudonymed.detection.vocabulary import Vocabulary

        self._vocabulary = Vocabulary(path)
        self.path = self._vocabulary.path
        self.connection = self._vocabulary.connection
        self.connection.executescript(lexicon.SCHEMA)

    def close(self) -> None:
        """Close the database connection."""
        self._vocabulary.close()

    def __enter__(self) -> IdentifierTable:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -------------------------------------------------------------------------------
    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        return lexicon.version(self.connection)

    def count(self) -> int:
        """How many rows the step shows."""
        return int(self.connection.execute(
            f"SELECT COUNT(*) FROM lexikon WHERE {_VISIBLE}").fetchone()[0])

    def categories(self) -> list[tuple[str, int]]:
        """Every class of the visible rows with its number of rows, the largest first."""
        return [(k, n) for k, n in self.connection.execute(
            f"SELECT klasse, COUNT(*) FROM lexikon WHERE {_VISIBLE} GROUP BY klasse"
            " ORDER BY COUNT(*) DESC, klasse")]

    def all_items(self, category: str = "") -> list[Entry]:
        """The visible rows in alphabetical order, of one class when given."""
        sql = f"SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon WHERE {_VISIBLE}"
        values: list[str] = []
        if category:
            sql += " AND klasse = ?"
            values.append(category)
        return [Entry(*r) for r in self.connection.execute(
            sql + " ORDER BY begriff COLLATE NOCASE", values)]

    def deleted(self) -> int:
        """How many bundled rows the user deleted or changed (kept from coming back)."""
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM lexikon_geloescht").fetchone()[0])

    def keys_for(self, term: str, category: str = "") -> list[tuple[str, str, str]]:
        """Keys of the visible rows with this term (case-insensitive), in one class or all."""
        sql = f"SELECT begriff, klasse, kennung FROM lexikon WHERE {_VISIBLE} AND begriff = ?"
        values = [" ".join(term.split())]
        if category:
            sql += " AND klasse = ?"
            values.append(category)
        return [tuple(r) for r in self.connection.execute(sql, values)]

    def _row(self, key: tuple[str, str, str]) -> Entry | None:
        """The row with this key, visible or not; None when there is none."""
        r = self.connection.execute(
            "SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon"
            " WHERE begriff = ? AND klasse = ? AND kennung = ?", key).fetchone()
        return Entry(*r) if r else None

    # -- writing -------------------------------------------------------------------------------
    def _finish(self, n: int) -> int:
        """Commit; raise the version when ``n`` rows changed; return ``n``."""
        if n:
            lexicon.increment_version(self.connection)
        self.connection.commit()
        return n

    def add(self, terms: Iterable[str], category: str = CATEGORY_OTHER) -> int:
        """Insert the user's own identifiers; returns how many were new."""
        category = category.strip() or CATEGORY_OTHER
        before = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, ?, ?, '', '')",
            [(b, category, lexicon.FEATURE, lexicon.USER)
             for b in dict.fromkeys(" ".join(str(b).split()) for b in terms)
             if len(b) >= MIN_LENGTH and not b.startswith("#")])
        return self._finish(self.connection.total_changes - before)

    def _delete(self, e: Entry) -> int:
        """Delete one row, recording the deletion of a bundled one; returns 1 or 0."""
        if not e.own:
            self.connection.execute(
                "INSERT OR IGNORE INTO lexikon_geloescht (begriff, klasse, kennung)"
                " VALUES (?, ?, ?)", e.key)
        before = self.connection.total_changes
        self.connection.execute(
            "DELETE FROM lexikon WHERE begriff = ? AND klasse = ? AND kennung = ?", e.key)
        return self.connection.total_changes - before

    def remove(self, key: Iterable[tuple[str, str, str]]) -> int:
        """Delete the rows with these keys; returns how many went."""
        n = 0
        for s in key:
            e = self._row(s)
            if e is not None:
                n += self._delete(e)
        return self._finish(n)

    def change(self, key: tuple[str, str, str], term: str | None = None,
               category: str | None = None) -> Entry | None:
        """Change term or class of one row and return it as it is now; None when the
        result is too short or exists already."""
        e = self._row(key)
        if e is None:
            return None
        new_term = " ".join((term if term is not None else e.term).split())
        new_category = (category if category is not None else e.category).strip() or CATEGORY_OTHER
        if len(new_term) < MIN_LENGTH or (new_term, new_category) == (e.term, e.category):
            return None
        same_row = (new_term.casefold(), new_category) == (e.term.casefold(), e.category)
        if not same_row and self._row((new_term, new_category, e.code)) is not None:
            return None
        self._delete(e)
        self.connection.execute(
            "INSERT OR REPLACE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (new_term, new_category, lexicon.FEATURE, lexicon.USER, e.code, e.extra))
        self._finish(1)
        return Entry(new_term, new_category, lexicon.USER, e.code, e.extra)

    def restore(self) -> int:
        """Forget the deletions and reload the bundled identifier sources; returns how many
        deletions were forgotten (rows fetched on request, such as the Austrian hospitals,
        have to be fetched again)."""
        n = self.deleted()
        self.connection.execute("DELETE FROM lexikon_geloescht")
        lexicon.ensure(self.connection, force=True, sources=FEATURE_SOURCES)
        self._finish(1)
        return n

    # -- CSV -------------------------------------------------------------------------------------
    def export_csv(self, path: Path, category: str = "") -> int:
        """Write ``begriff,klasse,quelle,angaben`` rows (comma, UTF-8, header line)."""
        lines = self.all_items(category)
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["begriff", "klasse", "quelle", "angaben"])
            w.writerows((e.term, e.category, e.source, e.details) for e in lines)
        return len(lines)

    def import_csv(self, path: Path, category: str = CATEGORY_OTHER) -> tuple[int, int]:
        """Read a CSV (``begriff[,klasse]``, header optional, comma, semicolon or tab) or a
        plain list (one term per line); returns (new, read).  A class column in the file
        wins over ``category``."""
        read_in = list_import.read_list(path, "klasse", category)
        return sum(self.add(b, k) for k, b in read_in.groups.items()), read_in.lines


# -- the matcher used by the analysis ------------------------------------------------------------
class _DefaultRecogniser:
    """The matcher over the identifier rows, kept until the table's version changes;
    ``cache_clear`` makes the next call rebuild it.  A callable object rather than a
    function with a bolted-on attribute, so that ``cache_clear`` is typed."""

    def __init__(self) -> None:
        """Starts without a cached matcher."""
        self._cache: tuple[Path, int, Recogniser] | None = None

    def __call__(self) -> Recogniser:
        """The matcher over the identifier rows, rebuilt only when the lexicon changed;
        without a usable database it knows the registry-number patterns only."""
        try:
            with IdentifierTable() as ident:
                version = ident.version()
                if self._cache is not None and self._cache[0] == ident.path \
                        and self._cache[1] == version:
                    return self._cache[2]
                recogniser = _recogniser_from(ident.connection)
                path = ident.path
        except (sqlite3.Error, OSError):
            return Recogniser()
        self._cache = (path, version, recogniser)
        return recogniser

    def cache_clear(self) -> None:
        """Forget the cached matcher (tests and the step after edits)."""
        self._cache = None


standard = _DefaultRecogniser()


def _recogniser_from(connection: sqlite3.Connection) -> Recogniser:
    """The matcher over the visible rows, with short hospital names for every town that
    has a listed hospital."""
    recogniser = Recogniser()
    places: set[str] = set()
    for term, category, extra in connection.execute(
            f"SELECT begriff, klasse, zusatz FROM lexikon WHERE {_VISIBLE}"):
        recogniser.add(term, category)
        if category == CATEGORY_HOSPITAL:
            places.add(_place_from(extra))
    for place in sorted(places - {""}):
        recogniser.add_place(place)
    return recogniser


def _place_from(extra: str) -> str:
    """The town in a hospital row's JSON addition; empty without one or when unreadable."""
    if not extra.startswith("{"):
        return ""
    try:
        return str(json.loads(extra).get("ort") or "")
    except ValueError:
        return ""

