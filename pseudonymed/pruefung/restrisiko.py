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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pseudonymed.erkennung.fachbegriffe import Fachbegriffe

DATEN = Path(__file__).resolve().parent.parent / "daten"
GEMEINDEN = DATEN / "gemeinden_de.csv.gz"
PRAEVALENZEN = DATEN / "orphanet_praevalenz_de.csv.gz"

REFERENZ_DEUTSCHLAND = 83_600_000  # Destatis, 31.12.2024
KOHORTEN = 85  # birth years with a noticeable share of the population
BITS_JAHR = math.log2(KOHORTEN)  # ≈ 6.41
BITS_ALTER_UND_DATUM = 2.6  # age next to an exact letter date: a window of ~59 of 365 days
LAMBDA_ROT = 5.0  # below: red (κ > 0.2)
LAMBDA_GRUEN = 11.0  # from here: green (κ ≤ 0.09)
KLEINE_GEMEINDE = 20_000  # Safe-Harbor analogue: smaller places should not stand in clear
SELTEN = 5 / 10_000  # EU definition of a rare disease
MIN_ORTSLAENGE = 4

_WORT = re.compile(r"[^\W\d_][\w\-]*", re.UNICODE)
_MONATE = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
_DATUM = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:19|20)\d{2}\b|\b\d{1,2}\.\s?(?:" + _MONATE
                    + r")\s+(?:19|20)\d{2}\b")
_GEB_JAHR = re.compile(r"\[geb\. (\d{4})\]")
_ALTER = re.compile(r"\[Alter (\d{1,3})\]|\b(\d{1,3})-?jährig|\bAlter[:\s]+(\d{1,3})\b"
                    r"|\b(\d{1,3}) Jahre alt\b")
_EREIGNIS = re.compile(r"\b(Unfall|Verkehrsunfall|Arbeitsunfall|Überfall|Übergriff|Einbruch|"
                       r"Brand(?:verletzung)?|Sturz|Suizidversuch|Festnahme|Gerichtstermin|"
                       r"Entbindung|Geburt des|Hochzeit|Beerdigung)\b", re.IGNORECASE)
_BERUF = re.compile(r"\b(?:(?:tätig|arbeitet|arbeitete|berufstätig|beschäftigt|angestellt)"
                    r"(?:\s+\w+){0,2}\s+(?:als|bei|in der|im)\b|von Beruf|Beruf:|Berufstätigkeit"
                    r" als|Arbeitgeber:|Firma|Unternehmen|Lehrer(?:in)? an|Professor(?:in)? (?:an|"
                    r"für)|Bürgermeister(?:in)?|Pfarrer(?:in)?|Abgeordnete(?:r)?)", re.IGNORECASE)
_FAMILIE = re.compile(r"\b(?:\d+|ein|zwei|drei|vier|fünf|sechs|sieben) Kinder(?:n)?\b|"
                      r"\bZwilling|\bDrilling|\balleinerziehend|\bverwitwet seit", re.IGNORECASE)
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

# Municipality names that are ordinary German words far more often than places.
ORTS_STOPPLISTE = {
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


def _norm(wort: str) -> str:
    return wort.casefold().translate(_TRANS)


def _formen(begriff: str) -> set[str]:
    """Normalised forms of a term, inflections of the last word included."""
    tokens = [_norm(t) for t in re.split(r"[\s/]+", begriff) if t]
    if not tokens:
        return set()
    formen = {" ".join(tokens)}
    letztes = tokens[-1]
    if len(letztes) >= 5:
        for suffix in ("s", "n", "en", "e", "es", "er", "em"):
            formen.add(" ".join(tokens[:-1] + [letztes + suffix]))
    return formen


class Begriffsindex:
    """Multi-word terms in a text, found through n-gram lookup on normalised tokens."""

    def __init__(self, max_woerter: int = 6) -> None:
        self.eintraege: dict[str, Any] = {}
        self.max_woerter = max_woerter

    def fuege_hinzu(self, begriff: str, wert: Any) -> None:
        """Registers a term under all its normalised forms; the first value for a form wins."""
        for form in _formen(begriff):
            self.eintraege.setdefault(form, wert)

    def finde(self, text: str) -> list[tuple[str, Any]]:
        """(matched text, value) for every entry that occurs, longest match first, once each."""
        tokens = [(m.start(), m.end(), m.group(0)) for m in _WORT.finditer(text)]
        gefunden: dict[str, tuple[str, Any]] = {}
        belegt: set[int] = set()
        for n in range(self.max_woerter, 0, -1):
            for i in range(len(tokens) - n + 1):
                if any(k in belegt for k in range(i, i + n)):
                    continue
                schluessel = " ".join(_norm(t[2]) for t in tokens[i:i + n])
                wert = self.eintraege.get(schluessel)
                if wert is None:
                    continue
                original = text[tokens[i][0]:tokens[i + n - 1][1]]
                gefunden.setdefault(schluessel, (original, wert))
                belegt.update(range(i, i + n))
        return list(gefunden.values())


# -- data ------------------------------------------------------------------------------------------
@dataclass
class Gemeinde:
    """One municipality of the Destatis register with the population that sets its weight."""

    name: str
    einwohner: int
    plz: str
    land: str
    mehrdeutig: int = 1  # how many municipalities carry the name


@dataclass
class Erkrankung:
    """One rare disease of the Orphanet table: prevalence ``p`` as a fraction, its class as text."""

    orpha: str
    name: str
    p: float
    klasse: str
    quelle: str


_gemeinden: tuple[Begriffsindex, str] | None = None
_erkrankungen: tuple[Begriffsindex, str] | None = None


def _lies(pfad: Path) -> tuple[str, list[dict[str, str]]]:
    with gzip.open(pfad, "rt", encoding="utf-8", newline="") as f:
        kopf = f.readline().lstrip("# ").strip()
        return kopf, list(csv.DictReader(f))


def gemeindenindex() -> tuple[Begriffsindex, str]:
    """Index of municipality names → Gemeinde (largest one for an ambiguous name)."""
    global _gemeinden
    if _gemeinden is None:
        index = Begriffsindex(max_woerter=5)
        quelle = ""
        if GEMEINDEN.exists():
            quelle, zeilen = _lies(GEMEINDEN)
            je_name: dict[str, list[dict[str, str]]] = {}
            for z in zeilen:
                je_name.setdefault(z["name"], []).append(z)
            for name, gruppe in je_name.items():
                if len(name) < MIN_ORTSLAENGE or _norm(name) in ORTS_STOPPLISTE:
                    continue
                groesste = max(gruppe, key=lambda z: int(z["einwohner"]))
                index.fuege_hinzu(name, Gemeinde(name, int(groesste["einwohner"]),
                                                 groesste["plz"], groesste["land"], len(gruppe)))
        _gemeinden = (index, quelle)
    return _gemeinden


def erkrankungsindex() -> tuple[Begriffsindex, str]:
    """Index of rare-disease names and synonyms → Erkrankung, read once from the Orphanet table."""
    global _erkrankungen
    if _erkrankungen is None:
        index = Begriffsindex(max_woerter=6)
        quelle = ""
        if PRAEVALENZEN.exists():
            quelle, zeilen = _lies(PRAEVALENZEN)
            for z in zeilen:
                index.fuege_hinzu(z["begriff"], Erkrankung(z["orpha"], z["begriff"], float(z["p"]),
                                                           z["klasse"], z["quelle"]))
        _erkrankungen = (index, quelle)
    return _erkrankungen


# -- result ----------------------------------------------------------------------------------------
@dataclass
class Merkmal:
    """One quasi-identifier that remains in the text, with its weight in bits."""

    schluessel: str  # stable key for the union across documents
    kategorie: str  # Geschlecht, Geburtsdatum, Ort, Diagnose, Datum
    beschreibung: str
    bits: float
    beleg: str = ""  # the text that triggered it (from the pseudonymised output)
    quelle: str = ""
    gezaehlt: bool = True  # False: listed, but not in the sum (weaker place/diagnosis)


@dataclass
class Markierung:
    """A rule flag without a weight: something the model cannot weigh but a reader must see."""

    schluessel: str
    text: str
    beleg: str = ""


@dataclass
class Restrisiko:
    """The estimate for one text or one patient: features, flags and the reference population;
    λ, κ and the light are derived from them."""

    merkmale: list[Merkmal] = field(default_factory=list)
    markierungen: list[Markierung] = field(default_factory=list)
    referenz: int = REFERENZ_DEUTSCHLAND
    fruehere: int = 0  # earlier documents of the same patient merged in
    datumsangaben: int = 0

    @property
    def bits(self) -> float:
        """Sum of the counted features' bits."""
        return sum(m.bits for m in self.merkmale if m.gezaehlt)

    @property
    def referenz_bits(self) -> float:
        """log₂ of the reference population: the bits that single one person out of it."""
        return math.log2(max(self.referenz, 2))

    @property
    def lam(self) -> float:
        """λ, the expected number of people in the reference population who share all counted
        features."""
        return 2.0 ** (self.referenz_bits - self.bits)

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
    def ampel(self) -> str:
        """The light: red below λ = 5, yellow below 11, green from there."""
        lam = self.lam
        if lam < LAMBDA_ROT:
            return "rot"
        if lam < LAMBDA_GRUEN:
            return "gelb"
        return "gruen"

    @property
    def dominant(self) -> Merkmal | None:
        """The counted feature with the most bits, None when nothing is counted."""
        gezaehlt = [m for m in self.merkmale if m.gezaehlt]
        return max(gezaehlt, key=lambda m: m.bits) if gezaehlt else None

    def als_dict(self) -> dict[str, Any]:
        """JSON-ready form with the derived figures and the model's name added."""
        d = asdict(self)
        d.update({"bits": round(self.bits, 2), "referenz_bits": round(self.referenz_bits, 2),
                  "lambda": round(self.lam, 3), "kappa": round(self.kappa, 4), "ampel": self.ampel,
                  "modell": "k-map-Schätzung (Synthese, kein Standard)"})
        return d

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Restrisiko:
        """The inverse of ``als_dict``; the derived figures are recomputed, not read."""
        return cls(merkmale=[Merkmal(**m) for m in d.get("merkmale", [])],
                   markierungen=[Markierung(**m) for m in d.get("markierungen", [])],
                   referenz=int(d.get("referenz", REFERENZ_DEUTSCHLAND)),
                   fruehere=int(d.get("fruehere", 0)),
                   datumsangaben=int(d.get("datumsangaben", 0)))

    def kurz(self) -> str:
        """One line with λ, κ and the light, for the report heading and the GUI."""
        return (f"λ ≈ {_zahl(self.lam)} gleichartige Personen, κ ≈ {self.kappa:.2f}"
                f" → {self.ampel.upper()}")

    def als_zeilen(self) -> list[str]:
        """Lines for the report: the estimate, the contributions, the flags."""
        zeilen = [
            "Restrisiko (Schätzung, Synthese nach El Emam/EMA/Rocher, kein Standard): "
            + self.kurz(),
            f"  Referenz {_tausender(self.referenz)} Personen = {self.referenz_bits:.1f} Bit;"
            f" verbliebene Merkmale {self.bits:.1f} Bit"
            + (f"; mit {self.fruehere} früheren Dokument(en) dieses Patienten" if self.fruehere
               else ""),
        ]
        for m in sorted(self.merkmale, key=lambda m: (-m.gezaehlt, -m.bits)):
            zeilen.append(f"  {'•' if m.gezaehlt else '◦'} {m.beschreibung}: {m.bits:.1f} Bit"
                          + ("" if m.gezaehlt else " (nicht gezählt)"))
        for mk in self.markierungen:
            zeilen.append(f"  ! {mk.text}" + (f" [{mk.beleg}]" if mk.beleg else ""))
        if self.datumsangaben:
            zeilen.append(f"  {self.datumsangaben} exakte Datumsangabe(n) im Text (zählen erst,"
                          " wenn der Angreifer den Patienten kennt)")
        return zeilen


def _tausender(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _zahl(x: float) -> str:
    if x >= 100:
        return f"{x:,.0f}".replace(",", ".")
    if x >= 10:
        return f"{x:.0f}"
    return f"{x:.1f}" if x >= 0.1 else f"{x:.3f}"


def ampel_text(ampel: str) -> str:
    """The threshold a light stands for, in the words of the report."""
    return {"gruen": f"λ ≥ {LAMBDA_GRUEN:.0f} (κ ≤ 0,09, EMA-Niveau)",
            "gelb": f"{LAMBDA_ROT:.0f} ≤ λ < {LAMBDA_GRUEN:.0f} (k = 5 der Praxisleitfäden)",
            "rot": f"λ < {LAMBDA_ROT:.0f} (κ > 0,2)"}[ampel]


# -- estimate --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Teilergebnis:
    """What one family of quasi-identifiers contributes: weighted features and unweighted flags.

    Each detector below returns one; ``schaetze`` concatenates them in a fixed order, so the
    report lists features and flags family by family, the same from run to run."""

    merkmale: tuple[Merkmal, ...] = ()
    markierungen: tuple[Markierung, ...] = ()


def _geschlecht(text: str, geschlecht: str) -> Teilergebnis:
    """One bit for the sex when it is known or the prose gives it away ("Patientin")."""
    if geschlecht not in ("m", "w") and not re.search(r"\bPatientin\b|\bPatient\b", text):
        return Teilergebnis()
    return Teilergebnis((Merkmal("geschlecht", "Geschlecht", "Geschlecht (Anrede, Pseudonym)",
                                 1.0),))


def _alter_im_text(text: str) -> list[str]:
    """Every age stated in the text, as the digit strings the age patterns captured."""
    return [next(g for g in m.groups() if g) for m in _ALTER.finditer(text)]


def _ab_90(alter: list[str], geb: list[str], jahr: int) -> bool:
    """Whether any age, stated or derived from a birth year, reaches the 90+ cohort."""
    alter_zahlen = [int(a) for a in alter if a.isdigit()] + [jahr - int(g) for g in geb
                                                               if g.isdigit()]
    return any(a >= 90 for a in alter_zahlen)


def _geburt(text: str, jahr: int, daten: list[str]) -> Teilergebnis:
    """The birth cohort: year of birth or age, narrowed further when both stand next to exact
    letter dates; ages from 90 are flagged (the small cohort HIPAA Safe Harbor merges)."""
    geb = _GEB_JAHR.findall(text)
    alter = _alter_im_text(text)
    if not (geb or alter):
        return Teilergebnis()
    beleg = f"[geb. {geb[0]}]" if geb else f"Alter {alter[0]}"
    merkmale = [Merkmal("geburt:jahr", "Geburtsdatum", "Geburtsjahr bzw. Alter", BITS_JAHR, beleg,
                        "Annahme: 1 von 85 Jahrgängen")]
    markierungen: list[Markierung] = []
    if geb and alter and daten:
        merkmale.append(Merkmal("geburt:fenster", "Geburtsdatum",
                                "Geburtsjahr und Alter neben exakten Daten engen den"
                                " Geburtstag ein", BITS_ALTER_UND_DATUM,
                                f"[geb. {geb[0]}] + {alter[0]} + {daten[0]}"))
        markierungen.append(Markierung("alter_und_datum",
                                       "Geburtsjahr und Alter zusammen mit exakten Daten"
                                       " im Text: eines davon genügt",
                                       f"[geb. {geb[0]}], Alter {alter[0]}, {daten[0]}"))
    if _ab_90(alter, geb, jahr):
        markierungen.append(Markierung("alter_90", "Alter ab 90: kleine Kohorte, wie bei"
                                       " HIPAA Safe Harbor zusammenfassen (90+)"))
    return Teilergebnis(tuple(merkmale), tuple(markierungen))


def _ist_patientenort(beleg: str, gemeinde: Gemeinde, eigene: set[str],
                      fachbegriffe: Fachbegriffe | None) -> bool:
    """Whether a matched municipality can be the patient's place: written with a capital, not
    one of the sender's own places, and not a medical term that happens to be a place name."""
    if not beleg[:1].isupper() or _norm(gemeinde.name) in eigene:
        return False
    return fachbegriffe is None or not fachbegriffe.ist_fachbegriff(gemeinde.name)


def _ortsmerkmal(beleg: str, g: Gemeinde, referenz: int, quelle: str, gezaehlt: bool) -> Merkmal:
    """The weight of one place: log₂ of the reference population over the municipality's."""
    bits = max(0.0, math.log2(referenz / max(g.einwohner, 1)))
    zusatz = f", {g.mehrdeutig} gleichnamige, größte gezählt" if g.mehrdeutig > 1 else ""
    return Merkmal(f"ort:{_norm(g.name)}", "Ort",
                   f"Ort {g.name} ({_tausender(g.einwohner)} Einwohner{zusatz})",
                   bits, beleg, quelle, gezaehlt=gezaehlt)


def _kleiner_ort(beleg: str, g: Gemeinde) -> Markierung:
    """The flag for a municipality below the Safe-Harbor-like threshold standing in clear."""
    return Markierung(f"kleiner_ort:{_norm(g.name)}",
                      f"Ortsname einer Gemeinde unter {_tausender(KLEINE_GEMEINDE)} Einwohnern im"
                      f" Klartext: {g.name}", beleg)


def _orte(text: str, referenz: int, eigene_orte: Iterable[str],
          fachbegriffe: Fachbegriffe | None) -> Teilergebnis:
    """Every municipality named in the text, smallest first.  Only the smallest is counted,
    because several places in one letter are usually correlated; every one under
    ``KLEINE_GEMEINDE`` inhabitants is flagged."""
    index, quelle = gemeindenindex()
    eigene = {_norm(o.strip()) for o in eigene_orte if o.strip()}
    orte = sorted((t for t in index.finde(text) if _ist_patientenort(*t, eigene, fachbegriffe)),
                  key=lambda o: o[1].einwohner)
    merkmale = tuple(_ortsmerkmal(beleg, g, referenz, quelle, gezaehlt=i == 0)
                     for i, (beleg, g) in enumerate(orte))
    markierungen = tuple(_kleiner_ort(beleg, g) for beleg, g in orte
                         if g.einwohner < KLEINE_GEMEINDE)
    return Teilergebnis(merkmale, markierungen)


def _erste_je_orpha(treffer: list[tuple[str, Erkrankung]]) -> list[tuple[str, Erkrankung]]:
    """Keeps the first match per disease, so that a name and its synonym count once."""
    je_orpha: dict[str, tuple[str, Erkrankung]] = {}
    for beleg, e in treffer:
        je_orpha.setdefault(e.orpha, (beleg, e))
    return list(je_orpha.values())


def _diagnosemerkmal(beleg: str, e: Erkrankung, quelle: str, gezaehlt: bool) -> Merkmal:
    """The weight of one rare disease: −log₂ of its Orphanet prevalence."""
    bits = -math.log2(max(e.p, 1e-9))
    klasse = e.klasse or f"{e.p:g}"
    return Merkmal(f"diagnose:{e.orpha}", "Diagnose",
                   f"Seltene Erkrankung {e.name} (ORPHA:{e.orpha}, Prävalenz"
                   f" {klasse}, {e.quelle})", bits, beleg, quelle, gezaehlt=gezaehlt)


def _seltene_erkrankungen(text: str) -> Teilergebnis:
    """Every rare disease named in the text, rarest first.  Only the rarest is counted; those
    at or below the EU threshold of a rare disease are flagged."""
    index, quelle = erkrankungsindex()
    krank = _erste_je_orpha(sorted(index.finde(text), key=lambda k: k[1].p))
    merkmale = tuple(_diagnosemerkmal(beleg, e, quelle, gezaehlt=i == 0)
                     for i, (beleg, e) in enumerate(krank))
    markierungen = tuple(Markierung(f"selten:{e.orpha}", f"Benannte seltene Erkrankung: {e.name}",
                                    beleg)
                         for beleg, e in krank if e.p <= SELTEN)
    return Teilergebnis(merkmale, markierungen)


def _ausschnitt(text: str, start: int, ende: int, laenge: int) -> str:
    """A whitespace-collapsed excerpt of the text, the evidence shown next to a flag."""
    return " ".join(text[start:ende].split())[:laenge]


def _datiertes_ereignis(text: str) -> Markierung | None:
    """The first reportable event (accident, assault, birth …) with an exact date within 80
    characters: such pairs also appear in police reports and local news."""
    for m in _EREIGNIS.finditer(text):
        start, ende = max(0, m.start() - 80), m.end() + 80
        if _DATUM.search(text[start:ende]):
            return Markierung("ereignis_datiert", "Datiertes, öffentlich berichtbares Ereignis",
                              _ausschnitt(text, start, ende, 120))
    return None


@dataclass(frozen=True)
class _Hinweismuster:
    """A pattern whose first match earns an unweighted flag, with an excerpt as evidence."""

    schluessel: str
    hinweis: str
    muster: re.Pattern[str]
    nachlauf: int  # characters kept after the match
    laenge: int  # length of the excerpt

    def finde(self, text: str) -> Markierung | None:
        """The flag for the first match, None when the pattern does not occur."""
        m = self.muster.search(text)
        if m is None:
            return None
        return Markierung(self.schluessel, self.hinweis,
                          _ausschnitt(text, m.start(), m.end() + self.nachlauf, self.laenge))


_HINWEISMUSTER = (
    _Hinweismuster("beruf", "Beruf, Arbeitgeber oder Institution genannt", _BERUF, 60, 100),
    _Hinweismuster("familie", "Familienkonstellation genannt", _FAMILIE, 40, 80),
)


def _hinweise(text: str) -> Teilergebnis:
    """Rule flags the model cannot weigh: a dated reportable event, an occupation or employer,
    a family constellation."""
    gefunden = [_datiertes_ereignis(text), *(h.finde(text) for h in _HINWEISMUSTER)]
    return Teilergebnis(markierungen=tuple(mk for mk in gefunden if mk is not None))


def schaetze(text: str, geschlecht: str = "", referenz: int = REFERENZ_DEUTSCHLAND,
             eigene_orte: Iterable[str] = (), fachbegriffe: Fachbegriffe | None = None,
             heute_jahr: int | None = None) -> Restrisiko:
    """Estimate for one pseudonymised text.  ``geschlecht`` is the patient's sex ("m", "w",
    "") when known; ``eigene_orte`` names places of the sender that are not the patient's
    (practice, clinic); ``fachbegriffe`` (a Fachbegriffe matcher) keeps medical terms from
    being taken for place names.  One detector per family of quasi-identifiers; their
    findings are concatenated in a fixed order."""
    bezug = max(int(referenz), 100)  # a practice's patients at the least
    jahr = heute_jahr or dt.date.today().year
    daten = _DATUM.findall(text)
    teile = (_geschlecht(text, geschlecht), _geburt(text, jahr, daten),
             _orte(text, bezug, eigene_orte, fachbegriffe), _seltene_erkrankungen(text),
             _hinweise(text))
    return Restrisiko(merkmale=[m for t in teile for m in t.merkmale],
                      markierungen=[mk for t in teile for mk in t.markierungen],
                      referenz=bezug, datumsangaben=len(daten))


def _uebernimm_staerkste(ziel: dict[str, Merkmal], merkmale: Iterable[Merkmal]) -> None:
    """Merges features into ``ziel`` by key, keeping the highest weight and counting every one
    again; which place or diagnosis counts is decided afterwards over the whole set."""
    for m in merkmale:
        alt = ziel.get(m.schluessel)
        if alt is None or m.bits > alt.bits:
            ziel[m.schluessel] = Merkmal(**{**asdict(m), "gezaehlt": True})


def _zaehle_nur_staerkste(merkmale: list[Merkmal]) -> None:
    """Leaves only the strongest place and the strongest diagnosis counted, because correlated
    attributes must not be multiplied."""
    for kategorie in ("Ort", "Diagnose"):
        gruppe = sorted((m for m in merkmale if m.kategorie == kategorie), key=lambda m: -m.bits)
        for m in gruppe[1:]:
            m.gezaehlt = False


def vereinige(risiken: Iterable[Restrisiko], fruehere: Iterable[Merkmal] = (),
              fruehere_anzahl: int = 0) -> Restrisiko:
    """All documents of one patient together: every key once with its highest weight,
    then again only the strongest place and the strongest diagnosis counted."""
    merkmale: dict[str, Merkmal] = {}
    markierungen: dict[str, Markierung] = {}
    referenz = REFERENZ_DEUTSCHLAND
    daten = 0
    for r in list(risiken):
        referenz = min(referenz, r.referenz)
        daten += r.datumsangaben
        _uebernimm_staerkste(merkmale, r.merkmale)
        for mk in r.markierungen:
            markierungen.setdefault(mk.schluessel, mk)
    _uebernimm_staerkste(merkmale, fruehere)
    _zaehle_nur_staerkste(list(merkmale.values()))
    return Restrisiko(merkmale=list(merkmale.values()), markierungen=list(markierungen.values()),
                      referenz=referenz, fruehere=fruehere_anzahl, datumsangaben=daten)
