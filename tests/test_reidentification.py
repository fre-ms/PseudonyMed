"""Pseudonyms in model output go back to clear data, inflections included."""

from __future__ import annotations

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.reidentification import reidentify


def _mapping(patient: Patient, secret: bytes | None = None) -> dict[str, str]:
    v = PseudonymAssignment(patient, "A", secret=secret)
    v.main_person()
    v.birth_date()
    v.street()
    v.postcode_place()
    v.relatives(1, patient.relatives[0])
    v.referrers(1, patient.referrers[0])
    return v.mapping


def test_address_codes(patient: Patient) -> None:
    z = _mapping(patient, secret=bytes(32))
    street = next(k for k in z if k.startswith("[Straße "))
    postcode_place = next(k for k in z if k.startswith("[PLZ Ort "))
    r = reidentify(f"wohnhaft {street}, {postcode_place}.", z)
    assert r.text == f"wohnhaft {patient.street}, {patient.postcode} {patient.place}."
    assert r.remaining == []
    code = street.split()[-1].rstrip("]")
    wrong = code[:-1] + ("A" if code[-1] != "A" else "B")
    r = reidentify(f"wohnhaft [Straße {wrong}].", z, known_codes=[])
    assert r.remaining == [f"[Straße {wrong}]"]
    assert "Prüfzeichen falsch" in r.notes[f"[Straße {wrong}]"]


def test_full_name(patient: Patient) -> None:
    z = _mapping(patient)
    born = patient.birth_date
    text = (f"Patient A, [geb. {born.year}], aus [Wohnort], wurde von Dr. Z1 überwiesen. "
            f"Die Anamnese des Patienten A ergab mit Angehörige A-1 ... Pat. A ist orientiert.")
    r = reidentify(text, z)
    assert patient.full_name in r.text
    assert born.strftime("%d.%m.%Y") in r.text
    assert patient.relatives[0].full_name in r.text
    assert "Dr. med. " + patient.referrers[0].full_name in r.text
    assert "Patient A" not in r.text and "Pat. A" not in r.text
    assert r.remaining == []
    assert r.replaced == 7


def test_salutation_by_case(patient: Patient) -> None:
    z = _mapping(patient)
    info = {"nachname": patient.surname, "vorname": patient.first_name, "geschlecht": "m"}
    r = reidentify("Patient A kam. Wir sahen Patienten A erneut.", z, info, form="anrede")
    assert r.text == f"Herr {patient.surname} kam. Wir sahen Herrn {patient.surname} erneut."


def test_salutation_female(female_patient: Patient) -> None:
    z = _mapping(female_patient)
    info = {"nachname": female_patient.surname, "geschlecht": "w"}
    r = reidentify("Patientin A berichtet.", z, info, form="anrede")
    assert r.text == f"Frau {female_patient.surname} berichtet."


def test_remaining_pseudonyms(patient: Patient) -> None:
    r = reidentify("Patient A und Patient C sowie [geb. 1888] und Dr. Z9.", _mapping(patient))
    assert "Patient C" in r.remaining
    assert "[geb. 1888]" in r.remaining
    assert "Dr. Z9" in r.remaining
    assert "Patient A" not in r.remaining


def test_hash_code_and_check_character(patient: Patient) -> None:
    from pseudonymed.pseudonyms.patient_code import hash_code

    k = hash_code(bytes(32), "657|47|1959-07-21")
    v = PseudonymAssignment(patient, k)
    v.main_person()
    v.relatives(1, patient.relatives[0])
    text = f"Patient {k} kam mit Angehörige {k}-1. Später hieß es Patientin {k}."
    r = reidentify(text, v.mapping)
    assert r.replaced == 3 and k not in r.text
    # a mangled code (wrong last character) is reported with a hint
    wrong = k[:-1] + ("A" if k[-1] != "A" else "B")
    r = reidentify(f"Patient {wrong} kam.", v.mapping, known_codes=[k])
    assert r.remaining == [f"Patient {wrong}"]
    assert "Prüfzeichen falsch" in r.notes[f"Patient {wrong}"]
    assert k in r.notes[f"Patient {wrong}"]


def test_notes_and_leftovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leftover pass keeps first-seen order without duplicates; the hint names a
    wrong check character and suggests the one stored code an edit away."""
    from pseudonymed.pseudonyms import reidentification as module
    from pseudonymed.pseudonyms.patient_code import hash_code

    k = hash_code(bytes(32), "657|47|1959-07-21")
    wrong = k[:-1] + ("A" if k[-1] != "A" else "B")
    remaining = module._leftovers(f"Dr. Z2 und Patient {wrong}, dann Dr. Z2 und [Alter 9].")
    assert remaining == [f"Patient {wrong}", "Dr. Z2", "[Alter 9]"]
    assert module._note("[Alter 9]", [k]) == ""  # not a hash code
    assert module._note(f"Patient {wrong}", []) == "Prüfzeichen falsch, vermutlich verschrieben"
    assert module._note(f"Angehörige {wrong}-1", [k]).endswith(f"gemeint wohl {k}")
    assert module._note(f"[Straße {wrong}]", [k, k]) == (
        "Prüfzeichen falsch, vermutlich verschrieben")  # two candidates: no suggestion
    monkeypatch.setattr(module, "is_valid_code", lambda code: True)
    assert module._note(f"Patient {wrong}", [k]) == f"unbekannt; ähnlich: {k}"
    assert module._note(f"Patient {wrong}", []) == ""
    assert module._notes_for([f"Patient {wrong}", "[Alter 9]"], [k]) == {
        f"Patient {wrong}": f"unbekannt; ähnlich: {k}"}
