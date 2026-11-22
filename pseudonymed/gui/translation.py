"""Interface language: German is the source language, en-GB comes from a Qt translation.

Order of precedence: ``--sprache``/``--language`` on the command line,
``PSEUDONYMED_SPRACHE`` or ``[gui] sprache`` in the configuration file, then the
system locale.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator

from pseudonymed.config import load_settings

FOLDER = Path(__file__).parent / "translations"


def determine_language(arguments: list[str]) -> str:
    """The language code in lower case: from the command line, else the configuration,
    else the system locale (for example "de_de", "en_gb")."""
    for i, a in enumerate(arguments):
        if a in ("--sprache", "--language") and i + 1 < len(arguments):
            return arguments[i + 1].lower()
        if a.startswith("--sprache=") or a.startswith("--language="):
            return a.split("=", 1)[1].lower()
    selected = load_settings().gui_language
    if selected:
        return selected.lower()
    return QLocale.system().name().lower()  # e.g. "de_de", "en_gb"


def without_language_arguments(arguments: list[str]) -> list[str]:
    """The command line without ``--sprache``/``--language`` and their values, as Qt
    gets it."""
    rest: list[str] = []
    skip = False
    for a in arguments:
        if skip:
            skip = False
            continue
        if a in ("--sprache", "--language"):
            skip = True
            continue
        if a.startswith(("--sprache=", "--language=")):
            continue
        rest.append(a)
    return rest


def interface_language() -> str:
    """"en" while the English translation is installed, else "de": the language for texts
    that are not built with tr(), such as optdoc's wording and messages."""
    return QCoreApplication.translate("Language", "de", "language code of the interface")


def install(app: QCoreApplication, language: str) -> QTranslator | None:
    """Install the en-GB translation when the language asks for English."""
    if not language.startswith("en"):
        return None
    translator = QTranslator(app)
    if translator.load(str(FOLDER / "pseudonymed_en_GB.qm")):
        app.installTranslator(translator)
        return translator
    return None
