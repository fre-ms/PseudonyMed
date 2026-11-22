"""Environment variables and per-user folders, including the fallbacks to the former name
(tests/test_umbenennung.py covers the happy path of the folder move)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pseudonymed import environment


@pytest.fixture
def platform_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The platform folders point into tmp_path and no data-folder variable is set."""
    for n in environment.names("DATENORDNER"):
        monkeypatch.delenv(n, raising=False)
    root = tmp_path / "plattform"
    monkeypatch.setattr(environment, "user_data_path",
                        lambda name, appauthor: root / "daten" / name)
    monkeypatch.setattr(environment, "user_config_path",
                        lambda name, appauthor: root / "konfig" / name)
    return root


def test_empty_variable_counts_as_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty current-name variable wins over a filled former-name one: set is set."""
    monkeypatch.setenv("PSEUDONYMED_PROBE", "")
    monkeypatch.setenv("ANONYMED_PROBE", "alt")
    assert environment.variable("PROBE", "standard") == ""
    monkeypatch.delenv("PSEUDONYMED_PROBE")
    assert environment.variable("PROBE", "standard") == "alt"
    monkeypatch.delenv("ANONYMED_PROBE")
    assert environment.variable("PROBE") is None


def test_both_folders_from_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSEUDONYMED_DATENORDNER", raising=False)
    monkeypatch.setenv("ANONYMED_DATENORDNER", str(tmp_path / "portabel"))
    assert environment.config_folder() == tmp_path / "portabel" == environment.data_folder()
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "neu"))
    assert environment.data_folder() == tmp_path / "neu" == environment.config_folder()


def test_move_creates_the_parent_folder(platform_dirs: Path) -> None:
    """Configuration and data live in different trees (Linux): the new tree may be missing."""
    old = platform_dirs / "konfig" / "AnonyMed"
    old.mkdir(parents=True)
    (old / "konfig.toml").write_text("[gui]\n", encoding="utf-8")
    new = environment.config_folder()
    assert new == platform_dirs / "konfig" / "PseudonyMed"
    assert (new / "konfig.toml").exists() and not old.exists()
    assert environment.data_folder() == platform_dirs / "daten" / "PseudonyMed"  # nothing to move


def test_move_fails_old_folder_remains(platform_dirs: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    old = platform_dirs / "daten" / "AnonyMed"
    old.mkdir(parents=True)

    def refused(self: Path, target: Path) -> None:
        raise OSError("anderer Datenträger")

    monkeypatch.setattr(Path, "rename", refused)
    assert environment.data_folder() == old and old.is_dir()
    assert not (platform_dirs / "daten" / "PseudonyMed").exists()
