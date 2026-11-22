"""Barcodes and QR codes carry patient numbers on labels and lab sheets: find and blank them."""

from __future__ import annotations

import pymupdf

from pseudonymed.core.model import Hit, Kind

CODE_DPI = 200


def available() -> bool:
    """True when zxing-cpp (extra "codes") can be imported."""
    try:
        import zxingcpp  # noqa: F401
    except ImportError:
        return False
    return True


def find_codes(
    page: pymupdf.Page, number: int | None = None, dpi: int = CODE_DPI
) -> list[Hit]:
    """All machine-readable codes on the page as hits with the pseudonym "[Code]"."""
    if not available():
        return []
    import numpy as np
    import zxingcpp

    scale_factor = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale_factor, scale_factor), alpha=False,
                          colorspace=pymupdf.csGRAY)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    number = page.number if number is None else number
    hits: list[Hit] = []
    for result in zxingcpp.read_barcodes(image):
        pos = result.position
        xs = [pt.x for pt in (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)]
        ys = [pt.y for pt in (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)]
        margin = 4.0
        rect = (min(xs) / scale_factor - margin, min(ys) / scale_factor - margin,
                max(xs) / scale_factor + margin, max(ys) / scale_factor + margin)
        hits.append(Hit(
            page=number, start=0, end=0, text=str(result.text), kind=Kind.MANUAL,
            rule=f"code.{result.format.name.lower()}", pseudonym="[Code]", rects=[rect],
            font_size=8.0, source="code",
        ))
    return hits
