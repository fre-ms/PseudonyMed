"""Render the app icon from ``packaging/icon/pseudonymed.svg`` into what the programs need.

* ``pseudonymed/gui/icon/pseudonymed_<n>.png`` for n in 16 … 1024: the window icon (QIcon
  picks the fitting size) and the Linux desktop;
* ``packaging/icon/pseudonymed.ico``: Windows, every size from 16 to 256 in one file;
* ``packaging/icon/pseudonymed.icns``: macOS, built with ``iconutil`` from an iconset
  (skipped elsewhere; the committed file serves then).

Rendering uses Qt's own SVG module (part of PySide6), so no other tool is needed.  Run it
after every change of the SVG; the results are committed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
SVG = WURZEL / "packaging" / "icon" / "pseudonymed.svg"
PNG_ORDNER = WURZEL / "pseudonymed" / "gui" / "icon"
ICO = WURZEL / "packaging" / "icon" / "pseudonymed.ico"
ICNS = WURZEL / "packaging" / "icon" / "pseudonymed.icns"
GROESSEN = (16, 32, 48, 64, 128, 256, 512, 1024)
ICO_GROESSEN = (16, 24, 32, 48, 64, 128, 256)
ICONSET = {  # file name in the iconset -> pixel size
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32, "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256, "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512, "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def rendere(svg: Path, groesse: int, ziel: Path) -> None:
    """One PNG of ``groesse`` pixels, square, transparent outside the tile."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _ = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError(f"{svg} lässt sich nicht als SVG lesen.")
    bild = QImage(QSize(groesse, groesse), QImage.Format.Format_ARGB32_Premultiplied)
    bild.fill(Qt.GlobalColor.transparent)
    maler = QPainter(bild)
    maler.setRenderHint(QPainter.RenderHint.Antialiasing)
    maler.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(maler)
    maler.end()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    if not bild.save(str(ziel), "PNG"):
        raise OSError(f"{ziel} konnte nicht geschrieben werden.")


def pngs(svg: Path = SVG, ordner: Path = PNG_ORDNER) -> list[Path]:
    aus = []
    for n in GROESSEN:
        ziel = ordner / f"pseudonymed_{n}.png"
        rendere(svg, n, ziel)
        aus.append(ziel)
    return aus


def ico(svg: Path = SVG, ziel: Path = ICO) -> Path:
    """Windows icon with every size in one file (Pillow writes the container)."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        bilder = []
        for n in ICO_GROESSEN:
            pfad = Path(tmp) / f"{n}.png"
            rendere(svg, n, pfad)
            bilder.append(Image.open(pfad).convert("RGBA"))
        groesstes = bilder[-1]
        groesstes.save(ziel, format="ICO", sizes=[(n, n) for n in ICO_GROESSEN],
                       append_images=bilder[:-1])
    return ziel


def icns(svg: Path = SVG, ziel: Path = ICNS) -> Path | None:
    """macOS icon through iconutil; None where iconutil is missing."""
    werkzeug = shutil.which("iconutil")
    if werkzeug is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "pseudonymed.iconset"
        iconset.mkdir()
        for name, n in ICONSET.items():
            rendere(svg, n, iconset / name)
        subprocess.run([werkzeug, "-c", "icns", str(iconset), "-o", str(ziel)], check=True)
    return ziel


def main() -> int:
    if not SVG.exists():
        print(f"fehlt: {SVG}", file=sys.stderr)
        return 1
    for p in pngs():
        print(p.relative_to(WURZEL))
    print(ico().relative_to(WURZEL))
    ergebnis = icns()
    print(ergebnis.relative_to(WURZEL) if ergebnis else "icns: iconutil fehlt (nicht macOS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
