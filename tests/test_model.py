"""Plain dicts become persons: dates in every accepted form, lists from single values."""

from __future__ import annotations

import datetime as dt

from pseudonymed.core.model import Patient, _date_from, _list_from, _person_fields


def test_date_from() -> None:
    assert _date_from("1970-01-02") == dt.date(1970, 1, 2)
    assert _date_from("2.1.1970") == dt.date(1970, 1, 2)
    assert _date_from(dt.datetime(1970, 1, 2, 10, 30)) == dt.date(1970, 1, 2)
    assert _date_from(dt.date(1970, 1, 2)) == dt.date(1970, 1, 2)
    assert _date_from("irgendwann") is None
    assert _date_from("") is None and _date_from(None) is None and _date_from(19700102) is None


def test_list_from() -> None:
    assert _list_from("0123") == ["0123"]
    assert _list_from("") == []
    assert _list_from(["1", "", 2, None]) == ["1", "2"]
    assert _list_from([]) == []


def test_person_fields_and_patient() -> None:
    fields = _person_fields({"nachname": " Beispiel ", "vorname": "Max", "telefon": "0123",
                             "email": "max@example.org", "geburtsdatum": "02.01.1970 10:30"})
    assert fields["surname"] == "Beispiel" and fields["phones"] == ["0123"]
    assert fields["emails"] == ["max@example.org"]
    assert fields["birth_date"] == dt.date(1970, 1, 2)
    assert _person_fields({"telefone": ["a"], "telefon": "b"})["phones"] == ["a"]
    p = Patient.from_dict({"nachname": "Beispiel", "aktenzeichen": [1],
                           "angehoerige": [{"nachname": "B", "telefon": "9"}]})
    assert p.relatives[0].phones == ["9"] and p.case_numbers == ["1"]
    assert p.as_dict()["angehoerige"] == [{"nachname": "B", "telefone": ["9"]}]
