"""Where PseudonyMed meets docrepro, the document layer it builds on.

docrepro's steps open, bind, prepare and read the pages (typdoc, optdoc, ocrdoc, joined by
docrepro's chain, :mod:`pseudonymed.document.chain`); their names are English, their
messages come in English and German (doccor's kinds of error).  The user sees docrepro's
part in the language of the interface: its wording of a preparation and its messages in
German or English, its engine "none" as "off".  PseudonyMed makes its own hits of the lines
OCR could not read, and hands ocrdoc its settings (which engine "auto" means, where
Tesseract lives) on import of this module.
"""

from __future__ import annotations

from typing import Any

from doccor.errors import DocError, describe_error
from ocrdoc import ocr
from ocrdoc.errors import OCRError
from ocrdoc.pagetext import PageText
from ocrdoc.settings import set_settings
from optdoc import cleanup

from pseudonymed.core.model import Hit, Kind
from pseudonymed.core.text import english, error_text

UNREADABLE = "[unlesbar]"  # what a line OCR could not read becomes in the output

__all__ = ["UNREADABLE", "DocError", "OCRError", "backend", "chain_settings", "description",
           "message", "reader", "unreadable_matches"]

# PseudonyMed's names for the engines, where they differ from ocrdoc's
_ENGINES = {"off": "none"}


class _Settings:
    """PseudonyMed's settings as ocrdoc reads them, looked up anew on every access so that a
    change on the settings page applies to the next reading without a restart."""

    @property
    def ocr_backend(self) -> str:
        """The engine "auto" stands for, in ocrdoc's spelling ("off" becomes "none")."""
        from pseudonymed.config import load_settings

        return reader(load_settings().ocr_backend)

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


set_settings(_Settings())


def reader(name: str | None) -> str:
    """ocrdoc's name of an engine PseudonyMed names ("auto", "rapidocr", "tesseract",
    "off"): "off" is "none"."""
    key = (name or "auto").lower()
    return _ENGINES.get(key, key)


def backend(name: str = "auto", languages: str | None = None,
            tables: bool = True) -> ocr.OCRBackend | None:
    """The OCR engine for PseudonyMed's name of it ("auto", "rapidocr", "tesseract", "off");
    None for "off" and for "auto" without an engine.  Unknown and missing engines end in an
    :class:`OCRError`."""
    return ocr.backend(reader(name), languages, tables=tables)


def chain_settings(engine: str = "auto", languages: str = "", tables: bool = True,
                   clean_up: bool = True, dpi: int = ocr.OCR_DPI) -> dict[str, Any]:
    """The settings of docrepro's steps for a fresh chain, in their names: optdoc prepares
    the pages by its suggestion unless ``clean_up`` is off; ocrdoc reads with the engine
    (PseudonyMed's name of it), its language packs, Tesseract's table pass and the
    resolution given."""
    return {"clean_up": clean_up, "reader": reader(engine), "languages": languages or None,
            "tables": tables, "dpi": dpi}


def description(plan: cleanup.CleanupPlan, language: str = "de") -> str:
    """What a preparation of a page does, in German ("de", the reports and the German
    interface) or English ("en", the English interface)."""
    return cleanup.describe(plan, "en" if english(language) else "de")


def message(error: BaseException, language: str = "de") -> str:
    """The message of an error for the user in the interface language: docrepro's (doccor's
    kinds, worded in both languages) in German unless the language is English,
    PseudonyMed's own in the language asked for."""
    if isinstance(error, DocError):
        return describe_error(error, "en" if english(language) else "de")
    return error_text(error, language)


def unreadable_matches(page: PageText) -> list[Hit]:
    """The lines OCR could not read on a page, as hits that blank them with "[unlesbar]".
    What cannot be read cannot be checked: such a line may hold a name or handwriting."""
    return [Hit(page=z.page_number, start=z.start, end=z.end, text=z.text,
                kind=Kind.MANUAL, rule="ocr.unreadable", pseudonym=UNREADABLE,
                rects=list(z.rects), font_size=z.font_size, source="ocr")
            for z in ocr.unreadable_lines(page)]
