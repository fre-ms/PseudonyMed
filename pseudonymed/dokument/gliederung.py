"""Layout of a recognised page as Markdown: paragraphs, headings, lists and tables.

OCR hands over lines.  A language model wants the text the way the document meant
it: paragraphs in one piece, headings marked, lists and tables kept apart.  The
geometry of the lines tells the layout: a line that runs to the right margin goes on
in the next one; a gap taller than the usual line spacing ends a paragraph; a line in
a larger face, or a short bold line on its own, is a heading; a bullet starts a list
item; big gaps inside a line (tabs in the page text, see
:func:`~pseudonymed.dokument.ocr.seitentext_aus_woerter`) are table cells.

The roles are computed once from the OCR geometry and applied to the page text later,
raw or with the pseudonyms in place: both have the same lines.  Italics are not
recognised: the engines report no slant, and a stroke-angle guess is not reliable
enough to mark text with.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import groupby, pairwise
from statistics import median
from typing import Any

import pymupdf

from pseudonymed.dokument.textebene import Box, Seitentext

_AUFZAEHLUNG = re.compile(r"^(?:(?P<symbol>[-•·▪◦●○■□*–—>])|(?P<nummer>\d{1,2}[.)])"
                          r"|(?P<buchstabe>[a-zA-Z][.)]))\s+(?=\S)")
# "Diagnosen:", "Medikation:", "Aktuelle Medikation:" at the start of a line open a block of
# their own even after a line that ran to the margin
_ETIKETT = re.compile(r"^[A-ZÄÖÜ][\wäöüß./-]{1,24}(?: [\wäöüß./-]{1,24}){0,2}:\s")
UEBERSCHRIFT_GROSS = 3  # Markdown levels below the page headings of the Markdown file
UEBERSCHRIFT_KLEIN = 4
_TRENNZEICHEN = {"wort": " ", "zeile": "\n"}  # what a separator puts between two pieces


@dataclass
class Zeile:
    """One OCR line: its span in the page text and its box in page points."""

    start: int
    ende: int
    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    hoehe: float = 0.0

    @property
    def leer(self) -> bool:
        """True for a line without visible text or without a box of its own."""
        return not self.text.strip() or self.x1 <= self.x0

    @property
    def breite(self) -> float:
        """Width of the line's box in page points."""
        return self.x1 - self.x0


@dataclass
class Rolle:
    """What a line is and how it attaches to the line before it."""

    art: str = "absatz"  # absatz, ueberschrift, liste, tabelle
    ebene: int = 0  # heading level
    fett: bool = False
    trenner: str = "absatz"  # absatz (blank line), zeile (newline), wort (space), silbe (join)
    tabelle: int = -1


@dataclass
class Gliederung:
    """The roles of the lines of one page; :meth:`rendere` turns a page text into
    Markdown.  Built by :func:`gliedere`."""

    rollen: list[Rolle] = field(default_factory=list)

    @property
    def tabellen(self) -> int:
        """Number of tables on the page."""
        return len({r.tabelle for r in self.rollen if r.art == "tabelle"})

    @property
    def ueberschriften(self) -> int:
        """Number of headings on the page."""
        return sum(1 for r in self.rollen if r.art == "ueberschrift")

    def rendere(self, text: str) -> str:
        """Markdown of ``text``, which must have the lines the roles were computed for
        (the raw OCR text or that text with pseudonyms).  Anything else comes back as
        it is."""
        segmente = self._segmente(text)
        if segmente is None:
            return text
        return "\n\n".join(b for b in _bloecke(self.rollen, segmente) if b.strip()) + "\n"

    def _segmente(self, text: str) -> list[str] | None:
        """The lines of ``text`` when they match the roles one to one (a trailing
        newline is tolerated), else None."""
        segmente = text.split("\n")
        if len(segmente) == len(self.rollen) + 1 and segmente[-1] == "":
            segmente.pop()
        return segmente if len(segmente) == len(self.rollen) else None


# -- blocks ----------------------------------------------------------------------------------------
_Stueck = tuple[Rolle, str]  # a role with the raw text of its line


def _blockschluessel(idx: int, stueck: _Stueck) -> tuple[str, int]:
    """Consecutive lines with the same key form one Markdown block: the rows of a table
    by its number, the lines of a list, a heading by itself, and runs of ordinary
    lines, which the separators then split into paragraphs."""
    rolle = stueck[0]
    if rolle.art == "tabelle":
        return "tabelle", rolle.tabelle
    if rolle.art == "ueberschrift":
        return "ueberschrift", idx
    if rolle.art == "liste":
        return "liste", 0
    return "absatz", 0


def _bloecke(rollen: list[Rolle], segmente: list[str]) -> Iterator[str]:
    """The Markdown blocks of a page in order; some of them may be empty."""
    absatz = _Absatz()
    nummeriert = enumerate(zip(rollen, segmente, strict=True))
    for (art, _), lauf in groupby(nummeriert, key=lambda p: _blockschluessel(*p)):
        stuecke = [stueck for _i, stueck in lauf]
        if art == "absatz":
            yield from _absatzbloecke(stuecke, absatz)
            continue
        yield absatz.schliesse()
        yield _BLOCKBAU[art](stuecke)
    yield absatz.schliesse()


def _absatzbloecke(stuecke: list[_Stueck], absatz: _Absatz) -> Iterator[str]:
    """Feeds ordinary lines into ``absatz`` and yields a paragraph wherever a line opens
    a new one; empty lines are skipped.  The last paragraph stays open in ``absatz``."""
    for rolle, roh in stuecke:
        text = roh.strip()
        if not text:
            continue
        if rolle.trenner == "absatz":
            yield absatz.schliesse()
        absatz.fuege_an(text, rolle.fett, rolle.trenner)


def _tabellenblock(stuecke: list[_Stueck]) -> str:
    """Markdown table of the rows; the tabs of the page text are the cell boundaries."""
    return _tabelle([[zelle.strip() for zelle in roh.split("\t")] for _rolle, roh in stuecke])


def _ueberschriftsblock(stuecke: list[_Stueck]) -> str:
    """Markdown heading of the one line; empty for a line without text."""
    rolle, roh = stuecke[0]
    text = roh.strip()
    return "#" * rolle.ebene + " " + text if text else ""


def _listenblock(stuecke: list[_Stueck]) -> str:
    """Markdown list of the lines; a line joined by word or syllable continues the item
    before it."""
    punkte: list[str] = []
    for rolle, roh in stuecke:
        text = roh.strip()
        if punkte and rolle.trenner in ("wort", "silbe"):
            punkte[-1] = _anhaengen(punkte[-1], text, rolle.trenner)
        elif text:
            punkte.append(_listenpunkt(text))
    return "\n".join(punkte)


_BLOCKBAU: dict[str, Callable[[list[_Stueck]], str]] = {
    "tabelle": _tabellenblock, "ueberschrift": _ueberschriftsblock, "liste": _listenblock}


class _Absatz:
    """Collects the pieces of one paragraph: joined with spaces, newlines or across a
    hyphen; bold runs are wrapped in ``**``."""

    def __init__(self) -> None:
        """Starts with no pieces."""
        self.teile: list[tuple[str, str, bool]] = []  # (trenner, text, fett)

    def fuege_an(self, text: str, fett: bool, trenner: str) -> None:
        """Adds a piece; the first piece of a paragraph needs no separator."""
        if not self.teile:
            trenner = "absatz"
        self.teile.append((trenner, text, fett))

    def schliesse(self) -> str:
        """The paragraph as Markdown; the collector is empty afterwards.  A bold run
        stays open across a space when the next piece is bold too, any other join
        closes it."""
        aus = ""
        offen = False  # inside a bold run
        for trenner, text, fett in self.teile:
            aus, fuge = _fuge(aus, trenner)
            weiter_fett = offen and fett and fuge == " "
            if offen and not weiter_fett:
                aus += "**"
            aus += fuge
            if fett and not weiter_fett:
                aus += "**"
            aus += text
            offen = fett
        self.teile = []
        return aus + "**" if offen else aus


def _fuge(bisher: str, trenner: str) -> tuple[str, str]:
    """``bisher`` made ready for the next piece, and the string to put between them: a
    syllable join drops the hyphen at the end, a word join puts a space, a line join a
    newline, a paragraph start nothing."""
    if trenner == "silbe" and bisher.rstrip().endswith("-"):
        return bisher.rstrip()[:-1], ""
    return bisher, _TRENNZEICHEN.get(trenner, "")


def _anhaengen(bisher: str, text: str, trenner: str) -> str:
    """``text`` joined to a list item: across the hyphen for a syllable join, else with
    a space."""
    if trenner == "silbe" and bisher.rstrip().endswith("-"):
        return bisher.rstrip()[:-1] + text
    return bisher + " " + text


def _listenpunkt(text: str) -> str:
    """The line as a Markdown list item: numbers keep their number, letters stay as
    text of a bullet item, symbols become the bullet."""
    m = _AUFZAEHLUNG.match(text)
    if m is None:
        return "- " + text
    rest = text[m.end():]
    if m.group("nummer"):
        return m.group("nummer").rstrip(")") .rstrip(".") + ". " + rest
    if m.group("buchstabe"):
        return "- " + m.group("buchstabe") + " " + rest
    return "- " + rest


def _tabelle(reihen: list[list[str]]) -> str:
    """Markdown table of the rows, the first one as header; short rows are padded."""
    spalten = max(len(r) for r in reihen)
    reihen = [r + [""] * (spalten - len(r)) for r in reihen]
    zelle = lambda c: c.replace("|", "\\|")  # noqa: E731
    zeilen = ["| " + " | ".join(zelle(c) for c in reihen[0]) + " |",
              "|" + " --- |" * spalten]
    zeilen += ["| " + " | ".join(zelle(c) for c in r) + " |" for r in reihen[1:]]
    return "\n".join(zeilen)


# -- geometry --------------------------------------------------------------------------------------
def zeilen_aus(seite: Seitentext) -> list[Zeile]:
    """The lines of a page text with their boxes (from the character boxes)."""
    segmente = seite.text.split("\n")
    if seite.text.endswith("\n"):
        segmente.pop()
    zeilen: list[Zeile] = []
    start = 0
    for text in segmente:
        zeilen.append(_zeile_mit_box(seite, start, text))
        start += len(text) + 1
    return zeilen


def _zeile_mit_box(seite: Seitentext, start: int, text: str) -> Zeile:
    """The line ``text`` at ``start`` of the page text with the box around its
    characters; a line without boxes keeps the zero box."""
    ende = start + len(text)
    boxen = [(box, zeichen) for box, zeichen
             in zip(seite.boxen[start:ende], text, strict=False) if box is not None]
    if not boxen:
        return Zeile(start, ende, text)
    return Zeile(start, ende, text,
                 x0=min(b[0] for b, _z in boxen), y0=min(b[1] for b, _z in boxen),
                 x1=max(b[2] for b, _z in boxen), y1=max(b[3] for b, _z in boxen),
                 hoehe=_zeilenhoehe(boxen))


def _zeilenhoehe(boxen: list[tuple[Box, str]]) -> float:
    """Median height of the letter and digit boxes, or of all boxes when the line has
    none: punctuation sits lower than the face."""
    hoehen = [b[3] - b[1] for b, z in boxen if z.isalnum()] or [b[3] - b[1] for b, _z in boxen]
    return median(hoehen)


def fette_zeilen(zeilen: list[Zeile], grau: Any, skala: float) -> set[int]:
    """Indices of lines whose ink coverage stands well above the page's median: bold
    face.  ``grau`` is a 2-D uint8 array at ``skala`` pixels per point."""
    import numpy as np

    bild = np.asarray(grau)
    dichten = {idx: d for idx, z in enumerate(zeilen)
               if (d := _tintendichte(z, bild, skala)) is not None}
    if len(dichten) < 4:
        return set()
    mitte = median(dichten.values())
    if mitte <= 0:
        return set()
    return {i for i, d in dichten.items() if _deutlich_dunkler(d, mitte)}


def _tintendichte(z: Zeile, bild: Any, skala: float) -> float | None:
    """Share of dark pixels in the line's box on the grey rendering, or None for a line
    with fewer than three letters or digits or a box too small to measure."""
    if z.leer or _alnum(z.text) < 3:
        return None
    h, w = bild.shape[:2]
    x0, y0 = max(0, int(z.x0 * skala)), max(0, int(z.y0 * skala))
    x1, y1 = min(w, int(z.x1 * skala) + 1), min(h, int(z.y1 * skala) + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    band = bild[y0:y1, x0:x1]
    return float((band < 128).mean())


def _deutlich_dunkler(dichte: float, mitte: float) -> bool:
    """True when an ink coverage stands well above the page's median both in ratio and
    in absolute terms, so that a faint page does not turn every line bold."""
    return dichte >= 1.45 * mitte and dichte - mitte >= 0.06


# -- roles -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Seitenmass:
    """Typical measures of the printed lines of a page, the yardstick of every rule:
    median line height, left and right text edge (the 10th and 90th percentile of the
    line edges, so that a stray word does not move them), their distance and the median
    gap between consecutive lines."""

    hoehe: float
    links: float
    rechts: float
    breite: float
    luecke: float

    @classmethod
    def aus(cls, gedruckt: list[Zeile]) -> _Seitenmass:
        """Measures of the printed lines of a page (at least one)."""
        hoehe = _typische_hoehe(gedruckt)
        links, rechts = _textkanten(gedruckt)
        return cls(hoehe, links, rechts, max(rechts - links, hoehe),
                   _zeilenabstand(gedruckt, hoehe))

    @property
    def absatzabstand(self) -> float:
        """Gap above which a line starts a paragraph of its own."""
        return self.luecke + 0.5 * self.hoehe


def _typische_hoehe(gedruckt: list[Zeile]) -> float:
    """Median height of the lines with a measured height; 10 points when none has one."""
    hoehen = [z.hoehe for z in gedruckt if z.hoehe > 0]
    return median(hoehen) if hoehen else 10.0


def _textkanten(gedruckt: list[Zeile]) -> tuple[float, float]:
    """Left and right text edge: the 10th percentile of the line starts and the 90th of
    the line ends, so that a stray word does not move them."""
    xs0 = sorted(z.x0 for z in gedruckt)
    xs1 = sorted(z.x1 for z in gedruckt)
    return xs0[len(xs0) // 10], xs1[(len(xs1) * 9) // 10]


def _zeilenabstand(gedruckt: list[Zeile], hoehe: float) -> float:
    """Median gap between consecutive lines, leaving out overlaps and paragraph gaps;
    three tenths of the line height when no gap is usual."""
    luecken = [b.y0 - a.y1 for a, b in pairwise(gedruckt)]
    normal = [g for g in luecken if -0.3 * hoehe < g < 1.0 * hoehe]
    return median(normal) if normal else 0.3 * hoehe


def gliedere(seite: Seitentext, fett: set[int] | None = None) -> Gliederung:
    """Roles of the lines of an OCR page from their geometry (see the module notes):
    tables first, then headings and list items, then how each line attaches to the
    one before it."""
    zeilen = zeilen_aus(seite)
    rollen = [Rolle() for _ in zeilen]
    gedruckt = [z for z in zeilen if not z.leer]
    if not gedruckt:
        return Gliederung(rollen)
    mass = _Seitenmass.aus(gedruckt)
    _markiere_tabellen(zeilen, rollen)
    _markiere_zeilenarten(zeilen, rollen, fett or set(), mass)
    _verbinde_zeilen(zeilen, rollen, mass)
    return Gliederung(rollen)


def _markiere_tabellen(zeilen: list[Zeile], rollen: list[Rolle]) -> None:
    """Numbers the tables of the page: every run of lines with cell gaps, and a single
    such line only when it has at least three cells, because two cells alone are as
    likely a label with its value."""
    tabellen = (lauf for lauf in _zelllaeufe(zeilen) if _ist_tabelle(zeilen, *lauf))
    for nummer, (start, ende) in enumerate(tabellen):
        for rolle in rollen[start:ende]:
            rolle.art, rolle.tabelle = "tabelle", nummer


def _zelllaeufe(zeilen: list[Zeile]) -> Iterator[tuple[int, int]]:
    """Index ranges ``(start, ende)`` of consecutive lines that contain a cell gap."""
    for mit_zellen, lauf in groupby(range(len(zeilen)), key=lambda i: "\t" in zeilen[i].text):
        if mit_zellen:
            indizes = list(lauf)
            yield indizes[0], indizes[-1] + 1


def _ist_tabelle(zeilen: list[Zeile], start: int, ende: int) -> bool:
    """A run of at least two lines with cell gaps, or one line with at least three cells."""
    return ende - start >= 2 or zeilen[start].text.count("\t") >= 2


def _markiere_zeilenarten(zeilen: list[Zeile], rollen: list[Rolle], fett: set[int],
                          mass: _Seitenmass) -> None:
    """Marks every printed line outside the tables as heading, list item or ordinary
    line, and notes bold face on all but headings."""
    for idx, (z, rolle) in enumerate(zip(zeilen, rollen, strict=True)):
        if z.leer or rolle.art == "tabelle":
            continue
        rolle.art, rolle.ebene = _zeilenart(z, idx in fett, mass)
        rolle.fett = idx in fett and rolle.art != "ueberschrift"


def _zeilenart(z: Zeile, fett: bool, mass: _Seitenmass) -> tuple[str, int]:
    """Kind and heading level of a printed line: a larger face is a heading (two sizes
    give two levels), a bullet or number opens a list item, and a short line that is
    bold or in capitals and does not end a sentence is a small heading."""
    text = z.text.strip()
    if _alnum(text) >= 2 and z.hoehe >= 1.3 * mass.hoehe:
        ebene = UEBERSCHRIFT_GROSS if z.hoehe >= 1.6 * mass.hoehe else UEBERSCHRIFT_KLEIN
        return "ueberschrift", ebene
    if _AUFZAEHLUNG.match(text):
        return "liste", 0
    if _kurze_hervorgehobene_zeile(z, fett, mass):
        return "ueberschrift", UEBERSCHRIFT_KLEIN
    return "absatz", 0


def _kurze_hervorgehobene_zeile(z: Zeile, fett: bool, mass: _Seitenmass) -> bool:
    """A short line in bold or in capitals that does not end with a full stop or comma:
    a heading set in the running face."""
    text = z.text.strip()
    return (_ist_kurz(z, mass) and _alnum(text) >= 3 and (fett or _ist_versal(text))
            and not text.endswith((".", ",")))


def _alnum(text: str) -> int:
    """Number of letters and digits in ``text``."""
    return sum(c.isalnum() for c in text)


def _ist_versal(text: str) -> bool:
    """True for at least four letters, all of them capitals."""
    buchstaben = [c for c in text if c.isalpha()]
    return len(buchstaben) >= 4 and all(c.isupper() for c in buchstaben)


def _ist_kurz(z: Zeile, mass: _Seitenmass) -> bool:
    """True for a line narrower than six tenths of the text width."""
    return z.breite < 0.6 * mass.breite


def _verbinde_zeilen(zeilen: list[Zeile], rollen: list[Rolle], mass: _Seitenmass) -> None:
    """Sets how every printed line attaches to the printed line before it.  The first
    printed line keeps the default: it opens a paragraph."""
    gedruckt = [(z, r) for z, r in zip(zeilen, rollen, strict=True) if not z.leer]
    for (vor, vor_rolle), (z, rolle) in pairwise(gedruckt):
        _verbinde_mit_vorzeile(z, rolle, vor, vor_rolle, mass)


def _verbinde_mit_vorzeile(z: Zeile, rolle: Rolle, vor: Zeile, vor_rolle: Rolle,
                           mass: _Seitenmass) -> None:
    """Decides the separator of ``z`` after ``vor``.  Tables, headings and list items
    settle it by themselves; an ordinary line hanging under a list item becomes part of
    that item; between two ordinary lines the geometry decides."""
    fest = _trenner_der_bloecke(rolle, vor_rolle)
    if fest is not None:
        rolle.trenner = fest
    elif vor_rolle.art != "liste":
        rolle.trenner = _absatztrenner(z, vor, mass)
    elif _haengt_unter_listenpunkt(z, vor, mass):
        rolle.art, rolle.trenner = "liste", _wortfuge(vor, z)
    else:
        rolle.trenner = "absatz"


def _trenner_der_bloecke(rolle: Rolle, vor_rolle: Rolle) -> str | None:
    """Separator settled by the block kinds alone: the rows of one table follow each
    other on new lines and so do list items; any other table boundary, a heading on
    either side and a list item after a non-list line open a block.  None when both
    lines are ordinary or the line hangs under a list item."""
    if rolle.art == "tabelle" or vor_rolle.art == "tabelle":
        gleiche = rolle.art == "tabelle" and rolle.tabelle == vor_rolle.tabelle
        return "zeile" if gleiche else "absatz"
    if rolle.art == "ueberschrift" or vor_rolle.art == "ueberschrift":
        return "absatz"
    if rolle.art == "liste":
        return "zeile" if vor_rolle.art == "liste" else "absatz"
    return None


def _haengt_unter_listenpunkt(z: Zeile, vor: Zeile, mass: _Seitenmass) -> bool:
    """True for a line close under a list item and indented past its bullet: the item
    goes on in it."""
    return _abstand(z, vor) <= mass.absatzabstand and z.x0 >= vor.x0 + 0.8 * mass.hoehe


def _absatztrenner(z: Zeile, vor: Zeile, mass: _Seitenmass) -> str:
    """How an ordinary line attaches to the ordinary line before it: a tall gap or a
    fresh indent opens a paragraph; a predecessor that ran to the margin goes on in
    this line, unless the line opens a labelled block; a colon line before a long line
    opens a block; otherwise the lines stay lines of one block."""
    if _abstand(z, vor) > mass.absatzabstand or _neu_eingerueckt(z, vor, mass):
        return "absatz"
    if _bis_zum_rand(vor, mass):
        return "absatz" if _oeffnet_etikett(z, vor) else _wortfuge(vor, z)
    if vor.text.rstrip().endswith(":") and not _ist_kurz(z, mass):
        return "absatz"
    return "zeile"


def _abstand(z: Zeile, vor: Zeile) -> float:
    """Vertical gap between the bottom of ``vor`` and the top of ``z``."""
    return z.y0 - vor.y1


def _neu_eingerueckt(z: Zeile, vor: Zeile, mass: _Seitenmass) -> bool:
    """True when the line is indented well past the left edge while the line before it
    was not: a paragraph that starts with an indent."""
    return (z.x0 - mass.links > 1.2 * mass.hoehe
            and vor.x0 - mass.links <= 0.6 * mass.hoehe)


def _bis_zum_rand(vor: Zeile, mass: _Seitenmass) -> bool:
    """True when the line reaches nearly the right text edge: it goes on in the next."""
    return vor.x1 >= mass.rechts - 0.12 * mass.breite


def _oeffnet_etikett(z: Zeile, vor: Zeile) -> bool:
    """True when the line starts with a label such as "Diagnosen:" and the line before
    it does not end in a hyphen: the label opens a block of its own."""
    return _ETIKETT.match(z.text.strip()) is not None and not vor.text.rstrip().endswith("-")


def _wortfuge(vor: Zeile, z: Zeile) -> str:
    """How a continuation line joins the line before it: across the hyphen when that
    line ends in one and this line starts in lower case, otherwise with a space."""
    silbe = vor.text.rstrip().endswith("-") and z.text.strip()[:1].islower()
    return "silbe" if silbe else "wort"


def gliedere_seite(page: pymupdf.Page, ocr: Seitentext, dpi: int = 100) -> Gliederung:
    """Roles of an OCR page including bold detection on a grey rendering of the page
    (needs NumPy; without it there is no bold)."""
    zeilen = zeilen_aus(ocr)
    fett: set[int] = set()
    try:
        import numpy as np

        skala = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(skala, skala), colorspace=pymupdf.csGRAY,
                              alpha=False)
        grau = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        fett = fette_zeilen(zeilen, grau, skala)
    except ImportError:
        pass
    return gliedere(ocr, fett)
