"""Environment variables and per-user folders: English names, German ones understood too."""

from __future__ import annotations

from pathlib import Path

import lexcor.store
import pytest

from pseudonymed import environment
from pseudonymed.detection import vocabulary


@pytest.fixture
def platform_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The platform folders point into tmp_path and no data-folder variable is set."""
    for n in environment.names("DATA_FOLDER"):
        monkeypatch.delenv(n, raising=False)
    root = tmp_path / "platform"
    monkeypatch.setattr(environment, "user_data_path",
                        lambda name, appauthor: root / "data" / name)
    monkeypatch.setattr(environment, "user_config_path",
                        lambda name, appauthor: root / "config" / name)
    return root


def test_english_names_and_their_german_equivalents(monkeypatch: pytest.MonkeyPatch) -> None:
    """The English name wins, the German one is understood; an empty variable is set."""
    assert environment.names("DATA_FOLDER") == ["PSEUDONYMED_DATA_FOLDER",
                                                "PSEUDONYMED_DATENORDNER"]
    assert environment.names("OCR") == ["PSEUDONYMED_OCR"]
    for n in environment.names("LANGUAGE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("PSEUDONYMED_SPRACHE", "de")
    assert environment.variable("LANGUAGE") == "de"
    monkeypatch.setenv("PSEUDONYMED_LANGUAGE", "")
    assert environment.variable("LANGUAGE", "standard") == ""
    monkeypatch.delenv("PSEUDONYMED_LANGUAGE")
    monkeypatch.delenv("PSEUDONYMED_SPRACHE")
    assert environment.variable("LANGUAGE") is None
    monkeypatch.setenv("ANONYMED_PROBE", "old")  # the project's former name is no more
    assert environment.variable("PROBE") is None


def test_both_folders_from_the_variable(tmp_path: Path, platform_dirs: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    assert environment.data_folder() == platform_dirs / "data" / "PseudonyMed"
    assert environment.config_folder() == platform_dirs / "config" / "PseudonyMed"
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "portable"))
    assert environment.config_folder() == tmp_path / "portable" == environment.data_folder()
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path / "new"))
    assert environment.data_folder() == tmp_path / "new" == environment.config_folder()


def test_where_the_lex_stores_are(tmp_path: Path, platform_dirs: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """``LEXCOR_DATA`` first; else PseudonyMed's own data folder when its variable names
    one; else the folder of the libraries, where their other users find the stores."""
    monkeypatch.delenv("LEXCOR_DATA", raising=False)
    shared = lexcor.store.data_folder()
    assert environment.lex_folder() == shared
    assert shared != environment.data_folder()  # not beside the pseudonym database
    assert vocabulary.default_path() == shared / "lexterm.sqlite"
    monkeypatch.setenv("PSEUDONYMED_DATA_FOLDER", str(tmp_path / "portable"))
    assert environment.lex_folder() == tmp_path / "portable" == environment.data_folder()
    assert vocabulary.default_path() == tmp_path / "portable" / "lexterm.sqlite"
    monkeypatch.setenv("LEXCOR_DATA", str(tmp_path / "lex"))
    assert environment.lex_folder() == tmp_path / "lex" == lexcor.store.data_folder()
    assert environment.data_folder() == tmp_path / "portable"  # the database stays put
    monkeypatch.setenv("LEXCOR_DATA", "")  # empty is not set, as lexcor reads it
    assert environment.lex_folder() == tmp_path / "portable"


def test_tests_run_in_their_own_data_folder() -> None:
    """The isolation is in place from collection on (see conftest.pytest_configure)."""
    from platformdirs import user_data_path

    folder = environment.data_folder()
    assert folder.name.startswith("pseudonymed-daten-")
    assert folder != user_data_path(environment.NAME, appauthor=False)
    assert environment.config_folder() == folder
