"""PseudonyMed's adapter to optdoc: its settings as optdoc reads them, PseudonyMed's names
of the engines and paper sizes, optdoc's wording and messages in the language of the
user, and the hits for unreadable lines."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import optdoc
import pytest
from optdoc import cleanup, ocr
from optdoc.ocr import OCRWord, page_text_from_words
from optdoc.settings import load_settings

import pseudonymed.konfig as konfig
from pseudonymed.dokument import anbindung

# the modules of optdoc whose errors can reach PseudonyMed's user
GENUTZTE_MODULE = ("loading", "cleanup", "ocr", "pagetext", "structure", "alignment")


def _einstellungen(monkeypatch: pytest.MonkeyPatch, backend: str, pfad: str = "",
                   sprachen: str = "") -> None:
    monkeypatch.setattr(konfig, "lade_einstellungen", lambda *a, **k: SimpleNamespace(
        ocr_backend=backend, tesseract_pfad=pfad, tesseract_sprachen=sprachen))


def test_optdoc_liest_die_eigenen_einstellungen(monkeypatch: pytest.MonkeyPatch) -> None:
    """optdoc reads PseudonyMed's settings anew on every access, "aus" in its spelling."""
    _einstellungen(monkeypatch, "aus", "/opt/tesseract", "deu+eng")
    s = load_settings()
    assert (s.ocr_backend, s.tesseract_path, s.tesseract_languages) == (
        "none", "/opt/tesseract", "deu+eng")
    assert anbindung.backend("AUTO") is None and anbindung.backend("") is None
    _einstellungen(monkeypatch, "Tesseract")
    assert load_settings().ocr_backend == "tesseract"


def test_engines_mit_pseudonymeds_namen(monkeypatch: pytest.MonkeyPatch) -> None:
    """"aus" is no engine; an unknown or missing engine is worded for the user."""
    assert anbindung.backend("aus") is None and anbindung.backend("AUS") is None
    with pytest.raises(anbindung.OCRFehler) as unbekannt:
        anbindung.backend("Zauber")
    assert anbindung.meldung(unbekannt.value) == (
        "Unbekanntes OCR-Backend: 'zauber' (auto, rapidocr, tesseract, aus).")
    assert anbindung.meldung(unbekannt.value, "en_gb").startswith("unknown OCR engine: 'zauber'")
    monkeypatch.setattr(ocr, "tesseract_program", lambda *_a, **_k: None)
    with pytest.raises(anbindung.OCRFehler) as fehlt:
        anbindung.backend("tesseract")
    assert anbindung.meldung(fehlt.value) == (
        "Tesseract ist nicht installiert (Programm 'tesseract' fehlt).")


def test_papierformate_mit_pseudonymeds_namen() -> None:
    """"bild" is a page as large as the image; an unknown size is worded for the user."""
    assert anbindung.papierformat("bild") is None and anbindung.papierformat("Bild") is None
    assert anbindung.papierformat("US Letter") == (612.0, 792.0)
    assert anbindung.papierformat(None) == anbindung.papierformat("a4")
    with pytest.raises(ValueError) as fehler:
        anbindung.papierformat("B5")
    assert anbindung.meldung(fehler.value) == (
        "Unbekanntes Papierformat: 'B5' (a4, a5, a3, letter, legal, bild).")


def test_beschreibung_folgt_der_sprache() -> None:
    plan = cleanup.CleanupPlan(rotation=90)
    assert anbindung.beschreibung(plan) == "um 90° gedreht"
    assert anbindung.beschreibung(plan, "en_gb") == "rotated by 90°"


def test_meldungen_in_beiden_sprachen() -> None:
    """optdoc's messages in German, the variable part carried over; in English unchanged;
    a message that is not optdoc's stays as it is."""
    e = ocr.OCRError("Tesseract reports an error: Error opening data file\nmehr")
    assert anbindung.meldung(e) == "Tesseract meldet einen Fehler: Error opening data file\nmehr"
    assert anbindung.meldung(e, "en") == str(e)
    assert anbindung.meldung(ValueError("page text 3: the character lists do not fit the text")) \
        == "Seitentext 3: Zeichenlisten passen nicht zum Text."
    assert anbindung.meldung(ValueError("Leere Auswahl.")) == "Leere Auswahl."


def _beispiel(knoten: ast.expr) -> str | None:
    """The text a raised message would have, with "1" for every variable part; None for
    a message that is not written out in the raise."""
    if isinstance(knoten, ast.Constant) and isinstance(knoten.value, str):
        return knoten.value
    if isinstance(knoten, ast.JoinedStr):
        return "".join(t.value if isinstance(t, ast.Constant) else "1" for t in knoten.values)
    return None


def _optdocs_meldungen() -> list[str]:
    """Every message the used optdoc modules raise, as an example text."""
    ordner = Path(optdoc.__file__).parent
    meldungen = []
    for name in GENUTZTE_MODULE:
        for knoten in ast.walk(ast.parse((ordner / f"{name}.py").read_text(encoding="utf-8"))):
            if isinstance(knoten, ast.Raise) and isinstance(knoten.exc, ast.Call) \
                    and knoten.exc.args:
                text = _beispiel(knoten.exc.args[0])
                if text is not None:
                    meldungen.append(text)
    return meldungen


def test_jede_meldung_von_optdoc_hat_einen_deutschen_wortlaut() -> None:
    """Guard for a new optdoc version: a message without a German wording would reach the
    German interface in English."""
    meldungen = _optdocs_meldungen()
    assert len(meldungen) >= 10
    ohne = [m for m in meldungen if anbindung.meldung(ValueError(m)) == m]
    assert ohne == []


def test_unlesbare_zeile_wird_zum_treffer() -> None:
    """A line of fragments read with low confidence becomes one hit "[unlesbar]" over the
    whole line; the readable line below it stays."""
    fragmente = ["G6ot", "n", "e", "n", "e", "i", "l", "e", "5g"]
    woerter = [OCRWord(w, 50 + 30 * i, 100, 70 + 30 * i, 112, k)
               for i, (w, k) in enumerate(zip(fragmente, [0.87, 0.5, 0.99, 0.9, 0.25, 0.5, 0.47,
                                                           0.97, 0.81], strict=True))]
    woerter += [OCRWord(w, 50 + 40 * i, 130, 85 + 40 * i, 142, 0.99)
                for i, w in enumerate(["in", "Riesa.", "Flantz,", "berichtet"])]
    s = page_text_from_words(woerter, 0, 595, 842)
    treffer = anbindung.unlesbare_treffer(s)
    assert len(treffer) == 1
    r = treffer[0]
    assert (r.pseudonym, r.quelle, r.regel, r.seite) == ("[unlesbar]", "ocr", "ocr.unlesbar", 0)
    assert s.text[r.start:r.ende] == r.text == "G6ot n e n e i l e 5g"
    assert r.rects and r.schriftgroesse > 0
