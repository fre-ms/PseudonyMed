"""SQLite store for the pseudonym key.  Lives in the user's data folder and
never leaves the machine.

Two things happen when a patient asks for a pseudonym: the store looks for
the same person first (exact key, then error-tolerant record linkage, see
:mod:`pseudonymed.pseudonyme.abgleich`), and only when nobody matches it mints
a new identifier, by default a keyed hash of the canonical identity (see
:mod:`pseudonymed.pseudonyme.kennung`), alternatively a running letter.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pseudonymed import umgebung
from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme.abgleich import Vergleich, abgleichen, vergleiche
from pseudonymed.pseudonyme.kennung import geheimnis_laden, hash_kennung, kanonische_identitaet
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

SCHEMATA = ("hash", "buchstabe")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patienten (
    schluessel   TEXT PRIMARY KEY,
    kennung      TEXT UNIQUE NOT NULL,
    schema       TEXT NOT NULL DEFAULT 'hash',
    identitaet   TEXT NOT NULL DEFAULT '',
    nachname     TEXT NOT NULL DEFAULT '',
    vorname      TEXT NOT NULL DEFAULT '',
    geburtsname  TEXT NOT NULL DEFAULT '',
    geschlecht   TEXT NOT NULL DEFAULT '',
    titel        TEXT NOT NULL DEFAULT '',
    geburtsdatum TEXT NOT NULL DEFAULT '',
    angelegt     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliasse (
    alias_schluessel TEXT PRIMARY KEY,
    schluessel       TEXT NOT NULL,
    identitaet       TEXT NOT NULL DEFAULT '',
    nachname         TEXT NOT NULL DEFAULT '',
    vorname          TEXT NOT NULL DEFAULT '',
    geburtsname      TEXT NOT NULL DEFAULT '',
    geburtsdatum     TEXT NOT NULL DEFAULT '',
    score            REAL NOT NULL DEFAULT 1.0,
    angelegt         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zuordnungen (
    schluessel TEXT NOT NULL,
    pseudonym  TEXT NOT NULL,
    klartext   TEXT NOT NULL,
    art        TEXT NOT NULL DEFAULT '',
    geaendert  TEXT NOT NULL,
    PRIMARY KEY (schluessel, pseudonym)
);
CREATE TABLE IF NOT EXISTS verworfen (
    schluessel TEXT NOT NULL,
    regel      TEXT NOT NULL,
    text       TEXT NOT NULL,
    geaendert  TEXT NOT NULL,
    PRIMARY KEY (schluessel, regel, text)
);
CREATE TABLE IF NOT EXISTS restrisiko (
    schluessel   TEXT NOT NULL,
    merkmal      TEXT NOT NULL,
    kategorie    TEXT NOT NULL DEFAULT '',
    beschreibung TEXT NOT NULL DEFAULT '',
    bits         REAL NOT NULL,
    geaendert    TEXT NOT NULL,
    PRIMARY KEY (schluessel, merkmal)
);
CREATE TABLE IF NOT EXISTS restrisiko_dokumente (
    schluessel TEXT NOT NULL,
    quelle     TEXT NOT NULL,
    bits       REAL NOT NULL,
    lam        REAL NOT NULL,
    geaendert  TEXT NOT NULL,
    PRIMARY KEY (schluessel, quelle)
);
CREATE TABLE IF NOT EXISTS vokabular (
    begriff   TEXT PRIMARY KEY COLLATE NOCASE,
    kategorie TEXT NOT NULL DEFAULT '',
    angelegt  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS vokabular_kategorie ON vokabular(kategorie);
CREATE TABLE IF NOT EXISTS vokabular_meta (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT NOT NULL
);
"""


class UnsichererAbgleich(Exception):
    """The patient resembles stored people but not closely enough to decide alone."""

    def __init__(self, kandidaten: list[Vergleich]) -> None:
        self.kandidaten = kandidaten
        super().__init__(f"{len(kandidaten)} ähnliche(r) Eintrag/Einträge im Pseudonymspeicher")


@dataclass
class Kennungsergebnis:
    """What asking for a patient's identifier yielded: the code, and whether it was minted
    just now (``neu``) or reached through record linkage (``verknuepft``, the match in
    ``abgleich``); neither for an identifier already stored under the patient's key."""

    kennung: str
    neu: bool = False
    verknuepft: bool = False
    abgleich: Vergleich | None = None


def standardpfad() -> Path:
    """The pseudonym database: the path from ``[speicher] datenbank`` in the
    configuration, else ``pseudonyme.sqlite`` in the user data folder
    (PSEUDONYMED_DATENORDNER overrides that folder)."""
    from pseudonymed.konfig import lade_einstellungen

    konfiguriert = lade_einstellungen().speicher_datenbank
    if konfiguriert:
        return Path(konfiguriert).expanduser()
    return umgebung.datenordner() / "pseudonyme.sqlite"


def schluesselpfad_zu(speicherpfad: Path) -> Path:
    """The key file next to a database (explicitly chosen databases keep their key there)."""
    return speicherpfad.parent / "pseudonymschluessel.txt"


def standard_schluesselpfad(speicherpfad: Path | None = None) -> Path:
    """The key file for the default database: ``[speicher] schluessel`` in the
    configuration, else next to the database."""
    from pseudonymed.konfig import lade_einstellungen

    konfiguriert = lade_einstellungen().speicher_schluessel
    if konfiguriert:
        return Path(konfiguriert).expanduser()
    return schluesselpfad_zu(speicherpfad or standardpfad())


def buchstabe(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (spreadsheet style)."""
    s = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        s = chr(65 + rest) + s
    return s


def _jetzt() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _geburtsdatum(patient: Patient) -> str:
    """The date of birth as the store keeps it: ISO text, empty when unknown."""
    return patient.geburtsdatum.isoformat() if patient.geburtsdatum else ""


def _datenbankpfad(pfad: str | Path | None) -> Path | None:
    """The database file: the one given, else the configured default; None for ``:memory:``."""
    if pfad is None:
        pfad = standardpfad()
    return Path(pfad) if str(pfad) != ":memory:" else None


def _schuetze(pfad: Path, modus: int) -> None:
    """Restricts a file or folder to its owner; file systems without POSIX modes are
    tolerated, the store works there all the same."""
    try:
        os.chmod(pfad, modus)
    except OSError:
        pass


def einmalspeicher(schema: str = "hash") -> Pseudonymspeicher:
    """An in-memory store with a fresh random key: pseudonyms for a recipient's capsule.
    Nothing is written locally and the codes bear no relation to the local ones."""
    return Pseudonymspeicher(":memory:", schema=schema, geheimnis=secrets.token_bytes(32))


class Pseudonymspeicher:
    """Identifier per patient and pseudonym -> clear text per patient."""

    def __init__(
        self,
        pfad: str | Path | None = None,
        schema: str = "hash",
        geheimnis: bytes | None = None,
        schluesselpfad: str | Path | None = None,
    ) -> None:
        if schema not in SCHEMATA:
            raise ValueError(f"Unbekanntes Pseudonymschema: {schema!r}")
        self.schema = schema
        explizit = pfad is not None
        self.pfad = _datenbankpfad(pfad)
        self.verbindung = self._oeffne()
        self._schema_anlegen()
        self.schluesselpfad: Path | None = None
        self._schluessel_laden(geheimnis, schluesselpfad, explizit)

    def _oeffne(self) -> sqlite3.Connection:
        """Opens the database file inside a folder only its owner may enter, or an
        in-memory database for a store without a file."""
        if self.pfad is not None:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
            _schuetze(self.pfad.parent, 0o700)
        # The desktop opens the store in one worker thread (analysis) and uses it in
        # another (export); access is sequential, so the same-thread check can go.
        verbindung = sqlite3.connect(str(self.pfad) if self.pfad else ":memory:",
                                     check_same_thread=False)
        verbindung.row_factory = sqlite3.Row
        return verbindung

    def _schema_anlegen(self) -> None:
        """Creates whatever tables and indexes are missing (every statement is
        ``IF NOT EXISTS``, so an older database is migrated in place) and keeps the
        file private."""
        self.verbindung.executescript(_SCHEMA)
        if self.pfad is not None:
            _schuetze(self.pfad, 0o600)

    def _schluesseldatei(self, schluesselpfad: str | Path | None, explizit: bool) -> Path | None:
        """The key file to load: the one named, else the one next to an explicitly
        chosen database, else the configured default; None for an in-memory store."""
        if schluesselpfad:
            return Path(schluesselpfad)
        if self.pfad is None:
            return None
        return schluesselpfad_zu(self.pfad) if explizit else standard_schluesselpfad(self.pfad)

    def _schluessel_laden(
        self, geheimnis: bytes | None, schluesselpfad: str | Path | None, explizit: bool
    ) -> None:
        """Sets the HMAC key: the one handed in, else the one in the key file (created
        on first use), else a fresh random key that dies with an in-memory store."""
        if geheimnis is not None:
            self.geheimnis = geheimnis
            return
        self.schluesselpfad = self._schluesseldatei(schluesselpfad, explizit)
        if self.schluesselpfad is None:
            self.geheimnis = secrets.token_bytes(32)  # in-memory store: ephemeral key
            return
        self.schluesselpfad.parent.mkdir(parents=True, exist_ok=True)
        self.geheimnis = geheimnis_laden(self.schluesselpfad)

    def close(self) -> None:
        """Closes the database connection."""
        self.verbindung.close()

    def __enter__(self) -> Pseudonymspeicher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identifiers ---------------------------------------------------------------------------
    def _kanonischer_schluessel(self, schluessel: str) -> str:
        zeile = self.verbindung.execute(
            "SELECT schluessel FROM aliasse WHERE alias_schluessel = ?", (schluessel,)
        ).fetchone()
        return zeile["schluessel"] if zeile else schluessel

    @property
    def ist_einmal(self) -> bool:
        """True for an in-memory store: nothing of this run is kept."""
        return self.pfad is None

    def kandidaten(self) -> list[dict[str, Any]]:
        """Every stored identity (patients and aliases) for matching against a person."""
        return self._kandidaten()

    def _kandidaten(self) -> list[dict[str, Any]]:
        zeilen = self.verbindung.execute(
            "SELECT p.schluessel, p.kennung, p.identitaet, p.nachname, p.vorname, p.geburtsname,"
            " p.geburtsdatum FROM patienten p"
            " UNION ALL"
            " SELECT p.schluessel, p.kennung, a.identitaet, a.nachname, a.vorname, a.geburtsname,"
            " a.geburtsdatum FROM aliasse a JOIN patienten p ON p.schluessel = a.schluessel"
        )
        return [dict(z) for z in zeilen]

    def _verknuepfe(self, patient: Patient, ziel: Vergleich) -> Kennungsergebnis:
        """Records the patient's key as an alias of the stored person and reports the link."""
        self.verbindung.execute(
            "INSERT OR IGNORE INTO aliasse (alias_schluessel, schluessel, identitaet, nachname,"
            " vorname, geburtsname, geburtsdatum, score, angelegt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (patient.schluessel(), ziel.schluessel, kanonische_identitaet(patient),
             patient.nachname, patient.vorname, patient.geburtsname, _geburtsdatum(patient),
             ziel.score, _jetzt()),
        )
        self.verbindung.commit()
        return Kennungsergebnis(ziel.kennung, verknuepft=True, abgleich=ziel)

    def _neue_kennung(self, patient: Patient) -> str:
        """An identifier nobody holds yet: the next free letter, or the keyed hash of the
        canonical identity (a variant of it when another person already holds the code)."""
        vergeben = {z["kennung"] for z in self.verbindung.execute("SELECT kennung FROM patienten")}
        if self.schema == "buchstabe":
            i = 0
            while buchstabe(i) in vergeben:
                i += 1
            return buchstabe(i)
        identitaet = kanonische_identitaet(patient)
        variante = 0
        while True:
            code = hash_kennung(self.geheimnis, identitaet, variante=variante)
            if code not in vergeben:
                return code
            variante += 1  # two different people with the same canonical identity

    def _lege_an(self, schluessel: str, patient: Patient) -> Kennungsergebnis:
        """Mints a new identifier and stores the patient under it."""
        neu = self._neue_kennung(patient)
        self.verbindung.execute(
            "INSERT INTO patienten (schluessel, kennung, schema, identitaet, nachname, vorname,"
            " geburtsname, geschlecht, titel, geburtsdatum, angelegt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (schluessel, neu, self.schema, kanonische_identitaet(patient), patient.nachname,
             patient.vorname, patient.geburtsname, patient.geschlecht, patient.titel,
             _geburtsdatum(patient), _jetzt()),
        )
        self.verbindung.commit()
        return Kennungsergebnis(neu, neu=True)

    def _gespeicherte_kennung(self, schluessel: str) -> str | None:
        """The identifier stored under a canonical key, else None."""
        zeile = self.verbindung.execute(
            "SELECT kennung FROM patienten WHERE schluessel = ?", (schluessel,)
        ).fetchone()
        return zeile["kennung"] if zeile else None

    def _gleiche_identitaet(
        self, patient: Patient, kandidaten: list[dict[str, Any]], treffer: list[Vergleich]
    ) -> Vergleich | None:
        """A stored person with the same canonical identity (phonetic names + date of
        birth), carrying the score the linkage gave it.  The hash scheme would mint the
        same code anyway, so such a person counts as the same one without asking."""
        identitaet = kanonische_identitaet(patient)
        gleiche = [k for k in kandidaten if k["identitaet"] == identitaet]
        if not gleiche:
            return None
        return next((t for t in treffer if t.schluessel == gleiche[0]["schluessel"]),
                    vergleiche(patient, gleiche[0]))

    def _gewuenschtes_ziel(self, entscheidung: str, treffer: list[Vergleich]) -> Vergleich:
        """The stored person a ``gleich:<KENNUNG>`` decision names: its match from the
        linkage, else a bare link with score 0; an identifier nobody holds raises
        ValueError."""
        gewuenscht = entscheidung.split(":", 1)[1].strip().upper()
        ziel = next((t for t in treffer if t.kennung == gewuenscht), None)
        if ziel is not None:
            return ziel
        info = self.finde(kennung_=gewuenscht)
        if not info:
            raise ValueError(f"Kennung {gewuenscht} ist nicht vergeben.")
        return Vergleich(kennung=gewuenscht, schluessel=info[0]["schluessel"], score=0.0)

    @staticmethod
    def _sicherer_treffer(treffer: list[Vergleich], entscheidung: str | None) -> Vergleich | None:
        """The best match when it is sure, else None.  An uncertain best match without
        the decision ``"neu"`` raises :class:`UnsichererAbgleich` listing every
        uncertain or sure match."""
        if not treffer:
            return None
        bester = treffer[0]
        if bester.gleich:
            return bester
        if bester.unsicher and entscheidung != "neu":
            raise UnsichererAbgleich([t for t in treffer if t.unsicher or t.gleich])
        return None

    def _ziel_fuer(
        self, patient: Patient, kandidaten: list[dict[str, Any]], treffer: list[Vergleich],
        entscheidung: str | None,
    ) -> Vergleich | None:
        """The stored person the patient is linked to, or None when a new identifier is
        due.  Same canonical identity wins, then the caller's decision, then a sure
        match; ``"neu"`` skips the identity shortcut and the uncertain-match question."""
        if entscheidung != "neu":
            ziel = self._gleiche_identitaet(patient, kandidaten, treffer)
            if ziel is not None:
                return ziel
        if entscheidung and entscheidung.startswith("gleich:"):
            return self._gewuenschtes_ziel(entscheidung, treffer)
        return self._sicherer_treffer(treffer, entscheidung)

    def kennung_fuer(self, patient: Patient, entscheidung: str | None = None) -> Kennungsergebnis:
        """Identifier of this patient: the stored one, the one of a matching person,
        or a new one.  ``entscheidung`` resolves an uncertain match: ``"neu"`` or
        ``"gleich:<KENNUNG>"``; without it an uncertain match raises
        :class:`UnsichererAbgleich`."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        gespeichert = self._gespeicherte_kennung(schluessel)
        if gespeichert is not None:
            return Kennungsergebnis(gespeichert)
        kandidaten = self._kandidaten()
        treffer = abgleichen(patient, kandidaten)
        ziel = self._ziel_fuer(patient, kandidaten, treffer, entscheidung)
        if ziel is not None:
            return self._verknuepfe(patient, ziel)
        return self._lege_an(schluessel, patient)

    def buchstabe_fuer(self, patient: Patient) -> str:
        """Compatibility wrapper: the identifier only."""
        return self.kennung_fuer(patient).kennung

    # -- mappings ------------------------------------------------------------------------------
    # -- discarded hits the user wants remembered ------------------------------------------------
    def verworfene(self, patient: Patient) -> set[tuple[str, str]]:
        """(rule, normalised text) pairs the user discarded for this patient before."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        zeilen = self.verbindung.execute(
            "SELECT regel, text FROM verworfen WHERE schluessel = ?", (schluessel,)).fetchall()
        return {(z["regel"], z["text"]) for z in zeilen}

    def merke_verworfen(self, patient: Patient, eintraege: Iterable[tuple[str, str]]) -> None:
        """Remembers (rule, normalised text) pairs the user discarded for this patient;
        a pair already known only gets a fresh timestamp."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        jetzt = _jetzt()
        for regel, text in eintraege:
            self.verbindung.execute(
                "INSERT INTO verworfen (schluessel, regel, text, geaendert) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(schluessel, regel, text)"
                " DO UPDATE SET geaendert = excluded.geaendert",
                (schluessel, regel, text, jetzt))
        self.verbindung.commit()

    def vergiss_verworfen(self, patient: Patient, eintraege: Iterable[tuple[str, str]]) -> None:
        """Forgets discarded pairs again, e.g. after the user restored such a hit."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        for regel, text in eintraege:
            self.verbindung.execute(
                "DELETE FROM verworfen WHERE schluessel = ? AND regel = ? AND text = ?",
                (schluessel, regel, text))
        self.verbindung.commit()

    def merke(self, patient: Patient, vergabe: Pseudonymvergabe) -> None:
        """Stores the pseudonym -> clear text mapping of an assignment for later
        re-identification; a pseudonym already known gets the newer clear text."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        jetzt = _jetzt()
        for pseudonym, klartext in vergabe.zuordnung.items():
            self.verbindung.execute(
                "INSERT INTO zuordnungen (schluessel, pseudonym, klartext, art, geaendert)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(schluessel, pseudonym) DO UPDATE SET"
                " klartext = excluded.klartext, art = excluded.art, geaendert = excluded.geaendert",
                (schluessel, pseudonym, klartext, vergabe.arten.get(pseudonym, ""), jetzt),
            )
        self.verbindung.commit()

    # -- residual risk across documents ----------------------------------------------------------
    def merke_restrisiko(self, patient: Patient, quelle: str, merkmale: Iterable[dict[str, Any]],
                         bits: float, lam: float) -> None:
        """Keep the categories and weights (never clear text) a released document revealed,
        so that the next document of the patient is judged together with it."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        jetzt = _jetzt()
        for m in merkmale:
            self.verbindung.execute(
                "INSERT INTO restrisiko (schluessel, merkmal, kategorie, beschreibung, bits,"
                " geaendert) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(schluessel, merkmal) DO UPDATE"
                " SET bits = MAX(bits, excluded.bits), beschreibung = excluded.beschreibung,"
                " geaendert = excluded.geaendert",
                (schluessel, m["schluessel"], m.get("kategorie", ""), m.get("beschreibung", ""),
                 float(m.get("bits", 0.0)), jetzt))
        self.verbindung.execute(
            "INSERT INTO restrisiko_dokumente (schluessel, quelle, bits, lam, geaendert)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(schluessel, quelle) DO UPDATE SET bits ="
            " excluded.bits, lam = excluded.lam, geaendert = excluded.geaendert",
            (schluessel, quelle, float(bits), float(lam), jetzt))
        self.verbindung.commit()

    def restrisiko_fruehere(self, patient: Patient) -> tuple[list[dict[str, Any]], int]:
        """Merkmale of earlier released documents of the patient and how many there were."""
        schluessel = self._kanonischer_schluessel(patient.schluessel())
        merkmale = [dict(r) for r in self.verbindung.execute(
            "SELECT merkmal AS schluessel, kategorie, beschreibung, bits FROM restrisiko"
            " WHERE schluessel = ?", (schluessel,))]
        n = self.verbindung.execute("SELECT COUNT(*) FROM restrisiko_dokumente WHERE schluessel"
                                    " = ?", (schluessel,)).fetchone()[0]
        return merkmale, n

    def zuordnung(self, schluessel: str) -> dict[str, str]:
        """Pseudonym -> clear text of a patient (an alias key resolves to the stored person)."""
        schluessel = self._kanonischer_schluessel(schluessel)
        return {
            r["pseudonym"]: r["klartext"]
            for r in self.verbindung.execute(
                "SELECT pseudonym, klartext FROM zuordnungen WHERE schluessel = ?", (schluessel,)
            )
        }

    def patient_info(self, schluessel: str) -> dict[str, Any] | None:
        """The stored row of a patient (an alias key resolves to the stored person), else None."""
        schluessel = self._kanonischer_schluessel(schluessel)
        r = self.verbindung.execute(
            "SELECT * FROM patienten WHERE schluessel = ?", (schluessel,)
        ).fetchone()
        return dict(r) if r else None

    def kennungen(self) -> list[str]:
        """Every identifier in the store."""
        return [r["kennung"] for r in self.verbindung.execute("SELECT kennung FROM patienten")]

    def aliasse(self, schluessel: str) -> list[dict[str, Any]]:
        """The alias rows (other spellings) linked to a stored person's key, oldest first."""
        return [dict(r) for r in self.verbindung.execute(
            "SELECT * FROM aliasse WHERE schluessel = ? ORDER BY angelegt", (schluessel,))]

    def finde(
        self, kennung_: str | None = None, suche: str | None = None,
        buchstabe_: str | None = None,
    ) -> list[dict[str, Any]]:
        """Patients by identifier or by a substring of their name (aliases included)."""
        kennung_ = kennung_ or buchstabe_
        if kennung_:
            zeilen = self.verbindung.execute(
                "SELECT * FROM patienten WHERE kennung = ?", (kennung_.strip().upper(),)
            )
        elif suche:
            muster = f"%{suche.casefold()}%"
            zeilen = self.verbindung.execute(
                "SELECT DISTINCT p.* FROM patienten p"
                " LEFT JOIN aliasse a ON a.schluessel = p.schluessel"
                " WHERE lower(p.nachname) LIKE ? OR lower(p.vorname) LIKE ?"
                " OR lower(a.nachname) LIKE ? OR lower(a.vorname) LIKE ? ORDER BY p.angelegt",
                (muster, muster, muster, muster),
            )
        else:
            zeilen = self.verbindung.execute("SELECT * FROM patienten ORDER BY angelegt")
        return [dict(r) for r in zeilen]
