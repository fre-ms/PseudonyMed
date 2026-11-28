"""Fuzzy search: the helpers behind ``targets_for`` and ``find_fuzzy``.  Nothing here is real."""

from __future__ import annotations

import datetime as dt

from pseudonymed.core.model import Kind, Person, Role
from pseudonymed.detection import fuzzy
from pseudonymed.detection.fuzzy import Target
from pseudonymed.document.text_file import text_page
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from tests.synthetic import synthetic_patient


def _target(tokens: list[str], kind: Kind = Kind.SURNAME) -> Target:
    return Target(tokens, "[x]", kind, "name.surname", Role.PATIENT)


# -- targets ---------------------------------------------------------------------------------------
def test_surname_parts_once_and_from_four_characters() -> None:
    assert fuzzy._surnames("Flantz-Gotthard") == ["Flantz-Gotthard", "Flantz", "Gotthard"]
    assert fuzzy._surnames("von Ott") == ["von Ott"]
    assert fuzzy._surnames("Ott") == []
    assert fuzzy._surnames("Flantz Flantz") == ["Flantz Flantz", "Flantz"]


def test_name_targets_order_and_minimum_length() -> None:
    person = Person(surname="Flantz-Gotthard", first_name="Hildegund Jo", birth_name="Briemer")
    targets = fuzzy._name_targets("name", person, "[P]", Role.PATIENT)
    assert [(z.tokens, z.rule) for z in targets] == [
        (["Hildegund", "Flantz-Gotthard"], "name.full_name"),
        (["Flantz-Gotthard"], "name.surname"), (["Flantz"], "name.surname"),
        (["Gotthard"], "name.surname"), (["Hildegund"], "name.first_name"),
        (["Briemer"], "name.birth_name"),
    ]
    assert {z.pseudonym for z in targets} == {"[P]"} and {z.role for z in targets} == {Role.PATIENT}
    short = Person(surname="Ott", first_name="Jo")
    assert fuzzy._name_targets("name", short, "[P]", Role.PATIENT) == []


def test_date_targets_five_spellings() -> None:
    targets = fuzzy._date_targets(dt.date(1959, 7, 1), "[geb. 1959]")
    assert [z.tokens for z in targets] == [["01.07.1959"], ["1.7.1959"], ["01.07.59"],
                                           ["1.", "Juli", "1959"], ["1.", "Jul.", "1959"]]
    assert all(z.kind is Kind.BIRTH_DATE and z.rule == "date.birth_date" for z in targets)


def test_address_targets_short_street_name_dropped() -> None:
    v = PseudonymAssignment(synthetic_patient(3), "K7Q4MX", secret=bytes(32))
    person = Person(surname="Flantz", street="Am Ring 3", postcode="01234", place="Riesa")
    targets = fuzzy._address_targets("address", person, v, Role.REFERRER)
    assert [(z.tokens, z.rule) for z in targets] == [
        (["Riesa"], "address.place"), (["01234", "Riesa"], "address.postcode_place")]
    assert {z.role for z in targets} == {Role.REFERRER}
    long = Person(surname="Flantz", street="Ringstraße 3", place="Ulm")
    targets = fuzzy._address_targets("address", long, v, Role.PATIENT)
    assert [z.tokens for z in targets] == [["Ringstraße", "3"], ["Ringstraße"]]
    assert len({z.pseudonym for z in targets}) == 1


def test_targets_for_groups_patient_before_companions() -> None:
    p = synthetic_patient(7)
    v = PseudonymAssignment(p, "K7Q4MX", secret=bytes(32))
    groups = [z.rule.split(".")[0] for z in fuzzy.targets_for(p, v)]
    first = [g for i, g in enumerate(groups) if g not in groups[:i]]
    expected = ["name", "address", "date", "insurance_number", "patient_id", "email",
                "case_number", "relative1", "referrer1"]
    assert first[0] == "name" and first[-1] == "referrer1"
    assert first == sorted(first, key=expected.index)


# -- the search ------------------------------------------------------------------------------------
def test_searchable_requires_characters_in_every_token() -> None:
    assert fuzzy._searchable(_target(["1.", "Juli", "1959"]))
    assert not fuzzy._searchable(_target(["1.", "...", "1959"]))
    assert not fuzzy._searchable(_target([]))


def test_without_edge_cuts_punctuation() -> None:
    text = '("Fiantz,") ...'
    assert fuzzy._without_edges(text, 0, 11) == (2, 8) and text[2:8] == "Fiantz"
    assert fuzzy._without_edges(text, 12, 15) == (15, 15)


def test_large_enough_only_for_names_and_places() -> None:
    name = _target(["lang"], Kind.SURNAME)
    assert not fuzzy._capitalised_enough(name, "lang", True)
    assert fuzzy._capitalised_enough(name, "Lang", True)
    assert fuzzy._capitalised_enough(name, "lang", False)
    assert fuzzy._capitalised_enough(_target(["lang"], Kind.EMAIL), "lang", True)


def test_split_windows_only_on_one_line_and_without_glued_word() -> None:
    s = text_page("Mail gotthard. flantz@example. org Ende\n"
                  "gotthard. flantz@example.\norg\n"
                  "x gotthard.flantz@example.org Ende")
    tokens = fuzzy._tokens(s.text)
    window = list(fuzzy._split_windows(s, tokens, "gotthard.flantz@example.org"))
    # pairs first, then triples; (6, 7) crosses a line, (9, 10) has a whole word glued on
    assert window == [(1, 2), (5, 6), (8, 9), (1, 3)]


def test_without_overlap_longest_first() -> None:
    z = _target(["x"])
    candidates = [(0, 5, z), (0, 12, z), (20, 25, z), (30, 34, z)]
    occupied = [(22, 30)]
    assert fuzzy._without_overlap(candidates, occupied) == [(0, 12, z), (30, 34, z)]
    assert occupied == [(22, 30), (0, 12), (30, 34)]
