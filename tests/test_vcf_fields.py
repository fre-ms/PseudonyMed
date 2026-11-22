"""Field-family readers of the vCard module: parameters, names, salutation, contacts, X fields."""

from __future__ import annotations

import random

from pseudonymed.core.model import Patient
from pseudonymed.sources.vcf import (
    VCard,
    _decoded,
    _emails,
    _insurance_number,
    _name_from_fn,
    _name_parts,
    _notes,
    _parameter,
    _phones,
    _salutation_and_title,
    _sex,
    parse_vcards,
)
from tests.synthetic import kvnr


def _card(*lines: str) -> VCard:
    return parse_vcards("BEGIN:VCARD\nVERSION:4.0\n" + "\n".join(lines) + "\nEND:VCARD\n")[0]


def test_parameter() -> None:
    assert _parameter(["TYPE=HOME,VOICE", "PREF"]) == {"TYPE": ["HOME", "VOICE", "PREF"]}
    assert _parameter(['CHARSET="utf-8"', "encoding=QUOTED-PRINTABLE"]) == {
        "CHARSET": ["utf-8"],
        "ENCODING": ["QUOTED-PRINTABLE"],
    }
    assert _parameter(["type=cell"]) == {"TYPE": ["cell"]}  # names upper-cased, values kept
    assert _parameter(["TYPE="]) == {"TYPE": []}
    assert _parameter([]) == {}


def test_decodes_quoted_printable() -> None:
    assert _decoded("Gr=C3=BC=C3=9Fe", {"ENCODING": ["QUOTED-PRINTABLE"]}) == "Grüße"
    latin = {"ENCODING": ["quoted-printable"], "CHARSET": ["latin-1"]}
    assert _decoded("Gr=FC=DFe", latin) == "Grüße"
    unknown = {"ENCODING": ["QUOTED-PRINTABLE"], "CHARSET": ["kein-zeichensatz"]}
    assert _decoded("Gr=FC=DFe", unknown) == "Gr=FC=DFe"  # unreadable charset: left alone
    assert _decoded("Gr=FC=DFe", {}) == "Gr=FC=DFe"


def test_name_from_fn(patient: Patient) -> None:
    assert _name_from_fn("") == ("", "")
    assert _name_from_fn("  Nachname ") == ("Nachname", "")
    assert _name_from_fn(patient.full_name) == (patient.surname, patient.first_name)


def test_name_parts(patient: Patient) -> None:
    with_n = _card("N:Nachname;Vorname;Zweiter;Herr Dr.;jr.")
    assert _name_parts(with_n) == ("Nachname", "Vorname Zweiter", "Herr Dr.")
    fn_only = _card(f"FN:{patient.full_name}", "N:;;;Frau;")
    assert _name_parts(fn_only) == (patient.surname, patient.first_name, "Frau")
    assert _name_parts(_card("ORG:Beispiel GmbH")) == ("", "", "")


def test_salutation_and_title() -> None:
    assert _salutation_and_title("Herr Dr. med.") == ("m", "Dr. med.")
    assert _salutation_and_title("Frau Prof. Dr.") == ("w", "Prof. Dr.")
    assert _salutation_and_title("Mrs, Dr") == ("w", "Dr")
    assert _salutation_and_title("Dr. med.") == ("", "Dr. med.")
    assert _salutation_and_title("") == ("", "")


def test_sex() -> None:
    assert _sex(_card("GENDER:F;weiblich"), "") == "w"
    assert _sex(_card("GENDER:m"), "") == "m"
    assert _sex(_card("GENDER:F"), "m") == "m"  # the salutation wins
    assert _sex(_card("GENDER:O"), "") == ""
    assert _sex(_card("FN:niemand"), "") == ""


def test_phones() -> None:
    card = _card(
        "TEL;VALUE=uri:tel:+49 30 1234567",
        "TEL;TYPE=cell:0171 2345678",
        "TEL:+49 30 1234567",
        "TEL:123",
        "TEL:TEL:+4989123",
    )
    assert _phones(card) == ["+49 30 1234567", "0171 2345678", "+4989123"]
    assert _phones(_card("FN:niemand")) == []


def test_emails() -> None:
    card = _card(
        "EMAIL:mailto:Test.Konto@Example.ORG",
        "EMAIL;TYPE=home:test.konto@example.org",
        "EMAIL:ohne-klammeraffe",
        "EMAIL;TYPE=work:zweit@example.org",
    )
    assert _emails(card) == ["test.konto@example.org", "zweit@example.org"]


def test_notes() -> None:
    card = _card("NOTE:", "NOTE: Erster Hinweis ", "ROLE:Lehrkraft", "NOTE:Zweiter")
    assert _notes(card) == ["Erster Hinweis", "Zweiter", "Beruf: Lehrkraft"]
    assert _notes(_card("FN:niemand")) == []


def test_insurance_number() -> None:
    rng = random.Random(3)
    a, b, c = kvnr(rng), kvnr(rng), kvnr(rng)
    both = _card(f"X-KVNR:{b}", f"X-VERSICHERTENNUMMER:{a}")
    assert _insurance_number(both, [f"KVNR {c}"]) == a  # X fields in fixed preference order
    assert _insurance_number(_card(f"X-INSURANCE-NUMBER:{b}"), []) == b
    assert _insurance_number(_card("FN:niemand"), ["nichts", f"Nr. {c}, weiteres"]) == c
    assert _insurance_number(_card("FN:niemand"), [f"Code {c}1"]) == ""  # ten digits: no KVNR
    assert _insurance_number(_card("X-KVNR:"), []) == ""
