"""Identifiers are stable per patient and reproducible; the key survives reopening."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.patient_code import is_valid_code
from pseudonymed.pseudonyms.store import (
    PseudonymStore,
    default_key_path,
    default_path,
    key_path_for,
    letter,
)

CODE = re.compile(r"^[A-Z2-9]{6}$")


def test_letter_sequence() -> None:
    sequence = [letter(i) for i in (0, 1, 25, 26, 27, 51, 52)]
    assert sequence == ["A", "B", "Z", "AA", "AB", "AZ", "BA"]


def test_letter_scheme(
    letter_store: PseudonymStore, patient: Patient, female_patient: Patient
) -> None:
    s = letter_store
    assert s.code_for(patient).code == "A"
    assert s.code_for(female_patient).code == "B"
    assert s.code_for(patient).code == "A"
    assert s.find(code_="b")[0]["surname"] == female_patient.surname
    assert s.find(search=patient.surname[:4].lower())[0]["code"] == "A"


def test_hash_scheme_stable(
    store: PseudonymStore, patient: Patient, female_patient: Patient
) -> None:
    res = store.code_for(patient)
    assert res.new and CODE.match(res.code) and is_valid_code(res.code)
    second = store.code_for(female_patient)
    assert second.code != res.code
    restored = store.code_for(patient)
    assert restored.code == res.code and not restored.new and not restored.linked


def test_same_key_same_code(tmp_path: Path, patient: Patient) -> None:
    """Two machines with the same key file yield the same identifier for the same person."""
    a = tmp_path / "a" / "pseudonyms.sqlite"
    b = tmp_path / "b" / "pseudonyms.sqlite"
    with PseudonymStore(a) as s:
        k1 = s.code_for(patient).code
    key = key_path_for(a)
    assert key.exists() and re.fullmatch(r"[0-9a-f]{64}\n", key.read_text())
    b.parent.mkdir()
    key_path_for(b).write_text(key.read_text())
    with PseudonymStore(b) as s:
        assert s.code_for(patient).code == k1
    c = tmp_path / "c" / "pseudonyms.sqlite"
    with PseudonymStore(c) as s:  # fresh random key -> different code
        assert s.code_for(patient).code != k1


def test_mapping_and_file(tmp_path: Path, patient: Patient) -> None:
    path = tmp_path / "pseudonyms.sqlite"
    with PseudonymStore(path) as s:
        k = s.code_for(patient).code
        assignment = PseudonymAssignment(patient, k, secret=s.secret)
        assignment.main_person()
        assignment.birth_date()
        street = assignment.street()
        s.remember(patient, assignment)
    assert path.exists()
    with PseudonymStore(path) as s:
        assert s.code_for(patient).code == k
        z = s.mapping(patient.key())
        assert z[f"Patient {k}"] == patient.full_name
        assert z[f"[geb. {patient.birth_date.year}]"] == patient.birth_date.strftime("%d.%m.%Y")
        assert z[street] == patient.street and street.startswith("[Straße ")
        info = s.patient_info(patient.key())
        assert info["sex"] == "m" and info["code"] == k and info["scheme"] == "hash"
        assert s.codes() == [k]


def test_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        PseudonymStore(":memory:", schema="random")


def test_same_decision_without_similarity(
    store: PseudonymStore, patient: Patient, female_patient: Patient
) -> None:
    """``same:<CODE>`` links even a person the linkage never proposed: the
    identifier is looked up in the store and the alias carries score 0."""
    k = store.code_for(patient).code
    res = store.code_for(female_patient, decision=f"same: {k.lower()}")
    assert res.code == k and res.linked and not res.new
    assert res.matching is not None and res.matching.score == 0.0
    assert res.matching.key == patient.key()
    aliases = store.aliases(patient.key())
    assert [a["alias_key"] for a in aliases] == [female_patient.key()]
    assert aliases[0]["score"] == 0.0 and aliases[0]["surname"] == female_patient.surname
    restored = store.code_for(female_patient)  # the alias is now known exactly
    assert restored.code == k and not restored.linked and not restored.new
    assert store.codes() == [k]


def test_key_choice(tmp_path: Path) -> None:
    """Where the HMAC key comes from: the key file named, the one next to an explicitly
    chosen database, the configured default, or (in memory) a key that is never written."""
    file = tmp_path / "schluessel" / "eigener.txt"
    with PseudonymStore(":memory:", key_path=file) as s:
        assert s.key_path == file and len(s.secret) == 32
        secret = s.secret
    assert re.fullmatch(r"[0-9a-f]{64}\n", file.read_text())
    with PseudonymStore(":memory:", key_path=str(file)) as s:
        assert s.secret == secret  # the same file yields the same key again
    db = tmp_path / "eigene" / "pseudonyms.sqlite"
    with PseudonymStore(db) as s:
        assert s.path == db and s.key_path == key_path_for(db)
        assert key_path_for(db).exists()
    with PseudonymStore() as s:  # the default store of the (isolated) data folder
        assert s.path == default_path() and s.key_path == default_key_path(s.path)
    with PseudonymStore(":memory:") as a, PseudonymStore(":memory:") as b:
        assert a.key_path is None and b.key_path is None
        assert len(a.secret) == 32 and a.secret != b.secret  # ephemeral, random
    with PseudonymStore(":memory:", secret=bytes(32)) as s:
        assert s.secret == bytes(32) and s.key_path is None and s.is_one_time
