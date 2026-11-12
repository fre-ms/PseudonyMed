"""The views of the visual review, driven offscreen with synthetic hits: which rectangles
and spans are painted for which kind of mark, and what the page does without a page."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsRectItem  # noqa: E402

from pseudonymed.gui import ansichten as modul  # noqa: E402
from pseudonymed.gui.ansichten import Seitenansicht, Textansicht  # noqa: E402
from pseudonymed.gui.pruefen_seite import PruefenSeite  # noqa: E402
from pseudonymed.gui.sitzung import Sitzung  # noqa: E402
from pseudonymed.kern.modell import Art, Fundstelle, Patient  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _png(breite: int = 200, hoehe: int = 100) -> bytes:
    """A white page image of the given size."""
    bild = QImage(breite, hoehe, QImage.Format.Format_RGB32)
    bild.fill(Qt.GlobalColor.white)
    puffer = QBuffer()
    puffer.open(QIODevice.OpenModeFlag.WriteOnly)
    bild.save(puffer, "PNG")
    return bytes(puffer.data())


def _treffer(regel: str, rects: list[tuple[float, float, float, float]] = (),
             start: int = 0, ende: int = 0, quelle: str = "text") -> Fundstelle:
    """A synthetic hit of the rule with the given boxes or span; its pseudonym names it."""
    return Fundstelle(seite=0, start=start, ende=ende, text="x", art=Art.MANUELL, regel=regel,
                      pseudonym=f"[{regel}]", rects=list(rects), quelle=quelle)


def _mit_farbe(kaesten: list[QGraphicsRectItem], farbe: QColor) -> list[QGraphicsRectItem]:
    return [k for k in kaesten if k.pen().color() == farbe]


def test_seitenansicht_malt_jede_art(qapp: QApplication) -> None:
    """Every kind of mark gets its colour, the hits lie on top, and only hits and discarded
    hits are clickable."""
    ansicht = Seitenansicht()
    rot = _treffer("name.nachname", [(10, 10, 50, 20)])
    vermutet = _treffer("ner.person", [(10, 30, 50, 40)])
    gelb = _treffer("ocr.unlesbar", [(10, 50, 50, 60)], quelle="ocr")
    maske = _treffer("maske", [(60, 10, 90, 20)], quelle="manuell")
    verworfen = _treffer("zuweiser1.name", [(60, 30, 90, 40), (60, 42, 90, 52)])
    ansicht.zeige(_png(), [rot, vermutet, gelb, maske], vermutet,
                  fachrects=[(100, 10, 140, 20)], verworfen=[verworfen],
                  unsicher=[(100, 30, 140, 40)], auswahl=[(100, 50, 140, 60)])
    kaesten = [i for i in ansicht.scene().items() if isinstance(i, QGraphicsRectItem)]
    assert len(kaesten) == 4 + 2 + 3  # four hits, two lines of the discarded one, three marks
    assert len(_mit_farbe(kaesten, modul.ROT)) == 2  # the rule hit and the guess
    assert len(_mit_farbe(kaesten, modul.GELB)) == 2  # the unreadable line, the unsure word
    assert len(_mit_farbe(kaesten, modul.GRUEN)) == 3  # the term, the discarded hit's lines
    assert len(_mit_farbe(kaesten, modul.BLAU)) == 1  # the picked word
    assert len(_mit_farbe(kaesten, modul.SCHWARZ)) == 1  # the mask
    # the selected guess is heavier, more opaque and dashed; a plain hit is thin and solid
    [g] = [k for k in kaesten if k.toolTip() == "ner.person → [ner.person]"]
    assert g.pen().widthF() == 3 and g.pen().style() == Qt.PenStyle.DashLine
    assert g.brush().color().alpha() == 90
    [r] = [k for k in kaesten if k.toolTip() == "name.nachname → [name.nachname]"]
    assert r.pen().widthF() == 1.5 and r.pen().style() == Qt.PenStyle.SolidLine
    assert r.brush().color().alpha() == 45
    [m] = _mit_farbe(kaesten, modul.SCHWARZ)
    assert m.brush().color().alpha() == 160 and m.pen().style() == Qt.PenStyle.SolidLine
    # the discarded hit: dashed green, both lines lead back to it
    gruen = _mit_farbe(kaesten, modul.GRUEN)
    assert sum(1 for k in gruen if ansicht._items.get(id(k)) is verworfen) == 2
    assert all(k.pen().style() == Qt.PenStyle.DashLine for k in gruen
               if ansicht._items.get(id(k)) is verworfen)
    # the marks are not clickable; the unsure word is an outline only
    assert {id(f) for f in ansicht._items.values()} == {
        id(rot), id(vermutet), id(gelb), id(maske), id(verworfen)}
    [u] = [k for k in kaesten if k.toolTip() == "unsicher gelesen"]
    assert u.brush().color().alpha() == 0
    assert any(k.toolTip() == "Fachbegriff (bleibt)" for k in kaesten)
    # painting order: the scene lists the topmost first, so hits lie above the term marks
    assert ansicht._items.get(id(kaesten[0])) is maske
    assert kaesten[-1].toolTip() == "Fachbegriff (bleibt)"
    ansicht.zeige_leer()
    assert not ansicht.scene().items() and not ansicht._items


def test_seitenansicht_ohne_marken(qapp: QApplication) -> None:
    """A page with only the image: nothing but the pixmap, nothing clickable; a second
    call replaces the first page completely."""
    ansicht = Seitenansicht()
    ansicht.zeige(_png(), [_treffer("name.vorname", [(1, 1, 5, 5)])], None)
    assert len(ansicht._items) == 1
    ansicht.zeige(_png(50, 50), [], None)
    assert len(ansicht.scene().items()) == 1 and not ansicht._items
    assert ansicht.scene().sceneRect().width() == 50


def test_textansicht_spannen(qapp: QApplication) -> None:
    """Spans in painting order (terms, discarded, hits), empty spans left out, the cursor on
    the selected hit, and the text loaded only once."""
    ansicht = Textansicht()
    text = "Die Person kam am Montag mit Kopfschmerzen in die Sprechstunde."
    a = text.index("Montag")
    treffer = _treffer("muster.datum", start=a, ende=a + len("Montag"))
    leer = _treffer("manuell", start=3, ende=3)
    verworfen = _treffer("ner.person", start=4, ende=4 + len("Person"))
    k = text.index("Kopfschmerzen")
    ansicht.zeige(text, [treffer, leer], treffer, fachspannen=[(k, k + 13)],
                  verworfen=[verworfen])
    assert ansicht.toPlainText() == text
    assert ansicht._treffer == [treffer, verworfen]  # the empty span is not clickable
    spannen = [(s.cursor.selectionStart(), s.cursor.selectionEnd(),
                s.format.background().color()) for s in ansicht.extraSelections()]
    assert [(x, y) for x, y, _c in spannen] == [(k, k + 13), (4, 10), (a, a + 6)]
    assert [c.alpha() for _x, _y, c in spannen] == [45, 70, 170]  # the hit is selected
    assert spannen[0][2].rgb() == modul.GRUEN.rgb() and spannen[2][2].rgb() == modul.ROT.rgb()
    assert ansicht.textCursor().position() == a
    revision = ansicht.document().revision()
    ansicht.zeige(text, [treffer], None, verworfen=[verworfen])
    assert ansicht.document().revision() == revision  # same text: not reloaded
    assert [c.alpha() for s in ansicht.extraSelections()
            for c in [s.format.background().color()]] == [70, 80]
    ansicht.zeige(text, [leer], leer)
    assert ansicht.textCursor().position() == a  # an empty selection moves nothing
    ansicht.zeige(text + " Nachtrag.", [], None)
    assert ansicht.document().revision() != revision and not ansicht.extraSelections()
    schrift = ansicht.font()
    ansicht.zoom(1.25)
    ansicht.zoom(0.8)
    ansicht.zoom_zuruecksetzen()
    assert ansicht.font() == schrift


def test_pruefen_seite_ohne_dokument(qapp: QApplication, tmp_path: Path) -> None:
    """Before a document was chosen every action is a no-op and nothing is written."""
    seite = PruefenSeite(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    seite.aktualisiere()
    assert seite.dokument is None and seite.fundstellen() == []
    seite.zeichne()
    seite.kasten((0, 0, 10, 10))
    seite.punkt(1.0, 1.0)
    seite.verwerfe()
    seite.aufnehmen()
    seite.markiere_auswahl()
    assert seite.arbeitsstand_speichern(tmp_path / "x.arbeitsstand.json") is None
    assert seite.tabelle.rowCount() == 0 and seite.hinweis.text() == ""
    assert seite._aktive_ansicht() is seite.ansicht
    assert not list(tmp_path.glob("*.arbeitsstand.*"))


def test_pruefen_seite_textdatei(qapp: QApplication, tmp_path: Path, patient: Patient) -> None:
    """A text file has no page: boxes and word picks do nothing, the text view is in front,
    and the replacement for a take-up comes from the field unless it holds the default."""
    from tests.synthetisch import erzeuge_transkript

    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    sitzung.patient = patient
    d = sitzung.fuege_hinzu(erzeuge_transkript(tmp_path / "gespraech.txt", patient))
    assert d is not None
    sitzung.analysiere(d)
    seite = PruefenSeite(sitzung)
    seite.synchron = True
    seite.aktualisiere()
    assert seite.dokument is d and seite.stapel.currentWidget() is seite.textansicht
    assert seite._aktive_ansicht() is seite.textansicht
    assert seite.markieren.isEnabled() and seite.maske_modus.isHidden()
    assert seite.tabelle.rowCount() == len(seite.fundstellen()) > 0
    hinweis = seite.hinweis.text()
    vorher = list(seite.fundstellen())
    seite.kasten((10, 10, 50, 20), "[Kasten]")  # no page to draw on
    seite.punkt(10.0, 10.0)
    assert seite.hinweis.text() == hinweis and seite.auswahl is None
    assert seite.fundstellen() == vorher
    assert seite._ersatz(None) is None and seite._ersatz("[X]") == "[X]"
    seite.ersatztext.setText("  ")
    assert seite._ersatz(None) is None
    seite.ersatztext.setText("[Begriff]")
    assert seite._ersatz(None) == "[Begriff]" and seite._ersatz("[X]") == "[X]"
    with pytest.raises(ValueError):
        seite.aufnehmen()  # nothing selected
    sitzung.schliessen()
