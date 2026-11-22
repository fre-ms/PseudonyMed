"""Address books: a local vCard file or folder and the CardDAV server, searched together."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pseudonymed.config import CardDAVConfig, load_config, load_password, load_settings
from pseudonymed.core.model import Patient
from pseudonymed.core.text import error_text, say
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError, fits
from pseudonymed.sources.vcf import patients_from_vcf

VCF_SUFFIXES = (".vcf", ".vcard")


@dataclass
class SearchResult:
    """What a search over every configured address book returns: the patients found, the sources
    that failed, the sources searched."""
    patients: list[Patient] = field(default_factory=list)
    error: list[str] = field(default_factory=list)  # sources that failed, as messages
    sources: list[str] = field(default_factory=list)  # sources that were searched


def local_files(path: str | Path) -> list[Path]:
    """The vCard files behind ``path``: the file itself, or the .vcf files of a folder."""
    p = Path(path).expanduser()
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() in VCF_SUFFIXES)
    return []


def search_local(path: str | Path, term: str) -> list[Patient]:
    """Cards of a vCard file or of every vCard file in a folder that match ``term``
    (empty: all)."""
    matches: list[Patient] = []
    for file in local_files(path):
        for p in patients_from_vcf(file):
            if (p.surname or p.first_name) and (not term or fits(p, term)):
                matches.append(p)
    return matches


def is_set_up(config_file: str | Path | None = None) -> bool:
    """True when a local address book or a CardDAV account is configured."""
    return bool(load_settings(config_file).local_address_book) or \
        load_config(config_file).complete


def search(term: str, config_file: str | Path | None = None,
           carddav: CardDAVConfig | None = None) -> SearchResult:
    """Local book first, then the CardDAV server; a failing source becomes a message
    instead of an exception, so the other one still answers."""
    result = SearchResult()
    local = load_settings(config_file).local_address_book
    if local:
        result.sources.append(f"lokal ({local})")
        try:
            result.patients += search_local(local, term)
        except (OSError, ValueError) as e:
            result.error.append(say(f"Lokales Adressbuch {local}: {e}",
                                    f"Local address book {local}: {error_text(e, 'en')}"))
    k = carddav or load_config(config_file)
    if k.complete:
        result.sources.append(f"CardDAV ({k.code})")
        password = load_password(k)
        if not password:
            result.error.append(say(f"Kein App-Passwort für {k.code} (Schlüsselbund,"
                                    " Konfiguration oder PSEUDONYMED_CARDDAV_PASSWORT).",
                                    f"No app password for {k.code} (keyring, configuration or"
                                    " PSEUDONYMED_CARDDAV_PASSWORT)."))
        else:
            try:
                client = CardDAVClient(k.url, k.user, password)
                contacts = client.search(client.address_book(k.address_book), term)
                known = {p.key() for p in result.patients}
                result.patients += [kk.patient for kk in contacts
                                    if kk.patient.key() not in known]
            except CardDAVError as e:
                result.error.append(say(f"CardDAV: {e}", f"CardDAV: {error_text(e, 'en')}"))
    return result
