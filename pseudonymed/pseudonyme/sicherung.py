"""Backup, restore and statistics of everything re-identification depends on.

The pseudonym database (``pseudonyme.sqlite``) holds the identities and the mapping
of every pseudonym back to its clear text; the key file (``pseudonymschluessel.txt``)
is the secret the codes are derived from.  Without both, a pseudonymised text cannot
be read in clear again, and with both anyone can.  A backup is therefore a ZIP of the
two (a consistent copy of the database through SQLite's backup API, never a raw copy
of an open file), optionally the configuration and the user's vocabulary lists, plus
a manifest with checksums, and it is protected by a passphrase (OpenPGP symmetric
encryption, readable by ``gpg --decrypt`` as well) unless the user insists on plain.

Restore replaces the database and the key after setting aside the current ones, or
merges an archive made with the *same* key into the local database (same key, same
codes for the same people); merging two different pseudonym spaces is refused.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pseudonymed import WERKZEUG
from pseudonymed.erkennung.fachbegriffe import datenordner as vokabularordner
from pseudonymed.konfig import konfigpfad
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme.speicher import schluesselpfad_zu, standard_schluesselpfad, standardpfad

VERSION = 1
NAME_DB = "pseudonyme.sqlite"
NAME_SCHLUESSEL = "pseudonymschluessel.txt"
NAME_KONFIG = "konfig.toml"
ORDNER_VOKABULAR = "fachbegriffe"
NAME_MANIFEST = "manifest.json"
_MARKE = "pseudonymed_sicherung"
_FRUEHERE_MARKE = "anonymed_sicherung"  # backups written before the project was renamed
MODI = ("ersetzen", "zusammenfuehren")

KATEGORIEN = [  # pseudonym prefix -> label (first match wins)
    ("Patientin ", "Patient"), ("Patient ", "Patient"), ("Angehörige", "Angehörige"),
    ("Dr. Z", "Zuweiser"), ("[geb. ", "Geburtsdatum"), ("[Alter ", "Geburtsdatum"),
    ("[Straße ", "Straße"), ("[PLZ Ort ", "PLZ und Ort"), ("[Ort ", "Ort"),
    ("[Wohnort]", "Ort"), ("Person ", "Person (NER)"), ("[Telefon", "Telefon"),
    ("[E-Mail", "E-Mail"), ("[Versicherten", "Versichertennummer"), ("[Pat", "Patienten-ID"),
    ("[Aktenzeichen", "Aktenzeichen"), ("[IBAN", "IBAN"), ("[Code]", "Barcode/QR"),
    ("[Beruf", "Beruf"), ("[Klinik", "Klinik"), ("[Studie", "Studie"), ("[Titel", "Titel"),
    ("[Identifikator", "Identifikator"), ("[Medikament", "Medikament"),
    ("[Dosierung", "Dosierung"), ("[Laborwert", "Laborwert"), ("[Messwert", "Messwert"),
]
_ART_LABELS = {
    "name": "Name", "vorname": "Name", "nachname": "Name", "geburtsname": "Geburtsname",
    "geburtsdatum": "Geburtsdatum", "adresse": "Adresse", "ort": "Ort", "telefon": "Telefon",
    "email": "E-Mail", "versichertennummer": "Versichertennummer",
    "patienten_id": "Patienten-ID", "aktenzeichen": "Aktenzeichen", "iban": "IBAN",
    "begriff": "Begriff", "manuell": "Manuell", "identifikator": "Identifikator",
    "medizin": "Medizinischer Inhalt",
}
_TABELLEN = ("patienten", "aliasse", "zuordnungen", "verworfen", "vokabular",  # merged in
             "restrisiko", "restrisiko_dokumente")  # this order; older archives may lack some


class SicherungFehler(Exception):
    """Anything that stops a backup, restore or relocation; the message is for the user."""


def kategorie(pseudonym: str, art: str = "") -> str:
    """Human label for the statistics: by the pseudonym's shape, else by its kind."""
    for anfang, label in KATEGORIEN:
        if pseudonym.startswith(anfang):
            return label
    return _ART_LABELS.get(art.lower(), art or "Sonstige")


def schluessel_kennung(geheimnis: bytes) -> str:
    """A public handle for a secret key: 16 hex digits of its SHA-256, never the key."""
    return hashlib.sha256(geheimnis).hexdigest()[:16]


# -- statistics ------------------------------------------------------------------------------------
@dataclass
class Statistik:
    """What ``datenbank status`` and the database page show about the local pseudonym store."""
    pfad: Path
    existiert: bool = False
    groesse: int = 0
    schema: str = ""
    schluessel_pfad: Path | None = None
    schluessel_vorhanden: bool = False
    schluessel_kennung: str = ""
    patienten: int = 0
    aliasse: int = 0
    zuordnungen: int = 0
    verworfen: int = 0
    je_kategorie: list[tuple[str, int]] = field(default_factory=list)
    verworfen_je_regel: list[tuple[str, int]] = field(default_factory=list)
    erster_eintrag: str = ""
    letzter_eintrag: str = ""
    integritaet: str = ""
    seiten: int = 0
    freie_seiten: int = 0
    vokabular_dateien: int = 0
    vokabular_begriffe: int = 0
    vokabular_kategorien: list[tuple[str, int]] = field(default_factory=list)
    lexikon_klassen: list[tuple[str, str, int]] = field(default_factory=list)
    lexikon_zusatz: list[tuple[str, int, str]] = field(default_factory=list)
    konfig_vorhanden: bool = False

    def zeilen(self) -> list[tuple[str, str]]:
        """Label/value pairs for display (CLI and desktop)."""
        return (self._kopfzeilen()
                + _eingerueckt(self.je_kategorie)
                + [("Gemerkte Verwerfungen", str(self.verworfen))]
                + _eingerueckt(self.verworfen_je_regel)
                + self._zustandszeilen()
                + _eingerueckt(self.vokabular_kategorien)
                + self._lexikonzeilen()
                + [("Konfiguration", "vorhanden" if self.konfig_vorhanden else "keine")])

    def _kopfzeilen(self) -> list[tuple[str, str]]:
        """Where database and key live, and the headline counts."""
        return [
            ("Datenbank", str(self.pfad)),
            ("Vorhanden", "ja" if self.existiert else "nein (noch nicht angelegt)"),
            ("Größe", _mb(self.groesse)),
            ("Schema", self.schema or "–"),
            ("Schlüsseldatei", str(self.schluessel_pfad or "–")),
            ("Schlüssel vorhanden", "ja" if self.schluessel_vorhanden else "nein"),
            ("Schlüssel-Kennung", self.schluessel_kennung or "–"),
            ("Patienten", str(self.patienten)),
            ("Verknüpfte Schreibweisen", str(self.aliasse)),
            ("Zuordnungen gesamt", str(self.zuordnungen)),
        ]

    def _zustandszeilen(self) -> list[tuple[str, str]]:
        """Time span of the entries, integrity, page usage and the vocabulary total."""
        return [
            ("Erster Eintrag", self.erster_eintrag or "–"),
            ("Letzter Eintrag", self.letzter_eintrag or "–"),
            ("Integrität", self.integritaet or "–"),
            ("Seiten / frei", f"{self.seiten} / {self.freie_seiten}"),
            ("Vokabular (Begriffe)", str(self.vokabular_begriffe)),
        ]

    def _lexikonzeilen(self) -> list[tuple[str, str]]:
        """The bundled lexicon per class (only when it is loaded) and the data sets the
        user fetched themselves."""
        aus: list[tuple[str, str]] = []
        if self.lexikon_klassen:
            aus.append(("Lexikon (Einträge)", str(sum(n for _k, _r, n in self.lexikon_klassen))))
            aus += [(f"    {k} ({'Identifikator' if r == 'merkmal' else 'Fachklasse'})", str(n))
                    for k, r, n in self.lexikon_klassen]
        aus += [("Zusatzdaten (selbst geladen)", f"{q}: {n} Einträge, {g[:10]}")
                for q, n, g in self.lexikon_zusatz]
        return aus


def _eingerueckt(paare: Iterable[tuple[str, int]]) -> list[tuple[str, str]]:
    """Sub-rows of a count, indented under their total."""
    return [(f"    {k}", str(n)) for k, n in paare]


def _mb(n: int) -> str:
    """A byte count as B, KB or MB with one decimal."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _pfade(speicherpfad: Path | None) -> tuple[Path, Path]:
    """Database and key file: the configured ones for the default database, the key
    next to the database when a database path was given explicitly."""
    if speicherpfad:
        db = Path(speicherpfad)
        return db, schluesselpfad_zu(db)
    return standardpfad(), standard_schluesselpfad()


def _zeitstempel() -> str:
    """The stamp that names files set aside (``*.verlegt-<stamp>``, ``*.vor-import-<stamp>``)."""
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _schuetze(*pfade: Path) -> None:
    """Restrict the files to their owner (0600); file systems without POSIX modes are tolerated."""
    try:
        for p in pfade:
            os.chmod(p, 0o600)
    except OSError:
        pass


def _kopiere_datenbank(quelle: Path, ziel: Path) -> None:
    """A consistent copy of a SQLite database through the backup API, which reads the
    source inside a transaction; a plain file copy of an open database could be torn."""
    von = sqlite3.connect(f"file:{quelle}?mode=ro", uri=True)
    nach = sqlite3.connect(ziel)
    try:
        von.backup(nach)
    finally:
        nach.close()
        von.close()


def _lege_beiseite(pfad: Path, stempel: str) -> Path:
    """Rename a file to ``<name>.verlegt-<stamp>`` and return the new path."""
    beiseite = pfad.with_name(f"{pfad.name}.verlegt-{stempel}")
    pfad.rename(beiseite)
    return beiseite


@dataclass
class Verlegung:
    """Result of ``verlege``: the new paths, whether files were copied and which were set aside."""
    datenbank: Path
    schluessel: Path
    kopiert: bool = False
    umbenannt: list[Path] = field(default_factory=list)

    def beschreibung(self) -> str:
        """One sentence for the user about the new location and what moved."""
        text = f"Speicherort: Datenbank {self.datenbank}, Schlüssel {self.schluessel}."
        if self.kopiert:
            text += " Vorhandene Dateien wurden dorthin kopiert"
            if self.umbenannt:
                text += "; die alten heißen jetzt " + ", ".join(p.name for p in self.umbenannt)
            text += "."
        return text


def verlege(datenbank: Path, schluessel: Path, kopieren: bool = True) -> Verlegung:
    """Store database and key at the given paths from now on (``[speicher]`` in the
    configuration).  With ``kopieren`` the current files are copied there first (the
    database through the backup API) and the originals renamed ``*.verlegt-<stamp>``,
    so that exactly one database stays live."""
    from pseudonymed.konfig import lade_einstellungen, speichere_einstellungen

    datenbank, schluessel = Path(datenbank).expanduser(), Path(schluessel).expanduser()
    if not datenbank.is_absolute() or not schluessel.is_absolute():
        raise SicherungFehler("Bitte absolute Pfade angeben.")
    if datenbank == schluessel:
        raise SicherungFehler("Datenbank und Schlüsseldatei brauchen verschiedene Dateinamen.")
    alt_db, alt_schluessel = _pfade(None)
    v = Verlegung(datenbank=datenbank, schluessel=schluessel)
    if kopieren and (alt_db != datenbank or alt_schluessel != schluessel):
        v.umbenannt = _ziehe_um(alt_db, alt_schluessel, datenbank, schluessel)
        v.kopiert = bool(v.umbenannt)
    e = lade_einstellungen()
    e.speicher_datenbank, e.speicher_schluessel = str(datenbank), str(schluessel)
    speichere_einstellungen(e)
    return v


def _ziehe_um(alt_db: Path, alt_schluessel: Path, datenbank: Path, schluessel: Path) -> list[Path]:
    """Copy the live database and key to their new paths and set the old files aside; returns
    the renamed originals.  The database goes first, so a refused target leaves the key put."""
    stempel = _zeitstempel()
    verlegt = [_verlege_datenbank(alt_db, datenbank, stempel),
               _verlege_schluessel(alt_schluessel, schluessel, stempel)]
    for p in (datenbank, schluessel):
        if p.exists():
            _schuetze(p)
    return [p for p in verlegt if p]


def _verlege_datenbank(alt: Path, neu: Path, stempel: str) -> Path | None:
    """Copy the live database to ``neu`` through the backup API and rename the old file; None
    when there is nothing to move.  A filled target is never overwritten: that is the import's
    job."""
    if not alt.exists() or alt == neu:
        return None
    if neu.exists() and not _leer(neu):
        raise SicherungFehler(f"Am Ziel gibt es schon eine Datenbank: {neu}."
                              " Anderen Pfad wählen oder sie über den Import ersetzen.")
    neu.parent.mkdir(parents=True, exist_ok=True)
    _kopiere_datenbank(alt, neu)
    return _lege_beiseite(alt, stempel)


def _verlege_schluessel(alt: Path, neu: Path, stempel: str) -> Path | None:
    """Copy the key file to ``neu`` and rename the old one; None when there is nothing to move.
    A different key at the target is refused, the same key (whitespace aside) is overwritten."""
    if not alt.exists() or alt == neu:
        return None
    if neu.exists() and neu.read_bytes().strip() != alt.read_bytes().strip():
        raise SicherungFehler(f"Am Ziel gibt es schon einen anderen Schlüssel: {neu}.")
    neu.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(alt, neu)
    return _lege_beiseite(alt, stempel)


def statistik(speicherpfad: Path | None = None) -> Statistik:
    """Describe the pseudonym store at ``speicherpfad`` (default: the configured one)
    without changing it; a database that cannot be read reports the error as integrity."""
    db, schluessel = _pfade(speicherpfad)
    st = Statistik(pfad=db, schluessel_pfad=schluessel, existiert=db.exists(),
                   konfig_vorhanden=konfigpfad().exists())
    if schluessel.exists():
        st.schluessel_vorhanden = True
        st.schluessel_kennung = _kennung_der_schluesseldatei(schluessel)
    st.vokabular_dateien = len(_vokabulardateien(vokabularordner()))
    if not db.exists():
        return st
    st.groesse = db.stat().st_size
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        _zaehle_inhalt(v, st)
    except sqlite3.DatabaseError as e:
        st.integritaet = f"Fehler: {e}"
    finally:
        v.close()
    return st


def _kennung_der_schluesseldatei(schluessel: Path) -> str:
    """The public handle of the key in a key file, or ``unlesbar`` when it is not hex digits."""
    try:
        return schluessel_kennung(bytes.fromhex(schluessel.read_text(encoding="utf-8").strip()))
    except ValueError:
        return "unlesbar"


def _vokabulardateien(ordner: Path) -> list[Path]:
    """Vocabulary list files (``.txt``, ``.gz``) of a folder, sorted; none without the folder."""
    if not ordner.is_dir():
        return []
    return sorted(p for p in ordner.iterdir() if p.is_file() and p.suffix in (".txt", ".gz"))


def _eins(v: sqlite3.Connection, sql: str, sonst: Any = 0) -> Any:
    """One value; a table that does not exist yet counts as empty."""
    try:
        return v.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return sonst


def _alle(v: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    """All rows; a table that does not exist yet has none."""
    try:
        return list(v.execute(sql))
    except sqlite3.OperationalError:
        return []


def _zaehle_inhalt(v: sqlite3.Connection, st: Statistik) -> None:
    """Fill counts, categories, time span, integrity and lexicon figures from an open connection."""
    st.patienten = _eins(v, "SELECT COUNT(*) FROM patienten")
    st.aliasse = _eins(v, "SELECT COUNT(*) FROM aliasse")
    st.zuordnungen = _eins(v, "SELECT COUNT(*) FROM zuordnungen")
    st.verworfen = _eins(v, "SELECT COUNT(*) FROM verworfen")
    st.schema = ", ".join(sorted(r[0] for r in _alle(v, "SELECT DISTINCT schema FROM patienten")))
    st.je_kategorie = _je_kategorie(v)
    st.verworfen_je_regel = [(r, n) for r, n in _alle(
        v, "SELECT regel, COUNT(*) FROM verworfen GROUP BY regel ORDER BY COUNT(*) DESC, regel")]
    st.erster_eintrag, st.letzter_eintrag = _zeitspanne(v)
    st.integritaet = str(_eins(v, "PRAGMA integrity_check", "?"))
    st.seiten = _eins(v, "PRAGMA page_count")
    st.freie_seiten = _eins(v, "PRAGMA freelist_count")
    st.vokabular_begriffe = _eins(v, "SELECT COUNT(*) FROM vokabular")
    st.vokabular_kategorien = [(k, n) for k, n in _alle(
        v, "SELECT kategorie, COUNT(*) FROM vokabular GROUP BY kategorie"
        " ORDER BY COUNT(*) DESC, kategorie")]
    from pseudonymed.erkennung.lexikon import je_klasse
    from pseudonymed.erkennung.zusatzdaten import geladene

    st.lexikon_klassen = je_klasse(v)
    st.lexikon_zusatz = geladene(v)


def _je_kategorie(v: sqlite3.Connection) -> list[tuple[str, int]]:
    """Mappings per human category, the most frequent first, ties by name."""
    zaehler = Counter(kategorie(pseudonym, art or "")
                      for pseudonym, art in _alle(v, "SELECT pseudonym, art FROM zuordnungen"))
    return sorted(zaehler.items(), key=lambda kv: (-kv[1], kv[0]))


def _zeitspanne(v: sqlite3.Connection) -> tuple[str, str]:
    """First and last entry as ``YYYY-MM-DD HH:MM:SS``; two empty strings for an empty store."""
    zeiten = [t for t in (_eins(v, "SELECT MIN(angelegt) FROM patienten", None),
                          _eins(v, "SELECT MAX(geaendert) FROM zuordnungen", None),
                          _eins(v, "SELECT MAX(angelegt) FROM patienten", None)) if t]
    if not zeiten:
        return "", ""
    return min(zeiten)[:19].replace("T", " "), max(zeiten)[:19].replace("T", " ")


# -- backup ----------------------------------------------------------------------------------------
@dataclass
class Manifest:
    """What ``manifest.json`` records about an archive: who wrote it and when, the counts
    at that moment, the key's public handle and a checksum per file."""
    version: int = VERSION
    erstellt: str = ""
    werkzeug: str = WERKZEUG
    schema: str = ""
    schluessel_kennung: str = ""
    patienten: int = 0
    aliasse: int = 0
    zuordnungen: int = 0
    verworfen: int = 0
    dateien: dict[str, dict[str, int | str]] = field(default_factory=dict)

    @property
    def konfig(self) -> bool:
        """Whether the configuration file travels in the archive."""
        return NAME_KONFIG in self.dateien

    @property
    def vokabular(self) -> int:
        """Number of vocabulary list files in the archive."""
        return sum(1 for n in self.dateien if n.startswith(ORDNER_VOKABULAR + "/"))

    def beschreibung(self) -> str:
        """One line for the user: date, tool, counts, key handle and the optional parts."""
        return (f"{self.erstellt[:16].replace('T', ' ')} · {self.werkzeug} · {self.patienten}"
                f" Patienten, {self.zuordnungen} Zuordnungen, {self.verworfen} Verwerfungen"
                f" · Schlüssel {self.schluessel_kennung}"
                + (" · Konfiguration" if self.konfig else "")
                + (f" · {self.vokabular} Vokabulardatei(en)" if self.vokabular else ""))


def _sha(daten: bytes) -> str:
    """SHA-256 of a file's bytes as hex, the checksum the manifest records."""
    return hashlib.sha256(daten).hexdigest()


def _db_kopie(db: Path) -> bytes:
    """A consistent snapshot of the database through SQLite's backup API."""
    with tempfile.TemporaryDirectory() as ordner:
        ziel = Path(ordner) / NAME_DB
        _kopiere_datenbank(db, ziel)
        return ziel.read_bytes()


def exportiere(ziel: Path, speicherpfad: Path | None = None, konfig: bool = True,
               vokabular: bool = True, passphrase: str | None = None) -> Path:
    """Write the backup archive; ``ziel`` is a folder (file name chosen) or a file.
    With a passphrase the ZIP is wrapped in an OpenPGP message (``.zip.gpg``)."""
    db, schluessel = _pfade(speicherpfad)
    if not db.exists():
        raise SicherungFehler(f"Keine Datenbank unter {db}.")
    if not schluessel.exists():
        raise SicherungFehler(f"Keine Schlüsseldatei unter {schluessel}; ohne sie lässt sich"
                              " nichts rekonstruieren.")
    st = statistik(db)
    manifest = Manifest(erstellt=dt.datetime.now().isoformat(timespec="seconds"),
                        schema=st.schema, schluessel_kennung=st.schluessel_kennung,
                        patienten=st.patienten, aliasse=st.aliasse, zuordnungen=st.zuordnungen,
                        verworfen=st.verworfen)
    inhalte = _sammle_inhalte(db, schluessel, konfig, vokabular)
    manifest.dateien = {name: {"sha256": _sha(daten), "bytes": len(daten)}
                        for name, daten in inhalte.items()}
    roh = _packe(manifest, inhalte)
    ziel = _archivpfad(Path(ziel), verschluesselt=bool(passphrase))
    if passphrase:
        roh = pgpmodul.verschluessele_mit_passphrase(roh, passphrase)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(roh)
    _schuetze(ziel)
    return ziel


def _sammle_inhalte(db: Path, schluessel: Path, konfig: bool, vokabular: bool) -> dict[str, bytes]:
    """The archive's files by name: a database snapshot, the key, and on request the
    configuration and the vocabulary lists."""
    inhalte: dict[str, bytes] = {NAME_DB: _db_kopie(db), NAME_SCHLUESSEL: schluessel.read_bytes()}
    if konfig and konfigpfad().exists():
        inhalte[NAME_KONFIG] = konfigpfad().read_bytes()
    if vokabular:
        for p in _vokabulardateien(vokabularordner()):
            inhalte[f"{ORDNER_VOKABULAR}/{p.name}"] = p.read_bytes()
    return inhalte


def _packe(manifest: Manifest, inhalte: dict[str, bytes]) -> bytes:
    """The ZIP archive as bytes: the manifest (with the project mark) first, then the files."""
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(NAME_MANIFEST, json.dumps({_MARKE: VERSION, **asdict(manifest)},
                                              ensure_ascii=False, indent=1))
        for name, daten in inhalte.items():
            z.writestr(name, daten)
    return puffer.getvalue()


def _archivpfad(ziel: Path, verschluesselt: bool) -> Path:
    """The file the archive goes to: in a folder (or at a path without suffix) a stamped
    ``pseudonymed_sicherung_<stamp>.zip``; an encrypted one ends in ``.gpg`` for ``gpg``'s sake."""
    if ziel.is_dir() or not ziel.suffix:
        stempel = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        ziel = ziel / f"pseudonymed_sicherung_{stempel}.zip"
    if verschluesselt and ziel.suffix != ".gpg":
        ziel = ziel.with_name(ziel.name + ".gpg")
    return ziel


def _entpacke(archiv: Path, passphrase: str | None) -> tuple[Manifest, dict[str, bytes]]:
    """Read an archive and verify it: decrypt when needed, read manifest and files, check
    every checksum and that database and key are present."""
    roh = _als_zip(Path(archiv).read_bytes(), passphrase)
    manifest, inhalte = _lies_archiv(roh)
    _pruefe_vollstaendigkeit(manifest, inhalte)
    return manifest, inhalte


def _als_zip(roh: bytes, passphrase: str | None) -> bytes:
    """The ZIP bytes: the file itself when it is one, else the OpenPGP message decrypted."""
    if roh.startswith(b"PK"):
        return roh
    if not passphrase:
        raise SicherungFehler("Das Archiv ist verschlüsselt; Passphrase nötig.")
    try:
        klar = pgpmodul.entschluessele_mit_passphrase(roh, passphrase)
    except pgpmodul.PGPFehler as e:
        raise SicherungFehler(str(e)) from e
    if not klar.startswith(b"PK"):
        raise SicherungFehler("Entschlüsselt, aber kein ZIP-Archiv.")
    return klar


def _lies_archiv(roh: bytes) -> tuple[Manifest, dict[str, bytes]]:
    """Manifest and files of a ZIP archive; a torn ZIP is reported as damaged."""
    try:
        with zipfile.ZipFile(io.BytesIO(roh)) as z:
            namen = z.namelist()
            if NAME_MANIFEST not in namen:
                raise SicherungFehler("Kein PseudonyMed-Archiv (manifest.json fehlt).")
            manifest = _manifest_aus(z.read(NAME_MANIFEST))
            inhalte = {n: z.read(n) for n in namen if n != NAME_MANIFEST}
    except zipfile.BadZipFile as e:
        raise SicherungFehler("Beschädigtes Archiv.") from e
    return manifest, inhalte


def _manifest_aus(roh: bytes) -> Manifest:
    """The manifest from its JSON: it must carry the project mark (the one of the earlier
    name is still accepted) and must not come from a newer PseudonyMed."""
    m = json.loads(roh.decode("utf-8"))
    marken = [m.pop(k, None) for k in (_MARKE, _FRUEHERE_MARKE)]
    if all(x is None for x in marken):
        raise SicherungFehler("Kein PseudonyMed-Archiv.")
    if int(m.get("version", 0)) > VERSION:
        raise SicherungFehler("Das Archiv stammt von einem neueren PseudonyMed.")
    return Manifest(**{k: v for k, v in m.items() if k in Manifest.__annotations__})


def _pruefe_vollstaendigkeit(manifest: Manifest, inhalte: dict[str, bytes]) -> None:
    """Every listed file must be present with its checksum, and database and key among them."""
    for name, info in manifest.dateien.items():
        if name not in inhalte:
            raise SicherungFehler(f"Datei {name} fehlt im Archiv.")
        if _sha(inhalte[name]) != info.get("sha256"):
            raise SicherungFehler(f"Prüfsumme von {name} stimmt nicht; Archiv beschädigt.")
    if NAME_DB not in inhalte or NAME_SCHLUESSEL not in inhalte:
        raise SicherungFehler("Archiv ohne Datenbank oder Schlüssel.")


def pruefe(archiv: Path, passphrase: str | None = None) -> Manifest:
    """Read and verify an archive without changing anything."""
    return _entpacke(archiv, passphrase)[0]


@dataclass
class Importbericht:
    """What a restore or merge changed, for the message to the user."""
    modus: str
    sicherung_vorher: list[Path] = field(default_factory=list)
    patienten_neu: int = 0
    aliasse_neu: int = 0
    zuordnungen_neu: int = 0
    verworfen_neu: int = 0
    schluessel_ersetzt: bool = False
    konfig_ersetzt: bool = False
    vokabular_dateien: int = 0

    vokabular_neu: int = 0

    def beschreibung(self) -> str:
        """One sentence per mode: what was restored, or how many entries were new."""
        if self.modus == "ersetzen":
            return (f"Wiederhergestellt: {self.patienten_neu} Patienten, {self.zuordnungen_neu}"
                    f" Zuordnungen, {self.verworfen_neu} Verwerfungen; Schlüssel"
                    f" {'ersetzt' if self.schluessel_ersetzt else 'unverändert'}"
                    + (", Konfiguration ersetzt" if self.konfig_ersetzt else "")
                    + (f", {self.vokabular_dateien} Vokabulardatei(en)" if self.vokabular_dateien
                       else "")
                    + (". Vorherige Fassung gesichert: "
                       + ", ".join(p.name for p in self.sicherung_vorher)
                       if self.sicherung_vorher else "."))
        return (f"Zusammengeführt: {self.patienten_neu} neue Patienten, {self.aliasse_neu} neue"
                f" Schreibweisen, {self.zuordnungen_neu} neue Zuordnungen, {self.verworfen_neu}"
                f" neue Verwerfungen, {self.vokabular_neu} neue Vokabeln"
                + (f", {self.vokabular_dateien} neue Vokabulardatei(en)" if self.vokabular_dateien
                   else "") + ".")


def _sicherheitskopie(pfad: Path, stempel: str) -> Path | None:
    """Copy a file to ``<name>.vor-import-<stamp>`` before the import touches it, or None."""
    if not pfad.exists():
        return None
    kopie = pfad.with_name(f"{pfad.name}.vor-import-{stempel}")
    shutil.copy2(pfad, kopie)
    _schuetze(kopie)
    return kopie


@dataclass(frozen=True)
class _Einspielung:
    """The local files an import works on and the two keys it compares: the one from the
    archive and the one on disk (None without one), plus the stamp naming the safety copies."""
    db: Path
    schluessel: Path
    archiv_db: Path
    neuer_schluessel: bytes
    lokaler_schluessel: bytes | None
    stempel: str

    def schreibe_schluessel(self) -> bool:
        """Put the archive's key in place; True when it differs from the one there before."""
        self.schluessel.write_bytes(self.neuer_schluessel + b"\n")
        return self.lokaler_schluessel != self.neuer_schluessel


def importiere(archiv: Path, speicherpfad: Path | None = None, passphrase: str | None = None,
               modus: str = "ersetzen", konfig: bool = True, vokabular: bool = True,
               ) -> Importbericht:
    """Restore (``ersetzen``) or merge (``zusammenfuehren``) an archive."""
    if modus not in MODI:
        raise SicherungFehler(f"Modus muss einer von {', '.join(MODI)} sein.")
    manifest, inhalte = _entpacke(archiv, passphrase)
    db, schluessel = _pfade(speicherpfad)
    neuer_schluessel = _schluessel_aus(inhalte)
    lokaler_schluessel = schluessel.read_bytes().strip() if schluessel.exists() else None
    bericht = Importbericht(modus=modus)
    stempel = _zeitstempel()
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as ordner:
        archiv_db = Path(ordner) / NAME_DB
        archiv_db.write_bytes(inhalte[NAME_DB])
        e = _Einspielung(db, schluessel, archiv_db, neuer_schluessel, lokaler_schluessel, stempel)
        if modus == "ersetzen":
            _ersetze(e, manifest, bericht, inhalte.get(NAME_KONFIG) if konfig else None)
        else:
            _fuehre_zusammen(e, bericht)
        _schuetze(schluessel, db)
        if vokabular:
            _uebernimm_vokabulardateien(inhalte, modus, bericht)
    return bericht


def _schluessel_aus(inhalte: dict[str, bytes]) -> bytes:
    """The archived key, stripped; it must be hex digits like the key file."""
    schluessel = inhalte[NAME_SCHLUESSEL].strip()
    try:
        bytes.fromhex(schluessel.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SicherungFehler("Der Schlüssel im Archiv ist nicht lesbar.") from e
    return schluessel


def _ersetze(e: _Einspielung, manifest: Manifest, bericht: Importbericht,
             konfig_neu: bytes | None) -> None:
    """Restore: set the current database, key and configuration aside, then put the
    archive's copies in their place; the counts are the manifest's."""
    bericht.sicherung_vorher = [p for p in (_sicherheitskopie(e.db, e.stempel),
                                            _sicherheitskopie(e.schluessel, e.stempel)) if p]
    _kopiere_datenbank(e.archiv_db, e.db)
    bericht.schluessel_ersetzt = e.schreibe_schluessel()
    bericht.patienten_neu, bericht.zuordnungen_neu, bericht.verworfen_neu = (
        manifest.patienten, manifest.zuordnungen, manifest.verworfen)
    if konfig_neu is not None:
        _sicherheitskopie(konfigpfad(), e.stempel)
        konfigpfad().parent.mkdir(parents=True, exist_ok=True)
        konfigpfad().write_bytes(konfig_neu)
        bericht.konfig_ersetzt = True


def _fuehre_zusammen(e: _Einspielung, bericht: Importbericht) -> None:
    """Merge: refuse a foreign key unless the local store is empty, set the database
    aside, adopt the archive's key where there was none, then copy the entries over."""
    lokal_leer = not e.db.exists() or _leer(e.db)
    _verlange_gleichen_schluessel(e, lokal_leer)
    bericht.sicherung_vorher = [p for p in (_sicherheitskopie(e.db, e.stempel),) if p]
    if e.lokaler_schluessel is None or lokal_leer:
        _sicherheitskopie(e.schluessel, e.stempel)
        bericht.schluessel_ersetzt = e.schreibe_schluessel()
    zaehler = _uebernimm_eintraege(e.db, e.archiv_db)
    bericht.patienten_neu, bericht.aliasse_neu = zaehler["patienten"], zaehler["aliasse"]
    bericht.zuordnungen_neu, bericht.verworfen_neu = (zaehler["zuordnungen"],
                                                      zaehler["verworfen"])
    bericht.vokabular_neu = zaehler.get("vokabular", 0)
    if bericht.vokabular_neu:
        _vokabular_vergessen()


def _verlange_gleichen_schluessel(e: _Einspielung, lokal_leer: bool) -> None:
    """Two pseudonym spaces are never mixed: the same person would carry two codes.  A
    missing local key or an empty local database is fine, the archive's key takes over."""
    if e.lokaler_schluessel is None or e.lokaler_schluessel == e.neuer_schluessel or lokal_leer:
        return
    k_archiv = schluessel_kennung(bytes.fromhex(e.neuer_schluessel.decode()))
    k_lokal = schluessel_kennung(bytes.fromhex(e.lokaler_schluessel.decode()))
    raise SicherungFehler(
        "Zusammenführen geht nur bei gleichem Pseudonymschlüssel: das Archiv hat"
        f" den Schlüssel {k_archiv}, die lokale Datenbank {k_lokal}. Zwei"
        " Pseudonymräume lassen sich nicht mischen; stattdessen Ersetzen.")


def _uebernimm_eintraege(db: Path, archiv_db: Path) -> dict[str, int]:
    """Copy the archive's rows into the local database table by table (INSERT OR IGNORE) and
    count the newcomers; new words bump the vocabulary version so cached matchers notice."""
    ziel = sqlite3.connect(db)
    try:
        from pseudonymed.pseudonyme.speicher import _SCHEMA

        ziel.executescript(_SCHEMA)
        ziel.execute("ATTACH DATABASE ? AS archiv", (str(archiv_db),))
        zaehler = {tabelle: _uebernimm_tabelle(ziel, tabelle) for tabelle in _TABELLEN}
        if zaehler.get("vokabular"):
            ziel.execute("INSERT INTO vokabular_meta (schluessel, wert) VALUES"
                         " ('version', '1') ON CONFLICT(schluessel) DO UPDATE SET"
                         " wert = CAST(CAST(wert AS INTEGER) + 1 AS TEXT)")
        ziel.commit()
        ziel.execute("DETACH DATABASE archiv")
    finally:
        ziel.close()
    return zaehler


def _uebernimm_tabelle(ziel: sqlite3.Connection, tabelle: str) -> int:
    """INSERT OR IGNORE the attached archive's rows of one table; returns how many were
    new.  An archive written before the table existed contributes nothing."""
    vorher = ziel.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
    try:
        ziel.execute(f"INSERT OR IGNORE INTO {tabelle} SELECT * FROM archiv.{tabelle}")
    except sqlite3.OperationalError:
        pass
    nachher = ziel.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
    return nachher - vorher


def _uebernimm_vokabulardateien(inhalte: dict[str, bytes], modus: str,
                                bericht: Importbericht) -> None:
    """Write the archive's vocabulary list files into the data folder: all of them when
    restoring, only the missing ones when merging."""
    ordner_v = vokabularordner()
    for name, daten in inhalte.items():
        if not name.startswith(ORDNER_VOKABULAR + "/"):
            continue
        ziel_v = ordner_v / Path(name).name
        if modus == "ersetzen" or not ziel_v.exists():
            ordner_v.mkdir(parents=True, exist_ok=True)
            ziel_v.write_bytes(daten)
            bericht.vokabular_dateien += 1
    if bericht.vokabular_dateien:
        _vokabular_vergessen()


def _vokabular_vergessen() -> None:
    """Drop the cached vocabulary matcher so the next analysis sees the imported words."""
    from pseudonymed.erkennung.fachbegriffe import standard

    standard.cache_clear()


def _leer(db: Path) -> bool:
    """True when the database holds neither patients nor mappings (or none of the tables yet)."""
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for tabelle in ("patienten", "zuordnungen"):
            try:
                if v.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]:
                    return False
            except sqlite3.DatabaseError:
                continue
        return True
    finally:
        v.close()
