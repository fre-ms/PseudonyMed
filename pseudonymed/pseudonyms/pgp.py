"""OpenPGP for key capsules: encrypt to a recipient's public key, decrypt with the
recipient's private key.

Two engines.  pysequoia (extra ``pgp``, Sequoia-PGP with wheels for every platform,
bundled in the desktop app) lets a patient or a colleague encrypt with nothing but the
recipient's public key.  The system's GnuPG serves the recipient, whose private key
lives in its keyring and whose agent asks for the passphrase.  Reading a public key
and encrypting try pysequoia first and fall back to ``gpg``; decrypting with a
private-key *file* goes through pysequoia, decrypting from the keyring through
``gpg``.  (GnuPG 2.5 exports Curve25519 secret keys in a form Sequoia's Python binding
does not read yet; such keys are used through the keyring route.)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pseudonymed.core.text import english, say

if TYPE_CHECKING:
    import pysequoia as sq

_GPG_LOCATIONS = (
    "/opt/homebrew/bin/gpg", "/usr/local/bin/gpg", "/usr/local/MacGPG2/bin/gpg2", "/usr/bin/gpg",
    r"C:\Program Files (x86)\GnuPG\bin\gpg.exe", r"C:\Program Files\GnuPG\bin\gpg.exe",
)
_PUBLIC_KEY_HEADER = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_MESSAGE_HEADER = "-----BEGIN PGP MESSAGE-----"
# The passphrase goes to gpg on stdin instead of through the agent's pinentry dialogue.
_LOOPBACK = ("--pinentry-mode", "loopback", "--passphrase-fd", "0")
# First byte of an unarmoured message: a public-key or symmetric-key encrypted session key
# packet (tags 1 and 3) in the old and the new packet format.
_BINARY_MESSAGE = (b"\x84", b"\x85", b"\xc1", b"\xc3")
MAX_KEY_BYTES = 262_144


class PGPError(Exception):
    """Any OpenPGP problem: no engine, an unreadable or invalid key, a failed operation."""


@dataclass
class Certificate:
    """A recipient's public key: fingerprint, user IDs and the armoured key itself."""

    fingerprint: str  # 40 hex digits, upper case
    user_ids: list[str] = field(default_factory=list)
    armor: str = ""
    expiry: str = ""

    @property
    def groups(self) -> str:
        """The fingerprint in groups of four, as key servers and key signing parties show it."""
        return " ".join(self.fingerprint[i:i + 4] for i in range(0, len(self.fingerprint), 4))

    @property
    def short(self) -> str:
        """The last four groups of the fingerprint, enough to tell keys apart in a list."""
        return self.groups[-19:]  # the last four groups

    def description(self, language: str = "de") -> str:
        """User IDs and the grouped fingerprint on one line, for dialogues and reports."""
        if english(language):
            who = "; ".join(self.user_ids) if self.user_ids else "without a name"
            return f"{who} · fingerprint {self.groups}"
        who = "; ".join(self.user_ids) if self.user_ids else "ohne Namen"
        return f"{who} · Fingerabdruck {self.groups}"


# -- engines ---------------------------------------------------------------------------------------
def pysequoia_available() -> bool:
    """True when the ``pgp`` extra (pysequoia) is importable."""
    try:
        import pysequoia  # noqa: F401
    except ImportError:
        return False
    return True


def gnupg_program() -> str | None:
    """Path of the ``gpg`` program: from PATH, else from the usual install locations
    (a desktop app launched from the Finder has a minimal PATH), else None."""
    for name in ("gpg", "gpg2"):
        path = shutil.which(name)
        if path:
            return path
    for place in _GPG_LOCATIONS:
        if os.path.isfile(place) and os.access(place, os.X_OK):
            return place
    return None


def available() -> bool:
    """True when at least one engine can encrypt and decrypt."""
    return pysequoia_available() or gnupg_program() is not None


def engines() -> list[str]:
    """The engines found, with version or path, for the configuration overview."""
    out = []
    if pysequoia_available():
        import importlib.metadata as m

        out.append(f"pysequoia {m.version('pysequoia')}")
    gpg = gnupg_program()
    if gpg:
        out.append(f"GnuPG {gpg}")
    return out


def _gpg(arguments: list[str], input_text: bytes | None = None, home: Path | None = None,
         timeout: int = 120) -> bytes:
    """Run ``gpg --batch --yes`` with ``arguments`` and return its stdout; a failure
    becomes a PGPError carrying gpg's last stderr line.  ``home`` sets GNUPGHOME."""
    program = gnupg_program()
    if not program:
        raise PGPError(say("GnuPG (gpg) ist nicht installiert.", "GnuPG (gpg) is not installed."))
    environment = dict(os.environ)
    if home is not None:
        environment["GNUPGHOME"] = str(home)
    try:
        run = subprocess.run([program, "--batch", "--yes", *arguments], input=input_text,
                             capture_output=True, check=False, timeout=timeout, env=environment)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PGPError(say(f"GnuPG konnte nicht ausgeführt werden: {e}",
                           f"GnuPG could not be run: {e}")) from e
    if run.returncode != 0:
        message = run.stderr.decode("utf-8", "replace").strip()
        last = message.splitlines()[-1] if message else ""
        raise PGPError(say(f"GnuPG meldet: {last or f'Rückgabewert {run.returncode}'}",
                           f"GnuPG reports: {last or f'exit code {run.returncode}'}"))
    return run.stdout


# -- public keys -----------------------------------------------------------------------------------
def _fetch_https(address: str) -> bytes:
    """The body of an https response, refused when larger than a key can be."""
    request = urllib.request.Request(address, headers={"User-Agent": "PseudonyMed"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            data = response.read(MAX_KEY_BYTES + 1)
    except OSError as e:
        raise PGPError(say(f"Schlüssel konnte nicht geladen werden: {e}",
                           f"The key could not be loaded: {e}")) from e
    if len(data) > MAX_KEY_BYTES:
        raise PGPError(say("Die Antwort ist zu groß für einen Schlüssel.",
                           "The answer is too large for a key."))
    return data


def _fetch_file(path: Path) -> bytes:
    """The bytes of a key file, refused when missing or larger than a key can be."""
    if not path.is_file():
        raise PGPError(say(f"Schlüsseldatei nicht gefunden: {path}", f"Key file not found: {path}"))
    if path.stat().st_size > MAX_KEY_BYTES:
        raise PGPError(say("Die Datei ist zu groß für einen Schlüssel.",
                           "The file is too large for a key."))
    return path.read_bytes()


def _fetch(source: str | Path) -> bytes:
    """Key material from armoured text, an https address or a file."""
    if isinstance(source, str) and source.lstrip().startswith("-----BEGIN PGP"):
        return source.encode("utf-8")
    text = str(source)
    if text.startswith("http://"):
        raise PGPError(say("Öffentliche Schlüssel nur über https laden (oder als Datei).",
                           "Load public keys over https only (or as a file)."))
    if text.startswith("https://"):
        return _fetch_https(text)
    return _fetch_file(Path(text).expanduser())


def load_certificate(source: str | Path) -> Certificate:
    """The recipient's public key from armoured text, an https address or a file,
    checked for revocation and expiry."""
    data = _fetch(source)
    if pysequoia_available():
        return _certificate_sequoia(data)
    if gnupg_program():
        return _certificate_gnupg(data)
    raise PGPError(say("Weder pysequoia (Extra 'pgp') noch GnuPG vorhanden.",
                       "Neither pysequoia (extra 'pgp') nor GnuPG is available."))


def _certificate_sequoia(data: bytes) -> Certificate:
    """The certificate as pysequoia reads it (binary or armoured), refused when revoked
    or expired."""
    import pysequoia as sq

    try:
        cert = sq.Cert.from_bytes(data)
    except Exception as e:  # noqa: BLE001
        raise PGPError(say(f"Kein lesbarer OpenPGP-Schlüssel: {e}",
                           f"Not a readable OpenPGP key: {e}")) from e
    if getattr(cert, "is_revoked", False):
        raise PGPError(say("Der Schlüssel ist widerrufen.", "The key is revoked."))
    expiry = cert.expiration
    if expiry is not None:
        import datetime as dt

        if expiry < dt.datetime.now(tz=expiry.tzinfo):
            raise PGPError(say(f"Der Schlüssel ist abgelaufen ({expiry:%d.%m.%Y}).",
                               f"The key expired on {expiry:%d %B %Y}."))
    return Certificate(fingerprint=str(cert.fingerprint).upper(),
                       user_ids=[str(u) for u in cert.user_ids],
                       armor=sq.armor(bytes(cert), sq.ArmorKind.PublicKey),
                      expiry=f"{expiry:%Y-%m-%d}" if expiry is not None else "")


def _certificate_gnupg(data: bytes) -> Certificate:
    """The certificate as ``gpg --import-options show-only`` reads it; only armoured
    text, because the armour is kept verbatim for encrypting later."""
    text = data.decode("utf-8", "replace")
    if _PUBLIC_KEY_HEADER not in text:
        raise PGPError(say("Ohne pysequoia bitte einen ASCII-gepanzerten Schlüssel"
                           " (gpg --armor --export) angeben.",
                           "Without pysequoia please give an ASCII-armoured key"
                           " (gpg --armor --export)."))
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "schluessel.asc"
        file.write_bytes(data)
        out = _gpg(["--with-colons", "--import-options", "show-only", "--import", str(file)])
    fpr, user_ids, expiry = _read_colon_output(out)
    if not fpr:
        raise PGPError(say("GnuPG fand keinen Schlüssel in der Datei.",
                           "GnuPG found no key in the file."))
    return Certificate(fingerprint=fpr.upper(), user_ids=user_ids, armor=text, expiry=expiry)


def _read_colon_output(out: bytes) -> tuple[str, list[str], str]:
    """Primary fingerprint, user IDs and expiry date from ``gpg --with-colons`` records
    (the first ``fpr`` record belongs to the primary key, later ones to subkeys); the
    ``pub`` record's validity field refuses a revoked or expired key."""
    fpr, user_ids, expiry = "", [], ""
    for line in out.decode("utf-8", "replace").splitlines():
        fields = line.split(":")
        match fields[0]:
            case "fpr" if not fpr:
                fpr = fields[9]
            case "uid":
                user_ids.append(fields[9])
            case "pub":
                _check_validity(fields[1])
                expiry = fields[6]
    return fpr, user_ids, expiry


def _check_validity(flags: str) -> None:
    """Refuse a key whose validity field marks it revoked (``r``) or expired (``e``)."""
    if "r" in flags:
        raise PGPError(say("Der Schlüssel ist widerrufen.", "The key is revoked."))
    if "e" in flags:
        raise PGPError(say("Der Schlüssel ist abgelaufen.", "The key has expired."))


# -- encrypt / decrypt -----------------------------------------------------------------------------
def encrypt(data: bytes, certificate: Certificate) -> bytes:
    """``data`` as an ASCII-armoured OpenPGP message for the certificate's owner."""
    if pysequoia_available():
        import pysequoia as sq

        cert = sq.Cert.from_bytes(certificate.armor.encode("utf-8"))
        return sq.encrypt(recipients=[cert], bytes=data)
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "empfaenger.asc"
        file.write_text(certificate.armor, encoding="utf-8")
        return _gpg(["--armor", "--trust-model", "always", "--recipient-file", str(file),
                     "--encrypt"], input_text=data)


def decrypt(data: bytes, private_key: str | Path | None = None,
            passphrase: str | None = None, gnupg_home: Path | None = None) -> bytes:
    """Decrypt an OpenPGP message: with a private-key file through pysequoia, else with
    the keyring of the system's GnuPG (its agent asks for the passphrase unless one is
    given)."""
    if private_key:
        return _decrypt_sequoia(data, private_key, passphrase)
    if not gnupg_program():
        raise PGPError(say("Kein privater Schlüssel angegeben und GnuPG nicht installiert.",
                           "No private key given and GnuPG not installed."))
    return _decrypt_gnupg(data, passphrase, gnupg_home)


def _decrypt_sequoia(data: bytes, private_key: str | Path,
                     passphrase: str | None) -> bytes:
    """Decrypt with a private-key file through pysequoia."""
    if not pysequoia_available():
        raise PGPError(say("Für eine Schlüsseldatei wird pysequoia gebraucht (Extra 'pgp');"
                           " sonst den Schlüssel in GnuPG importieren.",
                           "A key file needs pysequoia (extra 'pgp'); otherwise import the key"
                           " into GnuPG."))
    import pysequoia as sq

    try:
        tsk = sq.Tsk.from_file(str(Path(private_key).expanduser()))
    except Exception as e:  # noqa: BLE001
        raise PGPError(say(f"Privater Schlüssel nicht lesbar: {e}. GnuPG-2.5-Exporte von"
                           " Curve25519-Schlüsseln bitte über den Schlüsselbund nutzen.",
                           f"The private key is not readable: {e}. Please use GnuPG 2.5 exports"
                           " of Curve25519 keys through the keyring.")) from e
    dec = _unlocked(tsk, passphrase)
    try:
        return _plaintext(sq.decrypt(decryptor=dec, bytes=data))
    except Exception as e:  # noqa: BLE001
        raise PGPError(say(f"Entschlüsselung fehlgeschlagen: {e}",
                           f"Decryption failed: {e}")) from e


def _unlocked(tsk: sq.Tsk, passphrase: str | None) -> sq.PyDecryptor:
    """The key's decryptor, unlocked with the passphrase when one is given.  A locked
    key asked without any passphrase says "Passphrase nötig" so that the caller can
    ask for one; a wrong passphrase reports the library's error."""
    try:
        return tsk.decryptor(password=passphrase) if passphrase else tsk.decryptor()
    except Exception as e:  # noqa: BLE001
        if passphrase is None:
            raise PGPError(say("Passphrase nötig", "Passphrase needed")) from e
        raise PGPError(say(f"Schlüssel ließ sich nicht entsperren: {e}",
                           f"The key could not be unlocked: {e}")) from e


def _decrypt_gnupg(data: bytes, passphrase: str | None,
                   gnupg_home: Path | None) -> bytes:
    """Decrypt through the keyring of the system's GnuPG; without a passphrase the
    agent's pinentry asks for it."""
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "kapsel.asc"
        file.write_bytes(data)
        arguments = ["--decrypt"]
        input_text = None
        if passphrase is not None:
            arguments = [*_LOOPBACK, "--decrypt"]
            input_text = passphrase.encode("utf-8") + b"\n"
        return _gpg([*arguments, str(file)], input_text=input_text, home=gnupg_home)


def _plaintext(result: sq.Decrypted) -> bytes:
    """The plaintext of a pysequoia result (its ``bytes`` is None only for file
    operations, which this module does not use)."""
    return bytes(result.bytes or b"")


# -- passphrase (symmetric) ------------------------------------------------------------------------
def encrypt_with_passphrase(data: bytes, passphrase: str) -> bytes:
    """``data`` as a binary OpenPGP message protected by a passphrase (backups)."""
    if not passphrase:
        raise PGPError(say("Leere Passphrase.", "Empty passphrase."))
    if pysequoia_available():
        import pysequoia as sq

        return sq.encrypt(passwords=[passphrase], bytes=data, armor=False)
    return _gpg([*_LOOPBACK, "--symmetric", "--cipher-algo", "AES256", "--output", "-"],
                input_text=passphrase.encode("utf-8") + b"\n" + data)


def decrypt_with_passphrase(data: bytes, passphrase: str) -> bytes:
    """The plaintext of a passphrase-protected OpenPGP message (backups)."""
    if pysequoia_available():
        import pysequoia as sq

        try:
            return _plaintext(sq.decrypt(passwords=[passphrase], bytes=data))
        except Exception as e:  # noqa: BLE001
            raise PGPError(say(f"Entschlüsselung fehlgeschlagen (Passphrase?): {e}",
                               f"Decryption failed (passphrase?): {e}")) from e
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "sicherung.gpg"
        file.write_bytes(data)
        return _gpg([*_LOOPBACK, "--decrypt", str(file)],
                    input_text=passphrase.encode("utf-8") + b"\n")


# -- key pairs for people without GnuPG ------------------------------------------------------------
def create_key_pair(name: str, email: str) -> tuple[str, str]:
    """A fresh OpenPGP key pair (Curve25519) as armoured public and secret key."""
    if not pysequoia_available():
        raise PGPError(say("Schlüssel erzeugen braucht pysequoia (Extra 'pgp').",
                           "Creating a key needs pysequoia (extra 'pgp')."))
    import pysequoia as sq

    if not re.fullmatch(r"[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+", email or ""):
        raise PGPError(say("Bitte eine gültige E-Mail-Adresse angeben.",
                           "Please give a valid e-mail address."))
    tsk = sq.Tsk.generate(f"{name.strip()} <{email.strip()}>")
    public_key = sq.armor(bytes(tsk.extract_certificate()), sq.ArmorKind.PublicKey)
    secret_key = sq.armor(bytes(tsk), sq.ArmorKind.SecretKey)
    return public_key, secret_key


def is_message(data: bytes) -> bool:
    """True when ``data`` look like an OpenPGP message, armoured or binary."""
    return (data.lstrip().startswith(_MESSAGE_HEADER.encode())
            or data[:1] in _BINARY_MESSAGE)
