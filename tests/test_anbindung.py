"""PseudonyMed's adapter to optdoc: its settings as optdoc reads them, the German names of
the engines and paper sizes, German messages, the clean-up wording, unreadable lines."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from optdoc import cleanup, ocr
from optdoc.ocr import OCRWord, page_text_from_words
from optdoc.settings import load_settings

import pseudonymed.konfig as konfig
from pseudonymed.dokument import anbindung


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
    _einstellungen(monkeypatch, "zauber")
    assert load_settings().ocr_backend == "zauber"  # optdoc then falls back to "auto"


def test_engines_mit_deutschen_namen_und_meldungen(monkeypatch: pytest.MonkeyPatch) -> None:
    """"aus" is no engine; unknown and missing engines are refused in German."""
    assert anbindung.backend("aus") is None
    with pytest.raises(anbindung.OCRFehler, match="Unbekanntes OCR-Backend: 'Zauber'"):
        anbindung.backend("Zauber")
    monkeypatch.setattr(ocr.TesseractBackend, "available", staticmethod(lambda: False))
    with pytest.raises(anbindung.OCRFehler, match="Tesseract ist nicht installiert"):
        anbindung.backend("tesseract")
    monkeypatch.setattr(ocr.RapidOCRBackend, "available", staticmethod(lambda: False))
    with pytest.raises(anbindung.OCRFehler, match="RapidOCR ist nicht installiert"):
        anbindung.backend("rapidocr")


def test_papierformate_mit_deutschen_namen() -> None:
    """"bild" is a page as large as the image; an unknown size is refused in German."""
    assert anbindung.papierformat("bild") is None and anbindung.papierformat("Bild") is None
    assert anbindung.papierformat("US Letter") == (612.0, 792.0)
    assert anbindung.papierformat(None) == anbindung.papierformat("a4")
    with pytest.raises(ValueError, match="Unbekanntes Papierformat: 'B5'"):
        anbindung.papierformat("B5")


def test_beschreibung_folgt_der_sprache() -> None:
    plan = cleanup.CleanupPlan(rotation=90)
    assert anbindung.beschreibung(plan) == "um 90° gedreht"
    assert anbindung.beschreibung(plan, "en_gb") == "rotated by 90°"


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
