"""One page as it was read, and the text of pages with their hits replaced."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pseudonymed.dokument import gliederung as gliedmodul
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.dokument import textdatei as textmodul
from pseudonymed.dokument.textebene import Box, Seitentext
from pseudonymed.kern.modell import Fundstelle

_SATZZEICHEN_HINTEN = ".,;:!?)]}"
_KLAMMERN_VORN = "([{„\"'"


@dataclass
class Seite:
    """A page of the document: its text layer, its OCR reading and what was found."""

    nummer: int
    text: Seitentext
    ocr: Seitentext | None = None
    fundstellen: list[Fundstelle] = field(default_factory=list)
    fachrects: list[Box] = field(default_factory=list)
    fachspannen: list[tuple[int, int]] = field(default_factory=list)
    fachanzahl: int = 0
    korrektur: str | None = None  # the OCR text as the user corrected it
    korrektur_treffer: list[Fundstelle] = field(default_factory=list)
    ocr_dpi: int = ocrmodul.OCR_DPI  # resolution the page was (and is re-)read at
    ocr_max_pixel: int = ocrmodul.MAX_PIXEL
    ocr_filter: object = None  # letter-aware finish applied before the engine
    gliederung: gliedmodul.Gliederung | None = None  # layout roles of the OCR lines

    @property
    def bildseite(self) -> bool:
        """True for a page whose only text is the OCR reading."""
        return self.ocr is not None and not self.text.hat_textebene

    @property
    def textbasis(self) -> Seitentext:
        """The text the review clicks into: the OCR reading on image pages, else the
        text layer."""
        return self.ocr if self.ocr is not None and self.bildseite else self.text

    def ocr_lesen(self, page: object, backend: ocrmodul.OCRBackend) -> Seitentext:
        """Read the page with the engine at this page's resolution and finish."""
        return ocrmodul.erkenne_seite(page, backend, self.nummer, self.ocr_dpi,
                                      max_pixel=self.ocr_max_pixel, filter_=self.ocr_filter)

    def treffer_auf(self, basis: str) -> list[Fundstelle]:
        """The hits that belong to ``basis`` ("text" or "ocr"), manual boxes included
        where they were drawn on that basis; empty spans left out."""
        mit_manuell = (basis == "text") == self.text.hat_textebene
        quellen = (basis, "manuell") if mit_manuell else (basis,)
        return [f for f in self.fundstellen if f.quelle in quellen and f.ende > f.start]


def lesetext(s: Seite, gliedern: bool) -> str:
    """The OCR text as it is shown and corrected: the user's correction, else the raw
    reading, structured as Markdown when asked for."""
    if s.korrektur is not None:
        return s.korrektur
    if s.ocr is None:
        return ""
    if gliedern and s.gliederung is not None:
        return s.gliederung.rendere(s.ocr.text)
    return s.ocr.text


def unveraendert(s: Seite, text: str) -> bool:
    """True when ``text`` is the OCR reading itself, raw or structured."""
    assert s.ocr is not None
    kandidaten = [s.ocr.text.strip()]
    if s.gliederung is not None:
        kandidaten.append(s.gliederung.rendere(s.ocr.text).strip())
    return text.strip() in kandidaten


def ohne_rand(text: str, start: int, ende: int) -> tuple[int, int]:
    """``[start, ende)`` clipped to the text and stripped of surrounding whitespace."""
    start, ende = max(0, start), min(ende, len(text))
    while start < ende and text[start].isspace():
        start += 1
    while ende > start and text[ende - 1].isspace():
        ende -= 1
    if ende <= start:
        raise ValueError("Leere Auswahl.")
    return start, ende


def _zeichen_unter(basis: Seitentext, x: float, y: float, toleranz: float) -> int | None:
    """Offset of the first character whose box, widened by ``toleranz``, holds (x, y)."""
    for i, box in enumerate(basis.boxen):
        if box is None:
            continue
        if (box[0] - toleranz <= x <= box[2] + toleranz
                and box[1] - toleranz <= y <= box[3] + toleranz):
            return i
    return None


def _wortgrenzen(text: str, i: int) -> tuple[int, int]:
    """The run of non-whitespace around offset ``i``, trailing punctuation and
    leading brackets left out."""
    a, b = i, i + 1
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    while b < len(text) and not text[b].isspace():
        b += 1
    while b - 1 > a and text[b - 1] in _SATZZEICHEN_HINTEN:
        b -= 1
    while a < b - 1 and text[a] in _KLAMMERN_VORN:
        a += 1
    return a, b


def wort_bei(basis: Seitentext, x: float, y: float,
             toleranz: float = 2.0) -> tuple[int, int] | None:
    """Offsets of the word whose character box lies under (x, y), widened by ``toleranz``
    points; None without one.  A word runs between whitespace, trailing punctuation left
    out."""
    getroffen = _zeichen_unter(basis, x, y, toleranz)
    if getroffen is None or basis.text[getroffen].isspace():
        return None
    return _wortgrenzen(basis.text, getroffen)


def ueberlappen(a: Box, b: Box) -> bool:
    """Same line and overlapping: glyph boxes of neighbouring lines touch, so the boxes have
    to share at least half the lower height."""
    hoehe = min(a[3] - a[1], b[3] - b[1])
    return (a[0] < b[2] and b[0] < a[2]
            and min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * hoehe)


def markdown_aus_text(text: str) -> str:
    """The reading-order text as Markdown: page markers become headings."""
    zeilen = ["# Pseudonymisierter Text", ""]
    for zeile in text.splitlines():
        if zeile.startswith("=== Seite ") and zeile.endswith(" ==="):
            zeilen.append("## " + zeile.strip("= ").strip())
        elif zeile.strip() == "--- Bildtext (OCR, ungeprüft) ---":
            zeilen.append("## Bildtext (OCR, ungeprüft)")
        else:
            zeilen.append(zeile)
    return "\n".join(zeilen).rstrip() + "\n"


def _pseudonymtext(text: str, fundstellen: list[Fundstelle]) -> str:
    """``text`` with the hits replaced, blank runs collapsed."""
    return re.sub(r"\n{3,}", "\n\n", textmodul.ersetze(text, fundstellen)).strip()


def _ocr_text_pseudonym(s: Seite, gliedern: bool = True) -> str:
    """The OCR text of a page with pseudonyms: the user's correction when there is one,
    else the reading with the hits replaced, structured as Markdown when asked for
    (the structure is applied after the replacement: both texts share their lines)."""
    if s.korrektur is not None:
        return _pseudonymtext(s.korrektur, s.korrektur_treffer)
    assert s.ocr is not None
    roh = textmodul.ersetze(s.ocr.text, s.treffer_auf("ocr"))
    if gliedern and s.gliederung is not None:
        roh = s.gliederung.rendere(roh)
    return re.sub(r"\n{3,}", "\n\n", roh).strip()


def _block(s: Seite, text: str) -> str:
    """One page's text under its page marker."""
    return f"=== Seite {s.nummer + 1} ===\n\n{text}"


def text_mit_pseudonymen(seiten: list[Seite], gliedern: bool = True) -> str:
    """Reading-order text with pseudonyms: text layer first, OCR text as its own block."""
    bloecke = [_block(s, _pseudonymtext(s.text.text, s.treffer_auf("text"))) for s in seiten]
    teile = "\n\n".join(bloecke) + "\n"
    ocr_seiten = [s for s in seiten if s.ocr is not None]
    if ocr_seiten:
        ocr_text = "\n\n".join(_block(s, _ocr_text_pseudonym(s, gliedern))
                                for s in ocr_seiten) + "\n"
        teile = teile.rstrip("\n") + "\n\n--- Bildtext (OCR, ungeprüft) ---\n\n" + ocr_text
    return teile
