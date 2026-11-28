"""Patient description from a JSON or TOML file written by the user.

The keys are English (``surname``, ``first_name`` …); a file written by hand may use the
German names as well (``nachname``, ``vorname`` …, see :data:`pseudonymed.core.model.GERMAN_KEYS`),
and the template comes in the language of the interface.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from pseudonymed.core.model import GERMAN_KEYS, Patient, in_english

TEMPLATE: dict[str, Any] = {
    "surname": "",
    "first_name": "",
    "sex": "",
    "title": "",
    "birth_name": "",
    "birth_date": "",
    "street": "",
    "postcode": "",
    "place": "",
    "phones": [],
    "emails": [],
    "insurance_number": "",
    "patient_id": "",
    "case_numbers": [],
    "relatives": [{"surname": "", "first_name": "", "phones": [], "emails": []}],
    "referrers": [{"surname": "", "first_name": "", "title": "", "street": "", "phones": []}],
    "terms": [],
}
_TO_GERMAN = {en: de for de, en in reversed(GERMAN_KEYS.items())}  # the first German name wins


def _in_german(value: Any) -> Any:
    """The template with its keys in German."""
    if isinstance(value, dict):
        return {_TO_GERMAN.get(k, k): _in_german(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_in_german(v) for v in value]
    return value


def template(language: str = "en") -> str:
    """JSON template for ``pseudonymed template``, with German keys for a German
    interface."""
    data = _in_german(TEMPLATE) if language.lower().startswith("de") else TEMPLATE
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def load_patient(path: str | Path) -> Patient:
    """The patient from a JSON or TOML file written after the template."""
    path = Path(path)
    if path.suffix.lower() == ".toml":
        with path.open("rb") as f:
            data = tomllib.load(f)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not in_english(data).get("surname"):
        raise ValueError(f"{path.name}: 'surname' (or 'nachname') is required")
    return Patient.from_dict(data)
