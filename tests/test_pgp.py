"""OpenPGP engines: the paths the capsule and backup tests leave out.  Keys are made on
the fly, by pysequoia or in a throw-away GnuPG home; nothing touches the user's keyring
and nothing goes to the network (the https route is faked)."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from pseudonymed.pseudonyme import pgp

braucht_sequoia = pytest.mark.skipif(not pgp.pysequoia_verfuegbar(),
                                     reason="Extra 'pgp' (pysequoia) fehlt")
braucht_gnupg = pytest.mark.skipif(pgp.gnupg_programm() is None, reason="GnuPG fehlt")
PASSPHRASE = "pw"
ARMOR_LEER = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n-----END PGP PUBLIC KEY BLOCK-----\n"


class GnupgHome(NamedTuple):
    """A throw-away GnuPG home and the key it holds."""

    home: Path
    pub: Path  # armoured public key
    sec: Path  # armoured secret key, protected with PASSPHRASE


def _gpgconf() -> str:
    """The gpgconf next to the gpg in use (gpg.exe gets gpgconf.exe)."""
    gpg = Path(pgp.gnupg_programm() or "gpg")
    daneben = gpg.with_name("gpgconf" + gpg.suffix)
    return str(daneben) if daneben.exists() else "gpgconf"


def _agent_beenden(home: Path) -> None:
    """Stop the agent of ``home`` (it restarts on demand); its passphrase cache goes with it."""
    subprocess.run([_gpgconf(), "--kill", "gpg-agent"], env={**os.environ, "GNUPGHOME": str(home)},
                   capture_output=True, check=False)


@pytest.fixture(scope="module")
def sequoia_paar(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    """Public and secret key file of a fresh pysequoia pair, plus the armoured public key."""
    if not pgp.pysequoia_verfuegbar():
        pytest.skip("Extra 'pgp' fehlt")
    pub, sec = pgp.erzeuge_schluesselpaar("Test Empfaenger", "e@example.org")
    ordner = tmp_path_factory.mktemp("paar")
    (ordner / "pub.asc").write_text(pub, encoding="utf-8")
    (ordner / "sec.asc").write_text(sec, encoding="utf-8")
    return ordner / "pub.asc", ordner / "sec.asc", pub


@pytest.fixture(scope="module")
def gnupg_home() -> Iterator[GnupgHome]:
    """A throw-away GnuPG home with a passphrase-protected RSA key (sign + encrypt);
    skipped when GnuPG or its agent cannot work here.  RSA, because pysequoia reads
    GnuPG's RSA secret-key export but not its Curve25519 one."""
    gpg = pgp.gnupg_programm()
    if not gpg:
        pytest.skip("GnuPG fehlt")
    home = Path(tempfile.mkdtemp(prefix="ag"))  # short: the agent's socket path is limited
    os.chmod(home, 0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}

    def lauf(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([gpg, "--batch", "--quiet", "--pinentry-mode", "loopback",
                               "--passphrase", PASSPHRASE, *args], env=env,
                              capture_output=True, timeout=120, check=False)

    try:
        erzeugt = lauf("--quick-generate-key", "Test <t@example.org>", "rsa2048", "default",
                       "never").returncode == 0
        liste = subprocess.run([gpg, "--batch", "--list-keys", "--with-colons"], env=env,
                               capture_output=True, text=True, check=False).stdout
        fpr = next((z.split(":")[9] for z in liste.splitlines() if z.startswith("fpr")), "")
        if not (erzeugt and fpr
                and lauf("--quick-add-key", fpr, "rsa2048", "encr", "never").returncode == 0):
            pytest.skip("GnuPG konnte hier keinen Schlüssel erzeugen (Agent)")
        pub, sec = home / "pub.asc", home / "sec.asc"
        pub.write_bytes(lauf("--armor", "--export", "t@example.org").stdout)
        sec.write_bytes(lauf("--armor", "--export-secret-keys", "t@example.org").stdout)
        yield GnupgHome(home, pub, sec)
    finally:
        _agent_beenden(home)
        shutil.rmtree(home, ignore_errors=True)


class _Antwort:
    """Stands in for the response object of urllib.request.urlopen."""

    def __init__(self, daten: bytes) -> None:
        self.daten = daten

    def __enter__(self) -> _Antwort:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.daten[:n] if n >= 0 else self.daten


# -- certificates and their sources ----------------------------------------------------------------
def test_zertifikat_beschreibung() -> None:
    z = pgp.Zertifikat(fingerabdruck="0123456789ABCDEF0123456789ABCDEF01234567",
                       nutzer=["A <a@example.org>", "B"])
    assert z.gruppen == "0123 4567 89AB CDEF 0123 4567 89AB CDEF 0123 4567"
    assert z.kurz == "89AB CDEF 0123 4567"
    assert z.beschreibung() == "A <a@example.org>; B · Fingerabdruck " + z.gruppen
    assert pgp.Zertifikat(fingerabdruck="AB").beschreibung().startswith("ohne Namen")


def test_quellen_der_schluessel(tmp_path: Path) -> None:
    """Armoured text, a file (missing, too big), http refused: all before any engine runs."""
    text = "  -----BEGIN PGP PUBLIC KEY BLOCK-----\nx"
    assert pgp._hole(text) == text.encode()
    with pytest.raises(pgp.PGPFehler, match="nicht gefunden"):
        pgp._hole(tmp_path / "fehlt.asc")
    gross = tmp_path / "gross.asc"
    gross.write_bytes(b"x" * (pgp.MAX_SCHLUESSEL_BYTES + 1))
    with pytest.raises(pgp.PGPFehler, match="zu groß"):
        pgp._hole(gross)
    klein = tmp_path / "k.asc"
    klein.write_bytes(b"abc")
    assert pgp._hole(str(klein)) == b"abc"
    with pytest.raises(pgp.PGPFehler, match="https"):
        pgp._hole("http://example.org/k.asc")


def test_https_wird_gelesen_und_begrenzt(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: dict[str, object] = {}

    def urlopen(anfrage: urllib.request.Request, timeout: float) -> _Antwort:
        gesehen.update(url=anfrage.full_url, ua=anfrage.get_header("User-agent"),
                       timeout=timeout)
        return _Antwort(bytes(gesehen.pop("daten")))  # type: ignore[call-overload]

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    gesehen["daten"] = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    assert pgp._hole("https://example.org/k.asc") == b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    assert gesehen == {"url": "https://example.org/k.asc", "ua": "PseudonyMed", "timeout": 15}
    gesehen["daten"] = b"x" * (pgp.MAX_SCHLUESSEL_BYTES + 1)
    with pytest.raises(pgp.PGPFehler, match="zu groß"):
        pgp._hole("https://example.org/k.asc")

    def kaputt(anfrage: urllib.request.Request, timeout: float) -> _Antwort:
        raise OSError("keine Verbindung")

    monkeypatch.setattr(urllib.request, "urlopen", kaputt)
    with pytest.raises(pgp.PGPFehler, match="keine Verbindung"):
        pgp._hole("https://example.org/k.asc")


@braucht_sequoia
def test_lade_zertifikat_ueber_https(sequoia_paar: tuple[Path, Path, str],
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _pub, _sec, armor = sequoia_paar
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda anfrage, timeout: _Antwort(armor.encode()))
    z = pgp.lade_zertifikat("https://example.org/k.asc")
    assert "e@example.org" in z.nutzer[0] and z.ablauf == "" and z.armor.startswith("-----BEGIN")


@braucht_sequoia
def test_sequoia_lehnt_unlesbare_widerrufene_und_abgelaufene_ab(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import pysequoia

    class Cert:
        """A certificate as the checks see it, without any key material."""

        is_revoked = False
        expiration: dt.datetime | None = None

        @classmethod
        def from_bytes(cls, daten: bytes) -> Cert:
            if daten == b"kaputt":
                raise ValueError("no armor")
            c = cls()
            c.is_revoked = daten == b"widerrufen"
            if daten == b"abgelaufen":
                c.expiration = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
            return c

    monkeypatch.setattr(pysequoia, "Cert", Cert)
    with pytest.raises(pgp.PGPFehler, match="Kein lesbarer"):
        pgp._zertifikat_sequoia(b"kaputt")
    with pytest.raises(pgp.PGPFehler, match="widerrufen"):
        pgp._zertifikat_sequoia(b"widerrufen")
    with pytest.raises(pgp.PGPFehler, match=r"abgelaufen \(01\.01\.2020\)"):
        pgp._zertifikat_sequoia(b"abgelaufen")


@braucht_sequoia
def test_echter_ablauf() -> None:
    """A key that was valid for one second: pysequoia reports the expiry, the loader refuses."""
    import pysequoia as sq

    tsk = sq.Tsk.generate("Kurz <k@example.org>", validity_seconds=1)
    cert = tsk.extract_certificate()
    assert cert.expiration is not None
    while dt.datetime.now(tz=cert.expiration.tzinfo) <= cert.expiration:
        time.sleep(0.1)
    with pytest.raises(pgp.PGPFehler, match="abgelaufen"):
        pgp.lade_zertifikat(sq.armor(bytes(cert), sq.ArmorKind.PublicKey))


def test_colon_ausgabe_wird_gelesen() -> None:
    """``gpg --with-colons``: the primary key's fingerprint (the first ``fpr``), every user
    ID, the expiry field of ``pub``, and its validity flags."""
    aus = (b"tru:o:1:1:1:3:1:5\n"
           b"pub:-:255:22:0011223344556677:1700000000:1900000000::-:::cESC:::::ed25519:::0:\n"
           b"fpr:::::::::ABCDEF0123456789ABCDEF0123456789ABCDEF01:\n"
           b"uid:-::::1700000000::HASH::A B <a@example.org>::::::::::0:\n"
           b"uid:-::::1700000000::HASH::A B <b@example.org>::::::::::0:\n"
           b"sub:-:255:18:8877665544332211:1700000000::::::e:::::cv25519::\n"
           b"fpr:::::::::1111111111111111111111111111111111111111:\n")
    assert pgp._lese_colon_ausgabe(aus) == (
        "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        ["A B <a@example.org>", "A B <b@example.org>"], "1900000000")
    assert pgp._lese_colon_ausgabe(b"") == ("", [], "")
    with pytest.raises(pgp.PGPFehler, match="widerrufen"):
        pgp._lese_colon_ausgabe(b"pub:r:255:22:X:1::::::cESC::::::::0:\n")
    with pytest.raises(pgp.PGPFehler, match="abgelaufen"):
        pgp._lese_colon_ausgabe(b"pub:e:255:22:X:1::::::cESC::::::::0:\n")


def test_gnupg_zertifikat_ohne_schluessel(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pgp.PGPFehler, match="gepanzerten"):
        pgp._zertifikat_gnupg(b"\x99\x01binaer")
    monkeypatch.setattr(pgp, "_gpg", lambda argumente, **kw: b"tru:o:1:1:1:3:1:5\n")
    with pytest.raises(pgp.PGPFehler, match="keinen Schlüssel"):
        pgp._zertifikat_gnupg(ARMOR_LEER.encode())


@braucht_sequoia
@braucht_gnupg
def test_gnupg_liest_dasselbe_zertifikat(sequoia_paar: tuple[Path, Path, str], tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    pub, _sec, armor = sequoia_paar
    mit_sequoia = pgp.lade_zertifikat(pub)
    monkeypatch.setenv("GNUPGHOME", tmp_path.as_posix())  # show-only needs no agent, only a home
    monkeypatch.setattr(pgp, "pysequoia_verfuegbar", lambda: False)
    mit_gnupg = pgp.lade_zertifikat(pub)
    assert mit_gnupg.fingerabdruck == mit_sequoia.fingerabdruck
    assert mit_gnupg.nutzer == mit_sequoia.nutzer == ["Test Empfaenger <e@example.org>"]
    assert mit_gnupg.armor == armor and mit_gnupg.ablauf == ""


# -- engines ---------------------------------------------------------------------------------------
def test_gnupg_programm_sucht_feste_orte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(pgp, "_GPG_ORTE", (str(tmp_path / "fehlt"), str(tmp_path / "gpg")))
    assert pgp.gnupg_programm() is None
    (tmp_path / "gpg").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(tmp_path / "gpg", 0o755)
    assert pgp.gnupg_programm() == str(tmp_path / "gpg")
    monkeypatch.setattr(shutil, "which", lambda name: "/pfad/zu/" + name)
    assert pgp.gnupg_programm() == "/pfad/zu/gpg"


def test_engines_und_verfuegbarkeit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pgp, "pysequoia_verfuegbar", lambda: False)
    monkeypatch.setattr(pgp, "gnupg_programm", lambda: None)
    assert pgp.engines() == [] and not pgp.verfuegbar()
    with pytest.raises(pgp.PGPFehler, match="Weder"):
        pgp.lade_zertifikat(ARMOR_LEER)
    with pytest.raises(pgp.PGPFehler, match="braucht pysequoia"):
        pgp.erzeuge_schluesselpaar("A", "a@example.org")
    monkeypatch.setattr(pgp, "gnupg_programm", lambda: "/opt/bin/gpg")
    assert pgp.engines() == ["GnuPG /opt/bin/gpg"] and pgp.verfuegbar()


@braucht_sequoia
def test_engines_nennt_pysequoia() -> None:
    assert any(e.startswith("pysequoia ") for e in pgp.engines())


def test_gpg_aufruf_und_fehler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pgp, "gnupg_programm", lambda: None)
    with pytest.raises(pgp.PGPFehler, match="nicht installiert"):
        pgp._gpg(["--version"])
    with pytest.raises(pgp.PGPFehler, match="GnuPG nicht installiert"):
        pgp.entschluessele(b"x")
    monkeypatch.setattr(pgp, "gnupg_programm", lambda: "/pfad/gpg")
    gesehen: dict[str, object] = {}

    def lauf(befehl: list[str], **kw: object) -> subprocess.CompletedProcess:
        gesehen.update(befehl=befehl, home=dict(kw["env"]).get("GNUPGHOME"),  # type: ignore[call-overload]
                       eingabe=kw["input"])
        return subprocess.CompletedProcess(befehl, 2, b"",
                                           b"gpg: warnung\ngpg: decryption failed: No secret key\n")

    monkeypatch.setattr(subprocess, "run", lauf)
    with pytest.raises(pgp.PGPFehler, match="GnuPG meldet: gpg: decryption failed: No secret key"):
        pgp._gpg(["--decrypt"], eingabe=b"pw\n", home=tmp_path)
    assert gesehen == {"befehl": ["/pfad/gpg", "--batch", "--yes", "--decrypt"],
                       "home": str(tmp_path), "eingabe": b"pw\n"}
    monkeypatch.setattr(subprocess, "run",
                        lambda befehl, **kw: subprocess.CompletedProcess(befehl, 3, b"", b""))
    with pytest.raises(pgp.PGPFehler, match="Rückgabewert 3"):
        pgp._gpg([])

    def startet_nicht(befehl: list[str], **kw: object) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", startet_nicht)
    with pytest.raises(pgp.PGPFehler, match="nicht ausgeführt"):
        pgp._gpg([])
    monkeypatch.setattr(subprocess, "run",
                        lambda befehl, **kw: subprocess.CompletedProcess(befehl, 0, b"aus", b""))
    assert pgp._gpg([]) == b"aus"


# -- decrypting ------------------------------------------------------------------------------------
@braucht_sequoia
def test_schluesseldatei_fehler(sequoia_paar: tuple[Path, Path, str], tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    pub, sec, _armor = sequoia_paar
    nachricht = pgp.verschluessele(b"geheim", pgp.lade_zertifikat(pub))
    assert pgp.ist_nachricht(nachricht) and pgp.entschluessele(nachricht, sec) == b"geheim"
    with pytest.raises(pgp.PGPFehler, match="nicht lesbar"):
        pgp.entschluessele(nachricht, tmp_path / "fehlt.asc")
    with pytest.raises(pgp.PGPFehler, match="nicht entsperren"):  # unprotected key, passphrase
        pgp.entschluessele(nachricht, sec, "falsch")
    with pytest.raises(pgp.PGPFehler, match="Entschlüsselung fehlgeschlagen"):
        pgp.entschluessele(b"-----BEGIN PGP MESSAGE-----\n\nkaputt\n-----END PGP MESSAGE-----\n",
                           sec)
    anderer = tmp_path / "anderer.asc"
    anderer.write_text(pgp.erzeuge_schluesselpaar("Andere", "x@example.org")[1], encoding="utf-8")
    with pytest.raises(pgp.PGPFehler, match="fehlgeschlagen"):
        pgp.entschluessele(nachricht, anderer)
    monkeypatch.setattr(pgp, "pysequoia_verfuegbar", lambda: False)
    with pytest.raises(pgp.PGPFehler, match="pysequoia gebraucht"):
        pgp.entschluessele(nachricht, sec)


@braucht_sequoia
def test_geschuetzte_schluesseldatei(gnupg_home: GnupgHome) -> None:
    """A GnuPG RSA export with passphrase, read by pysequoia: no passphrase asks for one,
    a wrong one fails to unlock, the right one decrypts."""
    nachricht = pgp.verschluessele(b"geheim", pgp.lade_zertifikat(gnupg_home.pub))
    with pytest.raises(pgp.PGPFehler, match="Passphrase nötig"):
        pgp.entschluessele(nachricht, gnupg_home.sec)
    with pytest.raises(pgp.PGPFehler, match="nicht entsperren"):
        pgp.entschluessele(nachricht, gnupg_home.sec, "falsch")
    assert pgp.entschluessele(nachricht, gnupg_home.sec, PASSPHRASE) == b"geheim"


def test_gnupg_als_einzige_engine(gnupg_home: GnupgHome, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pysequoia: certificate, encryption, keyring decryption and the symmetric
    backup format all go through gpg (in the throw-away home)."""
    monkeypatch.setattr(pgp, "pysequoia_verfuegbar", lambda: False)
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home.home))
    assert pgp.engines() == [f"GnuPG {pgp.gnupg_programm()}"]
    z = pgp.lade_zertifikat(gnupg_home.pub)
    assert len(z.fingerabdruck) == 40 and z.nutzer == ["Test <t@example.org>"] and z.ablauf == ""
    nachricht = pgp.verschluessele(b"geheim", z)
    assert nachricht.startswith(b"-----BEGIN PGP MESSAGE-----") and pgp.ist_nachricht(nachricht)
    with pytest.raises(pgp.PGPFehler, match="GnuPG meldet"):  # first: the agent caches it
        pgp.entschluessele(nachricht, passphrase="falsch", gnupg_home=gnupg_home.home)
    assert pgp.entschluessele(nachricht, passphrase=PASSPHRASE,
                              gnupg_home=gnupg_home.home) == b"geheim"
    with pytest.raises(pgp.PGPFehler, match="pysequoia gebraucht"):
        pgp.entschluessele(nachricht, gnupg_home.sec, PASSPHRASE)
    sicherung = pgp.verschluessele_mit_passphrase(b"archiv", "sehr-geheim")
    assert sicherung[0] & 0x80 and not sicherung.startswith(b"-----")  # a binary packet
    _agent_beenden(gnupg_home.home)  # the agent would answer from its cache otherwise
    with pytest.raises(pgp.PGPFehler, match="GnuPG meldet"):
        pgp.entschluessele_mit_passphrase(sicherung, "anders")
    assert pgp.entschluessele_mit_passphrase(sicherung, "sehr-geheim") == b"archiv"


@braucht_sequoia
@braucht_gnupg
def test_gnupg_verschluesselt_sequoia_liest(sequoia_paar: tuple[Path, Path, str], tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Interoperability the other way round: gpg encrypts, a key file decrypts."""
    pub, sec, _armor = sequoia_paar
    monkeypatch.setenv("GNUPGHOME", tmp_path.as_posix())  # encrypting needs no agent
    monkeypatch.setattr(pgp, "pysequoia_verfuegbar", lambda: False)
    nachricht = pgp.verschluessele(b"quer", pgp.lade_zertifikat(pub))
    monkeypatch.undo()
    assert pgp.entschluessele(nachricht, sec) == b"quer"


# -- symmetric, key pairs, message detection -------------------------------------------------------
def test_leere_passphrase_und_inhalt() -> None:
    with pytest.raises(pgp.PGPFehler, match="Leere Passphrase"):
        pgp.verschluessele_mit_passphrase(b"x", "")
    assert pgp._inhalt(SimpleNamespace(bytes=b"ab")) == b"ab"
    assert pgp._inhalt(SimpleNamespace(bytes=None)) == b""


@braucht_sequoia
def test_sicherungsformat_mit_sequoia() -> None:
    s = pgp.verschluessele_mit_passphrase(b"archiv", "sehr-geheim")
    assert pgp.ist_nachricht(s) and s[:1] != b"-"
    assert pgp.entschluessele_mit_passphrase(s, "sehr-geheim") == b"archiv"
    with pytest.raises(pgp.PGPFehler, match=r"\(Passphrase\?\)"):
        pgp.entschluessele_mit_passphrase(s, "anders")
    with pytest.raises(pgp.PGPFehler, match="E-Mail"):
        pgp.erzeuge_schluesselpaar("A", "keine-adresse")


def test_ist_nachricht() -> None:
    assert pgp.ist_nachricht(b"\n -----BEGIN PGP MESSAGE-----\n")
    assert pgp.ist_nachricht(b"\x85\x01\x0c\x03")  # old packet format, two-octet length
    assert pgp.ist_nachricht(b"\xc1\x5e\x03")  # new packet format
    assert not pgp.ist_nachricht(b"") and not pgp.ist_nachricht(ARMOR_LEER.encode())
    assert not pgp.ist_nachricht(b"%PDF-1.7")
