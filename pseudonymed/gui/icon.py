"""The application icon for windows and dialogs.

The PNG sizes next to this module are rendered from ``packaging/icon/pseudonymed.svg`` by
``packaging/icon.py``; Qt picks the size that fits (the SVG itself would need the Qt SVG
module, which the packaged program leaves out).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

ORDNER = Path(__file__).resolve().parent / "icon"
GROESSEN = (16, 32, 48, 64, 128, 256, 512, 1024)


def dateien() -> list[Path]:
    return [p for p in (ORDNER / f"pseudonymed_{n}.png" for n in GROESSEN) if p.exists()]


def app_icon() -> QIcon:
    """The icon with every rendered size; empty when the files are missing."""
    icon = QIcon()
    for p in dateien():
        icon.addFile(str(p))
    return icon
