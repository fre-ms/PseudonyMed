"""Re-check the pseudonymised document with the same rules that produced it.

Any hit here means the export must not be released.  OCR of the result
(for rasterised output and scans) follows in milestone M3.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pymupdf
from optdoc.pagetext import PageText, read_text_layers

from pseudonymed.core.model import Kind, Recheck, Role
from pseudonymed.core.text import say
from pseudonymed.detection.rules import Rule, find, find_in_text
from pseudonymed.detection.variants import exact_pattern
from pseudonymed.redaction.pdf import ALLOWED_METADATA, NOTICE_PREFIX

KIND_NAMES: dict[Kind, str] = {
    Kind.NAME: say("Name", "Name"), Kind.FIRST_NAME: say("Vorname", "First name"),
    Kind.SURNAME: say("Nachname", "Surname"), Kind.BIRTH_NAME: say("Geburtsname", "Birth name"),
    Kind.BIRTH_DATE: say("Geburtsdatum", "Date of birth"), Kind.ADDRESS: say("Straße", "Street"),
    Kind.PLACE: say("Ort", "Place"), Kind.PHONE: say("Telefon", "Phone"),
    Kind.EMAIL: say("E-Mail", "E-mail"),
    Kind.INSURANCE_NUMBER: say("Versichertennummer", "Insurance number"),
    Kind.PATIENT_ID: say("Patienten-ID", "Patient ID"),
    Kind.CASE_NUMBER: say("Aktenzeichen", "Case number"), Kind.IBAN: say("IBAN", "IBAN"),
    Kind.TERM: say("Begriffe", "Terms"),
}
_RELATIVES = say("Angehörige", "Relatives")
_REFERRERS = say("Zuweiser", "Referrers")


def checked_from(rules: Iterable[Rule]) -> list[str]:
    """Readable list of what the rules cover: the patient's identifiers by kind, then
    the people around them, for the report's "searched again" line."""
    names: list[str] = []
    roles: list[str] = []
    for r in rules:
        entry: str
        if r.role == Role.RELATIVE:
            entry = _RELATIVES
        elif r.role == Role.REFERRER:
            entry = _REFERRERS
        else:
            entry = KIND_NAMES.get(r.kind, str(r.kind))
        target = roles if r.role in (Role.RELATIVE, Role.REFERRER) else names
        if entry not in target:
            target.append(entry)
    return names + roles


def tolerance_key(rule: str, text: str) -> tuple[str, str]:
    """Key of an occurrence the user deliberately kept: rule and normalised text."""
    return rule, " ".join(text.split()).casefold()


def pseudonym_spans(text: str, pseudonyms: Iterable[str]) -> list[tuple[int, int]]:
    """Where the inserted pseudonyms sit in ``text``; rules must not fire inside them
    (a code such as "KAZ2Q4W" could otherwise look like a file number)."""
    spans: list[tuple[int, int]] = []
    for p in pseudonyms:
        if not p:
            continue
        start = 0
        while True:
            i = text.find(p, start)
            if i < 0:
                break
            spans.append((i, i + len(p)))
            start = i + 1
    return spans


def in_pseudonym(a: int, b: int, spans: list[tuple[int, int]]) -> bool:
    """Whether the span ``a``..``b`` overlaps one of the pseudonym spans."""
    return any(a < y and b > x for x, y in spans)


_CODE = re.compile(r"\b[A-Z2-9]{6}(?:-\d+)?\b")


def pseudonym_spans_tolerant(text: str, pseudonyms: Iterable[str]) -> list[tuple[int, int]]:
    """Like :func:`pseudonym_spans`, for text that OCR read back: the codes inside the
    pseudonyms are also matched with the usual OCR confusions and regardless of the
    brackets and words around them, so that a re-read "(Ort AZT63Z)" still counts as
    the pseudonym and not as a file number."""
    pseudonyms = [p for p in pseudonyms if p]
    spans = pseudonym_spans(text, pseudonyms)
    codes = {m.group(0) for p in pseudonyms for m in _CODE.finditer(p)}
    for code in codes:
        muster = exact_pattern(code)
        if muster:
            spans += [(m.start(), m.end()) for m in re.finditer(muster, text)]
    return spans


@dataclass(frozen=True)
class _SearchTargets:
    """What one re-check searches for: the rules, the occurrences the user kept on
    purpose and the pseudonyms the redaction inserted, inside which no rule counts."""

    rules: list[Rule]
    tolerated: frozenset[tuple[str, str]]
    pseudonyms: tuple[str, ...]

    def is_tolerated(self, rule: str, text: str) -> bool:
        """Whether the user decided to keep this occurrence of ``rule``."""
        return tolerance_key(rule, text) in self.tolerated


def _plain_text_match(page: int, rule: Rule, source: str) -> dict[str, Any]:
    """Report entry for a hit in plain text, which has no rectangles to point at."""
    return {"seite": page, "art": str(rule.kind), "regel": rule.id,
            "pseudonym": rule.pseudonym, "rects": [], "quelle": source}


def _check_text_layer(targets: _SearchTargets, page: PageText, result: Recheck,
                      seen: set[tuple[int, str, str]]) -> None:
    """Runs the rules over one page's reconstructed text layer and reports residual hits
    with their rectangles.  Every hit, reported or not, is added to ``seen`` so that
    the plain-text pass does not report it a second time."""
    spans = pseudonym_spans(page.text, targets.pseudonyms)
    for f in find(page, targets.rules):
        seen.add((f.page, f.rule, f.text))
        if in_pseudonym(f.start, f.end, spans):
            continue
        if targets.is_tolerated(f.rule, f.text):
            result.kept_on_purpose.append(f.rule)
            continue
        result.residual_matches.append(f.without_clear_text())


def _check_plain_text(targets: _SearchTargets, number: int, text: str, result: Recheck,
                      seen: set[tuple[int, str, str]]) -> None:
    """Runs the rules over the plain text PyMuPDF extracts from one page, which can join
    characters differently from the reconstructed layer.  Hits the text-layer pass saw
    are skipped; new ones are reported without rectangles and remembered in turn."""
    spans = pseudonym_spans(text, targets.pseudonyms)
    for a, b, rule in find_in_text(text, targets.rules):
        key = (number, rule.id, text[a:b])
        if key in seen:
            continue
        if targets.is_tolerated(rule.id, text[a:b]):
            result.kept_on_purpose.append(rule.id)
            continue
        if in_pseudonym(a, b, spans):
            continue
        seen.add(key)
        result.residual_matches.append(_plain_text_match(number + 1, rule, "plaintext"))


def _check_extra_text(targets: _SearchTargets, number: int, text: str, result: Recheck) -> None:
    """Runs the rules over one companion text such as the exported transcript.  It
    repeats the pages, so tolerated occurrences are not counted in ``kept_on_purpose`` again."""
    spans = pseudonym_spans(text, targets.pseudonyms)
    for a, b, rule in find_in_text(text, targets.rules):
        if targets.is_tolerated(rule.id, text[a:b]) or in_pseudonym(a, b, spans):
            continue
        result.residual_matches.append(_plain_text_match(0, rule, f"zusatztext{number}"))


def _metadata_harmless(field_name: str, value: str | None) -> bool:
    """Whether a metadata entry may stay: PyMuPDF's format and encryption fields, empty
    values, the tool's own producer and creator lines and the release note."""
    if field_name in ("format", "encryption") or not value:
        return True
    if ALLOWED_METADATA.get(field_name) == value:
        return True
    return field_name == "subject" and value.startswith(NOTICE_PREFIX)


def _check_metadata(doc: pymupdf.Document, result: Recheck) -> None:
    """Flags every metadata entry that still carries content, and an XMP stream."""
    for field_name, value in doc.metadata.items():
        if _metadata_harmless(field_name, value):
            continue
        result.metadata_empty = False
        result.remarks.append(say(f"Metadatum '{field_name}' ist nicht leer",
                                  f"Metadata entry '{field_name}' is not empty"))
    if doc.xref_xml_metadata() > 0:
        result.metadata_empty = False
        result.remarks.append(say("XMP-Metadaten vorhanden", "XMP metadata present"))


def _check_structure(doc: pymupdf.Document, result: Recheck) -> None:
    """Flags embedded files, bookmarks, annotations and form fields: a clean export has
    none of them, and each could carry text the rules never saw."""
    findings = [
        (doc.embfile_count(), "{n} eingebettete Datei(en)", "{n} embedded file(s)"),
        (len(doc.get_toc()), "Lesezeichen vorhanden", "Bookmarks present"),
        (sum(len(list(p.annots())) for p in doc), "{n} Anmerkung(en) vorhanden",
         "{n} annotation(s) present"),
        (sum(len(list(p.widgets())) for p in doc), "{n} Formularfeld(er) vorhanden",
         "{n} form field(s) present"),
    ]
    for count, de, en in findings:
        if count:
            result.structure_clean = False
            result.remarks.append(say(de.format(n=count), en.format(n=count)))


def check(
    doc: pymupdf.Document, rules: list[Rule], extra_texts: Iterable[str] = (),
    tolerated: Iterable[tuple[str, str]] = (), pseudonyms: Iterable[str] = (),
) -> Recheck:
    """Re-run the rules on the output.  ``tolerated`` lists occurrences (rule, text)
    the user decided to keep; ``pseudonyms`` are the strings the redaction inserted,
    inside which no rule counts."""
    result = Recheck(checked=checked_from(rules))
    targets = _SearchTargets(rules, frozenset(tolerated), tuple(p for p in pseudonyms if p))
    seen: set[tuple[int, str, str]] = set()
    for page in read_text_layers(doc):
        _check_text_layer(targets, page, result, seen)
    for i, pdf_page in enumerate(doc):
        _check_plain_text(targets, i, pdf_page.get_text("text"), result, seen)
    for n, text in enumerate(extra_texts, 1):  # repeats the pages: count nothing twice
        _check_extra_text(targets, n, text, result)
    _check_metadata(doc, result)
    _check_structure(doc, result)
    return result
