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
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from pseudonymed import WERKZEUG
from pseudonymed.dokument.textebene import Seitentext, lese_seite
from pseudonymed.kern.modell import Fundstelle

ERLAUBTE_METADATEN = {"producer": WERKZEUG, "creator": WERKZEUG}
VERMERK_PRAEFIX = "pseudonymisiert mit PseudonyMed"


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
    for i in range(f.start, min(f.ende, len(seite.text))):
        box = seite.boxen[i]
        if box is not None and pymupdf.Rect(box).intersects(rect):
            return seite.zeilen[i]
    return -1


def _passende_groesse(text: str, breite: float, start: float, opt: Schwaerzoptionen) -> float:
    groesse = max(start, opt.min_schrift)
    while groesse > opt.min_schrift and pymupdf.get_text_length(
        text, fontname=opt.schrift, fontsize=groesse
    ) > breite:
        groesse -= 0.5
    return groesse


def lage_intakt(
    vorher: Seitentext, nachher: Seitentext, fundstellen: list[Fundstelle], toleranz: float = 0.6,
    kaesten: list[pymupdf.Rect] | None = None,
) -> bool:
    """True when every character outside the hits (and outside the applied boxes
    ``kaesten``) is still where it was."""
    entfernt: set[int] = set()
    kaesten = list(kaesten or [])
    for f in fundstellen:
        entfernt.update(range(f.start, f.ende))

    def unter_kasten(box: tuple[float, float, float, float]) -> bool:
        return any(k.intersects(pymupdf.Rect(box)) for k in kaesten)

    erwartet = sorted(
        ((vorher.text[i], vorher.boxen[i]) for i in range(len(vorher.text))
         if vorher.boxen[i] is not None and i not in entfernt and not vorher.text[i].isspace()
         and not unter_kasten(vorher.boxen[i])),
        key=lambda e: (round(e[1][1]), e[1][0]),
    )
    ist = sorted(
        ((nachher.text[i], nachher.boxen[i]) for i in range(len(nachher.text))
         if nachher.boxen[i] is not None and not nachher.text[i].isspace()),
        key=lambda e: (round(e[1][1]), e[1][0]),
    )
    if len(erwartet) != len(ist):
        return False
    for (c1, b1), (c2, b2) in zip(erwartet, ist, strict=True):
        if c1 != c2 or abs(b1[0] - b2[0]) > toleranz or abs(b1[1] - b2[1]) > toleranz:
            return False
    return True


def _einblendung(
    page: pymupdf.Page, fuell: pymupdf.Rect, basis: float, groesse: float, pseudonym: str,
    opt: Schwaerzoptionen, mit_flaeche: bool = True,
) -> None:
    if mit_flaeche:
        page.draw_rect(fuell, color=None, fill=opt.fuellfarbe, overlay=True)
    page.insert_text(
        (fuell.x0 + 0.75, basis),
        pseudonym,
        fontsize=groesse,
        fontname=opt.schrift,
        color=opt.textfarbe,
        overlay=True,
    )


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
    texte: list[tuple[pymupdf.Rect, float, float, str]] = []
    for f in fundstellen:
        for k, roh in enumerate(f.rects):
            rect = pymupdf.Rect(roh)
            tp.draw_rect(rect, color=None, fill=SCHWARZ if ist_maske(f) else opt.fuellfarbe,
                         overlay=True)
            if k == 0 and not ist_maske(f):
                basis = seite.basislinie(f.start, f.ende) if f.quelle == "text" else None
                if basis is None:
                    basis = rect.y1 - 0.22 * rect.height
                groesse = _passende_groesse(f.pseudonym, rect.width - 1.5, f.schriftgroesse, opt)
                texte.append((rect, basis, groesse, f.pseudonym))
    for rect, basis, groesse, pseudonym in texte:
        _einblendung(tp, rect, basis, groesse, pseudonym, opt, mit_flaeche=False)
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
    einblendungen: list[tuple[pymupdf.Rect, float, float, str]] = []
    anzahl = 0
    texttreffer = [f for f in fundstellen if f.quelle == "text"]
    manuell = [f for f in fundstellen if f.quelle == "manuell"]
    baender_kaesten: list[pymupdf.Rect] = []
    nur_pixel: list[tuple[pymupdf.Rect, tuple[float, float, float]]] = []
    flaechen: list[tuple[pymupdf.Rect, tuple[float, float, float]]] = []
    tinte = _Tinte(page) if any(f.quelle == "ocr" for f in fundstellen) else None
    for f in fundstellen:
        if not f.rects:
            continue
        gezeichnet = f.quelle == "manuell" and f.ende > f.start and seite is not None
        farbe = SCHWARZ if ist_maske(f) else opt.fuellfarbe
        for k, roh in enumerate(f.rects):
            rect = pymupdf.Rect(roh)
            if gezeichnet and k == len(f.rects) - 1 and len(f.rects) > 1:
                nur_pixel.append((rect & page.rect, farbe))  # the drawn box: pixels only
                continue
            if (f.quelle == "text" or gezeichnet) and seite is not None:
                zeile = _zeile_von_rect(seite, f, rect)
                band = _band(rect, zeile, seite) if zeile >= 0 else rect
            elif f.quelle == "manuell":
                band = pymupdf.Rect(rect) & page.rect  # box on an image page
                baender_kaesten.append(pymupdf.Rect(band))
            else:
                if f.quelle == "ocr" and tinte is not None:
                    rect = tinte.erweitere(rect)
                rand = max(OCR_RAND, 0.15 * rect.height)
                rect = pymupdf.Rect(rect.x0 - rand, rect.y0 - OCR_RAND,
                                    rect.x1 + rand, rect.y1 + OCR_RAND) & page.rect
                band = pymupdf.Rect(rect)
                baender_kaesten.append(pymupdf.Rect(band))
            fuell = pymupdf.Rect(rect)
            if ist_maske(f):
                flaechen.append((fuell, SCHWARZ))
            elif k == 0:
                textquelle = (f.quelle == "text" or gezeichnet) and seite is not None
                basis = seite.basislinie(f.start, f.ende) if textquelle else None
                if basis is None:
                    basis = rect.y1 - 0.22 * rect.height
                frei = seite.freier_raum_rechts(f.ende) if textquelle else rect.x1
                benoetigt = pymupdf.get_text_length(
                    f.pseudonym, fontname=opt.schrift, fontsize=f.schriftgroesse
                ) + 2.0
                x1 = max(rect.x1, min(rect.x0 + benoetigt, frei - 1.0))
                fuell.x1 = band.x1 = max(band.x1, x1 - 0.2)
                groesse = _passende_groesse(f.pseudonym, fuell.width - 1.5, f.schriftgroesse, opt)
                einblendungen.append((fuell, basis, groesse, f.pseudonym))
            page.add_redact_annot(band, fill=farbe, cross_out=False)
            anzahl += 1
    if not anzahl and not nur_pixel:
        return Seitenergebnis(0, True)
    if anzahl:
        page.apply_redactions(
            images=opt.bilder, graphics=opt.grafik, text=pymupdf.PDF_REDACT_TEXT_REMOVE
        )
    if nur_pixel:
        for rect, farbe in nur_pixel:
            page.add_redact_annot(rect, fill=farbe, cross_out=False)
        page.apply_redactions(images=opt.bilder, graphics=opt.grafik,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
        anzahl += len(nur_pixel)
    if seite is not None and (texttreffer or manuell or baender_kaesten):
        if not lage_intakt(seite, lese_seite(page, seite.nummer), texttreffer + manuell,
                           kaesten=baender_kaesten):
            return Seitenergebnis(anzahl, False)
    # all boxes first, then all pseudonyms, so that a neighbouring box never covers a text
    for rect, farbe in nur_pixel + flaechen:
        page.draw_rect(rect, color=None, fill=farbe, overlay=True)
    for fuell, _basis, _groesse, _pseudonym in einblendungen:
        page.draw_rect(fuell, color=None, fill=opt.fuellfarbe, overlay=True)
    for fuell, basis, groesse, pseudonym in _ohne_ueberlappung(einblendungen, opt):
        _einblendung(page, fuell, basis, groesse, pseudonym, opt, mit_flaeche=False)
    return Seitenergebnis(anzahl, True)


def _ohne_ueberlappung(
    einblendungen: list[tuple[pymupdf.Rect, float, float, str]], opt: Schwaerzoptionen
) -> list[tuple[pymupdf.Rect, float, float, str]]:
    """Shrink a pseudonym to the room before the next box on the same line, so that
    overlapping boxes (frequent on scans) never print one text over another."""
    ergebnis = []
    for fuell, basis, groesse, pseudonym in einblendungen:
        rechts = fuell.x1
        for anderes, andere_basis, _g, _p in einblendungen:
            if anderes is fuell or abs(andere_basis - basis) > 3:
                continue
            if fuell.x0 < anderes.x0 < rechts:
                rechts = anderes.x0
        if rechts < fuell.x1:
            groesse = _passende_groesse(pseudonym, rechts - fuell.x0 - 1.5, groesse, opt)
        ergebnis.append((fuell, basis, groesse, pseudonym))
    return ergebnis


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
    treffer = [f for f in fundstellen if f.ende > f.start]
    rects = [pymupdf.Rect(r) for f in fundstellen for r in f.rects]
    anzahl = 0

    def schreibe(text: str, rect: pymupdf.Rect) -> None:
        nonlocal anzahl
        if rect.is_empty or rect.height <= 0 or not text.strip():
            return
        # the size that makes the word as wide as its box (so that selecting it in a
        # reader covers the printed word), capped a little above the box height
        nach_hoehe = max(2.0, rect.height * 0.8)
        einheit = pymupdf.get_text_length(text, fontname=opt.schrift, fontsize=1.0)
        groesse = min(nach_hoehe * 1.15, rect.width / einheit) if einheit > 0 else nach_hoehe
        groesse = max(2.0, groesse)
        try:
            page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), text, fontsize=groesse,
                             fontname=opt.schrift, render_mode=3, overlay=True)
        except Exception:  # a glyph the base font lacks: write what it can encode
            ersatz = text.encode("cp1252", "replace").decode("cp1252")
            page.insert_text((rect.x0, rect.y1 - 0.22 * rect.height), ersatz, fontsize=groesse,
                             fontname=opt.schrift, render_mode=3, overlay=True)
        anzahl += 1

    for a, b in seite.woerter():
        if any(a < f.ende and b > f.start for f in treffer):
            continue
        kasten = seite.rects(a, b)
        if not kasten:
            continue
        wort = pymupdf.Rect(kasten[0])
        for r in kasten[1:]:
            wort |= r
        if any(r.intersects(wort) for r in rects):
            continue
        schreibe(seite.text[a:b], wort)
    if mit_pseudonymen:
        for f in fundstellen:
            if f.rects and f.pseudonym and not ist_maske(f):
                schreibe(f.pseudonym, pymupdf.Rect(f.rects[0]))
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
