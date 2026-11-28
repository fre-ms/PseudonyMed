"""Environment variables and per-user folders.

Environment variables are ``PSEUDONYMED_<NAME>`` with an English name
(``PSEUDONYMED_DATA_FOLDER``, ``PSEUDONYMED_LANGUAGE`` …); the German names
(``PSEUDONYMED_DATENORDNER``, ``PSEUDONYMED_SPRACHE`` …) are understood as well.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_path, user_data_path

NAME = "PseudonyMed"
PREFIX = "PSEUDONYMED_"
# the German name of a variable, for those who set it in German
GERMAN = {"DATA_FOLDER": "DATENORDNER", "LANGUAGE": "SPRACHE", "LEXICON": "LEXIKON",
          "CARDDAV_USER": "CARDDAV_BENUTZER", "CARDDAV_ADDRESS_BOOK": "CARDDAV_ADRESSBUCH",
          "CARDDAV_PASSWORD": "CARDDAV_PASSWORT", "BACKUP_PASSPHRASE": "SICHERUNG_PASSPHRASE"}


def names(name: str) -> list[str]:
    """The English and, where it differs, the German name of a variable (for typer's
    ``envvar``)."""
    return [PREFIX + name] + ([PREFIX + GERMAN[name]] if name in GERMAN else [])


def variable(name: str, standard: str | None = None) -> str | None:
    """``PSEUDONYMED_<name>``, else its German equivalent, else ``standard``."""
    for n in names(name):
        value = os.environ.get(n)
        if value is not None:
            return value
    return standard


def data_folder() -> Path:
    """Folder of database, key and lists: ``PSEUDONYMED_DATA_FOLDER`` or the platform's
    data folder."""
    folder = variable("DATA_FOLDER")
    return Path(folder) if folder else user_data_path(NAME, appauthor=False)


def lex_folder() -> Path:
    """Folder of the stores of lexterm and lexident (``lexterm.sqlite``, ``lexident.sqlite``):
    ``LEXCOR_DATA`` when it is set; else PseudonyMed's own data folder when
    ``PSEUDONYMED_DATA_FOLDER`` names one, so that a portable installation keeps everything
    together; else the folder of the two libraries, where their other users find the stores."""
    import lexcor.store

    own = variable("DATA_FOLDER")
    if own and not os.environ.get("LEXCOR_DATA"):
        return Path(own)
    return lexcor.store.data_folder()


def config_folder() -> Path:
    """Folder of ``config.toml``: ``PSEUDONYMED_DATA_FOLDER`` or the platform's
    configuration folder."""
    folder = variable("DATA_FOLDER")
    return Path(folder) if folder else user_config_path(NAME, appauthor=False)
