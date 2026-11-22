"""vCard reading: folding, escaping, parameters, DoctoNext fields, hand-made cards."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pseudonymed.core.model import Patient
from pseudonymed.sources.vcf import parse_bday, parse_vcards, patient_from_vcard, patients_from_vcf
from tests.synthetic import vcard_from_patient

HANDMADE = """BEGIN:VCARD
VERSION:3.0
N:Muster;Erika;Maria;Dr. med.;
FN:Dr. med. Erika Maria Muster
item1.TEL;TYPE=HOME,VOICE:tel:+49 (0)30 1234567
item1.X-ABLabel:Festnetz
TEL;CELL:0171 2345678
EMAIL;TYPE=INTERNET;PREF:Erika.Muster@Example.ORG
ADR;TYPE=WORK:;;Arbeitsweg 1;Berlin;;10115;Deutschland
ADR;TYPE=HOME:;Hinterhaus;Musterstraße 12\\nAufgang B;Potsdam;;14467;Deutschland
BDAY:1970-05-02
NOTE:Versichertennr. A123456789\\, Angehörige: Max Muster\\; Tel. privat
X-KVNR:B987654321
ORG:Beispiel GmbH;Abteilung\\, Entwicklung
END:VCARD
"""

SHORT = """BEGIN:VCARD
VERSION:4.0
FN:Hans Peter Lange
BDAY:--0315
TEL;VALUE=uri:tel:+4989123
END:VCARD
BEGIN:VCARD
VERSION:4.0
N:Kurz;Anna;;;
GENDER:F
BDAY;VALUE=text:circa 1980
END:VCARD
"""


def test_doctonext_card(patient: Patient) -> None:
    text = vcard_from_patient(patient, "uid-1")
    assert any(len(z.encode("utf-8")) <= 75 for z in text.splitlines())
    cards = parse_vcards(text)
    assert len(cards) == 1
    k = cards[0]
    assert k.uid == "uid-1" and k.version == "4.0" and k.fn == patient.full_name
    p = patient_from_vcard(k)
    assert (p.surname, p.first_name, p.sex) == (patient.surname, patient.first_name, "m")
    assert p.birth_date == patient.birth_date
    assert (p.street, p.postcode, p.place) == (patient.street, patient.postcode, patient.place)
    assert p.phones == patient.phones and p.emails == patient.emails
    assert p.patient_id == patient.patient_id
    assert p.insurance_number == patient.insurance_number  # found in NOTE
    assert any("Ehepartner" in h for h in p.notes)
    assert p.key() == patient.key()


def test_handmade_card() -> None:
    p = patient_from_vcard(parse_vcards(HANDMADE)[0])
    assert p.surname == "Muster" and p.first_name == "Erika Maria"
    assert p.title == "Dr. med." and p.sex == ""
    assert p.phones == ["+49 (0)30 1234567", "0171 2345678"]
    assert p.emails == ["erika.muster@example.org"]
    assert (p.street, p.postcode, p.place) == ("Musterstraße 12", "14467", "Potsdam")
    assert p.birth_date == dt.date(1970, 5, 2)
    assert p.insurance_number == "B987654321"  # X-KVNR wins over the NOTE
    assert p.notes[0] == "Versichertennr. A123456789, Angehörige: Max Muster; Tel. privat"


def test_incomplete_cards() -> None:
    cards = parse_vcards(SHORT)
    assert len(cards) == 2
    a = patient_from_vcard(cards[0])
    assert (a.surname, a.first_name) == ("Lange", "Hans Peter")
    assert a.birth_date is None  # no year
    assert a.phones == ["+4989123"]
    b = patient_from_vcard(cards[1])
    assert b.sex == "w" and b.birth_date is None


def test_parse_bday() -> None:
    assert parse_bday("19590721") == dt.date(1959, 7, 21)
    assert parse_bday("1959-07-21T00:00:00Z") == dt.date(1959, 7, 21)
    assert parse_bday("--0721") is None
    assert parse_bday("") is None


def test_file_with_bom_and_crlf(tmp_path: Path, patient: Patient, female_patient: Patient) -> None:
    path = tmp_path / "export.vcf"
    content = vcard_from_patient(patient) + vcard_from_patient(female_patient)
    path.write_bytes(b"\xef\xbb\xbf" + content.encode())
    patients = patients_from_vcf(path)
    assert [p.surname for p in patients] == [patient.surname, female_patient.surname]
    assert patients[1].sex == "w"
