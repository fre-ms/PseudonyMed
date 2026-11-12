"""OCR for scanned pages and photos, delivered as a :class:`Seitentext`.

Two backends: RapidOCR (pure pip wheels, default when installed, Latin model
for German text) and Tesseract (system binary, ``deu``).  Both return words
with boxes in page points; :func:`seitentext_aus_woerter` turns them into the
same character map the text layer produces, so that recognition and
redaction do not care where the text came from.  Character boxes inside a
word are approximated by dividing the word box evenly.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pymupdf

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.kern.modell import Art, Fundstelle

OCR_DPI = 300
MAX_PIXEL = 4000  # longest side; photos from phones are larger than needed
UNSICHER_KONFIDENZ = 0.6
UNLESBAR = "[unlesbar]"


@dataclass
class OCRWort:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    konfidenz: float = 1.0  # 0..1


class OCRBackend(Protocol):
    name: str

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        """Words of the rendered page; coordinates already divided by ``skala``."""


class OCRFehler(Exception):
    pass


def _rendere(page: pymupdf.Page, dpi: int = OCR_DPI,
             max_pixel: int = MAX_PIXEL) -> tuple[pymupdf.Pixmap, float]:
    """Render the page for OCR; returns the pixmap and the pixels-per-point scale."""
    skala = dpi / 72.0
    laengste = max(page.rect.width, page.rect.height) * skala
    if laengste > max_pixel:
        skala *= max_pixel / laengste
    pix = page.get_pixmap(matrix=pymupdf.Matrix(skala, skala), alpha=False,
                          colorspace=pymupdf.csRGB)
    return pix, skala


def _png(pix: pymupdf.Pixmap) -> bytes:
    return pix.tobytes("png")


def vorverarbeite(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    """Median filter against salt-and-pepper noise; a no-op when Pillow is missing."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return pix
    bild = Image.frombytes("RGB" if pix.n == 3 else "L", (pix.width, pix.height), pix.samples)
    bild = bild.filter(ImageFilter.MedianFilter(3))
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    neu = pymupdf.Pixmap(puffer.getvalue())
    if neu.n != 3 or neu.alpha:
        neu = pymupdf.Pixmap(pymupdf.csRGB, neu) if neu.n != 3 else pymupdf.Pixmap(neu, 0)
    return neu


# -- Tesseract -----------------------------------------------------------------------------------
_TESSERACT_ORTE = (
    "/opt/homebrew/bin/tesseract",  # Homebrew on Apple silicon
    "/usr/local/bin/tesseract",  # Homebrew on Intel, MacPorts, manual installs
    "/usr/bin/tesseract",  # apt, dnf, pacman
    "/snap/bin/tesseract",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def tesseract_programm(pfad: str | None = None) -> str | None:
    """The system's Tesseract: configured path, PATH, then the usual install locations.

    Applications started from the Finder or a desktop menu get a minimal PATH
    without Homebrew or /usr/local, so the fixed locations matter.
    """
    kandidaten = [pfad] if pfad else []
    kandidaten.append(shutil.which("tesseract"))
    kandidaten += list(_TESSERACT_ORTE)
    for k in kandidaten:
        if k and Path(k).is_file():
            return str(k)
    return None


def tesseract_sprachen(programm: str) -> list[str]:
    try:
        lauf = subprocess.run([programm, "--list-langs"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    zeilen = (lauf.stdout + lauf.stderr).splitlines()
    return [z.strip() for z in zeilen if z.strip() and not z.lower().startswith("list of")
            and " " not in z.strip()]


class TesseractBackend:
    """Tesseract with ``--psm 3`` for the page and, when asked for, a second pass for
    tables: Tesseract's layout analysis takes a boxed grid for graphics and drops the
    cells (a bordered medication table came back as a single word).  For every grid
    :func:`aufbereitung.gitterbereiche` finds, the crop is read again without its rules
    as one block (``--psm 6``); the reading with the higher confidence mass wins the box.
    Measured on synthetic bordered tables at 200 dpi: 0 of 45 cell words → 42; pages
    without grids are untouched."""

    name = "tesseract"

    def __init__(self, sprache: str | None = None, programm: str | None = None,
                 tabellen: bool = True) -> None:
        from pseudonymed.konfig import lade_einstellungen

        self.tabellen = tabellen
        einstellungen = lade_einstellungen()
        self.programm = tesseract_programm(programm or einstellungen.tesseract_pfad or None)
        if not self.programm:
            raise OCRFehler("Tesseract ist nicht installiert (Programm 'tesseract' fehlt).")
        gewuenscht = sprache or einstellungen.tesseract_sprachen
        installiert = set(tesseract_sprachen(self.programm))
        if gewuenscht:
            self.sprache = gewuenscht
        else:
            vorhanden = [s for s in ("deu", "eng") if s in installiert]
            self.sprache = "+".join(vorhanden) if vorhanden else "eng"

    @staticmethod
    def verfuegbar() -> bool:
        from pseudonymed.konfig import lade_einstellungen

        return tesseract_programm(lade_einstellungen().tesseract_pfad or None) is not None

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        woerter = self._lauf(_png(pix), "3", skala)
        if self.tabellen:
            woerter = self._tabellen_nachlesen(pix, skala, woerter)
        return woerter

    def _lauf(self, png: bytes, psm: str, skala: float,
              versatz: tuple[float, float] = (0.0, 0.0)) -> list[OCRWort]:
        """One Tesseract run on PNG bytes; ``versatz`` (pixels) places a crop on the page."""
        with tempfile.TemporaryDirectory() as ordner:
            bild = Path(ordner) / "seite.png"
            bild.write_bytes(png)
            try:
                lauf = subprocess.run(
                    [self.programm, str(bild), "-", "-l", self.sprache, "--psm", psm, "tsv"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False, timeout=600,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                raise OCRFehler(f"Tesseract konnte nicht ausgeführt werden: {e}") from e
        if lauf.returncode != 0:
            raise OCRFehler(f"Tesseract meldet Fehler: {lauf.stderr.strip()[:300]}")
        dx, dy = versatz
        woerter: list[OCRWort] = []
        for zeile in csv.DictReader(io.StringIO(lauf.stdout), delimiter="\t",
                                    quoting=csv.QUOTE_NONE):
            text = (zeile.get("text") or "").strip()
            if not text or zeile.get("level") != "5":
                continue
            try:
                x, y = float(zeile["left"]) + dx, float(zeile["top"]) + dy
                w, h = float(zeile["width"]), float(zeile["height"])
                konf = float(zeile.get("conf") or 0) / 100.0
            except (KeyError, ValueError):
                continue
            woerter.append(OCRWort(text, x / skala, y / skala, (x + w) / skala, (y + h) / skala,
                                   max(0.0, konf)))
        return woerter

    def _tabellen_nachlesen(self, pix: pymupdf.Pixmap, skala: float,
                            woerter: list[OCRWort]) -> list[OCRWort]:
        from pseudonymed.dokument import aufbereitung as aufmodul

        if not aufmodul.verfuegbar():
            return woerter
        import cv2
        import numpy as np

        bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        bild = np.ascontiguousarray(bild[:, :, ::-1]) if pix.n == 3 else bild
        rand, polster = 10, 30
        for x0, y0, x1, y1 in aufmodul.gitterbereiche(bild):
            x0, y0 = max(0, x0 - rand), max(0, y0 - rand)
            x1, y1 = min(pix.width, x1 + rand), min(pix.height, y1 + rand)
            ausschnitt = aufmodul.ohne_linien(bild[y0:y1, x0:x1])
            weiss = (255, 255, 255) if ausschnitt.ndim == 3 else 255
            ausschnitt = cv2.copyMakeBorder(ausschnitt, polster, polster, polster, polster,
                                            cv2.BORDER_CONSTANT, value=weiss)
            ok, png = cv2.imencode(".png", ausschnitt)
            if not ok:
                continue
            neu = self._lauf(png.tobytes(), "6", skala, (x0 - polster, y0 - polster))
            kasten = (x0 / skala, y0 / skala, x1 / skala, y1 / skala)
            innen = [w for w in woerter if _innerhalb(w, kasten)]
            if _konfidenzmasse(neu) > _konfidenzmasse(innen):
                woerter = [w for w in woerter if w not in innen] + neu
        return woerter


def _innerhalb(w: OCRWort, kasten: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = kasten
    return w.x0 >= x0 and w.y0 >= y0 and w.x1 <= x1 and w.y1 <= y1


def _konfidenzmasse(woerter: Iterable[OCRWort]) -> float:
    """Confidence weighted by length, letters and digits only: the better of two
    readings of the same box."""
    return sum(w.konfidenz * len(w.text) for w in woerter if any(c.isalnum() for c in w.text))


# -- RapidOCR ------------------------------------------------------------------------------------
class RapidOCRBackend:
    name = "rapidocr"

    def __init__(self, sprache: str | None = "de") -> None:
        try:
            from rapidocr import RapidOCR
        except ImportError as e:
            raise OCRFehler("RapidOCR ist nicht installiert (Extra 'ocr').") from e
        self._RapidOCR = RapidOCR
        self._params = {"Global.log_level": "error"}
        self._engine = None
        self._standard = sprache is None
        if sprache:
            try:
                self._engine = RapidOCR(params={**self._params, "Rec.lang_type": sprache})
            except Exception:
                self._engine = None
        if self._engine is None:
            self._engine = RapidOCR(params=self._params)
            self._standard = True

    @staticmethod
    def verfuegbar() -> bool:
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return False
        return True

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        import numpy as np

        bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 3:
            bild = bild[:, :, ::-1]  # RapidOCR expects BGR like OpenCV
        try:
            ergebnis = self._engine(bild, return_word_box=True)
        except Exception as e:
            # the language model is loaded lazily; unsupported or missing -> bundled default
            if self._standard:
                raise OCRFehler(f"RapidOCR fehlgeschlagen: {e}") from e
            self._engine = self._RapidOCR(params=self._params)
            self._standard = True
            ergebnis = self._engine(bild, return_word_box=True)
        boxen = getattr(ergebnis, "boxes", None)
        if boxen is None:
            return []
        woerter: list[OCRWort] = []
        wortboxen = getattr(ergebnis, "word_results", None) or ()
        for i, (box, text, score) in enumerate(zip(boxen, ergebnis.txts, ergebnis.scores,
                                                   strict=False)):
            if i < len(wortboxen) and wortboxen[i]:
                # real word boxes from the engine
                for wort, wort_score, wbox in wortboxen[i]:
                    xs = [float(pt[0]) for pt in wbox]
                    ys = [float(pt[1]) for pt in wbox]
                    if str(wort).strip():
                        woerter.append(OCRWort(str(wort).strip(), min(xs) / skala,
                                               min(ys) / skala, max(xs) / skala, max(ys) / skala,
                                               float(wort_score)))
                continue
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            zeile = OCRWort(str(text).strip(), min(xs) / skala, min(ys) / skala, max(xs) / skala,
                            max(ys) / skala, float(score))
            woerter.extend(_zerlege_zeile(zeile))
        return woerter


def _zerlege_zeile(zeile: OCRWort) -> list[OCRWort]:
    """RapidOCR returns text lines; split into words with boxes by character share."""
    teile = zeile.text.split()
    if len(teile) <= 1:
        return [zeile] if zeile.text else []
    gesamt = len(zeile.text)
    breite = zeile.x1 - zeile.x0
    woerter: list[OCRWort] = []
    pos = 0
    for teil in teile:
        start = zeile.text.index(teil, pos)
        ende = start + len(teil)
        pos = ende
        woerter.append(OCRWort(teil, zeile.x0 + breite * start / gesamt, zeile.y0,
                               zeile.x0 + breite * ende / gesamt, zeile.y1, zeile.konfidenz))
    return woerter


# -- selection -----------------------------------------------------------------------------------
def backend(name: str = "auto", sprachen: str | None = None,
            tabellen: bool = True) -> OCRBackend | None:
    """Return the requested backend, or for "auto" the configured preference
    ([ocr] backend in the configuration file, PSEUDONYMED_OCR), else the first
    available one; None for "aus".  ``sprachen`` names the language packs:
    "deu+eng" for Tesseract, a RapidOCR language type such as "de" or "latin";
    ``tabellen`` switches Tesseract's second pass for table grids."""
    name = (name or "auto").lower()
    sprachen = (sprachen or "").strip() or None
    if name == "auto":
        from pseudonymed.konfig import lade_einstellungen

        bevorzugt = (lade_einstellungen().ocr_backend or "auto").lower()
        if bevorzugt in ("rapidocr", "tesseract", "aus"):
            name = bevorzugt
    if name == "aus":
        return None
    if name == "rapidocr":
        return RapidOCRBackend(sprachen or "de")
    if name == "tesseract":
        return TesseractBackend(sprache=sprachen, tabellen=tabellen)
    if name != "auto":
        raise OCRFehler(f"Unbekanntes OCR-Backend: {name!r} (auto, rapidocr, tesseract, aus).")
    if RapidOCRBackend.verfuegbar():
        return RapidOCRBackend(sprachen if sprachen and "+" not in sprachen else "de")
    if TesseractBackend.verfuegbar():
        return TesseractBackend(sprache=sprachen if sprachen and "+" in sprachen else None,
                                tabellen=tabellen)
    return None


def verfuegbare_backends() -> list[str]:
    namen = []
    if RapidOCRBackend.verfuegbar():
        namen.append("rapidocr")
    if TesseractBackend.verfuegbar():
        namen.append("tesseract")
    return namen


# -- words -> Seitentext ---------------------------------------------------------------------------
def seitentext_aus_woerter(
    woerter: Iterable[OCRWort], nummer: int, breite: float, hoehe: float
) -> Seitentext:
    """Group words into lines by vertical overlap, order them, and build the character map.
    A gap between neighbouring words far wider than usual (table cells, columns) becomes
    a tab in the page text: the rules treat it like a space, the Markdown structuring
    reads it as a cell boundary."""
    seite = Seitentext(nummer=nummer, breite=breite, hoehe=hoehe, quelle="ocr")
    rest = sorted((w for w in woerter if w.text), key=lambda w: (w.y0, w.x0))
    zeilen: list[list[OCRWort]] = []
    for w in rest:
        for zeile in zeilen:
            ref = zeile[0]
            mitte = (w.y0 + w.y1) / 2
            if ref.y0 - 1 <= mitte <= ref.y1 + 1 and abs((w.y1 - w.y0) - (ref.y1 - ref.y0)) < \
                    max(w.y1 - w.y0, ref.y1 - ref.y0):
                zeile.append(w)
                break
        else:
            zeilen.append([w])
    zeilen.sort(key=lambda z: min(w.y0 for w in z))
    for zeile in zeilen:
        zeile.sort(key=lambda w: w.x0)
    luecken = sorted(b.x0 - a.x1 for z in zeilen for a, b in zip(z, z[1:], strict=False)
                     if b.x0 > a.x1)
    uebliche_luecke = luecken[len(luecken) // 2] if luecken else 0.0
    for nr, zeile in enumerate(zeilen, 1):
        for i, w in enumerate(zeile):
            if i > 0:
                luecke = w.x0 - zeile[i - 1].x1
                zellgrenze = len(luecken) >= 4 and \
                    luecke > max(1.5 * (w.y1 - w.y0), 3.0 * uebliche_luecke)
                seite._fuege_an("\t" if zellgrenze else " ", None, nr, w.y1 - w.y0, w.y1)
            n = max(len(w.text), 1)
            schritt = (w.x1 - w.x0) / n
            groesse = max(w.y1 - w.y0, 1.0) * 0.8
            start = len(seite.text)
            for k, c in enumerate(w.text):
                box = (w.x0 + k * schritt, w.y0, w.x0 + (k + 1) * schritt, w.y1)
                seite._fuege_an(c, box, nr, groesse, w.y1 - 0.2 * (w.y1 - w.y0))
            seite.konfidenzen.append(w.konfidenz)
            seite.wortspannen.append((start, len(seite.text)))
        seite._fuege_an("\n", None, nr, 0.0, 0.0)
    return seite


def _wort_unsicher(text: str, konfidenz: float) -> bool:
    kern = text.strip(",.;:()*\"'")
    if konfidenz < UNSICHER_KONFIDENZ:
        return True
    # a lone letter in the middle of a line is a fragment of a word OCR could not read
    return len(kern) == 1 and kern.isalpha() and konfidenz < 0.995


def unlesbare_regionen(seite: Seitentext, mindestanteil: float = 0.4) -> list[Fundstelle]:
    """Lines OCR could not read (many uncertain words or fragments) as hits to blank.

    What cannot be read cannot be checked: such a line may hold a name, a date
    or handwriting.  The whole line is covered and marked "[unlesbar]".
    """
    if seite.quelle != "ocr" or not seite.wortspannen:
        return []
    je_zeile: dict[int, list[tuple[int, int, bool]]] = {}
    for (a, b), konf in zip(seite.wortspannen, seite.konfidenzen, strict=True):
        z = seite.zeile_von(a)
        je_zeile.setdefault(z, []).append((a, b, _wort_unsicher(seite.text[a:b], konf)))
    treffer: list[Fundstelle] = []
    for woerter in je_zeile.values():
        unsicher = [w for w in woerter if w[2]]
        if len(unsicher) >= 3 or (unsicher and len(unsicher) / len(woerter) >= mindestanteil):
            if len(unsicher) >= 2 or len(woerter) == 1:
                # the whole line: confidently "read" fragments next to garbage are garbage too
                a, b = woerter[0][0], woerter[-1][1]
                treffer.append(Fundstelle(
                    seite=seite.nummer, start=a, ende=b, text=seite.text[a:b], art=Art.MANUELL,
                    regel="ocr.unlesbar", pseudonym=UNLESBAR,
                    rects=[tuple(r) for r in seite.rects(a, b)],
                    schriftgroesse=seite.schriftgroesse(a, b), quelle="ocr",
                ))
    return treffer


def erkenne_seite(page: pymupdf.Page, backend_: OCRBackend, nummer: int | None = None,
                  dpi: int = OCR_DPI, vorverarbeitung: bool = True,
                  max_pixel: int = MAX_PIXEL, filter_=None) -> Seitentext:
    """OCR of one page.  ``filter_`` (NumPy BGR image -> image) runs on the rendering
    before the engine, for the letter-aware finish of upscaled pages."""
    pix, skala = _rendere(page, dpi, max_pixel)
    if vorverarbeitung:
        pix = vorverarbeite(pix)
    if filter_ is not None:
        import numpy as np

        bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        bild = filter_(np.ascontiguousarray(bild[:, :, ::-1]) if pix.n == 3 else bild)
        if bild.ndim == 2:
            bild = np.repeat(bild[:, :, None], 3, axis=2)
        else:
            bild = bild[:, :, ::-1]
        bild = np.ascontiguousarray(bild)
        pix = pymupdf.Pixmap(pymupdf.csRGB, bild.shape[1], bild.shape[0], bild.tobytes(), False)
    woerter = backend_.erkenne(pix, skala)
    nummer = page.number if nummer is None else nummer
    return seitentext_aus_woerter(woerter, nummer, page.rect.width, page.rect.height)


def braucht_ocr(page: pymupdf.Page, textebene: Seitentext, mindestanteil: float = 0.2) -> bool:
    """True without a text layer, or when images cover a good part of the page
    (a scanned letter embedded in a PDF with a text layer)."""
    if not textebene.hat_textebene:
        return True
    flaeche = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if r.width > 40 and r.height > 40:
            flaeche += r.width * r.height
    return flaeche >= mindestanteil * page.rect.width * page.rect.height
