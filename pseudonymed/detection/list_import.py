"""Reading a word list from a file for the vocabulary and the identifiers tables.

Both steps exchange the same kind of file: a CSV with the term in the first column and an
optional second column that sorts it (``category`` for the vocabulary, ``category`` for the
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

from pseudonymed.document.text_file import read_text_file

DELIMITERS = (",", ";", "\t")
HEADER_WORDS = ("begriff", "term", "wort")


@dataclass(frozen=True)
class ReadResult:
    """What a list file yielded: the terms grouped by the value of the sorting column, in
    the order the values first appear, and how many rows carried a term."""

    groups: dict[str, list[str]]
    lines: int


def _content_lines(text: str) -> list[str]:
    """The lines that carry content: neither empty nor a ``#`` comment."""
    return [z for z in text.splitlines() if z.strip() and not z.lstrip().startswith("#")]


def _delimiter(line: str) -> str | None:
    """The CSV delimiter the first line uses, or None for a plain list."""
    return next((t for t in DELIMITERS if t in line), None)


def _is_header(fields: list[str]) -> bool:
    """Whether a row is the header line: its first field names the term column."""
    return fields[0].strip().casefold() in HEADER_WORDS


def _column(header: list[str] | None, name: str) -> int:
    """Index of the sorting column: where the header names it, else the second column."""
    return header.index(name) if header and name in header else 1


def _group(lines: list[str], separator: str, column: str, standard: str) -> ReadResult:
    """Group the terms of CSV rows by their sorting column; an empty or missing column
    value falls back to ``standard``.  Rows without a term are skipped."""
    groups: dict[str, list[str]] = {}
    read_in = 0
    header: list[str] | None = None
    for i, fields in enumerate(csv.reader(io.StringIO("\n".join(lines)), delimiter=separator)):
        if not fields or not fields[0].strip():
            continue
        if i == 0 and _is_header(fields):
            header = [f.strip().casefold() for f in fields]
            continue
        index = _column(header, column)
        value = fields[index].strip() if len(fields) > index else ""
        groups.setdefault(value or standard, []).append(fields[0].strip())
        read_in += 1
    return ReadResult(groups, read_in)


def read_list(path: Path, column: str, standard: str) -> ReadResult:
    """Read a CSV (``begriff[,<spalte>]``, header optional, comma, semicolon or tab) or a
    plain list (one term per line) and group the terms by the sorting column; a plain
    list and rows without a column value go to ``standard``."""
    lines = _content_lines(read_text_file(path))
    if not lines:
        return ReadResult({}, 0)
    separator = _delimiter(lines[0])
    if separator is None:
        return ReadResult({standard: [z.strip() for z in lines]}, len(lines))
    return _group(lines, separator, column, standard)
