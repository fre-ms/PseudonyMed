"""Phonetic codes, keyed-hash identifiers and their check character."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.patient_code import (
    ALPHABET,
    canonical_identity,
    canonical_place,
    canonical_street,
    check_character,
    hash_code,
    is_valid_code,
    main_part,
    split_house_number,
)
from pseudonymed.pseudonyms.phonetics import cologne_phonetics

KEY = bytes(range(32))


@pytest.mark.parametrize(
    ("word", "code"),
    [("Wikipedia", "3412"), ("Breschnew", "17863"), ("Müller-Lüdenscheidt", "65752682"),
     ("Müller", "657"), ("Mueller", "657"), ("Miller", "657"), ("Meier", "67"), ("Mayer", "67"),
     ("Meyer", "67"), ("Schmidt", "862"), ("Schmitt", "862"), ("Christian", "47826"),
     ("Kristian", "47826"), ("", "")],
)
def test_cologne_phonetics(word: str, code: str) -> None:
    assert cologne_phonetics(word) == code


def test_check_character() -> None:
    code = "K7Q4M"
    full = code + check_character(code)
    assert is_valid_code(full)
    for i in range(len(full)):
        for c in ALPHABET:
            if c != full[i]:
                assert not is_valid_code(full[:i] + c + full[i + 1 :])  # every single error
    assert not is_valid_code("K7Q4M0")  # 0 is not in the alphabet
    assert not is_valid_code("")


def test_hash_reproducible_and_key_dependent() -> None:
    a = hash_code(KEY, "657|47|1959-07-21")
    assert len(a) == 6 and is_valid_code(a) and set(a) <= set(ALPHABET)
    assert a == hash_code(KEY, "657|47|1959-07-21")
    assert a != hash_code(bytes(32), "657|47|1959-07-21")
    assert a != hash_code(KEY, "657|47|1959-07-22")
    assert a != hash_code(KEY, "657|47|1959-07-21", variant=1)
    assert len(hash_code(KEY, "x", length=8)) == 9


def test_canonical_identity_tolerates_spellings() -> None:
    base = Patient(surname="Müller", first_name="Hans Peter", birth_date=dt.date(1960, 2, 1))
    k = canonical_identity(base)
    assert k == "657|068|1960-02-01"
    for variant in (
        dataclasses.replace(base, surname="Mueller"),
        dataclasses.replace(base, surname="MÜLLER", first_name="Hans-Peter"),
        dataclasses.replace(base, surname="Miller", first_name="Hanns"),
        dataclasses.replace(base, surname="von Müller"),
    ):
        assert canonical_identity(variant) == k
    assert canonical_identity(dataclasses.replace(base, birth_date=dt.date(1961, 2, 1))) != k
    assert canonical_identity(dataclasses.replace(base, first_name="Peter Hans")) != k
    without = dataclasses.replace(base, birth_date=None)
    assert canonical_identity(without) == "muller|hans|"
    assert cologne_phonetics("Hans") == cologne_phonetics("Hanns") == "068"


def test_main_part() -> None:
    assert main_part("Müller-Lüdenscheidt") == "Lüdenscheidt"
    assert main_part("von der Leyen") == "Leyen"
    assert main_part("de") == "de"
    assert main_part("") == ""


def test_canonical_street() -> None:
    k = canonical_street("Musterstraße 12")
    assert k == "strasse:6827:str:12"
    for variant in ("Musterstr. 12", "Muster-Strasse 12", "MUSTERSTRASSE 12", "Musterstraße, 12",
                    "Musterstraße 12 "):
        assert canonical_street(variant) == k, variant
    assert canonical_street("Musterstraße 12a") == "strasse:6827:str:12a"
    assert canonical_street("Musterstraße 14") != k
    assert canonical_street("Musterweg 12") != k
    assert canonical_street("Lindenweg 5") == canonical_street("Linden-Weg 5")
    assert canonical_street("Am Alten Markt 3") == canonical_street("Am alten Markt 3")
    assert canonical_street("Straße des Friedens 1").startswith("strasse:8278")
    assert split_house_number("Hauptstraße 12 a") == ("Hauptstraße", "12a")
    assert split_house_number("Hauptstraße") == ("Hauptstraße", "")


def test_canonical_place() -> None:
    assert canonical_place("München") == canonical_place("Muenchen") == canonical_place("MÜNCHEN")
    assert canonical_place("Frankfurt am Main") == canonical_place("Frankfurt a. Main")
    assert canonical_place("Frankfurt am Main") != canonical_place("Frankfurt (Oder)")
    assert canonical_place("Bad Tölz") == canonical_place("Tölz")
    assert canonical_place("Riesa") != canonical_place("Dresden")
