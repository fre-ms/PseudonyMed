"""Medications and laboratory values, redacted only on request, in four independent levels.

Both belong to the medical content and stay by default.  Knowing *which* drug someone takes
is one thing, knowing the dose and the schedule another; knowing *what* was examined is one
thing, the measured value another.  The analysis therefore offers four options, each on its
own (:class:`Selection`), and with them the terms of the lexicon become hits that the visual
review may discard:

* ``drugs``: the drug name, "[Medikament <code>]";
* ``doses``: the dose and the schedule next to a drug name ("500 mg 1-0-1", "2 Tbl.",
  "bei Bedarf", "p. o."), "[Dosierung <code>]"; the drug name itself stays unless asked;
* ``lab_tests``: the lab name, the examination, wherever it stands, "[Laborwert <code>]";
* ``measurements``: the measurement right after a lab name ("2,3 mU/l" in "TSH 2,3 mU/l",
  "< 5 mg/l", "von 6,2 %"), "[Messwert <code>]".

Drug names are those of the lexicon (Wikidata, ATC and INN), less those that also name a
disease, finding, body part or organism ("ASS" is also an autism spectrum disorder) and a
few everyday substances, plus every term of the medical vocabulary filed under the category
"Medikament" (trade names such as Marcumar are missing from Wikidata).  Lab names are the
curated lab list of the lexicon plus the vocabulary category "Laborwert"; the Wikidata lab
rows are too noisy to use ("Zuname", "Arizona").  A term on both lists ("Kalium",
"Insulin", "Eisen") is a lab value when a measurement follows, a medication when a dose
follows, otherwise a lab value when lab values are asked for.

Entries of up to three characters and abbreviations (two capitals or a digit, up to six
characters: "Hb", "LSD", "HbA1c") count only in the listed spelling.  All hits are keyed on
their text and restored by the re-identification like any pseudonym.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from optdoc.pagetext import PageText

from pseudonymed.core.model import Hit, Kind
from pseudonymed.detection import lexicon
from pseudonymed.detection.identifiers import Candidate, Recogniser
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

CATEGORY_DRUG = "Medikament"
CATEGORY_LAB_TEST = "Laborwert"
PREFIX_DOSE = "Dosierung"
PREFIX_MEASUREMENT = "Messwert"
RULE_IDS = {CATEGORY_DRUG: "medizin.medikament", PREFIX_DOSE: "medizin.dosierung",
            CATEGORY_LAB_TEST: "medizin.laborwert", PREFIX_MEASUREMENT: "medizin.messwert"}
VOCABULARY_CATEGORIES = {
    CATEGORY_DRUG: ("medikament", "medikamente"),
    CATEGORY_LAB_TEST: ("laborwert", "laborwerte"),
}
SOURCE_LAB_TESTS = "Laborwerte"  # the curated list; the Wikidata lab rows are left out
# A drug name that also names one of these is no safe medication hit.
_OTHER_CATEGORIES = ("Erkrankung", "Symptom", "Befund", "Anatomie", "Physiologie", "Psyche",
                     "Organismus", "Eponym")
# Everyday substances that the drug classes of Wikidata list too.
_NOT_DRUG = {"sauerstoff", "salz", "kochsalz", "wasser", "zucker", "alkohol", "ethanol",
             "koffein", "nikotin", "luft", "licht", "kaffee", "tee"}
_ABBREVIATION = re.compile(r"^(?=.{2,6}$)(?=(?:.*[A-ZÄÖÜ]){2}|.*\d)")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

# -- measurements -----------------------------------------------------------------------------
_UNIT = (r"(?:%|‰|mmHg|mm/h|fl|fL|pg|INR|Ratio|(?:Tsd\.?|G|T|Mio\.?)/[µμunp]?[lL]"
         r"|/[µμn]?[lL]|[mµμunpkf]?(?:g|mol|U|IU|E|Eq|kat)(?:/(?:[mµμnd]?[lL]|h|min|kg|g"
         r"|24[ \t]?h|mmol))?)(?![\w/])")
_MEASUREMENT = re.compile(
    r"[ \t]*(?:\([^()\n]{1,20}\)[ \t]*)?(?:[:=][ \t]*)?(?:(?:von|bei|mit|auf|ca\.)[ \t]+)?"
    r"(?P<wert>(?:(?:[<>≤≥]=?)[ \t]*)?\d+(?:[.,]\d+)?(?![\d.,/:]*\d)(?:[ \t]*" + _UNIT
    + r")?)")

# -- doses and schedules ------------------------------------------------------------------------
_AMOUNT = r"(?:\d+(?:[.,]\d+)?|½|¼|¾|\d/\d)"
_DOSE_UNIT = (r"(?:mg|g|µg|μg|mcg|ug|ng|ml|l|IE|I\.\s?E\.|IU|U|E|Tbl\.?|Tabl\.?|Tabletten?"
              r"|Tab\.?|Kps\.?|Kapseln?|Tr\.?|Tropfen|Hub|Hübe|Amp\.?|Ampullen?|Supp\.?"
              r"|Zäpfchen|Sprühst(?:oß|öße)|Beutel|Btl\.?|Pflaster|St\.?|Stück"
              r"|Fertigspritzen?|Pen|mmol|mEq|%)")
_PER = (r"(?:\s?/\s?(?:kg(?:\s?KG)?|d|Tag|Tg\.?|h|Std\.?|Woche|Wo\.?|Monat|Mon\.?)"
        r"|\s(?:pro|je)\s(?:Tag|Woche|Monat|kg(?:\sKG)?))?")
_FREQUENCIES = (r"(?:tgl\.?|täglich|/d|/Tag|pro\sTag|wöchentlich|/Woche|pro\sWoche|monatlich"
                r"|pro\sMonat)")
_DOSE_WORDS = (r"(?:morgens|mittags|abends|nachts|zur\sNacht|z\.\s?N\.|bei\sBedarf|b\.\s?B\."
               r"|nüchtern|vor\sdem\s(?:Essen|Schlafen(?:gehen)?)|nach\sdem\sEssen|zum\sEssen"
               r"|zu\sden\sMahlzeiten|täglich|tgl\.|wöchentlich|monatlich|jeden\s(?:Morgen|Abend"
               r"|zweiten\sTag)|(?:ein|zwei|drei|vier)mal\s(?:täglich|wöchentlich|pro\sTag|pro\sWoche)"
               r"|alle\s\d+\s?(?:h|Std\.?|Stunden|Tage|Wochen)|p\.\s?o\.|i\.\s?v\.|s\.\s?c\."
               r"|i\.\s?m\.|oral|sublingual|subkutan|intravenös|per\sos|retard|ret\.|Dauermedikation)")
_DOSE_TOKEN = re.compile(
    r"(?:"
    rf"{_AMOUNT}(?:\s?[-–]\s?{_AMOUNT})?\s?{_DOSE_UNIT}{_PER}"  # 500 mg, 1-2 Tbl., 20 mg/kg
    r"|(?:\d+|½|¼|¾|\d/\d)(?:\s?[-–]\s?(?:\d+|½|¼|¾|\d/\d)){1,3}"  # 1-0-1, ½-0-½, 1-0-0-1
    rf"|\d+\s?[x×]\s?(?:\d+(?:[.,]\d+)?|½)(?:\s?{_DOSE_UNIT})?(?:\s?{_FREQUENCIES})?"  # 3x1
    rf"|\d+\s?[x×]\s?{_FREQUENCIES}"  # 2x täglich
    rf"|{_DOSE_WORDS}"
    r"|(?:\d{1,2}|½|¼|¾)"  # a bare count: valid only in a run that also has a unit or words
    r")(?![\wäöü])", re.IGNORECASE)
_BARE = re.compile(r"^(?:\d{1,2}|½|¼|¾)$")
_AMOUNT_WITH_UNIT = re.compile(rf"^{_AMOUNT}(?:\s?[-–]\s?{_AMOUNT})?\s?{_DOSE_UNIT}",
                               re.IGNORECASE)
_SEP = re.compile(r"[ \t,;:/()|®™\-–]{0,4}")
_SEP_WITHOUT_COMMA = re.compile(r"[ \t:/()|®™\-–]{0,4}")  # backwards, a comma ends the run
_BLANKS_ONLY = re.compile(r"[ \t]*")  # a dose before a drug name touches it ("2 Tbl. Ibuprofen")
_LOOKBACK = 80


@dataclass(frozen=True)
class Selection:
    """Which of the four levels are to be redacted."""

    drugs: bool = False  # the drug name
    doses: bool = False  # dose and schedule next to a drug name
    lab_tests: bool = False  # the lab name, the examination
    measurements: bool = False  # the measurement after a lab name

    @classmethod
    def from_options(cls, opt: object) -> Selection:
        """From the analysis options (attributes ``redact_<level>``)."""
        return cls(drugs=bool(getattr(opt, "redact_drugs", False)),
                   doses=bool(getattr(opt, "redact_doses", False)),
                   lab_tests=bool(getattr(opt, "redact_lab_tests", False)),
                   measurements=bool(getattr(opt, "redact_measurements", False)))

    @property
    def active(self) -> bool:
        """Whether any level is to be redacted at all."""
        return self.drugs or self.doses or self.lab_tests or self.measurements

    @property
    def drug_names(self) -> bool:
        """Whether drug names are needed (to redact them or to anchor the doses)."""
        return self.drugs or self.doses

    @property
    def lab_names(self) -> bool:
        """Whether lab names are needed (to redact them or to anchor the measurements)."""
        return self.lab_tests or self.measurements


def _norm(text: str) -> str:
    """Key of a term: whitespace collapsed, case-folded, umlauts transliterated."""
    return " ".join(text.split()).casefold().translate(_TRANS)


def _is_abbreviation(term: str) -> bool:
    """Short entries and abbreviations count only as listed ("Hb", "LSD", "HbA1c")."""
    return len(term) <= 3 or bool(_ABBREVIATION.match(term))


def measurement(text: str, end: int) -> tuple[int, int] | None:
    """Span of a measurement right after a lab name ending at ``end``, or None."""
    m = _MEASUREMENT.match(text, end)
    return (m.start("wert"), m.end("wert")) if m else None


def _valid_run(tokens: list[re.Match[str]]) -> bool:
    """A run of dose tokens counts only with a unit or a word in it; bare counts alone
    ("12", "3") are no dose."""
    return any(not _BARE.match(t.group()) for t in tokens)


def _separates_drugs(separator: str) -> bool:
    """Whether the separator is a comma or semicolon, behind which a new amount with a
    unit belongs to the next drug ("…, 1 Tbl. Paracetamol")."""
    return "," in separator or ";" in separator


def _dose_token_after(text: str, pos: int, in_run: bool) -> re.Match[str] | None:
    """The next dose token behind ``pos``, or None where the run ends: at anything that
    is no token, and inside a run at an amount with a unit behind a comma."""
    sep = _SEP.match(text, pos)
    separator = sep.group() if sep else ""
    tok = _DOSE_TOKEN.match(text, sep.end() if sep else pos)
    if tok is None or tok.end() == tok.start():
        return None
    if in_run and _separates_drugs(separator) and _AMOUNT_WITH_UNIT.match(tok.group()):
        return None
    return tok


def dose_after(text: str, end: int) -> tuple[int, int] | None:
    """Span of the dose and schedule right after a drug name ending at ``end``."""
    tokens: list[re.Match[str]] = []
    pos = end
    while (tok := _dose_token_after(text, pos, bool(tokens))) is not None:
        tokens.append(tok)
        pos = tok.end()
    if not tokens or not _valid_run(tokens):
        return None
    return tokens[0].start(), tokens[-1].end()


def dose_before(text: str, start: int) -> tuple[int, int] | None:
    """Span of a dose right before a drug name starting at ``start`` ("2 Tbl. Ibuprofen")."""
    begin = max(text.rfind("\n", 0, start) + 1, start - _LOOKBACK)
    before = text[begin:start]
    tokens: list[re.Match[str]] = []
    boundary = len(before)
    for m in reversed(list(_DOSE_TOKEN.finditer(before))):
        separator = _BLANKS_ONLY if not tokens else _SEP_WITHOUT_COMMA
        if separator.fullmatch(before[m.end():boundary]) is None:
            break
        tokens.insert(0, m)
        boundary = m.start()
    if not tokens or not _valid_run(tokens):
        return None
    return begin + tokens[0].start(), begin + tokens[-1].end()


# -- the lists ------------------------------------------------------------------------------------
def _rows(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Drug names and lab names, both always (a term on both lists needs both)."""
    connection.executescript(lexicon.SCHEMA)
    other = ",".join("?" * len(_OTHER_CATEGORIES))
    lines = [(b, CATEGORY_DRUG) for (b,) in connection.execute(
        "SELECT DISTINCT l.begriff FROM lexikon l WHERE l.klasse = ? AND l.rolle = ?"
        " AND NOT EXISTS (SELECT 1 FROM lexikon m WHERE m.begriff = l.begriff"
        f" AND m.rolle = ? AND m.klasse IN ({other}))",
        (CATEGORY_DRUG, lexicon.MEDICAL, lexicon.MEDICAL, *_OTHER_CATEGORIES))]
    lines += [(b, CATEGORY_LAB_TEST) for (b,) in connection.execute(
        "SELECT DISTINCT begriff FROM lexikon WHERE klasse = ? AND quelle = ?",
        (CATEGORY_LAB_TEST, SOURCE_LAB_TESTS))]
    for category, names in VOCABULARY_CATEGORIES.items():
        try:
            lines += [(b, category) for (b,) in connection.execute(
                f"SELECT begriff FROM vokabular WHERE lower(kategorie) IN"
                f" ({','.join('?' * len(names))})", names)]
        except sqlite3.OperationalError:
            pass
    return lines


class MedicalSearch:
    """The matcher over drug and lab names plus the chosen levels."""

    def __init__(self, lines: list[tuple[str, str]], selection: Selection) -> None:
        """Index the ``(begriff, klasse)`` rows; drug names under three characters and the
        everyday substances are left out, a term on both lists is noted in ``both``."""
        self.selection = selection
        self.recogniser = Recogniser(registry_numbers=False)
        per_class: dict[str, set[str]] = {CATEGORY_DRUG: set(), CATEGORY_LAB_TEST: set()}
        for term, category in lines:
            if category == CATEGORY_DRUG and (len(term) < 3
                                              or term.casefold() in _NOT_DRUG):
                continue
            if self.recogniser.add(term, category, exact=_is_abbreviation(term)):
                per_class[category].add(_norm(term))
        self.both = per_class[CATEGORY_DRUG] & per_class[CATEGORY_LAB_TEST]

    def using(self, selection: Selection) -> MedicalSearch:
        """The same lists with another selection (the lists are shared)."""
        new = object.__new__(MedicalSearch)
        new.selection, new.recogniser, new.both = selection, self.recogniser, self.both
        return new

    def missing_lists(self) -> list[str]:
        """Classes asked for whose list is empty (lexicon not loaded?)."""
        missing = []
        if self.selection.drug_names and not self.recogniser.per_class.get(CATEGORY_DRUG):
            missing.append(CATEGORY_DRUG)
        if self.selection.lab_names and not self.recogniser.per_class.get(CATEGORY_LAB_TEST):
            missing.append(CATEGORY_LAB_TEST)
        return missing

    def _category(self, text: str, start: int, end: int, category: str) -> str:
        """A term on both lists: by the context, else by what is asked for."""
        if _norm(text[start:end]) not in self.both:
            return category
        value, dose = measurement(text, end), dose_after(text, end)
        if value is not None and (dose is None or value[1] >= dose[1]):
            return CATEGORY_LAB_TEST  # "Kalium 4,1 mmol/l"; "Kalium 40 mmol 1-0-1" is a dose
        if dose is not None or dose_before(text, start) is not None:
            return CATEGORY_DRUG
        return CATEGORY_LAB_TEST if self.selection.lab_names else CATEGORY_DRUG

    def _drug_spans(self, text: str, k: Candidate) -> list[tuple[int, int, str]]:
        """For a drug term: the name if asked, the doses before and behind it if asked."""
        out: list[tuple[int, int, str]] = []
        if self.selection.drugs:
            out.append((k.start, k.end, CATEGORY_DRUG))
        if self.selection.doses:
            out += [(*span, PREFIX_DOSE)
                    for span in (dose_before(text, k.start), dose_after(text, k.end))
                    if span is not None]
        return out

    def _lab_spans(self, text: str, k: Candidate) -> list[tuple[int, int, str]]:
        """For a lab term: the name if asked, the measurement behind it if asked."""
        out: list[tuple[int, int, str]] = []
        if self.selection.lab_tests:
            out.append((k.start, k.end, CATEGORY_LAB_TEST))
        span = measurement(text, k.end) if self.selection.measurements else None
        if span is not None:
            out.append((*span, PREFIX_MEASUREMENT))
        return out

    def spans(self, text: str) -> list[tuple[int, int, str]]:
        """(start, end, kind) of every element to redact, kind being the pseudonym prefix."""
        out: list[tuple[int, int, str]] = []
        for k in self.recogniser.candidates(text):
            category = self._category(text, k.start, k.end, k.category)
            if category == CATEGORY_DRUG:
                out += self._drug_spans(text, k)
            else:
                out += self._lab_spans(text, k)
        return sorted(set(out))

    def find(self, page: PageText, assignment: PseudonymAssignment, existing: list[Hit],
             source: str = "text") -> list[Hit]:
        """The chosen elements on one page that no other hit covers."""
        text = page.text
        matches: list[Hit] = []
        for a, b, kind in self.spans(text):
            if any(f.end > f.start and a < f.end and b > f.start for f in existing + matches):
                continue
            plaintext = text[a:b]
            matches.append(Hit(
                page=page.number, start=a, end=b, text=plaintext, kind=Kind.MEDICAL,
                rule=RULE_IDS[kind], pseudonym=assignment.identifier(kind, plaintext, Kind.MEDICAL),
                rects=[tuple(r) for r in page.rects(a, b)],
                font_size=page.font_size(a, b), source=source))
        return matches


# -- the matcher used by the analysis ----------------------------------------------------------
class _DefaultSearch:
    """The matcher for the chosen medical levels, its lists kept until the lexicon or the
    vocabulary changes; ``cache_clear`` makes the next call rebuild them.  A callable
    object rather than a function with a bolted-on attribute, so that ``cache_clear`` is
    typed."""

    def __init__(self) -> None:
        """Starts without cached lists."""
        self._cache: tuple[Path, int, int, MedicalSearch] | None = None

    def __call__(self, selection: Selection) -> MedicalSearch | None:
        """The matcher for the chosen levels (None when none is chosen), its lists rebuilt
        only when the lexicon or the vocabulary changed."""
        if not selection.active:
            return None
        from pseudonymed.detection.vocabulary import Vocabulary

        try:
            with Vocabulary() as v:
                versions = (lexicon.version(v.connection), v.version())
                if self._cache is not None and self._cache[0] == v.path \
                        and self._cache[1:3] == versions:
                    return self._cache[3].using(selection)
                search = MedicalSearch(_rows(v.connection), selection)
                path = v.path
        except (sqlite3.Error, OSError):
            return MedicalSearch([], selection)
        self._cache = (path, *versions, search)
        return search

    def cache_clear(self) -> None:
        """Forget the cached lists (the tests swap the lexicon underneath them)."""
        self._cache = None


standard = _DefaultSearch()

