"""Pseudonyms in model output go back to clear data, inflections included."""

from __future__ import annotations

import pytest

from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


def _zuordnung(patient: Patient, geheimnis: bytes | None = None) -> dict[str, str]:
    v = Pseudonymvergabe(patient, "A", geheimnis=geheimnis)
    v.hauptperson()
    v.geburtsdatum()
    v.strasse()
    v.plz_ort()
    v.angehoerige(1, patient.angehoerige[0])
    v.zuweiser(1, patient.zuweiser[0])
    return v.zuordnung


def test_adresscodes(patient: Patient) -> None:
    z = _zuordnung(patient, geheimnis=bytes(32))
    strasse = next(k for k in z if k.startswith("[Straße "))
    plz_ort = next(k for k in z if k.startswith("[PLZ Ort "))
    r = rueckersetzen(f"wohnhaft {strasse}, {plz_ort}.", z)
    assert r.text == f"wohnhaft {patient.strasse}, {patient.plz} {patient.ort}."
    assert r.verbleibend == []
    code = strasse.split()[-1].rstrip("]")
    falsch = code[:-1] + ("A" if code[-1] != "A" else "B")
    r = rueckersetzen(f"wohnhaft [Straße {falsch}].", z, bekannte_kennungen=[])
    assert r.verbleibend == [f"[Straße {falsch}]"]
    assert "Prüfzeichen falsch" in r.hinweise[f"[Straße {falsch}]"]


def test_vollname(patient: Patient) -> None:
    z = _zuordnung(patient)
    geb = patient.geburtsdatum
    text = (f"Patient A, [geb. {geb.year}], aus [Wohnort], wurde von Dr. Z1 überwiesen. "
            f"Die Anamnese des Patienten A ergab mit Angehörige A-1 ... Pat. A ist orientiert.")
    r = rueckersetzen(text, z)
    assert patient.vollname in r.text
    assert geb.strftime("%d.%m.%Y") in r.text
    assert patient.angehoerige[0].vollname in r.text
    assert "Dr. med. " + patient.zuweiser[0].vollname in r.text
    assert "Patient A" not in r.text and "Pat. A" not in r.text
    assert r.verbleibend == []
    assert r.ersetzt == 7


def test_anrede_nach_kasus(patient: Patient) -> None:
    z = _zuordnung(patient)
    info = {"nachname": patient.nachname, "vorname": patient.vorname, "geschlecht": "m"}
    r = rueckersetzen("Patient A kam. Wir sahen Patienten A erneut.", z, info, form="anrede")
    assert r.text == f"Herr {patient.nachname} kam. Wir sahen Herrn {patient.nachname} erneut."


def test_anrede_weiblich(patientin: Patient) -> None:
    z = _zuordnung(patientin)
    info = {"nachname": patientin.nachname, "geschlecht": "w"}
    r = rueckersetzen("Patientin A berichtet.", z, info, form="anrede")
    assert r.text == f"Frau {patientin.nachname} berichtet."


def test_verbleibende_pseudonyme(patient: Patient) -> None:
    r = rueckersetzen("Patient A und Patient C sowie [geb. 1888] und Dr. Z9.", _zuordnung(patient))
    assert "Patient C" in r.verbleibend
    assert "[geb. 1888]" in r.verbleibend
    assert "Dr. Z9" in r.verbleibend
    assert "Patient A" not in r.verbleibend


def test_hashkennung_und_pruefzeichen(patient: Patient) -> None:
    from pseudonymed.pseudonyme.kennung import hash_kennung

    k = hash_kennung(bytes(32), "657|47|1959-07-21")
    v = Pseudonymvergabe(patient, k)
    v.hauptperson()
    v.angehoerige(1, patient.angehoerige[0])
    text = f"Patient {k} kam mit Angehörige {k}-1. Später hieß es Patientin {k}."
    r = rueckersetzen(text, v.zuordnung)
    assert r.ersetzt == 3 and k not in r.text
    # a mangled code (wrong last character) is reported with a hint
    falsch = k[:-1] + ("A" if k[-1] != "A" else "B")
    r = rueckersetzen(f"Patient {falsch} kam.", v.zuordnung, bekannte_kennungen=[k])
    assert r.verbleibend == [f"Patient {falsch}"]
    assert "Prüfzeichen falsch" in r.hinweise[f"Patient {falsch}"]
    assert k in r.hinweise[f"Patient {falsch}"]


def test_hinweise_und_reste(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leftover pass keeps first-seen order without duplicates; the hint names a
    wrong check character and suggests the one stored code an edit away."""
    from pseudonymed.pseudonyme import rueckersetzung as modul
    from pseudonymed.pseudonyme.kennung import hash_kennung

    k = hash_kennung(bytes(32), "657|47|1959-07-21")
    falsch = k[:-1] + ("A" if k[-1] != "A" else "B")
    reste = modul._verbleibende(f"Dr. Z2 und Patient {falsch}, dann Dr. Z2 und [Alter 9].")
    assert reste == [f"Patient {falsch}", "Dr. Z2", "[Alter 9]"]
    assert modul._hinweis("[Alter 9]", [k]) == ""  # not a hash code
    assert modul._hinweis(f"Patient {falsch}", []) == "Prüfzeichen falsch, vermutlich verschrieben"
    assert modul._hinweis(f"Angehörige {falsch}-1", [k]).endswith(f"gemeint wohl {k}")
    assert modul._hinweis(f"[Straße {falsch}]", [k, k]) == (
        "Prüfzeichen falsch, vermutlich verschrieben")  # two candidates: no suggestion
    monkeypatch.setattr(modul, "kennung_gueltig", lambda code: True)
    assert modul._hinweis(f"Patient {falsch}", [k]) == f"unbekannt; ähnlich: {k}"
    assert modul._hinweis(f"Patient {falsch}", []) == ""
    assert modul._hinweise_zu([f"Patient {falsch}", "[Alter 9]"], [k]) == {
        f"Patient {falsch}": f"unbekannt; ähnlich: {k}"}
