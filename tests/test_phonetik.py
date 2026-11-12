"""Context-dependent letters of the Kölner Phonetik.  All names are synthetic."""

from __future__ import annotations

import re

import pytest
from faker import Faker

from pseudonymed.pseudonyme.phonetik import koelner_phonetik


@pytest.mark.parametrize(
    ("wort", "code"),
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
def test_kontextabhaengige_buchstaben(wort: str, code: str) -> None:
    assert koelner_phonetik(wort) == code


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
def test_schreibvarianten_teilen_den_code(a: str, b: str) -> None:
    assert koelner_phonetik(a) == koelner_phonetik(b)


def test_c_ist_vor_l_und_r_nur_am_wortanfang_hart() -> None:
    assert koelner_phonetik("Cla").startswith("4")
    assert koelner_phonetik("Cra").startswith("4")
    assert koelner_phonetik("Acl") == "085"
    assert koelner_phonetik("Acr") == "087"


def test_synthetische_namen_liefern_wohlgeformte_codes() -> None:
    fake = Faker("de_DE")
    Faker.seed(2026)
    for _ in range(200):
        for name in (fake.first_name(), fake.last_name(), fake.city()):
            code = koelner_phonetik(name)
            assert re.fullmatch(r"\d*", code), name
            assert "0" not in code[1:], name  # zeros survive only at the start
            assert code == koelner_phonetik(name.upper()) == koelner_phonetik(name.lower()), name
            assert code == koelner_phonetik(name.replace("-", "").replace(" ", "")), name
