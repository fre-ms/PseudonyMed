"""Medical vocabulary: matching, protection of NER and fuzzy search, import."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import pymupdf
import pytest
from optdoc.ocr import OCRWord, page_text_from_words
from typer.testing import CliRunner

from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import ner as nermodul
from pseudonymed.erkennung.unscharf import finde_unscharf, ziele_fuer
from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


def _klein() -> fachmodul.Fachbegriffe:
    fb = fachmodul.Fachbegriffe()
    fb.fuege_hinzu(["Bauch", "Herz", "Morbus Parkinson", "arterielle Hypertonie", "Hypertonie",
                    "Ramipril", "Babinski-Zeichen", "Mini-Mental-Status-Test", "Alzheimer",
                    "Parkinson"], "test")
    return fb


def test_einzelwort_flexion_und_bindestrich() -> None:
    fb = _klein()
    for w in ("Bauch", "BAUCH", "Bauches", "Herzen", "Ramipril", "Parkinson-Syndrom",
              "Alzheimer-Demenz", "Babinski", "Mini-Mental-Status-Test", "Hypertonie"):
        assert fb.ist_fachbegriff(w), w
    for w in ("Bauer", "Baur", "Flantz", "Herzog", "Bau"):
        assert not fb.ist_fachbegriff(w), w


def test_spannen_mehrwort() -> None:
    fb = _klein()
    text = "Diagnosen: Morbus Parkinson, arterielle Hypertonie; Bauch weich, Herz rhythmisch."
    spannen = fb.spannen(text)
    teile = [text[a:b] for a, b in spannen]
    assert teile == ["Morbus Parkinson", "arterielle Hypertonie", "Bauch", "Herz"]
    assert fb.geschuetzt(text, text.index("Morbus"), text.index("Morbus") + 6, spannen)
    assert fb.geschuetzt(text, text.index("Bauch"), text.index("Bauch") + 5, spannen, ganz=True)
    assert not fb.geschuetzt(text, 0, 9, spannen)


def test_basisvokabular_geladen() -> None:
    fb = fachmodul.standard()
    assert fb.anzahl > 20000
    for w in ("Alzheimer", "Hypertonie", "Ramipril", "Lumbalpunktion", "Bauch"):
        assert fb.ist_fachbegriff(w), w
    for w in ("Berlin", "Flantz", "Gotthard", "Dresden", "Müller", "Hildegund", "Briemer"):
        assert not fb.ist_fachbegriff(w), w
    text = "Morbus Parkinson wurde in Berlin diagnostiziert."
    assert [text[a:b] for a, b in fb.spannen(text)] == ["Morbus Parkinson"]
    assert fb.geschuetzt(text, 0, 16, ganz=True) and not fb.geschuetzt(text, 0, 23, ganz=True)


def test_unscharfe_suche_verschont_fachbegriffe() -> None:
    """Patient Hertz: "Herz" (one edit away) is an organ, "Hertx" is a misread name."""
    p = Patient(nachname="Hertz", vorname="Karl", geburtsdatum=dt.date(1950, 1, 2))
    v = Pseudonymvergabe(p, "K7Q4MX", geheimnis=bytes(32))
    woerter = [OCRWord(w, 50 + 60 * i, 100, 100 + 60 * i, 112)
               for i, w in enumerate(["Das", "Herz", "schlägt", "ruhig", "Hertx", "kam"])]
    s = page_text_from_words(woerter, 0, 595, 842)
    ohne = {f.text for f in finde_unscharf(s, ziele_fuer(p, v), [])}
    mit = {f.text for f in finde_unscharf(s, ziele_fuer(p, v), [], _klein())}
    assert "Herz" in ohne and "Hertx" in ohne
    assert "Herz" not in mit and "Hertx" in mit


@pytest.mark.skipif(not nermodul.verfuegbar(), reason="spaCy oder Modell fehlt")
def test_ner_verschont_fachbegriffe(patient: Patient) -> None:
    from optdoc.pagetext import read_text_layer

    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for zeile in ("Verdacht auf Morbus Wilson und Morbus Parkinson, Babinski positiv.",
                  "Die Tochter Erika Hoffmann berichtet über Stürze in Dresden."):
        page.insert_text((56, y), zeile, fontsize=10.5, fontname="helv")
        y += 14
    seite = read_text_layer(page)
    allein = dataclasses.replace(patient, angehoerige=[], zuweiser=[])
    v = Pseudonymvergabe(allein, "K7Q4MX", geheimnis=bytes(32))
    verschont: list[str] = []
    treffer = nermodul.finde_ner(seite, nermodul.NER(), v, [], "text", fachmodul.standard(),
                                 verschont)
    texte = {f.text for f in treffer}
    assert "Erika Hoffmann" in texte and "Dresden" in texte
    assert not any("Wilson" in t or "Parkinson" in t or "Babinski" in t for t in texte)


def test_import_und_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path))
    fachmodul.standard.cache_clear()
    liste = tmp_path / "eigene.txt"
    liste.write_text("Flantzitis\nMusterkrankheit\n", encoding="utf-8")
    csv = tmp_path / "icd.csv"
    csv.write_text("A00;Cholera;x\nA01;Typhus abdominalis;y\n", encoding="utf-8")
    mesh = tmp_path / "mesh.xml"
    mesh.write_text('<DescriptorRecordSet><DescriptorRecord><DescriptorName><String>Gurkenkrankheit'
                    '</String></DescriptorName><ConceptList><Concept><TermList><Term><String>'
                    'Cucumis-Syndrom</String></Term></TermList></Concept></ConceptList>'
                    '</DescriptorRecord></DescriptorRecordSet>', encoding="utf-8")
    fachmodul.importiere(liste, "eigene")
    fachmodul.importiere(csv, "icd", "csv", spalte=1)
    fachmodul.importiere(mesh, "mesh", "mesh-xml")
    fb = fachmodul.standard()
    for w in ("Flantzitis", "Typhus abdominalis", "Cucumis-Syndrom", "Gurkenkrankheit"):
        assert fb.geschuetzt(w, 0, len(w), ganz=True), w
    assert any(name == "eigene" for name, _n in fb.quellen)

    from pseudonymed.cli import app

    r = CliRunner().invoke(app, ["fachbegriffe", "status"])
    assert r.exit_code == 0 and "eigene" in r.output and "vokabular" in r.output
    r = CliRunner().invoke(app, ["fachbegriffe", "pruefen", "Flantzitis", "Flantz",
                                 "Morbus Parkinson"])
    assert "Flantzitis: geschützt" in r.output and "Flantz: frei" in r.output
    assert "Morbus Parkinson: geschützt" in r.output
    r = CliRunner().invoke(app, ["fachbegriffe", "importieren", str(liste), "--name", "zwei"])
    assert r.exit_code == 0 and "0 Begriffe" in r.output  # already there under "eigene"
    r = CliRunner().invoke(app, ["fachbegriffe", "hinzufuegen", "Flantzose", "--kategorie", "x"])
    assert r.exit_code == 0 and "1 neue" in r.output
    assert fachmodul.standard().ist_fachbegriff("Flantzose")
    r = CliRunner().invoke(app, ["fachbegriffe", "entfernen", "Flantzose", "Cholera"])
    assert r.exit_code == 0 and "2 Begriffe gelöscht" in r.output
    assert not fachmodul.standard().ist_fachbegriff("Flantzose")
    r = CliRunner().invoke(app, ["fachbegriffe", "exportieren", str(tmp_path / "v.csv"),
                                 "--kategorie", "eigene"])
    assert r.exit_code == 0 and (tmp_path / "v.csv").read_text(encoding="utf-8").startswith(
        "begriff,kategorie\nFlantzitis,eigene\n")
    fachmodul.standard.cache_clear()


def test_kurze_woerter_nur_grossgeschrieben() -> None:
    fb = fachmodul.standard()
    text = "Er hat Schmerzen im Arm und am Ohr; der Test HAT war positiv."
    woerter = {text[a:b] for a, b in fb.spannen(text)}
    assert "hat" not in woerter and {"Arm", "Ohr", "HAT"} <= woerter


def test_koerperteile_sind_fachbegriffe() -> None:
    """Body parts in everyday German are protected, including the side words, so that
    NER cannot take "Linke Hand" or "Oberbauch" for a person."""
    fb = fachmodul.standard()
    for w in ("Kopf", "Nacken", "Oberbauch", "Unterschenkel", "Wade", "Knöchel",
              "Lendenwirbelsäule", "Handfläche", "Schläfe", "Leiste", "Bein", "Hüfte"):
        assert fb.ist_fachbegriff(w), w
    text = "Schmerzen in der linken Hand und im rechten Oberbauch, Knie frei."
    woerter = {text[a:b] for a, b in fb.spannen(text)}
    assert {"linken", "Hand", "rechten", "Oberbauch", "Knie"} <= woerter
    assert "Körperteile" in [q for q, _n in fb.quellen] if hasattr(fb, "quellen") else True


def test_befundbegriffe_sind_fachbegriffe() -> None:
    """Terms of the clinical examination that stand at the start of findings lines."""
    fb = fachmodul.standard()
    for w in ("Konzentration", "Haltung", "Nierenlager", "Strichgang", "Gangbild",
              "Merkfähigkeit", "Darmgeräusche", "Procedere", "Finger-Nase-Versuch"):
        assert fb.ist_fachbegriff(w), w
    assert "Befundbegriffe" in [q for q, _n in fb.quellen]
    text = "Konzentration: gut. Nierenlager: frei. Erika Hoffmann wohnt in Riesa."
    woerter = {text[a:b] for a, b in fb.spannen(text)}
    assert {"Konzentration", "Nierenlager"} <= woerter and "Riesa" not in woerter
