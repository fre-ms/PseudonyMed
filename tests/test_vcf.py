"""vCard reading: folding, escaping, parameters, DoctoNext fields, hand-made cards."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pseudonymed.kern.modell import Patient
from pseudonymed.quellen.vcf import parse_bday, parse_vcards, patient_aus_vcard, patienten_aus_vcf
from tests.synthetisch import vcard_aus_patient

HANDGEMACHT = """BEGIN:VCARD
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

KURZ = """BEGIN:VCARD
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


def test_doctonext_karte(patient: Patient) -> None:
    text = vcard_aus_patient(patient, "uid-1")
    assert any(len(z.encode("utf-8")) <= 75 for z in text.splitlines())
    karten = parse_vcards(text)
    assert len(karten) == 1
    k = karten[0]
    assert k.uid == "uid-1" and k.version == "4.0" and k.fn == patient.vollname
    p = patient_aus_vcard(k)
    assert (p.nachname, p.vorname, p.geschlecht) == (patient.nachname, patient.vorname, "m")
    assert p.geburtsdatum == patient.geburtsdatum
    assert (p.strasse, p.plz, p.ort) == (patient.strasse, patient.plz, patient.ort)
    assert p.telefone == patient.telefone and p.emails == patient.emails
    assert p.patienten_id == patient.patienten_id
    assert p.versichertennummer == patient.versichertennummer  # found in NOTE
    assert any("Ehepartner" in h for h in p.hinweise)
    assert p.schluessel() == patient.schluessel()


def test_handgemachte_karte() -> None:
    p = patient_aus_vcard(parse_vcards(HANDGEMACHT)[0])
    assert p.nachname == "Muster" and p.vorname == "Erika Maria"
    assert p.titel == "Dr. med." and p.geschlecht == ""
    assert p.telefone == ["+49 (0)30 1234567", "0171 2345678"]
    assert p.emails == ["erika.muster@example.org"]
    assert (p.strasse, p.plz, p.ort) == ("Musterstraße 12", "14467", "Potsdam")
    assert p.geburtsdatum == dt.date(1970, 5, 2)
    assert p.versichertennummer == "B987654321"  # X-KVNR wins over the NOTE
    assert p.hinweise[0] == "Versichertennr. A123456789, Angehörige: Max Muster; Tel. privat"


def test_unvollstaendige_karten() -> None:
    karten = parse_vcards(KURZ)
    assert len(karten) == 2
    a = patient_aus_vcard(karten[0])
    assert (a.nachname, a.vorname) == ("Lange", "Hans Peter")
    assert a.geburtsdatum is None  # no year
    assert a.telefone == ["+4989123"]
    b = patient_aus_vcard(karten[1])
    assert b.geschlecht == "w" and b.geburtsdatum is None


def test_parse_bday() -> None:
    assert parse_bday("19590721") == dt.date(1959, 7, 21)
    assert parse_bday("1959-07-21T00:00:00Z") == dt.date(1959, 7, 21)
    assert parse_bday("--0721") is None
    assert parse_bday("") is None


def test_datei_mit_bom_und_crlf(tmp_path: Path, patient: Patient, patientin: Patient) -> None:
    pfad = tmp_path / "export.vcf"
    inhalt = vcard_aus_patient(patient) + vcard_aus_patient(patientin)
    pfad.write_bytes(b"\xef\xbb\xbf" + inhalt.encode())
    patienten = patienten_aus_vcf(pfad)
    assert [p.nachname for p in patienten] == [patient.nachname, patientin.nachname]
    assert patienten[1].geschlecht == "w"
