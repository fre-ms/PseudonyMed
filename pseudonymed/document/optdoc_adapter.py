"""Where PseudonyMed meets optdoc, the document layer it builds on.

optdoc loads, cleans up and reads the pages (text layer and OCR); its names and messages
are English.  The user sees optdoc's part in the language of the interface: its wording of
a clean-up and its messages in German or English, its engine "none" as "aus" and its
paper size "image" as "bild".  PseudonyMed makes its own hits of the lines OCR could not
read, and hands optdoc its settings (which engine "auto" means, where Tesseract lives) on
import of this module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pymupdf
from optdoc import cleanup, loading, ocr
from optdoc.pagetext import PageText
from optdoc.settings import SettingsLike, set_settings

from pseudonymed.core.model import Hit, Kind

UNREADABLE = "[unlesbar]"  # what a line OCR could not read becomes in the output

OCRError = ocr.OCRError  # raised by the engines; :func:`message` words it for the user

# PseudonyMed's names for the engines and paper sizes, where they differ from optdoc's
_ENGINES = {"aus": "none"}
_PAPER = {"bild": "image"}

# optdoc's messages and their German wording; a named group carries the variable part over
_MESSAGES_DE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(muster, re.DOTALL), german) for muster, german in (
        (r"Tesseract is not installed \(no program 'tesseract'\)\.",
         "Tesseract ist nicht installiert (Programm 'tesseract' fehlt)."),
        (r"Tesseract could not be run: (?P<x>.*)", "Tesseract konnte nicht ausgeführt werden: {x}"),
        (r"Tesseract reports an error: (?P<x>.*)", "Tesseract meldet einen Fehler: {x}"),
        (r"RapidOCR is not installed \(extra 'ocr'\)\.",
         "RapidOCR ist nicht installiert (Extra 'ocr')."),
        (r"RapidOCR failed: (?P<x>.*)", "RapidOCR fehlgeschlagen: {x}"),
        (r"unknown OCR engine: (?P<x>.*) \(auto, rapidocr, tesseract, none\)",
         "Unbekanntes OCR-Backend: {x} (auto, rapidocr, tesseract, aus)."),
        (r"unknown paper size: (?P<x>.*) \([^()]*\)",
         "Unbekanntes Papierformat: {x} (a4, a5, a3, letter, legal, bild)."),
        (r"Unsupported file type: (?P<x>.*)", "Dateityp nicht unterstützt: {x}"),
        (r"HEIC/HEIF photos need the extra 'ocr' \(pillow-heif\): (?P<x>.*)",
         "HEIC/HEIF-Fotos brauchen das Extra 'ocr' (pillow-heif): {x}"),
        (r"page text (?P<x>\d+): the character lists do not fit the text",
         "Seitentext {x}: Zeichenlisten passen nicht zum Text."),
        (r"PNG encoding failed", "PNG-Kodierung fehlgeschlagen."),
    ))


class _Settings:
    """PseudonyMed's settings as optdoc reads them, looked up anew on every access so that a
    change on the settings page applies to the next reading without a restart."""

    @property
    def ocr_backend(self) -> str:
        """The engine "auto" stands for, in optdoc's spelling ("aus" becomes "none")."""
        from pseudonymed.config import load_settings

        name = (load_settings().ocr_backend or "auto").lower()
        return _ENGINES.get(name, name)

    @property
    def tesseract_path(self) -> str:
        """The configured Tesseract program, empty to search for it."""
        from pseudonymed.config import load_settings

        return load_settings().tesseract_path

    @property
    def tesseract_languages(self) -> str:
        """The configured Tesseract language packs, empty for the default."""
        from pseudonymed.config import load_settings

        return load_settings().tesseract_languages


# optdoc's protocol declares plain attributes, which mypy takes as writable; it only reads
# them, and read-only properties are what the live lookup needs
set_settings(cast(SettingsLike, _Settings()))


def backend(name: str = "auto", languages: str | None = None,
            tables: bool = True) -> ocr.OCRBackend | None:
    """The OCR engine for PseudonyMed's name of it ("auto", "rapidocr", "tesseract", "aus");
    None for "aus" and for "auto" without an engine.  Unknown and missing engines end in an
    :class:`OCRError`."""
    return ocr.backend(_ENGINES.get((name or "auto").lower(), name), languages, tables=tables)


def paper_size(name: str | None) -> tuple[float, float] | None:
    """Width and height in points of a paper size by PseudonyMed's name ("bild" for a page
    as large as the image, None then); an unknown name is a ValueError."""
    key = (name or "a4").lower()
    return loading.paper_size(_PAPER.get(key, name))


def open_file(path: str | Path, paper: str | None = "a4") -> pymupdf.Document:
    """Open a PDF, or an image on a sheet of the paper size PseudonyMed names."""
    key = (paper or "a4").lower()
    return loading.open_document(path, _PAPER.get(key, paper))


def description(plan: cleanup.CleanupPlan, language: str = "de") -> str:
    """What a page clean-up does, in German ("de", the reports and the German interface) or
    English ("en", the English interface)."""
    return cleanup.describe(plan, "en" if language.startswith("en") else "de")


def message(error: BaseException, language: str = "de") -> str:
    """The message of an error for the user: optdoc's own in German unless the language is
    English ("en"), every other message as it is."""
    text = str(error)
    if language.startswith("en"):
        return text
    for muster, german in _MESSAGES_DE:
        matches = muster.fullmatch(text)
        if matches:
            return german.format(**matches.groupdict())
    return text


def unreadable_matches(page: PageText) -> list[Hit]:
    """The lines OCR could not read on a page, as hits that blank them with "[unlesbar]".
    What cannot be read cannot be checked: such a line may hold a name or handwriting."""
    return [Hit(page=z.page_number, start=z.start, end=z.end, text=z.text,
                kind=Kind.MANUAL, rule="ocr.unlesbar", pseudonym=UNREADABLE,
                rects=list(z.rects), font_size=z.font_size, source="ocr")
            for z in ocr.unreadable_lines(page)]
