"""Field-family readers of the vCard module: parameters, names, salutation, contacts, X fields."""

from __future__ import annotations

import random

from pseudonymed.kern.modell import Patient
from pseudonymed.quellen.vcf import (
    VCard,
    _anrede_und_titel,
    _dekodiert,
    _emails,
    _geschlecht,
    _hinweise,
    _name_aus_fn,
    _namensteile,
    _parameter,
    _telefone,
    _versichertennummer,
    parse_vcards,
)
from tests.synthetisch import kvnr


def _karte(*zeilen: str) -> VCard:
    return parse_vcards("BEGIN:VCARD\nVERSION:4.0\n" + "\n".join(zeilen) + "\nEND:VCARD\n")[0]


def test_parameter() -> None:
    assert _parameter(["TYPE=HOME,VOICE", "PREF"]) == {"TYPE": ["HOME", "VOICE", "PREF"]}
    assert _parameter(['CHARSET="utf-8"', "encoding=QUOTED-PRINTABLE"]) == {
        "CHARSET": ["utf-8"],
        "ENCODING": ["QUOTED-PRINTABLE"],
    }
    assert _parameter(["type=cell"]) == {"TYPE": ["cell"]}  # names upper-cased, values kept
    assert _parameter(["TYPE="]) == {"TYPE": []}
    assert _parameter([]) == {}


def test_dekodiert_quoted_printable() -> None:
    assert _dekodiert("Gr=C3=BC=C3=9Fe", {"ENCODING": ["QUOTED-PRINTABLE"]}) == "Grüße"
    latin = {"ENCODING": ["quoted-printable"], "CHARSET": ["latin-1"]}
    assert _dekodiert("Gr=FC=DFe", latin) == "Grüße"
    unbekannt = {"ENCODING": ["QUOTED-PRINTABLE"], "CHARSET": ["kein-zeichensatz"]}
    assert _dekodiert("Gr=FC=DFe", unbekannt) == "Gr=FC=DFe"  # unreadable charset: left alone
    assert _dekodiert("Gr=FC=DFe", {}) == "Gr=FC=DFe"


def test_name_aus_fn(patient: Patient) -> None:
    assert _name_aus_fn("") == ("", "")
    assert _name_aus_fn("  Nachname ") == ("Nachname", "")
    assert _name_aus_fn(patient.vollname) == (patient.nachname, patient.vorname)


def test_namensteile(patient: Patient) -> None:
    mit_n = _karte("N:Nachname;Vorname;Zweiter;Herr Dr.;jr.")
    assert _namensteile(mit_n) == ("Nachname", "Vorname Zweiter", "Herr Dr.")
    nur_fn = _karte(f"FN:{patient.vollname}", "N:;;;Frau;")
    assert _namensteile(nur_fn) == (patient.nachname, patient.vorname, "Frau")
    assert _namensteile(_karte("ORG:Beispiel GmbH")) == ("", "", "")


def test_anrede_und_titel() -> None:
    assert _anrede_und_titel("Herr Dr. med.") == ("m", "Dr. med.")
    assert _anrede_und_titel("Frau Prof. Dr.") == ("w", "Prof. Dr.")
    assert _anrede_und_titel("Mrs, Dr") == ("w", "Dr")
    assert _anrede_und_titel("Dr. med.") == ("", "Dr. med.")
    assert _anrede_und_titel("") == ("", "")


def test_geschlecht() -> None:
    assert _geschlecht(_karte("GENDER:F;weiblich"), "") == "w"
    assert _geschlecht(_karte("GENDER:m"), "") == "m"
    assert _geschlecht(_karte("GENDER:F"), "m") == "m"  # the salutation wins
    assert _geschlecht(_karte("GENDER:O"), "") == ""
    assert _geschlecht(_karte("FN:niemand"), "") == ""


def test_telefone() -> None:
    karte = _karte(
        "TEL;VALUE=uri:tel:+49 30 1234567",
        "TEL;TYPE=cell:0171 2345678",
        "TEL:+49 30 1234567",
        "TEL:123",
        "TEL:TEL:+4989123",
    )
    assert _telefone(karte) == ["+49 30 1234567", "0171 2345678", "+4989123"]
    assert _telefone(_karte("FN:niemand")) == []


def test_emails() -> None:
    karte = _karte(
        "EMAIL:mailto:Test.Konto@Example.ORG",
        "EMAIL;TYPE=home:test.konto@example.org",
        "EMAIL:ohne-klammeraffe",
        "EMAIL;TYPE=work:zweit@example.org",
    )
    assert _emails(karte) == ["test.konto@example.org", "zweit@example.org"]


def test_hinweise() -> None:
    karte = _karte("NOTE:", "NOTE: Erster Hinweis ", "ROLE:Lehrkraft", "NOTE:Zweiter")
    assert _hinweise(karte) == ["Erster Hinweis", "Zweiter", "Beruf: Lehrkraft"]
    assert _hinweise(_karte("FN:niemand")) == []


def test_versichertennummer() -> None:
    rng = random.Random(3)
    a, b, c = kvnr(rng), kvnr(rng), kvnr(rng)
    beide = _karte(f"X-KVNR:{b}", f"X-VERSICHERTENNUMMER:{a}")
    assert _versichertennummer(beide, [f"KVNR {c}"]) == a  # X fields in fixed preference order
    assert _versichertennummer(_karte(f"X-INSURANCE-NUMBER:{b}"), []) == b
    assert _versichertennummer(_karte("FN:niemand"), ["nichts", f"Nr. {c}, weiteres"]) == c
    assert _versichertennummer(_karte("FN:niemand"), [f"Code {c}1"]) == ""  # ten digits: no KVNR
    assert _versichertennummer(_karte("X-KVNR:"), []) == ""
