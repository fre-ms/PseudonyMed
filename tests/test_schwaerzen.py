"""Redaction keeps neighbours, detects shifted layout and can fall back to an image."""

from __future__ import annotations

import pymupdf

from pseudonymed.dokument.textebene import lese_seite
from pseudonymed.kern.modell import Art, Fundstelle
from pseudonymed.schwaerzen.pdf import (
    OCR_RAND,
    Schwaerzoptionen,
    _bildband,
    _freie_woerter,
    _sammle_kaesten,
    bereinige,
    lage_intakt,
    rastere_seite,
    schwaerze_seite,
    unsichtbare_textebene,
)

ZEILE = "Herrn Flantz, geb. heute   Pat.-ID 123"


def _dokument(leading: float = 1.2) -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 100.0
    for text in (ZEILE, "Zweite Zeile bleibt unberührt", "Dritte Zeile bleibt auch"):
        page.insert_text((56, y), text, fontsize=10.5, fontname="helv")
        y += 10.5 * leading
    return doc


def _treffer(page: pymupdf.Page, wort: str, pseudonym: str = "Patient A") -> Fundstelle:
    s = lese_seite(page)
    a = s.text.index(wort)
    b = a + len(wort)
    return Fundstelle(0, a, b, wort, Art.NACHNAME, "test", pseudonym,
                      rects=[tuple(r) for r in s.rects(a, b)],
                      schriftgroesse=s.schriftgroesse(a, b))


def test_nachbarn_bleiben_auch_bei_engem_zeilenabstand() -> None:
    for leading in (1.2, 1.0):
        doc = _dokument(leading)
        page = doc[0]
        f = _treffer(page, "Flantz")
        erg = schwaerze_seite(page, lese_seite(page), [f], Schwaerzoptionen())
        assert erg.anzahl == 1 and erg.lage_intakt
        text = page.get_text("text")
        assert "Flantz" not in text
        assert "Zweite Zeile bleibt unberührt" in text
        assert "Dritte Zeile bleibt auch" in text
        assert ", geb. heute" in text and "Pat.-ID 123" in text
        assert "Patient A" in text


def test_pseudonym_wird_sichtbar_gezeichnet() -> None:
    doc = _dokument()
    page = doc[0]
    f = _treffer(page, "Flantz", "[Versichertennummer]")
    schwaerze_seite(page, lese_seite(page), [f], Schwaerzoptionen())
    zeichnungen = page.get_drawings()
    assert any(d.get("fill") for d in zeichnungen)
    # the comma after the name is still rendered to the right of the box
    s = lese_seite(page)
    komma = s.boxen[s.text.index(",")]
    assert komma is not None and komma[0] >= f.rects[0][2] - 0.5


def test_lage_intakt_erkennt_verschiebung() -> None:
    doc = pymupdf.open()
    vorher = doc.new_page()
    vorher.insert_text((56, 100), "Herrn Flantz, geb.", fontsize=10.5, fontname="helv")
    sv = lese_seite(vorher)
    a = sv.text.index("Flantz")
    rect = tuple(sv.rects(a, a + 6)[0])
    f = Fundstelle(0, a, a + 6, "Flantz", Art.NACHNAME, "t", "P", rects=[rect])
    x_komma = sv.boxen[a + 6][0]
    richtig = doc.new_page()
    richtig.insert_text((56, 100), "Herrn ", fontsize=10.5, fontname="helv")
    richtig.insert_text((x_komma, 100), ", geb.", fontsize=10.5, fontname="helv")
    assert lage_intakt(sv, lese_seite(richtig), [f])
    falsch = doc.new_page()
    falsch.insert_text((56, 100), "Herrn ", fontsize=10.5, fontname="helv")
    falsch.insert_text((x_komma - 6.4, 100), ", geb.", fontsize=10.5, fontname="helv")
    assert not lage_intakt(sv, lese_seite(falsch), [f])


def test_rastere_seite_ersetzt_seite_durch_bild() -> None:
    doc = _dokument()
    original = pymupdf.open("pdf", doc.tobytes())
    page = doc[0]
    s = lese_seite(page)
    f = _treffer(page, "Flantz")
    rastere_seite(doc, 0, original, s, [f], Schwaerzoptionen(dpi=72))
    assert len(doc) == 1
    assert doc[0].get_text("text").strip() == ""
    assert doc[0].get_images()
    assert abs(doc[0].rect.width - original[0].rect.width) < 0.1


def test_bereinige_entfernt_struktur() -> None:
    doc = _dokument()
    doc.set_metadata({"title": "geheim", "author": "jemand"})
    doc.set_toc([[1, "Kapitel", 1]])
    doc[0].add_text_annot((300, 60), "Notiz")
    doc.embfile_add("a.txt", b"x")
    bereinige(doc)
    assert doc.metadata["title"] == "" and doc.metadata["author"] == ""
    assert doc.metadata["producer"].startswith("PseudonyMed")
    assert doc.get_toc() == [] and doc.embfile_count() == 0
    assert list(doc[0].annots()) == []


def test_ocr_treffer_loescht_bildpixel() -> None:
    """A hit from OCR on an image-only page blanks the pixels and draws the pseudonym."""
    quelle = _dokument()
    pix = quelle[0].get_pixmap(dpi=150, alpha=False)
    doc = pymupdf.open()
    page = doc.new_page(width=quelle[0].rect.width, height=quelle[0].rect.height)
    page.insert_image(page.rect, pixmap=pix)
    s = lese_seite(quelle[0])
    a = s.text.index("Flantz")
    rect = s.rects(a, a + 6)[0]
    vorher = page.get_pixmap(dpi=150, clip=rect).samples
    f = Fundstelle(0, 0, 0, "Flantz", Art.NACHNAME, "ocr", "Patient A", rects=[tuple(rect)],
                   schriftgroesse=10.0, quelle="ocr")
    erg = schwaerze_seite(page, None, [f], Schwaerzoptionen())
    assert erg.anzahl == 1 and erg.lage_intakt
    nachher = page.get_pixmap(dpi=150, clip=rect).samples
    assert vorher != nachher
    assert "Patient A" in page.get_text()
    assert len(page.get_images()) == 1


def test_einblendung_nutzt_freien_raum_oder_schrumpft() -> None:
    """A pseudonym longer than the hit widens the box (and the removal band) into free
    space to the right; where the next character follows at once, the font shrinks."""
    doc = _dokument()
    page = doc[0]
    s = lese_seite(page)
    opt = Schwaerzoptionen()
    letztes = _treffer(page, "123", "[Patienten-ID K7Q4MX]")  # last word of its line
    plan = _sammle_kaesten(page, s, [letztes], opt)
    e = plan.einblendungen[0]
    assert e.fuell.x1 > letztes.rects[0][2] + 10
    assert e.groesse == letztes.schriftgroesse
    assert plan.baender[0][0].x1 == e.fuell.x1
    assert not plan.ohne_lagepruefung and not plan.nur_pixel and not plan.flaechen
    eng = _treffer(page, "Flantz", "[Versichertennummer K7Q4MX]")  # the comma follows
    plan = _sammle_kaesten(page, s, [eng], opt)
    e = plan.einblendungen[0]
    assert e.fuell.x1 <= eng.rects[0][2] + 0.01
    assert e.groesse < eng.schriftgroesse


def test_bildband_polstert_und_beschneidet() -> None:
    """A box on image pixels grows by the OCR margin (sideways at least 15 % of its
    height) and never leaves the page."""
    doc = pymupdf.open()
    page = doc.new_page()
    flach = _bildband(page, pymupdf.Rect(100, 100, 150, 110), None)
    assert flach.auf_bild and flach.band == flach.flaeche
    assert flach.flaeche == pymupdf.Rect(100 - OCR_RAND, 100 - OCR_RAND,
                                         150 + OCR_RAND, 110 + OCR_RAND)
    hoch = _bildband(page, pymupdf.Rect(-5, 0, 50, 40), None)
    assert (hoch.flaeche.x0, hoch.flaeche.y0, hoch.flaeche.x1) == (0, 0, 56)


def test_unsichtbare_textebene_laesst_treffer_aus() -> None:
    """The invisible layer carries every word except those a hit covers (by text span or
    by box), plus the pseudonyms on request, and leaves the rendering untouched."""
    quelle = _dokument()
    s = lese_seite(quelle[0])
    name = _treffer(quelle[0], "Flantz")
    a = s.text.index("Zweite")
    kasten = Fundstelle(0, 0, 0, "Zweite", Art.NACHNAME, "ocr", "[Ort K7Q4MX]",
                        rects=[tuple(s.rects(a, a + 6)[0])], quelle="ocr")
    frei = [wort for wort, _rect in _freie_woerter(s, [name, kasten])]
    assert "Flantz," not in frei and "Zweite" not in frei
    assert {"Pat.-ID", "123", "auch"} <= set(frei)
    doc = pymupdf.open()
    page = doc.new_page(width=s.breite, height=s.hoehe)
    page.insert_image(page.rect, pixmap=quelle[0].get_pixmap(dpi=100, alpha=False))
    vorher = page.get_pixmap(dpi=72, alpha=False).samples
    anzahl = unsichtbare_textebene(page, s, [name, kasten], mit_pseudonymen=True)
    assert anzahl == len(frei) + 2
    text = page.get_text()
    assert "Flantz" not in text and "Zweite" not in text
    assert "Patient A" in text and "[Ort K7Q4MX]" in text and "auch" in text
    assert page.get_pixmap(dpi=72, alpha=False).samples == vorher
