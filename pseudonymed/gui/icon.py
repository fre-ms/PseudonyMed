"""The application icon for windows and dialogs.

The PNG sizes next to this module are rendered from ``packaging/icon/pseudonymed.svg`` by
``packaging/icon.py``; Qt picks the size that fits (the SVG itself would need the Qt SVG
module, which the packaged program leaves out).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

FOLDER = Path(__file__).resolve().parent / "icon"
SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)


def files() -> list[Path]:
    """The rendered PNG files that exist, smallest first."""
    return [p for p in (FOLDER / f"pseudonymed_{n}.png" for n in SIZES) if p.exists()]


def app_icon() -> QIcon:
    """The icon with every rendered size; empty when the files are missing."""
    icon = QIcon()
    for p in files():
        icon.addFile(str(p))
    return icon
