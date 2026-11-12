"""Interface language: German is the source language, en-GB comes from a Qt translation.

Order of precedence: ``--sprache``/``--language`` on the command line,
``PSEUDONYMED_SPRACHE`` or ``[gui] sprache`` in the configuration file, then the
system locale.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator

from pseudonymed.konfig import lade_einstellungen

ORDNER = Path(__file__).parent / "uebersetzungen"


def sprache_bestimmen(argumente: list[str]) -> str:
    """The language code in lower case: from the command line, else the configuration,
    else the system locale (for example "de_de", "en_gb")."""
    for i, a in enumerate(argumente):
        if a in ("--sprache", "--language") and i + 1 < len(argumente):
            return argumente[i + 1].lower()
        if a.startswith("--sprache=") or a.startswith("--language="):
            return a.split("=", 1)[1].lower()
    gewaehlt = lade_einstellungen().gui_sprache
    if gewaehlt:
        return gewaehlt.lower()
    return QLocale.system().name().lower()  # e.g. "de_de", "en_gb"


def ohne_sprachargumente(argumente: list[str]) -> list[str]:
    """The command line without ``--sprache``/``--language`` and their values, as Qt
    gets it."""
    rest: list[str] = []
    ueberspringe = False
    for a in argumente:
        if ueberspringe:
            ueberspringe = False
            continue
        if a in ("--sprache", "--language"):
            ueberspringe = True
            continue
        if a.startswith(("--sprache=", "--language=")):
            continue
        rest.append(a)
    return rest


def installiere(app: QCoreApplication, sprache: str) -> QTranslator | None:
    """Install the en-GB translation when the language asks for English."""
    if not sprache.startswith("en"):
        return None
    uebersetzer = QTranslator(app)
    if uebersetzer.load(str(ORDNER / "pseudonymed_en_GB.qm")):
        app.installTranslator(uebersetzer)
        return uebersetzer
    return None
