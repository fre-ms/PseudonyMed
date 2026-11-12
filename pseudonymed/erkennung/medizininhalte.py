"""Medications and laboratory values, redacted only on request, in four independent levels.

Both belong to the medical content and stay by default.  Knowing *which* drug someone takes
is one thing, knowing the dose and the schedule another; knowing *what* was examined is one
thing, the measured value another.  The analysis therefore offers four options, each on its
own (:class:`Auswahl`), and with them the terms of the lexicon become hits that the visual
review may discard:

* ``medikamente``: the drug name, "[Medikament <code>]";
* ``dosierungen``: the dose and the schedule next to a drug name ("500 mg 1-0-1", "2 Tbl.",
  "bei Bedarf", "p. o."), "[Dosierung <code>]"; the drug name itself stays unless asked;
* ``laborwerte``: the lab name, the examination, wherever it stands, "[Laborwert <code>]";
* ``messwerte``: the measurement right after a lab name ("2,3 mU/l" in "TSH 2,3 mU/l",
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

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import lexikon
from pseudonymed.erkennung.identifikatoren import Erkenner, Kandidat
from pseudonymed.kern.modell import Art, Fundstelle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

KLASSE_MEDIKAMENT = "Medikament"
KLASSE_LABORWERT = "Laborwert"
PRAEFIX_DOSIERUNG = "Dosierung"
PRAEFIX_MESSWERT = "Messwert"
REGEL = {KLASSE_MEDIKAMENT: "medizin.medikament", PRAEFIX_DOSIERUNG: "medizin.dosierung",
         KLASSE_LABORWERT: "medizin.laborwert", PRAEFIX_MESSWERT: "medizin.messwert"}
VOKABULAR_KATEGORIEN = {
    KLASSE_MEDIKAMENT: ("medikament", "medikamente"),
    KLASSE_LABORWERT: ("laborwert", "laborwerte"),
}
QUELLE_LABORWERTE = "Laborwerte"  # the curated list; the Wikidata lab rows are left out
# A drug name that also names one of these is no safe medication hit.
_ANDERE_KLASSEN = ("Erkrankung", "Symptom", "Befund", "Anatomie", "Physiologie", "Psyche",
                   "Organismus", "Eponym")
# Everyday substances that the drug classes of Wikidata list too.
_KEIN_MEDIKAMENT = {"sauerstoff", "salz", "kochsalz", "wasser", "zucker", "alkohol", "ethanol",
                    "koffein", "nikotin", "luft", "licht", "kaffee", "tee"}
_ABKUERZUNG = re.compile(r"^(?=.{2,6}$)(?=(?:.*[A-ZÄÖÜ]){2}|.*\d)")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

# -- measurements -----------------------------------------------------------------------------
_EINHEIT = (r"(?:%|‰|mmHg|mm/h|fl|fL|pg|INR|Ratio|(?:Tsd\.?|G|T|Mio\.?)/[µμunp]?[lL]"
            r"|/[µμn]?[lL]|[mµμunpkf]?(?:g|mol|U|IU|E|Eq|kat)(?:/(?:[mµμnd]?[lL]|h|min|kg|g"
            r"|24[ \t]?h|mmol))?)(?![\w/])")
_MESSWERT = re.compile(
    r"[ \t]*(?:\([^()\n]{1,20}\)[ \t]*)?(?:[:=][ \t]*)?(?:(?:von|bei|mit|auf|ca\.)[ \t]+)?"
    r"(?P<wert>(?:(?:[<>≤≥]=?)[ \t]*)?\d+(?:[.,]\d+)?(?![\d.,/:]*\d)(?:[ \t]*" + _EINHEIT
    + r")?)")

# -- doses and schedules ------------------------------------------------------------------------
_MENGE = r"(?:\d+(?:[.,]\d+)?|½|¼|¾|\d/\d)"
_EINHEIT_DOSIS = (r"(?:mg|g|µg|μg|mcg|ug|ng|ml|l|IE|I\.\s?E\.|IU|U|E|Tbl\.?|Tabl\.?|Tabletten?"
                  r"|Tab\.?|Kps\.?|Kapseln?|Tr\.?|Tropfen|Hub|Hübe|Amp\.?|Ampullen?|Supp\.?"
                  r"|Zäpfchen|Sprühst(?:oß|öße)|Beutel|Btl\.?|Pflaster|St\.?|Stück"
                  r"|Fertigspritzen?|Pen|mmol|mEq|%)")
_PRO = (r"(?:\s?/\s?(?:kg(?:\s?KG)?|d|Tag|Tg\.?|h|Std\.?|Woche|Wo\.?|Monat|Mon\.?)"
        r"|\s(?:pro|je)\s(?:Tag|Woche|Monat|kg(?:\sKG)?))?")
_ZEITEN = (r"(?:tgl\.?|täglich|/d|/Tag|pro\sTag|wöchentlich|/Woche|pro\sWoche|monatlich"
           r"|pro\sMonat)")
_WORTE = (r"(?:morgens|mittags|abends|nachts|zur\sNacht|z\.\s?N\.|bei\sBedarf|b\.\s?B\."
          r"|nüchtern|vor\sdem\s(?:Essen|Schlafen(?:gehen)?)|nach\sdem\sEssen|zum\sEssen"
          r"|zu\sden\sMahlzeiten|täglich|tgl\.|wöchentlich|monatlich|jeden\s(?:Morgen|Abend"
          r"|zweiten\sTag)|(?:ein|zwei|drei|vier)mal\s(?:täglich|wöchentlich|pro\sTag|pro\sWoche)"
          r"|alle\s\d+\s?(?:h|Std\.?|Stunden|Tage|Wochen)|p\.\s?o\.|i\.\s?v\.|s\.\s?c\."
          r"|i\.\s?m\.|oral|sublingual|subkutan|intravenös|per\sos|retard|ret\.|Dauermedikation)")
_DOSIS_TOKEN = re.compile(
    r"(?:"
    rf"{_MENGE}(?:\s?[-–]\s?{_MENGE})?\s?{_EINHEIT_DOSIS}{_PRO}"  # 500 mg, 1-2 Tbl., 20 mg/kg
    r"|(?:\d+|½|¼|¾|\d/\d)(?:\s?[-–]\s?(?:\d+|½|¼|¾|\d/\d)){1,3}"  # 1-0-1, ½-0-½, 1-0-0-1
    rf"|\d+\s?[x×]\s?(?:\d+(?:[.,]\d+)?|½)(?:\s?{_EINHEIT_DOSIS})?(?:\s?{_ZEITEN})?"  # 3x1
    rf"|\d+\s?[x×]\s?{_ZEITEN}"  # 2x täglich
    rf"|{_WORTE}"
    r"|(?:\d{1,2}|½|¼|¾)"  # a bare count: valid only in a run that also has a unit or words
    r")(?![\wäöü])", re.IGNORECASE)
_BARE = re.compile(r"^(?:\d{1,2}|½|¼|¾)$")
_MENGE_MIT_EINHEIT = re.compile(rf"^{_MENGE}(?:\s?[-–]\s?{_MENGE})?\s?{_EINHEIT_DOSIS}",
                                re.IGNORECASE)
_SEP = re.compile(r"[ \t,;:/()|®™\-–]{0,4}")
_SEP_OHNE_KOMMA = re.compile(r"[ \t:/()|®™\-–]{0,4}")  # backwards, a comma ends the run
_NUR_LEER = re.compile(r"[ \t]*")  # a dose before a drug name touches it ("2 Tbl. Ibuprofen")
_RUECKBLICK = 80


@dataclass(frozen=True)
class Auswahl:
    """Which of the four levels are to be redacted."""

    medikamente: bool = False  # the drug name
    dosierungen: bool = False  # dose and schedule next to a drug name
    laborwerte: bool = False  # the lab name, the examination
    messwerte: bool = False  # the measurement after a lab name

    @classmethod
    def aus(cls, opt: object) -> Auswahl:
        """From the analysis options (attributes ``<level>_schwaerzen``)."""
        return cls(**{n: bool(getattr(opt, n + "_schwaerzen", False))
                      for n in ("medikamente", "dosierungen", "laborwerte", "messwerte")})

    @property
    def aktiv(self) -> bool:
        """Whether any level is to be redacted at all."""
        return self.medikamente or self.dosierungen or self.laborwerte or self.messwerte

    @property
    def medikamentennamen(self) -> bool:
        """Whether drug names are needed (to redact them or to anchor the doses)."""
        return self.medikamente or self.dosierungen

    @property
    def labornamen(self) -> bool:
        """Whether lab names are needed (to redact them or to anchor the measurements)."""
        return self.laborwerte or self.messwerte


def _norm(text: str) -> str:
    """Key of a term: whitespace collapsed, case-folded, umlauts transliterated."""
    return " ".join(text.split()).casefold().translate(_TRANS)


def _abkuerzung(begriff: str) -> bool:
    """Short entries and abbreviations count only as listed ("Hb", "LSD", "HbA1c")."""
    return len(begriff) <= 3 or bool(_ABKUERZUNG.match(begriff))


def messwert(text: str, ende: int) -> tuple[int, int] | None:
    """Span of a measurement right after a lab name ending at ``ende``, or None."""
    m = _MESSWERT.match(text, ende)
    return (m.start("wert"), m.end("wert")) if m else None


def _gueltiger_lauf(tokens: list[re.Match[str]]) -> bool:
    """A run of dose tokens counts only with a unit or a word in it; bare counts alone
    ("12", "3") are no dose."""
    return any(not _BARE.match(t.group()) for t in tokens)


def _trennt_medikamente(trenner: str) -> bool:
    """Whether the separator is a comma or semicolon, behind which a new amount with a
    unit belongs to the next drug ("…, 1 Tbl. Paracetamol")."""
    return "," in trenner or ";" in trenner


def _dosis_token_nach(text: str, pos: int, im_lauf: bool) -> re.Match[str] | None:
    """The next dose token behind ``pos``, or None where the run ends: at anything that
    is no token, and inside a run at an amount with a unit behind a comma."""
    sep = _SEP.match(text, pos)
    trenner = sep.group() if sep else ""
    tok = _DOSIS_TOKEN.match(text, sep.end() if sep else pos)
    if tok is None or tok.end() == tok.start():
        return None
    if im_lauf and _trennt_medikamente(trenner) and _MENGE_MIT_EINHEIT.match(tok.group()):
        return None
    return tok


def dosis_nach(text: str, ende: int) -> tuple[int, int] | None:
    """Span of the dose and schedule right after a drug name ending at ``ende``."""
    tokens: list[re.Match[str]] = []
    pos = ende
    while (tok := _dosis_token_nach(text, pos, bool(tokens))) is not None:
        tokens.append(tok)
        pos = tok.end()
    if not tokens or not _gueltiger_lauf(tokens):
        return None
    return tokens[0].start(), tokens[-1].end()


def dosis_vor(text: str, start: int) -> tuple[int, int] | None:
    """Span of a dose right before a drug name starting at ``start`` ("2 Tbl. Ibuprofen")."""
    anfang = max(text.rfind("\n", 0, start) + 1, start - _RUECKBLICK)
    vorher = text[anfang:start]
    tokens: list[re.Match[str]] = []
    grenze = len(vorher)
    for m in reversed(list(_DOSIS_TOKEN.finditer(vorher))):
        trenner = _NUR_LEER if not tokens else _SEP_OHNE_KOMMA
        if trenner.fullmatch(vorher[m.end():grenze]) is None:
            break
        tokens.insert(0, m)
        grenze = m.start()
    if not tokens or not _gueltiger_lauf(tokens):
        return None
    return anfang + tokens[0].start(), anfang + tokens[-1].end()


# -- the lists ------------------------------------------------------------------------------------
def _zeilen(verbindung: sqlite3.Connection) -> list[tuple[str, str]]:
    """Drug names and lab names, both always (a term on both lists needs both)."""
    verbindung.executescript(lexikon.SCHEMA)
    andere = ",".join("?" * len(_ANDERE_KLASSEN))
    zeilen = [(b, KLASSE_MEDIKAMENT) for (b,) in verbindung.execute(
        "SELECT DISTINCT l.begriff FROM lexikon l WHERE l.klasse = ? AND l.rolle = ?"
        " AND NOT EXISTS (SELECT 1 FROM lexikon m WHERE m.begriff = l.begriff"
        f" AND m.rolle = ? AND m.klasse IN ({andere}))",
        (KLASSE_MEDIKAMENT, lexikon.FACH, lexikon.FACH, *_ANDERE_KLASSEN))]
    zeilen += [(b, KLASSE_LABORWERT) for (b,) in verbindung.execute(
        "SELECT DISTINCT begriff FROM lexikon WHERE klasse = ? AND quelle = ?",
        (KLASSE_LABORWERT, QUELLE_LABORWERTE))]
    for klasse, namen in VOKABULAR_KATEGORIEN.items():
        try:
            zeilen += [(b, klasse) for (b,) in verbindung.execute(
                f"SELECT begriff FROM vokabular WHERE lower(kategorie) IN"
                f" ({','.join('?' * len(namen))})", namen)]
        except sqlite3.OperationalError:
            pass
    return zeilen


class Medizinsuche:
    """The matcher over drug and lab names plus the chosen levels."""

    def __init__(self, zeilen: list[tuple[str, str]], auswahl: Auswahl) -> None:
        """Index the ``(begriff, klasse)`` rows; drug names under three characters and the
        everyday substances are left out, a term on both lists is noted in ``beide``."""
        self.auswahl = auswahl
        self.erkenner = Erkenner(registernummern=False)
        je_klasse: dict[str, set[str]] = {KLASSE_MEDIKAMENT: set(), KLASSE_LABORWERT: set()}
        for begriff, klasse in zeilen:
            if klasse == KLASSE_MEDIKAMENT and (len(begriff) < 3
                                                or begriff.casefold() in _KEIN_MEDIKAMENT):
                continue
            if self.erkenner.fuege_hinzu(begriff, klasse, exakt=_abkuerzung(begriff)):
                je_klasse[klasse].add(_norm(begriff))
        self.beide = je_klasse[KLASSE_MEDIKAMENT] & je_klasse[KLASSE_LABORWERT]

    def mit(self, auswahl: Auswahl) -> Medizinsuche:
        """The same lists with another selection (the lists are shared)."""
        neu = object.__new__(Medizinsuche)
        neu.auswahl, neu.erkenner, neu.beide = auswahl, self.erkenner, self.beide
        return neu

    def fehlende_listen(self) -> list[str]:
        """Classes asked for whose list is empty (lexicon not loaded?)."""
        fehlt = []
        if self.auswahl.medikamentennamen and not self.erkenner.je_klasse.get(KLASSE_MEDIKAMENT):
            fehlt.append(KLASSE_MEDIKAMENT)
        if self.auswahl.labornamen and not self.erkenner.je_klasse.get(KLASSE_LABORWERT):
            fehlt.append(KLASSE_LABORWERT)
        return fehlt

    def _klasse(self, text: str, start: int, ende: int, klasse: str) -> str:
        """A term on both lists: by the context, else by what is asked for."""
        if _norm(text[start:ende]) not in self.beide:
            return klasse
        wert, dosis = messwert(text, ende), dosis_nach(text, ende)
        if wert is not None and (dosis is None or wert[1] >= dosis[1]):
            return KLASSE_LABORWERT  # "Kalium 4,1 mmol/l"; "Kalium 40 mmol 1-0-1" is a dose
        if dosis is not None or dosis_vor(text, start) is not None:
            return KLASSE_MEDIKAMENT
        return KLASSE_LABORWERT if self.auswahl.labornamen else KLASSE_MEDIKAMENT

    def _medikamentenspannen(self, text: str, k: Kandidat) -> list[tuple[int, int, str]]:
        """For a drug term: the name if asked, the doses before and behind it if asked."""
        aus: list[tuple[int, int, str]] = []
        if self.auswahl.medikamente:
            aus.append((k.start, k.ende, KLASSE_MEDIKAMENT))
        if self.auswahl.dosierungen:
            aus += [(*spanne, PRAEFIX_DOSIERUNG)
                    for spanne in (dosis_vor(text, k.start), dosis_nach(text, k.ende))
                    if spanne is not None]
        return aus

    def _laborspannen(self, text: str, k: Kandidat) -> list[tuple[int, int, str]]:
        """For a lab term: the name if asked, the measurement behind it if asked."""
        aus: list[tuple[int, int, str]] = []
        if self.auswahl.laborwerte:
            aus.append((k.start, k.ende, KLASSE_LABORWERT))
        spanne = messwert(text, k.ende) if self.auswahl.messwerte else None
        if spanne is not None:
            aus.append((*spanne, PRAEFIX_MESSWERT))
        return aus

    def spannen(self, text: str) -> list[tuple[int, int, str]]:
        """(start, end, kind) of every element to redact, kind being the pseudonym prefix."""
        aus: list[tuple[int, int, str]] = []
        for k in self.erkenner.kandidaten(text):
            klasse = self._klasse(text, k.start, k.ende, k.klasse)
            if klasse == KLASSE_MEDIKAMENT:
                aus += self._medikamentenspannen(text, k)
            else:
                aus += self._laborspannen(text, k)
        return sorted(set(aus))

    def finde(self, seite: Seitentext, vergabe: Pseudonymvergabe, vorhandene: list[Fundstelle],
              quelle: str = "text") -> list[Fundstelle]:
        """The chosen elements on one page that no other hit covers."""
        text = seite.text
        treffer: list[Fundstelle] = []
        for a, b, art in self.spannen(text):
            if any(f.ende > f.start and a < f.ende and b > f.start for f in vorhandene + treffer):
                continue
            klartext = text[a:b]
            treffer.append(Fundstelle(
                seite=seite.nummer, start=a, ende=b, text=klartext, art=Art.MEDIZIN,
                regel=REGEL[art], pseudonym=vergabe.identifikator(art, klartext, Art.MEDIZIN),
                rects=[tuple(r) for r in seite.rects(a, b)],
                schriftgroesse=seite.schriftgroesse(a, b), quelle=quelle))
        return treffer


# -- the matcher used by the analysis ----------------------------------------------------------
class _Standardsuche:
    """The matcher for the chosen medical levels, its lists kept until the lexicon or the
    vocabulary changes; ``cache_clear`` makes the next call rebuild them.  A callable
    object rather than a function with a bolted-on attribute, so that ``cache_clear`` is
    typed."""

    def __init__(self) -> None:
        """Starts without cached lists."""
        self._cache: tuple[Path, int, int, Medizinsuche] | None = None

    def __call__(self, auswahl: Auswahl) -> Medizinsuche | None:
        """The matcher for the chosen levels (None when none is chosen), its lists rebuilt
        only when the lexicon or the vocabulary changed."""
        if not auswahl.aktiv:
            return None
        from pseudonymed.erkennung.vokabular import Vokabular

        try:
            with Vokabular() as v:
                versionen = (lexikon.version(v.verbindung), v.version())
                if self._cache is not None and self._cache[0] == v.pfad \
                        and self._cache[1:3] == versionen:
                    return self._cache[3].mit(auswahl)
                suche = Medizinsuche(_zeilen(v.verbindung), auswahl)
                pfad = v.pfad
        except (sqlite3.Error, OSError):
            return Medizinsuche([], auswahl)
        self._cache = (pfad, *versionen, suche)
        return suche

    def cache_clear(self) -> None:
        """Forget the cached lists (the tests swap the lexicon underneath them)."""
        self._cache = None


standard = _Standardsuche()

