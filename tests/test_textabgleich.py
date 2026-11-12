"""Corrected or restructured OCR text gets the boxes of the OCR words."""

from __future__ import annotations

import pymupdf

from pseudonymed.dokument.ocr import seitentext_aus_woerter
from pseudonymed.dokument.textabgleich import (
    _geteilte_boxen,
    _hinter_anker,
    _laeufe,
    _Lauf,
    _wortspannen,
    _zwischen_nachbarn,
    textebene_aus_korrektur,
)
from tests.test_gliederung import _zeile


def _ocr():
    woerter = _zeile("Herr Max Mustermann wohnt in Dresden", 60, 100)
    woerter += _zeile("Diagnosen: Mlgraene seit Jahren", 60, 120)
    return seitentext_aus_woerter(woerter, 0, 595, 842)


def _box(seite, wort: str) -> pymupdf.Rect:
    a = seite.text.index(wort)
    return seite.rects(a, a + len(wort))[0]


def test_woerter_erhalten_ihre_boxen() -> None:
    ocr = _ocr()
    text = ("**Herr Max Mustermann** wohnt heute in Dresden\n\n"
            "#### Diagnosen: Migräne seit Jahren\n\nZusatz: neu\n")
    ebene = textebene_aus_korrektur(text, ocr)
    assert ebene.text == text
    for wort in ("Herr", "Mustermann", "Dresden", "Jahren"):
        assert abs(_box(ebene, wort).x0 - _box(ocr, wort).x0) < 0.01
        assert abs(_box(ebene, wort).x1 - _box(ocr, wort).x1) < 0.01
    # a corrected word keeps the box of the word it replaces
    assert abs(_box(ebene, "Migräne").x0 - _box(ocr, "Mlgraene").x0) < 0.01
    assert abs(_box(ebene, "Migräne").x1 - _box(ocr, "Mlgraene").x1) < 0.01
    # an inserted word sits between its neighbours on the same line
    heute = _box(ebene, "heute")
    assert _box(ocr, "wohnt").x1 <= heute.x0 and heute.x1 <= _box(ocr, "in").x0
    assert abs(heute.y0 - _box(ocr, "wohnt").y0) < 0.01
    # markup has no box and is no word of the layer
    woerter = {text[a:b] for a, b in ebene.wortspannen}
    assert "**" not in woerter and "####" not in woerter and "Herr" in woerter
    # text without a counterpart continues after the last word
    zusatz = _box(ebene, "Zusatz:")
    assert zusatz.x0 > _box(ocr, "Jahren").x1 and abs(zusatz.y0 - _box(ocr, "Jahren").y0) < 0.01


def test_zusammengezogene_zeilen_und_leere_ocr() -> None:
    ocr = _ocr()
    text = "Herr Max Mustermann wohnt in Dresden Diagnosen: Mlgraene seit Jahren\n"
    ebene = textebene_aus_korrektur(text, ocr)  # joined into one paragraph line
    assert abs(_box(ebene, "Diagnosen:").y0 - _box(ocr, "Diagnosen:").y0) < 0.01
    assert abs(_box(ebene, "Herr").y0 - _box(ocr, "Herr").y0) < 0.01
    leer = seitentext_aus_woerter([], 0, 595, 842)
    ebene = textebene_aus_korrektur("Nur Korrektur ohne OCR\n", leer)
    assert len(ebene.wortspannen) == 4 and all(b is not None for b in ebene.boxen[:3])


def test_wortspannen_lassen_markup_aus() -> None:
    text = "#### **Herr** Max, | - (Zusatz)"
    assert [text[a:b] for a, b in _wortspannen(text)] == ["Herr", "Max,", "(Zusatz)"]


def test_geteilte_boxen_teilen_und_vereinigen() -> None:
    eins, zwei = pymupdf.Rect(0, 0, 30, 10), pymupdf.Rect(40, 0, 60, 10)
    unten = pymupdf.Rect(0, 30, 20, 40)
    # two words on one OCR word: its box split by word length
    a, b = _geteilte_boxen([1, 2], [eins])
    assert (a.x0, a.x1, b.x0, b.x1) == (0, 10, 10, 30)
    # one word for two OCR words on one line: their union
    (vereint,) = _geteilte_boxen([5], [eins, zwei])
    assert (vereint.x0, vereint.x1) == (0, 60)
    # one word for two OCR words on different lines: the first box only
    (erste,) = _geteilte_boxen([5], [eins, unten])
    assert (erste.x0, erste.x1, erste.y1) == (0, 30, 10)


def test_laeufe_kennen_ihre_nachbarn() -> None:
    r1, r2 = pymupdf.Rect(0, 0, 10, 10), pymupdf.Rect(50, 0, 60, 10)
    tokens = [(0, 2), (3, 7), (8, 9), (10, 12), (13, 20)]
    laeufe = _laeufe(tokens, [r1, None, None, r2, None])
    assert [(lf.start, lf.ende, lf.laengen) for lf in laeufe] == [(1, 3, (4, 1)), (4, 5, (7,))]
    assert laeufe[0].vor == r1 and laeufe[0].nach == r2
    assert laeufe[1].vor == r2 and laeufe[1].nach is None


def test_rest_zwischen_nachbarn_oder_dahinter() -> None:
    vor, nach = pymupdf.Rect(0, 0, 10, 10), pymupdf.Rect(40, 0, 50, 10)
    (r,) = _zwischen_nachbarn(_Lauf(1, 2, (3,), vor, nach), 10.0)
    assert vor.x1 < r.x0 and r.x1 < nach.x0 and (r.y0, r.y1) == (0, 10)
    # neighbours on different lines leave no gap to sit in
    andere_zeile = pymupdf.Rect(40, 30, 50, 40)
    assert _zwischen_nachbarn(_Lauf(1, 2, (3,), vor, andere_zeile), 10.0) is None
    # after the previous word, wrapping to a new line below at the page's right edge
    rects = _hinter_anker(_Lauf(1, 4, (4, 4, 4), pymupdf.Rect(60, 0, 80, 10), None), 10.0, 120.0)
    assert (rects[0].x0, rects[0].y0) == (83, 0)
    assert rects[1].x0 == 36 and abs(rects[1].y0 - 12) < 1e-9 and rects[2].y0 == rects[1].y0
    # before the next word when nothing precedes the run
    (davor,) = _hinter_anker(_Lauf(0, 1, (2,), None, nach), 10.0, 595.0)
    assert davor.x1 < nach.x0 and davor.y0 == nach.y0
    # no anchor at all: an invented first line at the top left of the page
    (allein,) = _hinter_anker(_Lauf(0, 1, (2,), None, None), 10.0, 595.0)
    assert allein.x0 == 36 and abs(allein.y0 - 36) < 1e-9 and abs(allein.height - 10) < 1e-9
