"""The app icon: rendered sizes present, loadable as QIcon, renderable from the SVG."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pseudonymed.gui import icon as iconmodul
from tests.test_gui import qapp  # noqa: F401

WURZEL = Path(__file__).resolve().parent.parent


def test_gerenderte_groessen_vorhanden() -> None:
    assert [p.name for p in iconmodul.dateien()] == [
        f"pseudonymed_{n}.png" for n in iconmodul.GROESSEN]
    assert (WURZEL / "packaging" / "icon" / "pseudonymed.svg").exists()
    assert (WURZEL / "packaging" / "icon" / "pseudonymed_marke.svg").read_text().count(
        "currentColor") >= 2
    assert (WURZEL / "packaging" / "icon" / "pseudonymed.ico").stat().st_size > 1000
    for entwurf in ("b_klammer", "c_schluessel"):
        assert (WURZEL / "packaging" / "icon" / "entwuerfe" / f"{entwurf}.svg").exists()


def test_fenster_traegt_das_icon(qapp: QApplication) -> None:  # noqa: F811
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    icon = iconmodul.app_icon()
    assert not icon.isNull() and icon.availableSizes()
    fenster = Hauptfenster(Sitzung(speicherpfad=None))
    assert not fenster.windowIcon().isNull()
    fenster.close()


def test_rendern_aus_dem_svg(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sys.path.insert(0, str(WURZEL / "packaging"))
    import icon as bau

    pngs = bau.pngs(ordner=tmp_path / "png")
    assert len(pngs) == len(bau.GROESSEN) and all(p.stat().st_size > 100 for p in pngs)
    from PIL import Image

    with Image.open(pngs[0]) as klein:
        assert klein.size == (16, 16) and klein.mode == "RGBA"
        assert klein.getpixel((0, 0))[3] == 0  # transparent outside the rounded tile
        assert klein.getpixel((8, 8))[3] == 255
    ico = bau.ico(ziel=tmp_path / "x.ico")
    with Image.open(ico) as bild:
        assert bild.format == "ICO"
    if sys.platform == "darwin":
        icns = bau.icns(ziel=tmp_path / "x.icns")
        assert icns is not None and icns.stat().st_size > 10_000
    with pytest.raises(ValueError):
        bau.rendere(tmp_path / "fehlt.svg", 16, tmp_path / "y.png")
