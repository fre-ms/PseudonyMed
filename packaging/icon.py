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

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "packaging" / "icon" / "pseudonymed.svg"
PNG_FOLDER = ROOT / "pseudonymed" / "gui" / "icon"
ICO = ROOT / "packaging" / "icon" / "pseudonymed.ico"
ICNS = ROOT / "packaging" / "icon" / "pseudonymed.icns"
SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICONSET = {  # file name in the iconset -> pixel size
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32, "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256, "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512, "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def render(svg: Path, size: int, target: Path) -> None:
    """One PNG of ``size`` pixels, square, transparent outside the tile."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _ = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError(f"{svg} lässt sich nicht als SVG lesen.")
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(target), "PNG"):
        raise OSError(f"{target} konnte nicht geschrieben werden.")


def pngs(svg: Path = SVG, folder: Path = PNG_FOLDER) -> list[Path]:
    out = []
    for n in SIZES:
        target = folder / f"pseudonymed_{n}.png"
        render(svg, n, target)
        out.append(target)
    return out


def ico(svg: Path = SVG, target: Path = ICO) -> Path:
    """Windows icon with every size in one file (Pillow writes the container)."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        images = []
        for n in ICO_SIZES:
            path = Path(tmp) / f"{n}.png"
            render(svg, n, path)
            images.append(Image.open(path).convert("RGBA"))
        largest_image = images[-1]
        largest_image.save(target, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                           append_images=images[:-1])
    return target


def icns(svg: Path = SVG, target: Path = ICNS) -> Path | None:
    """macOS icon through iconutil; None where iconutil is missing."""
    tool = shutil.which("iconutil")
    if tool is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "pseudonymed.iconset"
        iconset.mkdir()
        for name, n in ICONSET.items():
            render(svg, n, iconset / name)
        subprocess.run([tool, "-c", "icns", str(iconset), "-o", str(target)], check=True)
    return target


def main() -> int:
    if not SVG.exists():
        print(f"fehlt: {SVG}", file=sys.stderr)
        return 1
    for p in pngs():
        print(p.relative_to(ROOT))
    print(ico().relative_to(ROOT))
    result = icns()
    print(result.relative_to(ROOT) if result else "icns: iconutil fehlt (nicht macOS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
