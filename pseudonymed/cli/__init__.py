"""Command line: pseudonymise, check, re-identify, list pseudonyms, show configuration.

The commands live in modules by subject; importing them registers them with ``app``, so
the import order below is the order the help lists them in."""

# ruff: noqa: I001
from pseudonymed.cli.basis import app
from pseudonymed.cli import dokumente, system, datenbank, listen, kontakte  # noqa: F401

__all__ = ["app"]
