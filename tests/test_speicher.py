"""Identifiers are stable per patient and reproducible; the key survives reopening."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme.kennung import kennung_gueltig
from pseudonymed.pseudonyme.speicher import (
    Pseudonymspeicher,
    buchstabe,
    schluesselpfad_zu,
    standard_schluesselpfad,
    standardpfad,
)
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

KENNUNG = re.compile(r"^[A-Z2-9]{6}$")


def test_buchstabenfolge() -> None:
    folge = [buchstabe(i) for i in (0, 1, 25, 26, 27, 51, 52)]
    assert folge == ["A", "B", "Z", "AA", "AB", "AZ", "BA"]


def test_buchstabenschema(
    buchstabenspeicher: Pseudonymspeicher, patient: Patient, patientin: Patient
) -> None:
    s = buchstabenspeicher
    assert s.kennung_fuer(patient).kennung == "A"
    assert s.kennung_fuer(patientin).kennung == "B"
    assert s.kennung_fuer(patient).kennung == "A"
    assert s.finde(kennung_="b")[0]["nachname"] == patientin.nachname
    assert s.finde(suche=patient.nachname[:4].lower())[0]["kennung"] == "A"


def test_hashschema_stabil(
    speicher: Pseudonymspeicher, patient: Patient, patientin: Patient
) -> None:
    erg = speicher.kennung_fuer(patient)
    assert erg.neu and KENNUNG.match(erg.kennung) and kennung_gueltig(erg.kennung)
    zweite = speicher.kennung_fuer(patientin)
    assert zweite.kennung != erg.kennung
    wieder = speicher.kennung_fuer(patient)
    assert wieder.kennung == erg.kennung and not wieder.neu and not wieder.verknuepft
    assert speicher.buchstabe_fuer(patient) == erg.kennung  # compatibility wrapper


def test_gleicher_schluessel_gleiche_kennung(tmp_path: Path, patient: Patient) -> None:
    """Two machines with the same key file yield the same identifier for the same person."""
    a = tmp_path / "a" / "pseudonyme.sqlite"
    b = tmp_path / "b" / "pseudonyme.sqlite"
    with Pseudonymspeicher(a) as s:
        k1 = s.kennung_fuer(patient).kennung
    schluessel = schluesselpfad_zu(a)
    assert schluessel.exists() and re.fullmatch(r"[0-9a-f]{64}\n", schluessel.read_text())
    b.parent.mkdir()
    schluesselpfad_zu(b).write_text(schluessel.read_text())
    with Pseudonymspeicher(b) as s:
        assert s.kennung_fuer(patient).kennung == k1
    c = tmp_path / "c" / "pseudonyme.sqlite"
    with Pseudonymspeicher(c) as s:  # fresh random key -> different code
        assert s.kennung_fuer(patient).kennung != k1


def test_zuordnung_und_datei(tmp_path: Path, patient: Patient) -> None:
    pfad = tmp_path / "pseudonyme.sqlite"
    with Pseudonymspeicher(pfad) as s:
        k = s.kennung_fuer(patient).kennung
        vergabe = Pseudonymvergabe(patient, k, geheimnis=s.geheimnis)
        vergabe.hauptperson()
        vergabe.geburtsdatum()
        strasse = vergabe.strasse()
        s.merke(patient, vergabe)
    assert pfad.exists()
    with Pseudonymspeicher(pfad) as s:
        assert s.kennung_fuer(patient).kennung == k
        z = s.zuordnung(patient.schluessel())
        assert z[f"Patient {k}"] == patient.vollname
        assert z[f"[geb. {patient.geburtsdatum.year}]"] == patient.geburtsdatum.strftime("%d.%m.%Y")
        assert z[strasse] == patient.strasse and strasse.startswith("[Straße ")
        info = s.patient_info(patient.schluessel())
        assert info["geschlecht"] == "m" and info["kennung"] == k and info["schema"] == "hash"
        assert s.kennungen() == [k]


def test_unbekanntes_schema() -> None:
    with pytest.raises(ValueError):
        Pseudonymspeicher(":memory:", schema="zufall")


def test_gleich_entscheidung_ohne_aehnlichkeit(
    speicher: Pseudonymspeicher, patient: Patient, patientin: Patient
) -> None:
    """``gleich:<KENNUNG>`` links even a person the linkage never proposed: the
    identifier is looked up in the store and the alias carries score 0."""
    k = speicher.kennung_fuer(patient).kennung
    erg = speicher.kennung_fuer(patientin, entscheidung=f"gleich: {k.lower()}")
    assert erg.kennung == k and erg.verknuepft and not erg.neu
    assert erg.abgleich is not None and erg.abgleich.score == 0.0
    assert erg.abgleich.schluessel == patient.schluessel()
    aliasse = speicher.aliasse(patient.schluessel())
    assert [a["alias_schluessel"] for a in aliasse] == [patientin.schluessel()]
    assert aliasse[0]["score"] == 0.0 and aliasse[0]["nachname"] == patientin.nachname
    wieder = speicher.kennung_fuer(patientin)  # the alias is now known exactly
    assert wieder.kennung == k and not wieder.verknuepft and not wieder.neu
    assert speicher.kennungen() == [k]


def test_schluesselwahl(tmp_path: Path) -> None:
    """Where the HMAC key comes from: the key file named, the one next to an explicitly
    chosen database, the configured default, or (in memory) a key that is never written."""
    datei = tmp_path / "schluessel" / "eigener.txt"
    with Pseudonymspeicher(":memory:", schluesselpfad=datei) as s:
        assert s.schluesselpfad == datei and len(s.geheimnis) == 32
        geheimnis = s.geheimnis
    assert re.fullmatch(r"[0-9a-f]{64}\n", datei.read_text())
    with Pseudonymspeicher(":memory:", schluesselpfad=str(datei)) as s:
        assert s.geheimnis == geheimnis  # the same file yields the same key again
    db = tmp_path / "eigene" / "pseudonyme.sqlite"
    with Pseudonymspeicher(db) as s:
        assert s.pfad == db and s.schluesselpfad == schluesselpfad_zu(db)
        assert schluesselpfad_zu(db).exists()
    with Pseudonymspeicher() as s:  # the default store of the (isolated) data folder
        assert s.pfad == standardpfad() and s.schluesselpfad == standard_schluesselpfad(s.pfad)
    with Pseudonymspeicher(":memory:") as a, Pseudonymspeicher(":memory:") as b:
        assert a.schluesselpfad is None and b.schluesselpfad is None
        assert len(a.geheimnis) == 32 and a.geheimnis != b.geheimnis  # ephemeral, random
    with Pseudonymspeicher(":memory:", geheimnis=bytes(32)) as s:
        assert s.geheimnis == bytes(32) and s.schluesselpfad is None and s.ist_einmal
