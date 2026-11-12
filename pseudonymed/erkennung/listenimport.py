"""Reading a word list from a file for the vocabulary and the identifiers tables.

Both steps exchange the same kind of file: a CSV with the term in the first column and an
optional second column that sorts it (``kategorie`` for the vocabulary, ``klasse`` for the
identifiers), with or without a header line and separated by comma, semicolon or tab; or a
plain list with one term per line.  Lines that are empty or start with ``#`` are skipped.
The reader groups the terms by that column, so that the caller inserts each group under
its own category, and reports how many rows carried a term.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from pseudonymed.dokument.textdatei import lese_textdatei

TRENNZEICHEN = (",", ";", "\t")
KOPFWOERTER = ("begriff", "term", "wort")


@dataclass(frozen=True)
class Gelesen:
    """What a list file yielded: the terms grouped by the value of the sorting column, in
    the order the values first appear, and how many rows carried a term."""

    gruppen: dict[str, list[str]]
    zeilen: int


def _inhaltszeilen(text: str) -> list[str]:
    """The lines that carry content: neither empty nor a ``#`` comment."""
    return [z for z in text.splitlines() if z.strip() and not z.lstrip().startswith("#")]


def _trennzeichen(zeile: str) -> str | None:
    """The CSV delimiter the first line uses, or None for a plain list."""
    return next((t for t in TRENNZEICHEN if t in zeile), None)


def _ist_kopf(felder: list[str]) -> bool:
    """Whether a row is the header line: its first field names the term column."""
    return felder[0].strip().casefold() in KOPFWOERTER


def _spalte(kopf: list[str] | None, name: str) -> int:
    """Index of the sorting column: where the header names it, else the second column."""
    return kopf.index(name) if kopf and name in kopf else 1


def _gruppiere(zeilen: list[str], trenner: str, spalte: str, standard: str) -> Gelesen:
    """Group the terms of CSV rows by their sorting column; an empty or missing column
    value falls back to ``standard``.  Rows without a term are skipped."""
    gruppen: dict[str, list[str]] = {}
    gelesen = 0
    kopf: list[str] | None = None
    for i, felder in enumerate(csv.reader(io.StringIO("\n".join(zeilen)), delimiter=trenner)):
        if not felder or not felder[0].strip():
            continue
        if i == 0 and _ist_kopf(felder):
            kopf = [f.strip().casefold() for f in felder]
            continue
        index = _spalte(kopf, spalte)
        wert = felder[index].strip() if len(felder) > index else ""
        gruppen.setdefault(wert or standard, []).append(felder[0].strip())
        gelesen += 1
    return Gelesen(gruppen, gelesen)


def lies_liste(pfad: Path, spalte: str, standard: str) -> Gelesen:
    """Read a CSV (``begriff[,<spalte>]``, header optional, comma, semicolon or tab) or a
    plain list (one term per line) and group the terms by the sorting column; a plain
    list and rows without a column value go to ``standard``."""
    zeilen = _inhaltszeilen(lese_textdatei(pfad))
    if not zeilen:
        return Gelesen({}, 0)
    trenner = _trennzeichen(zeilen[0])
    if trenner is None:
        return Gelesen({standard: [z.strip() for z in zeilen]}, len(zeilen))
    return _gruppiere(zeilen, trenner, spalte, standard)
