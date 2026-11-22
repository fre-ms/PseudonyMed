"""OpenPGP engines: the paths the capsule and backup tests leave out.  Keys are made on
the fly, by pysequoia or in a throw-away GnuPG home; nothing touches the user's keyring
and nothing goes to the network (the https route is faked)."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from pseudonymed.pseudonyms import pgp

needs_sequoia = pytest.mark.skipif(not pgp.pysequoia_available(),
                                   reason="Extra 'pgp' (pysequoia) fehlt")
needs_gnupg = pytest.mark.skipif(pgp.gnupg_program() is None, reason="GnuPG fehlt")
# the MSYS gpg of Git for Windows reads a Windows path in GNUPGHOME as relative to the
# working directory, so a home handed over by the environment only works elsewhere
home_from_environment = pytest.mark.skipif(sys.platform == "win32",
                                           reason="GNUPGHOME mit Windows-Pfad (MSYS-gpg)")
PASSPHRASE = "pw"
ARMOR_EMPTY = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n-----END PGP PUBLIC KEY BLOCK-----\n"


class GnupgHome(NamedTuple):
    """A throw-away GnuPG home and the key it holds."""

    home: Path
    pub: Path  # armoured public key
    sec: Path  # armoured secret key, protected with PASSPHRASE


def _gpgconf() -> str:
    """The gpgconf next to the gpg in use (gpg.exe gets gpgconf.exe)."""
    gpg = Path(pgp.gnupg_program() or "gpg")
    sibling = gpg.with_name("gpgconf" + gpg.suffix)
    return str(sibling) if sibling.exists() else "gpgconf"


def _stop_agent(home: Path) -> None:
    """Stop the agent of ``home`` (it restarts on demand); its passphrase cache goes with it."""
    subprocess.run([_gpgconf(), "--kill", "gpg-agent"], env={**os.environ, "GNUPGHOME": str(home)},
                   capture_output=True, check=False)


@pytest.fixture(scope="module")
def sequoia_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    """Public and secret key file of a fresh pysequoia pair, plus the armoured public key."""
    if not pgp.pysequoia_available():
        pytest.skip("Extra 'pgp' fehlt")
    pub, sec = pgp.create_key_pair("Test Empfaenger", "e@example.org")
    folder = tmp_path_factory.mktemp("paar")
    (folder / "pub.asc").write_text(pub, encoding="utf-8")
    (folder / "sec.asc").write_text(sec, encoding="utf-8")
    return folder / "pub.asc", folder / "sec.asc", pub


@pytest.fixture(scope="module")
def gnupg_home() -> Iterator[GnupgHome]:
    """A throw-away GnuPG home with a passphrase-protected RSA key (sign + encrypt);
    skipped when GnuPG or its agent cannot work here.  RSA, because pysequoia reads
    GnuPG's RSA secret-key export but not its Curve25519 one."""
    gpg = pgp.gnupg_program()
    if not gpg:
        pytest.skip("GnuPG fehlt")
    home = Path(tempfile.mkdtemp(prefix="ag"))  # short: the agent's socket path is limited
    os.chmod(home, 0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([gpg, "--batch", "--quiet", "--pinentry-mode", "loopback",
                               "--passphrase", PASSPHRASE, *args], env=env,
                              capture_output=True, timeout=120, check=False)

    try:
        created = run("--quick-generate-key", "Test <t@example.org>", "rsa2048", "default",
                      "never").returncode == 0
        listing = subprocess.run([gpg, "--batch", "--list-keys", "--with-colons"], env=env,
                                 capture_output=True, text=True, check=False).stdout
        fpr = next((z.split(":")[9] for z in listing.splitlines() if z.startswith("fpr")), "")
        if not (created and fpr
                and run("--quick-add-key", fpr, "rsa2048", "encr", "never").returncode == 0):
            pytest.skip("GnuPG konnte hier keinen Schlüssel erzeugen (Agent)")
        pub, sec = home / "pub.asc", home / "sec.asc"
        pub.write_bytes(run("--armor", "--export", "t@example.org").stdout)
        sec.write_bytes(run("--armor", "--export-secret-keys", "t@example.org").stdout)
        yield GnupgHome(home, pub, sec)
    finally:
        _stop_agent(home)
        shutil.rmtree(home, ignore_errors=True)


class _Response:
    """Stands in for the response object of urllib.request.urlopen."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.data[:n] if n >= 0 else self.data


# -- certificates and their sources ----------------------------------------------------------------
def test_certificate_description() -> None:
    z = pgp.Certificate(fingerprint="0123456789ABCDEF0123456789ABCDEF01234567",
                        user_ids=["A <a@example.org>", "B"])
    assert z.groups == "0123 4567 89AB CDEF 0123 4567 89AB CDEF 0123 4567"
    assert z.short == "89AB CDEF 0123 4567"
    assert z.description() == "A <a@example.org>; B · Fingerabdruck " + z.groups
    assert pgp.Certificate(fingerprint="AB").description().startswith("ohne Namen")


def test_key_sources(tmp_path: Path) -> None:
    """Armoured text, a file (missing, too big), http refused: all before any engine runs."""
    text = "  -----BEGIN PGP PUBLIC KEY BLOCK-----\nx"
    assert pgp._fetch(text) == text.encode()
    with pytest.raises(pgp.PGPError, match="nicht gefunden"):
        pgp._fetch(tmp_path / "fehlt.asc")
    large = tmp_path / "gross.asc"
    large.write_bytes(b"x" * (pgp.MAX_KEY_BYTES + 1))
    with pytest.raises(pgp.PGPError, match="zu groß"):
        pgp._fetch(large)
    small = tmp_path / "k.asc"
    small.write_bytes(b"abc")
    assert pgp._fetch(str(small)) == b"abc"
    with pytest.raises(pgp.PGPError, match="https"):
        pgp._fetch("http://example.org/k.asc")


def test_https_is_read_and_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.update(url=request.full_url, ua=request.get_header("User-agent"),
                    timeout=timeout)
        return _Response(bytes(seen.pop("data")))  # type: ignore[call-overload]

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    seen["data"] = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    assert pgp._fetch("https://example.org/k.asc") == b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    assert seen == {"url": "https://example.org/k.asc", "ua": "PseudonyMed", "timeout": 15}
    seen["data"] = b"x" * (pgp.MAX_KEY_BYTES + 1)
    with pytest.raises(pgp.PGPError, match="zu groß"):
        pgp._fetch("https://example.org/k.asc")

    def broken(request: urllib.request.Request, timeout: float) -> _Response:
        raise OSError("keine Verbindung")

    monkeypatch.setattr(urllib.request, "urlopen", broken)
    with pytest.raises(pgp.PGPError, match="keine Verbindung"):
        pgp._fetch("https://example.org/k.asc")


@needs_sequoia
def test_load_certificate_via_https(sequoia_pair: tuple[Path, Path, str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _pub, _sec, armor = sequoia_pair
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda request, timeout: _Response(armor.encode()))
    z = pgp.load_certificate("https://example.org/k.asc")
    assert "e@example.org" in z.user_ids[0] and z.expiry == "" and z.armor.startswith("-----BEGIN")


@needs_sequoia
def test_sequoia_rejects_unreadable_revoked_and_expired(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import pysequoia

    class Cert:
        """A certificate as the checks see it, without any key material."""

        is_revoked = False
        expiration: dt.datetime | None = None

        @classmethod
        def from_bytes(cls, data: bytes) -> Cert:
            if data == b"kaputt":
                raise ValueError("no armor")
            c = cls()
            c.is_revoked = data == b"widerrufen"
            if data == b"abgelaufen":
                c.expiration = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
            return c

    monkeypatch.setattr(pysequoia, "Cert", Cert)
    with pytest.raises(pgp.PGPError, match="Kein lesbarer"):
        pgp._certificate_sequoia(b"kaputt")
    with pytest.raises(pgp.PGPError, match="widerrufen"):
        pgp._certificate_sequoia(b"widerrufen")
    with pytest.raises(pgp.PGPError, match=r"abgelaufen \(01\.01\.2020\)"):
        pgp._certificate_sequoia(b"abgelaufen")


@needs_sequoia
def test_real_expiry() -> None:
    """A key that was valid for one second: pysequoia reports the expiry, the loader refuses."""
    import pysequoia as sq

    tsk = sq.Tsk.generate("Kurz <k@example.org>", validity_seconds=1)
    cert = tsk.extract_certificate()
    assert cert.expiration is not None
    while dt.datetime.now(tz=cert.expiration.tzinfo) <= cert.expiration:
        time.sleep(0.1)
    with pytest.raises(pgp.PGPError, match="abgelaufen"):
        pgp.load_certificate(sq.armor(bytes(cert), sq.ArmorKind.PublicKey))


def test_colon_output_is_read() -> None:
    """``gpg --with-colons``: the primary key's fingerprint (the first ``fpr``), every user
    ID, the expiry field of ``pub``, and its validity flags."""
    out = (b"tru:o:1:1:1:3:1:5\n"
           b"pub:-:255:22:0011223344556677:1700000000:1900000000::-:::cESC:::::ed25519:::0:\n"
           b"fpr:::::::::ABCDEF0123456789ABCDEF0123456789ABCDEF01:\n"
           b"uid:-::::1700000000::HASH::A B <a@example.org>::::::::::0:\n"
           b"uid:-::::1700000000::HASH::A B <b@example.org>::::::::::0:\n"
           b"sub:-:255:18:8877665544332211:1700000000::::::e:::::cv25519::\n"
           b"fpr:::::::::1111111111111111111111111111111111111111:\n")
    assert pgp._read_colon_output(out) == (
        "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        ["A B <a@example.org>", "A B <b@example.org>"], "1900000000")
    assert pgp._read_colon_output(b"") == ("", [], "")
    with pytest.raises(pgp.PGPError, match="widerrufen"):
        pgp._read_colon_output(b"pub:r:255:22:X:1::::::cESC::::::::0:\n")
    with pytest.raises(pgp.PGPError, match="abgelaufen"):
        pgp._read_colon_output(b"pub:e:255:22:X:1::::::cESC::::::::0:\n")


def test_gnupg_certificate_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pgp.PGPError, match="gepanzerten"):
        pgp._certificate_gnupg(b"\x99\x01binaer")
    monkeypatch.setattr(pgp, "_gpg", lambda arguments, **kw: b"tru:o:1:1:1:3:1:5\n")
    with pytest.raises(pgp.PGPError, match="keinen Schlüssel"):
        pgp._certificate_gnupg(ARMOR_EMPTY.encode())


@home_from_environment
@needs_sequoia
@needs_gnupg
def test_gnupg_reads_the_same_certificate(sequoia_pair: tuple[Path, Path, str], tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    pub, _sec, armor = sequoia_pair
    with_sequoia = pgp.load_certificate(pub)
    monkeypatch.setenv("GNUPGHOME", str(tmp_path))  # show-only needs no agent, only a home
    monkeypatch.setattr(pgp, "pysequoia_available", lambda: False)
    with_gnupg = pgp.load_certificate(pub)
    assert with_gnupg.fingerprint == with_sequoia.fingerprint
    assert with_gnupg.user_ids == with_sequoia.user_ids == ["Test Empfaenger <e@example.org>"]
    assert with_gnupg.armor == armor and with_gnupg.expiry == ""


# -- engines ---------------------------------------------------------------------------------------
def test_gnupg_program_searches_fixed_places(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(pgp, "_GPG_LOCATIONS", (str(tmp_path / "fehlt"), str(tmp_path / "gpg")))
    assert pgp.gnupg_program() is None
    (tmp_path / "gpg").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(tmp_path / "gpg", 0o755)
    assert pgp.gnupg_program() == str(tmp_path / "gpg")
    monkeypatch.setattr(shutil, "which", lambda name: "/pfad/zu/" + name)
    assert pgp.gnupg_program() == "/pfad/zu/gpg"


def test_engines_and_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pgp, "pysequoia_available", lambda: False)
    monkeypatch.setattr(pgp, "gnupg_program", lambda: None)
    assert pgp.engines() == [] and not pgp.available()
    with pytest.raises(pgp.PGPError, match="Weder"):
        pgp.load_certificate(ARMOR_EMPTY)
    with pytest.raises(pgp.PGPError, match="braucht pysequoia"):
        pgp.create_key_pair("A", "a@example.org")
    monkeypatch.setattr(pgp, "gnupg_program", lambda: "/opt/bin/gpg")
    assert pgp.engines() == ["GnuPG /opt/bin/gpg"] and pgp.available()


@needs_sequoia
def test_engines_names_pysequoia() -> None:
    assert any(e.startswith("pysequoia ") for e in pgp.engines())


def test_gpg_call_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pgp, "gnupg_program", lambda: None)
    with pytest.raises(pgp.PGPError, match="nicht installiert"):
        pgp._gpg(["--version"])
    with pytest.raises(pgp.PGPError, match="GnuPG nicht installiert"):
        pgp.decrypt(b"x")
    monkeypatch.setattr(pgp, "gnupg_program", lambda: "/pfad/gpg")
    seen: dict[str, object] = {}

    def run(command: list[str], **kw: object) -> subprocess.CompletedProcess:
        seen.update(command=command, home=dict(kw["env"]).get("GNUPGHOME"),  # type: ignore[call-overload]
                    input_text=kw["input"])
        return subprocess.CompletedProcess(command, 2, b"",
                                           b"gpg: warnung\ngpg: decryption failed: No secret key\n")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(pgp.PGPError, match="GnuPG meldet: gpg: decryption failed: No secret key"):
        pgp._gpg(["--decrypt"], input_text=b"pw\n", home=tmp_path)
    assert seen == {"command": ["/pfad/gpg", "--batch", "--yes", "--decrypt"],
                    "home": str(tmp_path), "input_text": b"pw\n"}
    monkeypatch.setattr(subprocess, "run",
                        lambda command, **kw: subprocess.CompletedProcess(command, 3, b"", b""))
    with pytest.raises(pgp.PGPError, match="Rückgabewert 3"):
        pgp._gpg([])

    def fails_to_start(command: list[str], **kw: object) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", fails_to_start)
    with pytest.raises(pgp.PGPError, match="nicht ausgeführt"):
        pgp._gpg([])
    monkeypatch.setattr(subprocess, "run",
                        lambda command, **kw: subprocess.CompletedProcess(command, 0, b"aus", b""))
    assert pgp._gpg([]) == b"aus"


# -- decrypting ------------------------------------------------------------------------------------
@needs_sequoia
def test_key_file_errors(sequoia_pair: tuple[Path, Path, str], tmp_path: Path,
                         monkeypatch: pytest.MonkeyPatch) -> None:
    pub, sec, _armor = sequoia_pair
    message = pgp.encrypt(b"geheim", pgp.load_certificate(pub))
    assert pgp.is_message(message) and pgp.decrypt(message, sec) == b"geheim"
    with pytest.raises(pgp.PGPError, match="nicht lesbar"):
        pgp.decrypt(message, tmp_path / "fehlt.asc")
    with pytest.raises(pgp.PGPError, match="nicht entsperren"):  # unprotected key, passphrase
        pgp.decrypt(message, sec, "falsch")
    with pytest.raises(pgp.PGPError, match="Entschlüsselung fehlgeschlagen"):
        pgp.decrypt(b"-----BEGIN PGP MESSAGE-----\n\nkaputt\n-----END PGP MESSAGE-----\n",
                    sec)
    other = tmp_path / "anderer.asc"
    other.write_text(pgp.create_key_pair("Andere", "x@example.org")[1], encoding="utf-8")
    with pytest.raises(pgp.PGPError, match="fehlgeschlagen"):
        pgp.decrypt(message, other)
    monkeypatch.setattr(pgp, "pysequoia_available", lambda: False)
    with pytest.raises(pgp.PGPError, match="pysequoia gebraucht"):
        pgp.decrypt(message, sec)


@needs_sequoia
def test_protected_key_file(gnupg_home: GnupgHome) -> None:
    """A GnuPG RSA export with passphrase, read by pysequoia: no passphrase asks for one,
    a wrong one fails to unlock, the right one decrypts."""
    message = pgp.encrypt(b"geheim", pgp.load_certificate(gnupg_home.pub))
    with pytest.raises(pgp.PGPError, match="Passphrase nötig"):
        pgp.decrypt(message, gnupg_home.sec)
    with pytest.raises(pgp.PGPError, match="nicht entsperren"):
        pgp.decrypt(message, gnupg_home.sec, "falsch")
    assert pgp.decrypt(message, gnupg_home.sec, PASSPHRASE) == b"geheim"


def test_gnupg_as_the_only_engine(gnupg_home: GnupgHome, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pysequoia: certificate, encryption, keyring decryption and the symmetric
    backup format all go through gpg (in the throw-away home)."""
    monkeypatch.setattr(pgp, "pysequoia_available", lambda: False)
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home.home))
    assert pgp.engines() == [f"GnuPG {pgp.gnupg_program()}"]
    z = pgp.load_certificate(gnupg_home.pub)
    assert len(z.fingerprint) == 40 and z.user_ids == ["Test <t@example.org>"] and z.expiry == ""
    message = pgp.encrypt(b"geheim", z)
    assert message.startswith(b"-----BEGIN PGP MESSAGE-----") and pgp.is_message(message)
    with pytest.raises(pgp.PGPError, match="GnuPG meldet"):  # first: the agent caches it
        pgp.decrypt(message, passphrase="falsch", gnupg_home=gnupg_home.home)
    assert pgp.decrypt(message, passphrase=PASSPHRASE,
                       gnupg_home=gnupg_home.home) == b"geheim"
    with pytest.raises(pgp.PGPError, match="pysequoia gebraucht"):
        pgp.decrypt(message, gnupg_home.sec, PASSPHRASE)
    backup = pgp.encrypt_with_passphrase(b"archiv", "sehr-geheim")
    assert backup[0] & 0x80 and not backup.startswith(b"-----")  # a binary packet
    _stop_agent(gnupg_home.home)  # the agent would answer from its cache otherwise
    with pytest.raises(pgp.PGPError, match="GnuPG meldet"):
        pgp.decrypt_with_passphrase(backup, "anders")
    assert pgp.decrypt_with_passphrase(backup, "sehr-geheim") == b"archiv"


@needs_sequoia
@home_from_environment
@needs_gnupg
def test_gnupg_encrypts_sequoia_reads(sequoia_pair: tuple[Path, Path, str], tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Interoperability the other way round: gpg encrypts, a key file decrypts."""
    pub, sec, _armor = sequoia_pair
    monkeypatch.setenv("GNUPGHOME", str(tmp_path))  # encrypting needs no agent
    monkeypatch.setattr(pgp, "pysequoia_available", lambda: False)
    message = pgp.encrypt(b"quer", pgp.load_certificate(pub))
    monkeypatch.undo()
    assert pgp.decrypt(message, sec) == b"quer"


# -- symmetric, key pairs, message detection -------------------------------------------------------
def test_empty_passphrase_and_content() -> None:
    with pytest.raises(pgp.PGPError, match="Leere Passphrase"):
        pgp.encrypt_with_passphrase(b"x", "")
    assert pgp._plaintext(SimpleNamespace(bytes=b"ab")) == b"ab"
    assert pgp._plaintext(SimpleNamespace(bytes=None)) == b""


@needs_sequoia
def test_backup_format_with_sequoia() -> None:
    s = pgp.encrypt_with_passphrase(b"archiv", "sehr-geheim")
    assert pgp.is_message(s) and s[:1] != b"-"
    assert pgp.decrypt_with_passphrase(s, "sehr-geheim") == b"archiv"
    with pytest.raises(pgp.PGPError, match=r"\(Passphrase\?\)"):
        pgp.decrypt_with_passphrase(s, "anders")
    with pytest.raises(pgp.PGPError, match="E-Mail"):
        pgp.create_key_pair("A", "keine-adresse")


def test_is_message() -> None:
    assert pgp.is_message(b"\n -----BEGIN PGP MESSAGE-----\n")
    assert pgp.is_message(b"\x85\x01\x0c\x03")  # old packet format, two-octet length
    assert pgp.is_message(b"\xc1\x5e\x03")  # new packet format
    assert not pgp.is_message(b"") and not pgp.is_message(ARMOR_EMPTY.encode())
    assert not pgp.is_message(b"%PDF-1.7")
