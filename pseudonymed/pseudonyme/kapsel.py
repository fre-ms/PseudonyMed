"""Key capsules: the pseudonym mapping of one document, encrypted to a recipient.

A patient or a colleague pseudonymises a document for someone who is to read it in
clear later (a physician, say).  The sender's PseudonyMed uses a one-time key for the
pseudonyms, so that the document itself reveals nothing and nothing is tied to the
sender's own pseudonym space, and writes the mapping back to the clear data into a
capsule encrypted with the recipient's public key: ``<name>_pseudonym.kapsel.asc``.  The
document and the language model's answer may travel anywhere; the capsule goes to
the recipient only, who opens it with the private key in step 8 and gets every
pseudonym back, including those of relatives, referrers and places the recipient's own
records do not know.  The capsule also names the patient, so the recipient can match
the person against their records.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pseudonymed import WERKZEUG
from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

VERSION = 1
ENDUNG = ".kapsel.asc"
_MARKE = "pseudonymed_kapsel"
_FRUEHERE_MARKE = "anonymed_kapsel"  # capsules written before the project was renamed


_FELDER = ("kennung", "patient", "zuordnungen", "arten", "empfaenger", "quelle", "erstellt",
           "werkzeug", "einmalschluessel")


class KapselFehler(Exception):
    """Content that is not a capsule, a newer capsule than this program, or one without
    mapping."""


@dataclass
class Kapsel:
    """The pseudonym mapping of one document together with the patient it belongs to."""

    kennung: str
    patient: dict[str, str]
    zuordnungen: dict[str, str]
    arten: dict[str, str] = field(default_factory=dict)
    empfaenger: str = ""  # fingerprint of the recipient's key
    quelle: str = ""  # file name of the document
    erstellt: str = ""
    werkzeug: str = WERKZEUG
    einmalschluessel: str = ""  # base64, the key the codes were derived with
    version: int = VERSION

    def als_json(self) -> bytes:
        """The clear JSON form; the version travels as the value of the capsule mark."""
        daten = {_MARKE: self.version, **asdict(self)}
        del daten["version"]
        return json.dumps(daten, ensure_ascii=False, indent=1).encode("utf-8")

    @classmethod
    def aus_json(cls, daten: bytes) -> Kapsel:
        """The capsule from its clear JSON form, pre-rename capsules included.

        Raises :class:`KapselFehler` for content that is not a capsule, for a newer
        version and for a capsule without identifier or mapping.
        """
        roh = _json_laden(daten)
        version = _version_von(roh)
        felder = _felder_von(roh)
        if not isinstance(felder.get("zuordnungen"), dict) or not felder.get("kennung"):
            raise KapselFehler("Die Kapsel enthält keine Zuordnungen.")
        return cls(version=version, **felder)

    def info(self) -> dict[str, Any]:
        """What the re-identification needs about the person (like a store row)."""
        return {**self.patient, "kennung": self.kennung,
                "schluessel": self.patient.get("schluessel", "")}

    def als_patient(self) -> Patient:
        """The named patient for the EpiLink match against the recipient's own records;
        an unreadable birth date is left empty."""
        p = self.patient
        geb = None
        if p.get("geburtsdatum"):
            try:
                geb = dt.date.fromisoformat(p["geburtsdatum"])
            except ValueError:
                geb = None
        return Patient(nachname=p.get("nachname", ""), vorname=p.get("vorname", ""),
                       geschlecht=p.get("geschlecht", ""), titel=p.get("titel", ""),
                       geburtsname=p.get("geburtsname", ""), geburtsdatum=geb)

    def beschreibung(self) -> str:
        """One line for the user: name, birth date, identifier, source file, mapping size."""
        p = self.patient
        geb = p.get("geburtsdatum", "")
        teile = [f"{p.get('nachname', '')}, {p.get('vorname', '')}".strip(", "),
                 f"geb. {geb}" if geb else "", f"Kennung {self.kennung}",
                 f"aus {self.quelle}" if self.quelle else "",
                 f"{len(self.zuordnungen)} Zuordnungen"]
        return " · ".join(t for t in teile if t)


def _json_laden(daten: bytes) -> Any:
    """The decoded JSON value; bytes that are not UTF-8 JSON are not a capsule."""
    try:
        return json.loads(daten.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KapselFehler("Der Inhalt ist keine PseudonyMed-Kapsel.") from e


def _version_von(roh: Any) -> int:
    """Take the capsule mark (current or pre-rename) out of the JSON object and return
    the version it carries; content without a mark or newer than this program is
    refused."""
    marke = next((m for m in (_MARKE, _FRUEHERE_MARKE) if isinstance(roh, dict) and m in roh),
                 None)
    if marke is None:
        raise KapselFehler("Der Inhalt ist keine PseudonyMed-Kapsel.")
    version = int(roh.pop(marke))
    if version > VERSION:
        raise KapselFehler(f"Kapsel-Version {version} ist neuer als dieses PseudonyMed.")
    return version


def _felder_von(roh: dict[str, Any]) -> dict[str, Any]:
    """The capsule's own fields as given; absent and null ones are left out so that the
    dataclass defaults apply (an empty kind map, an empty recipient, this tool's name)."""
    return {k: roh[k] for k in _FELDER if roh.get(k) is not None}


def erstelle(patient: Patient, kennung: str, vergabe: Pseudonymvergabe, quelle: str,
             empfaenger: str, geheimnis: bytes | None) -> Kapsel:
    """A capsule for ``patient`` from the mapping the pseudonym assignment collected,
    stamped with the time and the one-time key (base64) the codes were derived with."""
    return Kapsel(
        kennung=kennung,
        patient={"nachname": patient.nachname, "vorname": patient.vorname,
                 "geschlecht": patient.geschlecht, "titel": patient.titel,
                 "geburtsname": patient.geburtsname,
                 "geburtsdatum": patient.geburtsdatum.isoformat() if patient.geburtsdatum
                 else "", "schluessel": patient.schluessel()},
        zuordnungen=dict(vergabe.zuordnung),
        arten=dict(vergabe.arten),
        empfaenger=empfaenger,
        quelle=quelle,
        erstellt=dt.datetime.now().isoformat(timespec="seconds"),
        einmalschluessel=base64.b64encode(geheimnis).decode("ascii") if geheimnis else "",
    )


def schreibe(kapsel: Kapsel, zertifikat: pgpmodul.Zertifikat, pfad: Path) -> Path:
    """Encrypt the capsule to the recipient and write it (owner-readable only)."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(pgpmodul.verschluessele(kapsel.als_json(), zertifikat))
    try:
        os.chmod(pfad, 0o600)
    except OSError:
        pass
    return pfad


def oeffne(quelle: Path | bytes, privater_schluessel: str | Path | None = None,
           passphrase: str | None = None, gnupg_home: Path | None = None) -> Kapsel:
    """Decrypt and parse a capsule file (or its bytes)."""
    daten = quelle if isinstance(quelle, bytes) else Path(quelle).read_bytes()
    if not pgpmodul.ist_nachricht(daten):
        raise KapselFehler("Die Datei ist keine verschlüsselte Kapsel (PGP MESSAGE).")
    klar = pgpmodul.entschluessele(daten, privater_schluessel, passphrase, gnupg_home)
    return Kapsel.aus_json(klar)
