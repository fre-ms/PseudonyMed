"""Interface language of the desktop: German is the source language, en-GB comes from a Qt
translation.  The language is chosen as for the command line (:mod:`pseudonymed.language`),
with Qt's idea of the system locale as the last word.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator

from pseudonymed import language
from pseudonymed.core.text import in_language
from pseudonymed.document import optdoc_adapter

FOLDER = Path(__file__).parent / "translations"


def determine_language(arguments: list[str]) -> str:
    """The language code in lower case: from the command line, else the configuration,
    else the system locale as Qt sees it (for example "de_de", "en_gb")."""
    return language.determine(arguments, QLocale.system().name())


def without_language_arguments(arguments: list[str]) -> list[str]:
    """The command line without ``--sprache``/``--language`` and their values, as Qt
    gets it."""
    return language.without_arguments(arguments)


def interface_language() -> str:
    """"en" while the English translation is installed, else "de": the language for texts
    that are not built with tr(), such as optdoc's wording and messages."""
    return QCoreApplication.translate("Language", "de", "language code of the interface")


def shown(text: object) -> str:
    """A text of the core (a message, a state line, a label) in the interface language."""
    return in_language(text, interface_language())


def shown_error(error: BaseException) -> str:
    """The message of an error in the interface language, optdoc's and the core's alike."""
    return optdoc_adapter.message(error, interface_language())


def install(app: QCoreApplication, language: str) -> QTranslator | None:
    """Install the en-GB translation when the language asks for English.  The lex
    libraries, whose list of packages the steps show, speak the same language."""
    import lexcor

    lexcor.set_language("en" if language.startswith("en") else "de")
    if not language.startswith("en"):
        return None
    translator = QTranslator(app)
    if translator.load(str(FOLDER / "pseudonymed_en_GB.qm")):
        app.installTranslator(translator)
        return translator
    return None
