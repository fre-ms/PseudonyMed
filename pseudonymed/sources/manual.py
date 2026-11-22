"""Patient description from a JSON or TOML file written by the user."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pseudonymed.core.model import Patient

TEMPLATE = {
    "nachname": "",
    "vorname": "",
    "geschlecht": "",
    "titel": "",
    "geburtsname": "",
    "geburtsdatum": "",
    "strasse": "",
    "plz": "",
    "ort": "",
    "telefone": [],
    "emails": [],
    "versichertennummer": "",
    "patienten_id": "",
    "aktenzeichen": [],
    "angehoerige": [{"nachname": "", "vorname": "", "telefone": [], "emails": []}],
    "zuweiser": [{"nachname": "", "vorname": "", "titel": "", "strasse": "", "telefone": []}],
    "begriffe": [],
}


def template() -> str:
    """JSON template for ``pseudonymed vorlage``."""
    return json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n"


def load_patient(path: str | Path) -> Patient:
    """The patient from a JSON or TOML file written after the template."""
    path = Path(path)
    if path.suffix.lower() == ".toml":
        with path.open("rb") as f:
            data = tomllib.load(f)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("nachname"):
        raise ValueError(f"{path.name}: 'nachname' is required")
    return Patient.from_dict(data)
