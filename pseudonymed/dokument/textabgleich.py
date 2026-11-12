"""Boxes for a corrected or restructured OCR text.

The user may correct the recognised text, and the structured Markdown joins and
rewraps lines.  The invisible text layer of the output must still sit on the image
words, so that a reader selects text exactly where it is printed.  This module aligns
the words of such a text to the OCR words (longest common subsequence on normalised
words), gives every word the box of its OCR counterpart, splits or merges boxes where
the counts differ, and places words without counterpart between or after their
neighbours.  Markdown markup (``**``, ``|``, ``#``, list dashes) gets no box and no
place in the layer.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from itertools import groupby

import pymupdf

from pseudonymed.dokument.textebene import Seitentext

_WORT = re.compile(r"\S+")
_MARKUP = re.compile(r"^[#*_>|`~=-]+$")
_RAND = "*_`#|>"
_RAND_LINKS = 36.0  # left margin (pt) of lines invented for words without any anchor


@dataclass(frozen=True)
class _Lauf:
    """A run of consecutive corrected words without an OCR counterpart, framed by the
    boxes of the matched words before and after it (``None`` at the page's ends)."""

    start: int
    ende: int
    laengen: tuple[int, ...]
    vor: pymupdf.Rect | None
    nach: pymupdf.Rect | None


def _kern(token: str) -> tuple[int, int]:
    """Start and end of the token's text without the surrounding Markdown markup."""
    a, b = 0, len(token)
    while a < b and token[a] in _RAND:
        a += 1
    while b > a and token[b - 1] in "*_`|":
        b -= 1
    return a, b


def _schluessel(wort: str) -> str:
    """Comparison key of a word: letters and digits only, case folded."""
    return re.sub(r"[\W_]+", "", wort).casefold()


def _box(seite: Seitentext, a: int, b: int) -> pymupdf.Rect | None:
    """Union of the character boxes of ``seite.text[a:b]``, ``None`` without any box."""
    rects = seite.rects(a, b)
    if not rects:
        return None
    r = pymupdf.Rect(rects[0])
    for q in rects[1:]:
        r |= q
    return r


def textebene_aus_korrektur(text: str, ocr: Seitentext) -> Seitentext:
    """A page text whose string is ``text`` and whose boxes come from ``ocr``."""
    tokens = _wortspannen(text)
    boxen = _abgeglichene_boxen(text, tokens, ocr)
    _platziere_rest(tokens, boxen, ocr)
    return _seitentext(text, tokens, boxen, ocr)


def _wortspannen(text: str) -> list[tuple[int, int]]:
    """Spans of the words of ``text`` that belong in the layer: markup tokens are
    skipped, the markup around the remaining words is stripped."""
    spannen: list[tuple[int, int]] = []
    for m in _WORT.finditer(text):
        tok = m.group(0)
        if _MARKUP.match(tok):
            continue
        a, b = _kern(tok)
        if a < b:
            spannen.append((m.start() + a, m.start() + b))
    return spannen


def _ocr_woerter(ocr: Seitentext) -> tuple[list[str], list[pymupdf.Rect]]:
    """Comparison keys and boxes of the OCR words that have a printed box."""
    paare = [(_schluessel(ocr.text[a:b]), _box(ocr, a, b)) for a, b in ocr.woerter()]
    gueltig = [(s, r) for s, r in paare if r is not None and not r.is_empty]
    return [s for s, _ in gueltig], [r for _, r in gueltig]


def _wortlaengen(tokens: list[tuple[int, int]], indizes: range | list[int]) -> list[int]:
    """Character count of the listed words, at least one so that every share has width."""
    return [max(1, tokens[k][1] - tokens[k][0]) for k in indizes]


def _abgeglichene_boxen(
    text: str, tokens: list[tuple[int, int]], ocr: Seitentext
) -> list[pymupdf.Rect | None]:
    """A box for every word with an OCR counterpart (longest common subsequence of the
    comparison keys), ``None`` for the rest; a replaced stretch shares out the boxes of
    the OCR words it replaces."""
    schl_text = [_schluessel(text[a:b]) for a, b in tokens]
    schl_ocr, ocr_boxen = _ocr_woerter(ocr)
    boxen: list[pymupdf.Rect | None] = [None] * len(tokens)
    sm = difflib.SequenceMatcher(None, schl_text, schl_ocr, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            boxen[i1:i2] = ocr_boxen[j1:j2]
        elif tag == "replace":
            laengen = _wortlaengen(tokens, range(i1, i2))
            boxen[i1:i2] = _geteilte_boxen(laengen, ocr_boxen[j1:j2])
    return boxen


def _geteilte_boxen(laengen: list[int], ocr_boxen: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """n corrected words for m OCR words: share the boxes out in order; when several
    words fall on one OCR word, its box is split by word length."""
    n, m = len(laengen), len(ocr_boxen)
    gruppen: dict[tuple[int, int], list[int]] = {}
    for k in range(n):
        ja = (k * m) // n
        jb = max(ja + 1, ((k + 1) * m) // n)
        gruppen.setdefault((ja, jb), []).append(k)
    boxen: dict[int, pymupdf.Rect] = {}
    for (ja, jb), indizes in gruppen.items():
        vereint = _zeilenvereinigung(ocr_boxen[ja:jb])
        teile = _nach_laenge_geteilt(vereint, [laengen[k] for k in indizes])
        boxen.update(zip(indizes, teile, strict=True))
    return [boxen[k] for k in range(n)]


def _zeilenvereinigung(boxen: list[pymupdf.Rect]) -> pymupdf.Rect:
    """Union of the boxes on the line of the first one; a word joined across lines
    keeps the first box."""
    vereint = pymupdf.Rect(boxen[0])
    hoechste = vereint.height
    for r in boxen[1:]:
        if r.y0 < vereint.y1 + hoechste * 0.5 and r.y1 > vereint.y0 - hoechste * 0.5:
            vereint |= r
    return vereint


def _nach_laenge_geteilt(box: pymupdf.Rect, laengen: list[int]) -> list[pymupdf.Rect]:
    """``box`` cut into one piece per word, the widths proportional to the word lengths."""
    gesamt = sum(laengen)
    return _reihe(box.x0, box.y0, box.y1, [box.width * laenge / gesamt for laenge in laengen])


def _reihe(
    x: float, y0: float, y1: float, breiten: list[float], abstand: float = 0.0
) -> list[pymupdf.Rect]:
    """Boxes of the given widths laid side by side from ``x``, ``abstand`` apart."""
    rects = []
    for breite in breiten:
        rects.append(pymupdf.Rect(x, y0, x + breite, y1))
        x += breite + abstand
    return rects


def _platziere_rest(
    tokens: list[tuple[int, int]], boxen: list[pymupdf.Rect | None], ocr: Seitentext
) -> None:
    """Words without an OCR counterpart: between their neighbours on the same line,
    else after the previous word (wrapping to a new line below), else before the next."""
    hoehe = _zeilenhoehe(boxen) or max(ocr.hoehe / 60.0, 8.0)
    for lauf in _laeufe(tokens, boxen):
        rects = _zwischen_nachbarn(lauf, hoehe)
        if rects is None:
            rects = _hinter_anker(lauf, hoehe, ocr.breite)
        boxen[lauf.start:lauf.ende] = rects


def _laeufe(tokens: list[tuple[int, int]], boxen: list[pymupdf.Rect | None]) -> list[_Lauf]:
    """The runs of consecutive words without a box, each with its neighbours' boxes.
    The neighbours are matched words, so the runs are independent of each other."""
    laeufe: list[_Lauf] = []
    for ohne_box, gruppe in groupby(range(len(boxen)), key=lambda k: boxen[k] is None):
        if not ohne_box:
            continue
        indizes = list(gruppe)
        start, ende = indizes[0], indizes[-1] + 1
        laeufe.append(_Lauf(start, ende, tuple(_wortlaengen(tokens, indizes)),
                            vor=boxen[start - 1] if start > 0 else None,
                            nach=boxen[ende] if ende < len(boxen) else None))
    return laeufe


def _auf_einer_zeile(vor: pymupdf.Rect, nach: pymupdf.Rect, hoehe: float) -> bool:
    """True when ``nach`` follows ``vor`` on the same line with a gap between them."""
    return abs(nach.y0 - vor.y0) < 0.6 * hoehe and nach.x0 - vor.x1 > 0.05 * hoehe


def _zwischen_nachbarn(lauf: _Lauf, hoehe: float) -> list[pymupdf.Rect] | None:
    """Boxes for the run in the gap between its neighbours, ``None`` when the run has
    no neighbour on either side or the neighbours do not share a line.  However narrow
    the gap: the words have no printed counterpart, what matters is that they stay in
    reading order."""
    vor, nach = lauf.vor, lauf.nach
    if vor is None or nach is None or not _auf_einer_zeile(vor, nach, hoehe):
        return None
    luecke = nach.x0 - vor.x1
    gesamt = sum(lauf.laengen) + (len(lauf.laengen) + 1) * 0.5
    polster = luecke * 0.5 / gesamt
    breiten = [luecke * laenge / gesamt for laenge in lauf.laengen]
    return _reihe(vor.x1 + polster, vor.y0, vor.y1, breiten, polster)


def _startpunkt(lauf: _Lauf, hoehe: float) -> tuple[float, float, pymupdf.Rect]:
    """Where a run without a gap to sit in starts (x, y0), and the box whose height it
    borrows: after the previous word, else before the next word, else on an invented
    first line at the top left of the page."""
    if lauf.vor is not None:
        return lauf.vor.x1 + 0.3 * hoehe, lauf.vor.y0, lauf.vor
    if lauf.nach is not None:
        nach = lauf.nach
        x = max(0.0, nach.x0 - 0.3 * hoehe - 0.55 * hoehe * sum(lauf.laengen))
        return x, nach.y0, nach
    anker = pymupdf.Rect(_RAND_LINKS, 36 - 1.2 * hoehe, _RAND_LINKS, 36 - 0.2 * hoehe)
    return anker.x0, anker.y1 + 0.2 * hoehe, anker


def _hinter_anker(lauf: _Lauf, hoehe: float, seitenbreite: float) -> list[pymupdf.Rect]:
    """Boxes for the run laid out from its start point, 0.55 line heights per character,
    wrapping to a new line below at the page's right edge."""
    x, y0, anker = _startpunkt(lauf, hoehe)
    h = anker.height if anker.height > 0 else hoehe
    rects = []
    for laenge in lauf.laengen:
        breite = 0.55 * h * laenge
        if seitenbreite and x + breite > seitenbreite - 10 and x > _RAND_LINKS:
            x, y0 = _RAND_LINKS, y0 + 1.2 * h  # wrap below
        rects.append(pymupdf.Rect(x, y0, x + breite, y0 + h))
        x += breite + 0.3 * h
    return rects


def _zeilenhoehe(boxen: list[pymupdf.Rect | None]) -> float:
    """Median height of the boxes found so far, 0 when there is none."""
    hoehen = sorted(r.height for r in boxen if r is not None and r.height > 0)
    return hoehen[len(hoehen) // 2] if hoehen else 0.0


def _seitentext(
    text: str, tokens: list[tuple[int, int]], boxen: list[pymupdf.Rect | None], ocr: Seitentext
) -> Seitentext:
    """The page text: every word's box shared out over its characters, lines counted
    from the newlines of ``text``, line boxes and word spans as the OCR page has them."""
    n = len(text)
    seite = Seitentext(nummer=ocr.nummer, breite=ocr.breite, hoehe=ocr.hoehe, text=text,
                       quelle="ocr")
    seite.boxen = [None] * n
    seite.groessen = [0.0] * n
    seite.basislinien = [0.0] * n
    zeilen: list[int] = []
    zeile = 0
    for c in text:
        zeilen.append(zeile)
        if c == "\n":
            zeile += 1
    seite.zeilen = zeilen
    for (a, b), r in zip(tokens, boxen, strict=True):
        if r is None or r.is_empty:
            continue
        schritt = r.width / max(1, b - a)
        for k, i in enumerate(range(a, b)):
            seite.boxen[i] = (r.x0 + k * schritt, r.y0, r.x0 + (k + 1) * schritt, r.y1)
            seite.groessen[i] = max(r.height, 1.0) * 0.8
            seite.basislinien[i] = r.y1 - 0.2 * r.height
        z = zeilen[a]
        alt = seite.zeilenboxen.get(z)
        seite.zeilenboxen[z] = tuple(r) if alt is None else (
            min(alt[0], r.x0), min(alt[1], r.y0), max(alt[2], r.x1), max(alt[3], r.y1))
        seite.wortspannen.append((a, b))
        seite.konfidenzen.append(1.0)
    return seite
