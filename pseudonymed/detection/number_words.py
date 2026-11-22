"""German number words to digits, for transcripts.

A speech recogniser sometimes writes "einundzwanzigster Juli neunzehnhundert-
neunundfünfzig" or "Sieringgasse zweiundvierzig" where the rules expect
"21. Juli 1959" and "Sieringgasse 42".  :func:`normalise` rewrites such
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

_ONES = {"eins": 1, "ein": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
         "sechs": 6, "sieben": 7, "acht": 8, "neun": 9}
_TEN_TO_NINETEEN = {"zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13,
                    "vierzehn": 14, "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16,
                    "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_TENS = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
         "fuenfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90}
_IRREGULAR_ORDINALS = {"erst": 1, "dritt": 3, "siebt": 7}
_ARTICLES = {"ein", "eine", "einen", "einem", "einer", "eines"}
_MONTHS = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
           "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
           "dezember": 12}
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+|\d+")


def _below_hundred(w: str) -> int | None:
    if w in _ONES:
        return _ONES[w]
    if w in _TEN_TO_NINETEEN:
        return _TEN_TO_NINETEEN[w]
    if w in _TENS:
        return _TENS[w]
    ones, conj, tens = w.partition("und")
    if conj and ones in _ONES and ones != "eins" and tens in _TENS:
        return _ONES[ones] + _TENS[tens]
    return None


def _below_thousand(w: str) -> int | None:
    first, hundred, rest = w.partition("hundert")
    if not hundred:
        return _below_hundred(w)
    h = 1 if first in ("", "ein") else _below_hundred(first)
    if h is None or first == "eins":
        return None
    if not rest:
        return h * 100
    r = _below_hundred(rest)
    return None if r is None else h * 100 + r


def cardinal(word: str) -> int | None:
    """Value of a cardinal number word, or None.  "ein" alone is an article."""
    w = word.casefold()
    if w == "null":
        return 0
    if w in _ARTICLES:
        return None
    first, thousand, rest = w.partition("tausend")
    if thousand:
        t = 1 if first in ("", "ein") else _below_thousand(first)
        if t is None or first == "eins":
            return None
        if not rest:
            return t * 1000
        r = _below_thousand(rest)
        return None if r is None else t * 1000 + r
    return _below_thousand(w)


def ordinal(word: str) -> int | None:
    """Value of an ordinal number word ("einundzwanzigsten" -> 21), or None."""
    w = word.casefold()
    for stem, fixed in _IRREGULAR_ORDINALS.items():
        if re.fullmatch(stem + r"e[rsnm]?", w):
            return fixed
    m = re.fullmatch(r"(.+?)(?:ste|te)[rsnm]?", w)
    if m:
        value = cardinal(m.group(1))  # "zweite", "einundzwanzigsten", "hundertste"
        if value is not None:
            return value
    m = re.fullmatch(r"(.+?)e[rsnm]?", w)  # "achte", "achten"
    if m:
        value = cardinal(m.group(1))
        if value is not None and m.group(1) not in _ARTICLES:
            return value
    return None


@dataclass
class Normalised:
    """A copy of a text with the same length in which number words stand as digits, and
    the regions that were rewritten, so that a hit on the digits can be widened back to
    the words it stands for."""
    text: str
    spans: list[tuple[int, int]] = field(default_factory=list)  # rewritten regions

    def widen(self, start: int, end: int) -> tuple[int, int]:
        """Widen ``[start, ende)`` to cover every rewritten region it touches."""
        for a, b in self.spans:
            if start < b and end > a:
                start, end = min(start, a), max(end, b)
        return start, end


@dataclass
class _Token:
    start: int
    end: int
    word: str
    cardinal: int | None = None
    ordinal: int | None = None
    month: int | None = None
    digits: bool = False

    @property
    def number(self) -> bool:
        return self.cardinal is not None or self.ordinal is not None


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for m in _WORD.finditer(text):
        w = m.group(0)
        t = _Token(m.start(), m.end(), w)
        if w.isdigit():
            t.digits = True
        else:
            t.ordinal = ordinal(w)
            t.cardinal = None if t.ordinal is not None else cardinal(w)
            t.month = _MONTHS.get(w.casefold())
        tokens.append(t)
    return tokens


class _NumberGroup(NamedTuple):
    """Consecutive tokens that are rewritten together, and the digits replacing them."""
    n_tokens: int
    digits: str


def _adjoins(text: str, a: _Token, b: _Token) -> bool:
    """Whether only spaces or tabs separate token ``a`` from token ``b``; a line break
    ends every group."""
    return text[a.end:b.start].strip(" \t") == ""


def _single_digit(t: _Token) -> bool:
    """Whether the token is a single-digit number word ("null" to "neun")."""
    return t.cardinal is not None and 0 <= t.cardinal <= 9


def _year_pair(j: _Token, k: _Token | None, text: str) -> _NumberGroup | None:
    """A year spoken in two words ("neunzehn neunundfünfzig" -> 1959): a word from 10 to
    20 directly followed by a word from 10 to 99.  None otherwise."""
    if k is None or j.cardinal is None or k.cardinal is None:
        return None
    if 10 <= j.cardinal <= 20 and 10 <= k.cardinal <= 99 and _adjoins(text, j, k):
        return _NumberGroup(2, f"{j.cardinal}{k.cardinal:02d}")
    return None


def _digit_run(tokens: list[_Token], i: int, text: str) -> _NumberGroup | None:
    """A run of at least two single-digit words from token i ("acht zwei vier" -> 824),
    as postcodes and telephone numbers are spoken.  None otherwise."""
    if not _single_digit(tokens[i]):
        return None
    n = 1
    while (i + n < len(tokens) and _single_digit(tokens[i + n])
           and _adjoins(text, tokens[i + n - 1], tokens[i + n])):
        n += 1
    if n == 1:
        return None
    return _NumberGroup(n, "".join(str(t.cardinal) for t in tokens[i:i + n]))


def _date_day(t: _Token) -> int | None:
    """The day of a date: an ordinal from 1 to 31, else None."""
    if t.ordinal is not None and 1 <= t.ordinal <= 31:
        return t.ordinal
    return None


def _date_month(t: _Token, text: str) -> str | None:
    """The month of a date as it will be written: the name as it stands in the text, or
    "7." for an ordinal from 1 to 12.  None otherwise."""
    if t.month is not None:
        return text[t.start:t.end]
    if t.ordinal is not None and 1 <= t.ordinal <= 12:
        return f"{t.ordinal}."
    return None


def _year_digits(t: _Token) -> _NumberGroup | None:
    """A year written in digits: exactly two or four of them, else None."""
    if t.digits and len(t.word) in (2, 4):
        return _NumberGroup(1, t.word)
    return None


def _year_word(t: _Token) -> _NumberGroup | None:
    """A year as one number word: 0 to 99 ("neunundfünfzig") or 1000 to 2999, else None."""
    if t.cardinal is not None and (0 <= t.cardinal <= 99 or 1000 <= t.cardinal <= 2999):
        return _NumberGroup(1, str(t.cardinal))
    return None


def _date_year(tokens: list[_Token], i: int, text: str) -> _NumberGroup | None:
    """The year of a date at token i, directly after the month at token i - 1: digits, a
    pair of words or one word, in that order of preference.  None when there is none."""
    if i >= len(tokens) or not _adjoins(text, tokens[i - 1], tokens[i]):
        return None
    j = tokens[i]
    k = tokens[i + 1] if i + 1 < len(tokens) else None
    return _year_digits(j) or _year_pair(j, k, text) or _year_word(j)


def _date_text(day: int, month: str, year: str, month_name: bool) -> str:
    """"21. Juli 1959" when the month is a name, "21.7.1959" when it is an ordinal."""
    if month_name:
        return f"{day}. {month}" + (f" {year}" if year else "")
    return f"{day}.{month}{year}"


def _date(tokens: list[_Token], i: int, text: str) -> _NumberGroup | None:
    """A date starting at token i: ordinal day, month (name or ordinal), optional year
    (word or digits).  None when the tokens do not form one."""
    if i + 1 >= len(tokens):
        return None
    day, month = tokens[i], tokens[i + 1]
    day_value = _date_day(day)
    if day_value is None or not _adjoins(text, day, month):
        return None
    month_text = _date_month(month, text)
    if month_text is None:
        return None
    year = _date_year(tokens, i + 2, text) or _NumberGroup(0, "")
    replacement = _date_text(day_value, month_text, year.digits, month_name=month.month is not None)
    return _NumberGroup(2 + year.n_tokens, replacement)


def _number_group(tokens: list[_Token], i: int, text: str) -> _NumberGroup | None:
    """The number group that starts at token i, or None when that token is no number.
    The longest reading wins: a date, then a run of single digits, then a year in two
    words, then the word alone."""
    t = tokens[i]
    date = _date(tokens, i, text)
    if date is not None:
        return date
    if t.cardinal is None:
        return _NumberGroup(1, f"{t.ordinal}.") if t.ordinal is not None else None
    next_token = tokens[i + 1] if i + 1 < len(tokens) else None
    return (_digit_run(tokens, i, text) or _year_pair(t, next_token, text)
            or _NumberGroup(1, str(t.cardinal)))


class _Copy:
    """The length-preserving copy of a text while number words are being rewritten into
    it, together with the regions rewritten so far."""

    def __init__(self, original: str) -> None:
        self.original = original
        self.chars = list(original)
        self.spans: list[tuple[int, int]] = []

    def write(self, a: int, b: int, replacement: str) -> None:
        """Put ``replacement`` at ``a``, blank the rest of ``[a, b)`` (line breaks stay) and
        record the region.  A replacement longer than the region is dropped: that cannot
        happen for German number words, and the offsets must never be corrupted."""
        if len(replacement) > b - a:
            return
        for k in range(a, b):
            self.chars[k] = "\n" if self.original[k] == "\n" else " "
        self.chars[a:a + len(replacement)] = replacement
        self.spans.append((a, b))

    def result(self) -> Normalised:
        """The rewritten text with its rewritten regions."""
        return Normalised("".join(self.chars), self.spans)


def normalise(text: str) -> Normalised:
    """Rewrite number words as digits, keeping the length of ``text``."""
    copy = _Copy(text)
    tokens = _tokens(text)
    i = 0
    while i < len(tokens):
        group = _number_group(tokens, i, text)
        if group is None:
            i += 1
            continue
        copy.write(tokens[i].start, tokens[i + group.n_tokens - 1].end, group.digits)
        i += group.n_tokens
    return copy.result()
