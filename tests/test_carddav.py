"""CardDAV client against the in-process fake server."""

from __future__ import annotations

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError, fits
from tests.carddav_fake import FakeCardDAV


def _client(server: FakeCardDAV, password: str | None = None) -> CardDAVClient:
    return CardDAVClient(server.url, server.user, password or server.password)


def test_address_books(fake_carddav: FakeCardDAV) -> None:
    books = _client(fake_carddav).address_books()
    assert [(b.uri, b.name) for b in books] == [("contacts", "Kontakte"),
                                                ("medifed-patients", "Patienten")]
    request = fake_carddav.requests[-1]
    assert request.method == "PROPFIND" and request.headers["Depth"] == "1"
    assert "application/xml" in request.headers["Content-Type"]
    assert "displayname" in request.body


def test_choose_address_book(fake_carddav: FakeCardDAV) -> None:
    c = _client(fake_carddav)
    assert c.address_book("medifed-patients").name == "Patienten"
    assert c.address_book("patienten").uri == "medifed-patients"
    with pytest.raises(CardDAVError, match="Mehrere Adressbücher"):
        c.address_book("")
    with pytest.raises(CardDAVError, match="nicht gefunden"):
        c.address_book("gibtsnicht")


def test_search(fake_carddav: FakeCardDAV, patient: Patient, female_patient: Patient) -> None:
    c = _client(fake_carddav)
    book = c.address_book("medifed-patients")
    matches = c.search(book, patient.surname[:4].lower())
    assert [k.patient.surname for k in matches] == [patient.surname]
    k = matches[0]
    assert k.etag == '"etag0"' and k.href.endswith("karte0.vcf")
    assert k.patient.birth_date == patient.birth_date
    assert k.patient.key() == patient.key()
    report = fake_carddav.requests[-1]
    assert report.method == "REPORT" and report.headers["Depth"] == "1"
    assert "addressbook-query" in report.body and "i;unicode-casemap" in report.body
    # second token is filtered locally
    year = str(patient.birth_date.year)
    assert len(c.search(book, f"{patient.surname} {year}")) == 1
    assert c.search(book, f"{patient.surname} 1800") == []
    assert len(c.search(book, patient.birth_date.strftime("%d.%m.%Y"))) == 1
    all_items = c.all_items(book)
    assert {k.patient.surname for k in all_items} == {patient.surname, female_patient.surname}
    assert c.search(c.address_book("contacts"), "x") == []


def test_wrong_password(fake_carddav: FakeCardDAV) -> None:
    with pytest.raises(CardDAVError, match="Anmeldung abgelehnt"):
        _client(fake_carddav, "falsch").address_books()


def test_only_https_except_localhost() -> None:
    with pytest.raises(CardDAVError, match="https"):
        CardDAVClient("http://cloud.example.org", "u", "p")
    CardDAVClient("https://cloud.example.org/", "u", "p")  # fine
    with pytest.raises(CardDAVError, match="Server nicht erreichbar"):
        CardDAVClient("http://127.0.0.1:9", "u", "p", timeout=2).address_books()


def test_matches(patient: Patient) -> None:
    assert fits(patient, patient.surname.upper())
    assert fits(patient, f"{patient.first_name[:3]} {patient.birth_date.year}")
    assert fits(patient, patient.birth_date.strftime("%d.%m.%Y"))
    assert not fits(patient, "Zyxwv")
    assert not fits(patient, "1800")
    p = Patient(surname="Müller", first_name="Jörg")
    assert fits(p, "mueller") and fits(p, "muller") and fits(p, "Jörg")
