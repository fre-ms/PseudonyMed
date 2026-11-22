"""SQLite store for the pseudonym key.  Lives in the user's data folder and
never leaves the machine.

Two things happen when a patient asks for a pseudonym: the store looks for
the same person first (exact key, then error-tolerant record linkage, see
:mod:`pseudonymed.pseudonyms.matching`), and only when nobody matches it mints
a new identifier, by default a keyed hash of the canonical identity (see
:mod:`pseudonymed.pseudonyms.patient_code`), alternatively a running letter.
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

from pseudonymed import environment
from pseudonymed.core.model import Patient
from pseudonymed.core.text import say
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.matching import Comparison, compare, match_candidates
from pseudonymed.pseudonyms.patient_code import canonical_identity, hash_code, load_secret

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


class UncertainMatch(Exception):
    """The patient resembles stored people but not closely enough to decide alone."""

    def __init__(self, candidates: list[Comparison]) -> None:
        self.candidates = candidates
        super().__init__(say(f"{len(candidates)} ähnliche(r) Eintrag/Einträge im Pseudonymspeicher",
                             f"{len(candidates)} similar entry/entries in the pseudonym store"))


@dataclass
class CodeResult:
    """What asking for a patient's identifier yielded: the code, and whether it was minted
    just now (``new``) or reached through record linkage (``linked``, the match in
    ``matching``); neither for an identifier already stored under the patient's key."""

    code: str
    new: bool = False
    linked: bool = False
    matching: Comparison | None = None


def default_path() -> Path:
    """The pseudonym database: the path from ``[speicher] datenbank`` in the
    configuration, else ``pseudonyme.sqlite`` in the user data folder
    (PSEUDONYMED_DATENORDNER overrides that folder)."""
    from pseudonymed.config import load_settings

    configured = load_settings().store_database
    if configured:
        return Path(configured).expanduser()
    return environment.data_folder() / "pseudonyme.sqlite"


def key_path_for(store_path: Path) -> Path:
    """The key file next to a database (explicitly chosen databases keep their key there)."""
    return store_path.parent / "pseudonymschluessel.txt"


def default_key_path(store_path: Path | None = None) -> Path:
    """The key file for the default database: ``[speicher] schluessel`` in the
    configuration, else next to the database."""
    from pseudonymed.config import load_settings

    configured = load_settings().store_key
    if configured:
        return Path(configured).expanduser()
    return key_path_for(store_path or default_path())


def letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (spreadsheet style)."""
    s = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        s = chr(65 + rest) + s
    return s


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _birth_date(patient: Patient) -> str:
    """The date of birth as the store keeps it: ISO text, empty when unknown."""
    return patient.birth_date.isoformat() if patient.birth_date else ""


def _database_path(path: str | Path | None) -> Path | None:
    """The database file: the one given, else the configured default; None for ``:memory:``."""
    if path is None:
        path = default_path()
    return Path(path) if str(path) != ":memory:" else None


def _protect(path: Path, mode: int) -> None:
    """Restricts a file or folder to its owner; file systems without POSIX modes are
    tolerated, the store works there all the same."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def one_time_store(schema: str = "hash") -> PseudonymStore:
    """An in-memory store with a fresh random key: pseudonyms for a recipient's capsule.
    Nothing is written locally and the codes bear no relation to the local ones."""
    return PseudonymStore(":memory:", schema=schema, secret=secrets.token_bytes(32))


class PseudonymStore:
    """Identifier per patient and pseudonym -> clear text per patient."""

    def __init__(
        self,
        path: str | Path | None = None,
        schema: str = "hash",
        secret: bytes | None = None,
        key_path: str | Path | None = None,
    ) -> None:
        if schema not in SCHEMATA:
            raise ValueError(say(f"Unbekanntes Pseudonymschema: {schema!r}",
                                 f"Unknown pseudonym scheme: {schema!r}"))
        self.schema = schema
        explicit = path is not None
        self.path = _database_path(path)
        self.connection = self._open()
        self._create_schema()
        self.key_path: Path | None = None
        self._load_key(secret, key_path, explicit)

    def _open(self) -> sqlite3.Connection:
        """Opens the database file inside a folder only its owner may enter, or an
        in-memory database for a store without a file."""
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _protect(self.path.parent, 0o700)
        # The desktop opens the store in one worker thread (analysis) and uses it in
        # another (export); access is sequential, so the same-thread check can go.
        connection = sqlite3.connect(str(self.path) if self.path else ":memory:",
                                     check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        """Creates whatever tables and indexes are missing (every statement is
        ``IF NOT EXISTS``, so an older database is migrated in place) and keeps the
        file private."""
        self.connection.executescript(_SCHEMA)
        if self.path is not None:
            _protect(self.path, 0o600)

    def _key_file(self, key_path: str | Path | None, explicit: bool) -> Path | None:
        """The key file to load: the one named, else the one next to an explicitly
        chosen database, else the configured default; None for an in-memory store."""
        if key_path:
            return Path(key_path)
        if self.path is None:
            return None
        return key_path_for(self.path) if explicit else default_key_path(self.path)

    def _load_key(
        self, secret: bytes | None, key_path: str | Path | None, explicit: bool
    ) -> None:
        """Sets the HMAC key: the one handed in, else the one in the key file (created
        on first use), else a fresh random key that dies with an in-memory store."""
        if secret is not None:
            self.secret = secret
            return
        self.key_path = self._key_file(key_path, explicit)
        if self.key_path is None:
            self.secret = secrets.token_bytes(32)  # in-memory store: ephemeral key
            return
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret = load_secret(self.key_path)

    def close(self) -> None:
        """Closes the database connection."""
        self.connection.close()

    def __enter__(self) -> PseudonymStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identifiers ---------------------------------------------------------------------------
    def _canonical_key(self, key: str) -> str:
        line = self.connection.execute(
            "SELECT schluessel FROM aliasse WHERE alias_schluessel = ?", (key,)
        ).fetchone()
        return line["schluessel"] if line else key

    @property
    def is_one_time(self) -> bool:
        """True for an in-memory store: nothing of this run is kept."""
        return self.path is None

    def candidates(self) -> list[dict[str, Any]]:
        """Every stored identity (patients and aliases) for matching against a person."""
        return self._candidates()

    def _candidates(self) -> list[dict[str, Any]]:
        lines = self.connection.execute(
            "SELECT p.schluessel, p.kennung, p.identitaet, p.nachname, p.vorname, p.geburtsname,"
            " p.geburtsdatum FROM patienten p"
            " UNION ALL"
            " SELECT p.schluessel, p.kennung, a.identitaet, a.nachname, a.vorname, a.geburtsname,"
            " a.geburtsdatum FROM aliasse a JOIN patienten p ON p.schluessel = a.schluessel"
        )
        return [dict(z) for z in lines]

    def _link(self, patient: Patient, target: Comparison) -> CodeResult:
        """Records the patient's key as an alias of the stored person and reports the link."""
        self.connection.execute(
            "INSERT OR IGNORE INTO aliasse (alias_schluessel, schluessel, identitaet, nachname,"
            " vorname, geburtsname, geburtsdatum, score, angelegt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (patient.key(), target.key, canonical_identity(patient),
             patient.surname, patient.first_name, patient.birth_name, _birth_date(patient),
             target.score, _now()),
        )
        self.connection.commit()
        return CodeResult(target.code, linked=True, matching=target)

    def _new_code(self, patient: Patient) -> str:
        """An identifier nobody holds yet: the next free letter, or the keyed hash of the
        canonical identity (a variant of it when another person already holds the code)."""
        taken = {z["kennung"] for z in self.connection.execute("SELECT kennung FROM patienten")}
        if self.schema == "buchstabe":
            i = 0
            while letter(i) in taken:
                i += 1
            return letter(i)
        identity = canonical_identity(patient)
        variant = 0
        while True:
            code = hash_code(self.secret, identity, variant=variant)
            if code not in taken:
                return code
            variant += 1  # two different people with the same canonical identity

    def _add_patient(self, key: str, patient: Patient) -> CodeResult:
        """Mints a new identifier and stores the patient under it."""
        new = self._new_code(patient)
        self.connection.execute(
            "INSERT INTO patienten (schluessel, kennung, schema, identitaet, nachname, vorname,"
            " geburtsname, geschlecht, titel, geburtsdatum, angelegt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, new, self.schema, canonical_identity(patient), patient.surname,
             patient.first_name, patient.birth_name, patient.sex, patient.title,
             _birth_date(patient), _now()),
        )
        self.connection.commit()
        return CodeResult(new, new=True)

    def _stored_code(self, key: str) -> str | None:
        """The identifier stored under a canonical key, else None."""
        line = self.connection.execute(
            "SELECT kennung FROM patienten WHERE schluessel = ?", (key,)
        ).fetchone()
        return line["kennung"] if line else None

    def _same_identity(
        self, patient: Patient, candidates: list[dict[str, Any]], matches: list[Comparison]
    ) -> Comparison | None:
        """A stored person with the same canonical identity (phonetic names + date of
        birth), carrying the score the linkage gave it.  The hash scheme would mint the
        same code anyway, so such a person counts as the same one without asking."""
        identity = canonical_identity(patient)
        same_entries = [k for k in candidates if k["identitaet"] == identity]
        if not same_entries:
            return None
        return next((t for t in matches if t.key == same_entries[0]["schluessel"]),
                    compare(patient, same_entries[0]))

    def _requested_target(self, decision: str, matches: list[Comparison]) -> Comparison:
        """The stored person a ``gleich:<KENNUNG>`` decision names: its match from the
        linkage, else a bare link with score 0; an identifier nobody holds raises
        ValueError."""
        requested = decision.split(":", 1)[1].strip().upper()
        target = next((t for t in matches if t.code == requested), None)
        if target is not None:
            return target
        info = self.find(code_=requested)
        if not info:
            raise ValueError(say(f"Kennung {requested} ist nicht vergeben.",
                                 f"Code {requested} is not issued."))
        return Comparison(code=requested, key=info[0]["schluessel"], score=0.0)

    @staticmethod
    def _sure_match(matches: list[Comparison], decision: str | None) -> Comparison | None:
        """The best match when it is sure, else None.  An uncertain best match without
        the decision ``"neu"`` raises :class:`UncertainMatch` listing every
        uncertain or sure match."""
        if not matches:
            return None
        best = matches[0]
        if best.same:
            return best
        if best.uncertain and decision != "neu":
            raise UncertainMatch([t for t in matches if t.uncertain or t.same])
        return None

    def _target_for(
        self, patient: Patient, candidates: list[dict[str, Any]], matches: list[Comparison],
        decision: str | None,
    ) -> Comparison | None:
        """The stored person the patient is linked to, or None when a new identifier is
        due.  Same canonical identity wins, then the caller's decision, then a sure
        match; ``"neu"`` skips the identity shortcut and the uncertain-match question."""
        if decision != "neu":
            target = self._same_identity(patient, candidates, matches)
            if target is not None:
                return target
        if decision and decision.startswith("gleich:"):
            return self._requested_target(decision, matches)
        return self._sure_match(matches, decision)

    def code_for(self, patient: Patient, decision: str | None = None) -> CodeResult:
        """Identifier of this patient: the stored one, the one of a matching person,
        or a new one.  ``decision`` resolves an uncertain match: ``"neu"`` or
        ``"gleich:<KENNUNG>"``; without it an uncertain match raises
        :class:`UncertainMatch`."""
        key = self._canonical_key(patient.key())
        stored = self._stored_code(key)
        if stored is not None:
            return CodeResult(stored)
        candidates = self._candidates()
        matches = match_candidates(patient, candidates)
        target = self._target_for(patient, candidates, matches, decision)
        if target is not None:
            return self._link(patient, target)
        return self._add_patient(key, patient)

    def letter_for(self, patient: Patient) -> str:
        """Compatibility wrapper: the identifier only."""
        return self.code_for(patient).code

    # -- mappings ------------------------------------------------------------------------------
    # -- discarded hits the user wants remembered ------------------------------------------------
    def discarded_for(self, patient: Patient) -> set[tuple[str, str]]:
        """(rule, normalised text) pairs the user discarded for this patient before."""
        key = self._canonical_key(patient.key())
        lines = self.connection.execute(
            "SELECT regel, text FROM verworfen WHERE schluessel = ?", (key,)).fetchall()
        return {(z["regel"], z["text"]) for z in lines}

    def remember_discarded(self, patient: Patient, entries: Iterable[tuple[str, str]]) -> None:
        """Remembers (rule, normalised text) pairs the user discarded for this patient;
        a pair already known only gets a fresh timestamp."""
        key = self._canonical_key(patient.key())
        now = _now()
        for rule, text in entries:
            self.connection.execute(
                "INSERT INTO verworfen (schluessel, regel, text, geaendert) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(schluessel, regel, text)"
                " DO UPDATE SET geaendert = excluded.geaendert",
                (key, rule, text, now))
        self.connection.commit()

    def forget_discarded(self, patient: Patient, entries: Iterable[tuple[str, str]]) -> None:
        """Forgets discarded pairs again, e.g. after the user restored such a hit."""
        key = self._canonical_key(patient.key())
        for rule, text in entries:
            self.connection.execute(
                "DELETE FROM verworfen WHERE schluessel = ? AND regel = ? AND text = ?",
                (key, rule, text))
        self.connection.commit()

    def remember(self, patient: Patient, assignment: PseudonymAssignment) -> None:
        """Stores the pseudonym -> clear text mapping of an assignment for later
        re-identification; a pseudonym already known gets the newer clear text."""
        key = self._canonical_key(patient.key())
        now = _now()
        for pseudonym, plaintext in assignment.mapping.items():
            self.connection.execute(
                "INSERT INTO zuordnungen (schluessel, pseudonym, klartext, art, geaendert)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(schluessel, pseudonym) DO UPDATE SET"
                " klartext = excluded.klartext, art = excluded.art, geaendert = excluded.geaendert",
                (key, pseudonym, plaintext, assignment.kinds.get(pseudonym, ""), now),
            )
        self.connection.commit()

    # -- residual risk across documents ----------------------------------------------------------
    def remember_residual_risk(self, patient: Patient, source: str,
                               features: Iterable[dict[str, Any]], bits: float,
                               lam: float) -> None:
        """Keep the categories and weights (never clear text) a released document revealed,
        so that the next document of the patient is judged together with it."""
        key = self._canonical_key(patient.key())
        now = _now()
        for m in features:
            self.connection.execute(
                "INSERT INTO restrisiko (schluessel, merkmal, kategorie, beschreibung, bits,"
                " geaendert) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(schluessel, merkmal) DO UPDATE"
                " SET bits = MAX(bits, excluded.bits), beschreibung = excluded.beschreibung,"
                " geaendert = excluded.geaendert",
                (key, m["schluessel"], m.get("kategorie", ""), m.get("beschreibung", ""),
                 float(m.get("bits", 0.0)), now))
        self.connection.execute(
            "INSERT INTO restrisiko_dokumente (schluessel, quelle, bits, lam, geaendert)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(schluessel, quelle) DO UPDATE SET bits ="
            " excluded.bits, lam = excluded.lam, geaendert = excluded.geaendert",
            (key, source, float(bits), float(lam), now))
        self.connection.commit()

    def earlier_residual_risks(self, patient: Patient) -> tuple[list[dict[str, Any]], int]:
        """Merkmale of earlier released documents of the patient and how many there were."""
        key = self._canonical_key(patient.key())
        features = [dict(r) for r in self.connection.execute(
            "SELECT merkmal AS schluessel, kategorie, beschreibung, bits FROM restrisiko"
            " WHERE schluessel = ?", (key,))]
        n = self.connection.execute("SELECT COUNT(*) FROM restrisiko_dokumente WHERE schluessel"
                                    " = ?", (key,)).fetchone()[0]
        return features, n

    def mapping(self, key: str) -> dict[str, str]:
        """Pseudonym -> clear text of a patient (an alias key resolves to the stored person)."""
        key = self._canonical_key(key)
        return {
            r["pseudonym"]: r["klartext"]
            for r in self.connection.execute(
                "SELECT pseudonym, klartext FROM zuordnungen WHERE schluessel = ?", (key,)
            )
        }

    def patient_info(self, key: str) -> dict[str, Any] | None:
        """The stored row of a patient (an alias key resolves to the stored person), else None."""
        key = self._canonical_key(key)
        r = self.connection.execute(
            "SELECT * FROM patienten WHERE schluessel = ?", (key,)
        ).fetchone()
        return dict(r) if r else None

    def codes(self) -> list[str]:
        """Every identifier in the store."""
        return [r["kennung"] for r in self.connection.execute("SELECT kennung FROM patienten")]

    def aliases(self, key: str) -> list[dict[str, Any]]:
        """The alias rows (other spellings) linked to a stored person's key, oldest first."""
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM aliasse WHERE schluessel = ? ORDER BY angelegt", (key,))]

    def find(
        self, code_: str | None = None, search: str | None = None,
        letter_: str | None = None,
    ) -> list[dict[str, Any]]:
        """Patients by identifier or by a substring of their name (aliases included)."""
        code_ = code_ or letter_
        if code_:
            lines = self.connection.execute(
                "SELECT * FROM patienten WHERE kennung = ?", (code_.strip().upper(),)
            )
        elif search:
            muster = f"%{search.casefold()}%"
            lines = self.connection.execute(
                "SELECT DISTINCT p.* FROM patienten p"
                " LEFT JOIN aliasse a ON a.schluessel = p.schluessel"
                " WHERE lower(p.nachname) LIKE ? OR lower(p.vorname) LIKE ?"
                " OR lower(a.nachname) LIKE ? OR lower(a.vorname) LIKE ? ORDER BY p.angelegt",
                (muster, muster, muster, muster),
            )
        else:
            lines = self.connection.execute("SELECT * FROM patienten ORDER BY angelegt")
        return [dict(r) for r in lines]
