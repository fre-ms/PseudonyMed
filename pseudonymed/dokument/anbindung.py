"""Where PseudonyMed meets optdoc, the document layer it builds on.

optdoc loads, cleans up and reads the pages (text layer and OCR); its names and messages
are English.  PseudonyMed keeps its German vocabulary at the edges: the engine "aus", the
paper size "bild", German messages for the settings a user can get wrong, the German
wording of a clean-up in reports, and its own hits for the lines OCR could not read.  The
settings optdoc reads (which engine "auto" means, where Tesseract lives) are PseudonyMed's
configuration, handed over on import of this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pymupdf
from optdoc import cleanup, loading, ocr
from optdoc.pagetext import PageText
from optdoc.settings import SettingsLike, set_settings

from pseudonymed.kern.modell import Art, Fundstelle

UNLESBAR = "[unlesbar]"  # what a line OCR could not read becomes in the output

OCRFehler = ocr.OCRError  # raised by the engines; the selection below speaks German

# PseudonyMed's names for the engines and paper sizes, where they differ from optdoc's
_ENGINES = {"auto": "auto", "rapidocr": "rapidocr", "tesseract": "tesseract", "aus": "none"}
_PAPIER = {"bild": "image"}

PAPIERFORMATE = ("a4", "a5", "a3", "letter", "legal", "bild")


class _Einstellungen:
    """PseudonyMed's settings as optdoc reads them, looked up anew on every access so that a
    change on the settings page applies to the next reading without a restart."""

    @property
    def ocr_backend(self) -> str:
        """The engine "auto" stands for, in optdoc's spelling ("aus" becomes "none")."""
        from pseudonymed.konfig import lade_einstellungen

        name = (lade_einstellungen().ocr_backend or "auto").lower()
        return _ENGINES.get(name, name)

    @property
    def tesseract_path(self) -> str:
        """The configured Tesseract program, empty to search for it."""
        from pseudonymed.konfig import lade_einstellungen

        return lade_einstellungen().tesseract_pfad

    @property
    def tesseract_languages(self) -> str:
        """The configured Tesseract language packs, empty for the default."""
        from pseudonymed.konfig import lade_einstellungen

        return lade_einstellungen().tesseract_sprachen


# optdoc's protocol declares plain attributes, which mypy takes as writable; it only reads
# them, and read-only properties are what the live lookup needs
set_settings(cast(SettingsLike, _Einstellungen()))


def _nicht_installiert(name: str) -> str | None:
    """The German message for an engine that was asked for by name but is missing."""
    if name == "rapidocr" and not ocr.RapidOCRBackend.available():
        return "RapidOCR ist nicht installiert (Extra 'ocr')."
    if name == "tesseract" and not ocr.TesseractBackend.available():
        return "Tesseract ist nicht installiert (Programm 'tesseract' fehlt)."
    return None


def backend(name: str = "auto", sprachen: str | None = None,
            tabellen: bool = True) -> ocr.OCRBackend | None:
    """The OCR engine for PseudonyMed's name of it ("auto", "rapidocr", "tesseract", "aus");
    None for "aus" and for "auto" without an engine.  Unknown and missing engines end in an
    :class:`OCRFehler` with a German message."""
    gewuenscht = (name or "auto").lower()
    if gewuenscht not in _ENGINES:
        raise OCRFehler(f"Unbekanntes OCR-Backend: {name!r} (auto, rapidocr, tesseract, aus).")
    fehlt = _nicht_installiert(gewuenscht)
    if fehlt:
        raise OCRFehler(fehlt)
    return ocr.backend(_ENGINES[gewuenscht], sprachen, tables=tabellen)


def papierformat(name: str | None) -> tuple[float, float] | None:
    """Width and height in points of a paper size by PseudonyMed's name ("bild" for a page
    as large as the image, None then); an unknown name is a ValueError in German."""
    schluessel = (name or "a4").lower()
    try:
        return loading.paper_size(_PAPIER.get(schluessel, schluessel))
    except ValueError:
        raise ValueError(f"Unbekanntes Papierformat: {name!r} ({', '.join(PAPIERFORMATE)})") \
            from None


def oeffne(pfad: str | Path, papier: str | None = "a4") -> pymupdf.Document:
    """Open a PDF, or an image on a sheet of the paper size PseudonyMed names."""
    schluessel = (papier or "a4").lower()
    return loading.open_document(pfad, _PAPIER.get(schluessel, schluessel))


def beschreibung(plan: cleanup.CleanupPlan, sprache: str = "de") -> str:
    """What a page clean-up does: German for the reports, English for the interface in
    English ("en")."""
    return cleanup.describe(plan, "en" if sprache.startswith("en") else "de")


def unlesbare_treffer(seite: PageText) -> list[Fundstelle]:
    """The lines OCR could not read on a page, as hits that blank them with "[unlesbar]".
    What cannot be read cannot be checked: such a line may hold a name or handwriting."""
    return [Fundstelle(seite=z.page_number, start=z.start, ende=z.end, text=z.text,
                       art=Art.MANUELL, regel="ocr.unlesbar", pseudonym=UNLESBAR,
                       rects=list(z.rects), schriftgroesse=z.font_size, quelle="ocr")
            for z in ocr.unreadable_lines(seite)]
