"""Rules on page text: longest match wins, lower case words stay, neighbours merge."""

from __future__ import annotations

import datetime as dt
import re

import pymupdf

from pseudonymed.dokument.textebene import Seitentext, lese_seite
from pseudonymed.erkennung.deterministisch import (
    Regel,
    finde,
    finde_im_text,
    kasten_zu_fundstellen,
    regeln_fuer,
)
from pseudonymed.kern.modell import Art, Patient
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


def _patient() -> Patient:
    return Patient(nachname="Müller", vorname="Max", geschlecht="m",
                   geburtsdatum=dt.date(1990, 2, 1), strasse="Musterstraße 12", plz="80331",
                   ort="München", emails=["max@example.org"])


def test_laengster_treffer_gewinnt() -> None:
    p = _patient()
    regeln = regeln_fuer(p, Pseudonymvergabe(p, "A"))
    text = "Herr Max Müller, geb. 01.02.1990, max. 3 Tabletten, Müllerstraße"
    treffer = finde_im_text(text, regeln)
    ids = [r.id for _a, _b, r in treffer]
    assert ids == ["name.vollname", "datum.geburtsdatum"]
    a, b, _ = treffer[0]
    assert text[a:b] == "Max Müller"


def test_benachbarte_treffer_verschmelzen() -> None:
    p = _patient()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Anschrift: Musterstraße 12, 80331 München, Tel.", fontname="helv")
    regeln = regeln_fuer(p, Pseudonymvergabe(p, "A"))
    fundstellen = finde(lese_seite(page), regeln)
    assert [f.pseudonym for f in fundstellen] == ["[Wohnort]"]
    assert fundstellen[0].text == "Musterstraße 12, 80331 München"


def test_adresscodes_mit_schluessel() -> None:
    p = _patient()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Anschrift: Musterstr. 12, 80331 München; geboren in Muenchen.",
                     fontname="helv")
    vergabe = Pseudonymvergabe(p, "K7Q4MX", geheimnis=bytes(32))
    fundstellen = finde(lese_seite(page), regeln_fuer(p, vergabe))
    pseudonyme = [f.pseudonym for f in fundstellen]
    assert len(pseudonyme) == 3
    assert pseudonyme[0].startswith("[Straße ") and pseudonyme[1].startswith("[PLZ Ort ")
    assert pseudonyme[2].startswith("[Ort ")
    assert pseudonyme[1].split()[-1] == pseudonyme[2].split()[-1]  # same town, same code
    assert vergabe.zuordnung[pseudonyme[0]] == "Musterstraße 12"
    assert vergabe.zuordnung[pseudonyme[1]] == "80331 München"
    assert vergabe.zuordnung[pseudonyme[2]] == "München"


def test_generische_muster_und_rollen() -> None:
    p = _patient()
    regeln = regeln_fuer(p, Pseudonymvergabe(p, "A"))
    text = "KVNR X123456789, IBAN DE12 3456 7890 1234 5678 90, fremd@example.com"
    ids = sorted(r.id for _a, _b, r in finde_im_text(text, regeln))
    assert ids == ["muster.email", "muster.iban", "muster.versichertennummer"]
    # OCR puts spaces around dots: still one address
    treffer = finde_im_text("Mail an max. muster@example. org bitte", regeln)
    assert [("max. muster@example. org", "muster.email")] == [
        ("Mail an max. muster@example. org bitte"[a:b], r.id) for a, b, r in treffer]


def test_etikettregeln_treffen_nur_den_wert() -> None:
    p = _patient()
    regeln = regeln_fuer(p, Pseudonymvergabe(p, "A"))
    text = ("Versicherten-Nr. 0181590830 Pat.-ID 99887766 AZ 2024/555, Fall-Nr.: K-2024-0012\n"
            "Blutdruck 120/80 mmHg, Pat. wird vorgestellt.")
    treffer = {text[a:b]: r.id for a, b, r in finde_im_text(text, regeln)}
    assert treffer["0181590830"] == "muster.versichertennummer.etikett"
    assert treffer["99887766"] == "muster.patienten_id.etikett"
    assert treffer["2024/555"] == "muster.aktenzeichen.etikett"
    assert treffer["K-2024-0012"] == "muster.aktenzeichen.etikett"
    assert "120/80" not in treffer and "Versicherten-Nr." not in treffer


def test_etikett_nicht_in_codes() -> None:
    """A pseudonym code that happens to contain "AZ" is not a file number."""
    p = _patient()
    regeln = regeln_fuer(p, Pseudonymvergabe(p, "A"))
    text = ("Patient KAZ2Q4W und AZ 2024/17 sowie Vorgangs-Nr. X-1 und versicherten-nr."
            " 0181590830. AZ Sehr geehrte Frau, AZ MUSTER ohne Ziffer.")
    treffer = {text[a:b]: r.id for a, b, r in finde_im_text(text, regeln)}
    assert "2024/17" in treffer and "X-1" not in treffer  # the value needs 4 characters
    assert treffer["0181590830"] == "muster.versichertennummer.etikett"  # label any case
    assert not any("2Q4W" in t for t in treffer)  # "AZ" inside a code is not a label
    assert "Sehr" not in treffer and "MUSTER" not in treffer  # values need a digit


def test_gegenprobe_schuetzt_verlesene_pseudonyme() -> None:
    """OCR reads "[Ort AZT63Z]" back as "(Ort AZT63Z)" or "Ort AZT632": the code inside
    must still count as a pseudonym, never as a file number."""
    from pseudonymed.erkennung.muster import GLOBALE_MUSTER
    from pseudonymed.pruefung.gegenprobe import in_pseudonym, pseudonymspannen_tolerant

    regeln = [Regel(id_, m, art, "[x]", 5, None) for id_, m, art in GLOBALE_MUSTER]
    text = "Wohnhaft in (Ort AZT63Z), früher Ort AZT632, Akte AZ 2023/171."
    spannen = pseudonymspannen_tolerant(text, ["[Ort AZT63Z]", "Patient K7Q4MX"])
    treffer = [(text[a:b].rstrip("."), r.id) for a, b, r in finde_im_text(text, regeln)
               if not in_pseudonym(a, b, spannen)]
    assert treffer == [("2023/171", "muster.aktenzeichen.etikett")], treffer


def _regel(id_: str, muster: str, prio: int) -> Regel:
    return Regel(id_, re.compile(muster), Art.BEGRIFF, "[x]", prio, None)


def test_kandidaten_ohne_rand_und_mit_wertgruppe() -> None:
    """Blanks a pattern swallowed are cut off, a labelled pattern yields only its value,
    and a match of nothing but blanks is no candidate."""
    from pseudonymed.erkennung.deterministisch import _kandidaten

    regeln = [_regel("leer", r"\s*X\s*", 10), _regel("etikett", r"Nr\.\s*(?P<wert>\d+)", 5),
              _regel("nichts", r"\s+", 1)]
    text = "a  X  b Nr. 42"
    assert [(text[a:b], r.id) for a, b, r in _kandidaten(text, regeln)] == [
        ("X", "leer"), ("42", "etikett")]


def test_auswahl_laenge_vor_prioritaet_vor_reihenfolge() -> None:
    """The longest candidate wins, at equal length the higher priority, then the earlier
    rule; whatever overlaps a chosen hit is dropped."""
    from pseudonymed.erkennung.deterministisch import _ohne_ueberlappung

    lang, hoch = _regel("lang", "x", 1), _regel("hoch", "x", 9)
    frueh, spaet = _regel("frueh", "x", 5), _regel("spaet", "x", 5)
    kandidaten = [(0, 2, frueh), (0, 2, spaet), (0, 2, hoch), (2, 8, lang), (3, 5, hoch),
                  (9, 12, spaet), (9, 12, frueh)]
    assert [(a, b, r.id) for a, b, r in _ohne_ueberlappung(kandidaten)] == [
        (0, 2, "hoch"), (2, 8, "lang"), (9, 12, "spaet")]


def test_kasten_gruppiert_je_zeile() -> None:
    """A box over two lines becomes one hit per line (a word gap does not split a line),
    the drawn box itself is kept on the first hit, and an image page yields the box only."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Erste Zeile hier", fontname="helv")
    page.insert_text((72, 120), "Zweite Zeile", fontname="helv")
    rect = (70.0, 88.0, 200.0, 124.0)
    treffer = kasten_zu_fundstellen(lese_seite(page), rect, "[x]")
    assert [f.text for f in treffer] == ["Erste Zeile hier", "Zweite Zeile"]
    assert treffer[0].rects[-1] == rect and all(f.regel == "manuell" for f in treffer)
    leer = Seitentext(nummer=2, breite=595.0, hoehe=842.0, text="Bild")
    (einzig,) = kasten_zu_fundstellen(leer, rect, "[x]", "maske")
    assert (einzig.start, einzig.ende, einzig.rects, einzig.regel) == (0, 0, [rect], "maske")
