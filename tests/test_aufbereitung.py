"""Image clean-up: tilt estimate, sheet detection, upscaling, background, masks."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pseudonymed.dokument import aufbereitung as auf

pytestmark = pytest.mark.skipif(not auf.verfuegbar(), reason="OpenCV/NumPy fehlen (Extra ocr)")


def _seite(befund: Path) -> pymupdf.Page:
    doc = pymupdf.open(befund)
    return doc[0]


def test_winkel_wird_geschaetzt(befund: Path) -> None:
    import cv2

    bild, _ = auf.rendere(_seite(befund), 150)
    assert abs(auf.schaetze_winkel(bild)) < 0.3  # straight page
    for soll in (2.0, -3.5):
        h, w = bild.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), -soll, 1.0)  # clockwise = downhill
        schief = cv2.warpAffine(bild, m, (w, h), borderMode=cv2.BORDER_CONSTANT,
                                borderValue=(255, 255, 255))
        geschaetzt = auf.schaetze_winkel(schief)
        assert abs(geschaetzt - soll) <= 0.35, (soll, geschaetzt)
        assert auf.vorschlag(schief).begradigen  # 2° and 3.5° are worth straightening
        gerade = auf.wende_an(schief, auf.Aufbereitung(begradigen=True, winkel=geschaetzt))
        assert abs(auf.schaetze_winkel(gerade)) < 0.35


def test_blatt_im_foto_wird_gefunden(befund: Path) -> None:
    import cv2
    import numpy as np

    bild, _ = auf.rendere(_seite(befund), 100)
    h, w = bild.shape[:2]
    # a dark table with the page photographed at an angle
    foto = np.full((h + 300, w + 300, 3), (60, 70, 80), dtype=np.uint8)
    ziel = np.array([[180, 120], [w + 90, 170], [w + 150, h + 200], [120, h + 240]],
                    dtype=np.float32)
    m = cv2.getPerspectiveTransform(np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32),
                                    ziel)
    verzerrt = cv2.warpPerspective(bild, m, (w + 300, h + 300), dst=foto,
                                   borderMode=cv2.BORDER_TRANSPARENT)
    ecken = auf.finde_ecken(verzerrt)  # skala 1: corners in pixels
    assert ecken is not None and len(ecken) == 4
    for (x, y), (sx, sy) in zip(ecken, ziel, strict=True):
        assert abs(x - sx) < 12 and abs(y - sy) < 12, (ecken, ziel)
    entzerrt = auf.wende_an(verzerrt, auf.Aufbereitung(entzerren=True, ecken=ecken))
    assert abs(entzerrt.shape[1] / entzerrt.shape[0] - w / h) < 0.08
    assert abs(auf.schaetze_winkel(entzerrt)) < 0.6
    assert auf.finde_ecken(bild) is None  # a page that fills the image needs nothing
    v = auf.vorschlag(verzerrt, foto=True)
    assert v.entzerren and v.hintergrund and v.ecken is not None
    assert "entzerrt" in auf.beschreibung(v) and "Hintergrund" in auf.beschreibung(v)


def test_hochskalieren_hintergrund_masken(befund: Path) -> None:
    import numpy as np

    bild, skala = auf.rendere(_seite(befund), 72)
    h, w = bild.shape[:2]
    gross = auf.wende_an(bild, auf.Aufbereitung(hochskalieren=2))
    assert gross.shape[0] == 2 * h and gross.shape[1] == 2 * w
    schatten = bild.astype(np.int16) - np.linspace(0, 90, w, dtype=np.int16)[None, :, None]
    schatten = np.clip(schatten, 0, 255).astype(np.uint8)
    assert auf.hintergrund_ungleich(schatten) and not auf.hintergrund_ungleich(bild)
    sauber = auf.wende_an(schatten, auf.Aufbereitung(hintergrund=True))
    assert sauber.ndim == 2 and np.median(sauber[:, -40:]) > 240  # the shadow is gone
    sw = auf.wende_an(bild, auf.Aufbereitung(schwarzweiss=True))
    assert set(np.unique(sw)) <= {0, 255}
    maskiert = auf.wende_an(bild, auf.Aufbereitung(masken=[(10, 10, 60, 30)]), skala)
    assert maskiert[15:25, 15:55].min() == 255  # the clean-up erases (white)
    geschwaerzt = auf.wende_an(bild, auf.Aufbereitung(masken=[(10, 10, 60, 30)],
                                                     maskenfarbe="schwarz"), skala)
    assert geschwaerzt[15:25, 15:55].max() == 0 and geschwaerzt[100:, 100:].max() == 255
    v = auf.vorschlag(bild, aufloesung=120)
    assert v.hochskalieren == 2 and not v.entzerren and not v.begradigen
    assert v.als_dict()["hochskalieren"] == 2
    assert auf.vorschlag(bild, aufloesung=300).hochskalieren == 1
    assert auf.beschreibung(auf.Aufbereitung()) == "unverändert"


def test_seite_ersetzen(befund: Path, tmp_path: Path) -> None:
    doc = pymupdf.open(befund)
    breite = doc[0].rect.width
    skala = auf.seite_ersetzen(doc, 0, auf.Aufbereitung(begradigen=True, winkel=1.0,
                                                        masken=[(56, 60, 300, 90)]), dpi=100)
    assert skala > 0 and len(doc) == 2 and abs(doc[0].rect.width - breite) < 0.01
    assert doc[0].get_images() and not doc[0].get_text("text").strip()  # an image page now
    pix = doc[0].get_pixmap(clip=pymupdf.Rect(60, 65, 290, 85), colorspace=pymupdf.csGRAY)
    assert min(pix.samples) > 240  # the erased region is white on the new page
    png, skala_v = auf.vorschau_png(doc[1], auf.Aufbereitung(drehung=90), dpi=50)
    assert png.startswith(b"\x89PNG") and skala_v > 0
    gross, skala_g = auf.vorschau_png(doc[1], auf.Aufbereitung(hochskalieren=4), dpi=100)
    assert skala_g > skala_v  # shown enlarged, rendered at a lower dpi to stay bounded
    doc.close()


def test_weitere_schritte(befund: Path) -> None:
    import cv2
    import numpy as np

    bild, skala = auf.rendere(_seite(befund), 100)
    h, w = bild.shape[:2]
    # dark scanner border
    gerahmt = bild.copy()
    gerahmt[:, :25] = 20
    gerahmt[-30:, :] = 30
    assert auf.raender_dunkel(gerahmt) and not auf.raender_dunkel(bild)
    sauber = auf.wende_an(gerahmt, auf.Aufbereitung(raender=True))
    assert sauber[:, :20].min() == 255 and sauber[-20:, :].min() == 255
    assert sauber[100:200, 100:300].min() < 50  # the text is still there
    # a red stamp and a blue signature vanish, black print stays
    gestempelt = bild.copy()
    cv2.rectangle(gestempelt, (400, 600), (700, 700), (40, 40, 220), thickness=-1)
    cv2.line(gestempelt, (100, 900), (600, 960), (200, 60, 30), 8)
    assert auf.farbanteil(gestempelt) > 0.003 > auf.farbanteil(bild)
    ohne = auf.wende_an(gestempelt, auf.Aufbereitung(farbe_entfernen=True))
    assert ohne.ndim == 2 and ohne[620:680, 450:650].min() == 255
    assert ohne[100:200, 100:300].min() < 50
    # salt-and-pepper grain
    rng = np.random.default_rng(1)
    grau = cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY)
    koernig = grau.copy()
    maske = rng.random(grau.shape) < 0.02
    koernig[maske] = 0
    assert auf.rauschanteil(koernig) > 0.35 > auf.rauschanteil(grau)
    entrauscht = auf.wende_an(koernig, auf.Aufbereitung(entrauschen=True))
    assert auf.rauschanteil(entrauscht) < auf.rauschanteil(koernig) / 2
    # contrast and sharpening keep size and type
    for a in (auf.Aufbereitung(kontrast=True), auf.Aufbereitung(schaerfen=True)):
        aus = auf.wende_an(bild, a)
        assert aus.shape[:2] == (h, w) and aus.dtype == np.uint8
    # ruled lines vanish, letters survive
    liniert = bild.copy()
    cv2.line(liniert, (50, 500), (w - 50, 500), (0, 0, 0), 3)
    cv2.line(liniert, (300, 50), (300, h - 50), (0, 0, 0), 3)
    ohne_linien = auf.wende_an(liniert, auf.Aufbereitung(linien_entfernen=True))
    assert ohne_linien[497:504, 400:800].min() == 255
    assert ohne_linien[100:200, 100:300].min() < 50
    # erasing masks are white in the clean-up, black in redaction
    weiss = auf.wende_an(bild, auf.Aufbereitung(masken=[(10, 10, 60, 30)]), skala)
    assert weiss[15:25, 15:55].min() == 255
    schwarz = auf.wende_an(bild, auf.Aufbereitung(masken=[(10, 10, 60, 30)],
                                                 maskenfarbe="schwarz"), skala)
    assert schwarz[15:25, 15:55].max() == 0
    assert "gelöscht" in auf.beschreibung(auf.Aufbereitung(masken=[(0, 0, 1, 1)]))


def test_buchstabenmodus_und_faktoren(befund: Path) -> None:
    import numpy as np

    bild, _ = auf.rendere(_seite(befund), 72)
    h, w = bild.shape[:2]
    for faktor in (2, 3, 4):
        gross = auf.wende_an(bild, auf.Aufbereitung(hochskalieren=faktor, textmodus=True))
        assert gross.shape == (faktor * h, faktor * w) and gross.dtype == np.uint8
        # letter mode: grey image, white paper, dark sharpened strokes
        assert gross.ndim == 2 and np.median(gross) == 255 and gross.min() < 40
    fein = auf.textverfeinerung(bild, faktor=1)
    assert fein.shape == (h, w) and np.median(fein) == 255
    v = auf.vorschlag(bild, aufloesung=100)
    assert v.hochskalieren == 2 and not v.textmodus  # letter mode is never suggested
    assert "Buchstabenmodus" in auf.beschreibung(auf.Aufbereitung(textmodus=True))


def test_ausrichtung(befund: Path) -> None:
    import cv2

    bild, _ = auf.rendere(_seite(befund), 100)
    assert auf.schaetze_ausrichtung(bild) == 0
    quer = cv2.rotate(bild, cv2.ROTATE_90_COUNTERCLOCKWISE)
    assert auf.schaetze_ausrichtung(quer) == 90
    aufrecht = auf.wende_an(quer, auf.Aufbereitung(drehung=90))
    assert aufrecht.shape[:2] == bild.shape[:2]
    from pseudonymed.dokument import ocr as ocrmodul

    if ocrmodul.verfuegbare_backends():
        backend = ocrmodul.backend("auto")
        kopf = cv2.rotate(bild, cv2.ROTATE_180)
        assert auf.schaetze_ausrichtung(kopf, backend) == 180
        assert auf.schaetze_ausrichtung(bild, backend) == 0


def test_winkel_trotz_dunklem_rand(befund: Path) -> None:
    """A dark border that is tilted along with the page must not fool the estimate;
    cleaning runs before and after straightening."""
    import cv2

    bild, _ = auf.rendere(_seite(befund), 100)
    h, w = bild.shape[:2]
    bild[:, :20] = 25  # scanner border, then the whole thing is tilted
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -2.5, 1.0)
    schief = cv2.warpAffine(bild, m, (w, h), borderValue=(255, 255, 255))
    assert abs(auf.schaetze_winkel(schief) - 2.5) <= 0.4
    assert auf.raender_dunkel(schief)
    gerade = auf.wende_an(schief, auf.Aufbereitung(begradigen=True, winkel=2.5, raender=True))
    assert gerade[h // 2 - 100:h // 2 + 100, :12].min() == 255  # the wedge is gone


def test_beschreibung_und_aktiv() -> None:
    """Every step has its phrase, in the listed order; a plan is active once a step would run."""
    A = auf.Aufbereitung
    assert not A().aktiv and not A(hochskalieren=1).aktiv
    for plan in (A(drehung=90), A(hochskalieren=2), A(masken=[(0, 0, 1, 1)]), A(begradigen=True),
                 A(entzerren=True), A(textmodus=True), A(raender=True), A(farbe_entfernen=True),
                 A(entrauschen=True), A(kontrast=True), A(schaerfen=True), A(hintergrund=True),
                 A(linien_entfernen=True), A(schwarzweiss=True)):
        assert plan.aktiv, plan
    assert auf.beschreibung(A(drehung=90)) == "um 90° gedreht"
    assert auf.beschreibung(A(begradigen=True, winkel=2.04)) == "begradigt (+2,0°)"
    assert auf.beschreibung(A(begradigen=True, winkel=0.01)) == "unverändert"
    assert auf.beschreibung(A(entzerren=True)) == "unverändert"  # no corners, nothing to unwarp
    hoch = A(hochskalieren=3, textmodus=True)
    assert auf.beschreibung(hoch) == "3× hochskaliert (Buchstabenmodus)"
    schwarz = A(masken=[(0, 0, 1, 1)], maskenfarbe="schwarz")
    assert auf.beschreibung(schwarz) == "1 Bereich(e) geschwärzt"
    alles = A(drehung=180, begradigen=True, winkel=-1.5, raender=True, farbe_entfernen=True,
              entrauschen=True, kontrast=True, schaerfen=True, hintergrund=True,
              linien_entfernen=True, schwarzweiss=True, masken=[(0, 0, 1, 1), (2, 2, 3, 3)])
    assert auf.beschreibung(alles) == (
        "um 180° gedreht, begradigt (-1,5°), Ränder gesäubert, farbige Tinte entfernt, entrauscht, "
        "Kontrast angehoben, geschärft, Hintergrund geglättet, Linien entfernt, Schwarz-Weiß, "
        "2 Bereich(e) gelöscht")
