"""The command line drives the same pipeline: pseudonymise, check, re-identify."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.core.model import Patient

runner = CliRunner()


def _code(output: str) -> str:
    m = re.search(r"Pseudonym der Hauptperson: Patient(?:in)? ([A-Z2-9]+)", output)
    assert m, output
    return m.group(1)


def _patient_file(tmp_path: Path, p: Patient) -> Path:
    data = {
        "nachname": p.surname, "vorname": p.first_name, "geschlecht": p.sex,
        "geburtsdatum": p.birth_date.isoformat(), "strasse": p.street, "plz": p.postcode,
        "ort": p.place, "telefone": p.phones, "emails": p.emails,
        "versichertennummer": p.insurance_number, "patienten_id": p.patient_id,
        "aktenzeichen": p.case_numbers,
        "angehoerige": [{"nachname": a.surname, "vorname": a.first_name} for a in p.relatives],
        "zuweiser": [{"nachname": z.surname, "vorname": z.first_name, "titel": z.title,
                      "strasse": z.street} for z in p.referrers],
    }
    path = tmp_path / "patient.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_round_trip_via_cli(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    pfile = _patient_file(tmp_path, patient)
    store = tmp_path / "store.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                            "--speicher", str(store), "--markdown"])
    assert r.exit_code == 0, r.output
    assert "Gegenprobe: bestanden" in r.output and "Markdown:" in r.output
    k = _code(r.output)
    assert (tmp_path / "befund_pseudonym.pdf").exists()
    assert "## Seite 1" in (tmp_path / "befund_pseudonym.md").read_text(encoding="utf-8")
    txt = (tmp_path / "befund_pseudonym.txt").read_text(encoding="utf-8")
    assert patient.surname not in txt and f"Patient {k}" in txt

    r = runner.invoke(app, ["pruefen", str(tmp_path / "befund_pseudonym.pdf"), "--patient",
                            str(pfile), "--speicher", str(store)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["pruefen", str(report_pdf), "--patient", str(pfile),
                            "--speicher", str(store)])
    assert r.exit_code == 2 and "NICHT bestanden" in r.output

    r = runner.invoke(app, ["pseudonyme", "--speicher", str(store)])
    assert r.exit_code == 0 and f"{k}  {patient.surname}, {patient.first_name}" in r.output

    letter = tmp_path / "brief.txt"
    year = patient.birth_date.year
    letter.write_text(f"Patient {k}, [geb. {year}], wurde von Dr. Z1 vorgestellt.",
                      encoding="utf-8")
    r = runner.invoke(app, ["rueckersetzen", str(letter), "--kennung", k, "--form", "anrede",
                            "--speicher", str(store)])
    assert r.exit_code == 0, r.output
    assert f"Herr {patient.surname}" in r.output
    assert patient.birth_date.strftime("%d.%m.%Y") in r.output
    assert patient.referrers[0].surname in r.output

    r = runner.invoke(app, ["rueckersetzen", "--suche", patient.surname[:3], "--speicher",
                            str(store)], input=f"Patient {k} und Patient Q.")
    assert r.exit_code == 3
    assert patient.full_name in r.output and "Patient Q" in r.output
    wrong = k[:-1] + ("A" if k[-1] != "A" else "B")  # always a different, invalid code
    r = runner.invoke(app, ["rueckersetzen", "--kennung", k, "--speicher", str(store)],
                      input=f"Patient {wrong} kam.")
    assert r.exit_code == 3 and "Prüfzeichen falsch" in r.output and k in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                            "--speicher", str(store), "--schema", "buchstabe"])
    assert r.exit_code == 0 and _code(r.output) == k  # existing entry wins over the scheme


def test_values_answer_to_their_german_and_their_english_word(
        tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    """An option that takes one of a few words takes the English word like the German one."""
    from pseudonymed.cli import base

    assert base.german("letter", ("hash", "buchstabe")) == "buchstabe"
    assert base.german("letter", ("a4", "letter", "bild")) == "letter"  # a paper size itself
    assert base.german("image", ("bild",)) == "bild" and base.german("x", ("bild",)) == "x"
    assert base.choice("--datum", "age", ("jahr", "alter")) == "alter"
    assert base.choice("--datum", "alter", ("jahr", "alter")) == "alter"

    pfile = _patient_file(tmp_path, patient)
    store = tmp_path / "store.sqlite"
    texts = []
    for folder, words in (("de", ["--datum", "alter", "--vorschau", "nie", "--unlesbar",
                                  "lassen", "--aufbereiten", "aus", "--ocr", "aus", "--papier",
                                  "bild", "--schema", "buchstabe"]),
                          ("en", ["--date", "age", "--preview", "never", "--unreadable",
                                  "keep", "--clean-up", "off", "--ocr", "off", "--paper",
                                  "image", "--schema", "letter"])):
        r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                                "--speicher", str(store), "--ziel", str(tmp_path / folder),
                                *words])
        assert r.exit_code == 0, r.output
        texts.append((tmp_path / folder / "befund_pseudonym.txt").read_text(encoding="utf-8"))
    assert texts[0] == texts[1] and "[Alter " in texts[0] and "Patient A" in texts[0]

    letter = tmp_path / "brief.txt"
    letter.write_text("Patient A kam.", encoding="utf-8")
    forms = [runner.invoke(app, ["rueckersetzen", str(letter), "--kennung", "A", "--form", form,
                                 "--speicher", str(store)]) for form in ("anrede", "salutation")]
    assert forms[0].exit_code == forms[1].exit_code == 0, forms[1].output
    assert forms[0].output == forms[1].output and patient.surname in forms[1].output
    r = runner.invoke(app, ["rueckersetzen", str(letter), "--kennung", "A", "--form", "x",
                            "--speicher", str(store)])
    assert r.exit_code == 1 and "--form muss 'vollname' oder 'anrede' sein." in r.output
    r = runner.invoke(app, ["datenbank", "import", str(letter), "--modus", "x", "--speicher",
                            str(store)])
    assert r.exit_code == 1 and "'ersetzen' oder 'zusammenfuehren'" in r.output


def test_template_and_config(tmp_path: Path) -> None:
    r = runner.invoke(app, ["vorlage"])
    assert r.exit_code == 0 and json.loads(r.output)["nachname"] == ""
    r = runner.invoke(app, ["konfig", "--speicher", str(tmp_path / "x.sqlite")])
    assert r.exit_code == 0 and "PseudonyMed" in r.output and "x.sqlite" in r.output
    r = runner.invoke(app, ["kontakte"])
    assert r.exit_code == 2 and "einrichten" in r.output


def test_invalid_inputs(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    pfile = _patient_file(tmp_path, patient)
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                            "--datum", "x"])
    assert r.exit_code == 1
    empty = tmp_path / "leer.json"
    empty.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(empty)])
    assert r.exit_code == 1
    r = runner.invoke(app, ["rueckersetzen", "--speicher", str(tmp_path / "s.sqlite")], input="x")
    assert r.exit_code == 1


def test_data_folder_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    r = runner.invoke(app, ["konfig"])
    assert r.exit_code == 0 and str(tmp_path / "daten") in r.output


def test_module_start_shows_the_help(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m pseudonymed.cli`` runs the command line like the installed script."""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["pseudonymed", "--help"])
    with pytest.raises(SystemExit) as end:
        runpy.run_module("pseudonymed.cli", run_name="__main__")
    assert end.value.code == 0
    assert "pseudonymisieren" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
