"""Field scorers of the record linkage: the stored row, the name and date families and
the weighted combiner, each on its own.  All names are invented."""

from __future__ import annotations

import datetime as dt

import pytest

from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme.abgleich import (
    GEWICHTE,
    PHONETISCH_GLEICH,
    _datumsfelder,
    _gewichteter_score,
    _Kandidat,
    _namensfelder,
    _tag_und_monat_vertauscht,
    _teilaehnlichkeit,
    dice,
    namensaehnlichkeit,
)
from tests.synthetisch import synthetischer_patient


def _k(nachname: str = "Flantz", vorname: str = "Gotthard", geburtsname: str = "",
       geburtsdatum: dt.date | None = None) -> _Kandidat:
    return _Kandidat(nachname=nachname, vorname=vorname, geburtsname=geburtsname,
                     geburtsdatum=geburtsdatum)


def test_kandidat_macht_leere_und_fehlende_werte_einheitlich() -> None:
    p = synthetischer_patient(seed=3)
    assert p.geburtsdatum is not None
    zeile = {"nachname": p.nachname, "vorname": None,
             "geburtsdatum": p.geburtsdatum.isoformat() + "T00:00:00"}
    assert _Kandidat.aus(zeile) == _k(p.nachname, "", "", p.geburtsdatum)
    assert _Kandidat.aus({}) == _k("", "", "", None)
    assert _Kandidat.aus({"geburtsdatum": "unbekannt"}).geburtsdatum is None
    assert _Kandidat.aus({"geburtsdatum": dt.date(1960, 1, 1)}).geburtsdatum == dt.date(1960, 1, 1)
    assert _Kandidat.aus({"nachname": 0, "vorname": 12345}) == _k("", "12345", "", None)


def test_datumsfelder_je_komponente_mit_drehung() -> None:
    d = dt.date(1959, 3, 7)
    assert _datumsfelder(d, d) == {"geburtsjahr": 1.0, "geburtsmonat": 1.0, "geburtstag": 1.0}
    gedreht = _datumsfelder(d, dt.date(1959, 7, 3))
    assert gedreht == {"geburtsjahr": 1.0, "geburtsmonat": 0.5, "geburtstag": 0.5}
    # the swap is recognised independently of the year
    assert _datumsfelder(d, dt.date(1960, 7, 3)) == {"geburtsjahr": 0.0, "geburtsmonat": 0.5,
                                                     "geburtstag": 0.5}
    # one component equal is a plain typo, not a swap
    assert _datumsfelder(dt.date(1959, 3, 3), dt.date(1959, 3, 4)) == {
        "geburtsjahr": 1.0, "geburtsmonat": 1.0, "geburtstag": 0.0}
    assert _datumsfelder(None, d) == {}
    assert _datumsfelder(d, None) == {}


def test_tag_und_monat_vertauscht_braucht_zwei_abweichungen() -> None:
    assert _tag_und_monat_vertauscht(dt.date(1959, 3, 7), dt.date(1959, 7, 3))
    assert not _tag_und_monat_vertauscht(dt.date(1959, 5, 5), dt.date(1959, 5, 5))
    assert not _tag_und_monat_vertauscht(dt.date(1959, 3, 7), dt.date(1959, 3, 8))


def test_namensfelder_nur_fuer_beidseitig_vorhandene_namen() -> None:
    p = Patient(nachname="Flantz", vorname="Gotthard")
    assert _namensfelder(p, _k()) == {"nachname": 1.0, "vorname": 1.0}
    assert set(_namensfelder(p, _k(vorname=""))) == {"nachname"}
    assert set(_namensfelder(Patient(nachname="Flantz", vorname=""), _k())) == {"nachname"}
    assert _namensfelder(Patient(nachname="", vorname=""), _k()) == {}


def test_namensfelder_geburtsname_auf_beiden_seiten() -> None:
    verheiratet = Patient(nachname="Weber", vorname="Gotthard", geburtsname="Flantz")
    assert _namensfelder(verheiratet, _k())["nachname"] == 1.0
    ledig = Patient(nachname="Flantz", vorname="Gotthard")
    assert _namensfelder(ledig, _k(nachname="Weber", geburtsname="Flantz"))["nachname"] == 1.0


def test_namensfelder_vertauschte_namen_nur_wenn_besser() -> None:
    p = Patient(nachname="Flantz", vorname="Gotthard")
    assert _namensfelder(p, _k(nachname="Gotthard", vorname="Flantz")) == {
        "nachname": 1.0, "vorname": 1.0}
    felder = _namensfelder(p, _k(vorname="Gotthart"))
    assert felder["nachname"] == 1.0
    assert felder["vorname"] == namensaehnlichkeit("Gotthard", "Gotthart")


def test_gewichteter_score_ist_gewichtetes_mittel() -> None:
    assert _gewichteter_score({}) == 0.0
    assert _gewichteter_score({"nachname": 1.0, "vorname": 0.0}) == pytest.approx(15.2 / 27.2)
    assert _gewichteter_score(dict.fromkeys(GEWICHTE, 1.0)) == pytest.approx(1.0)
    gedreht = {"geburtsjahr": 1.0, "geburtsmonat": 0.5, "geburtstag": 0.5}
    erwartet = (5.1 + 0.5 * 3.6 + 0.5 * 4.9) / (5.1 + 3.6 + 4.9)
    assert _gewichteter_score(gedreht) == pytest.approx(erwartet)


def test_teilaehnlichkeit_phonetik_erst_ab_drei_zeichen() -> None:
    assert _teilaehnlichkeit("Flantz", "Flantz") == 1.0
    assert _teilaehnlichkeit("Meier", "Mayer") == PHONETISCH_GLEICH
    assert _teilaehnlichkeit("Al", "Ol") == dice("Al", "Ol") < PHONETISCH_GLEICH
