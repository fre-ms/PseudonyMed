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

import pymupdf

from pseudonymed.dokument.textebene import Seitentext

_WORT = re.compile(r"\S+")
_MARKUP = re.compile(r"^[#*_>|`~=-]+$")
_RAND = "*_`#|>"


def _kern(token: str) -> tuple[int, int]:
    a, b = 0, len(token)
    while a < b and token[a] in _RAND:
        a += 1
    while b > a and token[b - 1] in "*_`|":
        b -= 1
    return a, b


def _schluessel(wort: str) -> str:
    return re.sub(r"[\W_]+", "", wort).casefold()


def _box(seite: Seitentext, a: int, b: int) -> pymupdf.Rect | None:
    rects = seite.rects(a, b)
    if not rects:
        return None
    r = pymupdf.Rect(rects[0])
    for q in rects[1:]:
        r |= q
    return r


def textebene_aus_korrektur(text: str, ocr: Seitentext) -> Seitentext:
    """A page text whose string is ``text`` and whose boxes come from ``ocr``."""
    tokens: list[tuple[int, int]] = []
    for m in _WORT.finditer(text):
        tok = m.group(0)
        if _MARKUP.match(tok):
            continue
        a, b = _kern(tok)
        if a < b:
            tokens.append((m.start() + a, m.start() + b))
    ocr_spannen = ocr.woerter()
    ocr_boxen = [_box(ocr, a, b) for a, b in ocr_spannen]
    gueltig = [i for i, r in enumerate(ocr_boxen) if r is not None and not r.is_empty]
    ocr_spannen = [ocr_spannen[i] for i in gueltig]
    ocr_boxen = [ocr_boxen[i] for i in gueltig]
    schl_text = [_schluessel(text[a:b]) for a, b in tokens]
    schl_ocr = [_schluessel(ocr.text[a:b]) for a, b in ocr_spannen]

    boxen: list[pymupdf.Rect | None] = [None] * len(tokens)
    sm = difflib.SequenceMatcher(None, schl_text, schl_ocr, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                boxen[i1 + k] = ocr_boxen[j1 + k]
        elif tag == "replace":
            _verteile(tokens, boxen, i1, i2, ocr_boxen, j1, j2, text)
    _platziere_rest(tokens, boxen, text, ocr)
    return _seitentext(text, tokens, boxen, ocr)


def _verteile(tokens, boxen, i1, i2, ocr_boxen, j1, j2, text) -> None:
    """n corrected words for m OCR words: share the boxes out in order; when several
    words fall on one OCR word, its box is split by word length."""
    n, m = i2 - i1, j2 - j1
    gruppen: dict[tuple[int, int], list[int]] = {}
    for k in range(n):
        ja = j1 + (k * m) // n
        jb = max(ja + 1, j1 + ((k + 1) * m) // n)
        gruppen.setdefault((ja, jb), []).append(i1 + k)
    for (ja, jb), indizes in gruppen.items():
        vereint = pymupdf.Rect(ocr_boxen[ja])
        hoechste = vereint.height
        for r in ocr_boxen[ja + 1:jb]:
            if r.y0 < vereint.y1 + hoechste * 0.5 and r.y1 > vereint.y0 - hoechste * 0.5:
                vereint |= r  # same line only; a word joined across lines keeps the first box
        laengen = [max(1, tokens[i][1] - tokens[i][0]) for i in indizes]
        gesamt = sum(laengen)
        x = vereint.x0
        for i, laenge in zip(indizes, laengen, strict=True):
            breite = vereint.width * laenge / gesamt
            boxen[i] = pymupdf.Rect(x, vereint.y0, x + breite, vereint.y1)
            x += breite


def _platziere_rest(tokens, boxen, text, ocr: Seitentext) -> None:
    """Words without an OCR counterpart: between their neighbours on the same line,
    else after the previous word (wrapping to a new line below), else before the next."""
    n = len(tokens)
    hoehe = _zeilenhoehe(boxen) or max(ocr.hoehe / 60.0, 8.0)
    i = 0
    while i < n:
        if boxen[i] is not None:
            i += 1
            continue
        j = i
        while j < n and boxen[j] is None:
            j += 1
        laengen = [max(1, tokens[k][1] - tokens[k][0]) for k in range(i, j)]
        vor = boxen[i - 1] if i > 0 else None
        nach = boxen[j] if j < n else None
        if vor is not None and nach is not None and abs(nach.y0 - vor.y0) < 0.6 * hoehe \
                and nach.x0 - vor.x1 > 0.05 * hoehe:
            # between its neighbours, however narrow the gap: the word has no printed
            # counterpart, what matters is that it stays in reading order
            x, luecke = vor.x1, nach.x0 - vor.x1
            gesamt = sum(laengen) + (len(laengen) + 1) * 0.5
            x += luecke * 0.5 / gesamt
            for k, laenge in zip(range(i, j), laengen, strict=True):
                breite = luecke * laenge / gesamt
                boxen[k] = pymupdf.Rect(x, vor.y0, x + breite, vor.y1)
                x += breite + luecke * 0.5 / gesamt
        else:
            anker = vor if vor is not None else nach
            if anker is None:  # no OCR word at all: lines from the top left
                anker = pymupdf.Rect(36, 36 - 1.2 * hoehe, 36, 36 - 0.2 * hoehe)
                x, y0 = anker.x0, anker.y1 + 0.2 * hoehe
            elif vor is not None:
                x, y0 = vor.x1 + 0.3 * hoehe, vor.y0
            else:
                x, y0 = max(0.0, nach.x0 - 0.3 * hoehe - 0.55 * hoehe * sum(laengen)), nach.y0
            h = anker.height if anker.height > 0 else hoehe
            for k, laenge in zip(range(i, j), laengen, strict=True):
                breite = 0.55 * h * laenge
                if ocr.breite and x + breite > ocr.breite - 10 and x > 36:
                    x, y0 = 36.0, y0 + 1.2 * h  # wrap below
                boxen[k] = pymupdf.Rect(x, y0, x + breite, y0 + h)
                x += breite + 0.3 * h
        i = j


def _zeilenhoehe(boxen) -> float:
    hoehen = sorted(r.height for r in boxen if r is not None and r.height > 0)
    return hoehen[len(hoehen) // 2] if hoehen else 0.0


def _seitentext(text, tokens, boxen, ocr: Seitentext) -> Seitentext:
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


