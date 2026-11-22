"""The language of the interface, the same for the desktop and the command line.

Order of precedence: ``--sprache``/``--language`` on the command line,
``PSEUDONYMED_SPRACHE`` or ``[gui] sprache`` in the configuration file, then the
system's locale.  German is the source language; a code starting with "en" asks for
English.
"""

from __future__ import annotations

import locale
import os

from pseudonymed.config import load_settings

OPTIONS = ("--sprache", "--language")


def from_arguments(arguments: list[str]) -> str | None:
    """The language given with ``--sprache``/``--language`` (lower case), else None."""
    for i, a in enumerate(arguments):
        if a in OPTIONS and i + 1 < len(arguments):
            return arguments[i + 1].lower()
        if a.startswith(tuple(o + "=" for o in OPTIONS)):
            return a.split("=", 1)[1].lower()
    return None


def determine(arguments: list[str], system: str | None = None) -> str:
    """The language code in lower case ("de", "en_gb" …): from the command line, else the
    environment or the configuration, else ``system`` or the locale of the process."""
    chosen = from_arguments(arguments)
    if chosen:
        return chosen
    selected = load_settings().gui_language
    if selected:
        return selected.lower()
    return (system or system_language()).lower()


def system_language() -> str:
    """The locale the environment names (``LC_ALL``, ``LC_MESSAGES``, ``LANG``), else the
    process's locale; "de" when none names a language (the C locale, for instance)."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value and value.split(".")[0] not in ("C", "POSIX"):
            return value.split(".")[0]
    code = locale.getlocale()[0]
    return code if code and code not in ("C", "POSIX") else "de"


def without_arguments(arguments: list[str]) -> list[str]:
    """The command line without ``--sprache``/``--language`` and their values."""
    rest: list[str] = []
    skip = False
    for a in arguments:
        if skip:
            skip = False
            continue
        if a in OPTIONS:
            skip = True
            continue
        if a.startswith(tuple(o + "=" for o in OPTIONS)):
            continue
        rest.append(a)
    return rest
