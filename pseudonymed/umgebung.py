"""Environment variables and per-user folders, with the former name as a fallback.

The project was called AnonyMed until November 2026.  Environment variables are read as
``PSEUDONYMED_<NAME>`` first and ``ANONYMED_<NAME>`` second, and a folder of the old name
(database, key, configuration) is moved to the new name the first time it is needed, so an
existing installation keeps its pseudonyms.  When the move fails, the old folder is used.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_config_path, user_data_path

NAME = "PseudonyMed"
FRUEHERER_NAME = "AnonyMed"
PRAEFIX = "PSEUDONYMED_"
FRUEHERES_PRAEFIX = "ANONYMED_"


def namen(name: str) -> list[str]:
    """Both spellings of a variable, the current first (for typer's ``envvar``)."""
    return [PRAEFIX + name, FRUEHERES_PRAEFIX + name]


def variable(name: str, standard: str | None = None) -> str | None:
    """``PSEUDONYMED_<name>``, else ``ANONYMED_<name>``, else ``standard``."""
    for n in namen(name):
        wert = os.environ.get(n)
        if wert is not None:
            return wert
    return standard


def _umgezogen(pfad_fuer: Callable[..., Path]) -> Path:
    """The folder under the current name; a folder of the former name is renamed to it
    once, so an existing installation keeps its data.  When the rename fails (another
    volume, permissions), the old folder stays in use rather than starting empty."""
    neu = pfad_fuer(NAME, appauthor=False)
    alt = pfad_fuer(FRUEHERER_NAME, appauthor=False)
    if not neu.exists() and alt.is_dir():
        try:
            neu.parent.mkdir(parents=True, exist_ok=True)
            alt.rename(neu)
        except OSError:
            return alt
    return neu


def datenordner() -> Path:
    """Folder of database, key and lists: ``PSEUDONYMED_DATENORDNER`` or the platform's
    data folder (taken over from the former name)."""
    ordner = variable("DATENORDNER")
    return Path(ordner) if ordner else _umgezogen(user_data_path)


def konfigordner() -> Path:
    """Folder of ``konfig.toml``: ``PSEUDONYMED_DATENORDNER`` or the platform's
    configuration folder (taken over from the former name)."""
    ordner = variable("DATENORDNER")
    return Path(ordner) if ordner else _umgezogen(user_config_path)
