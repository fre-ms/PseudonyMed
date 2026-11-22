"""The app icon: rendered sizes present, loadable as QIcon, renderable from the SVG."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pseudonymed.gui import icon as icon_module
from tests.test_gui import qapp  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def test_rendered_sizes_present() -> None:
    assert [p.name for p in icon_module.files()] == [
        f"pseudonymed_{n}.png" for n in icon_module.SIZES]
    assert (ROOT / "packaging" / "icon" / "pseudonymed.svg").exists()
    assert (ROOT / "packaging" / "icon" / "pseudonymed_marke.svg").read_text().count(
        "currentColor") >= 2
    assert (ROOT / "packaging" / "icon" / "pseudonymed.ico").stat().st_size > 1000
    for draft in ("b_klammer", "c_schluessel"):
        assert (ROOT / "packaging" / "icon" / "entwuerfe" / f"{draft}.svg").exists()


def test_window_carries_the_icon(qapp: QApplication) -> None:  # noqa: F811
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    icon = icon_module.app_icon()
    assert not icon.isNull() and icon.availableSizes()
    window = MainWindow(Session(store_path=None))
    assert not window.windowIcon().isNull()
    window.close()


def test_render_from_the_svg(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sys.path.insert(0, str(ROOT / "packaging"))
    import icon as builder

    pngs = builder.pngs(folder=tmp_path / "png")
    assert len(pngs) == len(builder.SIZES) and all(p.stat().st_size > 100 for p in pngs)
    from PIL import Image

    with Image.open(pngs[0]) as small:
        assert small.size == (16, 16) and small.mode == "RGBA"
        assert small.getpixel((0, 0))[3] == 0  # transparent outside the rounded tile
        assert small.getpixel((8, 8))[3] == 255
    ico = builder.ico(target=tmp_path / "x.ico")
    with Image.open(ico) as image:
        assert image.format == "ICO"
    if sys.platform == "darwin":
        icns = builder.icns(target=tmp_path / "x.icns")
        assert icns is not None and icns.stat().st_size > 10_000
    with pytest.raises(ValueError):
        builder.render(tmp_path / "fehlt.svg", 16, tmp_path / "y.png")
