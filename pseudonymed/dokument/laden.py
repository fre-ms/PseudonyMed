"""Open PDFs and images as a PyMuPDF document."""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf

BILDENDUNGEN = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".pnm"}
HEIC_ENDUNGEN = {".heic", ".heif"}

# page sizes in points for photos and images; "bild" keeps the page as large as the image
PAPIERFORMATE: dict[str, tuple[float, float] | None] = {
    "a4": (595.276, 841.89),
    "a5": (419.528, 595.276),
    "a3": (841.89, 1190.551),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
    "bild": None,
}


def papierformat(name: str | None) -> tuple[float, float] | None:
    """Width and height in points (portrait) of a paper size name, None for "bild"."""
    schluessel = (name or "a4").lower().replace("-", "").replace(" ", "")
    schluessel = {"usletter": "letter", "uslegal": "legal", "image": "bild"}.get(schluessel,
                                                                                 schluessel)
    if schluessel not in PAPIERFORMATE:
        raise ValueError(f"Unbekanntes Papierformat: {name!r} ({', '.join(PAPIERFORMATE)})")
    return PAPIERFORMATE[schluessel]


def oeffne(pfad: str | Path, papier: str | None = "a4") -> pymupdf.Document:
    """Open a PDF, or an image as a PDF: each image page goes on a sheet of the chosen
    paper size (portrait or landscape following the image, scaled to fit, centred),
    with ``papier="bild"`` on a page exactly as large as the image.

    HEIC/HEIF photos need the optional ``ocr`` extra (pillow-heif).
    """
    pfad = Path(pfad)
    endung = pfad.suffix.lower()
    if endung == ".pdf":
        return pymupdf.open(pfad)
    if endung in BILDENDUNGEN:
        bild = pymupdf.open(pfad)
        try:
            return _auf_papier(bild, papier)
        finally:
            bild.close()
    if endung in HEIC_ENDUNGEN:
        return _heic(pfad, papier)
    raise ValueError(f"Unsupported file type: {pfad.suffix}")


def _auf_papier(bild: pymupdf.Document, papier: str | None) -> pymupdf.Document:
    """The pages of an image document as a PDF on the chosen paper.  The image keeps
    its bytes and its EXIF orientation (the image document already applied it); it is
    placed as a form on the sheet, which the redaction of image pixels reaches."""
    format_ = papierformat(papier)
    quelle = pymupdf.open("pdf", bild.convert_to_pdf())
    if format_ is None:
        return quelle
    doc = pymupdf.open()
    for i, seite in enumerate(quelle):
        breite, hoehe = format_
        if seite.rect.width > seite.rect.height:
            breite, hoehe = hoehe, breite
        page = doc.new_page(width=breite, height=hoehe)
        page.show_pdf_page(page.rect, quelle, i)
    ergebnis = pymupdf.open("pdf", doc.tobytes())  # independent of the source
    doc.close()
    quelle.close()
    return ergebnis


def _heic(pfad: Path, papier: str | None = "a4") -> pymupdf.Document:
    """HEIC/HEIF photos: decode with pillow-heif (extra 'ocr'), honour the EXIF orientation."""
    try:
        import pillow_heif
        from PIL import Image, ImageOps
    except ImportError as e:
        raise NotImplementedError(
            "HEIC/HEIF photos need the extra 'ocr' (pillow-heif): uv sync --extra ocr"
        ) from e
    pillow_heif.register_heif_opener()
    with Image.open(pfad) as bild:
        rgb = ImageOps.exif_transpose(bild).convert("RGB")
        puffer = io.BytesIO()
        rgb.save(puffer, format="PNG")
    zwischen = pymupdf.open("png", puffer.getvalue())
    try:
        return _auf_papier(zwischen, papier)
    finally:
        zwischen.close()
