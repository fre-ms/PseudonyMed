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
import gzip
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
    name: str
    einwohner: int
    plz: str
    land: str
    mehrdeutig: int = 1  # how many municipalities carry the name


@dataclass
class Erkrankung:
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
    schluessel: str  # stable key for the union across documents
    kategorie: str  # Geschlecht, Geburtsdatum, Ort, Diagnose, Datum
    beschreibung: str
    bits: float
    beleg: str = ""  # the text that triggered it (from the pseudonymised output)
    quelle: str = ""
    gezaehlt: bool = True  # False: listed, but not in the sum (weaker place/diagnosis)


@dataclass
class Markierung:
    schluessel: str
    text: str
    beleg: str = ""


@dataclass
class Restrisiko:
    merkmale: list[Merkmal] = field(default_factory=list)
    markierungen: list[Markierung] = field(default_factory=list)
    referenz: int = REFERENZ_DEUTSCHLAND
    fruehere: int = 0  # earlier documents of the same patient merged in
    datumsangaben: int = 0

    @property
    def bits(self) -> float:
        return sum(m.bits for m in self.merkmale if m.gezaehlt)

    @property
    def referenz_bits(self) -> float:
        return math.log2(max(self.referenz, 2))

    @property
    def lam(self) -> float:
        return 2.0 ** (self.referenz_bits - self.bits)

    @property
    def kappa(self) -> float:
        lam = self.lam
        if lam <= 0:
            return 1.0
        if lam > 50:
            return 1.0 / lam
        return (1.0 - math.exp(-lam)) / lam

    @property
    def ampel(self) -> str:
        lam = self.lam
        if lam < LAMBDA_ROT:
            return "rot"
        if lam < LAMBDA_GRUEN:
            return "gelb"
        return "gruen"

    @property
    def dominant(self) -> Merkmal | None:
        gezaehlt = [m for m in self.merkmale if m.gezaehlt]
        return max(gezaehlt, key=lambda m: m.bits) if gezaehlt else None

    def als_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update({"bits": round(self.bits, 2), "referenz_bits": round(self.referenz_bits, 2),
                  "lambda": round(self.lam, 3), "kappa": round(self.kappa, 4), "ampel": self.ampel,
                  "modell": "k-map-Schätzung (Synthese, kein Standard)"})
        return d

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Restrisiko:
        return cls(merkmale=[Merkmal(**m) for m in d.get("merkmale", [])],
                   markierungen=[Markierung(**m) for m in d.get("markierungen", [])],
                   referenz=int(d.get("referenz", REFERENZ_DEUTSCHLAND)),
                   fruehere=int(d.get("fruehere", 0)),
                   datumsangaben=int(d.get("datumsangaben", 0)))

    def kurz(self) -> str:
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
    return {"gruen": f"λ ≥ {LAMBDA_GRUEN:.0f} (κ ≤ 0,09, EMA-Niveau)",
            "gelb": f"{LAMBDA_ROT:.0f} ≤ λ < {LAMBDA_GRUEN:.0f} (k = 5 der Praxisleitfäden)",
            "rot": f"λ < {LAMBDA_ROT:.0f} (κ > 0,2)"}[ampel]


# -- estimate --------------------------------------------------------------------------------------
def schaetze(text: str, geschlecht: str = "", referenz: int = REFERENZ_DEUTSCHLAND,
             eigene_orte: Iterable[str] = (), fachbegriffe=None, heute_jahr: int | None = None,
             ) -> Restrisiko:
    """Estimate for one pseudonymised text.  ``geschlecht`` is the patient's sex ("m", "w",
    "") when known; ``eigene_orte`` names places of the sender that are not the patient's
    (practice, clinic); ``fachbegriffe`` (a Fachbegriffe matcher) keeps medical terms from
    being taken for place names."""
    r = Restrisiko(referenz=max(int(referenz), 100))  # a practice's patients at the least
    import datetime as dt

    jahr = heute_jahr or dt.date.today().year
    # sex
    if geschlecht in ("m", "w") or re.search(r"\bPatientin\b|\bPatient\b", text):
        r.merkmale.append(Merkmal("geschlecht", "Geschlecht", "Geschlecht (Anrede, Pseudonym)",
                                  1.0))
    # birth year / age
    geb = _GEB_JAHR.findall(text)
    alter = [next(g for g in m.groups() if g) for m in _ALTER.finditer(text)]
    daten = _DATUM.findall(text)
    r.datumsangaben = len(daten)
    if geb or alter:
        beleg = f"[geb. {geb[0]}]" if geb else f"Alter {alter[0]}"
        r.merkmale.append(Merkmal("geburt:jahr", "Geburtsdatum", "Geburtsjahr bzw. Alter",
                                  BITS_JAHR, beleg, "Annahme: 1 von 85 Jahrgängen"))
        if geb and alter and daten:
            r.merkmale.append(Merkmal("geburt:fenster", "Geburtsdatum",
                                      "Geburtsjahr und Alter neben exakten Daten engen den"
                                      " Geburtstag ein", BITS_ALTER_UND_DATUM,
                                      f"[geb. {geb[0]}] + {alter[0]} + {daten[0]}"))
            r.markierungen.append(Markierung("alter_und_datum",
                                             "Geburtsjahr und Alter zusammen mit exakten Daten"
                                             " im Text: eines davon genügt",
                                             f"[geb. {geb[0]}], Alter {alter[0]}, {daten[0]}"))
        alter_zahlen = [int(a) for a in alter if a.isdigit()] + \
            [jahr - int(g) for g in geb if g.isdigit()]
        if any(a >= 90 for a in alter_zahlen):
            r.markierungen.append(Markierung("alter_90", "Alter ab 90: kleine Kohorte, wie bei"
                                             " HIPAA Safe Harbor zusammenfassen (90+)"))
    # places
    index, quelle_orte = gemeindenindex()
    eigene = {_norm(o.strip()) for o in eigene_orte if o.strip()}
    orte: list[tuple[str, Gemeinde]] = []
    for beleg, gemeinde in index.finde(text):
        if not beleg[:1].isupper() or _norm(gemeinde.name) in eigene:
            continue
        if fachbegriffe is not None and fachbegriffe.ist_fachbegriff(gemeinde.name):
            continue
        orte.append((beleg, gemeinde))
    orte.sort(key=lambda o: o[1].einwohner)
    for i, (beleg, g) in enumerate(orte):
        bits = max(0.0, math.log2(r.referenz / max(g.einwohner, 1)))
        zusatz = f", {g.mehrdeutig} gleichnamige, größte gezählt" if g.mehrdeutig > 1 else ""
        r.merkmale.append(Merkmal(f"ort:{_norm(g.name)}", "Ort",
                                  f"Ort {g.name} ({_tausender(g.einwohner)} Einwohner{zusatz})",
                                  bits, beleg, quelle_orte, gezaehlt=i == 0))
        if g.einwohner < KLEINE_GEMEINDE:
            r.markierungen.append(Markierung(
                f"kleiner_ort:{_norm(g.name)}",
                f"Ortsname einer Gemeinde unter {_tausender(KLEINE_GEMEINDE)} Einwohnern im"
                f" Klartext: {g.name}", beleg))
    # rare diseases
    index_k, quelle_k = erkrankungsindex()
    krank = sorted(index_k.finde(text), key=lambda k: k[1].p)
    gesehen: set[str] = set()
    for i, (beleg, e) in enumerate(krank):
        if e.orpha in gesehen:
            continue
        gesehen.add(e.orpha)
        bits = -math.log2(max(e.p, 1e-9))
        klasse = e.klasse or f"{e.p:g}"
        r.merkmale.append(Merkmal(f"diagnose:{e.orpha}", "Diagnose",
                                  f"Seltene Erkrankung {e.name} (ORPHA:{e.orpha}, Prävalenz"
                                  f" {klasse}, {e.quelle})", bits, beleg, quelle_k,
                                  gezaehlt=i == 0))
        if e.p <= SELTEN:
            r.markierungen.append(Markierung(f"selten:{e.orpha}",
                                             f"Benannte seltene Erkrankung: {e.name}", beleg))
    # rule flags without a weight
    for m in _EREIGNIS.finditer(text):
        umfeld = text[max(0, m.start() - 80):m.end() + 80]
        if _DATUM.search(umfeld):
            r.markierungen.append(Markierung("ereignis_datiert",
                                             "Datiertes, öffentlich berichtbares Ereignis",
                                             " ".join(umfeld.split())[:120]))
            break
    m = _BERUF.search(text)
    if m:
        r.markierungen.append(Markierung("beruf", "Beruf, Arbeitgeber oder Institution genannt",
                                         " ".join(text[m.start():m.end() + 60].split())[:100]))
    m = _FAMILIE.search(text)
    if m:
        r.markierungen.append(Markierung("familie", "Familienkonstellation genannt",
                                         " ".join(text[m.start():m.end() + 40].split())[:80]))
    return r


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
        for m in r.merkmale:
            alt = merkmale.get(m.schluessel)
            if alt is None or m.bits > alt.bits:
                merkmale[m.schluessel] = Merkmal(**{**asdict(m), "gezaehlt": True})
        for mk in r.markierungen:
            markierungen.setdefault(mk.schluessel, mk)
    for m in fruehere:
        alt = merkmale.get(m.schluessel)
        if alt is None or m.bits > alt.bits:
            merkmale[m.schluessel] = Merkmal(**{**asdict(m), "gezaehlt": True})
    for kategorie in ("Ort", "Diagnose"):
        gruppe = sorted((m for m in merkmale.values() if m.kategorie == kategorie),
                        key=lambda m: -m.bits)
        for m in gruppe[1:]:
            m.gezaehlt = False
    return Restrisiko(merkmale=list(merkmale.values()), markierungen=list(markierungen.values()),
                      referenz=referenz, fruehere=fruehere_anzahl, datumsangaben=daten)
