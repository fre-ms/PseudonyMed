"""Error-tolerant record linkage of a patient against the pseudonym store.

A weighted field comparison in the manner of EpiLink (Contiero et al. 2005),
which the Mainzelliste uses: every field contributes a similarity between 0
and 1, weighted by how identifying the field is; the score is the weighted
mean over the fields present on both sides.  Names are compared with the
Dice coefficient over character bigrams, component-wise for compound names
and with swapped first/last names tried as well; equal Kölner Phonetik codes
count as a strong similarity.  Date of birth is compared per component so
that a typo in the day does not break the match.

Scores at or above ``SCHWELLE_GLEICH`` are treated as the same person,
scores between the two thresholds are "uncertain" and need a human decision,
anything below is a different person.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from pseudonymed.kern.modell import Patient, normalisiere
from pseudonymed.pseudonyme.phonetik import koelner_phonetik

# EpiLink weights w = log2((1 - errorRate) / frequency) from the Mainzelliste defaults
# (config/mainzelliste.conf.default: vorname f=0.000235 e=0.01, nachname f=0.0000271 e=0.008,
# geburtstag f=0.0333 e=0.005, geburtsmonat f=0.0833 e=0.002, geburtsjahr f=0.0286 e=0.004).
GEWICHTE = {
    "nachname": 15.2,
    "vorname": 12.0,
    "geburtsjahr": 5.1,
    "geburtsmonat": 3.6,
    "geburtstag": 4.9,
}
SCHWELLE_GLEICH = 0.95  # Mainzelliste threshold_match
SCHWELLE_UNSICHER = 0.80  # Mainzelliste uses 0.6; raised so that mere namesakes do not ask
PHONETISCH_GLEICH = 0.9


@dataclass
class Vergleich:
    """Outcome of scoring a patient against one stored row: the row's identity
    (``kennung``, ``schluessel``), the weighted score, the similarity per field
    and the row's own name and birth date for display."""

    kennung: str
    schluessel: str
    score: float
    felder: dict[str, float] = field(default_factory=dict)
    nachname: str = ""
    vorname: str = ""
    geburtsdatum: str = ""

    @property
    def gleich(self) -> bool:
        """True when the score reaches ``SCHWELLE_GLEICH``: the same person."""
        return self.score >= SCHWELLE_GLEICH

    @property
    def unsicher(self) -> bool:
        """True between the two thresholds: a human has to decide."""
        return SCHWELLE_UNSICHER <= self.score < SCHWELLE_GLEICH


def _bigramme(text: str) -> list[str]:
    """Character bigrams of the text padded with one space on either side."""
    t = f" {text} "
    return [t[i : i + 2] for i in range(len(t) - 1)]


_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _formen(text: str) -> set[str]:
    """Accent-stripped and transliterated normal forms (Müller -> muller, mueller)."""
    return {normalisiere(text), normalisiere(text.lower().translate(_TRANS))} - {""}


def _dice_roh(a: str, b: str) -> float:
    """Dice coefficient over the bigram multisets of two already normalised strings."""
    if a == b:
        return 1.0
    ba, bb = _bigramme(a), _bigramme(b)
    gemeinsam = 0
    rest = list(bb)
    for g in ba:
        if g in rest:
            rest.remove(g)
            gemeinsam += 1
    return 2 * gemeinsam / (len(ba) + len(bb))


def dice(a: str, b: str) -> float:
    """Dice coefficient over character bigrams, best over the normal forms of both."""
    fa, fb = _formen(a), _formen(b)
    if not fa or not fb:
        return 0.0
    return max(_dice_roh(x, y) for x in fa for y in fb)


def _teile(name: str) -> list[str]:
    """Components of a compound name, split at blanks, hyphens and commas."""
    return [t for t in re.split(r"[\s\-‐‑–,]+", name.strip()) if t]


def _teilaehnlichkeit(x: str, y: str) -> float:
    """Dice of two name components, lifted to ``PHONETISCH_GLEICH`` when their Kölner
    Phonetik codes agree.  Components shorter than three letters are excluded from the
    phonetic shortcut because their codes are too coarse to tell names apart."""
    s = dice(x, y)
    if len(x) >= 3 and len(y) >= 3 and koelner_phonetik(x) == koelner_phonetik(y):
        return max(s, PHONETISCH_GLEICH)
    return s


def namensaehnlichkeit(a: str, b: str) -> float:
    """Best of: whole strings, any pair of components; phonetic equality counts 0.9."""
    if not a.strip() or not b.strip():
        return 0.0
    ta, tb = _teile(a), _teile(b)
    kandidaten = [dice(a, b), *(_teilaehnlichkeit(x, y) for x in ta for y in tb)]
    if len(ta) > 1 and len(tb) > 1:
        kandidaten.append(dice("".join(ta), "".join(tb)))
    return max(kandidaten)


def _datum(wert: Any) -> dt.date | None:
    """The stored birth date as a date; unparsable or empty values give ``None``."""
    if isinstance(wert, dt.date):
        return wert
    if isinstance(wert, str) and wert:
        try:
            return dt.date.fromisoformat(wert[:10])
        except ValueError:
            return None
    return None


def _text(wert: Any) -> str:
    """The stored value as a string; ``None`` and other empty values become ``""``."""
    return str(wert or "")


@dataclass(frozen=True)
class _Kandidat:
    """One stored row with missing and ``None`` fields made uniform: names as plain
    strings, the birth date parsed or ``None``."""

    nachname: str
    vorname: str
    geburtsname: str
    geburtsdatum: dt.date | None

    @classmethod
    def aus(cls, kandidat: dict[str, Any]) -> _Kandidat:
        """Build from a store row (keys nachname, vorname, geburtsname, geburtsdatum)."""
        return cls(
            nachname=_text(kandidat.get("nachname")),
            vorname=_text(kandidat.get("vorname")),
            geburtsname=_text(kandidat.get("geburtsname")),
            geburtsdatum=_datum(kandidat.get("geburtsdatum")),
        )


def _nachnamenscore(patient: Patient, k: _Kandidat) -> float:
    """Surname similarity, also accepting either side's birth name for the other's
    surname, so that a marriage between two documents does not break the match."""
    score = namensaehnlichkeit(patient.nachname, k.nachname)
    if patient.geburtsname:
        score = max(score, namensaehnlichkeit(patient.geburtsname, k.nachname))
    if k.geburtsname:
        score = max(score, namensaehnlichkeit(patient.nachname, k.geburtsname))
    return score


def _vertauschte_namen(patient: Patient, k: _Kandidat) -> float:
    """Similarity if first and last name were entered the other way round on one side."""
    return min(namensaehnlichkeit(patient.nachname, k.vorname),
               namensaehnlichkeit(patient.vorname, k.nachname))


def _namensfelder(patient: Patient, k: _Kandidat) -> dict[str, float]:
    """Name similarities keyed by field, only for names present on both sides.  Swapped
    first and last names replace both scores when they fit better on average."""
    nach = _nachnamenscore(patient, k)
    vor = namensaehnlichkeit(patient.vorname, k.vorname)
    getauscht = _vertauschte_namen(patient, k)
    if getauscht > (nach + vor) / 2:
        nach = vor = getauscht
    felder: dict[str, float] = {}
    if patient.nachname and k.nachname:
        felder["nachname"] = nach
    if patient.vorname and k.vorname:
        felder["vorname"] = vor
    return felder


def _tag_und_monat_vertauscht(d1: dt.date, d2: dt.date) -> bool:
    """True when day and month differ but match each other crosswise (01.02. vs 02.01.)."""
    return (d1.month != d2.month and d1.day != d2.day
            and d1.day == d2.month and d1.month == d2.day)


def _datumsfelder(d1: dt.date | None, d2: dt.date | None) -> dict[str, float]:
    """Equality per birth-date component; nothing when a side has no date.  A swapped
    day and month is a common slip and scores half on both components."""
    if not d1 or not d2:
        return {}
    felder = {
        "geburtsjahr": 1.0 if d1.year == d2.year else 0.0,
        "geburtsmonat": 1.0 if d1.month == d2.month else 0.0,
        "geburtstag": 1.0 if d1.day == d2.day else 0.0,
    }
    if _tag_und_monat_vertauscht(d1, d2):
        felder["geburtsmonat"] = felder["geburtstag"] = 0.5
    return felder


def _gewichteter_score(felder: dict[str, float]) -> float:
    """Weighted mean of the field similarities by ``GEWICHTE``; 0.0 without any field."""
    summe = sum(GEWICHTE[f] for f in felder)
    return sum(GEWICHTE[f] * s for f, s in felder.items()) / summe if summe else 0.0


def vergleiche(patient: Patient, kandidat: dict[str, Any]) -> Vergleich:
    """Score ``patient`` against one stored row (keys nachname, vorname, geburtsname,
    geburtsdatum, kennung, schluessel)."""
    k = _Kandidat.aus(kandidat)
    felder = {**_namensfelder(patient, k), **_datumsfelder(patient.geburtsdatum, k.geburtsdatum)}
    return Vergleich(
        kennung=str(kandidat.get("kennung", "")),
        schluessel=str(kandidat.get("schluessel", "")),
        score=round(_gewichteter_score(felder), 4),
        felder=felder,
        nachname=k.nachname,
        vorname=k.vorname,
        geburtsdatum=k.geburtsdatum.isoformat() if k.geburtsdatum else "",
    )


def abgleichen(patient: Patient, kandidaten: list[dict[str, Any]]) -> list[Vergleich]:
    """All candidates scored, best first; only scores above the lower threshold."""
    ergebnisse = [vergleiche(patient, k) for k in kandidaten]
    return sorted((v for v in ergebnisse if v.score >= SCHWELLE_UNSICHER),
                  key=lambda v: -v.score)
