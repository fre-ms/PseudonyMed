"""German number words to digits, for transcripts.

A speech recogniser sometimes writes "einundzwanzigster Juli neunzehnhundert-
neunundfünfzig" or "Sieringgasse zweiundvierzig" where the rules expect
"21. Juli 1959" and "Sieringgasse 42".  :func:`normalisiere` rewrites such
words in a copy of the text **of the same length**: the digits take the place
of the word (or of a group of words) and the rest is padded with spaces, so
that every offset still points into the original.  The spans it rewrote are
returned so that a hit touching one can be widened to the whole word or group.

Recognised: cardinals up to the millions as one word ("zweitausendsechs-
undzwanzig"), ordinals ("erste", "einundzwanzigsten", "siebte", "achte"),
runs of single digits ("acht zwei vier sechs zwei" -> 82462), year pairs
("neunzehn neunundfünfzig" -> 1959) and dates (ordinal day, month as name or
ordinal, year as word or digits).  The articles "ein", "eine", "einen" … are
left alone; inside a compound ("einundzwanzig") they count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

_EINER = {"eins": 1, "ein": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
          "sechs": 6, "sieben": 7, "acht": 8, "neun": 9}
_ZEHN_BIS_NEUNZEHN = {"zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13,
                      "vierzehn": 14, "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16,
                      "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_ZEHNER = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
           "fuenfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90}
_ORDINAL_UNREGELMAESSIG = {"erst": 1, "dritt": 3, "siebt": 7}
_ARTIKEL = {"ein", "eine", "einen", "einem", "einer", "eines"}
_MONATE = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
           "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
           "dezember": 12}
_WORT = re.compile(r"[A-Za-zÄÖÜäöüß]+|\d+")


def _unter_hundert(w: str) -> int | None:
    if w in _EINER:
        return _EINER[w]
    if w in _ZEHN_BIS_NEUNZEHN:
        return _ZEHN_BIS_NEUNZEHN[w]
    if w in _ZEHNER:
        return _ZEHNER[w]
    einer, und, zehner = w.partition("und")
    if und and einer in _EINER and einer != "eins" and zehner in _ZEHNER:
        return _EINER[einer] + _ZEHNER[zehner]
    return None


def _unter_tausend(w: str) -> int | None:
    vor, hundert, rest = w.partition("hundert")
    if not hundert:
        return _unter_hundert(w)
    h = 1 if vor in ("", "ein") else _unter_hundert(vor)
    if h is None or vor == "eins":
        return None
    if not rest:
        return h * 100
    r = _unter_hundert(rest)
    return None if r is None else h * 100 + r


def kardinal(wort: str) -> int | None:
    """Value of a cardinal number word, or None.  "ein" alone is an article."""
    w = wort.casefold()
    if w == "null":
        return 0
    if w in _ARTIKEL:
        return None
    vor, tausend, rest = w.partition("tausend")
    if tausend:
        t = 1 if vor in ("", "ein") else _unter_tausend(vor)
        if t is None or vor == "eins":
            return None
        if not rest:
            return t * 1000
        r = _unter_tausend(rest)
        return None if r is None else t * 1000 + r
    return _unter_tausend(w)


def ordinal(wort: str) -> int | None:
    """Value of an ordinal number word ("einundzwanzigsten" -> 21), or None."""
    w = wort.casefold()
    for stamm, fest in _ORDINAL_UNREGELMAESSIG.items():
        if re.fullmatch(stamm + r"e[rsnm]?", w):
            return fest
    m = re.fullmatch(r"(.+?)(?:ste|te)[rsnm]?", w)
    if m:
        wert = kardinal(m.group(1))  # "zweite", "einundzwanzigsten", "hundertste"
        if wert is not None:
            return wert
    m = re.fullmatch(r"(.+?)e[rsnm]?", w)  # "achte", "achten"
    if m:
        wert = kardinal(m.group(1))
        if wert is not None and m.group(1) not in _ARTIKEL:
            return wert
    return None


@dataclass
class Normalisiert:
    """A copy of a text with the same length in which number words stand as digits, and
    the regions that were rewritten, so that a hit on the digits can be widened back to
    the words it stands for."""
    text: str
    spannen: list[tuple[int, int]] = field(default_factory=list)  # rewritten regions

    def erweitere(self, start: int, ende: int) -> tuple[int, int]:
        """Widen ``[start, ende)`` to cover every rewritten region it touches."""
        for a, b in self.spannen:
            if start < b and ende > a:
                start, ende = min(start, a), max(ende, b)
        return start, ende


@dataclass
class _Token:
    start: int
    ende: int
    wort: str
    kardinal: int | None = None
    ordinal: int | None = None
    monat: int | None = None
    ziffern: bool = False

    @property
    def zahl(self) -> bool:
        return self.kardinal is not None or self.ordinal is not None


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for m in _WORT.finditer(text):
        w = m.group(0)
        t = _Token(m.start(), m.end(), w)
        if w.isdigit():
            t.ziffern = True
        else:
            t.ordinal = ordinal(w)
            t.kardinal = None if t.ordinal is not None else kardinal(w)
            t.monat = _MONATE.get(w.casefold())
        tokens.append(t)
    return tokens


class _Zahlengruppe(NamedTuple):
    """Consecutive tokens that are rewritten together, and the digits replacing them."""
    anzahl: int
    ziffern: str


def _schliesst_an(text: str, a: _Token, b: _Token) -> bool:
    """Whether only spaces or tabs separate token ``a`` from token ``b``; a line break
    ends every group."""
    return text[a.ende:b.start].strip(" \t") == ""


def _einstellig(t: _Token) -> bool:
    """Whether the token is a single-digit number word ("null" to "neun")."""
    return t.kardinal is not None and 0 <= t.kardinal <= 9


def _jahrespaar(j: _Token, k: _Token | None, text: str) -> _Zahlengruppe | None:
    """A year spoken in two words ("neunzehn neunundfünfzig" -> 1959): a word from 10 to
    20 directly followed by a word from 10 to 99.  None otherwise."""
    if k is None or j.kardinal is None or k.kardinal is None:
        return None
    if 10 <= j.kardinal <= 20 and 10 <= k.kardinal <= 99 and _schliesst_an(text, j, k):
        return _Zahlengruppe(2, f"{j.kardinal}{k.kardinal:02d}")
    return None


def _ziffernlauf(tokens: list[_Token], i: int, text: str) -> _Zahlengruppe | None:
    """A run of at least two single-digit words from token i ("acht zwei vier" -> 824),
    as postcodes and telephone numbers are spoken.  None otherwise."""
    if not _einstellig(tokens[i]):
        return None
    n = 1
    while (i + n < len(tokens) and _einstellig(tokens[i + n])
           and _schliesst_an(text, tokens[i + n - 1], tokens[i + n])):
        n += 1
    if n == 1:
        return None
    return _Zahlengruppe(n, "".join(str(t.kardinal) for t in tokens[i:i + n]))


def _datumstag(t: _Token) -> int | None:
    """The day of a date: an ordinal from 1 to 31, else None."""
    if t.ordinal is not None and 1 <= t.ordinal <= 31:
        return t.ordinal
    return None


def _datumsmonat(t: _Token, text: str) -> str | None:
    """The month of a date as it will be written: the name as it stands in the text, or
    "7." for an ordinal from 1 to 12.  None otherwise."""
    if t.monat is not None:
        return text[t.start:t.ende]
    if t.ordinal is not None and 1 <= t.ordinal <= 12:
        return f"{t.ordinal}."
    return None


def _jahresziffern(t: _Token) -> _Zahlengruppe | None:
    """A year written in digits: exactly two or four of them, else None."""
    if t.ziffern and len(t.wort) in (2, 4):
        return _Zahlengruppe(1, t.wort)
    return None


def _jahreswort(t: _Token) -> _Zahlengruppe | None:
    """A year as one number word: 0 to 99 ("neunundfünfzig") or 1000 to 2999, else None."""
    if t.kardinal is not None and (0 <= t.kardinal <= 99 or 1000 <= t.kardinal <= 2999):
        return _Zahlengruppe(1, str(t.kardinal))
    return None


def _datumsjahr(tokens: list[_Token], i: int, text: str) -> _Zahlengruppe | None:
    """The year of a date at token i, directly after the month at token i - 1: digits, a
    pair of words or one word, in that order of preference.  None when there is none."""
    if i >= len(tokens) or not _schliesst_an(text, tokens[i - 1], tokens[i]):
        return None
    j = tokens[i]
    k = tokens[i + 1] if i + 1 < len(tokens) else None
    return _jahresziffern(j) or _jahrespaar(j, k, text) or _jahreswort(j)


def _datumstext(tag: int, monat: str, jahr: str, monatsname: bool) -> str:
    """"21. Juli 1959" when the month is a name, "21.7.1959" when it is an ordinal."""
    if monatsname:
        return f"{tag}. {monat}" + (f" {jahr}" if jahr else "")
    return f"{tag}.{monat}{jahr}"


def _datum(tokens: list[_Token], i: int, text: str) -> _Zahlengruppe | None:
    """A date starting at token i: ordinal day, month (name or ordinal), optional year
    (word or digits).  None when the tokens do not form one."""
    if i + 1 >= len(tokens):
        return None
    tag, monat = tokens[i], tokens[i + 1]
    tag_wert = _datumstag(tag)
    if tag_wert is None or not _schliesst_an(text, tag, monat):
        return None
    monat_text = _datumsmonat(monat, text)
    if monat_text is None:
        return None
    jahr = _datumsjahr(tokens, i + 2, text) or _Zahlengruppe(0, "")
    ersatz = _datumstext(tag_wert, monat_text, jahr.ziffern, monatsname=monat.monat is not None)
    return _Zahlengruppe(2 + jahr.anzahl, ersatz)


def _zahlengruppe(tokens: list[_Token], i: int, text: str) -> _Zahlengruppe | None:
    """The number group that starts at token i, or None when that token is no number.
    The longest reading wins: a date, then a run of single digits, then a year in two
    words, then the word alone."""
    t = tokens[i]
    datum = _datum(tokens, i, text)
    if datum is not None:
        return datum
    if t.kardinal is None:
        return _Zahlengruppe(1, f"{t.ordinal}.") if t.ordinal is not None else None
    naechstes = tokens[i + 1] if i + 1 < len(tokens) else None
    return (_ziffernlauf(tokens, i, text) or _jahrespaar(t, naechstes, text)
            or _Zahlengruppe(1, str(t.kardinal)))


class _Kopie:
    """The length-preserving copy of a text while number words are being rewritten into
    it, together with the regions rewritten so far."""

    def __init__(self, original: str) -> None:
        self.original = original
        self.zeichen = list(original)
        self.spannen: list[tuple[int, int]] = []

    def schreibe(self, a: int, b: int, ersatz: str) -> None:
        """Put ``ersatz`` at ``a``, blank the rest of ``[a, b)`` (line breaks stay) and
        record the region.  A replacement longer than the region is dropped: that cannot
        happen for German number words, and the offsets must never be corrupted."""
        if len(ersatz) > b - a:
            return
        for k in range(a, b):
            self.zeichen[k] = "\n" if self.original[k] == "\n" else " "
        self.zeichen[a:a + len(ersatz)] = ersatz
        self.spannen.append((a, b))

    def ergebnis(self) -> Normalisiert:
        """The rewritten text with its rewritten regions."""
        return Normalisiert("".join(self.zeichen), self.spannen)


def normalisiere(text: str) -> Normalisiert:
    """Rewrite number words as digits, keeping the length of ``text``."""
    kopie = _Kopie(text)
    tokens = _tokens(text)
    i = 0
    while i < len(tokens):
        gruppe = _zahlengruppe(tokens, i, text)
        if gruppe is None:
            i += 1
            continue
        kopie.schreibe(tokens[i].start, tokens[i + gruppe.anzahl - 1].ende, gruppe.ziffern)
        i += gruppe.anzahl
    return kopie.ergebnis()
