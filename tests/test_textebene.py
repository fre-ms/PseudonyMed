"""Character map of a page: offsets map back to rectangles."""

from __future__ import annotations

import pymupdf
import pytest

from pseudonymed.dokument.textebene import (
    Seitentext,
    _glyphen,
    _lese_zeile,
    _wortluecke,
    lese_seite,
)


def _seite() -> pymupdf.Page:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Erste Zeile mit Mustermann, Komma", fontsize=11, fontname="helv")
    page.insert_text((72, 116), "Zweite Zeile", fontsize=11, fontname="helv")
    return page


def test_text_und_zeilen() -> None:
    s = lese_seite(_seite())
    assert "Erste Zeile mit Mustermann, Komma" in s.text
    assert "Zweite Zeile" in s.text
    assert s.hat_textebene
    a = s.text.index("Mustermann")
    b = a + len("Mustermann")
    assert s.zeile_von(a) == s.zeile_von(b - 1)
    assert s.zeile_von(a) != s.zeile_von(s.text.index("Zweite"))


def test_rects_und_freier_raum() -> None:
    s = lese_seite(_seite())
    a = s.text.index("Mustermann")
    b = a + len("Mustermann")
    rects = s.rects(a, b)
    assert len(rects) == 1
    r = rects[0]
    assert 72 < r.x0 < r.x1 < s.breite
    assert 85 < r.y0 < r.y1 < 110
    # the comma follows immediately, so there is no free space to the right
    assert abs(s.freier_raum_rechts(b) - r.x1) < 0.5
    assert s.schriftgroesse(a, b) == 11.0
    assert s.basislinie(a, b) is not None and abs(s.basislinie(a, b) - 100) < 0.5


def test_leere_seite() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    s = lese_seite(page)
    assert s.text.strip() == ""
    assert not s.hat_textebene


def test_dict_rundlauf_und_pruefung() -> None:
    """The JSON reader restores tuples, drops line boxes without a box and rejects
    character lists that do not fit the text."""
    roh = {"nummer": 3, "breite": 595.2, "hoehe": 841.9, "text": "ab c\n", "quelle": "ocr",
           "boxen": [[1, 2, 3, 4], [5, 6, 7, 8], None, [9, 10, 11, 12], None],
           "zeilen": [1, 1, 1, 1, 1], "groessen": [10, 10, 0, 10, 0],
           "basislinien": [4, 4, 0, 12, 0], "zeilenboxen": {"1": [1, 2, 11, 12], "2": None},
           "konfidenzen": [0.9, 0.4], "wortspannen": [[0, 2], [3, 4]]}
    s = Seitentext.aus_dict(roh)
    assert s.boxen[0] == (1, 2, 3, 4) and s.boxen[2] is None
    assert s.zeilenboxen == {1: (1, 2, 11, 12)}
    assert s.wortspannen == [(0, 2), (3, 4)] and s.woerter() == [(0, 2), (3, 4)]
    assert s.unsichere_woerter == 1 and s.quelle == "ocr"
    assert Seitentext.aus_dict(s.als_dict()) == s
    assert Seitentext.aus_dict({"nummer": 0, "breite": 1, "hoehe": 2, "text": ""}).boxen == []
    with pytest.raises(ValueError, match="Seitentext 4"):
        Seitentext.aus_dict(dict(roh, zeilen=[1, 1]))


def test_glyphen_und_wortluecke() -> None:
    """A rawdict line yields one glyph per character (baseline from the span when the
    character has none), and a wide gap without a space in the text gets one."""
    line = {"spans": [
        {"size": 12, "origin": (72, 100),
         "chars": [{"c": "a", "bbox": (72, 90, 78, 102), "origin": (72, 100)},
                   {"c": "b", "bbox": (78, 90, 84, 102)}]},
        {"size": 8, "origin": (100, 100),
         "chars": [{"c": "c", "bbox": (100, 92, 104, 102), "origin": (100, 0)}]},
    ]}
    glyphen = list(_glyphen(line))
    assert [g[0] for g in glyphen] == ["a", "b", "c"]
    assert glyphen[1] == ("b", (78.0, 90.0, 84.0, 102.0), 12.0, 100.0)
    assert glyphen[2][3] == 100.0  # origin y = 0 falls back to the span's baseline
    assert _wortluecke("ab", (72, 90, 84, 102), (100, 92, 104, 102), 8.0)
    assert not _wortluecke("ab ", (72, 90, 84, 102), (100, 92, 104, 102), 8.0)
    assert not _wortluecke("ab", (72, 90, 84, 102), (85, 92, 104, 102), 8.0)
    s = Seitentext(nummer=0, breite=200, hoehe=200)
    _lese_zeile(s, line, 1)
    assert s.text == "ab c\n" and s.boxen[2] is None and s.zeilen == [1] * 5
    assert s.zeilenboxen == {1: (72.0, 90.0, 104.0, 102.0)}
