"""The renaming from AnonyMed to PseudonyMed: old variables, folders, files and commands work."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pseudonymed import environment
from pseudonymed.cli import app
from pseudonymed.pseudonyms import backup as backup_mod
from pseudonymed.pseudonyms import capsule as capsule_module
from pseudonymed.pseudonyms.store import PseudonymStore


def test_variables_with_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSEUDONYMED_OCR", raising=False)
    monkeypatch.delenv("ANONYMED_OCR", raising=False)
    assert environment.variable("OCR", "auto") == "auto"
    monkeypatch.setenv("ANONYMED_OCR", "tesseract")
    assert environment.variable("OCR") == "tesseract"
    monkeypatch.setenv("PSEUDONYMED_OCR", "rapidocr")
    assert environment.variable("OCR") == "rapidocr"
    assert environment.names("OCR") == ["PSEUDONYMED_OCR", "ANONYMED_OCR"]


@pytest.fixture
def without_data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PSEUDONYMED_DATENORDNER", raising=False)
    monkeypatch.delenv("ANONYMED_DATENORDNER", raising=False)
    platform_dirs = tmp_path / "Application Support"
    monkeypatch.setattr(environment, "user_data_path", lambda name, appauthor: platform_dirs / name)
    monkeypatch.setattr(environment, "user_config_path", lambda name,
                        appauthor: platform_dirs / name)
    return platform_dirs


def test_old_data_folder_moves(without_data_folder: Path) -> None:
    old = without_data_folder / "AnonyMed"
    old.mkdir(parents=True)
    (old / "pseudonyme.sqlite").write_bytes(b"db")
    (old / "pseudonymschluessel.txt").write_text("schluessel", encoding="utf-8")
    new = environment.data_folder()
    assert new == without_data_folder / "PseudonyMed" and not old.exists()
    assert (new / "pseudonyme.sqlite").read_bytes() == b"db"
    assert environment.config_folder() == new  # macOS: one folder for both, moved once
    old.mkdir()  # both exist: the new one wins, the old one is left alone
    (old / "pseudonyme.sqlite").write_bytes(b"alt")
    assert environment.data_folder() == new and (old / "pseudonyme.sqlite").exists()


def test_without_old_folder_the_new_one(without_data_folder: Path) -> None:
    assert environment.data_folder() == without_data_folder / "PseudonyMed"
    assert not (without_data_folder / "PseudonyMed").exists()  # created by whoever writes


def test_data_folder_from_old_variable(tmp_path: Path, without_data_folder: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANONYMED_DATENORDNER", str(tmp_path / "portabel"))
    assert environment.data_folder() == tmp_path / "portabel"


def test_old_capsule_is_read() -> None:
    k = capsule_module.Capsule(code="K7Q4MX", patient={"nachname": "Test"},
                               mappings={"Patient K7Q4MX": "Max Test"})
    data = json.loads(k.as_json())
    data["anonymed_kapsel"] = data.pop("pseudonymed_kapsel")
    assert capsule_module.Capsule.from_json(json.dumps(data).encode()) == k


def test_old_backup_is_read(tmp_path: Path) -> None:
    db = tmp_path / "daten" / "pseudonyme.sqlite"
    db.parent.mkdir()
    PseudonymStore(db).close()
    archive = backup_mod.export(tmp_path / "neu.zip", db, config=False, vocabulary=False)
    old = tmp_path / "alt.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(old, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == backup_mod.NAME_MANIFEST:
                m = json.loads(content)
                m["anonymed_sicherung"] = m.pop("pseudonymed_sicherung")
                content = json.dumps(m).encode()
            target.writestr(name, content)
    assert backup_mod.check(old).files == backup_mod.check(archive).files


def test_keyring_with_old_service(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")
    from pseudonymed import config

    monkeypatch.delenv("PSEUDONYMED_CARDDAV_PASSWORT", raising=False)
    monkeypatch.delenv("ANONYMED_CARDDAV_PASSWORT", raising=False)
    entries = {("AnonyMed CardDAV", "dr@cloud.example"): "app-passwort"}
    monkeypatch.setattr(keyring, "get_password", lambda service, name: entries.get((service, name)))
    k = config.CardDAVConfig(url="https://cloud.example/remote.php/dav", user="dr")
    assert config.load_password(k) == "app-passwort"


def _without_colours(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)  # the CI forces coloured help


def test_old_command_remains(tmp_path: Path) -> None:
    r = CliRunner().invoke(app, ["anonymisieren", "--help"])
    assert r.exit_code == 0 and "--patient" in _without_colours(r.output), r.output
    help_text = _without_colours(CliRunner().invoke(app, ["--help"]).output)
    assert "pseudonymisieren" in help_text and "anonymisieren " not in help_text


def test_tests_run_in_their_own_data_folder() -> None:
    """The isolation is in place from collection on (see conftest.pytest_configure)."""
    from platformdirs import user_data_path

    folder = environment.data_folder()
    assert folder.name.startswith("pseudonymed-daten-")
    assert folder != user_data_path(environment.NAME, appauthor=False)
    assert environment.config_folder() == folder
