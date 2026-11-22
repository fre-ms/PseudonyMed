"""The keys of the files PseudonyMed writes and reads again.

Patient files, reports, working states, capsules and backup manifests keep their German
keys: people write patient files by hand, another copy of PseudonyMed reads a capsule or a
backup, and files written earlier must stay readable.  The dataclasses behind them have
English field names.  A class lists the fields whose stored key differs in
``STORED_KEYS`` (field name → stored key); the two functions here translate in both
directions.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any


def keys_of(cls: type) -> Mapping[str, str]:
    """The ``STORED_KEYS`` table of a class, empty when it has none."""
    keys: Mapping[str, str] = getattr(cls, "STORED_KEYS", {})
    return keys


def as_stored(value: Any) -> Any:
    """Like :func:`dataclasses.asdict`, with the fields of every dataclass under their
    stored keys (recursively); containers are rebuilt, other values deep-copied."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        keys = keys_of(type(value))
        return {keys.get(f.name, f.name): as_stored(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    return _rebuilt(value)


def _rebuilt(value: Any) -> Any:
    """A container rebuilt with its items stored, any other value deep-copied."""
    if isinstance(value, tuple) and hasattr(value, "_fields"):  # named tuple
        return type(value)(*(as_stored(v) for v in value))
    if isinstance(value, (list, tuple)):
        return type(value)(as_stored(v) for v in value)
    if isinstance(value, dict):
        return type(value)((as_stored(k), as_stored(v)) for k, v in value.items())
    return copy.deepcopy(value)


def field_values(cls: type, stored: Mapping[str, Any]) -> dict[str, Any]:
    """The entries of a stored mapping that are fields of the dataclass ``cls``, under
    the field names; keys the class does not know are left out."""
    keys = keys_of(cls)
    names = {keys.get(f.name, f.name): f.name for f in dataclasses.fields(cls)}
    return {names[k]: v for k, v in stored.items() if k in names}
