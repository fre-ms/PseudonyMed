"""Residual-risk estimate of a pseudonymised text: how many people share what is left.

The re-check proves that the patient's own identifiers are gone.  This module asks the
next question: do the details that remain, taken together, still single the patient out?
It follows the k-map idea of disclosure-risk assessment (El Emam; EMA Policy 0070;
Health Canada): every remaining quasi-identifier has a population frequency f, the
expected number of people in the reference population N who share all of them is
λ = N · Π f, and the probability that a match is the right person is
κ = (1 − e^(−λ)) / λ (Rocher, Hendrickx & de Montjoye 2019), which for large groups is
El Emam's 1/k.  Bits (−log₂ f) make the contributions comparable: about 26.3 bits single
one person out of Germany.

What counts, and with which weight:

* sex (1 bit), always, because German prose shows it;
* the year of birth or age (≈ 6.4 bits for one of ~85 cohorts, as PseudonyMed writes
  ``[geb. 1959]``), more when an age next to an exact letter date narrows the window;
* the smallest place named in the text, by the population of that municipality
  (Destatis GV-ISys): a village of 500 is 17.4 bits, a city of 500,000 is 7.4;
* the rarest named rare disease, by its Orphanet prevalence (1/100,000 ≈ 16.6 bits).

Only the strongest place and the strongest diagnosis are counted, because several of
them are usually correlated; the others are listed.  Independence between the counted
attributes is assumed, which is optimistic for atypical combinations, so the result is a
band, not a probability: the published thresholds give the light, λ ≥ 11 (κ ≤ 0.09, the
EMA/Health Canada level) green, 5 ≤ λ < 11 (the k = 5 of German practice guides)
yellow, λ < 5 red.  Rule flags name what the model cannot weigh: ages from 90, exact
dates of reportable events, occupations, employers, family details.

This score is a synthesis of published parts, not a published standard; the report
says so wherever it is shown.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pseudonymed.core.stored import as_stored, field_values

if TYPE_CHECKING:
    from pseudonymed.detection.medical_terms import MedicalTerms

DATA = Path(__file__).resolve().parent.parent / "data"
MUNICIPALITIES = DATA / "gemeinden_de.csv.gz"
PREVALENCES = DATA / "orphanet_praevalenz_de.csv.gz"

REFERENCE_GERMANY = 83_600_000  # Destatis, 31.12.2024
COHORTS = 85  # birth years with a noticeable share of the population
BITS_YEAR = math.log2(COHORTS)  # ≈ 6.41
BITS_AGE_AND_DATE = 2.6  # age next to an exact letter date: a window of ~59 of 365 days
LAMBDA_RED = 5.0  # below: red (κ > 0.2)
LAMBDA_GREEN = 11.0  # from here: green (κ ≤ 0.09)
SMALL_MUNICIPALITY = 20_000  # Safe-Harbor analogue: smaller places should not stand in clear
RARE = 5 / 10_000  # EU definition of a rare disease
MIN_PLACE_LENGTH = 4

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
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

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
    return word.casefold().translate(_TRANS)


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


# -- data ------------------------------------------------------------------------------------------
@dataclass
class Municipality:
    """One municipality of the Destatis register with the population that sets its weight."""

    name: str
    population: int
    postcode: str
    country: str
    namesakes: int = 1  # how many municipalities carry the name


@dataclass
class Disease:
    """One rare disease of the Orphanet table: prevalence ``p`` as a fraction, its class as text."""

    orpha: str
    name: str
    p: float
    category: str
    source: str


_municipalities: tuple[TermIndex, str] | None = None
_diseases: tuple[TermIndex, str] | None = None


def _read(path: Path) -> tuple[str, list[dict[str, str]]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        header = f.readline().lstrip("# ").strip()
        return header, list(csv.DictReader(f))


def municipality_index() -> tuple[TermIndex, str]:
    """Index of municipality names → Gemeinde (largest one for an ambiguous name)."""
    global _municipalities
    if _municipalities is None:
        index = TermIndex(max_words=5)
        source = ""
        if MUNICIPALITIES.exists():
            source, lines = _read(MUNICIPALITIES)
            per_name: dict[str, list[dict[str, str]]] = {}
            for z in lines:
                per_name.setdefault(z["name"], []).append(z)
            for name, group in per_name.items():
                if len(name) < MIN_PLACE_LENGTH or _norm(name) in PLACE_STOPLIST:
                    continue
                largest = max(group, key=lambda z: int(z["einwohner"]))
                index.add(name, Municipality(name, int(largest["einwohner"]),
                                             largest["plz"], largest["land"], len(group)))
        _municipalities = (index, source)
    return _municipalities


def disease_index() -> tuple[TermIndex, str]:
    """Index of rare-disease names and synonyms → Erkrankung, read once from the Orphanet table."""
    global _diseases
    if _diseases is None:
        index = TermIndex(max_words=6)
        source = ""
        if PREVALENCES.exists():
            source, lines = _read(PREVALENCES)
            for z in lines:
                index.add(z["begriff"], Disease(z["orpha"], z["begriff"], float(z["p"]),
                                                z["klasse"], z["quelle"]))
        _diseases = (index, source)
    return _diseases


# -- result ----------------------------------------------------------------------------------------
@dataclass
class Feature:
    """One quasi-identifier that remains in the text, with its weight in bits."""

    STORED_KEYS: ClassVar[dict[str, str]] = {
        "key": "schluessel", "category": "kategorie", "description": "beschreibung",
        "evidence": "beleg", "source": "quelle", "counted": "gezaehlt"}

    key: str  # stable key for the union across documents
    category: str  # Geschlecht, Geburtsdatum, Ort, Diagnose, Datum
    description: str
    bits: float
    evidence: str = ""  # the text that triggered it (from the pseudonymised output)
    source: str = ""
    counted: bool = True  # False: listed, but not in the sum (weaker place/diagnosis)


@dataclass
class Marker:
    """A rule flag without a weight: something the model cannot weigh but a reader must see."""

    STORED_KEYS: ClassVar[dict[str, str]] = {"key": "schluessel", "evidence": "beleg"}

    key: str
    text: str
    evidence: str = ""


@dataclass
class ResidualRisk:
    """The estimate for one text or one patient: features, flags and the reference population;
    λ, κ and the light are derived from them."""

    STORED_KEYS: ClassVar[dict[str, str]] = {
        "features": "merkmale", "markers": "markierungen", "reference": "referenz",
        "earlier": "fruehere", "date_mentions": "datumsangaben"}

    features: list[Feature] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    reference: int = REFERENCE_GERMANY
    earlier: int = 0  # earlier documents of the same patient merged in
    date_mentions: int = 0

    @property
    def bits(self) -> float:
        """Sum of the counted features' bits."""
        return sum(m.bits for m in self.features if m.counted)

    @property
    def reference_bits(self) -> float:
        """log₂ of the reference population: the bits that single one person out of it."""
        return math.log2(max(self.reference, 2))

    @property
    def lam(self) -> float:
        """λ, the expected number of people in the reference population who share all counted
        features."""
        return 2.0 ** (self.reference_bits - self.bits)

    @property
    def kappa(self) -> float:
        """κ, the probability that a match is the right person (Rocher 2019); 1/λ for large
        groups, where the exact form only costs precision."""
        lam = self.lam
        if lam <= 0:
            return 1.0
        if lam > 50:
            return 1.0 / lam
        return (1.0 - math.exp(-lam)) / lam

    @property
    def traffic_light(self) -> str:
        """The light: red below λ = 5, yellow below 11, green from there."""
        lam = self.lam
        if lam < LAMBDA_RED:
            return "rot"
        if lam < LAMBDA_GREEN:
            return "gelb"
        return "gruen"

    @property
    def dominant(self) -> Feature | None:
        """The counted feature with the most bits, None when nothing is counted."""
        counted = [m for m in self.features if m.counted]
        return max(counted, key=lambda m: m.bits) if counted else None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form with the derived figures and the model's name added."""
        d = as_stored(self)
        d.update({"bits": round(self.bits, 2), "referenz_bits": round(self.reference_bits, 2),
                  "lambda": round(self.lam, 3), "kappa": round(self.kappa, 4),
                  "ampel": self.traffic_light,
                  "modell": "k-map-Schätzung (Synthese, kein Standard)"})
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResidualRisk:
        """The inverse of ``as_dict``; the derived figures are recomputed, not read."""
        return cls(features=[Feature(**field_values(Feature, m)) for m in d.get("merkmale", [])],
                   markers=[Marker(**field_values(Marker, m)) for m in d.get("markierungen", [])],
                   reference=int(d.get("referenz", REFERENCE_GERMANY)),
                   earlier=int(d.get("fruehere", 0)),
                   date_mentions=int(d.get("datumsangaben", 0)))

    def short(self) -> str:
        """One line with λ, κ and the light, for the report heading and the GUI."""
        return (f"λ ≈ {_number(self.lam)} gleichartige Personen, κ ≈ {self.kappa:.2f}"
                f" → {self.traffic_light.upper()}")

    def as_lines(self) -> list[str]:
        """Lines for the report: the estimate, the contributions, the flags."""
        lines = [
            "Restrisiko (Schätzung, Synthese nach El Emam/EMA/Rocher, kein Standard): "
            + self.short(),
            f"  Referenz {_thousands(self.reference)} Personen = {self.reference_bits:.1f} Bit;"
            f" verbliebene Merkmale {self.bits:.1f} Bit"
            + (f"; mit {self.earlier} früheren Dokument(en) dieses Patienten" if self.earlier
               else ""),
        ]
        for m in sorted(self.features, key=lambda m: (-m.counted, -m.bits)):
            lines.append(f"  {'•' if m.counted else '◦'} {m.description}: {m.bits:.1f} Bit"
                         + ("" if m.counted else " (nicht gezählt)"))
        for mk in self.markers:
            lines.append(f"  ! {mk.text}" + (f" [{mk.evidence}]" if mk.evidence else ""))
        if self.date_mentions:
            lines.append(f"  {self.date_mentions} exakte Datumsangabe(n) im Text (zählen erst,"
                         " wenn der Angreifer den Patienten kennt)")
        return lines


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _number(x: float) -> str:
    if x >= 100:
        return f"{x:,.0f}".replace(",", ".")
    if x >= 10:
        return f"{x:.0f}"
    return f"{x:.1f}" if x >= 0.1 else f"{x:.3f}"


def traffic_light_text(traffic_light: str) -> str:
    """The threshold a light stands for, in the words of the report."""
    return {"gruen": f"λ ≥ {LAMBDA_GREEN:.0f} (κ ≤ 0,09, EMA-Niveau)",
            "gelb": f"{LAMBDA_RED:.0f} ≤ λ < {LAMBDA_GREEN:.0f} (k = 5 der Praxisleitfäden)",
            "rot": f"λ < {LAMBDA_RED:.0f} (κ > 0,2)"}[traffic_light]


# -- estimate --------------------------------------------------------------------------------------
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
    return PartialResult((Feature("geschlecht", "Geschlecht", "Geschlecht (Anrede, Pseudonym)",
                                  1.0),))


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
    evidence = f"[geb. {born[0]}]" if born else f"Alter {age[0]}"
    features = [Feature("geburt:jahr", "Geburtsdatum", "Geburtsjahr bzw. Alter", BITS_YEAR,
                        evidence,
                        "Annahme: 1 von 85 Jahrgängen")]
    markers: list[Marker] = []
    if born and age and data:
        features.append(Feature("geburt:fenster", "Geburtsdatum",
                                "Geburtsjahr und Alter neben exakten Daten engen den"
                                " Geburtstag ein", BITS_AGE_AND_DATE,
                                f"[geb. {born[0]}] + {age[0]} + {data[0]}"))
        markers.append(Marker("alter_und_datum",
                              "Geburtsjahr und Alter zusammen mit exakten Daten"
                              " im Text: eines davon genügt",
                              f"[geb. {born[0]}], Alter {age[0]}, {data[0]}"))
    if _aged_90_plus(age, born, year):
        markers.append(Marker("alter_90", "Alter ab 90: kleine Kohorte, wie bei"
                              " HIPAA Safe Harbor zusammenfassen (90+)"))
    return PartialResult(tuple(features), tuple(markers))


def _is_patient_place(evidence: str, municipality: Municipality, own: set[str],
                      medical_terms: MedicalTerms | None) -> bool:
    """Whether a matched municipality can be the patient's place: written with a capital, not
    one of the sender's own places, and not a medical term that happens to be a place name."""
    if not evidence[:1].isupper() or _norm(municipality.name) in own:
        return False
    return medical_terms is None or not medical_terms.is_medical_term(municipality.name)


def _place_feature(evidence: str, g: Municipality, reference: int, source: str,
                   counted: bool) -> Feature:
    """The weight of one place: log₂ of the reference population over the municipality's."""
    bits = max(0.0, math.log2(reference / max(g.population, 1)))
    extra = f", {g.namesakes} gleichnamige, größte gezählt" if g.namesakes > 1 else ""
    return Feature(f"ort:{_norm(g.name)}", "Ort",
                   f"Ort {g.name} ({_thousands(g.population)} Einwohner{extra})",
                   bits, evidence, source, counted=counted)


def _small_place(evidence: str, g: Municipality) -> Marker:
    """The flag for a municipality below the Safe-Harbor-like threshold standing in clear."""
    return Marker(f"kleiner_ort:{_norm(g.name)}",
                  f"Ortsname einer Gemeinde unter {_thousands(SMALL_MUNICIPALITY)} Einwohnern"
                  f" im Klartext: {g.name}", evidence)


def _places(text: str, reference: int, own_places: Iterable[str],
            medical_terms: MedicalTerms | None) -> PartialResult:
    """Every municipality named in the text, smallest first.  Only the smallest is counted,
    because several places in one letter are usually correlated; every one under
    ``SMALL_MUNICIPALITY`` inhabitants is flagged."""
    index, source = municipality_index()
    own = {_norm(o.strip()) for o in own_places if o.strip()}
    places = sorted((t for t in index.find(text) if _is_patient_place(*t, own, medical_terms)),
                    key=lambda o: o[1].population)
    features = tuple(_place_feature(evidence, g, reference, source, counted=i == 0)
                     for i, (evidence, g) in enumerate(places))
    markers = tuple(_small_place(evidence, g) for evidence, g in places
                    if g.population < SMALL_MUNICIPALITY)
    return PartialResult(features, markers)


def _first_per_orpha(matches: list[tuple[str, Disease]]) -> list[tuple[str, Disease]]:
    """Keeps the first match per disease, so that a name and its synonym count once."""
    per_orpha: dict[str, tuple[str, Disease]] = {}
    for evidence, e in matches:
        per_orpha.setdefault(e.orpha, (evidence, e))
    return list(per_orpha.values())


def _diagnosis_feature(evidence: str, e: Disease, source: str, counted: bool) -> Feature:
    """The weight of one rare disease: −log₂ of its Orphanet prevalence."""
    bits = -math.log2(max(e.p, 1e-9))
    category = e.category or f"{e.p:g}"
    return Feature(f"diagnose:{e.orpha}", "Diagnose",
                   f"Seltene Erkrankung {e.name} (ORPHA:{e.orpha}, Prävalenz"
                   f" {category}, {e.source})", bits, evidence, source, counted=counted)


def _rare_diseases(text: str) -> PartialResult:
    """Every rare disease named in the text, rarest first.  Only the rarest is counted; those
    at or below the EU threshold of a rare disease are flagged."""
    index, source = disease_index()
    diseases = _first_per_orpha(sorted(index.find(text), key=lambda k: k[1].p))
    features = tuple(_diagnosis_feature(evidence, e, source, counted=i == 0)
                     for i, (evidence, e) in enumerate(diseases))
    markers = tuple(Marker(f"selten:{e.orpha}", f"Benannte seltene Erkrankung: {e.name}",
                           evidence)
                    for evidence, e in diseases if e.p <= RARE)
    return PartialResult(features, markers)


def _excerpt(text: str, start: int, end: int, length: int) -> str:
    """A whitespace-collapsed excerpt of the text, the evidence shown next to a flag."""
    return " ".join(text[start:end].split())[:length]


def _dated_event(text: str) -> Marker | None:
    """The first reportable event (accident, assault, birth …) with an exact date within 80
    characters: such pairs also appear in police reports and local news."""
    for m in _EVENT.finditer(text):
        start, end = max(0, m.start() - 80), m.end() + 80
        if _DATE.search(text[start:end]):
            return Marker("ereignis_datiert", "Datiertes, öffentlich berichtbares Ereignis",
                          _excerpt(text, start, end, 120))
    return None


@dataclass(frozen=True)
class _NotePattern:
    """A pattern whose first match earns an unweighted flag, with an excerpt as evidence."""

    key: str
    note: str
    muster: re.Pattern[str]
    trailing: int  # characters kept after the match
    length: int  # length of the excerpt

    def find(self, text: str) -> Marker | None:
        """The flag for the first match, None when the pattern does not occur."""
        m = self.muster.search(text)
        if m is None:
            return None
        return Marker(self.key, self.note,
                      _excerpt(text, m.start(), m.end() + self.trailing, self.length))


_NOTE_PATTERNS = (
    _NotePattern("beruf", "Beruf, Arbeitgeber oder Institution genannt", _OCCUPATION, 60, 100),
    _NotePattern("familie", "Familienkonstellation genannt", _FAMILY, 40, 80),
)


def _notes(text: str) -> PartialResult:
    """Rule flags the model cannot weigh: a dated reportable event, an occupation or employer,
    a family constellation."""
    found = [_dated_event(text), *(h.find(text) for h in _NOTE_PATTERNS)]
    return PartialResult(markers=tuple(mk for mk in found if mk is not None))


def estimate(text: str, sex: str = "", reference: int = REFERENCE_GERMANY,
             own_places: Iterable[str] = (), medical_terms: MedicalTerms | None = None,
             current_year: int | None = None) -> ResidualRisk:
    """Estimate for one pseudonymised text.  ``sex`` is the patient's sex ("m", "w",
    "") when known; ``own_places`` names places of the sender that are not the patient's
    (practice, clinic); ``medical_terms`` (a MedicalTerms matcher) keeps medical terms from
    being taken for place names.  One detector per family of quasi-identifiers; their
    findings are concatenated in a fixed order."""
    ref = max(int(reference), 100)  # a practice's patients at the least
    year = current_year or dt.date.today().year
    data = _DATE.findall(text)
    parts = (_sex(text, sex), _birth(text, year, data),
             _places(text, ref, own_places, medical_terms), _rare_diseases(text),
             _notes(text))
    return ResidualRisk(features=[m for t in parts for m in t.features],
                        markers=[mk for t in parts for mk in t.markers],
                        reference=ref, date_mentions=len(data))


def _adopt_strongest(target: dict[str, Feature], features: Iterable[Feature]) -> None:
    """Merges features into ``target`` by key, keeping the highest weight and counting every one
    again; which place or diagnosis counts is decided afterwards over the whole set."""
    for m in features:
        old = target.get(m.key)
        if old is None or m.bits > old.bits:
            target[m.key] = Feature(**{**asdict(m), "counted": True})


def _count_only_strongest(features: list[Feature]) -> None:
    """Leaves only the strongest place and the strongest diagnosis counted, because correlated
    attributes must not be multiplied."""
    for category in ("Ort", "Diagnose"):
        group = sorted((m for m in features if m.category == category), key=lambda m: -m.bits)
        for m in group[1:]:
            m.counted = False


def merge(risks: Iterable[ResidualRisk], earlier: Iterable[Feature] = (),
          earlier_count: int = 0) -> ResidualRisk:
    """All documents of one patient together: every key once with its highest weight,
    then again only the strongest place and the strongest diagnosis counted."""
    features: dict[str, Feature] = {}
    markers: dict[str, Marker] = {}
    reference = REFERENCE_GERMANY
    data = 0
    for r in list(risks):
        reference = min(reference, r.reference)
        data += r.date_mentions
        _adopt_strongest(features, r.features)
        for mk in r.markers:
            markers.setdefault(mk.key, mk)
    _adopt_strongest(features, earlier)
    _count_only_strongest(list(features.values()))
    return ResidualRisk(features=list(features.values()), markers=list(markers.values()),
                        reference=reference, earlier=earlier_count, date_mentions=data)
