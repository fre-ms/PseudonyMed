"""The address books of the Mac's Contacts app, read through a stand-in for Apple's
framework (tests/contacts_fake.py); every card is synthetic."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed import config as config_module
from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.sources import address_book, mac_contacts
from pseudonymed.sources.vcf import parse_vcards, patient_from_vcard
from tests.contacts_fake import (
    AUTHORIZED,
    DENIED,
    HOME,
    NOT_DETERMINED,
    WORK,
    Address,
    Contact,
    Container,
    Day,
    FakeContacts,
    Labelled,
    contact_from_patient,
)
from tests.synthetic import vcard_from_patient
from tests.test_gui import qapp  # noqa: F401


@pytest.fixture
def data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "daten"
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(folder))
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_URL", raising=False)
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_PASSWORT", raising=False)
    return folder


def _framework(monkeypatch: pytest.MonkeyPatch, *containers: Container,
               status: int = AUTHORIZED, answer: int = AUTHORIZED) -> FakeContacts:
    fake = FakeContacts(list(containers), status=status, answer=answer)
    monkeypatch.setattr(mac_contacts, "_framework", lambda: fake)
    return fake


def _books(patient: Patient, female_patient: Patient) -> tuple[Container, Container]:
    """iCloud with both patients, a local book with the woman again (linked) and a firm."""
    icloud = Container("icloud-1", "iCloud", 3, [contact_from_patient(patient, "c1", "Dr."),
                                                 contact_from_patient(female_patient, "c2")])
    local = Container("local-1", "", 1, [contact_from_patient(female_patient, "c2"),
                                         Contact("c3")])  # a firm: no name of a person
    return icloud, local


def test_a_card_reads_as_its_vcard_does(monkeypatch: pytest.MonkeyPatch,
                                        patient: Patient) -> None:
    """The same card from the Contacts app and from a vCard file gives the same patient."""
    fake = _framework(monkeypatch)
    p = dataclasses.replace(patient, birth_name="Geburtsname-Beispiel")
    from_app = patient_from_vcard(mac_contacts.vcard(contact_from_patient(p, "c1", "Dr."), fake))
    from_file = patient_from_vcard(parse_vcards(vcard_from_patient(p, "c1"))[0])
    for name in ("surname", "first_name", "sex", "birth_name", "birth_date", "street",
                 "postcode", "place", "phones", "emails"):
        assert getattr(from_app, name) == getattr(from_file, name), name
    assert from_app.title == "Dr." and from_app.key() == p.key()
    assert not from_app.insurance_number and not from_app.patient_id  # the app has no X fields


def test_separators_birthdays_and_the_home_address(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _framework(monkeypatch)
    contact = Contact(
        "c9", family="Muster; Beispiel", given="Anna, Maria", middle="Luise", prefix="Frau Prof.",
        job="Lehrerin", born=Day(3, 4),  # a birthday without a year
        addresses=[Labelled(WORK, Address("Schulweg 1", "Arbeitsort", "11111")),
                   Labelled(HOME, Address("Gartenstraße 2\nHinterhaus", "Wohnort", "22222"))])
    p = patient_from_vcard(mac_contacts.vcard(contact, fake))
    assert (p.surname, p.first_name) == ("Muster; Beispiel", "Anna, Maria Luise")
    assert (p.sex, p.title, p.birth_date) == ("w", "Prof.", None)
    assert (p.street, p.postcode, p.place) == ("Gartenstraße 2", "22222", "Wohnort")
    assert p.notes == ["Beruf: Lehrerin"]
    contact.born = Day(3, 4, 1961)
    assert patient_from_vcard(mac_contacts.vcard(contact, fake)).birth_date.isoformat() == \
        "1961-04-03"


def test_search_in_every_book_or_the_chosen(monkeypatch: pytest.MonkeyPatch, patient: Patient,
                                            female_patient: Patient) -> None:
    fake = _framework(monkeypatch, *_books(patient, female_patient))
    assert [b.shown for b in mac_contacts.address_books()] == ["iCloud (CardDAV)",
                                                               "auf diesem Mac"]
    assert mac_contacts.address_books()[1].shown.en == "on this Mac"
    everyone = mac_contacts.search("")
    assert sorted(p.surname for p in everyone) == sorted([patient.surname,
                                                          female_patient.surname])
    assert [p.surname for p in mac_contacts.search(female_patient.first_name)] == \
        [female_patient.surname]  # the linked card comes once, the firm not at all
    assert [p.surname for p in mac_contacts.search("", ["local-1"])] == [female_patient.surname]
    assert [p.surname for p in mac_contacts.search(patient.birth_date.strftime("%d.%m.%Y"))] \
        == [patient.surname]
    with pytest.raises(mac_contacts.MacContactsError, match="nicht mehr"):
        mac_contacts.search("", ["verschwunden"])
    assert all("note" not in str(k).lower() for keys in fake.keys for k in keys)
    books = mac_contacts.address_books()
    assert mac_contacts.choose(books, ["icloud"]) == books[:1]  # by name, any case
    assert mac_contacts.choose(books, ["local-1", "iCloud"]) == [books[1], books[0]]
    with pytest.raises(mac_contacts.MacContactsError, match="vorhanden: iCloud"):
        mac_contacts.choose(books, ["Arbeit"])


def test_access_is_asked_once_and_never_by_a_search(monkeypatch: pytest.MonkeyPatch,
                                                    patient: Patient) -> None:
    book = Container("icloud-1", "iCloud", 3, [contact_from_patient(patient, "c1")])
    assert mac_contacts.access() == mac_contacts.UNAVAILABLE and not mac_contacts.supported()
    fake = _framework(monkeypatch, book, status=NOT_DETERMINED)
    assert mac_contacts.access() == mac_contacts.UNDECIDED
    with pytest.raises(mac_contacts.MacContactsError, match="noch nicht gefragt"):
        mac_contacts.search("")
    assert fake.asked == 0
    assert mac_contacts.ask() == mac_contacts.GRANTED and fake.asked == 1
    assert mac_contacts.ask() == mac_contacts.GRANTED and fake.asked == 1
    assert mac_contacts.search(patient.surname)
    refused = _framework(monkeypatch, book, status=NOT_DETERMINED, answer=DENIED)
    assert mac_contacts.ask() == mac_contacts.DENIED and refused.asked == 1
    assert mac_contacts.ask() == mac_contacts.DENIED and refused.asked == 1
    assert "Datenschutz & Sicherheit" in mac_contacts.access_text(mac_contacts.DENIED)
    assert "Privacy & Security" in mac_contacts.access_text(mac_contacts.DENIED).en


def test_with_the_other_address_books(data_folder: Path, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch, patient: Patient,
                                      female_patient: Patient) -> None:
    """The Mac's contacts join the vCard file; the same patient comes once, and a refusal
    of macOS leaves the vCard file answering."""
    _framework(monkeypatch, *_books(patient, female_patient))
    e = config_module.load_settings()
    assert not e.mac_contacts and not address_book.is_set_up()
    e.mac_contacts, e.mac_address_books = True, ["icloud-1", "local-1"]
    config_module.save_settings(e)
    loaded = config_module.load_settings()
    assert loaded.mac_contacts and loaded.mac_address_books == ["icloud-1", "local-1"]
    assert address_book.is_set_up()
    vcf = tmp_path / "k.vcf"
    vcf.write_text(vcard_from_patient(patient, "u1"), encoding="utf-8")
    loaded.local_address_book = str(vcf)
    config_module.save_settings(loaded)
    result = address_book.search("")
    assert sorted(p.surname for p in result.patients) == sorted([patient.surname,
                                                                 female_patient.surname])
    assert result.patients[0].patient_id == patient.patient_id  # the vCard file's comes first
    assert "Kontakte dieses Mac" in result.sources and not result.error
    # switched on but never asked (a new build has a new signature): the search asks once
    fake = _framework(monkeypatch, *_books(patient, female_patient), status=NOT_DETERMINED)
    assert len(address_book.search("").patients) == 2 and fake.asked == 1
    assert len(address_book.search("").patients) == 2 and fake.asked == 1
    _framework(monkeypatch, *_books(patient, female_patient), status=NOT_DETERMINED,
               answer=DENIED)
    result = address_book.search("")
    assert [p.surname for p in result.patients] == [patient.surname]
    assert len(result.error) == 1 and "Kein Zugriff" in result.error[0]
    assert "No access" in result.error[0].en


def test_the_command_line(data_folder: Path, monkeypatch: pytest.MonkeyPatch,
                          patient: Patient, female_patient: Patient) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["kontakte", "mac", "--an"])
    assert r.exit_code == 1 and "nur auf dem Mac" in r.output
    fake = _framework(monkeypatch, *_books(patient, female_patient), status=NOT_DETERMINED)
    r = runner.invoke(app, ["kontakte", "mac"])
    assert r.exit_code == 0 and "aus; Zugriff noch nicht gefragt" in r.output and not fake.asked
    r = runner.invoke(app, ["konfig"])
    assert "Kontakte (Mac):      aus; Zugriff noch nicht gefragt" in r.output, r.output
    r = runner.invoke(app, ["kontakte", "mac", "--an"])
    assert r.exit_code == 0 and fake.asked == 1, r.output
    assert "[x] iCloud (CardDAV)" in r.output and "[x] auf diesem Mac" in r.output
    assert config_module.load_settings().mac_contacts
    r = runner.invoke(app, ["kontakte", "suchen", female_patient.surname])
    assert r.exit_code == 0 and female_patient.surname in r.output, r.output
    r = runner.invoke(app, ["kontakte", "mac", "--adressbuch", "iCloud"])
    assert "[x] iCloud" in r.output and "[ ] auf diesem Mac" in r.output, r.output
    assert config_module.load_settings().mac_address_books == ["icloud-1"]
    r = runner.invoke(app, ["konfig"])
    assert "Kontakte (Mac):      an, 1 Adressbuch/Adressbücher; Zugriff erlaubt" in r.output
    r = runner.invoke(app, ["kontakte", "mac", "--adressbuch", "Arbeit"])
    assert r.exit_code == 1 and "Kein Adressbuch 'Arbeit'" in r.output
    r = runner.invoke(app, ["kontakte", "mac", "--alle"])
    assert r.exit_code == 0 and config_module.load_settings().mac_address_books == []
    r = runner.invoke(app, ["kontakte", "mac", "--aus"])
    assert r.exit_code == 0 and not config_module.load_settings().mac_contacts
    fake.status = DENIED
    r = runner.invoke(app, ["kontakte", "mac", "--an"])
    assert r.exit_code == 1 and "Systemeinstellungen" in r.output
    assert not config_module.load_settings().mac_contacts


def test_the_dialog(qapp: QApplication, data_folder: Path,  # noqa: F811
                    monkeypatch: pytest.MonkeyPatch, patient: Patient,
                    female_patient: Patient) -> None:
    from pseudonymed.gui.address_book_dialog import AddressBookDialog

    assert AddressBookDialog().mac is None  # not a Mac: no such part
    fake = _framework(monkeypatch, *_books(patient, female_patient), status=NOT_DETERMINED)
    dialog = AddressBookDialog()
    assert dialog.mac is not None and not dialog.mac_books.count() and not fake.asked
    assert "macOS fragt" in dialog.mac_info.text()
    dialog.mac.setChecked(True)  # ticking it asks macOS and lists the books
    assert fake.asked == 1 and dialog.mac_books.count() == 2
    items = [dialog.mac_books.item(i) for i in range(2)]
    assert all(t.checkState() == Qt.CheckState.Checked for t in items)
    assert dialog.save()
    e = config_module.load_settings()
    assert e.mac_contacts and e.mac_address_books == []  # every book, also ones added later
    dialog = AddressBookDialog()  # allowed now: the books are there at once
    assert dialog.mac.isChecked() and dialog.mac_books.count() == 2 and fake.asked == 1
    dialog.mac_books.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.save() and config_module.load_settings().mac_address_books == ["icloud-1"]
    dialog = AddressBookDialog()
    assert dialog.mac_books.item(1).checkState() == Qt.CheckState.Unchecked
    dialog.mac_books.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.save() and "mindestens ein Adressbuch" in dialog.status.text()
    fake.status = DENIED
    dialog = AddressBookDialog()
    assert "verweigert" in dialog.mac_info.text() and not dialog.mac_books.count()
    assert not dialog.save() and "kein Zugriff" in dialog.status.text()
    dialog.mac.setChecked(False)
    assert dialog.save() and not config_module.load_settings().mac_contacts
    assert config_module.load_settings().mac_address_books == ["icloud-1"]  # kept for later
