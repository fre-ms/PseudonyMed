"""Command line: pseudonymise, check, re-identify, list pseudonyms, show configuration.

The commands live in modules by subject; importing them registers them with ``app``, so
the import order below is the order the help lists them in.

The command line speaks German or English, the code English.  Command names are
therefore given to ``command()`` explicitly, in both languages, option names to
``typer.Option`` through ``flags()``, and a positional argument carries its name as
``metavar``.  A list argument keeps a German parameter name instead (``dateien``,
``begriffe`` …), because Typer drops the ``...`` from the usage line once a metavar is
set; English names it through ``list_metavar()``."""

# ruff: noqa: I001
from pseudonymed.cli.base import app
from pseudonymed.cli import documents, system, database, lists, contacts  # noqa: F401

__all__ = ["app"]
