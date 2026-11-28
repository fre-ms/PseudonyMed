"""Error-tolerant record linkage of a patient against the pseudonym store.

A weighted field comparison in the manner of EpiLink (Contiero et al. 2005),
which the Mainzelliste uses: every field contributes a similarity between 0
and 1, weighted by how identifying the field is; the score is the weighted
mean over the fields present on both sides.  Names are compared with the
Dice coefficient over character bigrams, component-wise for compound names
and with swapped first/last names tried as well; equal Kölner Phonetik codes
count as a strong similarity.  Date of birth is compared per component so
that a typo in the day does not break the match.

Scores at or above ``MATCH_THRESHOLD`` are treated as the same person,
scores between the two thresholds are "uncertain" and need a human decision,
anything below is a different person.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from pseudonymed.core.model import Patient, normalise
from pseudonymed.pseudonyms.phonetics import cologne_phonetics

# EpiLink weights w = log2((1 - errorRate) / frequency) from the Mainzelliste defaults
# (config/mainzelliste.conf.default: vorname f=0.000235 e=0.01, nachname f=0.0000271 e=0.008,
# geburtstag f=0.0333 e=0.005, geburtsmonat f=0.0833 e=0.002, geburtsjahr f=0.0286 e=0.004).
WEIGHTS = {
    "surname": 15.2,
    "first_name": 12.0,
    "birth_year": 5.1,
    "birth_month": 3.6,
    "birth_day": 4.9,
}
MATCH_THRESHOLD = 0.95  # Mainzelliste threshold_match
UNCERTAIN_THRESHOLD = 0.80  # Mainzelliste uses 0.6; raised so that mere namesakes do not ask
PHONETIC_MATCH = 0.9


@dataclass
class Comparison:
    """Outcome of scoring a patient against one stored row: the row's identity
    (``code``, ``key``), the weighted score, the similarity per field
    and the row's own name and birth date for display."""

    code: str
    key: str
    score: float
    fields: dict[str, float] = field(default_factory=dict)
    surname: str = ""
    first_name: str = ""
    birth_date: str = ""

    @property
    def same(self) -> bool:
        """True when the score reaches ``MATCH_THRESHOLD``: the same person."""
        return self.score >= MATCH_THRESHOLD

    @property
    def uncertain(self) -> bool:
        """True between the two thresholds: a human has to decide."""
        return UNCERTAIN_THRESHOLD <= self.score < MATCH_THRESHOLD


def _bigrams(text: str) -> list[str]:
    """Character bigrams of the text padded with one space on either side."""
    t = f" {text} "
    return [t[i : i + 2] for i in range(len(t) - 1)]


_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _forms(text: str) -> set[str]:
    """Accent-stripped and transliterated normal forms (Müller -> muller, mueller)."""
    return {normalise(text), normalise(text.lower().translate(_TRANS))} - {""}


def _dice_raw(a: str, b: str) -> float:
    """Dice coefficient over the bigram multisets of two already normalised strings."""
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    shared = 0
    rest = list(bb)
    for g in ba:
        if g in rest:
            rest.remove(g)
            shared += 1
    return 2 * shared / (len(ba) + len(bb))


def dice(a: str, b: str) -> float:
    """Dice coefficient over character bigrams, best over the normal forms of both."""
    fa, fb = _forms(a), _forms(b)
    if not fa or not fb:
        return 0.0
    return max(_dice_raw(x, y) for x in fa for y in fb)


def _parts(name: str) -> list[str]:
    """Components of a compound name, split at blanks, hyphens and commas."""
    return [t for t in re.split(r"[\s\-‐‑–,]+", name.strip()) if t]


def _part_similarity(x: str, y: str) -> float:
    """Dice of two name components, lifted to ``PHONETIC_MATCH`` when their Kölner
    Phonetik codes agree.  Components shorter than three letters are excluded from the
    phonetic shortcut because their codes are too coarse to tell names apart."""
    s = dice(x, y)
    if len(x) >= 3 and len(y) >= 3 and cologne_phonetics(x) == cologne_phonetics(y):
        return max(s, PHONETIC_MATCH)
    return s


def name_similarity(a: str, b: str) -> float:
    """Best of: whole strings, any pair of components; phonetic equality counts 0.9."""
    if not a.strip() or not b.strip():
        return 0.0
    ta, tb = _parts(a), _parts(b)
    candidates = [dice(a, b), *(_part_similarity(x, y) for x in ta for y in tb)]
    if len(ta) > 1 and len(tb) > 1:
        candidates.append(dice("".join(ta), "".join(tb)))
    return max(candidates)


def _date(value: Any) -> dt.date | None:
    """The stored birth date as a date; unparsable or empty values give ``None``."""
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    """The stored value as a string; ``None`` and other empty values become ``""``."""
    return str(value or "")


@dataclass(frozen=True)
class _Candidate:
    """One stored row with missing and ``None`` fields made uniform: names as plain
    strings, the birth date parsed or ``None``."""

    surname: str
    first_name: str
    birth_name: str
    birth_date: dt.date | None

    @classmethod
    def from_row(cls, candidate: dict[str, Any]) -> _Candidate:
        """Build from a store row (keys surname, first_name, birth_name, birth_date)."""
        return cls(
            surname=_text(candidate.get("surname")),
            first_name=_text(candidate.get("first_name")),
            birth_name=_text(candidate.get("birth_name")),
            birth_date=_date(candidate.get("birth_date")),
        )


def _surname_score(patient: Patient, k: _Candidate) -> float:
    """Surname similarity, also accepting either side's birth name for the other's
    surname, so that a marriage between two documents does not break the match."""
    score = name_similarity(patient.surname, k.surname)
    if patient.birth_name:
        score = max(score, name_similarity(patient.birth_name, k.surname))
    if k.birth_name:
        score = max(score, name_similarity(patient.surname, k.birth_name))
    return score


def _swapped_names(patient: Patient, k: _Candidate) -> float:
    """Similarity if first and last name were entered the other way round on one side."""
    return min(name_similarity(patient.surname, k.first_name),
               name_similarity(patient.first_name, k.surname))


def _name_fields(patient: Patient, k: _Candidate) -> dict[str, float]:
    """Name similarities keyed by field, only for names present on both sides.  Swapped
    first and last names replace both scores when they fit better on average."""
    last = _surname_score(patient, k)
    first = name_similarity(patient.first_name, k.first_name)
    swapped = _swapped_names(patient, k)
    if swapped > (last + first) / 2:
        last = first = swapped
    fields: dict[str, float] = {}
    if patient.surname and k.surname:
        fields["surname"] = last
    if patient.first_name and k.first_name:
        fields["first_name"] = first
    return fields


def _day_and_month_swapped(d1: dt.date, d2: dt.date) -> bool:
    """True when day and month differ but match each other crosswise (01.02. vs 02.01.)."""
    return (d1.month != d2.month and d1.day != d2.day
            and d1.day == d2.month and d1.month == d2.day)


def _date_fields(d1: dt.date | None, d2: dt.date | None) -> dict[str, float]:
    """Equality per birth-date component; nothing when a side has no date.  A swapped
    day and month is a common slip and scores half on both components."""
    if not d1 or not d2:
        return {}
    fields = {
        "birth_year": 1.0 if d1.year == d2.year else 0.0,
        "birth_month": 1.0 if d1.month == d2.month else 0.0,
        "birth_day": 1.0 if d1.day == d2.day else 0.0,
    }
    if _day_and_month_swapped(d1, d2):
        fields["birth_month"] = fields["birth_day"] = 0.5
    return fields


def _weighted_score(fields: dict[str, float]) -> float:
    """Weighted mean of the field similarities by ``WEIGHTS``; 0.0 without any field."""
    sum_total = sum(WEIGHTS[f] for f in fields)
    return sum(WEIGHTS[f] * s for f, s in fields.items()) / sum_total if sum_total else 0.0


def compare(patient: Patient, candidate: dict[str, Any]) -> Comparison:
    """Score ``patient`` against one stored row (keys surname, first_name, birth_name,
    birth_date, code, patient_key)."""
    k = _Candidate.from_row(candidate)
    fields = {**_name_fields(patient, k), **_date_fields(patient.birth_date, k.birth_date)}
    return Comparison(
        code=str(candidate.get("code", "")),
        key=str(candidate.get("patient_key", "")),
        score=round(_weighted_score(fields), 4),
        fields=fields,
        surname=k.surname,
        first_name=k.first_name,
        birth_date=k.birth_date.isoformat() if k.birth_date else "",
    )


def match_candidates(patient: Patient, candidates: list[dict[str, Any]]) -> list[Comparison]:
    """All candidates scored, best first; only scores above the lower threshold."""
    results = [compare(patient, k) for k in candidates]
    return sorted((v for v in results if v.score >= UNCERTAIN_THRESHOLD),
                  key=lambda v: -v.score)
