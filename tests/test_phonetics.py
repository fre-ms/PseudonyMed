"""Context-dependent letters of the Kölner Phonetik.  All names are synthetic."""

from __future__ import annotations

import re

import pytest
from faker import Faker

from pseudonymed.pseudonyms.phonetics import cologne_phonetics


@pytest.mark.parametrize(
    ("word", "code"),
    [
        # H is silent, also at the start and in runs
        ("Hans", "068"), ("Hahn", "06"), ("H", ""), ("Hhh", ""),
        # P before H sounds like F
        ("Philipp", "351"), ("Joseph", "083"),
        # D and T before C, S or Z merge into the sibilant
        ("Dietz", "28"), ("Matz", "68"), ("Dschungel", "8645"), ("Todt", "22"),
        # C at the start: hard before A, H, K, L, O, Q, R, U, X; soft otherwise
        ("Claudia", "452"), ("Christian", "47826"), ("Carl", "475"), ("Celine", "856"),
        ("Cyrill", "875"),
        # C inside a word: always soft after S or Z; L and R no longer make it hard
        ("Scott", "82"), ("Ascher", "087"), ("Oclu", "085"), ("Acra", "087"),
        ("Michael", "645"), ("Eckart", "0472"),
        # X is KS unless a C, K or Q already carries the K
        ("Xaver", "4837"), ("Max", "648"), ("Marx", "6748"), ("Axel", "0485"), ("Ckx", "48"),
        ("Luxus", "5488"),
        # separators, digits and blanks are dropped before coding
        ("O'Brien", "0176"), ("Anna-Lena", "0656"), ("Müller, Hans", "65768"), ("Hans2", "068"),
        ("123", ""), ("", ""), ("   ", ""), ("-", ""),
    ],
)
def test_context_dependent_letters(word: str, code: str) -> None:
    assert cologne_phonetics(word) == code


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Hans", "Ans"), ("Philipp", "Filip"), ("Stephan", "Stefan"), ("Dietz", "Diez"),
        ("Matz", "Maz"), ("Dschungel", "Schungel"), ("Claudia", "Klaudia"),
        ("Christian", "Kristian"), ("Carl", "Karl"), ("Celine", "Seline"), ("Fuchs", "Fux"),
        ("Lux", "Luks"), ("Jürgen", "Juergen"), ("Straße", "Strasse"), ("Renée", "Renee"),
        ("François", "Francois"), ("Muñoz", "Munoz"), ("Ångström", "Angstrom"),
        ("O'Brien", "OBrien"), ("Anna-Lena", "Annalena"), ("Hans2", "Hans"), ("SCHMIDT", "schmidt"),
    ],
)
def test_spelling_variants_share_the_code(a: str, b: str) -> None:
    assert cologne_phonetics(a) == cologne_phonetics(b)


def test_c_before_l_and_r_is_hard_only_at_word_start() -> None:
    assert cologne_phonetics("Cla").startswith("4")
    assert cologne_phonetics("Cra").startswith("4")
    assert cologne_phonetics("Acl") == "085"
    assert cologne_phonetics("Acr") == "087"


def test_synthetic_names_yield_well_formed_codes() -> None:
    fake = Faker("de_DE")
    Faker.seed(2026)
    for _ in range(200):
        for name in (fake.first_name(), fake.last_name(), fake.city()):
            code = cologne_phonetics(name)
            assert re.fullmatch(r"\d*", code), name
            assert "0" not in code[1:], name  # zeros survive only at the start
            assert code == cologne_phonetics(name.upper()) == cologne_phonetics(name.lower()), name
            assert code == cologne_phonetics(name.replace("-", "").replace(" ", "")), name
