"""User configuration (TOML in the config folder) and the CardDAV app password.

The password comes from the environment variable ``PSEUDONYMED_CARDDAV_PASSWORD``, from
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
PASSWORD_ENV_VAR = "PSEUDONYMED_CARDDAV_PASSWORD"


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
    """Configuration file in the user config folder; PSEUDONYMED_DATA_FOLDER overrides the
    folder."""
    return environment.config_folder() / "config.toml"


@dataclass
class Settings:
    """The non-CardDAV part of the configuration file."""

    ocr_backend: str = "auto"  # auto, rapidocr, tesseract, off
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


# German names of sections and keys, understood when the file was written by hand in German
_GERMAN_SECTIONS = {"empfaenger": "recipient", "speicher": "store", "adressbuch": "address_book",
                    "kontrolle": "control"}
_GERMAN_KEYS = {"benutzer": "user", "adressbuch": "address_book", "passwort": "password",
                "sprachen": "languages", "sprache": "language", "schluessel": "key",
                "fingerabdruck": "fingerprint", "privater_schluessel": "private_key",
                "datenbank": "database", "lokal": "local", "mac_adressbuecher": "mac_address_books",
                "referenzpopulation": "reference_population", "eigene_orte": "own_places"}


def _read_toml(path: Path) -> dict[str, dict]:
    """The parsed file with English section and key names (German ones translated), or an
    empty mapping when there is no file yet."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    return {_GERMAN_SECTIONS.get(name, name):
            {_GERMAN_KEYS.get(k, k): v for k, v in section.items()}
            for name, section in data.items() if isinstance(section, dict)}


def load_settings(path: str | Path | None = None) -> Settings:
    """Everything but [carddav]; PSEUDONYMED_OCR, PSEUDONYMED_TESSERACT and
    PSEUDONYMED_LANGUAGE override."""
    data = _read_toml(Path(path) if path else config_path())
    ocr = data.get("ocr", {})
    gui = data.get("gui", {})
    recipient = data.get("recipient", {})
    pgp = data.get("pgp", {})
    store = data.get("store", {})
    address_book = data.get("address_book", {})
    control = data.get("control", {})
    e = Settings(
        ocr_backend=str(ocr.get("backend", "auto")),
        tesseract_path=str(ocr.get("tesseract", "")),
        tesseract_languages=str(ocr.get("languages", "")),
        gui_language=str(gui.get("language", "")),
        recipient_key=str(recipient.get("key", "")),
        recipient_fingerprint=str(recipient.get("fingerprint", "")),
        pgp_private_key=str(pgp.get("private_key", "")),
        store_database=str(store.get("database", "")),
        store_key=str(store.get("key", "")),
        local_address_book=str(address_book.get("local", "")),
        mac_contacts=address_book.get("mac") is True,
        mac_address_books=[str(b) for b in address_book.get("mac_address_books", [])
                           if str(b).strip()],
        reference_population=int(control.get("reference_population", 83_600_000) or 83_600_000),
        own_places=str(control.get("own_places", "")),
    )
    e.ocr_backend = (environment.variable("OCR", e.ocr_backend) or "").strip() or "auto"
    e.tesseract_path = (environment.variable("TESSERACT", e.tesseract_path) or "").strip()
    e.gui_language = (environment.variable("LANGUAGE", e.gui_language) or "").strip()
    return e


def _write(path: Path, carddav: dict, e: Settings) -> Path:
    """Write the whole file in English, every section in a fixed order, and keep it private
    (0600) because it may hold the app password; chmod failures (Windows) are tolerated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[carddav]"]
    for k in ("url", "user", "address_book"):
        lines.append(f"{k} = {_toml_value(str(carddav.get(k, '')))}")
    if carddav.get("password"):
        lines.append(f"password = {_toml_value(str(carddav['password']))}")
    lines += ["", "[ocr]", f"backend = {_toml_value(e.ocr_backend)}",
              f"tesseract = {_toml_value(e.tesseract_path)}",
              f"languages = {_toml_value(e.tesseract_languages)}",
              "", "[gui]", f"language = {_toml_value(e.gui_language)}",
              "", "[recipient]", f"key = {_toml_value(e.recipient_key)}",
              f"fingerprint = {_toml_value(e.recipient_fingerprint)}",
              "", "[pgp]", f"private_key = {_toml_value(e.pgp_private_key)}",
              "", "[store]", f"database = {_toml_value(e.store_database)}",
              f"key = {_toml_value(e.store_key)}",
              "", "[address_book]", f"local = {_toml_value(e.local_address_book)}",
              f"mac = {'true' if e.mac_contacts else 'false'}",
              "mac_address_books = [" + ", ".join(map(_toml_value, e.mac_address_books)) + "]",
              "", "[control]", f"reference_population = {int(e.reference_population)}",
              f"own_places = {_toml_value(e.own_places)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_settings(e: Settings, path: str | Path | None = None) -> Path:
    """Write every section but [carddav], keeping [carddav] as it is."""
    path = Path(path) if path else config_path()
    return _write(path, _read_toml(path).get("carddav", {}), e)


def load_config(path: str | Path | None = None) -> CardDAVConfig:
    """File values, overridden by PSEUDONYMED_CARDDAV_URL/USER/ADDRESS_BOOK."""
    data = _read_toml(Path(path) if path else config_path()).get("carddav", {})
    k = CardDAVConfig(
        url=str(data.get("url", "")),
        user=str(data.get("user", "")),
        address_book=str(data.get("address_book", "")),
        password_in_file=str(data.get("password", "")),
    )
    k.url = (environment.variable("CARDDAV_URL", k.url) or "").strip()
    k.user = (environment.variable("CARDDAV_USER", k.user) or "").strip()
    k.address_book = (environment.variable("CARDDAV_ADDRESS_BOOK", k.address_book) or "").strip()
    return k


def _toml_value(text: str) -> str:
    """``text`` as a quoted TOML basic string (backslash and quote escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(k: CardDAVConfig, path: str | Path | None = None) -> Path:
    """Write the [carddav] section, keeping the other sections as they are."""
    path = Path(path) if path else config_path()
    e = load_settings(path) if path.exists() else Settings()
    return _write(path, {"url": k.url, "user": k.user, "address_book": k.address_book,
                         "password": k.password_in_file}, e)


def load_password(k: CardDAVConfig) -> str | None:
    """The app password: environment variable, else the keyring, else the one kept in the
    file, else None."""
    from_environment = environment.variable("CARDDAV_PASSWORD")
    if from_environment:
        return from_environment
    try:
        import keyring

        stored = keyring.get_password(SERVICE, k.code)
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
