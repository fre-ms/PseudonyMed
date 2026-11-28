"""Key capsules: the pseudonym mapping of one document, encrypted to a recipient.

A patient or a colleague pseudonymises a document for someone who is to read it in
clear later (a physician, say).  The sender's PseudonyMed uses a one-time key for the
pseudonyms, so that the document itself reveals nothing and nothing is tied to the
sender's own pseudonym space, and writes the mapping back to the clear data into a
capsule encrypted with the recipient's public key: ``<name>_pseudonym.capsule.asc``.  The
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pseudonymed import TOOL
from pseudonymed.core.model import Patient
from pseudonymed.core.stored import as_stored, field_values
from pseudonymed.core.text import english, say
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

VERSION = 1
EXTENSION = ".capsule.asc"
_MARK = "pseudonymed_capsule"
_FIELDS = ("code", "patient", "mappings", "kinds", "recipient", "source", "created", "tool",
           "one_time_key")


class CapsuleError(Exception):
    """Content that is not a capsule, a newer capsule than this program, or one without
    mapping."""


@dataclass
class Capsule:
    """The pseudonym mapping of one document together with the patient it belongs to."""

    code: str
    patient: dict[str, str]
    mappings: dict[str, str]
    kinds: dict[str, str] = field(default_factory=dict)
    recipient: str = ""  # fingerprint of the recipient's key
    source: str = ""  # file name of the document
    created: str = ""
    tool: str = TOOL
    one_time_key: str = ""  # base64, the key the codes were derived with
    version: int = VERSION

    def as_json(self) -> bytes:
        """The clear JSON form; the version travels as the value of the capsule mark."""
        data = {_MARK: self.version, **as_stored(self)}
        del data["version"]
        return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> Capsule:
        """The capsule from its clear JSON form.

        Raises :class:`CapsuleError` for content that is not a capsule, for a newer
        version and for a capsule without identifier or mapping.
        """
        raw = _load_json(data)
        version = _version_of(raw)
        fields = _fields_of(raw)
        if not isinstance(fields.get("mappings"), dict) or not fields.get("code"):
            raise CapsuleError(say("Die Kapsel enthält keine Zuordnungen.",
                                   "The capsule holds no mappings."))
        return cls(version=version, **field_values(cls, fields))

    def info(self) -> dict[str, Any]:
        """What the re-identification needs about the person (like a store row)."""
        return {**self.patient, "code": self.code,
                "patient_key": self.patient.get("patient_key", "")}

    def as_patient(self) -> Patient:
        """The named patient for the EpiLink match against the recipient's own records;
        an unreadable birth date is left empty."""
        p = self.patient
        born = None
        if p.get("birth_date"):
            try:
                born = dt.date.fromisoformat(p["birth_date"])
            except ValueError:
                born = None
        return Patient(surname=p.get("surname", ""), first_name=p.get("first_name", ""),
                       sex=p.get("sex", ""), title=p.get("title", ""),
                       birth_name=p.get("birth_name", ""), birth_date=born)

    def description(self, language: str = "de") -> str:
        """One line for the user: name, birth date, identifier, source file, mapping size."""
        p = self.patient
        born = p.get("birth_date", "")
        name = f"{p.get('surname', '')}, {p.get('first_name', '')}".strip(", ")
        if english(language):
            parts = [name, f"born {born}" if born else "", f"code {self.code}",
                     f"from {self.source}" if self.source else "",
                     f"{len(self.mappings)} mappings"]
            return " · ".join(t for t in parts if t)
        parts = [name, f"geb. {born}" if born else "", f"Kennung {self.code}",
                 f"aus {self.source}" if self.source else "",
                 f"{len(self.mappings)} Zuordnungen"]
        return " · ".join(t for t in parts if t)


def _load_json(data: bytes) -> Any:
    """The decoded JSON value; bytes that are not UTF-8 JSON are not a capsule."""
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CapsuleError(say("Der Inhalt ist keine PseudonyMed-Kapsel.",
                               "The content is not a PseudonyMed capsule.")) from e


def _version_of(raw: Any) -> int:
    """Take the capsule mark out of the JSON object and return the version it carries;
    content without the mark or newer than this program is refused."""
    if not isinstance(raw, dict) or _MARK not in raw:
        raise CapsuleError(say("Der Inhalt ist keine PseudonyMed-Kapsel.",
                               "The content is not a PseudonyMed capsule."))
    version = int(raw.pop(_MARK))
    if version > VERSION:
        raise CapsuleError(say(f"Kapsel-Version {version} ist neuer als dieses PseudonyMed.",
                               f"Capsule version {version} is newer than this PseudonyMed."))
    return version


def _fields_of(raw: dict[str, Any]) -> dict[str, Any]:
    """The capsule's own fields as given; absent and null ones are left out so that the
    dataclass defaults apply (an empty kind map, an empty recipient, this tool's name)."""
    return {k: raw[k] for k in _FIELDS if raw.get(k) is not None}


def create(patient: Patient, code: str, assignment: PseudonymAssignment, source: str,
           recipient: str, secret: bytes | None) -> Capsule:
    """A capsule for ``patient`` from the mapping the pseudonym assignment collected,
    stamped with the time and the one-time key (base64) the codes were derived with."""
    return Capsule(
        code=code,
        patient={"surname": patient.surname, "first_name": patient.first_name,
                 "sex": patient.sex, "title": patient.title,
                 "birth_name": patient.birth_name,
                 "birth_date": patient.birth_date.isoformat() if patient.birth_date
                 else "", "patient_key": patient.key()},
        mappings=dict(assignment.mapping),
        kinds=dict(assignment.kinds),
        recipient=recipient,
        source=source,
        created=dt.datetime.now().isoformat(timespec="seconds"),
        one_time_key=base64.b64encode(secret).decode("ascii") if secret else "",
    )


def write(capsule: Capsule, certificate: pgp_module.Certificate, path: Path) -> Path:
    """Encrypt the capsule to the recipient and write it (owner-readable only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pgp_module.encrypt(capsule.as_json(), certificate))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def open_file(source: Path | bytes, private_key: str | Path | None = None,
              passphrase: str | None = None, gnupg_home: Path | None = None) -> Capsule:
    """Decrypt and parse a capsule file (or its bytes)."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if not pgp_module.is_message(data):
        raise CapsuleError(say("Die Datei ist keine verschlüsselte Kapsel (PGP MESSAGE).",
                               "The file is not an encrypted capsule (PGP MESSAGE)."))
    decrypted = pgp_module.decrypt(data, private_key, passphrase, gnupg_home)
    return Capsule.from_json(decrypted)
