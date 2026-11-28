"""The files PseudonyMed writes and reads again: dataclasses as plain JSON-ready mappings.

Patient files, reports, working states, capsules, backup manifests and the options keep the
English field names of their dataclasses as keys.  Where people write a file by hand (the
patient file, the configuration) the German names are understood as well; that lives with
those readers, not here.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from typing import Any


def as_stored(value: Any) -> Any:
    """Like :func:`dataclasses.asdict`, keeping texts that carry their English form (a
    German ``Text`` with ``en``) as they are; containers are rebuilt, other values
    deep-copied."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: as_stored(getattr(value, f.name)) for f in dataclasses.fields(value)}
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
    """The entries of a stored mapping that are fields of the dataclass ``cls``; keys the
    class does not know are left out."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in stored.items() if k in names}
