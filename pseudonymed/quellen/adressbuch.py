"""Address books: a local vCard file or folder and the CardDAV server, searched together."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pseudonymed.kern.modell import Patient
from pseudonymed.konfig import CardDAVKonfig, lade_einstellungen, lade_konfig, passwort_laden
from pseudonymed.quellen.carddav import CardDAVClient, CardDAVFehler, passt
from pseudonymed.quellen.vcf import patienten_aus_vcf

VCF_ENDUNGEN = (".vcf", ".vcard")


@dataclass
class Suchergebnis:
    """What a search over every configured address book returns: the patients found, the sources
    that failed, the sources searched."""
    patienten: list[Patient] = field(default_factory=list)
    fehler: list[str] = field(default_factory=list)  # sources that failed, as messages
    quellen: list[str] = field(default_factory=list)  # sources that were searched


def lokale_dateien(pfad: str | Path) -> list[Path]:
    """The vCard files behind ``pfad``: the file itself, or the .vcf files of a folder."""
    p = Path(pfad).expanduser()
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(q for q in p.iterdir() if q.is_file() and q.suffix.lower() in VCF_ENDUNGEN)
    return []


def suche_lokal(pfad: str | Path, begriff: str) -> list[Patient]:
    """Cards of a vCard file or of every vCard file in a folder that match ``begriff``
    (empty: all)."""
    treffer: list[Patient] = []
    for datei in lokale_dateien(pfad):
        for p in patienten_aus_vcf(datei):
            if (p.nachname or p.vorname) and (not begriff or passt(p, begriff)):
                treffer.append(p)
    return treffer


def eingerichtet(konfig_pfad: str | Path | None = None) -> bool:
    """True when a local address book or a CardDAV account is configured."""
    return bool(lade_einstellungen(konfig_pfad).adressbuch_lokal) or \
        lade_konfig(konfig_pfad).vollstaendig


def suche(begriff: str, konfig_pfad: str | Path | None = None,
          carddav: CardDAVKonfig | None = None) -> Suchergebnis:
    """Local book first, then the CardDAV server; a failing source becomes a message
    instead of an exception, so the other one still answers."""
    ergebnis = Suchergebnis()
    lokal = lade_einstellungen(konfig_pfad).adressbuch_lokal
    if lokal:
        ergebnis.quellen.append(f"lokal ({lokal})")
        try:
            ergebnis.patienten += suche_lokal(lokal, begriff)
        except (OSError, ValueError) as e:
            ergebnis.fehler.append(f"Lokales Adressbuch {lokal}: {e}")
    k = carddav or lade_konfig(konfig_pfad)
    if k.vollstaendig:
        ergebnis.quellen.append(f"CardDAV ({k.kennung})")
        passwort = passwort_laden(k)
        if not passwort:
            ergebnis.fehler.append(f"Kein App-Passwort für {k.kennung} (Schlüsselbund,"
                                   " Konfiguration oder PSEUDONYMED_CARDDAV_PASSWORT).")
        else:
            try:
                client = CardDAVClient(k.url, k.benutzer, passwort)
                kontakte = client.suche(client.adressbuch(k.adressbuch), begriff)
                bekannt = {p.schluessel() for p in ergebnis.patienten}
                ergebnis.patienten += [kk.patient for kk in kontakte
                                       if kk.patient.schluessel() not in bekannt]
            except CardDAVFehler as e:
                ergebnis.fehler.append(f"CardDAV: {e}")
    return ergebnis
