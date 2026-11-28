"""The re-check: hits inside pseudonyms do not count, tolerated ones are counted once,
the plain-text pass adds only what the text layer missed, and leftover metadata or
structure blocks the release."""

from __future__ import annotations

import re

import pymupdf
import pytest
from ocrdoc.pagetext import read_text_layer

from pseudonymed.checks import recheck as rc
from pseudonymed.core.model import Kind, Recheck
from pseudonymed.detection.rules import Rule
from pseudonymed.redaction.pdf import ALLOWED_METADATA, NOTICE_PREFIX

WORD = Rule("probe.wort", re.compile(r"Beispielwort"), Kind.TERM, "[Begriff]")
CASE_NUMBER = Rule("probe.akte", re.compile(r"\b[A-Z]{2}\d{4}\b"), Kind.CASE_NUMBER, "[Akte]")
RULES = [WORD, CASE_NUMBER]
TOLERATED = frozenset({rc.tolerance_key("probe.wort", "Beispielwort")})


def _targets(tolerated: frozenset[tuple[str, str]] = frozenset(),
             pseudonyms: tuple[str, ...] = ()) -> rc._SearchTargets:
    return rc._SearchTargets(RULES, tolerated, pseudonyms)


def _document_of(*pages: str) -> pymupdf.Document:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    return doc


def _brief(result: Recheck) -> list[tuple[int, str, str]]:
    return [(r["page"], r["rule"], r["source"]) for r in result.residual_matches]


def test_is_tolerated_normalises_whitespace_and_case() -> None:
    targets = _targets(frozenset({rc.tolerance_key("probe.wort", "Beispiel  Wort")}))
    assert targets.is_tolerated("probe.wort", "beispiel\twort")
    assert not targets.is_tolerated("probe.akte", "beispiel wort")


def test_text_layer_protects_pseudonyms_and_counts_kept() -> None:
    doc = _document_of("Beispielwort, Akte AB1234, dann [Akte CD5678].")
    result = Recheck()
    seen: set[tuple[int, str, str]] = set()
    rc._check_text_layer(_targets(TOLERATED, ("[Akte CD5678]",)), read_text_layer(doc[0], 0),
                         result, seen)
    assert _brief(result) == [(1, "probe.akte", "text")]
    assert result.kept_on_purpose == ["probe.wort"]
    assert seen == {(0, "probe.wort", "Beispielwort"), (0, "probe.akte", "AB1234"),
                    (0, "probe.akte", "CD5678")}


def test_plaintext_reports_only_new_and_each_match_once() -> None:
    text = "Beispielwort; AB1234; AB1234; [Akte CD5678]"
    result = Recheck()
    seen: set[tuple[int, str, str]] = {(2, "probe.wort", "Beispielwort")}
    rc._check_plain_text(_targets(pseudonyms=("[Akte CD5678]",)), 2, text, result, seen)
    assert result.residual_matches == [{
        "page": 3, "kind": str(Kind.CASE_NUMBER), "rule": "probe.akte",
        "pseudonym": "[Akte]", "rects": [], "source": "plaintext"}]
    assert result.kept_on_purpose == []
    assert seen == {(2, "probe.wort", "Beispielwort"), (2, "probe.akte", "AB1234")}


def test_plaintext_counts_kept() -> None:
    result = Recheck()
    seen: set[tuple[int, str, str]] = set()
    rc._check_plain_text(_targets(TOLERATED), 0, "Beispielwort", result, seen)
    assert result.residual_matches == [] and result.kept_on_purpose == ["probe.wort"]


def test_extra_text_does_not_count_kept_again() -> None:
    result = Recheck()
    rc._check_extra_text(_targets(TOLERATED, ("[Akte CD5678]",)), 2,
                         "Beispielwort, AB1234, [Akte CD5678]", result)
    assert _brief(result) == [(0, "probe.akte", "companion_text2")]
    assert result.kept_on_purpose == []


@pytest.mark.parametrize(("field_name", "value", "expected"), [
    ("format", "PDF 1.7", True),
    ("encryption", None, True),
    ("title", "", True),
    ("author", None, True),
    ("producer", ALLOWED_METADATA["producer"], True),
    ("producer", "anderes Werkzeug", False),
    ("subject", NOTICE_PREFIX + " am 01.01.2026", True),
    ("subject", "Befund", False),
    ("keywords", "k", False),
])
def test_metadata_harmless(field_name: str, value: str | None, expected: bool) -> None:
    assert rc._metadata_harmless(field_name, value) is expected


def test_metadata_and_structure_block_in_fixed_order() -> None:
    doc = _document_of("Nur Text.")
    doc.set_metadata({"title": "Probe", "keywords": "k",
                      "producer": ALLOWED_METADATA["producer"],
                      "subject": NOTICE_PREFIX + " 1.0"})
    doc.set_xml_metadata("<x/>")
    doc.embfile_add("anhang.txt", b"x")
    doc.set_toc([[1, "Abschnitt", 1]])
    page = doc[0]
    page.add_text_annot((20, 20), "Notiz")
    field_name = pymupdf.Widget()
    field_name.field_name = "feld"
    field_name.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    field_name.rect = pymupdf.Rect(10, 100, 200, 120)
    page.add_widget(field_name)
    result = rc.check(doc, RULES)
    assert result.residual_matches == [] and not result.passed
    assert not result.metadata_empty and not result.structure_clean
    assert result.remarks == [
        "Metadatum 'title' ist nicht leer", "Metadatum 'keywords' ist nicht leer",
        "XMP-Metadaten vorhanden", "1 eingebettete Datei(en)", "Lesezeichen vorhanden",
        "1 Anmerkung(en) vorhanden", "1 Formularfeld(er) vorhanden",
    ]


def test_check_collects_sources_in_fixed_order() -> None:
    """Text-layer hits come first, the plain-text pass adds nothing the layer saw,
    companion texts follow with page 0, and tolerated hits are counted once."""
    doc = _document_of("Beispielwort und AB1234.", "Seite zwei mit EF9012.")
    result = rc.check(doc, RULES, extra_texts=["Beispielwort, GH3456"],
                      tolerated=[rc.tolerance_key("probe.wort", "Beispielwort")],
                      pseudonyms=["", "[Akte CD5678]"])
    assert _brief(result) == [(1, "probe.akte", "text"), (2, "probe.akte", "text"),
                              (0, "probe.akte", "companion_text1")]
    assert result.kept_on_purpose == ["probe.wort"]
    assert result.checked == ["Begriffe", "Aktenzeichen"]
    assert not result.passed


def test_check_passes_on_clean_document() -> None:
    result = rc.check(_document_of("Nur Text ohne Kennzeichen."), RULES)
    assert result.passed and result.remarks == []
