"""Patient description from a JSON or TOML file written by the user."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pseudonymed.kern.modell import Patient

VORLAGE = {
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


def vorlage() -> str:
    """JSON template for ``pseudonymed vorlage``."""
    return json.dumps(VORLAGE, ensure_ascii=False, indent=2) + "\n"


def lade_patient(pfad: str | Path) -> Patient:
    """The patient from a JSON or TOML file written after the template."""
    pfad = Path(pfad)
    if pfad.suffix.lower() == ".toml":
        with pfad.open("rb") as f:
            daten = tomllib.load(f)
    else:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    if not daten.get("nachname"):
        raise ValueError(f"{pfad.name}: 'nachname' is required")
    return Patient.aus_dict(daten)
