"""Redact hits in a PDF page and write the pseudonym into the gap.

MuPDF removes every character whose glyph box is *touched* by a redaction
rectangle, and glyph boxes of neighbouring lines overlap even at ordinary
line spacing.  The removal band is therefore clipped against the boxes of all
other lines before it is applied.  The pseudonym is then drawn by us on the
original baseline, widened into free space to the right where possible and
shrunk otherwise, so that it stays legible.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import pymupdf

from pseudonymed import WERKZEUG
from pseudonymed.dokument.textebene import Box, Seitentext, lese_seite
from pseudonymed.kern.modell import Fundstelle

ERLAUBTE_METADATEN = {"producer": WERKZEUG, "creator": WERKZEUG}
VERMERK_PRAEFIX = "pseudonymisiert mit PseudonyMed"

Farbe = tuple[float, float, float]


@dataclass
class Seitenergebnis:
    """What happened to one page: number of boxes and whether the remaining
    text kept its position (MuPDF repositions glyphs after a removal; a wrong
    offset would hide or overlap neighbouring characters)."""

    anzahl: int = 0
    lage_intakt: bool = True


SCHWARZ = (0.0, 0.0, 0.0)


def ist_maske(f: Fundstelle) -> bool:
    """A region the user blacked out: no pseudonym is written into it."""
    return f.regel == "maske"


@dataclass
class Schwaerzoptionen:
    """How hits are removed and painted: fill and text colour of the pseudonym box,
    the base font and its smallest size, MuPDF's treatment of images and line art
    under a box, the metadata note, optional rasterising with its resolution, and
    whether scans get an invisible text layer."""

    fuellfarbe: tuple[float, float, float] = (0.88, 0.88, 0.88)
    textfarbe: tuple[float, float, float] = (0.0, 0.0, 0.0)
    schrift: str = "helv"
    min_schrift: float = 4.0
    bilder: int = pymupdf.PDF_REDACT_IMAGE_PIXELS
    grafik: int = pymupdf.PDF_REDACT_LINE_ART_NONE
    vermerk: bool = True
    rastern: bool = False
    dpi: int = 200
    textebene: bool = True  # embed the OCR text invisibly so that scans stay searchable


def _band(rect: pymupdf.Rect, zeile: int, seite: Seitentext) -> pymupdf.Rect:
    """Shrink ``rect`` so that it touches no character of another line."""
    band = pymupdf.Rect(rect)
    mitte = (band.y0 + band.y1) / 2
    for z, box in seite.zeilenboxen.items():
        if z == zeile:
            continue
        andere = pymupdf.Rect(box)
        if not andere.intersects(band) or andere.x1 <= band.x0 or andere.x0 >= band.x1:
            continue
        andere_mitte = (andere.y0 + andere.y1) / 2
        if andere_mitte < mitte:
            band.y0 = max(band.y0, andere.y1 + 0.2)
        else:
            band.y1 = min(band.y1, andere.y0 - 0.2)
    if band.y1 - band.y0 < 1.0:  # pathological overlap: keep the middle sliver
        band.y0, band.y1 = mitte - 0.5, mitte + 0.5
    band.x0 += 0.2
    band.x1 -= 0.2
    return band


def _zeile_von_rect(seite: Seitentext, f: Fundstelle, rect: pymupdf.Rect) -> int:
    """Line of the first character of ``f`` whose box touches ``rect``, or -1."""
    for i in range(f.start, min(f.ende, len(seite.text))):
        box = seite.boxen[i]
        if box is not None and pymupdf.Rect(box).intersects(rect):
            return seite.zeilen[i]
    return -1


def _passende_groesse(text: str, breite: float, start: float, opt: Schwaerzoptionen) -> float:
    """The largest font size at or below ``start`` at which ``text`` fits into ``breite``,
    never below the configured minimum."""
    groesse = max(start, opt.min_schrift)
    while groesse > opt.min_schrift and pymupdf.get_text_length(
        text, fontname=opt.schrift, fontsize=groesse
    ) > breite:
        groesse -= 0.5
    return groesse


class _Einblendung(NamedTuple):
    """A pseudonym to draw: the box it fills, its baseline, font size and text."""

    fuell: pymupdf.Rect
    basis: float
    groesse: float
    pseudonym: str


def _zeichenlagen(seite: Seitentext) -> list[tuple[int, str, Box]]:
    """Offset, character and box of every printed, non-blank character of ``seite``."""
    return [
        (i, c, box) for i, c in enumerate(seite.text)
        if (box := seite.boxen[i]) is not None and not c.isspace()
    ]


def _nach_lage(zeichen: Iterable[tuple[str, Box]]) -> list[tuple[str, Box]]:
    """Characters in reading order: by rounded top edge, then by left edge."""
    return sorted(zeichen, key=lambda e: (round(e[1][1]), e[1][0]))


def _unter_kasten(box: Box, kaesten: list[pymupdf.Rect]) -> bool:
    """True when one of the applied boxes touches the character box."""
    return any(k.intersects(pymupdf.Rect(box)) for k in kaesten)


def _verschoben(a: tuple[str, Box], b: tuple[str, Box], toleranz: float) -> bool:
    """True when the character differs or moved by more than ``toleranz`` points."""
    (c1, b1), (c2, b2) = a, b
    return c1 != c2 or abs(b1[0] - b2[0]) > toleranz or abs(b1[1] - b2[1]) > toleranz


def lage_intakt(
    vorher: Seitentext, nachher: Seitentext, fundstellen: list[Fundstelle], toleranz: float = 0.6,
    kaesten: list[pymupdf.Rect] | None = None,
) -> bool:
    """True when every character outside the hits (and outside the applied boxes
    ``kaesten``) is still where it was."""
    entfernt = {i for f in fundstellen for i in range(f.start, f.ende)}
    kaesten = list(kaesten or [])
    erwartet = _nach_lage(
        (c, box) for i, c, box in _zeichenlagen(vorher)
        if i not in entfernt and not _unter_kasten(box, kaesten)
    )
    ist = _nach_lage((c, box) for _i, c, box in _zeichenlagen(nachher))
    if len(erwartet) != len(ist):
        return False
    return not any(_verschoben(e, i, toleranz) for e, i in zip(erwartet, ist, strict=True))


def _einblendung(page: pymupdf.Page, e: _Einblendung, opt: Schwaerzoptionen) -> None:
    """Write the pseudonym on its baseline, a little inside the left edge of its box."""
    page.insert_text(
        (e.fuell.x0 + 0.75, e.basis),
        e.pseudonym,
        fontsize=e.groesse,
        fontname=opt.schrift,
        color=opt.textfarbe,
        overlay=True,
    )


def _beschriftung(
    seite: Seitentext, f: Fundstelle, rect: pymupdf.Rect, opt: Schwaerzoptionen
) -> _Einblendung:
    """The pseudonym of ``f`` fitted into its first box: on the original baseline for a
    text-layer hit, otherwise near the bottom of the box."""
    basis = seite.basislinie(f.start, f.ende) if f.quelle == "text" else None
    if basis is None:
        basis = rect.y1 - 0.22 * rect.height
    groesse = _passende_groesse(f.pseudonym, rect.width - 1.5, f.schriftgroesse, opt)
    return _Einblendung(rect, basis, groesse, f.pseudonym)


def _uebermale_treffer(
    page: pymupdf.Page, seite: Seitentext, fundstellen: list[Fundstelle], opt: Schwaerzoptionen
) -> None:
    """Paint every box of every hit (black for masks), then write the pseudonyms, so
    that a neighbouring box never covers a text."""
    texte: list[_Einblendung] = []
    for f in fundstellen:
        farbe = SCHWARZ if ist_maske(f) else opt.fuellfarbe
        for k, roh in enumerate(f.rects):
            rect = pymupdf.Rect(roh)
            page.draw_rect(rect, color=None, fill=farbe, overlay=True)
            if k == 0 and not ist_maske(f):
                texte.append(_beschriftung(seite, f, rect, opt))
    for e in texte:
        _einblendung(page, e, opt)


def rastere_seite(
    doc: pymupdf.Document,
    nummer: int,
    original: pymupdf.Document,
    seite: Seitentext,
    fundstellen: list[Fundstelle],
    opt: Schwaerzoptionen,
) -> None:
    """Replace page ``nummer`` of ``doc`` by an image of the *original* page with
    the hits painted over and the pseudonyms written on top.  Used when the
    text-layer redaction disturbed the layout; nothing of the text layer survives."""
    temp = pymupdf.open()
    temp.insert_pdf(original, from_page=nummer, to_page=nummer)
    tp = temp[0]
    _uebermale_treffer(tp, seite, fundstellen, opt)
    pix = tp.get_pixmap(dpi=opt.dpi, alpha=False)
    breite, hoehe = tp.rect.width, tp.rect.height
    temp.close()
    neu = doc.new_page(pno=nummer, width=breite, height=hoehe)
    neu.insert_image(neu.rect, pixmap=pix)
    doc.delete_page(nummer + 1)


OCR_RAND = 1.5  # points of padding around OCR word boxes before pixels are blanked
SNAP_DPI = 200


class _Tinte:
    """Grey rendering of a page for snapping OCR boxes to the gaps between words.

    OCR engines report line or word boxes; the position of a hit inside them is
    an estimate, so the box is widened outward to the nearest gap that is wider
    than the gap between two letters but not wider than a word space (ink-free
    run of at least 20 % of the box height), at most one box height to each side.
    Over-covering a neighbour is accepted; leaving a character of a name or a
    date uncovered is not.
    """

    def __init__(self, page: pymupdf.Page, dpi: int = SNAP_DPI) -> None:
        self.skala = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(self.skala, self.skala), alpha=False,
                              colorspace=pymupdf.csGRAY)
        self.breite, self.hoehe = pix.width, pix.height
        self.pixel = pix.samples  # one byte per pixel, row-major

    def _spalte_leer(self, x: int, y0: int, y1: int) -> bool:
        if x < 0 or x >= self.breite:
            return True
        erlaubt = max(1, (min(self.hoehe, y1) - max(0, y0)) // 12)  # noise specks
        dunkel = 0
        for y in range(max(0, y0), min(self.hoehe, y1)):
            if self.pixel[y * self.breite + x] < 128:
                dunkel += 1
                if dunkel > erlaubt:
                    return False
        return True

    def erweitere(self, rect: pymupdf.Rect) -> pymupdf.Rect:
        """``rect`` widened to the nearest word gap on each side, same height."""
        h = rect.height
        if h <= 0:
            return rect
        y0, y1 = int(rect.y0 * self.skala), int(rect.y1 * self.skala) + 1
        luecke = max(3, int(0.2 * h * self.skala))
        grenze = int(1.0 * h * self.skala)
        x0 = int(rect.x0 * self.skala)
        x1 = int(rect.x1 * self.skala)
        neu_x0 = self._suche(x0, -1, y0, y1, luecke, grenze)
        neu_x1 = self._suche(x1, +1, y0, y1, luecke, grenze)
        return pymupdf.Rect(neu_x0 / self.skala, rect.y0, neu_x1 / self.skala, rect.y1)

    def _suche(self, start: int, richtung: int, y0: int, y1: int, luecke: int, grenze: int) -> int:
        """Walk from ``start`` in ``richtung`` until a run of ``luecke`` empty columns."""
        x = start
        leer_in_folge = 0
        schritte = 0
        while schritte < grenze:
            x += richtung
            schritte += 1
            if self._spalte_leer(x, y0, y1):
                leer_in_folge += 1
                if leer_in_folge >= luecke:
                    return x - richtung * (luecke - 1)
            else:
                leer_in_folge = 0
        return start + richtung * grenze


class _Loeschband(NamedTuple):
    """How one rectangle of a hit is removed: ``flaeche`` is the (possibly padded) box
    that is painted over, ``band`` the rectangle handed to MuPDF for the removal.
    ``auf_bild`` marks a box that blanks pixels rather than a known span of text-layer
    characters: it exempts the characters under it from the layout check and has no
    baseline or free space of its own."""

    flaeche: pymupdf.Rect
    band: pymupdf.Rect
    auf_bild: bool


@dataclass
class _Schwaerzplan:
    """Everything decided for one page before MuPDF is touched: the removal bands with
    their fill colour in hit order, the boxes the user drew (pixels only), the boxes
    whose characters the layout check must skip, the black mask areas and the
    pseudonyms to write."""

    baender: list[tuple[pymupdf.Rect, Farbe]] = field(default_factory=list)
    nur_pixel: list[tuple[pymupdf.Rect, Farbe]] = field(default_factory=list)
    ohne_lagepruefung: list[pymupdf.Rect] = field(default_factory=list)
    flaechen: list[tuple[pymupdf.Rect, Farbe]] = field(default_factory=list)
    einblendungen: list[_Einblendung] = field(default_factory=list)


def _ist_gezeichnet(f: Fundstelle, seite: Seitentext | None) -> bool:
    """True for a box the user drew over text: its rects are the text spans per line,
    followed by the box itself."""
    return f.quelle == "manuell" and f.ende > f.start and seite is not None


def _textband(seite: Seitentext, f: Fundstelle, rect: pymupdf.Rect) -> _Loeschband:
    """The removal band of a text-layer hit: clipped to its own line so that MuPDF
    leaves the neighbouring lines alone, or the box itself when no character of the
    hit touches it."""
    zeile = _zeile_von_rect(seite, f, rect)
    band = _band(rect, zeile, seite) if zeile >= 0 else pymupdf.Rect(rect)
    return _Loeschband(pymupdf.Rect(rect), band, False)


def _bildband(page: pymupdf.Page, rect: pymupdf.Rect, tinte: _Tinte | None) -> _Loeschband:
    """The removal box of a hit on image pixels (OCR, codes): an OCR box is first
    widened to the nearest word gaps, then every box gets a margin, because the
    position of a hit inside an OCR box is an estimate."""
    if tinte is not None:
        rect = tinte.erweitere(rect)
    rand = max(OCR_RAND, 0.15 * rect.height)
    flaeche = pymupdf.Rect(rect.x0 - rand, rect.y0 - OCR_RAND,
                           rect.x1 + rand, rect.y1 + OCR_RAND) & page.rect
    return _Loeschband(pymupdf.Rect(flaeche), pymupdf.Rect(flaeche), True)


def _loeschband(
    page: pymupdf.Page, seite: Seitentext | None, f: Fundstelle, rect: pymupdf.Rect,
    gezeichnet: bool, tinte: _Tinte | None,
) -> _Loeschband:
    """Decide how ``rect`` of ``f`` is removed: by a line-clipped band when the hit
    lives in the text layer, by the box itself for a box drawn on an image page,
    and by a padded box for OCR and code hits."""
    if seite is not None and (f.quelle == "text" or gezeichnet):
        return _textband(seite, f, rect)
    if f.quelle == "manuell":
        return _Loeschband(pymupdf.Rect(rect), pymupdf.Rect(rect) & page.rect, True)
    return _bildband(page, rect, tinte if f.quelle == "ocr" else None)


def _einblendung_fuer(
    seite: Seitentext | None, f: Fundstelle, lb: _Loeschband, opt: Schwaerzoptionen
) -> _Einblendung:
    """Decide where and how large the pseudonym of ``f`` is written: on the original
    baseline for a text-layer hit, widened to the right into free space when the
    pseudonym needs more room than the hit, and shrunk otherwise.  Widens
    ``lb.flaeche`` and ``lb.band`` in place so that removal and fill cover that room."""
    flaeche, band = lb.flaeche, lb.band
    basis = frei = None
    if seite is not None and not lb.auf_bild:
        basis = seite.basislinie(f.start, f.ende)
        frei = seite.freier_raum_rechts(f.ende)
    if basis is None:
        basis = flaeche.y1 - 0.22 * flaeche.height
    if frei is None:
        frei = flaeche.x1
    benoetigt = pymupdf.get_text_length(
        f.pseudonym, fontname=opt.schrift, fontsize=f.schriftgroesse
    ) + 2.0
    x1 = max(flaeche.x1, min(flaeche.x0 + benoetigt, frei - 1.0))
    flaeche.x1 = band.x1 = max(band.x1, x1 - 0.2)
    groesse = _passende_groesse(f.pseudonym, flaeche.width - 1.5, f.schriftgroesse, opt)
    return _Einblendung(flaeche, basis, groesse, f.pseudonym)


def _sammle_treffer(
    page: pymupdf.Page, seite: Seitentext | None, f: Fundstelle, tinte: _Tinte | None,
    opt: Schwaerzoptionen, plan: _Schwaerzplan,
) -> None:
    """Add the boxes of one hit to ``plan``.  A box the user drew over text is
    separated from the text spans in front of it: it only blanks pixels, because a
    drawn box reaches into the glyph boxes of neighbouring lines.  The first text
    span carries the pseudonym; masks get a black area instead."""
    gezeichnet = _ist_gezeichnet(f, seite)
    farbe = SCHWARZ if ist_maske(f) else opt.fuellfarbe
    spannen = list(f.rects)
    if gezeichnet and len(spannen) > 1:
        plan.nur_pixel.append((pymupdf.Rect(spannen.pop()) & page.rect, farbe))
    for k, roh in enumerate(spannen):
        lb = _loeschband(page, seite, f, pymupdf.Rect(roh), gezeichnet, tinte)
        if lb.auf_bild:
            plan.ohne_lagepruefung.append(pymupdf.Rect(lb.band))
        if ist_maske(f):
            plan.flaechen.append((lb.flaeche, SCHWARZ))
        elif k == 0:
            plan.einblendungen.append(_einblendung_fuer(seite, f, lb, opt))
        plan.baender.append((lb.band, farbe))


def _sammle_kaesten(
    page: pymupdf.Page, seite: Seitentext | None, fundstellen: list[Fundstelle],
    opt: Schwaerzoptionen,
) -> _Schwaerzplan:
    """Decide for every rectangle of every hit how it is removed and painted, without
    touching the page yet.  The grey rendering for snapping OCR boxes is taken once,
    and only when an OCR hit needs it."""
    plan = _Schwaerzplan()
    tinte = _Tinte(page) if any(f.quelle == "ocr" for f in fundstellen) else None
    for f in fundstellen:
        _sammle_treffer(page, seite, f, tinte, opt, plan)
    return plan


def _wende_kaesten_an(page: pymupdf.Page, plan: _Schwaerzplan, opt: Schwaerzoptionen) -> int:
    """Hand the boxes to MuPDF in two passes and return their number: first the
    bands that remove text, then the boxes the user drew, which must not touch
    any text."""
    for band, farbe in plan.baender:
        page.add_redact_annot(band, fill=farbe, cross_out=False)
    if plan.baender:
        page.apply_redactions(
            images=opt.bilder, graphics=opt.grafik, text=pymupdf.PDF_REDACT_TEXT_REMOVE
        )
    if plan.nur_pixel:
        for rect, farbe in plan.nur_pixel:
            page.add_redact_annot(rect, fill=farbe, cross_out=False)
        page.apply_redactions(images=opt.bilder, graphics=opt.grafik,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
    return len(plan.baender) + len(plan.nur_pixel)


def _lage_bleibt(
    page: pymupdf.Page, seite: Seitentext | None, fundstellen: list[Fundstelle],
    plan: _Schwaerzplan,
) -> bool:
    """Re-read the page and check that every character outside the hits kept its
    place.  Only a page with a text layer that a hit or a box touched is checked."""
    if seite is None:
        return True
    treffer = [f for f in fundstellen if f.quelle in ("text", "manuell")]
    if not treffer and not plan.ohne_lagepruefung:
        return True
    return lage_intakt(seite, lese_seite(page, seite.nummer), treffer,
                       kaesten=plan.ohne_lagepruefung)


def _zeichne_einblendungen(page: pymupdf.Page, plan: _Schwaerzplan, opt: Schwaerzoptionen) -> None:
    """Paint all boxes first, then all pseudonyms, so that a neighbouring box never
    covers a text."""
    for rect, farbe in plan.nur_pixel + plan.flaechen:
        page.draw_rect(rect, color=None, fill=farbe, overlay=True)
    for e in plan.einblendungen:
        page.draw_rect(e.fuell, color=None, fill=opt.fuellfarbe, overlay=True)
    for e in _ohne_ueberlappung(plan.einblendungen, opt):
        _einblendung(page, e, opt)


def schwaerze_seite(
    page: pymupdf.Page,
    seite: Seitentext | None,
    fundstellen: list[Fundstelle],
    opt: Schwaerzoptionen | None = None,
) -> Seitenergebnis:
    """Apply all hits of one page in two passes.

    Pass one removes text: text-layer hits and the text spans of manual boxes
    use the band clipped to their line (so neighbouring lines are untouched),
    OCR and code hits use their padded box.  Pass two blanks image pixels under
    the boxes the user drew without touching any text, because a drawn box
    reaches into the glyph boxes of neighbouring lines.  Returns the number of
    boxes and whether the remaining text kept its position; the caller
    rasterises the page if not.
    """
    opt = opt or Schwaerzoptionen()
    plan = _sammle_kaesten(page, seite, fundstellen, opt)
    if not plan.baender and not plan.nur_pixel:
        return Seitenergebnis(0, True)
    anzahl = _wende_kaesten_an(page, plan, opt)
    if not _lage_bleibt(page, seite, fundstellen, plan):
        return Seitenergebnis(anzahl, False)
    _zeichne_einblendungen(page, plan, opt)
    return Seitenergebnis(anzahl, True)


def _rechte_grenze(e: _Einblendung, andere: list[_Einblendung]) -> float:
    """Left edge of the nearest box on the same line to the right of ``e``, or its
    own right edge when nothing follows."""
    rechts = e.fuell.x1
    for a in andere:
        if a is e or abs(a.basis - e.basis) > 3:
            continue
        if e.fuell.x0 < a.fuell.x0 < rechts:
            rechts = a.fuell.x0
    return rechts


def _ohne_ueberlappung(
    einblendungen: list[_Einblendung], opt: Schwaerzoptionen
) -> list[_Einblendung]:
    """Shrink a pseudonym to the room before the next box on the same line, so that
    overlapping boxes (frequent on scans) never print one text over another."""
    ergebnis = []
    for e in einblendungen:
        rechts = _rechte_grenze(e, einblendungen)
        groesse = e.groesse
        if rechts < e.fuell.x1:
            groesse = _passende_groesse(e.pseudonym, rechts - e.fuell.x0 - 1.5, groesse, opt)
        ergebnis.append(e._replace(groesse=groesse))
    return ergebnis


def _wortkasten(seite: Seitentext, a: int, b: int) -> pymupdf.Rect | None:
    """The union of the boxes of the word ``seite.text[a:b]``, or None without boxes."""
    kasten = seite.rects(a, b)
    if not kasten:
        return None
    wort = pymupdf.Rect(kasten[0])
    for r in kasten[1:]:
        wort |= r
    return wort


def _im_treffer(a: int, b: int, treffer: list[Fundstelle]) -> bool:
    """True when the span ``a:b`` overlaps the text span of a hit."""
    return any(a < f.ende and b > f.start for f in treffer)


def _unter_treffer(wort: pymupdf.Rect, rects: list[pymupdf.Rect]) -> bool:
    """True when the word box touches the box of a hit."""
    return any(r.intersects(wort) for r in rects)


def _freie_woerter(
    seite: Seitentext, fundstellen: list[Fundstelle]
) -> list[tuple[str, pymupdf.Rect]]:
    """The words of ``seite`` with their boxes, except words a hit covers: by offset
    for hits with a text span, by rectangle for the boxes of every hit."""
    treffer = [f for f in fundstellen if f.ende > f.start]
    rects = [pymupdf.Rect(r) for f in fundstellen for r in f.rects]
    frei: list[tuple[str, pymupdf.Rect]] = []
    for a, b in seite.woerter():
        wort = None if _im_treffer(a, b, treffer) else _wortkasten(seite, a, b)
        if wort is not None and not _unter_treffer(wort, rects):
            frei.append((seite.text[a:b], wort))
    return frei


def _unsichtbare_groesse(text: str, rect: pymupdf.Rect, opt: Schwaerzoptionen) -> float:
    """The font size that makes ``text`` as wide as its box (so that selecting it in a
    reader covers the printed word), capped a little above the box height."""
    nach_hoehe = max(2.0, rect.height * 0.8)
    einheit = pymupdf.get_text_length(text, fontname=opt.schrift, fontsize=1.0)
    groesse = min(nach_hoehe * 1.15, rect.width / einheit) if einheit > 0 else nach_hoehe
    return max(2.0, groesse)


def _schreibe_unsichtbar(
    page: pymupdf.Page, text: str, rect: pymupdf.Rect, opt: Schwaerzoptionen
) -> bool:
    """Write ``text`` invisibly (render mode 3) over ``rect``; False when there is
    nothing to write.  A glyph the base font lacks is replaced by what it can encode."""
    if rect.is_empty or rect.height <= 0 or not text.strip():
        return False
    groesse = _unsichtbare_groesse(text, rect, opt)
    try:
        page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), text, fontsize=groesse,
                         fontname=opt.schrift, render_mode=3, overlay=True)
    except Exception:
        ersatz = text.encode("cp1252", "replace").decode("cp1252")
        page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), ersatz, fontsize=groesse,
                         fontname=opt.schrift, render_mode=3, overlay=True)
    return True


def _schreibe_pseudonyme(
    page: pymupdf.Page, fundstellen: list[Fundstelle], opt: Schwaerzoptionen
) -> int:
    """Write the pseudonym of every hit invisibly over its first box (masks have
    none) and return how many were written."""
    anzahl = 0
    for f in fundstellen:
        if not f.rects or not f.pseudonym or ist_maske(f):
            continue
        if _schreibe_unsichtbar(page, f.pseudonym, pymupdf.Rect(f.rects[0]), opt):
            anzahl += 1
    return anzahl


def unsichtbare_textebene(
    page: pymupdf.Page, seite: Seitentext, fundstellen: list[Fundstelle],
    mit_pseudonymen: bool = False, opt: Schwaerzoptionen | None = None,
) -> int:
    """Write the words of ``seite`` invisibly (render mode 3) where they stand, except
    words that a hit covers; with ``mit_pseudonymen`` the pseudonyms are written
    invisibly too (for rasterised pages, whose visible pseudonyms are pixels).  The
    result is a searchable text layer for scans and photos, which a language model
    or a PDF reader can extract like the text of a born-digital document.
    Returns the number of words written."""
    opt = opt or Schwaerzoptionen()
    anzahl = 0
    for text, wort in _freie_woerter(seite, fundstellen):
        if _schreibe_unsichtbar(page, text, wort, opt):
            anzahl += 1
    if mit_pseudonymen:
        anzahl += _schreibe_pseudonyme(page, fundstellen, opt)
    return anzahl


def bereinige(doc: pymupdf.Document, vermerk: bool = True, datum: dt.date | None = None) -> None:
    """Remove metadata, XMP, attachments, bookmarks, annotations and form fields.

    Hidden text stays: it was read and redacted like visible text, and it is the
    text layer of scanned PDFs (theirs or ours)."""
    doc.scrub(
        attached_files=True,
        clean_pages=True,
        embedded_files=True,
        hidden_text=False,
        javascript=True,
        metadata=True,
        redactions=True,
        redact_images=0,
        remove_links=True,
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    for page in doc:
        for widget in list(page.widgets()):
            page.delete_widget(widget)
        for annot in list(page.annots()):
            page.delete_annot(annot)
    doc.set_toc([])
    meta = {k: "" for k in ("title", "author", "subject", "keywords", "creator", "producer",
                            "creationDate", "modDate")}
    if vermerk:
        datum = datum or dt.date.today()
        meta.update(ERLAUBTE_METADATEN)
        meta["subject"] = f"{VERMERK_PRAEFIX} am {datum.strftime('%d.%m.%Y')}"
    doc.set_metadata(meta)


def rastere(doc: pymupdf.Document, dpi: int = 200) -> pymupdf.Document:
    """Return a new document whose pages are plain images of the given one."""
    neu = pymupdf.open()
    for page in doc:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        seite = neu.new_page(width=page.rect.width, height=page.rect.height)
        seite.insert_image(seite.rect, pixmap=pix)
    return neu


def speichere(doc: pymupdf.Document, ziel: Path) -> None:
    """Full (non-incremental) save with garbage collection so that nothing removed survives."""
    doc.save(
        ziel,
        garbage=4,
        deflate=True,
        clean=True,
        incremental=False,
        encryption=pymupdf.PDF_ENCRYPT_NONE,
    )
