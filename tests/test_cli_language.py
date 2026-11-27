"""The command line speaks the interface language: German by default, English on request.

The language is fixed when the command line starts, so these tests run it as a process of
its own.  Commands and options answer to their German and their English names in both
languages; the help lists the commands of its language and shows that name first.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pseudonymed import language
from pseudonymed.core.model import Patient


def _run(arguments: list[str], tmp_path: Path, spoken: str | None,
         columns: int = 100) -> subprocess.CompletedProcess:
    # GITHUB_ACTIONS, FORCE_COLOR and PY_COLORS make Typer force a terminal of 80 columns,
    # whatever COLUMNS says; the help is to be read as a person's terminal shows it
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDONYMED_")
           and k not in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS")}
    env.update(PSEUDONYMED_DATENORDNER=str(tmp_path / "daten"), PSEUDONYMED_LEXIKON="0",
               COLUMNS=str(columns), NO_COLOR="1", TERM="dumb", LANG="C.UTF-8",
               PYTHONIOENCODING="utf-8")  # Windows writes a pipe in cp1252 otherwise
    if spoken:
        env["PSEUDONYMED_SPRACHE"] = spoken
    run = subprocess.run([sys.executable, "-m", "pseudonymed.cli", *arguments],
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    run.stdout = re.sub(r"\x1b\[[0-9;]*m", "", run.stdout)  # the CI forces coloured help
    return run


def _flat(help_text: str) -> str:
    """The help as one run of words: the frames of its panels out, wrapped lines joined.
    Where a line breaks depends on the terminal (Windows gives the help one column less),
    so words a test looks for may stand on two lines."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", help_text).split())


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
    assert "│ pseudonymise " in english.stdout and "│ pseudonymisieren " not in english.stdout
    assert "│ pseudonymisieren " in german.stdout and "│ pseudonymise " not in german.stdout
    assert "│ database " in english.stdout and "│ datenbank " in german.stdout
    assert _one_under_other("--language", "--sprache", english.stdout), english.stdout
    sub = _run(["pseudonymise", "--help"], tmp_path, "en")
    assert "Patient file" in sub.stdout and "Patientendatei" not in sub.stdout
    assert _one_under_other("--target", "--ziel", sub.stdout), sub.stdout
    assert _one_under_other("--no-text", "--kein-text", sub.stdout)
    sub = _run(["pseudonymisieren", "--help"], tmp_path, "de")
    assert _one_under_other("--ziel", "--target", sub.stdout), sub.stdout
    assert _one_under_other("--kein-text", "--no-text", sub.stdout)


def _one_under_other(first: str, second: str, help_text: str) -> bool:
    """Whether the help shows an option's two names in one cell, ``first,`` above ``second``."""
    pattern = rf"(?<![\w-]){first},[^\n]*\n[^\n]*(?<![\w-]){second}(?![\w-])"
    return re.search(pattern, help_text) is not None


def test_a_narrow_help_cuts_nothing_off(tmp_path: Path) -> None:
    """With two names to an option, 80 columns still show every word: Rich folds instead of
    ending a word with an ellipsis (a guard over the patch to Typer in ``cli/base.py``)."""
    for spoken, arguments in (("de", ["pseudonymisieren", "--help"]),
                              ("en", ["database", "export", "--help"])):
        run = _run(arguments, tmp_path, spoken, columns=80)
        assert run.returncode == 0 and "…" not in run.stdout, run.stdout


def test_both_names_work_in_either_language(tmp_path: Path) -> None:
    """German names in the English command line and English ones in the German line."""
    from typer.testing import CliRunner

    from pseudonymed.cli import app

    runner = CliRunner()
    for arguments in (["database", "status"], ["datenbank", "status"],
                      ["medical-terms", "check", "Hirninfarkt"], ["config", "--store",
                                                                  str(tmp_path / "s.sqlite")]):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, (arguments, result.output)
    english = _run(["datenbank", "status"], tmp_path, "en")
    assert english.returncode == 0 and "Key present" in english.stdout, english.stdout


@pytest.mark.parametrize("columns", [100, 99])
def test_the_english_line_names_the_english_values(tmp_path: Path, columns: int) -> None:
    """Help, defaults and messages name the words of the language; both words are taken.
    At 99 columns the help wraps as it does on Windows at 100."""
    english = _flat(_run(["pseudonymise", "--help"], tmp_path, "en", columns).stdout)
    for words in ("'year'", "'age'", "never.", "keep.", "tesseract or off.", "auto or off.",
                  "[default: year]", "[default: redact]"):
        assert words in english, (words, english)
    for words in ("[default: jahr]", "immer", "(off)", "'alter'"):
        assert words not in english, words
    german = _flat(_run(["pseudonymisieren", "--help"], tmp_path, "de", columns).stdout)
    assert "'jahr'" in german and "[default: jahr]" in german and "'year'" not in german
    document = tmp_path / "x.txt"
    document.write_text("x", encoding="utf-8")
    for spoken, option, message in (
            ("en", "--date", "--date must be 'year' or 'age'."),
            ("de", "--date", "--datum muss 'jahr' oder 'alter' sein.")):
        run = _run(["pseudonymise", str(document), "--patient", str(document), option, "x"],
                   tmp_path, spoken)
        assert run.returncode == 1 and message in run.stderr, run.stderr
    run = _run(["database", "import", str(document), "--mode", "x"], tmp_path, "en")
    assert run.returncode == 1 and "--mode must be 'replace' or 'merge'." in run.stderr
    run = _run(["database", "extra-data", "x"], tmp_path, "en")
    assert run.returncode == 2 and "hospitals-at" in run.stderr


def test_a_run_reports_in_english(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    patient_file = tmp_path / "patient.json"
    patient_file.write_text(json.dumps(patient.as_dict(), ensure_ascii=False), encoding="utf-8")
    run = _run(["pseudonymise", str(report_pdf), "--patient", str(patient_file),
                "--target", str(tmp_path / "out")], tmp_path, "en")
    assert run.returncode == 0, run.stderr
    assert "audit report" in run.stdout and "Re-check: passed" in run.stdout, run.stdout
    assert "Prüfbericht" not in run.stdout and "Gegenprobe" not in run.stdout
    report = next((tmp_path / "out").glob("*.bericht.json"))
    stored = json.loads(report.read_text(encoding="utf-8"))
    assert stored["gegenprobe"]["geprueft"][0] == "Name" and "gegenprobe" in stored
    again = _run(["pseudonymisieren", str(report_pdf), "--patient", str(patient_file),
                  "--ziel", str(tmp_path / "de")], tmp_path, "de")
    assert "Prüfbericht" in again.stdout and "Gegenprobe: bestanden" in again.stdout
