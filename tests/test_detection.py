"""Rules on page text: longest match wins, lower case words stay, neighbours merge."""

from __future__ import annotations

import datetime as dt
import re

import pymupdf
from ocrdoc.pagetext import PageText, read_text_layer

from pseudonymed.core.model import Kind, Patient
from pseudonymed.detection.rules import (
    Rule,
    box_to_hits,
    find,
    find_in_text,
    rules_for,
)
from pseudonymed.pseudonyms.assignment import PseudonymAssignment


def _patient() -> Patient:
    return Patient(surname="Müller", first_name="Max", sex="m",
                   birth_date=dt.date(1990, 2, 1), street="Musterstraße 12", postcode="80331",
                   place="München", emails=["max@example.org"])


def test_longest_match_wins() -> None:
    p = _patient()
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    text = "Herr Max Müller, geb. 01.02.1990, max. 3 Tabletten, Müllerstraße"
    matches = find_in_text(text, rules)
    ids = [r.id for _a, _b, r in matches]
    assert ids == ["name.vollname", "datum.geburtsdatum"]
    a, b, _ = matches[0]
    assert text[a:b] == "Max Müller"


def test_surname_as_an_initial_after_a_form_of_address() -> None:
    """ "Herr M." is the patient Müller; "Herr M. Schmidt" is someone whose first name begins
    with M, and "Morbus M." is no person at all."""
    p = _patient()
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    text = ("Da Herr M. zunehmend unruhig war, haben wir Herrn M. beobachtet. Pat. M. schlief. "
            "Wir sahen Fr. M. Die Untersuchung blieb ohne Befund.\nDer Bruder, Herr M. Schmidt, "
            "kam später. Ein Morbus M. liegt nicht vor, Herr K. war nicht da.")
    found = [(text[a:b], r.id) for a, b, r in find_in_text(text, rules)]
    assert found == [("M.", "name.nachname.initial")] * 4
    oe = Patient(surname="Özdemir", first_name="Aylin", sex="w", birth_date=dt.date(1980, 3, 4))
    found = [text[a:b] for text in ("Frau Ö. kam allein.", "Frau O. kam allein.")
             for a, b, _r in find_in_text(text, rules_for(oe, PseudonymAssignment(oe, "B")))]
    assert found == ["Ö.", "O."]


def test_adjacent_matches_merge() -> None:
    p = _patient()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Anschrift: Musterstraße 12, 80331 München, Tel.", fontname="helv")
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    hits = find(read_text_layer(page), rules)
    assert [f.pseudonym for f in hits] == ["[Wohnort]"]
    assert hits[0].text == "Musterstraße 12, 80331 München"


def test_address_codes_with_key() -> None:
    p = _patient()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Anschrift: Musterstr. 12, 80331 München; geboren in Muenchen.",
                     fontname="helv")
    assignment = PseudonymAssignment(p, "K7Q4MX", secret=bytes(32))
    hits = find(read_text_layer(page), rules_for(p, assignment))
    pseudonyms = [f.pseudonym for f in hits]
    assert len(pseudonyms) == 3
    assert pseudonyms[0].startswith("[Straße ") and pseudonyms[1].startswith("[PLZ Ort ")
    assert pseudonyms[2].startswith("[Ort ")
    assert pseudonyms[1].split()[-1] == pseudonyms[2].split()[-1]  # same town, same code
    assert assignment.mapping[pseudonyms[0]] == "Musterstraße 12"
    assert assignment.mapping[pseudonyms[1]] == "80331 München"
    assert assignment.mapping[pseudonyms[2]] == "München"


def test_generic_patterns_and_roles() -> None:
    p = _patient()
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    text = "KVNR X123456789, IBAN DE12 3456 7890 1234 5678 90, fremd@example.com"
    ids = sorted(r.id for _a, _b, r in find_in_text(text, rules))
    assert ids == ["muster.email", "muster.iban", "muster.versichertennummer"]
    # OCR puts spaces around dots: still one address
    matches = find_in_text("Mail an max. muster@example. org bitte", rules)
    assert [("max. muster@example. org", "muster.email")] == [
        ("Mail an max. muster@example. org bitte"[a:b], r.id) for a, b, r in matches]


def test_label_rules_match_only_the_value() -> None:
    p = _patient()
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    text = ("Versicherten-Nr. 0181590830 Pat.-ID 99887766 AZ 2024/555, Fall-Nr.: K-2024-0012\n"
            "Blutdruck 120/80 mmHg, Pat. wird vorgestellt.")
    matches = {text[a:b]: r.id for a, b, r in find_in_text(text, rules)}
    assert matches["0181590830"] == "muster.versichertennummer.etikett"
    assert matches["99887766"] == "muster.patienten_id.etikett"
    assert matches["2024/555"] == "muster.aktenzeichen.etikett"
    assert matches["K-2024-0012"] == "muster.aktenzeichen.etikett"
    assert "120/80" not in matches and "Versicherten-Nr." not in matches


def test_label_not_in_codes() -> None:
    """A pseudonym code that happens to contain "AZ" is not a file number."""
    p = _patient()
    rules = rules_for(p, PseudonymAssignment(p, "A"))
    text = ("Patient KAZ2Q4W und AZ 2024/17 sowie Vorgangs-Nr. X-1 und versicherten-nr."
            " 0181590830. AZ Sehr geehrte Frau, AZ MUSTER ohne Ziffer.")
    matches = {text[a:b]: r.id for a, b, r in find_in_text(text, rules)}
    assert "2024/17" in matches and "X-1" not in matches  # the value needs 4 characters
    assert matches["0181590830"] == "muster.versichertennummer.etikett"  # label any case
    assert not any("2Q4W" in t for t in matches)  # "AZ" inside a code is not a label
    assert "Sehr" not in matches and "MUSTER" not in matches  # values need a digit


def test_recheck_protects_misread_pseudonyms() -> None:
    """OCR reads "[Ort AZT63Z]" back as "(Ort AZT63Z)" or "Ort AZT632": the code inside
    must still count as a pseudonym, never as a file number."""
    from pseudonymed.checks.recheck import in_pseudonym, pseudonym_spans_tolerant
    from pseudonymed.detection.patterns import GLOBAL_PATTERNS

    rules = [Rule(id_, m, kind, "[x]", 5, None) for id_, m, kind in GLOBAL_PATTERNS]
    text = "Wohnhaft in (Ort AZT63Z), früher Ort AZT632, Akte AZ 2023/171."
    spans = pseudonym_spans_tolerant(text, ["[Ort AZT63Z]", "Patient K7Q4MX"])
    matches = [(text[a:b].rstrip("."), r.id) for a, b, r in find_in_text(text, rules)
               if not in_pseudonym(a, b, spans)]
    assert matches == [("2023/171", "muster.aktenzeichen.etikett")], matches


def _rule(id_: str, muster: str, priority: int) -> Rule:
    return Rule(id_, re.compile(muster), Kind.TERM, "[x]", priority, None)


def test_candidates_without_edge_and_with_value_group() -> None:
    """Blanks a pattern swallowed are cut off, a labelled pattern yields only its value,
    and a match of nothing but blanks is no candidate."""
    from pseudonymed.detection.rules import _candidates

    rules = [_rule("leer", r"\s*X\s*", 10), _rule("etikett", r"Nr\.\s*(?P<wert>\d+)", 5),
             _rule("nichts", r"\s+", 1)]
    text = "a  X  b Nr. 42"
    assert [(text[a:b], r.id) for a, b, r in _candidates(text, rules)] == [
        ("X", "leer"), ("42", "etikett")]


def test_selection_length_before_priority_before_order() -> None:
    """The longest candidate wins, at equal length the higher priority, then the earlier
    rule; whatever overlaps a chosen hit is dropped."""
    from pseudonymed.detection.rules import _without_overlap

    long, high = _rule("lang", "x", 1), _rule("hoch", "x", 9)
    early, late = _rule("frueh", "x", 5), _rule("spaet", "x", 5)
    candidates = [(0, 2, early), (0, 2, late), (0, 2, high), (2, 8, long), (3, 5, high),
                  (9, 12, late), (9, 12, early)]
    assert [(a, b, r.id) for a, b, r in _without_overlap(candidates)] == [
        (0, 2, "hoch"), (2, 8, "lang"), (9, 12, "spaet")]


def test_box_grouped_per_line() -> None:
    """A box over two lines becomes one hit per line (a word gap does not split a line),
    the drawn box itself is kept on the first hit, and an image page yields the box only."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Erste Zeile hier", fontname="helv")
    page.insert_text((72, 120), "Zweite Zeile", fontname="helv")
    rect = (70.0, 88.0, 200.0, 124.0)
    matches = box_to_hits(read_text_layer(page), rect, "[x]")
    assert [f.text for f in matches] == ["Erste Zeile hier", "Zweite Zeile"]
    assert matches[0].rects[-1] == rect and all(f.rule == "manuell" for f in matches)
    empty = PageText(number=2, width=595.0, height=842.0, text="Bild")
    (single,) = box_to_hits(empty, rect, "[x]", "maske")
    assert (single.start, single.end, single.rects, single.rule) == (0, 0, [rect], "maske")
