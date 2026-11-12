"""Barcodes and QR codes carry patient numbers on labels and lab sheets: find and blank them."""

from __future__ import annotations

import pymupdf

from pseudonymed.kern.modell import Art, Fundstelle

CODE_DPI = 200


def verfuegbar() -> bool:
    """True when zxing-cpp (extra "codes") can be imported."""
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        return False
    return True


def finde_codes(
    page: pymupdf.Page, nummer: int | None = None, dpi: int = CODE_DPI
) -> list[Fundstelle]:
    """All machine-readable codes on the page as hits with the pseudonym "[Code]"."""
    if not verfuegbar():
        return []
    import numpy as np
    import zxingcpp

    skala = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(skala, skala), alpha=False,
                          colorspace=pymupdf.csGRAY)
    bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    nummer = page.number if nummer is None else nummer
    fundstellen: list[Fundstelle] = []
    for ergebnis in zxingcpp.read_barcodes(bild):
        pos = ergebnis.position
        xs = [pt.x for pt in (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)]
        ys = [pt.y for pt in (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)]
        rand = 4.0
        rect = (min(xs) / skala - rand, min(ys) / skala - rand,
                max(xs) / skala + rand, max(ys) / skala + rand)
        fundstellen.append(Fundstelle(
            seite=nummer, start=0, ende=0, text=str(ergebnis.text), art=Art.MANUELL,
            regel=f"code.{ergebnis.format.name.lower()}", pseudonym="[Code]", rects=[rect],
            schriftgroesse=8.0, quelle="code",
        ))
    return fundstellen
