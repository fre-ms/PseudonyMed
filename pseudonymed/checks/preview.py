"""Preview images of the pseudonymised pages for the visual check of scans and photos."""

from __future__ import annotations

from pathlib import Path

import pymupdf


def write_preview(doc: pymupdf.Document, folder: Path, dpi: int = 100) -> list[Path]:
    """Write one PNG per page of ``doc`` into ``folder``; returns the paths in page order."""
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, page in enumerate(doc, 1):
        path = folder / f"seite_{i:02d}.png"
        page.get_pixmap(dpi=dpi, alpha=False).save(path)
        paths.append(path)
    return paths
