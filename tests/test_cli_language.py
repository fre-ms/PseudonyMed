"""The command line speaks the interface language: German by default, English on request.

The language is fixed when the command line starts, so these tests run it as a process of
its own; command and option names are the same in both languages.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pseudonymed import language
from pseudonymed.core.model import Patient


def _run(arguments: list[str], tmp_path: Path, spoken: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDONYMED_")}
    env.update(PSEUDONYMED_DATENORDNER=str(tmp_path / "daten"), PSEUDONYMED_LEXIKON="0",
               COLUMNS="100", NO_COLOR="1", TERM="dumb", LANG="C.UTF-8",
               PYTHONIOENCODING="utf-8")  # Windows writes a pipe in cp1252 otherwise
    if spoken:
        env["PSEUDONYMED_SPRACHE"] = spoken
    return subprocess.run([sys.executable, "-m", "pseudonymed.cli", *arguments],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)


def test_output_that_is_not_utf8_replaces_instead_of_failing(tmp_path: Path) -> None:
    """A run whose output is cp1252 (a redirected Windows console) still ends normally."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDONYMED_")}
    env.update(PSEUDONYMED_DATENORDNER=str(tmp_path / "daten"), PSEUDONYMED_SPRACHE="de",
               PYTHONIOENCODING="cp1252", COLUMNS="100", NO_COLOR="1", TERM="dumb")
    code = ("import sys; from pseudonymed.cli.base import _tolerant_output; _tolerant_output();"
            " print('λ ≈ 3 → ROT')")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
    assert run.returncode == 0 and run.stdout.strip() == "? ? 3 ? ROT".encode("cp1252"), run


def test_the_language_is_chosen_like_the_desktops(monkeypatch: pytest.MonkeyPatch) -> None:
    assert language.from_arguments(["konfig", "--sprache", "EN"]) == "en"
    assert language.from_arguments(["--language=en_GB"]) == "en_gb"
    assert language.from_arguments(["konfig"]) is None
    assert language.without_arguments(["--sprache", "en", "konfig"]) == ["konfig"]
    monkeypatch.setenv("PSEUDONYMED_SPRACHE", "en")
    assert language.determine([]) == "en"
    assert language.determine(["--sprache", "de"]) == "de"
    monkeypatch.delenv("PSEUDONYMED_SPRACHE")
    monkeypatch.setattr("pseudonymed.language.load_settings",
                        lambda: type("S", (), {"gui_language": ""})())
    for name in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert language.determine([]) == "en_gb"
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setattr("pseudonymed.language.locale.getlocale", lambda: (None, None))
    assert language.system_language() == "de"  # no language named: the source language


def test_help_in_both_languages(tmp_path: Path) -> None:
    german = _run(["--help"], tmp_path, "de")
    english = _run(["--sprache", "en", "--help"], tmp_path, None)
    assert german.returncode == english.returncode == 0
    assert "Patientendokumente lokal pseudonymisieren" in german.stdout
    assert "pseudonymise patient documents locally" in english.stdout
    assert "pseudonymisieren" in english.stdout  # the command names stay
    sub = _run(["pseudonymisieren", "--help"], tmp_path, "en")
    assert "Patient file" in sub.stdout and "--ziel" in sub.stdout, sub.stdout
    assert "Patientendatei" not in sub.stdout


def test_a_run_reports_in_english(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    patient_file = tmp_path / "patient.json"
    patient_file.write_text(json.dumps(patient.as_dict(), ensure_ascii=False), encoding="utf-8")
    run = _run(["pseudonymisieren", str(report_pdf), "--patient", str(patient_file),
                "--ziel", str(tmp_path / "out")], tmp_path, "en")
    assert run.returncode == 0, run.stderr
    assert "audit report" in run.stdout and "Re-check: passed" in run.stdout, run.stdout
    assert "Prüfbericht" not in run.stdout and "Gegenprobe" not in run.stdout
    report = next((tmp_path / "out").glob("*.bericht.json"))
    stored = json.loads(report.read_text(encoding="utf-8"))
    assert stored["gegenprobe"]["geprueft"][0] == "Name" and "gegenprobe" in stored
    again = _run(["pseudonymisieren", str(report_pdf), "--patient", str(patient_file),
                  "--ziel", str(tmp_path / "de")], tmp_path, "de")
    assert "Prüfbericht" in again.stdout and "Gegenprobe: bestanden" in again.stdout
