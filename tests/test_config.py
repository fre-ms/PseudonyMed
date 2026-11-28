"""Configuration file: the settings round trip, the [carddav] section next to it, the
environment overrides and the keyring fallbacks (with a fake backend, never the user's
keyring)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from pseudonymed import config, environment

_NAMES = ("OCR", "TESSERACT", "LANGUAGE", "CARDDAV_URL", "CARDDAV_USER", "CARDDAV_ADDRESS_BOOK",
          "CARDDAV_PASSWORD")
_VARIABLES = [n for name in _NAMES for n in environment.names(name)]
ACCOUNT = config.CardDAVConfig(url="https://cloud.example.org", user="dr",
                               password_in_file="aus-datei")


@pytest.fixture(autouse=True)
def without_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file alone decides; the developer's environment must not leak in."""
    for n in _VARIABLES:
        monkeypatch.delenv(n, raising=False)


def _settings() -> config.Settings:
    """Every field away from its default, with characters TOML must escape."""
    return config.Settings(
        ocr_backend="tesseract", tesseract_path="/opt/bin/tesseract", tesseract_languages="deu+eng",
        gui_language="en", recipient_key="https://example.org/key.asc",
        recipient_fingerprint="ABCD" * 10, pgp_private_key="~/sec.asc",
        store_database="/daten/p.sqlite", store_key="/daten/s.txt",
        local_address_book='C:\\Karten\\"alle".vcf', reference_population=9_000_000,
        own_places="Musterstadt, Beispielhausen")


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ordner" / "config.toml"  # the folder is created on the way
    e = _settings()
    assert config.save_settings(e, path) == path
    assert config.load_settings(path) == e
    text = path.read_text(encoding="utf-8")
    assert text.index("[carddav]") < text.index("[ocr]") < text.index("[control]")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_carddav_and_settings_stay_separate(tmp_path: Path) -> None:
    """Each writer keeps the other's sections as they are."""
    path = tmp_path / "config.toml"
    k = config.CardDAVConfig(url="https://cloud.example.org", user="dr",
                             address_book="patienten", password_in_file="app-pw")
    config.save_config(k, path)
    config.save_settings(_settings(), path)
    assert config.load_config(path) == k and config.load_settings(path) == _settings()
    config.save_config(config.CardDAVConfig(url="https://neu.example.org", user="dr"),
                       path)
    assert config.load_settings(path) == _settings()
    text = path.read_text(encoding="utf-8")
    assert "password" not in text and 'url = "https://neu.example.org"' in text


def test_without_file_the_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "nein.toml"
    assert config.load_config(missing) == config.CardDAVConfig()
    assert config.load_settings(missing) == config.Settings()
    (tmp_path / "k.toml").write_text('[control]\nreference_population = 0\n[ocr]\nbackend = ""\n',
                                     encoding="utf-8")
    e = config.load_settings(tmp_path / "k.toml")
    assert e.reference_population == 83_600_000 and e.ocr_backend == "auto"


def test_german_names_are_read_english_ones_written(tmp_path: Path) -> None:
    """A file written by hand with the German section and key names is understood; saving
    it again writes the English names only."""
    path = tmp_path / "config.toml"
    path.write_text('[carddav]\nurl = "https://cloud.example.org"\nbenutzer = "dr"\n'
                    'adressbuch = "patienten"\npasswort = "app-pw"\n'
                    '[ocr]\nsprachen = "deu"\n[gui]\nsprache = "en"\n'
                    '[empfaenger]\nschluessel = "k.asc"\nfingerabdruck = "ABCD"\n'
                    '[pgp]\nprivater_schluessel = "s.asc"\n'
                    '[speicher]\ndatenbank = "p.sqlite"\nschluessel = "p.txt"\n'
                    '[adressbuch]\nlokal = "karten.vcf"\nmac_adressbuecher = ["A1"]\n'
                    '[kontrolle]\nreferenzpopulation = 9000\neigene_orte = "Musterstadt"\n',
                    encoding="utf-8")
    k = config.load_config(path)
    assert (k.user, k.address_book, k.password_in_file) == ("dr", "patienten", "app-pw")
    e = config.load_settings(path)
    assert e == config.Settings(
        tesseract_languages="deu", gui_language="en", recipient_key="k.asc",
        recipient_fingerprint="ABCD", pgp_private_key="s.asc", store_database="p.sqlite",
        store_key="p.txt", local_address_book="karten.vcf", mac_address_books=["A1"],
        reference_population=9000, own_places="Musterstadt")
    config.save_settings(e, path)
    text = path.read_text(encoding="utf-8")
    assert config.load_config(path) == k and config.load_settings(path) == e
    for german in ("benutzer", "adressbuch", "passwort", "sprache", "empfaenger", "schluessel",
                   "speicher", "datenbank", "lokal", "kontrolle", "referenzpopulation"):
        assert german not in text, german


def test_environment_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    config.save_config(config.CardDAVConfig(url="https://alt.example.org", user="alt",
                                            address_book="a"), path)
    config.save_settings(_settings(), path)
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_URL", " https://neu.example.org ")
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_BENUTZER", "neu")  # the German name counts too
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_ADDRESS_BOOK", "")
    k = config.load_config(path)
    assert (k.url, k.user, k.address_book) == ("https://neu.example.org", "neu", "")
    monkeypatch.setenv("PSEUDONYMED_OCR", " ")  # blank: back to auto
    monkeypatch.setenv("PSEUDONYMED_TESSERACT", "/usr/bin/tesseract")
    monkeypatch.setenv("PSEUDONYMED_LANGUAGE", "de")
    e = config.load_settings(path)
    assert (e.ocr_backend, e.tesseract_path, e.gui_language) == ("auto", "/usr/bin/tesseract", "de")
    assert e.tesseract_languages == "deu+eng"  # no variable for it: stays as in the file


def test_code_and_completeness() -> None:
    k = config.CardDAVConfig(url="https://cloud.example.org/remote.php/dav", user="dr")
    assert k.code == "dr@cloud.example.org" and k.complete
    without_scheme = config.CardDAVConfig(url="cloud.example.org", user="dr")
    assert without_scheme.code == "dr@cloud.example.org"
    assert not config.CardDAVConfig(url="https://cloud.example.org").complete


def test_toml_value_escaped() -> None:
    assert config._toml_value('a\\b"c') == '"a\\\\b\\"c"'
    assert config._toml_value("") == '""'


@pytest.fixture
def keyring_backend(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """A fake keyring: a dict behind get and set, so the user's keyring stays untouched."""
    keyring = pytest.importorskip("keyring")
    entries: dict[tuple[str, str], str] = {}

    def get_password(service: str, name: str) -> str | None:
        return entries.get((service, name))

    def set_password(service: str, name: str, password: str) -> None:
        entries[(service, name)] = password

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", set_password)
    return entries


def test_password_order(keyring_backend: dict[tuple[str, str], str],
                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment before keyring, keyring before the file."""
    assert config.load_password(ACCOUNT) == "aus-datei"
    assert config.save_password(ACCOUNT, "neu")
    assert keyring_backend == {(config.SERVICE, "dr@cloud.example.org"): "neu"}
    assert config.load_password(ACCOUNT) == "neu"
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_PASSWORT", "aus-umgebung")  # the German name
    assert config.load_password(ACCOUNT) == "aus-umgebung"
    monkeypatch.setenv(config.PASSWORD_ENV_VAR, "from-environment")  # the English one first
    assert config.load_password(ACCOUNT) == "from-environment"
    del keyring_backend[(config.SERVICE, "dr@cloud.example.org")]
    monkeypatch.delenv(config.PASSWORD_ENV_VAR)
    monkeypatch.delenv("PSEUDONYMED_CARDDAV_PASSWORT")
    assert config.load_password(ACCOUNT) == "aus-datei"


def test_keyring_error_falls_back_to_file(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")

    def broken(*_args: str) -> None:
        raise RuntimeError("kein Backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    monkeypatch.setattr(keyring, "set_password", broken)
    assert config.load_password(ACCOUNT) == "aus-datei"
    other = config.CardDAVConfig(url="https://x.example.org", user="dr")
    assert config.load_password(other) is None
    assert config.save_password(ACCOUNT, "pw") is False


def test_without_keyring_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the extra ``carddav``: no backend name, no storing, the file password still."""
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes ``import keyring`` fail
    assert config.keyring_backend() == ""
    assert config.save_password(ACCOUNT, "pw") is False
    assert config.load_password(ACCOUNT) == "aus-datei"


def test_keyring_name(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")
    from keyring.backends.fail import Keyring as NoKeyring

    class Dummy:
        """Stands in for a working backend."""

    monkeypatch.setattr(keyring, "get_keyring", lambda: Dummy())
    assert config.keyring_backend() == "test_config.Dummy"
    monkeypatch.setattr(keyring, "get_keyring", lambda: NoKeyring())
    assert config.keyring_backend() == ""

    def broken() -> None:
        raise RuntimeError("kein Backend")

    monkeypatch.setattr(keyring, "get_keyring", broken)
    assert config.keyring_backend() == ""
