"""Markdown structure of a recognised page from the geometry of its lines."""

from __future__ import annotations

import pymupdf

from pseudonymed.dokument import gliederung as gl
from pseudonymed.dokument.ocr import OCRWort, seitentext_aus_woerter
from pseudonymed.dokument.textebene import Seitentext, lese_seite


def _zeile(text: str, x: float, y: float, h: float = 10.0, zeichen: float = 5.0,
           luecke: float = 3.0) -> list[OCRWort]:
    """Words of one line with boxes: ``zeichen`` points per character, ``luecke`` between."""
    woerter = []
    for w in text.split(" "):
        if not w:
            continue
        b = len(w) * zeichen
        woerter.append(OCRWort(w, x, y, x + b, y + h))
        x += b + luecke
    return woerter


VOLL1 = "Die Patientin berichtet über seit drei Wochen bestehende Kopfschmerzen und einen Blut-"
VOLL2 = "druck von 160 zu 95 mmHg, dazu Schwindel beim Aufstehen, ohne Sturz, ohne Doppelbilder."
VOLL3 = "Die Beschwerden begannen nach einer fieberhaften Infektion und haben seither zugenommen,"
KURZ = "weshalb sie sich heute vorstellt."


def _seite():
    woerter = _zeile("Arztbrief Neurologie", 60, 60, h=18, zeichen=9)
    woerter += _zeile(VOLL1, 60, 100) + _zeile(VOLL2, 60, 114) + _zeile(VOLL3, 60, 128)
    woerter += _zeile(KURZ, 60, 142)
    woerter += _zeile("Diagnosen:", 60, 170)
    woerter += _zeile("- Kopfschmerz seit drei Wochen", 60, 190)
    woerter += _zeile("- Schwindel beim Aufstehen", 60, 204)
    woerter += _zeile("Medikament", 60, 240) + _zeile("Dosis", 260, 240)
    woerter += _zeile("morgens", 360, 240)
    woerter += _zeile("Ramipril", 60, 254) + _zeile("5 mg", 260, 254) + _zeile("1", 360, 254)
    woerter += _zeile("Amlodipin", 60, 268) + _zeile("5 mg", 260, 268) + _zeile("0", 360, 268)
    return seitentext_aus_woerter(woerter, 0, 595, 842)


def test_tabs_fuer_zellgrenzen() -> None:
    s = _seite()
    zeilen = s.text.splitlines()
    assert zeilen[-3] == "Medikament\tDosis\tmorgens" and zeilen[-1] == "Amlodipin\t5 mg\t0"
    assert "\t" not in zeilen[1]  # ordinary word gaps stay spaces


def test_gliederung_als_markdown() -> None:
    s = _seite()
    diagnosen = s.text.splitlines().index("Diagnosen:")
    g = gl.gliedere(s, fett={diagnosen})
    md = g.rendere(s.text)
    assert md.startswith("### Arztbrief Neurologie\n\n")
    assert "Kopfschmerzen und einen Blutdruck von 160" in md  # hyphenation undone
    assert "ohne Doppelbilder. Die Beschwerden" in md  # full lines joined with a space
    assert "zugenommen, weshalb sie sich heute vorstellt." in md  # short last line joined
    assert "\n\n#### Diagnosen:\n\n" in md  # short bold line on its own: a heading
    assert "- Kopfschmerz seit drei Wochen\n- Schwindel beim Aufstehen\n\n" in md
    assert "| Medikament | Dosis | morgens |\n| --- | --- | --- |\n| Ramipril | 5 mg | 1 |" in md
    assert g.tabellen == 1 and g.ueberschriften == 2
    # the same structure over the pseudonymised text (same lines, other words)
    anonym = s.text.replace("Die Patientin", "Patient K7Q4MX")
    md2 = g.rendere(anonym)
    assert md2.startswith("### Arztbrief Neurologie") and "Patient K7Q4MX berichtet" in md2
    assert "| Ramipril | 5 mg | 1 |" in md2
    assert g.rendere("ganz anderer Text") == "ganz anderer Text"  # lines do not match: as is


def test_absatz_ohne_fett_und_zeilen() -> None:
    """Short lines stay lines of one block; a colon line before a long one opens a block."""
    woerter = _zeile("Konzentration: gut.", 60, 100) + _zeile("Haltung: aufrecht.", 60, 114)
    woerter += _zeile("Gangbild: sicher.", 60, 128) + _zeile("Beurteilung:", 60, 150)
    woerter += _zeile(VOLL3, 60, 164) + _zeile(KURZ, 60, 178)
    woerter += _zeile(VOLL2, 60, 192) + _zeile("Medikation: Ramipril 5 mg morgens.", 60, 206)
    s = seitentext_aus_woerter(woerter, 0, 595, 842)
    md = gl.gliedere(s).rendere(s.text)
    assert "Konzentration: gut.\nHaltung: aufrecht.\nGangbild: sicher.\n\n" in md
    assert "Beurteilung:\n\nDie Beschwerden" in md and "zugenommen, weshalb" in md
    # a labelled line opens a block even after a line that ran to the margin
    assert "ohne Doppelbilder.\n\nMedikation: Ramipril" in md


def test_fette_zeilen_am_bild() -> None:
    """Ink coverage finds the bold line of a rendered page."""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    texte = ["Sehr geehrte Frau Kollegin,", "wir berichten über die Patientin.",
             "Die Untersuchung ergab einen regelrechten Befund.", "Diagnosen und Befunde",
             "Die Medikation wurde unverändert fortgeführt.", "Mit freundlichen Grüßen",
             "Die Wiedervorstellung ist in drei Monaten geplant."]
    for i, t in enumerate(texte):
        p.insert_text((60, 100 + 20 * i), t, fontsize=11, fontname="hebo" if i == 3 else "helv")
    seite = lese_seite(p, 0)
    zeilen = gl.zeilen_aus(seite)
    import numpy as np

    skala = 100 / 72.0
    pix = p.get_pixmap(matrix=pymupdf.Matrix(skala, skala), colorspace=pymupdf.csGRAY, alpha=False)
    grau = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    fett = gl.fette_zeilen(zeilen, grau, skala)
    assert fett == {next(i for i, z in enumerate(zeilen) if "Diagnosen" in z.text)}
    md = gl.gliedere_seite(p, seite).rendere(seite.text)
    assert "#### Diagnosen und Befunde" in md
    doc.close()


def _z(text: str, x0: float = 60.0, y0: float = 100.0, breite: float = 200.0,
       hoehe: float = 10.0) -> gl.Zeile:
    """A line with a box, for the rules that look at geometry only."""
    return gl.Zeile(0, len(text), text, x0, y0, x0 + breite, y0 + hoehe, hoehe)


MASS = gl._Seitenmass(hoehe=10.0, links=60.0, rechts=500.0, breite=440.0, luecke=4.0)


def test_tabellen_nur_ab_drei_zellen_oder_zwei_zeilen() -> None:
    """A lone line with two cells is a label with its value, not a table."""
    zeilen = [_z("Name:\tWert"), _z("Text"), _z("a\tb\tc"), _z("Text"), _z("a\tb"), _z("c\td")]
    rollen = [gl.Rolle() for _ in zeilen]
    gl._markiere_tabellen(zeilen, rollen)
    assert [r.art for r in rollen] == ["absatz", "absatz", "tabelle", "absatz", "tabelle",
                                       "tabelle"]
    assert [r.tabelle for r in rollen] == [-1, -1, 0, -1, 1, 1]


def test_zeilenart_nach_schrift_und_form() -> None:
    """Large face, bullets, capitals and bold decide the kind of a line."""
    assert gl._zeilenart(_z("Befund", hoehe=17.0), False, MASS) == ("ueberschrift", 3)
    assert gl._zeilenart(_z("Befund", hoehe=14.0), False, MASS) == ("ueberschrift", 4)
    assert gl._zeilenart(_z("- Punkt eins"), False, MASS) == ("liste", 0)
    assert gl._zeilenart(_z("ANAMNESE"), False, MASS) == ("ueberschrift", 4)
    assert gl._zeilenart(_z("Anamnese"), True, MASS) == ("ueberschrift", 4)
    assert gl._zeilenart(_z("Anamnese."), True, MASS) == ("absatz", 0)  # ends a sentence
    assert gl._zeilenart(_z("Anamnese", breite=400.0), True, MASS) == ("absatz", 0)  # long
    assert gl._zeilenart(_z("Anamnese"), False, MASS) == ("absatz", 0)


def test_trenner_der_bloecke() -> None:
    """Tables, headings and list items settle the separator without geometry."""
    tab0, tab1 = gl.Rolle("tabelle", tabelle=0), gl.Rolle("tabelle", tabelle=1)
    kopf, liste, text = gl.Rolle("ueberschrift", 4), gl.Rolle("liste"), gl.Rolle()
    assert gl._trenner_der_bloecke(tab0, tab0) == "zeile"
    assert gl._trenner_der_bloecke(tab1, tab0) == "absatz"
    assert gl._trenner_der_bloecke(text, tab0) == "absatz"
    assert gl._trenner_der_bloecke(kopf, text) == gl._trenner_der_bloecke(text, kopf) == "absatz"
    assert gl._trenner_der_bloecke(liste, liste) == "zeile"
    assert gl._trenner_der_bloecke(liste, text) == "absatz"
    assert gl._trenner_der_bloecke(text, liste) is None  # the geometry decides
    assert gl._trenner_der_bloecke(text, text) is None


def test_absatztrenner_nach_geometrie() -> None:
    """Gap, indent, a full predecessor, a label and a colon line decide the join."""
    voll = _z("Die Beschwerden bestehen seit drei Wochen und", y0=100.0, breite=440.0)
    kurz = _z("seit drei Wochen.", y0=100.0, breite=100.0)
    assert gl._absatztrenner(_z("weiter", y0=130.0), voll, MASS) == "absatz"  # tall gap
    assert gl._absatztrenner(_z("weiter", x0=80.0, y0=114.0), kurz, MASS) == "absatz"  # indent
    assert gl._absatztrenner(_z("weiter", y0=114.0), voll, MASS) == "wort"
    assert gl._absatztrenner(_z("Diagnosen: keine", y0=114.0), voll, MASS) == "absatz"
    assert gl._absatztrenner(_z("weiter", y0=114.0), kurz, MASS) == "zeile"
    doppelpunkt = _z("Beurteilung:", y0=100.0, breite=100.0)
    assert gl._absatztrenner(_z("lange Zeile", y0=114.0, breite=400.0), doppelpunkt,
                             MASS) == "absatz"
    assert gl._absatztrenner(_z("kurz", y0=114.0, breite=100.0), doppelpunkt, MASS) == "zeile"
    getrennt = _z("ein Blut-", y0=100.0, breite=440.0)
    assert gl._absatztrenner(_z("druck", y0=114.0), getrennt, MASS) == "silbe"
    assert gl._absatztrenner(_z("Druck", y0=114.0), getrennt, MASS) == "wort"


def test_fortsetzung_unter_listenpunkt() -> None:
    """A line close under a bullet and indented past it continues the item."""
    punkt = _z("- Kopfschmerz seit", y0=100.0)
    assert gl._haengt_unter_listenpunkt(_z("drei Wochen", x0=70.0, y0=114.0), punkt, MASS)
    assert not gl._haengt_unter_listenpunkt(_z("drei Wochen", x0=60.0, y0=114.0), punkt, MASS)
    assert not gl._haengt_unter_listenpunkt(_z("drei Wochen", x0=70.0, y0=130.0), punkt, MASS)


def test_absatz_fettlaeufe() -> None:
    """Bold runs stay open across a space, close at a newline, and join across a hyphen."""
    a = gl._Absatz()
    a.fuege_an("Eins", True, "absatz")
    a.fuege_an("zwei", True, "wort")
    a.fuege_an("drei", False, "wort")
    assert a.schliesse() == "**Eins zwei** drei" and a.schliesse() == ""
    a.fuege_an("Eins", True, "absatz")
    a.fuege_an("zwei", True, "zeile")
    assert a.schliesse() == "**Eins**\n**zwei**"
    a.fuege_an("Blut-", False, "absatz")
    a.fuege_an("druck", False, "silbe")
    a.fuege_an("hoch", False, "silbe")  # no hyphen before it: joined without a gap
    assert a.schliesse() == "Blutdruckhoch"


def test_zeilen_ohne_boxen() -> None:
    """Lines without character boxes keep the zero box and count as empty."""
    text = "ab\n\ncd\n"
    boxen = [(10.0, 20.0, 15.0, 30.0), None, None, None, None, None]
    seite = Seitentext(0, 595, 842, text=text, boxen=boxen)
    zeilen = gl.zeilen_aus(seite)
    assert [z.text for z in zeilen] == ["ab", "", "cd"]
    assert (zeilen[0].x0, zeilen[0].y0, zeilen[0].x1, zeilen[0].y1) == (10.0, 20.0, 15.0, 30.0)
    assert zeilen[0].hoehe == 10.0 and not zeilen[0].leer
    assert zeilen[1].leer and zeilen[2].leer and zeilen[2].breite == 0.0
    assert gl.gliedere(seite).rendere(text) == "ab\n\ncd\n"


def test_seitenmass_mit_rueckfall() -> None:
    """Without measured heights or usual gaps the measures fall back to defaults."""
    eine = gl._Seitenmass.aus([gl.Zeile(0, 4, "Text", 60.0, 100.0, 260.0, 110.0, 0.0)])
    assert (eine.hoehe, eine.links, eine.rechts, eine.breite, eine.luecke) == (10.0, 60.0, 260.0,
                                                                                200.0, 3.0)
    assert eine.absatzabstand == 8.0
    zwei = gl._Seitenmass.aus([_z("a", y0=100.0), _z("b", y0=114.0, breite=400.0)])
    assert (zwei.hoehe, zwei.breite, zwei.luecke) == (10.0, 400.0, 4.0)
