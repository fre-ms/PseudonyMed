"""Plain dicts become persons: dates in every accepted form, lists from single values."""

from __future__ import annotations

import datetime as dt

from pseudonymed.kern.modell import Patient, _datum_aus, _liste_aus, _personenfelder


def test_datum_aus() -> None:
    assert _datum_aus("1970-01-02") == dt.date(1970, 1, 2)
    assert _datum_aus("2.1.1970") == dt.date(1970, 1, 2)
    assert _datum_aus(dt.datetime(1970, 1, 2, 10, 30)) == dt.date(1970, 1, 2)
    assert _datum_aus(dt.date(1970, 1, 2)) == dt.date(1970, 1, 2)
    assert _datum_aus("irgendwann") is None
    assert _datum_aus("") is None and _datum_aus(None) is None and _datum_aus(19700102) is None


def test_liste_aus() -> None:
    assert _liste_aus("0123") == ["0123"]
    assert _liste_aus("") == []
    assert _liste_aus(["1", "", 2, None]) == ["1", "2"]
    assert _liste_aus([]) == []


def test_personenfelder_und_patient() -> None:
    felder = _personenfelder({"nachname": " Beispiel ", "vorname": "Max", "telefon": "0123",
                              "email": "max@example.org", "geburtsdatum": "02.01.1970 10:30"})
    assert felder["nachname"] == "Beispiel" and felder["telefone"] == ["0123"]
    assert felder["emails"] == ["max@example.org"]
    assert felder["geburtsdatum"] == dt.date(1970, 1, 2)
    assert _personenfelder({"telefone": ["a"], "telefon": "b"})["telefone"] == ["a"]
    p = Patient.aus_dict({"nachname": "Beispiel", "aktenzeichen": [1],
                          "angehoerige": [{"nachname": "B", "telefon": "9"}]})
    assert p.angehoerige[0].telefone == ["9"] and p.aktenzeichen == ["1"]
    assert p.als_dict()["angehoerige"] == [{"nachname": "B", "telefone": ["9"]}]
