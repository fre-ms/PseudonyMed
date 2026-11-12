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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import pymupdf

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.kern.modell import Art, Fundstelle

if TYPE_CHECKING:
    from rapidocr import RapidOCR

OCR_DPI = 300
MAX_PIXEL = 4000  # longest side; photos from phones are larger than needed
UNSICHER_KONFIDENZ = 0.6
UNLESBAR = "[unlesbar]"

Kasten = tuple[float, float, float, float]


@dataclass
class OCRWort:
    """One recognised word: its text, its box in page points and the engine's confidence."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    konfidenz: float = 1.0  # 0..1


class OCRBackend(Protocol):
    """An OCR engine: turns a rendered page into words with boxes."""

    name: str

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        """Words of the rendered page; coordinates already divided by ``skala``."""


class OCRFehler(Exception):
    """An engine is missing, cannot be started, or failed on a page."""


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
    """The pixmap as PNG bytes, the form Tesseract reads from a file."""
    return pix.tobytes("png")


def _bild_aus_pixmap(pix: pymupdf.Pixmap) -> Any:
    """The pixmap as a NumPy array, in BGR order for colour (what OpenCV and RapidOCR
    expect); a grey pixmap keeps its single channel."""
    import numpy as np

    bild = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.ascontiguousarray(bild[:, :, ::-1]) if pix.n == 3 else bild


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
    kandidaten: list[str | None] = [pfad] if pfad else []
    kandidaten.append(shutil.which("tesseract"))
    kandidaten += list(_TESSERACT_ORTE)
    for k in kandidaten:
        if k and Path(k).is_file():
            return str(k)
    return None


def tesseract_sprachen(programm: str) -> list[str]:
    """The language packs ``programm --list-langs`` reports; empty when it cannot run."""
    try:
        lauf = subprocess.run([programm, "--list-langs"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    zeilen = (lauf.stdout + lauf.stderr).splitlines()
    return [z.strip() for z in zeilen if z.strip() and not z.lower().startswith("list of")
            and " " not in z.strip()]


def _woerter_aus_tsv(tsv: str, skala: float, versatz: tuple[float, float]) -> list[OCRWort]:
    """Tesseract's TSV output as words in page points: the word rows (level 5) with
    text, shifted by ``versatz`` pixels; rows with unreadable numbers are skipped."""
    dx, dy = versatz
    woerter: list[OCRWort] = []
    for zeile in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
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
        """Locate the program and settle the language packs: the ones given, else the
        configured ones, else whatever of ``deu`` and ``eng`` is installed (``eng`` as
        the last resort).  Raises :class:`OCRFehler` without a program."""
        from pseudonymed.konfig import lade_einstellungen

        self.tabellen = tabellen
        einstellungen = lade_einstellungen()
        gefunden = tesseract_programm(programm or einstellungen.tesseract_pfad or None)
        if not gefunden:
            raise OCRFehler("Tesseract ist nicht installiert (Programm 'tesseract' fehlt).")
        self.programm = gefunden
        gewuenscht = sprache or einstellungen.tesseract_sprachen
        installiert = set(tesseract_sprachen(self.programm))
        if gewuenscht:
            self.sprache = gewuenscht
        else:
            vorhanden = [s for s in ("deu", "eng") if s in installiert]
            self.sprache = "+".join(vorhanden) if vorhanden else "eng"

    @staticmethod
    def verfuegbar() -> bool:
        """True when a Tesseract program is found (configured path, PATH or usual places)."""
        from pseudonymed.konfig import lade_einstellungen

        return tesseract_programm(lade_einstellungen().tesseract_pfad or None) is not None

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        """Words of the page from the ``--psm 3`` run, revised inside table grids when
        the second pass is switched on."""
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
        return _woerter_aus_tsv(lauf.stdout, skala, versatz)

    def _tabellen_nachlesen(self, pix: pymupdf.Pixmap, skala: float,
                            woerter: list[OCRWort]) -> list[OCRWort]:
        """The second pass: every table grid on the page is read again without its
        rules as one block, and the better reading replaces the words in the grid's box.
        Needs OpenCV; without it the words come back unchanged."""
        from pseudonymed.dokument import aufbereitung as aufmodul

        if not aufmodul.verfuegbar():
            return woerter
        bild = _bild_aus_pixmap(pix)
        rand, polster = 10, 30
        for gitter in aufmodul.gitterbereiche(bild):
            x0, y0, x1, y1 = _mit_rand(gitter, rand, pix.width, pix.height)
            png = _gitter_png(bild[y0:y1, x0:x1], polster)
            if png is None:
                continue
            neu = self._lauf(png, "6", skala, (x0 - polster, y0 - polster))
            kasten = (x0 / skala, y0 / skala, x1 / skala, y1 / skala)
            woerter = _bessere_lesung(woerter, neu, kasten)
        return woerter


def _mit_rand(gitter: tuple[int, int, int, int], rand: int, breite: int,
              hoehe: int) -> tuple[int, int, int, int]:
    """The grid's pixel box widened by ``rand`` on every side and clamped to the image."""
    x0, y0, x1, y1 = gitter
    return max(0, x0 - rand), max(0, y0 - rand), min(breite, x1 + rand), min(hoehe, y1 + rand)


def _gitter_png(ausschnitt: Any, polster: int) -> bytes | None:
    """The crop of a table grid as PNG for the second pass: rules removed and a white
    border of ``polster`` pixels added, so Tesseract sees a free-standing text block.
    None when OpenCV cannot encode the image."""
    import cv2

    from pseudonymed.dokument import aufbereitung as aufmodul

    ohne = aufmodul.ohne_linien(ausschnitt)
    weiss = (255, 255, 255) if ohne.ndim == 3 else 255
    ohne = cv2.copyMakeBorder(ohne, polster, polster, polster, polster, cv2.BORDER_CONSTANT,
                              value=weiss)
    ok, png = cv2.imencode(".png", ohne)
    return png.tobytes() if ok else None


def _bessere_lesung(woerter: list[OCRWort], neu: list[OCRWort], kasten: Kasten) -> list[OCRWort]:
    """The page words with those inside ``kasten`` replaced by ``neu`` when the new
    reading carries the higher confidence mass; otherwise the words unchanged."""
    innen = [w for w in woerter if _innerhalb(w, kasten)]
    if _konfidenzmasse(neu) <= _konfidenzmasse(innen):
        return woerter
    return [w for w in woerter if w not in innen] + neu


def _innerhalb(w: OCRWort, kasten: Kasten) -> bool:
    """True when the word's box lies entirely inside ``kasten`` (page points)."""
    x0, y0, x1, y1 = kasten
    return w.x0 >= x0 and w.y0 >= y0 and w.x1 <= x1 and w.y1 <= y1


def _konfidenzmasse(woerter: Iterable[OCRWort]) -> float:
    """Confidence weighted by length, letters and digits only: the better of two
    readings of the same box."""
    return sum(w.konfidenz * len(w.text) for w in woerter if any(c.isalnum() for c in w.text))


# -- RapidOCR ------------------------------------------------------------------------------------
class RapidOCRBackend:
    """RapidOCR (ONNX models from the wheel, no download) with the language model asked
    for; the bundled default model takes over when the language model is unknown to
    the installed version or fails on first use."""

    name = "rapidocr"

    def __init__(self, sprache: str | None = "de") -> None:
        """Start the engine for ``sprache`` (a RapidOCR ``lang_type``), else the default."""
        try:
            from rapidocr import RapidOCR
        except ImportError as e:
            raise OCRFehler("RapidOCR ist nicht installiert (Extra 'ocr').") from e
        self._RapidOCR = RapidOCR
        self._params = {"Global.log_level": "error"}
        sprachmodell = self._sprachmodell(sprache)
        self._standard = sprachmodell is None
        self._engine = sprachmodell if sprachmodell is not None else RapidOCR(params=self._params)

    def _sprachmodell(self, sprache: str | None) -> RapidOCR | None:
        """The engine with ``Rec.lang_type`` set, or None when no language was asked
        for or RapidOCR refuses it at start-up."""
        if not sprache:
            return None
        try:
            return self._RapidOCR(params={**self._params, "Rec.lang_type": sprache})
        except Exception:
            return None

    @staticmethod
    def verfuegbar() -> bool:
        """True when the ``rapidocr`` package can be imported."""
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return False
        return True

    def erkenne(self, pix: pymupdf.Pixmap, skala: float) -> list[OCRWort]:
        """Words of the page: the engine's own word boxes where it gives them, else its
        text lines split into words by character share."""
        return _woerter_aus_ergebnis(self._erkennung(_bild_aus_pixmap(pix)), skala)

    def _erkennung(self, bild: Any) -> Any:
        """The engine's result for the image.  The language model is loaded lazily;
        when it is unsupported or missing, the first page fails and the bundled default
        model takes over for good."""
        try:
            return self._engine(bild, return_word_box=True)
        except Exception as e:
            if self._standard:
                raise OCRFehler(f"RapidOCR fehlgeschlagen: {e}") from e
        self._engine = self._RapidOCR(params=self._params)
        self._standard = True
        return self._engine(bild, return_word_box=True)


def _woerter_aus_ergebnis(ergebnis: Any, skala: float) -> list[OCRWort]:
    """RapidOCR's output as words in page points: for every text line the engine's
    word boxes when it reports them, otherwise the line box split by character share."""
    boxen = getattr(ergebnis, "boxes", None)
    if boxen is None:
        return []
    wortboxen = getattr(ergebnis, "word_results", None) or ()
    woerter: list[OCRWort] = []
    for i, (box, text, score) in enumerate(zip(boxen, ergebnis.txts, ergebnis.scores,
                                               strict=False)):
        if i < len(wortboxen) and wortboxen[i]:
            woerter.extend(_woerter_aus_wortboxen(wortboxen[i], skala))
        else:
            zeile = OCRWort(str(text).strip(), *_grenzen(box, skala), float(score))
            woerter.extend(_zerlege_zeile(zeile))
    return woerter


def _grenzen(punkte: Iterable[Any], skala: float) -> Kasten:
    """Bounding box of a polygon in page points: pixel corners divided by ``skala``."""
    xs = [float(pt[0]) for pt in punkte]
    ys = [float(pt[1]) for pt in punkte]
    return min(xs) / skala, min(ys) / skala, max(xs) / skala, max(ys) / skala


def _woerter_aus_wortboxen(wortboxen: Iterable[Any], skala: float) -> list[OCRWort]:
    """The engine's ``(word, score, polygon)`` triples of one line as words, blanks dropped."""
    return [OCRWort(str(wort).strip(), *_grenzen(wbox, skala), float(wort_score))
            for wort, wort_score, wbox in wortboxen if str(wort).strip()]


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
_BACKEND_NAMEN = ("rapidocr", "tesseract", "aus")


def _bevorzugtes_backend(name: str) -> str:
    """The name "auto" resolved to the configured preference ([ocr] backend,
    PSEUDONYMED_OCR) when that names an engine or "aus"; any other name unchanged."""
    if name != "auto":
        return name
    from pseudonymed.konfig import lade_einstellungen

    bevorzugt = (lade_einstellungen().ocr_backend or "auto").lower()
    return bevorzugt if bevorzugt in _BACKEND_NAMEN else name


def _erstes_verfuegbare(sprachen: str | None, tabellen: bool) -> OCRBackend | None:
    """RapidOCR when installed, else Tesseract, else None.  ``sprachen`` only reaches
    the engine whose notation it uses: "+" joins Tesseract packs, a single word is a
    RapidOCR language type."""
    if RapidOCRBackend.verfuegbar():
        return RapidOCRBackend(sprachen if sprachen and "+" not in sprachen else "de")
    if TesseractBackend.verfuegbar():
        return TesseractBackend(sprache=sprachen if sprachen and "+" in sprachen else None,
                                tabellen=tabellen)
    return None


def backend(name: str = "auto", sprachen: str | None = None,
            tabellen: bool = True) -> OCRBackend | None:
    """Return the requested backend, or for "auto" the configured preference
    ([ocr] backend in the configuration file, PSEUDONYMED_OCR), else the first
    available one; None for "aus".  ``sprachen`` names the language packs:
    "deu+eng" for Tesseract, a RapidOCR language type such as "de" or "latin";
    ``tabellen`` switches Tesseract's second pass for table grids."""
    name = _bevorzugtes_backend((name or "auto").lower())
    sprachen = (sprachen or "").strip() or None
    wahl: dict[str, Callable[[], OCRBackend | None]] = {
        "aus": lambda: None,
        "rapidocr": lambda: RapidOCRBackend(sprachen or "de"),
        "tesseract": lambda: TesseractBackend(sprache=sprachen, tabellen=tabellen),
        "auto": lambda: _erstes_verfuegbare(sprachen, tabellen),
    }
    if name not in wahl:
        raise OCRFehler(f"Unbekanntes OCR-Backend: {name!r} (auto, rapidocr, tesseract, aus).")
    return wahl[name]()


def verfuegbare_backends() -> list[str]:
    """Names of the installed engines in order of preference: "rapidocr", "tesseract"."""
    namen = []
    if RapidOCRBackend.verfuegbar():
        namen.append("rapidocr")
    if TesseractBackend.verfuegbar():
        namen.append("tesseract")
    return namen


# -- words -> Seitentext ---------------------------------------------------------------------------
class _Lueckenmass(NamedTuple):
    """How many gaps between neighbouring words the page has, and the usual (median)
    gap width in points."""

    anzahl: int
    ueblich: float

    def ist_zellgrenze(self, luecke: float, zeilenhoehe: float) -> bool:
        """A gap far wider than usual (table cells, columns); judged only once the
        page has enough gaps for the usual width to mean something."""
        return self.anzahl >= 4 and luecke > max(1.5 * zeilenhoehe, 3.0 * self.ueblich)


def _gleiche_zeile(w: OCRWort, ref: OCRWort) -> bool:
    """True when the word's vertical centre falls into the reference word's band and
    both heights are of the same order, so a heading is not pulled into a body line."""
    mitte = (w.y0 + w.y1) / 2
    hoehe, ref_hoehe = w.y1 - w.y0, ref.y1 - ref.y0
    return ref.y0 - 1 <= mitte <= ref.y1 + 1 and abs(hoehe - ref_hoehe) < max(hoehe, ref_hoehe)


def _zeilen_aus_woertern(woerter: Iterable[OCRWort]) -> list[list[OCRWort]]:
    """Words grouped into lines by vertical overlap with each line's first word, the
    lines top to bottom and the words of a line left to right; blank words dropped."""
    zeilen: list[list[OCRWort]] = []
    for w in sorted((w for w in woerter if w.text), key=lambda w: (w.y0, w.x0)):
        zeile = next((z for z in zeilen if _gleiche_zeile(w, z[0])), None)
        if zeile is None:
            zeilen.append([w])
        else:
            zeile.append(w)
    zeilen.sort(key=lambda z: min(w.y0 for w in z))
    for zeile in zeilen:
        zeile.sort(key=lambda w: w.x0)
    return zeilen


def _lueckenmass(zeilen: list[list[OCRWort]]) -> _Lueckenmass:
    """Count and median of the positive gaps between neighbouring words of a line."""
    luecken = sorted(b.x0 - a.x1 for z in zeilen for a, b in zip(z, z[1:], strict=False)
                     if b.x0 > a.x1)
    return _Lueckenmass(len(luecken), luecken[len(luecken) // 2] if luecken else 0.0)


def _fuege_wort_an(seite: Seitentext, w: OCRWort, nr: int) -> None:
    """Append the word's characters to line ``nr`` with boxes from an even split of the
    word box, then record the word's confidence and its span in the page text."""
    n = max(len(w.text), 1)
    schritt = (w.x1 - w.x0) / n
    groesse = max(w.y1 - w.y0, 1.0) * 0.8
    basislinie = w.y1 - 0.2 * (w.y1 - w.y0)
    start = len(seite.text)
    for k, c in enumerate(w.text):
        box = (w.x0 + k * schritt, w.y0, w.x0 + (k + 1) * schritt, w.y1)
        seite._fuege_an(c, box, nr, groesse, basislinie)
    seite.konfidenzen.append(w.konfidenz)
    seite.wortspannen.append((start, len(seite.text)))


def seitentext_aus_woerter(
    woerter: Iterable[OCRWort], nummer: int, breite: float, hoehe: float
) -> Seitentext:
    """Group words into lines by vertical overlap, order them, and build the character map.
    A gap between neighbouring words far wider than usual (table cells, columns) becomes
    a tab in the page text: the rules treat it like a space, the Markdown structuring
    reads it as a cell boundary."""
    seite = Seitentext(nummer=nummer, breite=breite, hoehe=hoehe, quelle="ocr")
    zeilen = _zeilen_aus_woertern(woerter)
    mass = _lueckenmass(zeilen)
    for nr, zeile in enumerate(zeilen, 1):
        for i, w in enumerate(zeile):
            if i > 0:
                zellgrenze = mass.ist_zellgrenze(w.x0 - zeile[i - 1].x1, w.y1 - w.y0)
                seite._fuege_an("\t" if zellgrenze else " ", None, nr, w.y1 - w.y0, w.y1)
            _fuege_wort_an(seite, w, nr)
        seite._fuege_an("\n", None, nr, 0.0, 0.0)
    return seite


# -- unreadable lines ------------------------------------------------------------------------------
class _Wortlage(NamedTuple):
    """An OCR word's offsets in the page text and whether it counts as uncertain."""

    start: int
    ende: int
    unsicher: bool


def _wort_unsicher(text: str, konfidenz: float) -> bool:
    """True below the confidence threshold, or for a lone letter (punctuation aside)
    that is not read with near certainty: a fragment of a word OCR could not read."""
    kern = text.strip(",.;:()*\"'")
    if konfidenz < UNSICHER_KONFIDENZ:
        return True
    # a lone letter in the middle of a line is a fragment of a word OCR could not read
    return len(kern) == 1 and kern.isalpha() and konfidenz < 0.995


def _woerter_je_zeile(seite: Seitentext) -> dict[int, list[_Wortlage]]:
    """The page's OCR words grouped by line, in text order, each judged for certainty."""
    je_zeile: dict[int, list[_Wortlage]] = {}
    for (a, b), konf in zip(seite.wortspannen, seite.konfidenzen, strict=True):
        lage = _Wortlage(a, b, _wort_unsicher(seite.text[a:b], konf))
        je_zeile.setdefault(seite.zeile_von(a), []).append(lage)
    return je_zeile


def _zeile_unlesbar(woerter: list[_Wortlage], mindestanteil: float) -> bool:
    """True for a line with at least three uncertain words, or with a share of them
    at or above ``mindestanteil``; a single uncertain word only counts when it is the
    whole line.  Confidently "read" fragments next to garbage are garbage too, so the
    whole line is taken."""
    unsicher = sum(1 for w in woerter if w.unsicher)
    if unsicher == 0:
        return False
    viele = unsicher >= 3 or unsicher / len(woerter) >= mindestanteil
    return viele and (unsicher >= 2 or len(woerter) == 1)


def _unlesbar_treffer(seite: Seitentext, a: int, b: int) -> Fundstelle:
    """The hit that blanks the page text ``[a, b)`` of an unreadable line."""
    return Fundstelle(
        seite=seite.nummer, start=a, ende=b, text=seite.text[a:b], art=Art.MANUELL,
        regel="ocr.unlesbar", pseudonym=UNLESBAR,
        rects=[tuple(r) for r in seite.rects(a, b)],
        schriftgroesse=seite.schriftgroesse(a, b), quelle="ocr",
    )


def unlesbare_regionen(seite: Seitentext, mindestanteil: float = 0.4) -> list[Fundstelle]:
    """Lines OCR could not read (many uncertain words or fragments) as hits to blank.

    What cannot be read cannot be checked: such a line may hold a name, a date
    or handwriting.  The whole line is covered and marked "[unlesbar]".
    """
    if seite.quelle != "ocr" or not seite.wortspannen:
        return []
    return [_unlesbar_treffer(seite, woerter[0].start, woerter[-1].ende)
            for woerter in _woerter_je_zeile(seite).values()
            if _zeile_unlesbar(woerter, mindestanteil)]


# -- page ------------------------------------------------------------------------------------------
def _gefiltert(pix: pymupdf.Pixmap, filter_: Callable[[Any], Any]) -> pymupdf.Pixmap:
    """The rendering after the NumPy filter, back as an RGB pixmap; a grey result is
    spread over three channels."""
    import numpy as np

    bild = filter_(_bild_aus_pixmap(pix))
    if bild.ndim == 2:
        bild = np.repeat(bild[:, :, None], 3, axis=2)
    else:
        bild = bild[:, :, ::-1]
    bild = np.ascontiguousarray(bild)
    return pymupdf.Pixmap(pymupdf.csRGB, bild.shape[1], bild.shape[0], bild.tobytes(), False)


def erkenne_seite(page: pymupdf.Page, backend_: OCRBackend, nummer: int | None = None,
                  dpi: int = OCR_DPI, vorverarbeitung: bool = True,
                  max_pixel: int = MAX_PIXEL, filter_=None) -> Seitentext:
    """OCR of one page.  ``filter_`` (NumPy BGR image -> image) runs on the rendering
    before the engine, for the letter-aware finish of upscaled pages."""
    pix, skala = _rendere(page, dpi, max_pixel)
    if vorverarbeitung:
        pix = vorverarbeite(pix)
    if filter_ is not None:
        pix = _gefiltert(pix, filter_)
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
