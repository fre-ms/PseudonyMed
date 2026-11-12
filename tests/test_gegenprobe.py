"""The re-check: hits inside pseudonyms do not count, tolerated ones are counted once,
the plain-text pass adds only what the text layer missed, and leftover metadata or
structure blocks the release."""

from __future__ import annotations

import re

import pymupdf
import pytest

from pseudonymed.dokument.textebene import lese_seite
from pseudonymed.erkennung.deterministisch import Regel
from pseudonymed.kern.modell import Art, Gegenprobe
from pseudonymed.pruefung import gegenprobe as gp
from pseudonymed.schwaerzen.pdf import ERLAUBTE_METADATEN, VERMERK_PRAEFIX

WORT = Regel("probe.wort", re.compile(r"Beispielwort"), Art.BEGRIFF, "[Begriff]")
AKTE = Regel("probe.akte", re.compile(r"\b[A-Z]{2}\d{4}\b"), Art.AKTENZEICHEN, "[Akte]")
REGELN = [WORT, AKTE]
TOLERIERT = frozenset({gp.toleranzschluessel("probe.wort", "Beispielwort")})


def _ziele(toleriert: frozenset[tuple[str, str]] = frozenset(),
           pseudonyme: tuple[str, ...] = ()) -> gp._Suchziele:
    return gp._Suchziele(REGELN, toleriert, pseudonyme)


def _dokument(*seiten: str) -> pymupdf.Document:
    doc = pymupdf.open()
    for text in seiten:
        doc.new_page().insert_text((72, 72), text)
    return doc


def _kurz(ergebnis: Gegenprobe) -> list[tuple[int, str, str]]:
    return [(r["seite"], r["regel"], r["quelle"]) for r in ergebnis.resttreffer]


def test_ist_toleriert_normalisiert_leerraum_und_schreibung() -> None:
    ziele = _ziele(frozenset({gp.toleranzschluessel("probe.wort", "Beispiel  Wort")}))
    assert ziele.ist_toleriert("probe.wort", "beispiel\twort")
    assert not ziele.ist_toleriert("probe.akte", "beispiel wort")


def test_textebene_schuetzt_pseudonyme_und_zaehlt_belassenes() -> None:
    doc = _dokument("Beispielwort, Akte AB1234, dann [Akte CD5678].")
    ergebnis = Gegenprobe()
    gesehen: set[tuple[int, str, str]] = set()
    gp._pruefe_textebene(_ziele(TOLERIERT, ("[Akte CD5678]",)), lese_seite(doc[0], 0),
                         ergebnis, gesehen)
    assert _kurz(ergebnis) == [(1, "probe.akte", "text")]
    assert ergebnis.belassen == ["probe.wort"]
    assert gesehen == {(0, "probe.wort", "Beispielwort"), (0, "probe.akte", "AB1234"),
                       (0, "probe.akte", "CD5678")}


def test_klartext_meldet_nur_neues_und_jeden_treffer_einmal() -> None:
    text = "Beispielwort; AB1234; AB1234; [Akte CD5678]"
    ergebnis = Gegenprobe()
    gesehen: set[tuple[int, str, str]] = {(2, "probe.wort", "Beispielwort")}
    gp._pruefe_klartext(_ziele(pseudonyme=("[Akte CD5678]",)), 2, text, ergebnis, gesehen)
    assert ergebnis.resttreffer == [{
        "seite": 3, "art": str(Art.AKTENZEICHEN), "regel": "probe.akte",
        "pseudonym": "[Akte]", "rects": [], "quelle": "plaintext"}]
    assert ergebnis.belassen == []
    assert gesehen == {(2, "probe.wort", "Beispielwort"), (2, "probe.akte", "AB1234")}


def test_klartext_zaehlt_belassenes() -> None:
    ergebnis = Gegenprobe()
    gesehen: set[tuple[int, str, str]] = set()
    gp._pruefe_klartext(_ziele(TOLERIERT), 0, "Beispielwort", ergebnis, gesehen)
    assert ergebnis.resttreffer == [] and ergebnis.belassen == ["probe.wort"]


def test_zusatztext_zaehlt_belassenes_nicht_erneut() -> None:
    ergebnis = Gegenprobe()
    gp._pruefe_zusatztext(_ziele(TOLERIERT, ("[Akte CD5678]",)), 2,
                          "Beispielwort, AB1234, [Akte CD5678]", ergebnis)
    assert _kurz(ergebnis) == [(0, "probe.akte", "zusatztext2")]
    assert ergebnis.belassen == []


@pytest.mark.parametrize(("feld", "wert", "erwartet"), [
    ("format", "PDF 1.7", True),
    ("encryption", None, True),
    ("title", "", True),
    ("author", None, True),
    ("producer", ERLAUBTE_METADATEN["producer"], True),
    ("producer", "anderes Werkzeug", False),
    ("subject", VERMERK_PRAEFIX + " am 01.01.2026", True),
    ("subject", "Befund", False),
    ("keywords", "k", False),
])
def test_metadatum_unbedenklich(feld: str, wert: str | None, erwartet: bool) -> None:
    assert gp._metadatum_unbedenklich(feld, wert) is erwartet


def test_metadaten_und_struktur_blockieren_in_fester_reihenfolge() -> None:
    doc = _dokument("Nur Text.")
    doc.set_metadata({"title": "Probe", "keywords": "k",
                      "producer": ERLAUBTE_METADATEN["producer"],
                      "subject": VERMERK_PRAEFIX + " 1.0"})
    doc.set_xml_metadata("<x/>")
    doc.embfile_add("anhang.txt", b"x")
    doc.set_toc([[1, "Abschnitt", 1]])
    seite = doc[0]
    seite.add_text_annot((20, 20), "Notiz")
    feld = pymupdf.Widget()
    feld.field_name = "feld"
    feld.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    feld.rect = pymupdf.Rect(10, 100, 200, 120)
    seite.add_widget(feld)
    ergebnis = gp.pruefe(doc, REGELN)
    assert ergebnis.resttreffer == [] and not ergebnis.bestanden
    assert not ergebnis.metadaten_leer and not ergebnis.struktur_sauber
    assert ergebnis.anmerkungen == [
        "Metadatum 'title' ist nicht leer", "Metadatum 'keywords' ist nicht leer",
        "XMP-Metadaten vorhanden", "1 eingebettete Datei(en)", "Lesezeichen vorhanden",
        "1 Anmerkung(en) vorhanden", "1 Formularfeld(er) vorhanden",
    ]


def test_pruefe_sammelt_quellen_in_fester_reihenfolge() -> None:
    """Text-layer hits come first, the plain-text pass adds nothing the layer saw,
    companion texts follow with page 0, and tolerated hits are counted once."""
    doc = _dokument("Beispielwort und AB1234.", "Seite zwei mit EF9012.")
    ergebnis = gp.pruefe(doc, REGELN, zusatztexte=["Beispielwort, GH3456"],
                         toleriert=[gp.toleranzschluessel("probe.wort", "Beispielwort")],
                         pseudonyme=["", "[Akte CD5678]"])
    assert _kurz(ergebnis) == [(1, "probe.akte", "text"), (2, "probe.akte", "text"),
                               (0, "probe.akte", "zusatztext1")]
    assert ergebnis.belassen == ["probe.wort"]
    assert ergebnis.geprueft == ["Begriffe", "Aktenzeichen"]
    assert not ergebnis.bestanden


def test_pruefe_besteht_bei_sauberem_dokument() -> None:
    ergebnis = gp.pruefe(_dokument("Nur Text ohne Kennzeichen."), REGELN)
    assert ergebnis.bestanden and ergebnis.anmerkungen == []
