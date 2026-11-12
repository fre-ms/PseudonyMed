"""Configuration file: the settings round trip, the [carddav] section next to it, the
environment overrides and the keyring fallbacks (with a fake backend, never the user's
keyring)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from pseudonymed import konfig, umgebung

_NAMEN = ("OCR", "TESSERACT", "SPRACHE", "CARDDAV_URL", "CARDDAV_BENUTZER", "CARDDAV_ADRESSBUCH",
          "CARDDAV_PASSWORT")
_VARIABLEN = [n for name in _NAMEN for n in umgebung.namen(name)]
KONTO = konfig.CardDAVKonfig(url="https://cloud.example.org", benutzer="dr",
                             passwort_datei="aus-datei")


@pytest.fixture(autouse=True)
def ohne_variablen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file alone decides; the developer's environment must not leak in."""
    for n in _VARIABLEN:
        monkeypatch.delenv(n, raising=False)


def _einstellungen() -> konfig.Einstellungen:
    """Every field away from its default, with characters TOML must escape."""
    return konfig.Einstellungen(
        ocr_backend="tesseract", tesseract_pfad="/opt/bin/tesseract", tesseract_sprachen="deu+eng",
        gui_sprache="en", empfaenger_schluessel="https://example.org/key.asc",
        empfaenger_fingerabdruck="ABCD" * 10, pgp_privater_schluessel="~/sec.asc",
        speicher_datenbank="/daten/p.sqlite", speicher_schluessel="/daten/s.txt",
        adressbuch_lokal='C:\\Karten\\"alle".vcf', referenzpopulation=9_000_000,
        eigene_orte="Musterstadt, Beispielhausen")


def test_einstellungen_rundlauf(tmp_path: Path) -> None:
    pfad = tmp_path / "ordner" / "konfig.toml"  # the folder is created on the way
    e = _einstellungen()
    assert konfig.speichere_einstellungen(e, pfad) == pfad
    assert konfig.lade_einstellungen(pfad) == e
    text = pfad.read_text(encoding="utf-8")
    assert text.index("[carddav]") < text.index("[ocr]") < text.index("[kontrolle]")
    if os.name == "posix":
        assert stat.S_IMODE(pfad.stat().st_mode) == 0o600


def test_carddav_und_einstellungen_bleiben_getrennt(tmp_path: Path) -> None:
    """Each writer keeps the other's sections as they are."""
    pfad = tmp_path / "konfig.toml"
    k = konfig.CardDAVKonfig(url="https://cloud.example.org", benutzer="dr",
                             adressbuch="patienten", passwort_datei="app-pw")
    konfig.speichere_konfig(k, pfad)
    konfig.speichere_einstellungen(_einstellungen(), pfad)
    assert konfig.lade_konfig(pfad) == k and konfig.lade_einstellungen(pfad) == _einstellungen()
    konfig.speichere_konfig(konfig.CardDAVKonfig(url="https://neu.example.org", benutzer="dr"),
                            pfad)
    assert konfig.lade_einstellungen(pfad) == _einstellungen()
    text = pfad.read_text(encoding="utf-8")
    assert "passwort" not in text and 'url = "https://neu.example.org"' in text


def test_ohne_datei_die_standardwerte(tmp_path: Path) -> None:
    fehlt = tmp_path / "nein.toml"
    assert konfig.lade_konfig(fehlt) == konfig.CardDAVKonfig()
    assert konfig.lade_einstellungen(fehlt) == konfig.Einstellungen()
    (tmp_path / "k.toml").write_text('[kontrolle]\nreferenzpopulation = 0\n[ocr]\nbackend = ""\n',
                                     encoding="utf-8")
    e = konfig.lade_einstellungen(tmp_path / "k.toml")
    assert e.referenzpopulation == 83_600_000 and e.ocr_backend == "auto"


def test_umgebung_ueberstimmt_die_datei(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pfad = tmp_path / "konfig.toml"
    konfig.speichere_konfig(konfig.CardDAVKonfig(url="https://alt.example.org", benutzer="alt",
                                                 adressbuch="a"), pfad)
    konfig.speichere_einstellungen(_einstellungen(), pfad)
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_URL", " https://neu.example.org ")
    monkeypatch.setenv("ANONYMED_CARDDAV_BENUTZER", "neu")  # the former name still counts
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_ADRESSBUCH", "")
    k = konfig.lade_konfig(pfad)
    assert (k.url, k.benutzer, k.adressbuch) == ("https://neu.example.org", "neu", "")
    monkeypatch.setenv("PSEUDONYMED_OCR", " ")  # blank: back to auto
    monkeypatch.setenv("ANONYMED_TESSERACT", "/usr/bin/tesseract")
    monkeypatch.setenv("PSEUDONYMED_SPRACHE", "de")
    e = konfig.lade_einstellungen(pfad)
    assert (e.ocr_backend, e.tesseract_pfad, e.gui_sprache) == ("auto", "/usr/bin/tesseract", "de")
    assert e.tesseract_sprachen == "deu+eng"  # no variable for it: stays as in the file


def test_kennung_und_vollstaendigkeit() -> None:
    k = konfig.CardDAVKonfig(url="https://cloud.example.org/remote.php/dav", benutzer="dr")
    assert k.kennung == "dr@cloud.example.org" and k.vollstaendig
    ohne_schema = konfig.CardDAVKonfig(url="cloud.example.org", benutzer="dr")
    assert ohne_schema.kennung == "dr@cloud.example.org"
    assert not konfig.CardDAVKonfig(url="https://cloud.example.org").vollstaendig


def test_toml_wert_maskiert() -> None:
    assert konfig._toml_wert('a\\b"c') == '"a\\\\b\\"c"'
    assert konfig._toml_wert("") == '""'


@pytest.fixture
def schluesselbund(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """A fake keyring: a dict behind get and set, so the user's keyring stays untouched."""
    keyring = pytest.importorskip("keyring")
    eintraege: dict[tuple[str, str], str] = {}

    def lesen(dienst: str, name: str) -> str | None:
        return eintraege.get((dienst, name))

    def schreiben(dienst: str, name: str, passwort: str) -> None:
        eintraege[(dienst, name)] = passwort

    monkeypatch.setattr(keyring, "get_password", lesen)
    monkeypatch.setattr(keyring, "set_password", schreiben)
    return eintraege


def test_passwort_reihenfolge(schluesselbund: dict[tuple[str, str], str],
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment before keyring, current service before the former one, file last."""
    assert konfig.passwort_laden(KONTO) == "aus-datei"
    assert konfig.passwort_speichern(KONTO, "neu")
    assert schluesselbund == {(konfig.DIENST, "dr@cloud.example.org"): "neu"}
    schluesselbund[(konfig.FRUEHERER_DIENST, "dr@cloud.example.org")] = "alt"
    assert konfig.passwort_laden(KONTO) == "neu"
    del schluesselbund[(konfig.DIENST, "dr@cloud.example.org")]
    assert konfig.passwort_laden(KONTO) == "alt"
    monkeypatch.setenv("ANONYMED_CARDDAV_PASSWORT", "aus-umgebung")
    assert konfig.passwort_laden(KONTO) == "aus-umgebung"


def test_schluesselbund_fehler_faellt_auf_datei_zurueck(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")

    def kaputt(*_argumente: str) -> None:
        raise RuntimeError("kein Backend")

    monkeypatch.setattr(keyring, "get_password", kaputt)
    monkeypatch.setattr(keyring, "set_password", kaputt)
    assert konfig.passwort_laden(KONTO) == "aus-datei"
    assert konfig.passwort_laden(konfig.CardDAVKonfig(url="https://x.example.org",
                                                      benutzer="dr")) is None
    assert konfig.passwort_speichern(KONTO, "pw") is False


def test_ohne_keyring_paket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the extra ``carddav``: no backend name, no storing, the file password still."""
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes ``import keyring`` fail
    assert konfig.schluesselbund() == ""
    assert konfig.passwort_speichern(KONTO, "pw") is False
    assert konfig.passwort_laden(KONTO) == "aus-datei"


def test_schluesselbund_name(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")
    from keyring.backends.fail import Keyring as Keiner

    class Attrappe:
        """Stands in for a working backend."""

    monkeypatch.setattr(keyring, "get_keyring", lambda: Attrappe())
    assert konfig.schluesselbund() == "test_konfig.Attrappe"
    monkeypatch.setattr(keyring, "get_keyring", lambda: Keiner())
    assert konfig.schluesselbund() == ""

    def kaputt() -> None:
        raise RuntimeError("kein Backend")

    monkeypatch.setattr(keyring, "get_keyring", kaputt)
    assert konfig.schluesselbund() == ""
