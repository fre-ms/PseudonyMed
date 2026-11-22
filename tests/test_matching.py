"""Error-tolerant record linkage: scores, thresholds and the store's decisions."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.matching import (
    MATCH_THRESHOLD,
    UNCERTAIN_THRESHOLD,
    compare,
    dice,
    name_similarity,
)
from pseudonymed.pseudonyms.store import PseudonymStore, UncertainMatch


def _p(surname: str = "Flantz", first_name: str = "Gotthard", born: str = "1959-07-21",
       birth_name: str = "") -> Patient:
    return Patient(surname=surname, first_name=first_name, birth_name=birth_name,
                   birth_date=dt.date.fromisoformat(born))


def _row(p: Patient, code: str = "K7Q4MX") -> dict:
    return {"kennung": code, "schluessel": p.key(), "nachname": p.surname,
            "vorname": p.first_name, "geburtsname": p.birth_name,
            "geburtsdatum": p.birth_date.isoformat()}


def test_dice_and_names() -> None:
    assert dice("Müller", "Mueller") == 1.0  # normalisation strips the umlaut
    assert dice("Flantz", "Flantzz") == pytest.approx(14 / 15)
    assert dice("abc", "xyz") == 0.0
    assert name_similarity("Meier", "Mayer") >= 0.9  # phonetically equal
    assert name_similarity("Müller-Lüdenscheidt", "Lüdenscheidt") == 1.0
    assert name_similarity("Hans Peter", "Peter") == 1.0
    assert name_similarity("", "Peter") == 0.0


def test_scores() -> None:
    p = _p()
    assert compare(p, _row(p)).score == 1.0
    assert compare(p, _row(_p(surname="Flantzz"))).score >= MATCH_THRESHOLD
    assert compare(p, _row(_p(first_name="Gotthart"))).score >= MATCH_THRESHOLD
    swapped = compare(p, _row(_p(surname="Gotthard", first_name="Flantz")))
    assert swapped.score == 1.0
    year = compare(p, _row(_p(born="1958-07-21")))
    assert UNCERTAIN_THRESHOLD <= year.score < MATCH_THRESHOLD
    day_month = compare(p, _row(_p(born="1959-07-21")), )
    assert day_month.score == 1.0
    flipped = compare(_p(born="1959-03-07"), _row(_p(born="1959-07-03")))
    assert UNCERTAIN_THRESHOLD <= flipped.score < MATCH_THRESHOLD
    namesake = compare(p, _row(_p(born="1975-01-02")))
    assert namesake.score < UNCERTAIN_THRESHOLD
    foreign = compare(p, _row(_p(surname="Zahn", first_name="Ursel", born="1977-02-01")))
    assert foreign.score < 0.5
    birth_name = compare(_p(surname="Weber", birth_name="Flantz"), _row(p))
    assert birth_name.score == 1.0


def test_store_links_spelling_variants(store: PseudonymStore) -> None:
    p = _p()
    k = store.code_for(p).code
    variant = _p(surname="Flantzz", first_name="GOTTHARD")
    res = store.code_for(variant)
    assert res.code == k and res.linked and res.matching is not None
    assert res.matching.score >= MATCH_THRESHOLD
    assert store.code_for(variant).code == k  # alias now known exactly
    assert store.mapping(variant.key()) == store.mapping(p.key())
    assert store.patient_info(variant.key())["kennung"] == k
    assert len(store.aliases(p.key())) == 1
    assert store.find(search="flantzz")[0]["kennung"] == k


def test_uncertain_matching_needs_a_decision(store: PseudonymStore) -> None:
    p = _p()
    k = store.code_for(p).code
    wrong_year = _p(born="1958-07-21")
    with pytest.raises(UncertainMatch) as e:
        store.code_for(wrong_year)
    assert e.value.candidates[0].code == k
    new = store.code_for(wrong_year, decision="neu")
    assert new.new and new.code != k
    other = _p(born="1959-07-22")
    same = store.code_for(other, decision=f"gleich:{k.lower()}")
    assert same.code == k and same.linked
    with pytest.raises(ValueError):
        store.code_for(_p(born="1959-07-23"), decision="gleich:ZZZZZZ")


def test_same_canonical_identity(store: PseudonymStore) -> None:
    """Phonetically equal names with the same date of birth are the same person
    (the hash scheme would mint the same code on an empty machine anyway)."""
    a = _p(surname="Meier", first_name="Hans", born="1960-01-01")
    b = _p(surname="Mayer", first_name="Hanns", born="1960-01-01")
    ka = store.code_for(a).code
    res = store.code_for(b)
    assert res.code == ka and res.linked
    # declared different: a variant code of the same identity, never the same code
    c = _p(surname="Maier", first_name="Hannes", born="1960-01-01")
    kc = store.code_for(c, decision="neu").code
    assert kc != ka and len(kc) == 6 and store.code_for(c).code == kc


def test_namesake_is_new_person(store: PseudonymStore) -> None:
    k1 = store.code_for(_p()).code
    res = store.code_for(_p(born="1975-01-02"))
    assert res.new and res.code != k1


def test_dataclass_replace_helps_with_testing() -> None:
    p = _p()
    assert dataclasses.replace(p, surname="X").surname == "X"
