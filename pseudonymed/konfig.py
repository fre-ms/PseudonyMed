"""User configuration (TOML in the config folder) and the CardDAV app password.

The password comes from the environment variable ``PSEUDONYMED_CARDDAV_PASSWORT``, from
the operating system's keyring (extra ``carddav``) or, only when the user chose so
because no keyring is available, from the configuration file itself (mode 0600).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pseudonymed import umgebung

DIENST = "PseudonyMed CardDAV"
FRUEHERER_DIENST = "AnonyMed CardDAV"  # keyring entries made before the renaming
UMGEBUNG_PASSWORT = "PSEUDONYMED_CARDDAV_PASSWORT"


@dataclass
class CardDAVKonfig:
    """The ``[carddav]`` section: server, user and address book; the password lives in
    the keyring unless the user put it into the file."""

    url: str = ""  # https://cloud.example.org
    benutzer: str = ""
    adressbuch: str = ""  # URI segment or display name, empty = the only/first one
    passwort_datei: str = ""  # app password kept in the file: fallback without a keyring

    @property
    def vollstaendig(self) -> bool:
        """True when server and user are set, the minimum for a connection."""
        return bool(self.url and self.benutzer)

    @property
    def kennung(self) -> str:
        """Keyring account name: user@host."""
        host = urlsplit(self.url).hostname or self.url
        return f"{self.benutzer}@{host}"


def konfigpfad() -> Path:
    """Configuration file in the user config folder; PSEUDONYMED_DATENORDNER overrides the
    folder."""
    return umgebung.konfigordner() / "konfig.toml"


@dataclass
class Einstellungen:
    """The non-CardDAV part of the configuration file."""

    ocr_backend: str = "auto"  # auto, rapidocr, tesseract, aus
    tesseract_pfad: str = ""  # explicit path to the tesseract program
    tesseract_sprachen: str = ""  # e.g. "deu+eng"; empty = whatever of deu/eng is installed
    gui_sprache: str = ""  # "de", "en" or empty = system locale
    empfaenger_schluessel: str = ""  # public key of the recipient: file or https address
    empfaenger_fingerabdruck: str = ""  # pinned once the user confirmed it
    pgp_privater_schluessel: str = ""  # private-key file for capsules; empty = GnuPG keyring
    speicher_datenbank: str = ""  # pseudonym database elsewhere than the data folder
    speicher_schluessel: str = ""  # key file elsewhere than next to the database
    adressbuch_lokal: str = ""  # a vCard file or a folder of vCard files as address book
    referenzpopulation: int = 83_600_000  # people the residual-risk estimate chooses from
    eigene_orte: str = ""  # places of the sender that are not the patient's, comma-separated


def _lese_toml(pfad: Path) -> dict:
    """The parsed file, or an empty mapping when there is no file yet."""
    if not pfad.exists():
        return {}
    with pfad.open("rb") as f:
        return tomllib.load(f)


def lade_einstellungen(pfad: str | Path | None = None) -> Einstellungen:
    """[ocr] and [gui] sections; PSEUDONYMED_OCR and PSEUDONYMED_SPRACHE override."""
    daten = _lese_toml(Path(pfad) if pfad else konfigpfad())
    ocr = daten.get("ocr", {})
    gui = daten.get("gui", {})
    empfaenger = daten.get("empfaenger", {})
    pgp = daten.get("pgp", {})
    speicher = daten.get("speicher", {})
    adressbuch = daten.get("adressbuch", {})
    kontrolle = daten.get("kontrolle", {})
    e = Einstellungen(
        ocr_backend=str(ocr.get("backend", "auto")),
        tesseract_pfad=str(ocr.get("tesseract", "")),
        tesseract_sprachen=str(ocr.get("sprachen", "")),
        gui_sprache=str(gui.get("sprache", "")),
        empfaenger_schluessel=str(empfaenger.get("schluessel", "")),
        empfaenger_fingerabdruck=str(empfaenger.get("fingerabdruck", "")),
        pgp_privater_schluessel=str(pgp.get("privater_schluessel", "")),
        speicher_datenbank=str(speicher.get("datenbank", "")),
        speicher_schluessel=str(speicher.get("schluessel", "")),
        adressbuch_lokal=str(adressbuch.get("lokal", "")),
        referenzpopulation=int(kontrolle.get("referenzpopulation", 83_600_000) or 83_600_000),
        eigene_orte=str(kontrolle.get("eigene_orte", "")),
    )
    e.ocr_backend = (umgebung.variable("OCR", e.ocr_backend) or "").strip() or "auto"
    e.tesseract_pfad = (umgebung.variable("TESSERACT", e.tesseract_pfad) or "").strip()
    e.gui_sprache = (umgebung.variable("SPRACHE", e.gui_sprache) or "").strip()
    return e


def _schreibe(pfad: Path, carddav: dict, e: Einstellungen) -> Path:
    """Write the whole file, every section in a fixed order, and keep it private (0600)
    because it may hold the app password; chmod failures (Windows) are tolerated."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    zeilen = ["[carddav]"]
    for k in ("url", "benutzer", "adressbuch"):
        zeilen.append(f"{k} = {_toml_wert(str(carddav.get(k, '')))}")
    if carddav.get("passwort"):
        zeilen.append(f"passwort = {_toml_wert(str(carddav['passwort']))}")
    zeilen += ["", "[ocr]", f"backend = {_toml_wert(e.ocr_backend)}",
               f"tesseract = {_toml_wert(e.tesseract_pfad)}",
               f"sprachen = {_toml_wert(e.tesseract_sprachen)}",
               "", "[gui]", f"sprache = {_toml_wert(e.gui_sprache)}",
               "", "[empfaenger]", f"schluessel = {_toml_wert(e.empfaenger_schluessel)}",
               f"fingerabdruck = {_toml_wert(e.empfaenger_fingerabdruck)}",
               "", "[pgp]", f"privater_schluessel = {_toml_wert(e.pgp_privater_schluessel)}",
               "", "[speicher]", f"datenbank = {_toml_wert(e.speicher_datenbank)}",
               f"schluessel = {_toml_wert(e.speicher_schluessel)}",
               "", "[adressbuch]", f"lokal = {_toml_wert(e.adressbuch_lokal)}",
               "", "[kontrolle]", f"referenzpopulation = {int(e.referenzpopulation)}",
               f"eigene_orte = {_toml_wert(e.eigene_orte)}"]
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    try:
        os.chmod(pfad, 0o600)
    except OSError:
        pass
    return pfad


def speichere_einstellungen(e: Einstellungen, pfad: str | Path | None = None) -> Path:
    """Write [ocr], [gui], [empfaenger] and [pgp], keeping [carddav] as it is."""
    pfad = Path(pfad) if pfad else konfigpfad()
    return _schreibe(pfad, _lese_toml(pfad).get("carddav", {}), e)


def lade_konfig(pfad: str | Path | None = None) -> CardDAVKonfig:
    """File values, overridden by PSEUDONYMED_CARDDAV_URL/BENUTZER/ADRESSBUCH."""
    daten = _lese_toml(Path(pfad) if pfad else konfigpfad()).get("carddav", {})
    k = CardDAVKonfig(
        url=str(daten.get("url", "")),
        benutzer=str(daten.get("benutzer", "")),
        adressbuch=str(daten.get("adressbuch", "")),
        passwort_datei=str(daten.get("passwort", "")),
    )
    k.url = (umgebung.variable("CARDDAV_URL", k.url) or "").strip()
    k.benutzer = (umgebung.variable("CARDDAV_BENUTZER", k.benutzer) or "").strip()
    k.adressbuch = (umgebung.variable("CARDDAV_ADRESSBUCH", k.adressbuch) or "").strip()
    return k


def _toml_wert(text: str) -> str:
    """``text`` as a quoted TOML basic string (backslash and quote escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def speichere_konfig(k: CardDAVKonfig, pfad: str | Path | None = None) -> Path:
    """Write the [carddav] section, keeping the other sections as they are."""
    pfad = Path(pfad) if pfad else konfigpfad()
    e = lade_einstellungen(pfad) if pfad.exists() else Einstellungen()
    return _schreibe(pfad, {"url": k.url, "benutzer": k.benutzer, "adressbuch": k.adressbuch,
                            "passwort": k.passwort_datei}, e)


def passwort_laden(k: CardDAVKonfig) -> str | None:
    """The app password: environment variable, else the keyring (under the current and
    then the former service name), else the one kept in the file, else None."""
    aus_umgebung = umgebung.variable("CARDDAV_PASSWORT")
    if aus_umgebung:
        return aus_umgebung
    try:
        import keyring

        gespeichert = (keyring.get_password(DIENST, k.kennung)
                       or keyring.get_password(FRUEHERER_DIENST, k.kennung))
    except Exception:  # no keyring, or a backend raising its own error type
        gespeichert = None
    return gespeichert or (k.passwort_datei or None)


def schluesselbund() -> str:
    """Name of the usable keyring backend, or "" when there is none."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as Keiner

        aktiv = keyring.get_keyring()
    except Exception:
        return ""
    if isinstance(aktiv, Keiner):
        return ""
    return type(aktiv).__module__.split(".")[-1] + "." + type(aktiv).__name__


def passwort_speichern(k: CardDAVKonfig, passwort: str) -> bool:
    """Store in the keyring; False when no keyring is available."""
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.set_password(DIENST, k.kennung, passwort)
    except Exception:
        return False
    return True
