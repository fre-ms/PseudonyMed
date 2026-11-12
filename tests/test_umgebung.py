"""Environment variables and per-user folders, including the fallbacks to the former name
(tests/test_umbenennung.py covers the happy path of the folder move)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pseudonymed import umgebung


@pytest.fixture
def plattform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The platform folders point into tmp_path and no data-folder variable is set."""
    for n in umgebung.namen("DATENORDNER"):
        monkeypatch.delenv(n, raising=False)
    wurzel = tmp_path / "plattform"
    monkeypatch.setattr(umgebung, "user_data_path",
                        lambda name, appauthor: wurzel / "daten" / name)
    monkeypatch.setattr(umgebung, "user_config_path",
                        lambda name, appauthor: wurzel / "konfig" / name)
    return wurzel


def test_leere_variable_zaehlt_als_gesetzt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty current-name variable wins over a filled former-name one: set is set."""
    monkeypatch.setenv("PSEUDONYMED_PROBE", "")
    monkeypatch.setenv("ANONYMED_PROBE", "alt")
    assert umgebung.variable("PROBE", "standard") == ""
    monkeypatch.delenv("PSEUDONYMED_PROBE")
    assert umgebung.variable("PROBE", "standard") == "alt"
    monkeypatch.delenv("ANONYMED_PROBE")
    assert umgebung.variable("PROBE") is None


def test_beide_ordner_aus_der_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSEUDONYMED_DATENORDNER", raising=False)
    monkeypatch.setenv("ANONYMED_DATENORDNER", str(tmp_path / "portabel"))
    assert umgebung.konfigordner() == tmp_path / "portabel" == umgebung.datenordner()
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "neu"))
    assert umgebung.datenordner() == tmp_path / "neu" == umgebung.konfigordner()


def test_umzug_legt_den_elternordner_an(plattform: Path) -> None:
    """Configuration and data live in different trees (Linux): the new tree may be missing."""
    alt = plattform / "konfig" / "AnonyMed"
    alt.mkdir(parents=True)
    (alt / "konfig.toml").write_text("[gui]\n", encoding="utf-8")
    neu = umgebung.konfigordner()
    assert neu == plattform / "konfig" / "PseudonyMed"
    assert (neu / "konfig.toml").exists() and not alt.exists()
    assert umgebung.datenordner() == plattform / "daten" / "PseudonyMed"  # nothing to move


def test_umzug_scheitert_alter_ordner_bleibt(plattform: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    alt = plattform / "daten" / "AnonyMed"
    alt.mkdir(parents=True)

    def verweigert(self: Path, ziel: Path) -> None:
        raise OSError("anderer Datenträger")

    monkeypatch.setattr(Path, "rename", verweigert)
    assert umgebung.datenordner() == alt and alt.is_dir()
    assert not (plattform / "daten" / "PseudonyMed").exists()
