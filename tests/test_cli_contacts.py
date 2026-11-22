"""CLI with CardDAV: search, show, export, pseudonymise straight from the address book."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from tests.carddav_fake import FakeCardDAV
from tests.synthetic import vcard_from_patient

runner = CliRunner()


def test_address_books_and_search(carddav_environment: FakeCardDAV, patient: Patient,
                                  female_patient: Patient, tmp_path: Path) -> None:
    r = runner.invoke(app, ["kontakte", "adressbuecher"])
    assert r.exit_code == 0 and "medifed-patients" in r.output and "Kontakte" in r.output
    store = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["kontakte", "suchen", patient.surname[:3], "--speicher",
                            str(store)])
    assert r.exit_code == 0, r.output
    assert f"{patient.surname}, {patient.first_name}" in r.output
    assert patient.birth_date.strftime("%d.%m.%Y") in r.output
    r = runner.invoke(app, ["kontakte", "suchen", "Zyxwv"])
    assert r.exit_code == 1


def test_show_and_export(carddav_environment: FakeCardDAV, patient: Patient,
                         tmp_path: Path) -> None:
    r = runner.invoke(app, ["kontakte", "zeigen", patient.surname])
    assert r.exit_code == 0, r.output
    assert patient.insurance_number in r.output and "Herr" in r.output
    r = runner.invoke(app, ["kontakte", "zeigen", patient.surname, "--json"])
    data = json.loads(r.output)
    assert data["nachname"] == patient.surname
    assert data["geburtsdatum"] == patient.birth_date.isoformat()
    target = tmp_path / "patient.json"
    r = runner.invoke(app, ["kontakte", "export", patient.surname, "-o", str(target)])
    assert r.exit_code == 0 and target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["angehoerige"] == []
    r = runner.invoke(app, ["kontakte", "export", patient.surname, "-o", str(target)])
    assert r.exit_code == 1 and "existiert" in r.output


def test_pseudonymise_from_address_book(carddav_environment: FakeCardDAV, report_pdf: Path,
                                      patient: Patient, tmp_path: Path) -> None:
    store = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--kontakt", patient.surname,
                            "--speicher", str(store)])
    assert r.exit_code == 0, r.output
    assert "Gegenprobe: bestanden" in r.output
    txt = (report_pdf.parent / "befund_pseudonym.txt").read_text(encoding="utf-8")
    assert patient.surname not in txt and re.search(r"Patient [A-Z2-9]{6}", txt)
    # the identifier is now visible in the search
    r = runner.invoke(app, ["kontakte", "suchen", patient.surname, "--speicher", str(store)])
    assert re.search(r"→ Patient [A-Z2-9]{6}", r.output)
    r = runner.invoke(app, ["pruefen", str(report_pdf.parent / "befund_pseudonym.pdf"), "--kontakt",
                            patient.surname, "--speicher", str(store)])
    assert r.exit_code == 0, r.output


def test_ambiguous_and_nr(carddav_environment: FakeCardDAV, report_pdf: Path, patient: Patient,
                          female_patient: Patient, tmp_path: Path) -> None:
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--kontakt", "Zyxwv"])
    assert r.exit_code == 1 and "Kein Kontakt" in r.output
    vcf = tmp_path / "beide.vcf"
    vcf.write_text(vcard_from_patient(patient) + vcard_from_patient(female_patient),
                   encoding="utf-8")
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--vcf", str(vcf)])
    assert r.exit_code == 1 and "Mehrere Treffer" in r.output and female_patient.surname in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--vcf", str(vcf), "--nr", "1",
                            "--speicher", str(tmp_path / "s.sqlite")])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--vcf", str(vcf), "--kontakt",
                            female_patient.surname, "--speicher", str(tmp_path / "s.sqlite")])
    assert r.exit_code == 0, r.output
    assert re.search(r"Patientin [A-Z2-9]{6}", r.output)
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf)])
    assert r.exit_code == 1 and "--patient, --vcf oder --kontakt" in r.output


def test_setup_and_config(fake_carddav: FakeCardDAV, tmp_path: Path,
                          monkeypatch) -> None:
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_URL", raising=False)
    config = tmp_path / "konfig.toml"
    r = runner.invoke(app, ["kontakte", "einrichten", "--url", fake_carddav.url, "--benutzer",
                            fake_carddav.user, "--adressbuch", "medifed-patients", "--konfig",
                            str(config), "--kein-schluesselbund"],
                      input=fake_carddav.password + "\n")
    assert r.exit_code == 0, r.output
    assert config.exists() and "medifed-patients" in config.read_text(encoding="utf-8")
    assert fake_carddav.password not in config.read_text(encoding="utf-8")
    r = runner.invoke(app, ["kontakte", "einrichten", "--url", fake_carddav.url, "--benutzer",
                            fake_carddav.user, "--konfig", str(config),
                            "--kein-schluesselbund"], input="falsch\n")
    assert r.exit_code == 1 and "Anmeldung abgelehnt" in r.output
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_PASSWORT", fake_carddav.password)
    r = runner.invoke(app, ["kontakte", "adressbuecher", "--konfig", str(config)])
    assert r.exit_code == 0 and "Patienten" in r.output
    r = runner.invoke(app, ["kontakte", "adressbuecher", "--konfig", str(tmp_path / "fehlt.toml")])
    assert r.exit_code == 1 and "nicht eingerichtet" in r.output
