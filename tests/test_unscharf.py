"""Fuzzy search: the helpers behind ``ziele_fuer`` and ``finde_unscharf``.  Nothing here is real."""

from __future__ import annotations

import datetime as dt

from pseudonymed.dokument.textdatei import textseite
from pseudonymed.erkennung import unscharf
from pseudonymed.erkennung.unscharf import Ziel
from pseudonymed.kern.modell import Art, Person, Rolle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe
from tests.synthetisch import synthetischer_patient


def _ziel(tokens: list[str], art: Art = Art.NACHNAME) -> Ziel:
    return Ziel(tokens, "[x]", art, "name.nachname", Rolle.PATIENT)


# -- targets ---------------------------------------------------------------------------------------
def test_nachnamen_teile_einmal_und_ab_vier_zeichen() -> None:
    assert unscharf._nachnamen("Flantz-Gotthard") == ["Flantz-Gotthard", "Flantz", "Gotthard"]
    assert unscharf._nachnamen("von Ott") == ["von Ott"]
    assert unscharf._nachnamen("Ott") == []
    assert unscharf._nachnamen("Flantz Flantz") == ["Flantz Flantz", "Flantz"]


def test_namensziele_reihenfolge_und_mindestlaenge() -> None:
    person = Person(nachname="Flantz-Gotthard", vorname="Hildegund Jo", geburtsname="Briemer")
    ziele = unscharf._namensziele("name", person, "[P]", Rolle.PATIENT)
    assert [(z.tokens, z.regel) for z in ziele] == [
        (["Hildegund", "Flantz-Gotthard"], "name.vollname"),
        (["Flantz-Gotthard"], "name.nachname"), (["Flantz"], "name.nachname"),
        (["Gotthard"], "name.nachname"), (["Hildegund"], "name.vorname"),
        (["Briemer"], "name.geburtsname"),
    ]
    assert {z.pseudonym for z in ziele} == {"[P]"} and {z.rolle for z in ziele} == {Rolle.PATIENT}
    kurz = Person(nachname="Ott", vorname="Jo")
    assert unscharf._namensziele("name", kurz, "[P]", Rolle.PATIENT) == []


def test_datumsziele_fuenf_schreibweisen() -> None:
    ziele = unscharf._datumsziele(dt.date(1959, 7, 1), "[geb. 1959]")
    assert [z.tokens for z in ziele] == [["01.07.1959"], ["1.7.1959"], ["01.07.59"],
                                         ["1.", "Juli", "1959"], ["1.", "Jul.", "1959"]]
    assert all(z.art is Art.GEBURTSDATUM and z.regel == "datum.geburtsdatum" for z in ziele)


def test_adressziele_kurzer_strassenname_entfaellt() -> None:
    v = Pseudonymvergabe(synthetischer_patient(3), "K7Q4MX", geheimnis=bytes(32))
    person = Person(nachname="Flantz", strasse="Am Ring 3", plz="01234", ort="Riesa")
    ziele = unscharf._adressziele("adresse", person, v, Rolle.ZUWEISER)
    assert [(z.tokens, z.regel) for z in ziele] == [(["Riesa"], "adresse.ort"),
                                                   (["01234", "Riesa"], "adresse.plz_ort")]
    assert {z.rolle for z in ziele} == {Rolle.ZUWEISER}
    lang = Person(nachname="Flantz", strasse="Ringstraße 3", ort="Ulm")
    ziele = unscharf._adressziele("adresse", lang, v, Rolle.PATIENT)
    assert [z.tokens for z in ziele] == [["Ringstraße", "3"], ["Ringstraße"]]
    assert len({z.pseudonym for z in ziele}) == 1


def test_ziele_fuer_gruppiert_patient_vor_begleitpersonen() -> None:
    p = synthetischer_patient(7)
    v = Pseudonymvergabe(p, "K7Q4MX", geheimnis=bytes(32))
    gruppen = [z.regel.split(".")[0] for z in unscharf.ziele_fuer(p, v)]
    erste = [g for i, g in enumerate(gruppen) if g not in gruppen[:i]]
    erwartet = ["name", "adresse", "datum", "versichertennummer", "patienten_id", "email",
                "aktenzeichen", "angehoerige1", "zuweiser1"]
    assert erste[0] == "name" and erste[-1] == "zuweiser1"
    assert erste == sorted(erste, key=erwartet.index)


# -- the search ------------------------------------------------------------------------------------
def test_suchbar_verlangt_zeichen_in_jedem_token() -> None:
    assert unscharf._suchbar(_ziel(["1.", "Juli", "1959"]))
    assert not unscharf._suchbar(_ziel(["1.", "...", "1959"]))
    assert not unscharf._suchbar(_ziel([]))


def test_ohne_rand_schneidet_satzzeichen() -> None:
    text = '("Fiantz,") ...'
    assert unscharf._ohne_rand(text, 0, 11) == (2, 8) and text[2:8] == "Fiantz"
    assert unscharf._ohne_rand(text, 12, 15) == (15, 15)


def test_gross_genug_nur_fuer_namen_und_orte() -> None:
    name = _ziel(["lang"], Art.NACHNAME)
    assert not unscharf._gross_genug(name, "lang", True)
    assert unscharf._gross_genug(name, "Lang", True)
    assert unscharf._gross_genug(name, "lang", False)
    assert unscharf._gross_genug(_ziel(["lang"], Art.EMAIL), "lang", True)


def test_geteilte_fenster_nur_auf_einer_zeile_und_ohne_angeklebtes_wort() -> None:
    s = textseite("Mail gotthard. flantz@example. org Ende\n"
                  "gotthard. flantz@example.\norg\n"
                  "x gotthard.flantz@example.org Ende")
    tokens = unscharf._tokens(s.text)
    fenster = list(unscharf._geteilte_fenster(s, tokens, "gotthard.flantz@example.org"))
    # pairs first, then triples; (6, 7) crosses a line, (9, 10) has a whole word glued on
    assert fenster == [(1, 2), (5, 6), (8, 9), (1, 3)]


def test_ohne_ueberlappung_laengste_zuerst() -> None:
    z = _ziel(["x"])
    kandidaten = [(0, 5, z), (0, 12, z), (20, 25, z), (30, 34, z)]
    belegt = [(22, 30)]
    assert unscharf._ohne_ueberlappung(kandidaten, belegt) == [(0, 12, z), (30, 34, z)]
    assert belegt == [(22, 30), (0, 12), (30, 34)]
