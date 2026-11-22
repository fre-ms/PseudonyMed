"""Field scorers of the record linkage: the stored row, the name and date families and
the weighted combiner, each on its own.  All names are invented."""

from __future__ import annotations

import datetime as dt

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.matching import (
    PHONETIC_MATCH,
    WEIGHTS,
    _Candidate,
    _date_fields,
    _day_and_month_swapped,
    _name_fields,
    _part_similarity,
    _weighted_score,
    dice,
    name_similarity,
)
from tests.synthetic import synthetic_patient


def _k(surname: str = "Flantz", first_name: str = "Gotthard", birth_name: str = "",
       birth_date: dt.date | None = None) -> _Candidate:
    return _Candidate(surname=surname, first_name=first_name, birth_name=birth_name,
                      birth_date=birth_date)


def test_candidate_makes_empty_and_missing_values_uniform() -> None:
    p = synthetic_patient(seed=3)
    assert p.birth_date is not None
    line = {"nachname": p.surname, "vorname": None,
            "geburtsdatum": p.birth_date.isoformat() + "T00:00:00"}
    assert _Candidate.from_row(line) == _k(p.surname, "", "", p.birth_date)
    assert _Candidate.from_row({}) == _k("", "", "", None)
    assert _Candidate.from_row({"geburtsdatum": "unbekannt"}).birth_date is None
    born = dt.date(1960, 1, 1)
    assert _Candidate.from_row({"geburtsdatum": born}).birth_date == born
    assert _Candidate.from_row({"nachname": 0, "vorname": 12345}) == _k("", "12345", "", None)


def test_date_fields_per_component_with_swap() -> None:
    d = dt.date(1959, 3, 7)
    assert _date_fields(d, d) == {"geburtsjahr": 1.0, "geburtsmonat": 1.0, "geburtstag": 1.0}
    flipped = _date_fields(d, dt.date(1959, 7, 3))
    assert flipped == {"geburtsjahr": 1.0, "geburtsmonat": 0.5, "geburtstag": 0.5}
    # the swap is recognised independently of the year
    assert _date_fields(d, dt.date(1960, 7, 3)) == {"geburtsjahr": 0.0, "geburtsmonat": 0.5,
                                                    "geburtstag": 0.5}
    # one component equal is a plain typo, not a swap
    assert _date_fields(dt.date(1959, 3, 3), dt.date(1959, 3, 4)) == {
        "geburtsjahr": 1.0, "geburtsmonat": 1.0, "geburtstag": 0.0}
    assert _date_fields(None, d) == {}
    assert _date_fields(d, None) == {}


def test_day_and_month_swapped_needs_two_deviations() -> None:
    assert _day_and_month_swapped(dt.date(1959, 3, 7), dt.date(1959, 7, 3))
    assert not _day_and_month_swapped(dt.date(1959, 5, 5), dt.date(1959, 5, 5))
    assert not _day_and_month_swapped(dt.date(1959, 3, 7), dt.date(1959, 3, 8))


def test_name_fields_only_for_names_present_on_both_sides() -> None:
    p = Patient(surname="Flantz", first_name="Gotthard")
    assert _name_fields(p, _k()) == {"nachname": 1.0, "vorname": 1.0}
    assert set(_name_fields(p, _k(first_name=""))) == {"nachname"}
    assert set(_name_fields(Patient(surname="Flantz", first_name=""), _k())) == {"nachname"}
    assert _name_fields(Patient(surname="", first_name=""), _k()) == {}


def test_name_fields_birth_name_on_both_sides() -> None:
    married = Patient(surname="Weber", first_name="Gotthard", birth_name="Flantz")
    assert _name_fields(married, _k())["nachname"] == 1.0
    unmarried = Patient(surname="Flantz", first_name="Gotthard")
    assert _name_fields(unmarried, _k(surname="Weber", birth_name="Flantz"))["nachname"] == 1.0


def test_name_fields_swapped_names_only_if_better() -> None:
    p = Patient(surname="Flantz", first_name="Gotthard")
    assert _name_fields(p, _k(surname="Gotthard", first_name="Flantz")) == {
        "nachname": 1.0, "vorname": 1.0}
    fields = _name_fields(p, _k(first_name="Gotthart"))
    assert fields["nachname"] == 1.0
    assert fields["vorname"] == name_similarity("Gotthard", "Gotthart")


def test_weighted_score_is_weighted_mean() -> None:
    assert _weighted_score({}) == 0.0
    assert _weighted_score({"nachname": 1.0, "vorname": 0.0}) == pytest.approx(15.2 / 27.2)
    assert _weighted_score(dict.fromkeys(WEIGHTS, 1.0)) == pytest.approx(1.0)
    flipped = {"geburtsjahr": 1.0, "geburtsmonat": 0.5, "geburtstag": 0.5}
    expected = (5.1 + 0.5 * 3.6 + 0.5 * 4.9) / (5.1 + 3.6 + 4.9)
    assert _weighted_score(flipped) == pytest.approx(expected)


def test_partial_similarity_phonetics_only_from_three_characters() -> None:
    assert _part_similarity("Flantz", "Flantz") == 1.0
    assert _part_similarity("Meier", "Mayer") == PHONETIC_MATCH
    assert _part_similarity("Al", "Ol") == dice("Al", "Ol") < PHONETIC_MATCH
