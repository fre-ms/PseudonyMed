"""User configuration (TOML in the config folder) and the CardDAV app password.

The password comes from the environment variable ``PSEUDONYMED_CARDDAV_PASSWORT``, from
the operating system's keyring (extra ``carddav``) or, only when the user chose so
because no keyring is available, from the configuration file itself (mode 0600).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from pseudonymed import environment

SERVICE = "PseudonyMed CardDAV"
FORMER_SERVICE = "AnonyMed CardDAV"  # keyring entries made before the renaming
PASSWORD_ENV_VAR = "PSEUDONYMED_CARDDAV_PASSWORT"


@dataclass
class CardDAVConfig:
    """The ``[carddav]`` section: server, user and address book; the password lives in
    the keyring unless the user put it into the file."""

    url: str = ""  # https://cloud.example.org
    user: str = ""
    address_book: str = ""  # URI segment or display name, empty = the only/first one
    password_in_file: str = ""  # app password kept in the file: fallback without a keyring

    @property
    def complete(self) -> bool:
        """True when server and user are set, the minimum for a connection."""
        return bool(self.url and self.user)

    @property
    def code(self) -> str:
        """Keyring account name: user@host."""
        host = urlsplit(self.url).hostname or self.url
        return f"{self.user}@{host}"


def config_path() -> Path:
    """Configuration file in the user config folder; PSEUDONYMED_DATENORDNER overrides the
    folder."""
    return environment.config_folder() / "konfig.toml"


@dataclass
class Settings:
    """The non-CardDAV part of the configuration file."""

    ocr_backend: str = "auto"  # auto, rapidocr, tesseract, aus
    tesseract_path: str = ""  # explicit path to the tesseract program
    tesseract_languages: str = ""  # e.g. "deu+eng"; empty = whatever of deu/eng is installed
    gui_language: str = ""  # "de", "en" or empty = system locale
    recipient_key: str = ""  # public key of the recipient: file or https address
    recipient_fingerprint: str = ""  # pinned once the user confirmed it
    pgp_private_key: str = ""  # private-key file for capsules; empty = GnuPG keyring
    store_database: str = ""  # pseudonym database elsewhere than the data folder
    store_key: str = ""  # key file elsewhere than next to the database
    local_address_book: str = ""  # a vCard file or a folder of vCard files as address book
    mac_contacts: bool = False  # search the address books of the Mac's Contacts app
    # identifiers of the Contacts app's address books to search; empty = every one
    mac_address_books: list[str] = field(default_factory=list)
    reference_population: int = 83_600_000  # people the residual-risk estimate chooses from
    own_places: str = ""  # places of the sender that are not the patient's, comma-separated


def _read_toml(path: Path) -> dict:
    """The parsed file, or an empty mapping when there is no file yet."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(path: str | Path | None = None) -> Settings:
    """[ocr] and [gui] sections; PSEUDONYMED_OCR and PSEUDONYMED_SPRACHE override."""
    data = _read_toml(Path(path) if path else config_path())
    ocr = data.get("ocr", {})
    gui = data.get("gui", {})
    recipient = data.get("empfaenger", {})
    pgp = data.get("pgp", {})
    store = data.get("speicher", {})
    address_book = data.get("adressbuch", {})
    control = data.get("kontrolle", {})
    e = Settings(
        ocr_backend=str(ocr.get("backend", "auto")),
        tesseract_path=str(ocr.get("tesseract", "")),
        tesseract_languages=str(ocr.get("sprachen", "")),
        gui_language=str(gui.get("sprache", "")),
        recipient_key=str(recipient.get("schluessel", "")),
        recipient_fingerprint=str(recipient.get("fingerabdruck", "")),
        pgp_private_key=str(pgp.get("privater_schluessel", "")),
        store_database=str(store.get("datenbank", "")),
        store_key=str(store.get("schluessel", "")),
        local_address_book=str(address_book.get("lokal", "")),
        mac_contacts=address_book.get("mac") is True,
        mac_address_books=[str(b) for b in address_book.get("mac_adressbuecher", [])
                           if str(b).strip()],
        reference_population=int(control.get("referenzpopulation", 83_600_000) or 83_600_000),
        own_places=str(control.get("eigene_orte", "")),
    )
    e.ocr_backend = (environment.variable("OCR", e.ocr_backend) or "").strip() or "auto"
    e.tesseract_path = (environment.variable("TESSERACT", e.tesseract_path) or "").strip()
    e.gui_language = (environment.variable("SPRACHE", e.gui_language) or "").strip()
    return e


def _write(path: Path, carddav: dict, e: Settings) -> Path:
    """Write the whole file, every section in a fixed order, and keep it private (0600)
    because it may hold the app password; chmod failures (Windows) are tolerated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[carddav]"]
    for k in ("url", "benutzer", "adressbuch"):
        lines.append(f"{k} = {_toml_value(str(carddav.get(k, '')))}")
    if carddav.get("passwort"):
        lines.append(f"passwort = {_toml_value(str(carddav['passwort']))}")
    lines += ["", "[ocr]", f"backend = {_toml_value(e.ocr_backend)}",
              f"tesseract = {_toml_value(e.tesseract_path)}",
              f"sprachen = {_toml_value(e.tesseract_languages)}",
              "", "[gui]", f"sprache = {_toml_value(e.gui_language)}",
              "", "[empfaenger]", f"schluessel = {_toml_value(e.recipient_key)}",
              f"fingerabdruck = {_toml_value(e.recipient_fingerprint)}",
              "", "[pgp]", f"privater_schluessel = {_toml_value(e.pgp_private_key)}",
              "", "[speicher]", f"datenbank = {_toml_value(e.store_database)}",
              f"schluessel = {_toml_value(e.store_key)}",
              "", "[adressbuch]", f"lokal = {_toml_value(e.local_address_book)}",
              f"mac = {'true' if e.mac_contacts else 'false'}",
              "mac_adressbuecher = [" + ", ".join(map(_toml_value, e.mac_address_books)) + "]",
              "", "[kontrolle]", f"referenzpopulation = {int(e.reference_population)}",
              f"eigene_orte = {_toml_value(e.own_places)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_settings(e: Settings, path: str | Path | None = None) -> Path:
    """Write [ocr], [gui], [empfaenger] and [pgp], keeping [carddav] as it is."""
    path = Path(path) if path else config_path()
    return _write(path, _read_toml(path).get("carddav", {}), e)


def load_config(path: str | Path | None = None) -> CardDAVConfig:
    """File values, overridden by PSEUDONYMED_CARDDAV_URL/BENUTZER/ADRESSBUCH."""
    data = _read_toml(Path(path) if path else config_path()).get("carddav", {})
    k = CardDAVConfig(
        url=str(data.get("url", "")),
        user=str(data.get("benutzer", "")),
        address_book=str(data.get("adressbuch", "")),
        password_in_file=str(data.get("passwort", "")),
    )
    k.url = (environment.variable("CARDDAV_URL", k.url) or "").strip()
    k.user = (environment.variable("CARDDAV_BENUTZER", k.user) or "").strip()
    k.address_book = (environment.variable("CARDDAV_ADRESSBUCH", k.address_book) or "").strip()
    return k


def _toml_value(text: str) -> str:
    """``text`` as a quoted TOML basic string (backslash and quote escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(k: CardDAVConfig, path: str | Path | None = None) -> Path:
    """Write the [carddav] section, keeping the other sections as they are."""
    path = Path(path) if path else config_path()
    e = load_settings(path) if path.exists() else Settings()
    return _write(path, {"url": k.url, "benutzer": k.user, "adressbuch": k.address_book,
                         "passwort": k.password_in_file}, e)


def load_password(k: CardDAVConfig) -> str | None:
    """The app password: environment variable, else the keyring (under the current and
    then the former service name), else the one kept in the file, else None."""
    from_environment = environment.variable("CARDDAV_PASSWORT")
    if from_environment:
        return from_environment
    try:
        import keyring

        stored = (keyring.get_password(SERVICE, k.code)
                  or keyring.get_password(FORMER_SERVICE, k.code))
    except Exception:  # no keyring, or a backend raising its own error type
        stored = None
    return stored or (k.password_in_file or None)


def keyring_backend() -> str:
    """Name of the usable keyring backend, or "" when there is none."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as NoKeyring

        active = keyring.get_keyring()
    except Exception:
        return ""
    if isinstance(active, NoKeyring):
        return ""
    return type(active).__module__.split(".")[-1] + "." + type(active).__name__


def save_password(k: CardDAVConfig, password: str) -> bool:
    """Store in the keyring; False when no keyring is available."""
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.set_password(SERVICE, k.code, password)
    except Exception:
        return False
    return True
