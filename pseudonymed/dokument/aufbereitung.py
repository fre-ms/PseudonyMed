"""Image clean-up before OCR: straighten, unwarp, upscale, even out the background.

Scans arrive tilted, photos are taken at an angle under uneven light, and both
are often small.  Every step here is optional, has an automatic suggestion
and is meant to be checked by eye in the desktop's "Aufbereiten" step.  The
steps work on a rendered page (an image); the processed image replaces the
page, so that OCR, redaction and the output all see the same corrected page.
Needs OpenCV and NumPy (they come with the ``ocr`` extra); without them only
masks are available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pymupdf

Ecke = tuple[float, float]
Rechteck = tuple[float, float, float, float]


def verfuegbar() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Aufbereitung:
    """What to do with one page.  Angles in degrees; corners in points of the source
    page (after ``drehung``), masks as ``(x0, y0, x1, y1)`` in points of the result
    page (see :func:`seite_ersetzen`)."""

    drehung: int = 0  # 0, 90, 180, 270 clockwise, applied first
    begradigen: bool = False  # rotate by -winkel
    winkel: float = 0.0  # estimated tilt, positive = text runs downhill to the right
    entzerren: bool = False  # perspective correction with ``ecken``
    ecken: list[Ecke] | None = None  # four corners of the page in the (rotated) image
    hochskalieren: int = 1  # 1 to 4: OCR reads the page at that multiple of the resolution
    textmodus: bool = False  # letter-aware upscaling: Lanczos enlargement, sharpened strokes
    raender: bool = False  # dark scanner borders -> white
    farbe_entfernen: bool = False  # coloured ink (stamps, highlighter) -> white
    entrauschen: bool = False  # median plus non-local means against specks and grain
    entrauschen_staerke: int = 2  # 1 (gentle) to 5 (strong)
    kontrast: bool = False  # CLAHE on faint copies
    kontrast_staerke: int = 2  # 1 to 5
    schaerfen: bool = False  # unsharp mask
    schaerfen_staerke: int = 2  # 1 to 5
    hintergrund: bool = False  # even out illumination, white paper
    linien_entfernen: bool = False  # long ruled and table lines -> white
    schwarzweiss: bool = False  # binarise after that
    masken: list[Rechteck] = field(default_factory=list)  # regions to wipe, result page points
    maskenfarbe: str = "weiss"  # "weiss" erases (clean-up step), "schwarz" redacts

    @property
    def aktiv(self) -> bool:
        return bool(self.drehung or self.begradigen or self.entzerren or self.hochskalieren > 1
                    or self.textmodus or self.raender or self.farbe_entfernen or self.entrauschen
                    or self.kontrast or self.schaerfen or self.hintergrund
                    or self.linien_entfernen or self.schwarzweiss or self.masken)

    def als_dict(self) -> dict[str, Any]:
        return {"drehung": self.drehung, "begradigen": self.begradigen,
                "winkel": round(self.winkel, 2), "entzerren": self.entzerren,
                "hochskalieren": self.hochskalieren, "textmodus": self.textmodus,
                "raender": self.raender, "farbe_entfernen": self.farbe_entfernen,
                "entrauschen": self.entrauschen, "entrauschen_staerke": self.entrauschen_staerke,
                "kontrast": self.kontrast, "kontrast_staerke": self.kontrast_staerke,
                "schaerfen": self.schaerfen, "schaerfen_staerke": self.schaerfen_staerke,
                "hintergrund": self.hintergrund,
                "linien_entfernen": self.linien_entfernen, "schwarzweiss": self.schwarzweiss,
                "masken": len(self.masken), "maskenfarbe": self.maskenfarbe}

    def vollstaendig(self) -> dict[str, Any]:
        """Every field, masks and corners included (the working state)."""
        d = {k: v for k, v in self.__dict__.items() if k not in ("ecken", "masken")}
        d["ecken"] = [list(e) for e in self.ecken] if self.ecken is not None else None
        d["masken"] = [list(m) for m in self.masken]
        return d

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Aufbereitung:
        felder = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if felder.get("ecken") is not None:
            felder["ecken"] = [tuple(e) for e in felder["ecken"]]
        felder["masken"] = [tuple(m) for m in felder.get("masken", [])]
        return cls(**felder)


# -- rendering -----------------------------------------------------------------------------------


def rendere(page: pymupdf.Page, dpi: int = 300, max_pixel: int = 4000) -> tuple[Any, float]:
    """The page as a BGR NumPy image and the pixels-per-point scale."""
    import numpy as np

    skala = dpi / 72.0
    laengste = max(page.rect.width, page.rect.height) * skala
    if laengste > max_pixel:
        skala *= max_pixel / laengste
    pix = page.get_pixmap(matrix=pymupdf.Matrix(skala, skala), alpha=False,
                          colorspace=pymupdf.csRGB)
    bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return bild[:, :, ::-1].copy(), skala


def _grau(bild: Any) -> Any:
    import cv2

    return bild if bild.ndim == 2 else cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY)


def _verkleinert(grau: Any, laengste: int = 1200) -> tuple[Any, float]:
    import cv2

    h, w = grau.shape[:2]
    f = min(1.0, laengste / max(h, w))
    if f < 1.0:
        return cv2.resize(grau, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA), f
    return grau, 1.0


# -- analysis ------------------------------------------------------------------------------------


def schaetze_winkel(bild: Any, maximal: float = 10.0) -> float:
    """Tilt of the text lines in degrees (positive: the lines run downhill to the
    right).  Projection profile: the angle at which the row sums of the binarised,
    rotated image vary most is the one at which the lines are horizontal."""
    import cv2
    import numpy as np

    grau, _ = _verkleinert(_grau(bild), 800)
    _, bw = cv2.threshold(grau, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw = _nur_text(bw)
    h, w = bw.shape
    mitte = (w / 2, h / 2)

    def guete(winkel: float) -> float:
        m = cv2.getRotationMatrix2D(mitte, winkel, 1.0)
        gedreht = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        profil = gedreht.sum(axis=1, dtype=np.float64)
        return float(np.var(profil))

    beste, bester_wert = 0.0, -1.0
    for a in np.arange(-maximal, maximal + 0.01, 0.5):  # coarse, then fine around the best
        g = guete(float(a))
        if g > bester_wert:
            beste, bester_wert = float(a), g
    for a in np.arange(beste - 0.5, beste + 0.51, 0.1):
        g = guete(float(a))
        if g > bester_wert:
            beste, bester_wert = float(a), g
    # ``beste`` is the counter-clockwise correction; the tilt itself has the same sign
    # (content turned clockwise by 2° needs 2° counter-clockwise)
    return round(beste, 2) if abs(beste) >= 0.05 else 0.0


def _nur_text(bw: Any) -> Any:
    """Drop what is not text from a binary image: borders, bars, stamps, photos (large
    or very elongated connected components), so that the line profile is not fooled."""
    import cv2
    import numpy as np

    h, w = bw.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n <= 1:
        return bw
    breiten = stats[:, cv2.CC_STAT_WIDTH]
    hoehen = stats[:, cv2.CC_STAT_HEIGHT]
    flaechen = stats[:, cv2.CC_STAT_AREA]
    schlecht = (breiten > 0.25 * w) | (hoehen > 0.12 * h) | (flaechen > 0.01 * w * h)
    schlecht[0] = False  # the background label
    if not schlecht.any():
        return bw
    aus = bw.copy()
    aus[np.isin(labels, np.flatnonzero(schlecht))] = 0
    return aus if aus.any() else bw


def finde_ecken(bild: Any, skala: float = 1.0) -> list[Ecke] | None:
    """The four corners of a sheet photographed against a background, ordered
    top-left, top-right, bottom-right, bottom-left, in points (pixels / ``skala``);
    None when no convincing quadrilateral is found (a scan that fills the image)."""
    import cv2
    import numpy as np

    grau_voll = _grau(bild)
    grau, f = _verkleinert(grau_voll, 1000)
    h, w = grau.shape
    weich = cv2.GaussianBlur(grau, (5, 5), 0)
    kanten = cv2.Canny(weich, 50, 150)
    kanten = cv2.dilate(kanten, np.ones((3, 3), np.uint8), iterations=2)
    konturen, _ = cv2.findContours(kanten, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    beste: Any = None
    beste_flaeche = 0.0
    for k in sorted(konturen, key=cv2.contourArea, reverse=True)[:10]:
        flaeche = cv2.contourArea(k)
        if flaeche < 0.2 * w * h:
            break
        umfang = cv2.arcLength(k, True)
        naeherung = cv2.approxPolyDP(k, 0.02 * umfang, True)
        if len(naeherung) == 4 and cv2.isContourConvex(naeherung) and flaeche > beste_flaeche:
            beste, beste_flaeche = naeherung.reshape(4, 2).astype(np.float64), flaeche
    if beste is None or beste_flaeche > 0.97 * w * h:
        return None  # nothing, or the whole image: no correction needed
    beste /= f * skala
    summe = beste.sum(axis=1)
    diff = np.diff(beste, axis=1).ravel()
    geordnet = [beste[np.argmin(summe)], beste[np.argmin(diff)], beste[np.argmax(summe)],
                beste[np.argmax(diff)]]
    return [(float(x), float(y)) for x, y in geordnet]


def raender_dunkel(bild: Any, breite: float = 0.04, grenze: int = 90) -> bool:
    """A dark frame around the page (scanner lid, photographed table edge), also when
    it runs at a slant: enough of the edge strip is dark."""
    import numpy as np

    grau, _ = _verkleinert(_grau(bild), 600)
    h, w = grau.shape
    r = max(2, int(min(h, w) * breite))
    streifen = [grau[:r, :], grau[-r:, :], grau[:, :r], grau[:, -r:]]
    return any(float(np.mean(s)) < grenze or float(np.mean(s < 60)) > 0.3 for s in streifen)


def farbanteil(bild: Any) -> float:
    """Share of clearly coloured pixels (stamps, highlighter, coloured print)."""
    import cv2
    import numpy as np

    if bild.ndim != 3:
        return 0.0
    klein, _ = _verkleinert(bild, 600)
    hsv = cv2.cvtColor(klein, cv2.COLOR_BGR2HSV)
    bunt = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60)
    return float(np.mean(bunt))


def rauschanteil(bild: Any) -> float:
    """Share of tiny isolated dark specks among all dark components (grain, dust)."""
    import cv2

    grau, _ = _verkleinert(_grau(bild), 1000)
    _, bw = cv2.threshold(grau, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n <= 1:
        return 0.0
    flaechen = stats[1:, cv2.CC_STAT_AREA]
    return float((flaechen <= 2).sum() / len(flaechen))


def hintergrund_ungleich(bild: Any) -> bool:
    """True when the paper is not evenly white (photo, shadow, grey scan)."""
    import cv2
    import numpy as np

    grau, _ = _verkleinert(_grau(bild), 600)
    hintergrund = cv2.medianBlur(grau, 51)
    return bool(np.std(hintergrund) > 12 or np.median(hintergrund) < 215)


def native_dpi(page: pymupdf.Page) -> float | None:
    """Resolution of the largest image on the page, or None without images."""
    beste: float | None = None
    for info in page.get_image_info():
        bbox = pymupdf.Rect(info["bbox"]) & page.rect
        if bbox.width < 20 or bbox.height < 20:
            continue
        dpi = info["width"] / (bbox.width / 72.0)
        if beste is None or dpi > beste:
            beste = dpi
    return beste


def vorschlag(bild: Any, foto: bool = False, skala: float = 1.0,
              aufloesung: float | None = None) -> Aufbereitung:
    """Automatic suggestion for one page: straighten a visible tilt, unwarp a
    photographed sheet, upscale low-resolution images, even out an uneven background.
    ``skala`` is pixels per point of ``bild``, ``aufloesung`` the native dpi."""
    a = Aufbereitung()
    if foto:
        ecken = finde_ecken(bild, skala)
        if ecken is not None:
            a.ecken, a.entzerren = ecken, True
    grundlage = wende_an(bild, Aufbereitung(entzerren=a.entzerren, ecken=a.ecken), skala) \
        if a.entzerren else bild
    a.winkel = schaetze_winkel(grundlage)
    # OCR engines tolerate a slight tilt, and rotating a noisy scan costs a little;
    # straighten only what the eye notices
    a.begradigen = abs(a.winkel) >= 1.2
    a.hochskalieren = 2 if aufloesung is not None and aufloesung < 150 else 1
    a.textmodus = False  # measured: no gain over plain enlargement, so an explicit choice
    a.hintergrund = foto or hintergrund_ungleich(grundlage)
    a.raender = raender_dunkel(grundlage)
    a.farbe_entfernen = farbanteil(grundlage) > 0.003
    a.entrauschen = rauschanteil(grundlage) > 0.35
    return a


def beschreibung(a: Aufbereitung) -> str:
    """What a page clean-up does, for reports and the interface."""
    teile = []
    if a.drehung:
        teile.append(f"um {a.drehung}° gedreht")
    if a.entzerren and a.ecken:
        teile.append("entzerrt")
    if a.begradigen and abs(a.winkel) >= 0.05:
        teile.append(f"begradigt ({a.winkel:+.1f}°)".replace(".", ","))
    if a.hochskalieren > 1:
        teile.append(f"{a.hochskalieren}× hochskaliert" + (" (Buchstabenmodus)" if a.textmodus
                                                            else ""))
    elif a.textmodus:
        teile.append("Buchstabenmodus")
    if a.raender:
        teile.append("Ränder gesäubert")
    if a.farbe_entfernen:
        teile.append("farbige Tinte entfernt")
    if a.entrauschen:
        teile.append("entrauscht")
    if a.kontrast:
        teile.append("Kontrast angehoben")
    if a.schaerfen:
        teile.append("geschärft")
    if a.hintergrund:
        teile.append("Hintergrund geglättet")
    if a.linien_entfernen:
        teile.append("Linien entfernt")
    if a.schwarzweiss:
        teile.append("Schwarz-Weiß")
    if a.masken:
        teile.append(f"{len(a.masken)} Bereich(e) "
                     + ("gelöscht" if a.maskenfarbe == "weiss" else "geschwärzt"))
    return ", ".join(teile) if teile else "unverändert"


# -- application ---------------------------------------------------------------------------------


def _entzerre(bild: Any, ecken: list[Ecke], skala: float) -> Any:
    import cv2
    import numpy as np

    quelle = np.array(ecken, dtype=np.float32) * skala
    (ol, orr, ur, ul) = quelle
    breite = int(max(np.linalg.norm(orr - ol), np.linalg.norm(ur - ul)))
    hoehe = int(max(np.linalg.norm(ul - ol), np.linalg.norm(ur - orr)))
    if breite < 10 or hoehe < 10:
        return bild
    ziel = np.array([[0, 0], [breite - 1, 0], [breite - 1, hoehe - 1], [0, hoehe - 1]],
                    dtype=np.float32)
    m = cv2.getPerspectiveTransform(quelle, ziel)
    return cv2.warpPerspective(bild, m, (breite, hoehe), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _drehe(bild: Any, winkel: float) -> Any:
    """Rotate by ``winkel`` degrees counter-clockwise around the centre, canvas enlarged
    so that nothing is cut off, white border."""
    import cv2

    h, w = bild.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), winkel, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    weiss = (255, 255, 255) if bild.ndim == 3 else 255
    return cv2.warpAffine(bild, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=weiss)


def _raender_saeubern(bild: Any, grenze: int = 90, maximal: float = 0.15) -> Any:
    """Paint dark strips at the four edges white, as far as a row or column stays dark
    (at most 15 % of the side)."""
    import numpy as np

    grau = _grau(bild)
    h, w = grau.shape
    aus = bild.copy()
    weiss = (255, 255, 255) if aus.ndim == 3 else 255

    def tiefe(reihen: Any, n: int) -> int:
        t = 0
        for r in reihen:
            if float(np.mean(r)) < grenze and t < n:
                t += 1
            else:
                break
        return t

    oben = tiefe((grau[i, :] for i in range(h)), int(h * maximal))
    unten = tiefe((grau[h - 1 - i, :] for i in range(h)), int(h * maximal))
    links = tiefe((grau[:, i] for i in range(w)), int(w * maximal))
    rechts = tiefe((grau[:, w - 1 - i] for i in range(w)), int(w * maximal))
    if oben:
        aus[:oben + 2, :] = weiss
    if unten:
        aus[h - unten - 2:, :] = weiss
    if links:
        aus[:, :links + 2] = weiss
    if rechts:
        aus[:, w - rechts - 2:] = weiss
    return aus


def _farbe_entfernen(bild: Any) -> Any:
    """Coloured ink (stamps, highlighter, signatures in blue) becomes white; black and
    grey print stays.  Returns a grey image."""
    import cv2
    import numpy as np

    if bild.ndim != 3:
        return bild
    hsv = cv2.cvtColor(bild, cv2.COLOR_BGR2HSV)
    bunt = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60)
    bunt = cv2.dilate(bunt.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
    grau = cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY)
    grau[bunt] = 255
    return grau


def _entrausche(bild: Any, staerke: int = 2) -> Any:
    """Median against isolated specks (dust, salt-and-pepper), then non-local means
    against grain; both keep the strokes of letters.  Strength 1 to 5."""
    import cv2

    staerke = max(1, min(5, int(staerke)))
    glatt = cv2.medianBlur(bild, 3 if staerke < 4 else 5)
    h = (3, 5, 8, 11, 15)[staerke - 1]
    if glatt.ndim == 3:
        return cv2.fastNlMeansDenoisingColored(glatt, None, h, h, 7, 21)
    return cv2.fastNlMeansDenoising(glatt, None, h, 7, 21)


def _kontrast(bild: Any, staerke: int = 2) -> Any:
    import cv2

    staerke = max(1, min(5, int(staerke)))
    clahe = cv2.createCLAHE(clipLimit=(1.0, 2.0, 3.0, 4.0, 6.0)[staerke - 1],
                            tileGridSize=(8, 8))
    if bild.ndim == 3:
        lab = cv2.cvtColor(bild, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return clahe.apply(bild)


def _schaerfe(bild: Any, staerke: int = 2) -> Any:
    import cv2

    staerke = max(1, min(5, int(staerke)))
    menge = (0.3, 0.6, 1.0, 1.5, 2.2)[staerke - 1]
    weich = cv2.GaussianBlur(bild, (0, 0), 1.0)
    return cv2.addWeighted(bild, 1.0 + menge, weich, -menge, 0)


def textverfeinerung(grau: Any, faktor: int = 2) -> Any:
    """Letter-aware finish for an upscaled (or large) rendering: grey, strokes sharpened.

    Measured on synthetic scans (RapidOCR): a hard local threshold (Sauvola) after the
    enlargement looked crisp but wrecked the recognition (down to 0.0 on a noisy
    150-dpi scan), whereas Lanczos enlargement plus unsharp masking in grey scale
    matched or beat the plain enlargement.  The recogniser wants grey, not binary.
    """
    return _schaerfe(_grau(grau), 2)


def _hochskaliere(bild: Any, faktor: int, textmodus: bool) -> Any:
    import cv2

    if textmodus:
        grau = _grau(bild)
        gross = cv2.resize(grau, None, fx=faktor, fy=faktor, interpolation=cv2.INTER_LANCZOS4)
        return textverfeinerung(gross, faktor)
    return cv2.resize(bild, None, fx=faktor, fy=faktor, interpolation=cv2.INTER_CUBIC)


def _linien_entfernen(bild: Any) -> Any:
    """Long horizontal and vertical lines (forms, tables, underlines) become white;
    letters are far shorter than the kernels and survive."""
    import cv2
    import numpy as np

    grau = _grau(bild)
    h, w = grau.shape
    _, bw = cv2.threshold(grau, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    waag = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(30, w // 25), 1)))
    senk = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(30, h // 25))))
    linien = cv2.dilate(cv2.bitwise_or(waag, senk), np.ones((3, 3), np.uint8))
    aus = bild.copy()
    aus[linien > 0] = (255, 255, 255) if aus.ndim == 3 else 255
    return aus


def gitterbereiche(bild: Any) -> list[tuple[int, int, int, int]]:
    """Pixel boxes of table grids: connected long horizontal and vertical rules that
    together span at least 15 % of the width and 2 % of the height.  Tesseract's page
    segmentation treats such a grid as graphics and drops the cells; the OCR reads the
    boxes again without their lines (:class:`~pseudonymed.dokument.ocr.TesseractBackend`)."""
    import cv2

    grau = _grau(bild)
    h, w = grau.shape
    _, bw = cv2.threshold(grau, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    waag = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(20, w // 25), 1)))
    senk = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(20, h // 40))))
    linien = cv2.dilate(cv2.bitwise_or(waag, senk), cv2.getStructuringElement(
        cv2.MORPH_RECT, (5, 5)))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(linien, connectivity=8)
    bereiche: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, bb, bh, _area = (int(v) for v in stats[i])
        if bb < w * 0.15 or bh < h * 0.02:
            continue
        if not cv2.countNonZero(waag[y:y + bh, x:x + bb]) or \
                not cv2.countNonZero(senk[y:y + bh, x:x + bb]):
            continue  # a lone rule or a bar, not a grid
        bereiche.append((x, y, x + bb, y + bh))
    return bereiche


def ohne_linien(bild: Any) -> Any:
    """Public wrapper: the image with long rules removed (see :func:`_linien_entfernen`)."""
    return _linien_entfernen(bild)


def _hintergrund(bild: Any) -> Any:
    """Divide by the blurred background: shadows and grey paper become white, ink stays."""
    import cv2
    import numpy as np

    grau = _grau(bild)
    k = max(31, (min(grau.shape) // 20) | 1)
    hinter = cv2.medianBlur(grau, k)
    hinter = np.maximum(hinter, 1)
    norm = cv2.divide(grau.astype(np.float32), hinter.astype(np.float32), scale=255.0)
    return np.clip(norm, 0, 255).astype(np.uint8)


def wende_an(bild: Any, a: Aufbereitung, skala: float = 1.0) -> Any:
    """Apply the steps in their fixed order: rotate, clean borders, unwarp, straighten,
    drop colour, denoise, contrast, upscale (letter mode), sharpen, background, lines,
    black-and-white, masks.  ``skala`` is pixels per point of ``bild`` (corners) and
    of the result (masks)."""
    import cv2
    import numpy as np

    aus = bild
    if a.drehung in (90, 180, 270):
        aus = cv2.rotate(aus, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                               270: cv2.ROTATE_90_COUNTERCLOCKWISE}[a.drehung])
    if a.raender:  # before straightening: the scanner border is still a straight strip
        aus = _raender_saeubern(aus)
    if a.entzerren and a.ecken and len(a.ecken) == 4:
        aus = _entzerre(aus, a.ecken, skala)
    if a.begradigen and abs(a.winkel) >= 0.05:
        aus = _drehe(aus, a.winkel)  # counter-clockwise by the tilt
        if a.raender:  # a border that was tilted is straight now
            aus = _raender_saeubern(aus)
    if a.farbe_entfernen:
        aus = _farbe_entfernen(aus)
    if a.entrauschen:
        aus = _entrausche(aus, a.entrauschen_staerke)
    if a.kontrast:
        aus = _kontrast(aus, a.kontrast_staerke)
    if a.hochskalieren > 1:
        aus = _hochskaliere(aus, a.hochskalieren, a.textmodus)
    elif a.textmodus:
        aus = textverfeinerung(aus, 1)
    if a.schaerfen and not a.textmodus:
        aus = _schaerfe(aus, a.schaerfen_staerke)
    if a.hintergrund and not a.textmodus:
        aus = _hintergrund(aus)
    if a.linien_entfernen:
        aus = _linien_entfernen(aus)
    if a.schwarzweiss and not a.textmodus:
        grau = _grau(aus)
        aus = cv2.adaptiveThreshold(grau, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 15)
    if a.masken:
        if aus.ndim == 2:
            aus = cv2.cvtColor(aus, cv2.COLOR_GRAY2BGR)
        else:
            aus = aus.copy()
        farbe = (255, 255, 255) if a.maskenfarbe == "weiss" else (0, 0, 0)
        for x0, y0, x1, y1 in a.masken:
            cv2.rectangle(aus, (int(x0 * skala), int(y0 * skala)), (int(x1 * skala),
                          int(y1 * skala)), farbe, thickness=-1)
    return np.ascontiguousarray(aus)


def als_png(bild: Any) -> bytes:
    import cv2

    ok, puffer = cv2.imencode(".png", bild)
    if not ok:
        raise ValueError("PNG-Kodierung fehlgeschlagen.")
    return bytes(puffer)


def _verarbeite(page: pymupdf.Page, a: Aufbereitung, dpi: int,
                mit_hochskalieren: bool = True) -> tuple[Any, float, float]:
    """Processed image of ``page``, its width in points and its pixels per point."""
    bild, skala = rendere(page, dpi)
    schritte = Aufbereitung(**{**a.__dict__, "masken": [],
                               "hochskalieren": a.hochskalieren if mit_hochskalieren else 1})
    aus = wende_an(bild, schritte, skala)
    breite = page.rect.width if a.drehung in (0, 180) else page.rect.height
    neue_skala = aus.shape[1] / breite
    if a.masken:
        aus = wende_an(aus, Aufbereitung(masken=a.masken), neue_skala)
    return aus, breite, neue_skala


def seiten_ersetzen(docs: list[pymupdf.Document], nummer: int, a: Aufbereitung,
                    dpi: int = 300) -> float:
    """Replace page ``nummer`` in every document of ``docs`` (copies of the same file)
    by its processed image, rendered once from the first.  The page keeps its width
    in points; the height follows the image.  Returns the pixels per point of the
    new page."""
    # upscaling is not baked into the page (no information gained, the file would bloat);
    # the pipeline renders such pages for OCR at the higher resolution instead
    aus, breite, neue_skala = _verarbeite(docs[0][nummer], a, dpi, mit_hochskalieren=False)
    png = als_png(aus)
    hoehe = aus.shape[0] / neue_skala
    for doc in docs:
        neu = doc.new_page(pno=nummer, width=breite, height=hoehe)
        neu.insert_image(neu.rect, stream=png)
        doc.delete_page(nummer + 1)
    return neue_skala


def seite_ersetzen(doc: pymupdf.Document, nummer: int, a: Aufbereitung, dpi: int = 300) -> float:
    return seiten_ersetzen([doc], nummer, a, dpi)


def vorschau_png(page: pymupdf.Page, a: Aufbereitung, dpi: int = 100,
                 max_pixel: int = 3000) -> tuple[bytes, float]:
    """The processed page as PNG for the before/after view, masks included, and its
    pixels per point.  Upscaling is shown: the rendering resolution is lowered so that
    the enlarged image stays below ``max_pixel`` on its long side; the view scales it
    back to the size of the original, so zooming in reveals the gain."""
    f = max(1, a.hochskalieren)
    if f > 1:
        lang = max(page.rect.width, page.rect.height) / 72.0  # inches
        dpi = max(36, min(dpi, int(max_pixel / (f * lang))))
    aus, _breite, skala = _verarbeite(page, a, dpi, mit_hochskalieren=True)
    return als_png(aus), skala


def _np_zu_pixmap(bild: Any) -> pymupdf.Pixmap:
    import cv2

    rgb = cv2.cvtColor(bild, cv2.COLOR_BGR2RGB) if bild.ndim == 3 else cv2.cvtColor(
        bild, cv2.COLOR_GRAY2RGB)
    rgb = np_contig(rgb)
    return pymupdf.Pixmap(pymupdf.csRGB, rgb.shape[1], rgb.shape[0], rgb.tobytes(), False)


def np_contig(bild: Any) -> Any:
    import numpy as np

    return np.ascontiguousarray(bild)


def schaetze_ausrichtung(bild: Any, backend: Any = None) -> int:
    """Clockwise rotation in degrees that puts the page upright: 0 or 90 from the text
    lines (rows vary most when lines are horizontal); 0 against 180 only with an OCR
    backend, which reads both and keeps the one it is more confident about."""
    import cv2
    import numpy as np

    grau, _ = _verkleinert(_grau(bild), 800)
    _, bw = cv2.threshold(grau, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    waag = float(np.var(bw.sum(axis=1, dtype=np.float64)))
    senk = float(np.var(bw.sum(axis=0, dtype=np.float64)))
    # normalise by length: a page has more columns than rows or the other way round
    waag /= bw.shape[1] ** 2
    senk /= bw.shape[0] ** 2
    grund = 0 if waag >= senk else 90
    if backend is None:
        return grund
    aufrecht = cv2.rotate(bild, cv2.ROTATE_90_CLOCKWISE) if grund == 90 else bild
    klein, f = _verkleinert(aufrecht, 1600)

    def wert(kandidat: Any) -> float:
        woerter = backend.erkenne(_np_zu_pixmap(kandidat), 1.0)
        return sum(w.konfidenz for w in woerter if len(w.text.strip()) >= 3)

    if wert(cv2.rotate(klein, cv2.ROTATE_180)) > wert(klein) * 1.15:
        return (grund + 180) % 360
    return grund


