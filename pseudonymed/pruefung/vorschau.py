"""Preview images of the pseudonymised pages for the visual check of scans and photos."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def schreibe_vorschau(doc: pymupdf.Document, ordner: Path, dpi: int = 100) -> list[Path]:
    """Write one PNG per page of ``doc`` into ``ordner``; returns the paths in page order."""
    ordner.mkdir(parents=True, exist_ok=True)
    pfade: list[Path] = []
    for i, page in enumerate(doc, 1):
        pfad = ordner / f"seite_{i:02d}.png"
        page.get_pixmap(dpi=dpi, alpha=False).save(pfad)
        pfade.append(pfad)
    return pfade
