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
from dataclasses import asdict, dataclass, field
from pathlib import Path

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


class SicherungFehler(Exception):
    pass


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
        aus: list[tuple[str, str]] = [
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
        aus += [(f"    {k}", str(n)) for k, n in self.je_kategorie]
        aus.append(("Gemerkte Verwerfungen", str(self.verworfen)))
        aus += [(f"    {r}", str(n)) for r, n in self.verworfen_je_regel]
        aus += [
            ("Erster Eintrag", self.erster_eintrag or "–"),
            ("Letzter Eintrag", self.letzter_eintrag or "–"),
            ("Integrität", self.integritaet or "–"),
            ("Seiten / frei", f"{self.seiten} / {self.freie_seiten}"),
            ("Vokabular (Begriffe)", str(self.vokabular_begriffe)),
        ]
        aus += [(f"    {k}", str(n)) for k, n in self.vokabular_kategorien]
        if self.lexikon_klassen:
            aus.append(("Lexikon (Einträge)", str(sum(n for _k, _r, n in self.lexikon_klassen))))
            aus += [(f"    {k} ({'Identifikator' if r == 'merkmal' else 'Fachklasse'})", str(n))
                    for k, r, n in self.lexikon_klassen]
        aus += [("Zusatzdaten (selbst geladen)", f"{q}: {n} Einträge, {g[:10]}")
                for q, n, g in self.lexikon_zusatz]
        aus.append(("Konfiguration", "vorhanden" if self.konfig_vorhanden else "keine"))
        return aus


def _mb(n: int) -> str:
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


@dataclass
class Verlegung:
    datenbank: Path
    schluessel: Path
    kopiert: bool = False
    umbenannt: list[Path] = field(default_factory=list)

    def beschreibung(self) -> str:
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
        stempel = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        if alt_db.exists() and alt_db != datenbank:
            if datenbank.exists() and not _leer(datenbank):
                raise SicherungFehler(f"Am Ziel gibt es schon eine Datenbank: {datenbank}."
                                      " Anderen Pfad wählen oder sie über den Import ersetzen.")
            datenbank.parent.mkdir(parents=True, exist_ok=True)
            quelle = sqlite3.connect(f"file:{alt_db}?mode=ro", uri=True)
            ziel = sqlite3.connect(datenbank)
            try:
                quelle.backup(ziel)
            finally:
                ziel.close()
                quelle.close()
            alt_db.rename(alt_db.with_name(f"{alt_db.name}.verlegt-{stempel}"))
            v.umbenannt.append(alt_db.with_name(f"{alt_db.name}.verlegt-{stempel}"))
            v.kopiert = True
        if alt_schluessel.exists() and alt_schluessel != schluessel:
            if schluessel.exists() and schluessel.read_bytes().strip() != \
                    alt_schluessel.read_bytes().strip():
                raise SicherungFehler("Am Ziel gibt es schon einen anderen Schlüssel:"
                                      f" {schluessel}.")
            schluessel.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(alt_schluessel, schluessel)
            alt_schluessel.rename(alt_schluessel.with_name(
                f"{alt_schluessel.name}.verlegt-{stempel}"))
            v.umbenannt.append(alt_schluessel.with_name(f"{alt_schluessel.name}.verlegt-{stempel}"))
            v.kopiert = True
        for p in (datenbank, schluessel):
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass
    e = lade_einstellungen()
    e.speicher_datenbank, e.speicher_schluessel = str(datenbank), str(schluessel)
    speichere_einstellungen(e)
    return v


def statistik(speicherpfad: Path | None = None) -> Statistik:
    db, schluessel = _pfade(speicherpfad)
    st = Statistik(pfad=db, schluessel_pfad=schluessel, existiert=db.exists(),
                   konfig_vorhanden=konfigpfad().exists())
    if schluessel.exists():
        st.schluessel_vorhanden = True
        try:
            st.schluessel_kennung = schluessel_kennung(
                bytes.fromhex(schluessel.read_text(encoding="utf-8").strip()))
        except ValueError:
            st.schluessel_kennung = "unlesbar"
    ordner = vokabularordner()
    if ordner.is_dir():
        dateien = [p for p in ordner.iterdir() if p.is_file() and p.suffix in (".txt", ".gz")]
        st.vokabular_dateien = len(dateien)
    if not db.exists():
        return st
    st.groesse = db.stat().st_size
    v = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    def eins(sql: str, sonst=0):
        """One value; a table that does not exist yet counts as empty."""
        try:
            return v.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return sonst

    def zeilen(sql: str) -> list:
        try:
            return list(v.execute(sql))
        except sqlite3.OperationalError:
            return []

    try:
        st.patienten = eins("SELECT COUNT(*) FROM patienten")
        st.aliasse = eins("SELECT COUNT(*) FROM aliasse")
        st.zuordnungen = eins("SELECT COUNT(*) FROM zuordnungen")
        st.verworfen = eins("SELECT COUNT(*) FROM verworfen")
        st.schema = ", ".join(sorted(r[0] for r in zeilen("SELECT DISTINCT schema FROM patienten")))
        zaehler: dict[str, int] = {}
        for pseudonym, art in zeilen("SELECT pseudonym, art FROM zuordnungen"):
            k = kategorie(pseudonym, art or "")
            zaehler[k] = zaehler.get(k, 0) + 1
        st.je_kategorie = sorted(zaehler.items(), key=lambda kv: (-kv[1], kv[0]))
        st.verworfen_je_regel = [(r, n) for r, n in zeilen(
            "SELECT regel, COUNT(*) FROM verworfen GROUP BY regel ORDER BY COUNT(*) DESC, regel")]
        zeiten = [t for t in (eins("SELECT MIN(angelegt) FROM patienten", None),
                              eins("SELECT MAX(geaendert) FROM zuordnungen", None),
                              eins("SELECT MAX(angelegt) FROM patienten", None)) if t]
        if zeiten:
            st.erster_eintrag = min(zeiten)[:19].replace("T", " ")
            st.letzter_eintrag = max(zeiten)[:19].replace("T", " ")
        st.integritaet = str(eins("PRAGMA integrity_check", "?"))
        st.seiten = eins("PRAGMA page_count")
        st.freie_seiten = eins("PRAGMA freelist_count")
        st.vokabular_begriffe = eins("SELECT COUNT(*) FROM vokabular")
        st.vokabular_kategorien = [(k, n) for k, n in zeilen(
            "SELECT kategorie, COUNT(*) FROM vokabular GROUP BY kategorie"
            " ORDER BY COUNT(*) DESC, kategorie")]
        from pseudonymed.erkennung.lexikon import je_klasse

        st.lexikon_klassen = je_klasse(v)
        from pseudonymed.erkennung.zusatzdaten import geladene

        st.lexikon_zusatz = geladene(v)
    except sqlite3.DatabaseError as e:
        st.integritaet = f"Fehler: {e}"
    finally:
        v.close()
    return st


# -- backup ----------------------------------------------------------------------------------------
@dataclass
class Manifest:
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
        return NAME_KONFIG in self.dateien

    @property
    def vokabular(self) -> int:
        return sum(1 for n in self.dateien if n.startswith(ORDNER_VOKABULAR + "/"))

    def beschreibung(self) -> str:
        return (f"{self.erstellt[:16].replace('T', ' ')} · {self.werkzeug} · {self.patienten}"
                f" Patienten, {self.zuordnungen} Zuordnungen, {self.verworfen} Verwerfungen"
                f" · Schlüssel {self.schluessel_kennung}"
                + (" · Konfiguration" if self.konfig else "")
                + (f" · {self.vokabular} Vokabulardatei(en)" if self.vokabular else ""))


def _sha(daten: bytes) -> str:
    return hashlib.sha256(daten).hexdigest()


def _db_kopie(db: Path) -> bytes:
    """A consistent snapshot of the database through SQLite's backup API."""
    with tempfile.TemporaryDirectory() as ordner:
        ziel = Path(ordner) / NAME_DB
        quelle = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        kopie = sqlite3.connect(ziel)
        try:
            quelle.backup(kopie)
        finally:
            kopie.close()
            quelle.close()
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
    inhalte: dict[str, bytes] = {NAME_DB: _db_kopie(db), NAME_SCHLUESSEL: schluessel.read_bytes()}
    if konfig and konfigpfad().exists():
        inhalte[NAME_KONFIG] = konfigpfad().read_bytes()
    if vokabular and vokabularordner().is_dir():
        for p in sorted(vokabularordner().iterdir()):
            if p.is_file() and p.suffix in (".txt", ".gz"):
                inhalte[f"{ORDNER_VOKABULAR}/{p.name}"] = p.read_bytes()
    for name, daten in inhalte.items():
        manifest.dateien[name] = {"sha256": _sha(daten), "bytes": len(daten)}
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(NAME_MANIFEST, json.dumps({_MARKE: VERSION, **asdict(manifest)},
                                              ensure_ascii=False, indent=1))
        for name, daten in inhalte.items():
            z.writestr(name, daten)
    roh = puffer.getvalue()
    ziel = Path(ziel)
    if ziel.is_dir() or not ziel.suffix:
        stempel = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        ziel = ziel / f"pseudonymed_sicherung_{stempel}.zip"
    if passphrase:
        roh = pgpmodul.verschluessele_mit_passphrase(roh, passphrase)
        if ziel.suffix != ".gpg":
            ziel = ziel.with_name(ziel.name + ".gpg")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(roh)
    try:
        os.chmod(ziel, 0o600)
    except OSError:
        pass
    return ziel


def _entpacke(archiv: Path, passphrase: str | None) -> tuple[Manifest, dict[str, bytes]]:
    roh = Path(archiv).read_bytes()
    if not roh.startswith(b"PK"):
        if not passphrase:
            raise SicherungFehler("Das Archiv ist verschlüsselt; Passphrase nötig.")
        try:
            roh = pgpmodul.entschluessele_mit_passphrase(roh, passphrase)
        except pgpmodul.PGPFehler as e:
            raise SicherungFehler(str(e)) from e
        if not roh.startswith(b"PK"):
            raise SicherungFehler("Entschlüsselt, aber kein ZIP-Archiv.")
    try:
        with zipfile.ZipFile(io.BytesIO(roh)) as z:
            namen = z.namelist()
            if NAME_MANIFEST not in namen:
                raise SicherungFehler("Kein PseudonyMed-Archiv (manifest.json fehlt).")
            m = json.loads(z.read(NAME_MANIFEST).decode("utf-8"))
            marken = [m.pop(k, None) for k in (_MARKE, _FRUEHERE_MARKE)]
            if all(x is None for x in marken):
                raise SicherungFehler("Kein PseudonyMed-Archiv.")
            if int(m.get("version", 0)) > VERSION:
                raise SicherungFehler("Das Archiv stammt von einem neueren PseudonyMed.")
            manifest = Manifest(**{k: v for k, v in m.items() if k in Manifest.__annotations__})
            inhalte = {n: z.read(n) for n in namen if n != NAME_MANIFEST}
    except zipfile.BadZipFile as e:
        raise SicherungFehler("Beschädigtes Archiv.") from e
    for name, info in manifest.dateien.items():
        if name not in inhalte:
            raise SicherungFehler(f"Datei {name} fehlt im Archiv.")
        if _sha(inhalte[name]) != info.get("sha256"):
            raise SicherungFehler(f"Prüfsumme von {name} stimmt nicht; Archiv beschädigt.")
    if NAME_DB not in inhalte or NAME_SCHLUESSEL not in inhalte:
        raise SicherungFehler("Archiv ohne Datenbank oder Schlüssel.")
    return manifest, inhalte


def pruefe(archiv: Path, passphrase: str | None = None) -> Manifest:
    """Read and verify an archive without changing anything."""
    return _entpacke(archiv, passphrase)[0]


@dataclass
class Importbericht:
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
    if not pfad.exists():
        return None
    kopie = pfad.with_name(f"{pfad.name}.vor-import-{stempel}")
    shutil.copy2(pfad, kopie)
    try:
        os.chmod(kopie, 0o600)
    except OSError:
        pass
    return kopie


def importiere(archiv: Path, speicherpfad: Path | None = None, passphrase: str | None = None,
               modus: str = "ersetzen", konfig: bool = True, vokabular: bool = True,
               ) -> Importbericht:
    """Restore (``ersetzen``) or merge (``zusammenfuehren``) an archive."""
    if modus not in MODI:
        raise SicherungFehler(f"Modus muss einer von {', '.join(MODI)} sein.")
    manifest, inhalte = _entpacke(archiv, passphrase)
    db, schluessel = _pfade(speicherpfad)
    neuer_schluessel = inhalte[NAME_SCHLUESSEL].strip()
    try:
        bytes.fromhex(neuer_schluessel.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SicherungFehler("Der Schlüssel im Archiv ist nicht lesbar.") from e
    lokaler_schluessel = schluessel.read_bytes().strip() if schluessel.exists() else None
    bericht = Importbericht(modus=modus)
    stempel = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as ordner:
        quelle_pfad = Path(ordner) / NAME_DB
        quelle_pfad.write_bytes(inhalte[NAME_DB])
        if modus == "ersetzen":
            bericht.sicherung_vorher = [p for p in (_sicherheitskopie(db, stempel),
                                                    _sicherheitskopie(schluessel, stempel)) if p]
            quelle = sqlite3.connect(f"file:{quelle_pfad}?mode=ro", uri=True)
            ziel = sqlite3.connect(db)
            try:
                quelle.backup(ziel)
            finally:
                ziel.close()
                quelle.close()
            schluessel.write_bytes(neuer_schluessel + b"\n")
            bericht.schluessel_ersetzt = lokaler_schluessel != neuer_schluessel
            bericht.patienten_neu, bericht.zuordnungen_neu, bericht.verworfen_neu = (
                manifest.patienten, manifest.zuordnungen, manifest.verworfen)
            if konfig and NAME_KONFIG in inhalte:
                _sicherheitskopie(konfigpfad(), stempel)
                konfigpfad().parent.mkdir(parents=True, exist_ok=True)
                konfigpfad().write_bytes(inhalte[NAME_KONFIG])
                bericht.konfig_ersetzt = True
        else:
            lokal_leer = not db.exists() or _leer(db)
            if lokaler_schluessel is not None and lokaler_schluessel != neuer_schluessel \
                    and not lokal_leer:
                k_archiv = schluessel_kennung(bytes.fromhex(neuer_schluessel.decode()))
                k_lokal = schluessel_kennung(bytes.fromhex(lokaler_schluessel.decode()))
                raise SicherungFehler(
                    "Zusammenführen geht nur bei gleichem Pseudonymschlüssel: das Archiv hat"
                    f" den Schlüssel {k_archiv}, die lokale Datenbank {k_lokal}. Zwei"
                    " Pseudonymräume lassen sich nicht mischen; stattdessen Ersetzen.")
            bericht.sicherung_vorher = [p for p in (_sicherheitskopie(db, stempel),) if p]
            if lokaler_schluessel is None or lokal_leer:
                _sicherheitskopie(schluessel, stempel)
                schluessel.write_bytes(neuer_schluessel + b"\n")
                bericht.schluessel_ersetzt = lokaler_schluessel != neuer_schluessel
            ziel = sqlite3.connect(db)
            try:
                from pseudonymed.pseudonyme.speicher import _SCHEMA

                ziel.executescript(_SCHEMA)
                ziel.execute("ATTACH DATABASE ? AS archiv", (str(quelle_pfad),))
                zaehler = {}
                for tabelle in ("patienten", "aliasse", "zuordnungen", "verworfen", "vokabular",
                                "restrisiko", "restrisiko_dokumente"):
                    vorher = ziel.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
                    try:
                        ziel.execute(f"INSERT OR IGNORE INTO {tabelle}"
                                     f" SELECT * FROM archiv.{tabelle}")
                    except sqlite3.OperationalError:
                        pass  # an archive from before the table existed
                    nachher = ziel.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
                    zaehler[tabelle] = nachher - vorher
                if zaehler.get("vokabular"):
                    ziel.execute("INSERT INTO vokabular_meta (schluessel, wert) VALUES"
                                 " ('version', '1') ON CONFLICT(schluessel) DO UPDATE SET"
                                 " wert = CAST(CAST(wert AS INTEGER) + 1 AS TEXT)")
                ziel.commit()
                ziel.execute("DETACH DATABASE archiv")
            finally:
                ziel.close()
            bericht.patienten_neu, bericht.aliasse_neu = zaehler["patienten"], zaehler["aliasse"]
            bericht.zuordnungen_neu, bericht.verworfen_neu = (zaehler["zuordnungen"],
                                                              zaehler["verworfen"])
            bericht.vokabular_neu = zaehler.get("vokabular", 0)
            if bericht.vokabular_neu:
                from pseudonymed.erkennung.fachbegriffe import standard

                standard.cache_clear()
        try:
            os.chmod(schluessel, 0o600)
            os.chmod(db, 0o600)
        except OSError:
            pass
        if vokabular:
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
                from pseudonymed.erkennung.fachbegriffe import standard

                standard.cache_clear()
    return bericht


def _leer(db: Path) -> bool:
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
