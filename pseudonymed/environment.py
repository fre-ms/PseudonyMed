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
FORMER_NAME = "AnonyMed"
PREFIX = "PSEUDONYMED_"
FORMER_PREFIX = "ANONYMED_"


def names(name: str) -> list[str]:
    """Both spellings of a variable, the current first (for typer's ``envvar``)."""
    return [PREFIX + name, FORMER_PREFIX + name]


def variable(name: str, standard: str | None = None) -> str | None:
    """``PSEUDONYMED_<name>``, else ``ANONYMED_<name>``, else ``standard``."""
    for n in names(name):
        value = os.environ.get(n)
        if value is not None:
            return value
    return standard


def _migrated(path_for: Callable[..., Path]) -> Path:
    """The folder under the current name; a folder of the former name is renamed to it
    once, so an existing installation keeps its data.  When the rename fails (another
    volume, permissions), the old folder stays in use rather than starting empty."""
    new = path_for(NAME, appauthor=False)
    old = path_for(FORMER_NAME, appauthor=False)
    if not new.exists() and old.is_dir():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
        except OSError:
            return old
    return new


def data_folder() -> Path:
    """Folder of database, key and lists: ``PSEUDONYMED_DATENORDNER`` or the platform's
    data folder (taken over from the former name)."""
    folder = variable("DATENORDNER")
    return Path(folder) if folder else _migrated(user_data_path)


def lex_folder() -> Path:
    """Folder of the stores of lexterm and lexident (``lexterm.sqlite``, ``lexident.sqlite``):
    ``LEXCOR_DATA`` when it is set; else PseudonyMed's own data folder when
    ``PSEUDONYMED_DATENORDNER`` names one, so that a portable installation keeps everything
    together; else the folder of the two libraries, where their other users find the stores."""
    import lexcor.store

    own = variable("DATENORDNER")
    if own and not os.environ.get("LEXCOR_DATA"):
        return Path(own)
    return lexcor.store.data_folder()


def config_folder() -> Path:
    """Folder of ``konfig.toml``: ``PSEUDONYMED_DATENORDNER`` or the platform's
    configuration folder (taken over from the former name)."""
    folder = variable("DATENORDNER")
    return Path(folder) if folder else _migrated(user_config_path)
