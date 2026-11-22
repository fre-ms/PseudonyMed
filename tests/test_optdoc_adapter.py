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

import pseudonymed.config as config
from pseudonymed.document import optdoc_adapter

# the modules of optdoc whose errors can reach PseudonyMed's user
USED_MODULES = ("loading", "cleanup", "ocr", "pagetext", "structure", "alignment")


def _settings(monkeypatch: pytest.MonkeyPatch, backend: str, path: str = "",
              languages: str = "") -> None:
    monkeypatch.setattr(config, "load_settings", lambda *a, **k: SimpleNamespace(
        ocr_backend=backend, tesseract_path=path, tesseract_languages=languages))


def test_optdoc_reads_its_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """optdoc reads PseudonyMed's settings anew on every access, "aus" in its spelling."""
    _settings(monkeypatch, "aus", "/opt/tesseract", "deu+eng")
    s = load_settings()
    assert (s.ocr_backend, s.tesseract_path, s.tesseract_languages) == (
        "none", "/opt/tesseract", "deu+eng")
    assert optdoc_adapter.backend("AUTO") is None and optdoc_adapter.backend("") is None
    _settings(monkeypatch, "Tesseract")
    assert load_settings().ocr_backend == "tesseract"


def test_engines_with_pseudonymeds_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """"aus" is no engine; an unknown or missing engine is worded for the user."""
    assert optdoc_adapter.backend("aus") is None and optdoc_adapter.backend("AUS") is None
    with pytest.raises(optdoc_adapter.OCRError) as unknown:
        optdoc_adapter.backend("Zauber")
    assert optdoc_adapter.message(unknown.value) == (
        "Unbekanntes OCR-Backend: 'zauber' (auto, rapidocr, tesseract, aus).")
    assert optdoc_adapter.message(unknown.value, "en_gb").startswith("unknown OCR engine: 'zauber'")
    monkeypatch.setattr(ocr, "tesseract_program", lambda *_a, **_k: None)
    with pytest.raises(optdoc_adapter.OCRError) as missing:
        optdoc_adapter.backend("tesseract")
    assert optdoc_adapter.message(missing.value) == (
        "Tesseract ist nicht installiert (Programm 'tesseract' fehlt).")


def test_paper_formats_with_pseudonymeds_names() -> None:
    """"bild" is a page as large as the image; an unknown size is worded for the user."""
    assert optdoc_adapter.paper_size("bild") is None and optdoc_adapter.paper_size("Bild") is None
    assert optdoc_adapter.paper_size("US Letter") == (612.0, 792.0)
    assert optdoc_adapter.paper_size(None) == optdoc_adapter.paper_size("a4")
    with pytest.raises(ValueError) as error:
        optdoc_adapter.paper_size("B5")
    assert optdoc_adapter.message(error.value) == (
        "Unbekanntes Papierformat: 'B5' (a4, a5, a3, letter, legal, bild).")


def test_description_follows_the_language() -> None:
    plan = cleanup.CleanupPlan(rotation=90)
    assert optdoc_adapter.description(plan) == "um 90° gedreht"
    assert optdoc_adapter.description(plan, "en_gb") == "rotated by 90°"


def test_messages_in_both_languages() -> None:
    """optdoc's messages in German, the variable part carried over; in English unchanged;
    a message that is not optdoc's stays as it is."""
    e = ocr.OCRError("Tesseract reports an error: Error opening data file\nmehr")
    assert optdoc_adapter.message(e) == ("Tesseract meldet einen Fehler: Error opening data"
                                         " file\nmehr")
    assert optdoc_adapter.message(e, "en") == str(e)
    mismatch = ValueError("page text 3: the character lists do not fit the text")
    assert optdoc_adapter.message(mismatch) == "Seitentext 3: Zeichenlisten passen nicht zum Text."
    assert optdoc_adapter.message(ValueError("Leere Auswahl.")) == "Leere Auswahl."


def _example(node: ast.expr) -> str | None:
    """The text a raised message would have, with "1" for every variable part; None for
    a message that is not written out in the raise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(t.value if isinstance(t, ast.Constant) else "1" for t in node.values)
    return None


def _optdoc_messages() -> list[str]:
    """Every message the used optdoc modules raise, as an example text."""
    folder = Path(optdoc.__file__).parent
    messages = []
    for name in USED_MODULES:
        for node in ast.walk(ast.parse((folder / f"{name}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                    and node.exc.args:
                text = _example(node.exc.args[0])
                if text is not None:
                    messages.append(text)
    return messages


def test_every_optdoc_message_has_a_german_wording() -> None:
    """Guard for a new optdoc version: a message without a German wording would reach the
    German interface in English."""
    messages = _optdoc_messages()
    assert len(messages) >= 10
    without = [m for m in messages if optdoc_adapter.message(ValueError(m)) == m]
    assert without == []


def test_unreadable_line_becomes_a_match() -> None:
    """A line of fragments read with low confidence becomes one hit "[unlesbar]" over the
    whole line; the readable line below it stays."""
    fragments = ["G6ot", "n", "e", "n", "e", "i", "l", "e", "5g"]
    words = [OCRWord(w, 50 + 30 * i, 100, 70 + 30 * i, 112, k)
             for i, (w, k) in enumerate(zip(fragments, [0.87, 0.5, 0.99, 0.9, 0.25, 0.5, 0.47,
                                                           0.97, 0.81], strict=True))]
    words += [OCRWord(w, 50 + 40 * i, 130, 85 + 40 * i, 142, 0.99)
              for i, w in enumerate(["in", "Riesa.", "Flantz,", "berichtet"])]
    s = page_text_from_words(words, 0, 595, 842)
    matches = optdoc_adapter.unreadable_matches(s)
    assert len(matches) == 1
    r = matches[0]
    assert (r.pseudonym, r.source, r.rule, r.page) == ("[unlesbar]", "ocr", "ocr.unlesbar", 0)
    assert s.text[r.start:r.end] == r.text == "G6ot n e n e i l e 5g"
    assert r.rects and r.font_size > 0
