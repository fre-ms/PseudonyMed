"""Residual-risk estimate of a pseudonymised text: what remains, found in the text and weighed
by identicalc.

The re-check proves that the patient's own identifiers are gone.  This module asks the
next question: do the details that remain, taken together, still single the patient out?
The model, the weights and the population data are identicalc's (a k-map estimate after
El Emam, EMA Policy 0070 and Rocher et al.; a synthesis, not a standard).  What is
PseudonyMed's own is finding the quasi-identifiers in a German letter:

* the sex, from the patient's record or the prose ("Patientin");
* the year of birth or an age (``[geb. 1959]`` as PseudonyMed writes it, "66-jährig"), and
  whether both stand next to exact dates;
* the places named, by the municipality names of identicalc's data, leaving out words
  that are ordinary German far more often than places, the sender's own places and medical
  terms;
* the rare diseases named, by the Orphanet names and synonyms of identicalc's data;
* flags the model cannot weigh: dated reportable events, occupations or employers, family
  constellations.

PseudonyMed's files and its database keep German keys and values (``merkmale``, ``Ort``,
``gruen``); :func:`stored` and :func:`from_stored` translate at that boundary, as
:mod:`pseudonymed.core.stored` does for every file PseudonyMed writes.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import identicalc
from identicalc import Disease, Estimate, Feature, Marker, Municipality, features

from pseudonymed.core.text import english, in_language, say

if TYPE_CHECKING:
    from pseudonymed.detection.medical_terms import MedicalTerms

MIN_PLACE_LENGTH = 4
MIN_REFERENCE = 100  # a practice's patients at the least

_WORD = re.compile(r"[^\W\d_][\w\-]*", re.UNICODE)
_MONTHS = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
_DATE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:19|20)\d{2}\b|\b\d{1,2}\.\s?(?:" + _MONTHS
                   + r")\s+(?:19|20)\d{2}\b")
_BIRTH_YEAR = re.compile(r"\[geb\. (\d{4})\]")
_AGE = re.compile(r"\[Alter (\d{1,3})\]|\b(\d{1,3})-?jährig|\bAlter[:\s]+(\d{1,3})\b"
                  r"|\b(\d{1,3}) Jahre alt\b")
_EVENT = re.compile(r"\b(Unfall|Verkehrsunfall|Arbeitsunfall|Überfall|Übergriff|Einbruch|"
                    r"Brand(?:verletzung)?|Sturz|Suizidversuch|Festnahme|Gerichtstermin|"
                    r"Entbindung|Geburt des|Hochzeit|Beerdigung)\b", re.IGNORECASE)
_OCCUPATION = re.compile(r"\b(?:(?:tätig|arbeitet|arbeitete|berufstätig|beschäftigt|angestellt)"
                         r"(?:\s+\w+){0,2}\s+(?:als|bei|in der|im)\b|von Beruf|Beruf:"
                         r"|Berufstätigkeit als|Arbeitgeber:|Firma|Unternehmen|Lehrer(?:in)? an"
                         r"|Professor(?:in)? (?:an|für)|Bürgermeister(?:in)?|Pfarrer(?:in)?"
                         r"|Abgeordnete(?:r)?)", re.IGNORECASE)
_FAMILY = re.compile(r"\b(?:\d+|ein|zwei|drei|vier|fünf|sechs|sieben) Kinder(?:n)?\b|"
                     r"\bZwilling|\bDrilling|\balleinerziehend|\bverwitwet seit", re.IGNORECASE)

# Municipality names that are ordinary German words far more often than places.
PLACE_STOPLIST = {
    "essen", "halle", "lage", "wald", "berg", "buch", "brand", "grund", "haar", "rot", "lauf",
    "kirchen", "weiden", "hof", "burg", "stein", "aue", "ohne", "sande", "linden", "mark",
    "lehre", "rast", "welt", "hausen", "kreis", "bach", "born", "dorf", "feld", "garten", "horn",
    "holz", "loch", "muehle", "winkel", "zell", "mitte", "ende", "kinder", "mann", "hand",
    "herz", "blut", "druck", "hilfe", "rat", "tag", "nacht", "morgen", "abend", "jahr", "monat",
    "woche", "liebe", "glueck", "ruhe", "kraft", "angst", "sorge", "freude", "schlaf", "ernst",
    "frieden", "hoffnung", "wunsch", "post", "bank", "markt", "platz", "weg", "bruecke", "tor",
    "kirche", "schule", "amt", "haus", "heim", "wehr", "neuss", "wesel", "wert", "sinn", "mode",
    "bitte", "danke", "waren", "stand", "wende", "schwall", "fahren", "lassen", "legen",
    "klein", "gross", "lang", "kurz", "hoch", "tief", "alt", "neu", "frei", "voll", "leer",
    "rund", "eben", "kalt", "warm", "still", "wild", "zart", "fein", "grob", "roh", "reif",
    "heute", "gestern", "immer", "selten", "oft", "gern", "fast", "ganz", "sehr", "zwar",
    "haupt", "seite", "ecke", "spitze", "boden", "decke", "wand", "tuer", "fenster",
    "wasser", "feuer", "erde", "luft", "licht", "schatten", "regen", "schnee", "wind", "sturm",
    "sommer", "winter", "herbst", "fruehling", "sonne", "mond", "stern", "himmel",
    "arzt", "pflege", "praxis", "klinik", "station", "termin", "befund", "diagnose",
}


def _norm(word: str) -> str:
    return features.norm(word)


def _forms(term: str) -> set[str]:
    """Normalised forms of a term, inflections of the last word included."""
    tokens = [_norm(t) for t in re.split(r"[\s/]+", term) if t]
    if not tokens:
        return set()
    forms = {" ".join(tokens)}
    last = tokens[-1]
    if len(last) >= 5:
        for suffix in ("s", "n", "en", "e", "es", "er", "em"):
            forms.add(" ".join(tokens[:-1] + [last + suffix]))
    return forms


class TermIndex:
    """Multi-word terms in a text, found through n-gram lookup on normalised tokens."""

    def __init__(self, max_words: int = 6) -> None:
        self.entries: dict[str, Any] = {}
        self.max_words = max_words

    def add(self, term: str, value: Any) -> None:
        """Registers a term under all its normalised forms; the first value for a form wins."""
        for form in _forms(term):
            self.entries.setdefault(form, value)

    def find(self, text: str) -> list[tuple[str, Any]]:
        """(matched text, value) for every entry that occurs, longest match first, once each."""
        tokens = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
        found: dict[str, tuple[str, Any]] = {}
        occupied: set[int] = set()
        for n in range(self.max_words, 0, -1):
            for i in range(len(tokens) - n + 1):
                if any(k in occupied for k in range(i, i + n)):
                    continue
                key = " ".join(_norm(t[2]) for t in tokens[i:i + n])
                value = self.entries.get(key)
                if value is None:
                    continue
                original = text[tokens[i][0]:tokens[i + n - 1][1]]
                found.setdefault(key, (original, value))
                occupied.update(range(i, i + n))
        return list(found.values())


# -- the names to look for, from identicalc's data -------------------------------------------------
@cache
def municipality_index() -> TermIndex:
    """Index of municipality names → the largest municipality of that name."""
    index = TermIndex(max_words=5)
    per_name: dict[str, list[Municipality]] = {}
    for m in identicalc.municipalities():
        per_name.setdefault(m.name, []).append(m)
    for name, group in per_name.items():
        if len(name) < MIN_PLACE_LENGTH or _norm(name) in PLACE_STOPLIST:
            continue
        index.add(name, max(group, key=lambda m: m.population))
    return index


@cache
def municipality_names() -> frozenset[str]:
    """Every name of a German municipality, in lower case, common words among them ("Essen",
    "Haar"): for a word that context already marks as a place ("in Essen")."""
    return frozenset(m.name.split(",")[0].strip().lower() for m in identicalc.municipalities())


@cache
def disease_index() -> TermIndex:
    """Index of rare-disease names and synonyms → the disease under that name."""
    index = TermIndex(max_words=6)
    for d in identicalc.diseases():
        index.add(d.name, d)
    return index


# -- finding them in a text ------------------------------------------------------------------------
@dataclass(frozen=True)
class PartialResult:
    """What one family of quasi-identifiers contributes: weighted features and unweighted flags.

    Each detector below returns one; ``estimate`` concatenates them in a fixed order, so the
    report lists features and flags family by family, the same from run to run."""

    features: tuple[Feature, ...] = ()
    markers: tuple[Marker, ...] = ()


def _sex(text: str, sex: str) -> PartialResult:
    """One bit for the sex when it is known or the prose gives it away ("Patientin")."""
    if sex not in ("m", "w") and not re.search(r"\bPatientin\b|\bPatient\b", text):
        return PartialResult()
    return PartialResult((features.sex(),))


def _ages_in_text(text: str) -> list[str]:
    """Every age stated in the text, as the digit strings the age patterns captured."""
    return [next(g for g in m.groups() if g) for m in _AGE.finditer(text)]


def _aged_90_plus(age: list[str], born: list[str], year: int) -> bool:
    """Whether any age, stated or derived from a birth year, reaches the 90+ cohort."""
    age_numbers = [int(a) for a in age if a.isdigit()] + [year - int(g) for g in born
                                                               if g.isdigit()]
    return any(a >= 90 for a in age_numbers)


def _birth(text: str, year: int, data: list[str]) -> PartialResult:
    """The birth cohort: year of birth or age, narrowed further when both stand next to exact
    letter dates; ages from 90 are flagged (the small cohort HIPAA Safe Harbor merges)."""
    born = _BIRTH_YEAR.findall(text)
    age = _ages_in_text(text)
    if not (born or age):
        return PartialResult()
    found = [features.birth_year(f"[geb. {born[0]}]" if born else f"Alter {age[0]}")]
    markers: list[Marker] = []
    if born and age and data:
        found.append(features.birth_window(f"[geb. {born[0]}] + {age[0]} + {data[0]}"))
        markers.append(features.age_and_date(f"[geb. {born[0]}], Alter {age[0]}, {data[0]}"))
    if _aged_90_plus(age, born, year):
        markers.append(features.aged_90_plus())
    return PartialResult(tuple(found), tuple(markers))


def _is_patient_place(evidence: str, municipality: Municipality, own: set[str],
                      medical_terms: MedicalTerms | None) -> bool:
    """Whether a matched municipality can be the patient's place: written with a capital, not
    one of the sender's own places, and not a medical term that happens to be a place name."""
    if not evidence[:1].isupper() or _norm(municipality.name) in own:
        return False
    return medical_terms is None or not medical_terms.is_medical_term(municipality.name)


def _places(text: str, reference: int, own_places: Iterable[str],
            medical_terms: MedicalTerms | None) -> PartialResult:
    """Every municipality named in the text, smallest first.  Only the smallest is counted,
    because several places in one letter are usually correlated; every small one is flagged."""
    own = {_norm(o.strip()) for o in own_places if o.strip()}
    places = sorted((t for t in municipality_index().find(text)
                     if _is_patient_place(*t, own, medical_terms)),
                    key=lambda o: o[1].population)
    found = tuple(features.place(g, reference, evidence, counted=i == 0)
                  for i, (evidence, g) in enumerate(places))
    markers = tuple(mk for evidence, g in places
                    if (mk := features.small_place(g, evidence)) is not None)
    return PartialResult(found, markers)


def _first_per_orpha(matches: list[tuple[str, Disease]]) -> list[tuple[str, Disease]]:
    """Keeps the first match per disease, so that a name and its synonym count once."""
    per_orpha: dict[str, tuple[str, Disease]] = {}
    for evidence, d in matches:
        per_orpha.setdefault(d.orpha, (evidence, d))
    return list(per_orpha.values())


def _rare_diseases(text: str) -> PartialResult:
    """Every rare disease named in the text, rarest first.  Only the rarest is counted; those
    at or below the EU threshold of a rare disease are flagged."""
    found = _first_per_orpha(sorted(disease_index().find(text), key=lambda k: k[1].prevalence))
    weighed = tuple(features.rare_disease(d, evidence, counted=i == 0)
                    for i, (evidence, d) in enumerate(found))
    markers = tuple(mk for evidence, d in found
                    if (mk := features.rare_disease_flag(d, evidence)) is not None)
    return PartialResult(weighed, markers)


def _excerpt(text: str, start: int, end: int, length: int) -> str:
    """A whitespace-collapsed excerpt of the text, the evidence shown next to a flag."""
    return " ".join(text[start:end].split())[:length]


def _dated_event(text: str) -> Marker | None:
    """The first reportable event (accident, assault, birth …) with an exact date within 80
    characters: such pairs also appear in police reports and local news."""
    for m in _EVENT.finditer(text):
        start, end = max(0, m.start() - 80), m.end() + 80
        if _DATE.search(text[start:end]):
            return Marker("dated_event",
                          say("Datiertes, öffentlich berichtbares Ereignis",
                              "Dated, publicly reportable event"),
                          _excerpt(text, start, end, 120))
    return None


@dataclass(frozen=True)
class _NotePattern:
    """A pattern whose first match earns an unweighted flag, with an excerpt as evidence."""

    key: str
    note: str
    pattern: re.Pattern[str]
    trailing: int  # characters kept after the match
    length: int  # length of the excerpt

    def find(self, text: str) -> Marker | None:
        """The flag for the first match, None when the pattern does not occur."""
        m = self.pattern.search(text)
        if m is None:
            return None
        return Marker(self.key, self.note,
                      _excerpt(text, m.start(), m.end() + self.trailing, self.length))


_NOTE_PATTERNS = (
    _NotePattern("occupation", say("Beruf, Arbeitgeber oder Institution genannt",
                                   "Occupation, employer or institution named"), _OCCUPATION,
                 60, 100),
    _NotePattern("family", say("Familienkonstellation genannt", "Family constellation named"),
                 _FAMILY, 40, 80),
)


def _notes(text: str) -> PartialResult:
    """Rule flags the model cannot weigh: a dated reportable event, an occupation or employer,
    a family constellation."""
    found = [_dated_event(text), *(h.find(text) for h in _NOTE_PATTERNS)]
    return PartialResult(markers=tuple(mk for mk in found if mk is not None))


def estimate(text: str, sex: str = "", reference: int = identicalc.REFERENCE_GERMANY,
             own_places: Iterable[str] = (), medical_terms: MedicalTerms | None = None,
             current_year: int | None = None) -> Estimate:
    """Estimate for one pseudonymised text.  ``sex`` is the patient's sex ("m", "w",
    "") when known; ``own_places`` names places of the sender that are not the patient's
    (practice, clinic); ``medical_terms`` (a MedicalTerms matcher) keeps medical terms from
    being taken for place names.  One detector per family of quasi-identifiers; their
    findings are concatenated in a fixed order."""
    ref = max(int(reference), MIN_REFERENCE)
    year = current_year or dt.date.today().year
    data = _DATE.findall(text)
    parts = (_sex(text, sex), _birth(text, year, data),
             _places(text, ref, own_places, medical_terms), _rare_diseases(text),
             _notes(text))
    return Estimate(features=[m for t in parts for m in t.features],
                    markers=[mk for t in parts for mk in t.markers],
                    reference=ref, date_mentions=len(data))


# -- PseudonyMed's stored form ---------------------------------------------------------------------
_CATEGORIES = {identicalc.SEX: "Geschlecht", identicalc.BIRTH: "Geburtsdatum",
               identicalc.PLACE: "Ort", identicalc.DIAGNOSIS: "Diagnose"}
# keys as identicalc and PseudonyMed's own detectors make them → as PseudonyMed stores them;
# an entry ending in ":" is a prefix
_KEYS = (("sex", "geschlecht"), ("birth:year", "geburt:jahr"),
         ("birth:window", "geburt:fenster"), ("place:", "ort:"), ("diagnosis:", "diagnose:"),
         ("small_place:", "kleiner_ort:"), ("rare:", "selten:"),
         ("age_and_date", "alter_und_datum"), ("age_90", "alter_90"),
         ("dated_event", "ereignis_datiert"), ("occupation", "beruf"), ("family", "familie"))
_LIGHTS = {identicalc.GREEN: "gruen", identicalc.YELLOW: "gelb", identicalc.RED: "rot"}


def _translated_key(key: str, pairs: Iterable[tuple[str, str]]) -> str:
    """A key with its name or prefix replaced by the pair's other side."""
    for old, new in pairs:
        if key == old or (old.endswith(":") and key.startswith(old)):
            return new + key[len(old):]
    return key


def _stored_key(key: str) -> str:
    return _translated_key(key, _KEYS)


def _key_from_stored(key: str) -> str:
    return _translated_key(key, ((de, en) for en, de in _KEYS))


def light_colour(e: Estimate) -> str:
    """The light of an estimate in PseudonyMed's words for its lights ("gruen", "gelb",
    "rot")."""
    return _LIGHTS[e.light]


def stored_feature(m: Feature, language: str = "de") -> dict[str, Any]:
    """A feature as PseudonyMed's reports and database keep it."""
    en = english(language)
    return {"schluessel": _stored_key(m.key), "kategorie": _CATEGORIES.get(m.category, m.category),
            "beschreibung": in_language(m.description, language) if en else m.description,
            "bits": m.bits, "beleg": m.evidence,
            "quelle": in_language(m.source, language) if en else m.source,
            "gezaehlt": m.counted}


def feature_from_stored(d: dict[str, Any]) -> Feature:
    """The inverse of :func:`stored_feature`; what the database leaves out takes the
    defaults."""
    categories = {de: en for en, de in _CATEGORIES.items()}
    return Feature(_key_from_stored(str(d.get("schluessel", ""))),
                   categories.get(d.get("kategorie", ""), d.get("kategorie", "")),
                   d.get("beschreibung", ""), float(d.get("bits", 0.0)), d.get("beleg", ""),
                   d.get("quelle", ""), bool(d.get("gezaehlt", True)))


def stored(e: Estimate, language: str = "de") -> dict[str, Any]:
    """An estimate as PseudonyMed's reports keep it: German keys and values, the derived
    figures, the model and the identicalc that computed it; with an English ``language`` the
    wording (descriptions, flags, sources) is English."""
    en = english(language)
    return {
        "merkmale": [stored_feature(m, language) for m in e.features],
        "markierungen": [{"schluessel": _stored_key(mk.key),
                          "text": in_language(mk.text, language) if en else mk.text,
                          "beleg": mk.evidence} for mk in e.markers],
        "referenz": e.reference, "fruehere": e.earlier, "datumsangaben": e.date_mentions,
        "bits": round(e.bits, 2), "referenz_bits": round(e.reference_bits, 2),
        "lambda": round(e.lam, 3), "kappa": round(e.kappa, 4), "ampel": light_colour(e),
        "modell": in_language(identicalc.MODEL, language),
        "identicalc": identicalc.__version__}


def from_stored(d: dict[str, Any]) -> Estimate:
    """The inverse of :func:`stored`; the derived figures are computed again, not read."""
    return Estimate(features=[feature_from_stored(m) for m in d.get("merkmale", [])],
                    markers=[Marker(_key_from_stored(str(mk.get("schluessel", ""))),
                                    mk.get("text", ""), mk.get("beleg", ""))
                             for mk in d.get("markierungen", [])],
                    reference=int(d.get("referenz", identicalc.REFERENCE_GERMANY)),
                    earlier=int(d.get("fruehere", 0)),
                    date_mentions=int(d.get("datumsangaben", 0)))
