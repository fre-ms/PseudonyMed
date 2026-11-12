"""Identifiers beyond the patient's own data: professions, hospitals, studies, titles.

The GeMTeX annotation guideline for identifying information (2nd version, 7 September
2025, doi:10.5281/zenodo.15747389) marks more than the patient's names and numbers: the
patient's PROFESSION, hospitals and practices (LOCATION_HOSPITAL), studies the patient
takes part in (ID) and academic titles (NAME_TITLE).  The ``merkmal`` rows of the lexicon
(:mod:`pseudonymed.erkennung.lexikon`) list such identifiers.  The identifiers step shows
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

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import lexikon, listenimport
from pseudonymed.kern.modell import Art, Fundstelle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

KLASSE_KLINIK = "Klinik"
KLASSE_BERUF = "Beruf"
KLASSE_STUDIE = "Studie"
KLASSE_TITEL = "Titel"
KLASSE_NUTZER = "Sonstiges"
KLASSEN = (KLASSE_KLINIK, KLASSE_BERUF, KLASSE_STUDIE, KLASSE_TITEL, KLASSE_NUTZER)
PRAEFIX = {KLASSE_NUTZER: "Identifikator"}
MERKMAL_QUELLEN = ("Berufe", "Titel", "Kliniken", "Studien")
MIN_LAENGE = 3
MAX_WOERTER = 16

# What the step shows and the analysis redacts: identifier rows, without the employment
# status (no GeMTeX type) and without the registry numbers of the studies (the pattern
# below finds every registry number, listed or not; the acronym rows carry the number).
_SICHTBAR = ("rolle = 'merkmal' AND klasse != 'Erwerbsstatus'"
             " AND NOT (klasse = 'Studie' AND quelle = 'Studien' AND zusatz != '')")

_TOKEN = re.compile(r"[^\W\d_]\w*|\d+\w*")  # parts of hyphenated words are tokens of their own
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_SUFFIXE = ("ern", "en", "es", "er", "em", "e", "n", "s")
_FUGE = re.compile(r"[\s.,'’&/()+:–—-]{0,6}")

# -- hospital names -----------------------------------------------------------------------------
_RECHTSFORM = re.compile(
    r"[\s,]+(?:gemeinnützige\s+)?(?:g?GmbH(?:\s*&\s*Co\.?\s*KG)?|g?AG|KG|mbH|e\.\s?V\.|eV|SE"
    r"|KdöR|AöR|gGmbH)\.?\s*$", re.IGNORECASE)
_TRENNER = re.compile(r"\s+[–—-]\s+")
# "Klinikum Augsburg": a generic hospital word before a town that has a hospital in the list.
_HAUSWOERTER = (
    "Klinikum", "Klinik", "Kliniken", "Krankenhaus", "Kreisklinik", "Kreisklinikum",
    "Kreiskrankenhaus", "Stadtklinik", "Städtisches Klinikum", "Uniklinik", "Uniklinikum",
    "Universitätsklinik", "Universitätsklinikum", "Bezirksklinikum", "Bezirkskrankenhaus",
    "Spital", "Kantonsspital", "Landesklinikum", "Landeskrankenhaus", "Fachklinik",
)
_KLAMMER = re.compile(r"\s*\([^)]*\)")
# A one-word name must say that it is a hospital ("Eifelklinik", not "Saarland" or "Caritas").
_HAUSWORT = re.compile(r"(?i)klinik|clinic|spital|krankenhaus|sanatorium|hospital|zentrum|centrum")
_GENERISCH = {
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
_BERUF_VOR = re.compile(
    r"(?i)(?:\b(?:gelernte?[nrms]?|ehemalige?[nrms]?|frühere?[nrms]?|pensionierte?[nrms]?"
    r"|berentete?[nrms]?|verrentete?[nrms]?|selbstst?ändige?[nrms]?|freiberufliche?[nrms]?"
    r"|ausgebildete?[nrms]?|angehende?[nrms]?|arbeitslose?[nrms]?)"
    r"|\b(?:arbeite[nt]?e?[nt]?|gearbeitet|tätig|beschäftigt|angestellt|jobbt|Tätigkeit|Arbeit"
    r"|Job|Stelle|Anstellung|zuletzt|Ausbildung|Umschulung|Lehre|Weiterbildung|Beruf|beruflich)"
    r"\b(?:[ \t]+\S+){0,4}?[ \t]+(?:als|zum|zur)"
    r"|\b(?:von[ \t]+Beruf|Beruf(?:sbezeichnung)?[ \t]*:|beruflich)"
    r"|\b(?:ist|war|sei|wäre|wurde|wird|bin)(?:[ \t]+(?:von[ \t]+Beruf|seit[ \t]+\S+|jetzt|früher"
    r"|dort|hier|eine?r?|gelernte?r?|ehemalige?r?))?)\s+$")
_BERUF_NACH = re.compile(
    r"(?i)^[ \t]*,?[ \t]*(?:\S+[ \t]+){0,3}?(?:von[ \t]+Beruf|tätig|beschäftigt|angestellt"
    r"|gearbeitet|im[ \t]+Ruhestand|in[ \t]+Rente|berentet|a\.[ \t]?D\.)")
_ANREDE = re.compile(r"(?i)\b(?:Herr|Herrn|Frau|Hr|Fr|Dr|Prof|Familie|Fam)\.?\s+$")
# Words of the classification that rarely name the patient's own profession.
_KEIN_BERUF = {"angehoerige", "angehoeriger", "ordner", "ordnerin", "mitglied", "partner",
               "partnerin", "spender", "spenderin", "halter", "halterin", "betreuer",
               "betreuerin", "patient", "patientin", "pate", "patin"}
_ANAMNESE = re.compile(
    r"(?i)\b(?:Sozialanamnese|Berufsanamnese|Sozial-\s*und\s*Berufsanamnese|Soziale\s+Situation"
    r"|Sozialanamnestisch|Beruf\s*:)")
_UEBERSCHRIFT = re.compile(r"\n[A-ZÄÖÜ][\w /-]{2,40}:\s*(?:\n|$)")
_SATZENDE = re.compile(r"[.!?](?=\s+[A-ZÄÖÜ])|\n\s*\n")

# -- studies --------------------------------------------------------------------------------------
_AKRONYM = re.compile(r"(?=(?:.*[A-ZÄÖÜ]){2}|.*\d)(?=(?:.*[^\W\d_]){2})")  # "CARE", "HoPe", "POEM3"
_STUDIE = re.compile(
    r"(?i)\b(?:Studie\w*|Studien\w*|Trial|Register\w*|Kohorte\w*|randomisiert\w*|eingeschlossen"
    r"|Einschluss|Teilnahme|teilnehm\w*|Proband\w*|Prüfzentrum|Prüfärzt\w*|Prüfarzt|Screening)\b")
_STUDIEN_ID = re.compile(r"\b(?:NCT\d{8}|DRKS\d{8}|ISRCTN\d{8}|20\d{2}-5\d{5}-\d{2}-\d{2})\b")
_EUDRACT = re.compile(r"(?i)\bEudra\s?CT(?:[\s-]*(?:Nr\.?|No\.?|Nummer|number))?[\s:]*"
                      r"((?:19|20)\d{2}-\d{6}-\d{2})\b")

_PERSONENARTEN = (Art.NAME, Art.VORNAME, Art.NACHNAME, Art.GEBURTSNAME)


def _norm(token: str) -> str:
    """Case-folded token with the umlauts transliterated, the key of the index."""
    return token.casefold().translate(_TRANS)


def regelname(klasse: str) -> str:
    """Rule id of a class: ``identifikator.klinik``, ``identifikator.sonstiges`` …"""
    return "identifikator." + (re.sub(r"\W+", "_", _norm(klasse)).strip("_") or "sonstiges")


def praefix(klasse: str) -> str:
    """The word in the pseudonym of a class: ``[Klinik <code>]``, ``[Identifikator <code>]``
    for the user's own class."""
    return PRAEFIX.get(klasse, klasse.strip() or PRAEFIX[KLASSE_NUTZER])


def _spezifisch(name: str) -> bool:
    """Whether a hospital name holds a word of its own (a place, a saint, a brand)
    rather than generic words only."""
    for token in _TOKEN.findall(name):
        teil = _norm(token)
        if (len(teil) >= 3 and teil.isalpha() and teil not in _GENERISCH
                and not re.search(r"(?:isch|lich)(?:e[nrms]?)?$", teil)):
            return True
    return False


def klinik_formen(name: str) -> set[str]:
    """The official name, its main part before a dash and both without the legal form or
    parentheses; only forms with a word of their own."""
    formen = {name, _TRENNER.split(name)[0]}
    formen |= {_RECHTSFORM.sub("", f).strip(" ,") for f in formen}
    formen |= {_KLAMMER.sub("", f).strip(" ,") for f in formen}
    return {f for f in formen if len(f) >= 6 and _spezifisch(f)
            and (len(_TOKEN.findall(f)) > 1 or _HAUSWORT.search(f))}


def _titel_formen(begriff: str) -> set[str]:
    """A title as entered; nothing for a nobiliary particle ("zur", "von")."""
    if len(begriff) < 2 or (begriff.islower() and len(begriff) <= 4):
        return set()
    return {begriff}


def _studien_formen(begriff: str) -> set[str]:
    """A study acronym as registered; nothing for a plain word ("Dekan", "Radix")."""
    if len(begriff) < MIN_LAENGE or not _AKRONYM.match(begriff):
        return set()
    return {begriff}


def _wort_formen(begriff: str, exakt: bool | None) -> set[str]:
    """Any other entry as entered; one matched only in its exact spelling may be short ("Hb")."""
    return {begriff} if len(begriff) >= (2 if exakt else MIN_LAENGE) else set()


def _formen_fuer(begriff: str, klasse: str, exakt: bool | None) -> set[str]:
    """The forms under which an entry of a class goes into the index: hospitals with their
    short forms, professions without words that rarely name the patient's own."""
    if klasse == KLASSE_TITEL:
        return _titel_formen(begriff)
    if klasse == KLASSE_STUDIE:
        return _studien_formen(begriff)
    if klasse == KLASSE_KLINIK:
        return klinik_formen(begriff)
    formen = _wort_formen(begriff, exakt)
    if klasse == KLASSE_BERUF:
        return {f for f in formen if _norm(f) not in _KEIN_BERUF}
    return formen


# -- matching -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Muster:
    """One indexed form of an entry: its tokens as typed and normalised."""

    normal: tuple[str, ...]
    original: tuple[str, ...]
    klasse: str
    exakt: bool  # registered spelling only (acronyms, titles)
    schluss: str  # closing full stop or parenthesis of the entry ("Dr. med.", "Halle (Saale)")


@dataclass(frozen=True)
class Kandidat:
    """A span of the text that an entry of the class matched."""

    start: int
    ende: int
    klasse: str


class Erkenner:
    """The identifier rows with their matcher."""

    def __init__(self, registernummern: bool = True) -> None:
        self.registernummern = registernummern  # NCT, DRKS … as study hits
        self.index: dict[str, list[_Muster]] = {}
        self.je_klasse: dict[str, int] = {}
        self._gesehen: set[tuple[tuple[str, ...], str]] = set()

    @property
    def anzahl(self) -> int:
        """How many rows the matcher took on, over all classes."""
        return sum(self.je_klasse.values())

    def fuege_hinzu(self, begriff: str, klasse: str, exakt: bool | None = None) -> int:
        """Add one row (hospitals with their short forms); returns how many forms.
        ``exakt`` asks for the registered spelling (default: titles and studies only)."""
        begriff = " ".join(begriff.split())
        neu = sum(self._form(form, klasse, exakt) for form in _formen_fuer(begriff, klasse, exakt))
        if neu:
            self.je_klasse[klasse] = self.je_klasse.get(klasse, 0) + 1
        return neu

    def fuege_ort_hinzu(self, ort: str) -> int:
        """Short hospital names for a town with a hospital: "Klinikum <town>",
        "Krankenhaus <town>" …, also without a parenthesised addition ("Halle (Saale)")."""
        orte = {" ".join(ort.split()), _KLAMMER.sub("", ort).strip()}
        return sum(self._form(f"{w} {o}", KLASSE_KLINIK)
                   for o in orte if len(o) >= 3 for w in _HAUSWOERTER)

    def _form(self, form: str, klasse: str, exakt: bool | None = None) -> int:
        """Index one form (1) unless it is empty, too long or known already (0)."""
        original = tuple(_TOKEN.findall(form))
        if not original or len(original) > MAX_WOERTER:
            return 0
        normal = tuple(_norm(t) for t in original)
        if (normal, klasse) in self._gesehen:
            return 0
        self._gesehen.add((normal, klasse))
        if exakt is None:
            exakt = klasse in (KLASSE_TITEL, KLASSE_STUDIE)
        self.index.setdefault(normal[0], []).append(
            _Muster(normal, original, klasse, exakt, form[-1] if form[-1] in ".)" else ""))
        return 1

    @staticmethod
    def _passt(muster_original: str, muster_normal: str, token: str, exakt: bool) -> bool:
        """Whether a token is one word of an entry: exactly, or normalised with an
        inflection ending, keeping a capital where the entry has one."""
        if exakt:
            return token == muster_original
        if muster_original[:1].isupper() and not token[:1].isupper():
            return False
        n = _norm(token)
        if n == muster_normal:
            return True
        return len(muster_normal) >= 4 and any(
            n.endswith(s) and n[:-len(s)] == muster_normal for s in _SUFFIXE)

    def _an(self, text: str, tokens: list[re.Match[str]], i: int) -> list[tuple[int, _Muster]]:
        """Every entry that matches from token ``i`` on: (end offset, entry)."""
        n = _norm(tokens[i].group())
        schluessel = {n} | {n[:-len(s)] for s in _SUFFIXE if n.endswith(s) and len(n) > len(s) + 3}
        treffer: list[tuple[int, _Muster]] = []
        for key in schluessel:
            for m in self.index.get(key, ()):
                ende = self._ende_von(text, tokens, i, m)
                if ende is not None:
                    treffer.append((ende, m))
        return treffer

    def _ende_von(self, text: str, tokens: list[re.Match[str]], i: int,
                  m: _Muster) -> int | None:
        """End offset of entry ``m`` when its words match the tokens from ``i`` on, each
        joined to the previous one; None otherwise."""
        if i + len(m.normal) > len(tokens):
            return None
        for k, (orig, norm) in enumerate(zip(m.original, m.normal, strict=True)):
            t = tokens[i + k]
            if k and not _verbunden(text, tokens[i + k - 1], t):
                return None
            if not self._passt(orig, norm, t.group(), m.exakt):
                return None
        ende = tokens[i + len(m.normal) - 1].end()
        if m.schluss and text[ende:ende + 1] == m.schluss:
            ende += 1
        return ende

    def kandidaten(self, text: str, titel: bool = False) -> list[Kandidat]:
        """Non-overlapping hits in reading order that pass their class's context rule;
        titles only when asked (they need the people found)."""
        tokens = list(_TOKEN.finditer(text))
        aus: list[Kandidat] = []
        beruf_ende = 0
        i = 0
        while i < len(tokens):
            gewaehlt = self._treffer_bei(text, tokens, i, titel, beruf_ende)
            if gewaehlt is None:
                i += 1
                continue
            aus.append(gewaehlt)
            if gewaehlt.klasse == KLASSE_BERUF:
                beruf_ende = gewaehlt.ende
            while i < len(tokens) and tokens[i].start() < gewaehlt.ende:
                i += 1
        if not titel and self.registernummern:
            aus += _studien_nummern(text, aus)
            aus.sort(key=lambda k: k.start)
        return aus

    def _treffer_bei(self, text: str, tokens: list[re.Match[str]], i: int, titel: bool,
                     beruf_ende: int) -> Kandidat | None:
        """The longest entry at token ``i`` of the wanted kind (titles or the rest) that
        passes its class's context rule; None when none does."""
        a = tokens[i].start()
        for ende, m in sorted(self._an(text, tokens, i), key=lambda t: -(t[0] - a)):
            if (m.klasse == KLASSE_TITEL) == titel and _im_kontext(m.klasse, text, a, ende,
                                                                   beruf_ende):
                return Kandidat(a, ende, m.klasse)
        return None


def _verbunden(text: str, vorher: re.Match[str], token: re.Match[str]) -> bool:
    """Whether two neighbouring tokens belong to one entry: at most six characters of
    punctuation or space between them and no paragraph break."""
    zwischen = text[vorher.end():token.start()]
    return _FUGE.fullmatch(zwischen) is not None and "\n\n" not in zwischen


def _im_kontext(klasse: str, text: str, a: int, b: int, beruf_ende: int) -> bool:
    """Whether a hit passes the context rule of its class: a profession needs a cue, a study
    acronym a study word nearby; every other class matches wherever it stands."""
    if klasse == KLASSE_BERUF:
        return _beruf_im_kontext(text, a, b, beruf_ende)
    if klasse == KLASSE_STUDIE:
        return _studie_im_kontext(text, a, b)
    return True


def _satzanfang(text: str, a: int, grenze: int) -> int:
    """Offset where the sentence around ``a`` begins: after the last sentence end within
    80 characters, never before ``grenze``."""
    anfang = max(0, a - 80, grenze)
    letzte = None
    for m in _SATZENDE.finditer(text, anfang, a):
        letzte = m
    return letzte.end() if letzte is not None else anfang


def _beruf_im_kontext(text: str, a: int, b: int, beruf_ende: int) -> bool:
    """Whether a profession at ``a:b`` has a cue right before or after it, or stands in a
    social-history paragraph; a salutation before it makes it a name."""
    vorher = text[_satzanfang(text, a, beruf_ende):a]
    if _ANREDE.search(vorher):
        return False  # "Herr Bauer": a name
    if _BERUF_VOR.search(vorher):
        return True
    satzende = _SATZENDE.search(text, b)
    if _BERUF_NACH.search(text[b:min(b + 60, satzende.start() if satzende else len(text))]):
        return True
    fenster = text[max(0, a - 300, beruf_ende):a]
    kopf = None
    for m in _ANAMNESE.finditer(fenster):
        kopf = m
    return kopf is not None and not _UEBERSCHRIFT.search(fenster, kopf.end())


def _studie_im_kontext(text: str, a: int, b: int) -> bool:
    """Whether a study word stands within 40 characters on the same line."""
    vorher = text[max(0, a - 40):a].rsplit("\n", 1)[-1]
    nachher = text[b:b + 40].split("\n", 1)[0]
    return bool(_STUDIE.search(vorher) or _STUDIE.search(nachher))


def _studien_nummern(text: str, belegt: list[Kandidat]) -> list[Kandidat]:
    """Registry numbers (NCT, DRKS, ISRCTN, EU CT, labelled EudraCT) outside the hits."""
    spannen = [(m.start(), m.end()) for m in _STUDIEN_ID.finditer(text)]
    spannen += [(m.start(1), m.end(1)) for m in _EUDRACT.finditer(text)]
    return [Kandidat(x, y, KLASSE_STUDIE) for x, y in spannen
            if not any(x < k.ende and y > k.start for k in belegt)]


# -- hits -----------------------------------------------------------------------------------------
def _fundstelle(seite: Seitentext, a: int, b: int, klasse: str, vergabe: Pseudonymvergabe,
                quelle: str) -> Fundstelle:
    """The hit for ``a:b`` with its pseudonym, rectangles and font size."""
    text = seite.text[a:b]
    return Fundstelle(
        seite=seite.nummer, start=a, ende=b, text=text, art=Art.IDENTIFIKATOR,
        regel=regelname(klasse), pseudonym=vergabe.identifikator(praefix(klasse), text),
        rects=[tuple(r) for r in seite.rects(a, b)], schriftgroesse=seite.schriftgroesse(a, b),
        quelle=quelle)


def _frei(a: int, b: int, belegt: list[Fundstelle]) -> bool:
    """Whether no existing hit overlaps ``a:b``."""
    return not any(f.ende > f.start and a < f.ende and b > f.start for f in belegt)


def finde_identifikatoren(seite: Seitentext, erkenner: Erkenner, vergabe: Pseudonymvergabe,
                          vorhandene: list[Fundstelle], quelle: str = "text") -> list[Fundstelle]:
    """Hospitals, professions, studies and the user's own identifiers on one page; what
    overlaps an existing hit is left to that hit."""
    treffer: list[Fundstelle] = []
    for k in erkenner.kandidaten(seite.text):
        if _frei(k.start, k.ende, vorhandene + treffer):
            treffer.append(_fundstelle(seite, k.start, k.ende, k.klasse, vergabe, quelle))
    return treffer


def finde_titel(seite: Seitentext, erkenner: Erkenner, vergabe: Pseudonymvergabe,
                vorhandene: list[Fundstelle], quelle: str = "text") -> list[Fundstelle]:
    """Titles directly before a person ("Prof. Dr. med. Person K7Q4MX") or after it
    ("…, Dipl.-Ing."), run after the NER; adjacent titles become one hit."""
    personen = [f for f in vorhandene if f.ende > f.start
                and (f.art in _PERSONENARTEN or f.regel == "ner.person")]
    if not personen:
        return []
    kandidaten = erkenner.kandidaten(seite.text, titel=True)
    treffer = [
        _fundstelle(seite, kette[0].start, kette[-1].ende, KLASSE_TITEL, vergabe, quelle)
        for p in personen
        for kette in (_titel_vor(seite.text, kandidaten, p.start),
                      _titel_nach(seite.text, kandidaten, p.ende))
        if kette]
    return _ohne_ueberlappung(treffer, vorhandene)


def _angrenzend(zwischen: str) -> bool:
    """Whether a title and its neighbour are separated by spaces, one comma and at most
    one line break only."""
    return re.fullmatch(r"[ \t]*,?[ \t]*\n?[ \t]*", zwischen) is not None


def _titel_vor(text: str, kandidaten: list[Kandidat], grenze: int) -> list[Kandidat]:
    """The unbroken chain of titles that ends right before ``grenze``, a person's start."""
    kette: list[Kandidat] = []
    for k in reversed([k for k in kandidaten if k.ende <= grenze]):
        if not _angrenzend(text[k.ende:grenze]):
            break
        kette.insert(0, k)
        grenze = k.start
    return kette


def _titel_nach(text: str, kandidaten: list[Kandidat], grenze: int) -> list[Kandidat]:
    """The unbroken chain of titles that starts right after ``grenze``, a person's end."""
    kette: list[Kandidat] = []
    for k in [k for k in kandidaten if k.start >= grenze]:
        if not _angrenzend(text[grenze:k.start]):
            break
        kette.append(k)
        grenze = k.ende
    return kette


def _ohne_ueberlappung(treffer: list[Fundstelle], vorhandene: list[Fundstelle]) -> list[Fundstelle]:
    """The hits in reading order, the longer first at the same start, each one only where
    neither an existing hit nor an accepted one lies."""
    eindeutig: list[Fundstelle] = []
    for f in sorted(treffer, key=lambda f: (f.start, -f.ende)):
        if _frei(f.start, f.ende, vorhandene + eindeutig):
            eindeutig.append(f)
    return eindeutig


# -- the table behind the identifiers step ------------------------------------------------------
@dataclass(frozen=True)
class Eintrag:
    """One visible row of the lexicon: the identifier, its class, where it came from, the
    source's id and the JSON addition of the reference data."""

    begriff: str
    klasse: str
    quelle: str
    kennung: str
    zusatz: str

    @property
    def schluessel(self) -> tuple[str, str, str]:
        """What identifies the row: (term, class, id)."""
        return self.begriff, self.klasse, self.kennung

    @property
    def eigen(self) -> bool:
        """Whether the user entered or changed the row (bundled rows leave a trace when
        deleted, the user's own do not)."""
        return self.quelle == lexikon.NUTZER

    @property
    def angaben(self) -> str:
        """Short description from the reference data (place, registry number, meaning)."""
        beschreibung = _ANGABEN.get(self.klasse)
        return beschreibung(self) if beschreibung else ""


def _klinik_angaben(e: Eintrag) -> str:
    """Town and country from a hospital's JSON addition; empty without one."""
    if not e.zusatz:
        return ""
    try:
        z = json.loads(e.zusatz)
    except ValueError:
        return ""
    return f"{z.get('ort', '')} ({z.get('land', '')})".replace(" ()", "").strip()


def _studien_angaben(e: Eintrag) -> str:
    """The registry number behind the registry's name ("ClinicalTrials.gov:NCT…")."""
    return e.kennung.split(":", 1)[1] if ":" in e.kennung else ""


def _titel_angaben(e: Eintrag) -> str:
    """The fields of a title from its ``|``-separated id, "alle" (every field) left out."""
    if "|" not in e.kennung:
        return ""
    return " · ".join(t for t in e.kennung.split("|") if t and t != "alle")


def _beruf_angaben(e: Eintrag) -> str:
    """The KldB 2010 code of a profession when the id is one."""
    return f"KldB {e.kennung}" if e.kennung.isdigit() else ""


_ANGABEN: dict[str, Callable[[Eintrag], str]] = {
    KLASSE_KLINIK: _klinik_angaben, KLASSE_STUDIE: _studien_angaben,
    KLASSE_TITEL: _titel_angaben, KLASSE_BERUF: _beruf_angaben,
}


class Identifikatoren:
    """Reads and writes the identifier rows of the lexicon in the pseudonym database.
    Deleting or changing a bundled row records the deletion, so that the next update of
    the bundled data does not bring it back; a changed bundled row becomes the user's."""

    def __init__(self, pfad: str | Path | None = None) -> None:
        from pseudonymed.erkennung.vokabular import Vokabular

        self._vokabular = Vokabular(pfad)
        self.pfad = self._vokabular.pfad
        self.verbindung = self._vokabular.verbindung
        self.verbindung.executescript(lexikon.SCHEMA)

    def close(self) -> None:
        """Close the database connection."""
        self._vokabular.close()

    def __enter__(self) -> Identifikatoren:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading -------------------------------------------------------------------------------
    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        return lexikon.version(self.verbindung)

    def anzahl(self) -> int:
        """How many rows the step shows."""
        return int(self.verbindung.execute(
            f"SELECT COUNT(*) FROM lexikon WHERE {_SICHTBAR}").fetchone()[0])

    def klassen(self) -> list[tuple[str, int]]:
        """Every class of the visible rows with its number of rows, the largest first."""
        return [(k, n) for k, n in self.verbindung.execute(
            f"SELECT klasse, COUNT(*) FROM lexikon WHERE {_SICHTBAR} GROUP BY klasse"
            " ORDER BY COUNT(*) DESC, klasse")]

    def alle(self, klasse: str = "") -> list[Eintrag]:
        """The visible rows in alphabetical order, of one class when given."""
        sql = f"SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon WHERE {_SICHTBAR}"
        werte: list[str] = []
        if klasse:
            sql += " AND klasse = ?"
            werte.append(klasse)
        return [Eintrag(*r) for r in self.verbindung.execute(
            sql + " ORDER BY begriff COLLATE NOCASE", werte)]

    def geloescht(self) -> int:
        """How many bundled rows the user deleted or changed (kept from coming back)."""
        return int(self.verbindung.execute(
            "SELECT COUNT(*) FROM lexikon_geloescht").fetchone()[0])

    def schluessel_fuer(self, begriff: str, klasse: str = "") -> list[tuple[str, str, str]]:
        """Keys of the visible rows with this term (case-insensitive), in one class or all."""
        sql = f"SELECT begriff, klasse, kennung FROM lexikon WHERE {_SICHTBAR} AND begriff = ?"
        werte = [" ".join(begriff.split())]
        if klasse:
            sql += " AND klasse = ?"
            werte.append(klasse)
        return [tuple(r) for r in self.verbindung.execute(sql, werte)]

    def _zeile(self, schluessel: tuple[str, str, str]) -> Eintrag | None:
        """The row with this key, visible or not; None when there is none."""
        r = self.verbindung.execute(
            "SELECT begriff, klasse, quelle, kennung, zusatz FROM lexikon"
            " WHERE begriff = ? AND klasse = ? AND kennung = ?", schluessel).fetchone()
        return Eintrag(*r) if r else None

    # -- writing -------------------------------------------------------------------------------
    def _fertig(self, n: int) -> int:
        """Commit; raise the version when ``n`` rows changed; return ``n``."""
        if n:
            lexikon.erhoehe_version(self.verbindung)
        self.verbindung.commit()
        return n

    def fuege_hinzu(self, begriffe: Iterable[str], klasse: str = KLASSE_NUTZER) -> int:
        """Insert the user's own identifiers; returns how many were new."""
        klasse = klasse.strip() or KLASSE_NUTZER
        vorher = self.verbindung.total_changes
        self.verbindung.executemany(
            "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, ?, ?, '', '')",
            [(b, klasse, lexikon.MERKMAL, lexikon.NUTZER)
             for b in dict.fromkeys(" ".join(str(b).split()) for b in begriffe)
             if len(b) >= MIN_LAENGE and not b.startswith("#")])
        return self._fertig(self.verbindung.total_changes - vorher)

    def _loesche(self, e: Eintrag) -> int:
        """Delete one row, recording the deletion of a bundled one; returns 1 or 0."""
        if not e.eigen:
            self.verbindung.execute(
                "INSERT OR IGNORE INTO lexikon_geloescht (begriff, klasse, kennung)"
                " VALUES (?, ?, ?)", e.schluessel)
        vorher = self.verbindung.total_changes
        self.verbindung.execute(
            "DELETE FROM lexikon WHERE begriff = ? AND klasse = ? AND kennung = ?", e.schluessel)
        return self.verbindung.total_changes - vorher

    def entferne(self, schluessel: Iterable[tuple[str, str, str]]) -> int:
        """Delete the rows with these keys; returns how many went."""
        n = 0
        for s in schluessel:
            e = self._zeile(s)
            if e is not None:
                n += self._loesche(e)
        return self._fertig(n)

    def aendere(self, schluessel: tuple[str, str, str], begriff: str | None = None,
                klasse: str | None = None) -> Eintrag | None:
        """Change term or class of one row and return it as it is now; None when the
        result is too short or exists already."""
        e = self._zeile(schluessel)
        if e is None:
            return None
        neu_begriff = " ".join((begriff if begriff is not None else e.begriff).split())
        neu_klasse = (klasse if klasse is not None else e.klasse).strip() or KLASSE_NUTZER
        if len(neu_begriff) < MIN_LAENGE or (neu_begriff, neu_klasse) == (e.begriff, e.klasse):
            return None
        selbst = (neu_begriff.casefold(), neu_klasse) == (e.begriff.casefold(), e.klasse)
        if not selbst and self._zeile((neu_begriff, neu_klasse, e.kennung)) is not None:
            return None
        self._loesche(e)
        self.verbindung.execute(
            "INSERT OR REPLACE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (neu_begriff, neu_klasse, lexikon.MERKMAL, lexikon.NUTZER, e.kennung, e.zusatz))
        self._fertig(1)
        return Eintrag(neu_begriff, neu_klasse, lexikon.NUTZER, e.kennung, e.zusatz)

    def wiederherstellen(self) -> int:
        """Forget the deletions and reload the bundled identifier sources; returns how many
        deletions were forgotten (rows fetched on request, such as the Austrian hospitals,
        have to be fetched again)."""
        n = self.geloescht()
        self.verbindung.execute("DELETE FROM lexikon_geloescht")
        lexikon.sicherstellen(self.verbindung, erzwingen=True, quellen=MERKMAL_QUELLEN)
        self._fertig(1)
        return n

    # -- CSV -------------------------------------------------------------------------------------
    def exportiere_csv(self, pfad: Path, klasse: str = "") -> int:
        """Write ``begriff,klasse,quelle,angaben`` rows (comma, UTF-8, header line)."""
        zeilen = self.alle(klasse)
        with Path(pfad).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["begriff", "klasse", "quelle", "angaben"])
            w.writerows((e.begriff, e.klasse, e.quelle, e.angaben) for e in zeilen)
        return len(zeilen)

    def importiere_csv(self, pfad: Path, klasse: str = KLASSE_NUTZER) -> tuple[int, int]:
        """Read a CSV (``begriff[,klasse]``, header optional, comma, semicolon or tab) or a
        plain list (one term per line); returns (new, read).  A class column in the file
        wins over ``klasse``."""
        gelesen = listenimport.lies_liste(pfad, "klasse", klasse)
        return sum(self.fuege_hinzu(b, k) for k, b in gelesen.gruppen.items()), gelesen.zeilen


# -- the matcher used by the analysis ------------------------------------------------------------
class _Standarderkenner:
    """The matcher over the identifier rows, kept until the table's version changes;
    ``cache_clear`` makes the next call rebuild it.  A callable object rather than a
    function with a bolted-on attribute, so that ``cache_clear`` is typed."""

    def __init__(self) -> None:
        """Starts without a cached matcher."""
        self._cache: tuple[Path, int, Erkenner] | None = None

    def __call__(self) -> Erkenner:
        """The matcher over the identifier rows, rebuilt only when the lexicon changed;
        without a usable database it knows the registry-number patterns only."""
        try:
            with Identifikatoren() as ident:
                version = ident.version()
                if self._cache is not None and self._cache[0] == ident.pfad \
                        and self._cache[1] == version:
                    return self._cache[2]
                erkenner = _erkenner_aus(ident.verbindung)
                pfad = ident.pfad
        except (sqlite3.Error, OSError):
            return Erkenner()
        self._cache = (pfad, version, erkenner)
        return erkenner

    def cache_clear(self) -> None:
        """Forget the cached matcher (tests and the step after edits)."""
        self._cache = None


standard = _Standarderkenner()


def _erkenner_aus(verbindung: sqlite3.Connection) -> Erkenner:
    """The matcher over the visible rows, with short hospital names for every town that
    has a listed hospital."""
    erkenner = Erkenner()
    orte: set[str] = set()
    for begriff, klasse, zusatz in verbindung.execute(
            f"SELECT begriff, klasse, zusatz FROM lexikon WHERE {_SICHTBAR}"):
        erkenner.fuege_hinzu(begriff, klasse)
        if klasse == KLASSE_KLINIK:
            orte.add(_ort_aus(zusatz))
    for ort in sorted(orte - {""}):
        erkenner.fuege_ort_hinzu(ort)
    return erkenner


def _ort_aus(zusatz: str) -> str:
    """The town in a hospital row's JSON addition; empty without one or when unreadable."""
    if not zusatz.startswith("{"):
        return ""
    try:
        return str(json.loads(zusatz).get("ort") or "")
    except ValueError:
        return ""

