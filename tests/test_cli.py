"""The command line drives the same pipeline: pseudonymise, check, re-identify."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.kern.modell import Patient

runner = CliRunner()


def _kennung(output: str) -> str:
    m = re.search(r"Pseudonym der Hauptperson: Patient(?:in)? ([A-Z2-9]+)", output)
    assert m, output
    return m.group(1)


def _patientendatei(tmp_path: Path, p: Patient) -> Path:
    daten = {
        "nachname": p.nachname, "vorname": p.vorname, "geschlecht": p.geschlecht,
        "geburtsdatum": p.geburtsdatum.isoformat(), "strasse": p.strasse, "plz": p.plz,
        "ort": p.ort, "telefone": p.telefone, "emails": p.emails,
        "versichertennummer": p.versichertennummer, "patienten_id": p.patienten_id,
        "aktenzeichen": p.aktenzeichen,
        "angehoerige": [{"nachname": a.nachname, "vorname": a.vorname} for a in p.angehoerige],
        "zuweiser": [{"nachname": z.nachname, "vorname": z.vorname, "titel": z.titel,
                      "strasse": z.strasse} for z in p.zuweiser],
    }
    pfad = tmp_path / "patient.json"
    pfad.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    return pfad


def test_rundlauf_ueber_cli(tmp_path: Path, befund: Path, patient: Patient) -> None:
    pdatei = _patientendatei(tmp_path, patient)
    speicher = tmp_path / "store.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(pdatei),
                            "--speicher", str(speicher), "--markdown"])
    assert r.exit_code == 0, r.output
    assert "Gegenprobe: bestanden" in r.output and "Markdown:" in r.output
    k = _kennung(r.output)
    assert (tmp_path / "befund_pseudonym.pdf").exists()
    assert "## Seite 1" in (tmp_path / "befund_pseudonym.md").read_text(encoding="utf-8")
    txt = (tmp_path / "befund_pseudonym.txt").read_text(encoding="utf-8")
    assert patient.nachname not in txt and f"Patient {k}" in txt

    r = runner.invoke(app, ["pruefen", str(tmp_path / "befund_pseudonym.pdf"), "--patient",
                            str(pdatei), "--speicher", str(speicher)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["pruefen", str(befund), "--patient", str(pdatei),
                            "--speicher", str(speicher)])
    assert r.exit_code == 2 and "NICHT bestanden" in r.output

    r = runner.invoke(app, ["pseudonyme", "--speicher", str(speicher)])
    assert r.exit_code == 0 and f"{k}  {patient.nachname}, {patient.vorname}" in r.output

    brief = tmp_path / "brief.txt"
    jahr = patient.geburtsdatum.year
    brief.write_text(f"Patient {k}, [geb. {jahr}], wurde von Dr. Z1 vorgestellt.", encoding="utf-8")
    r = runner.invoke(app, ["rueckersetzen", str(brief), "--kennung", k, "--form", "anrede",
                            "--speicher", str(speicher)])
    assert r.exit_code == 0, r.output
    assert f"Herr {patient.nachname}" in r.output
    assert patient.geburtsdatum.strftime("%d.%m.%Y") in r.output
    assert patient.zuweiser[0].nachname in r.output

    r = runner.invoke(app, ["rueckersetzen", "--suche", patient.nachname[:3], "--speicher",
                            str(speicher)], input=f"Patient {k} und Patient Q.")
    assert r.exit_code == 3
    assert patient.vollname in r.output and "Patient Q" in r.output
    falsch = k[:-1] + ("A" if k[-1] != "A" else "B")  # always a different, invalid code
    r = runner.invoke(app, ["rueckersetzen", "--kennung", k, "--speicher", str(speicher)],
                      input=f"Patient {falsch} kam.")
    assert r.exit_code == 3 and "Prüfzeichen falsch" in r.output and k in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(pdatei),
                            "--speicher", str(speicher), "--schema", "buchstabe"])
    assert r.exit_code == 0 and _kennung(r.output) == k  # existing entry wins over the scheme


def test_vorlage_und_konfig(tmp_path: Path) -> None:
    r = runner.invoke(app, ["vorlage"])
    assert r.exit_code == 0 and json.loads(r.output)["nachname"] == ""
    r = runner.invoke(app, ["konfig", "--speicher", str(tmp_path / "x.sqlite")])
    assert r.exit_code == 0 and "PseudonyMed" in r.output and "x.sqlite" in r.output
    r = runner.invoke(app, ["kontakte"])
    assert r.exit_code == 2 and "einrichten" in r.output


def test_fehlerhafte_eingaben(tmp_path: Path, befund: Path, patient: Patient) -> None:
    pdatei = _patientendatei(tmp_path, patient)
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(pdatei),
                            "--datum", "x"])
    assert r.exit_code == 1
    leer = tmp_path / "leer.json"
    leer.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(leer)])
    assert r.exit_code == 1
    r = runner.invoke(app, ["rueckersetzen", "--speicher", str(tmp_path / "s.sqlite")], input="x")
    assert r.exit_code == 1


def test_datenordner_uebersteuerung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    r = runner.invoke(app, ["konfig"])
    assert r.exit_code == 0 and str(tmp_path / "daten") in r.output


def test_modulstart_zeigt_die_hilfe(monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m pseudonymed.cli`` runs the command line like the installed script."""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["pseudonymed", "--help"])
    with pytest.raises(SystemExit) as ende:
        runpy.run_module("pseudonymed.cli", run_name="__main__")
    assert ende.value.code == 0
    assert "pseudonymisieren" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
