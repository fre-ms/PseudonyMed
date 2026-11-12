"""End-to-end: synthetic report in, pseudonymised PDF plus text and report out, check passes."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pymupdf
import pytest

from pseudonymed.ablauf import Ablaufoptionen, abschliessen, abschluss, pseudonymisiere, vorbereiten
from pseudonymed.kern.modell import Patient
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.kennung import kennung_gueltig
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.schwaerzen.pdf import Schwaerzoptionen, Seitenergebnis, rastere
from tests.conftest import TESTSCHLUESSEL
from tests.synthetisch import datum_lang, erzeuge_befund


def klardaten(p: Patient) -> list[str]:
    geb = p.geburtsdatum
    assert geb is not None
    return [
        p.nachname, p.vorname, p.nachname.upper(), p.strasse, p.ort, p.plz + " " + p.ort,
        geb.strftime("%d.%m.%Y"), geb.strftime("%d.%m.%y"), datum_lang(geb),
        p.versichertennummer, p.patienten_id, p.emails[0], p.aktenzeichen[0],
        re.sub(r"\D", "", p.telefone[0])[-6:],
        p.angehoerige[0].vorname, p.zuweiser[0].nachname, p.zuweiser[0].strasse,
    ]


def gefundene_klardaten(text: str, p: Patient) -> list[str]:
    flach = re.sub(r"[\s\-/().]+", "", text).casefold()
    return [k for k in klardaten(p) if re.sub(r"[\s\-/().]+", "", k).casefold() in flach]


def test_rundlauf(befund: Path, patient: Patient, speicher: Pseudonymspeicher) -> None:
    erg = pseudonymisiere(befund, patient, Ablaufoptionen(), speicher=speicher)
    assert erg.freigegeben, als_text(erg.bericht)
    assert erg.bericht.bestanden
    k = erg.kennung
    assert len(k) == 6 and kennung_gueltig(k)
    assert erg.ziel_pdf and erg.ziel_pdf.exists()
    assert erg.ziel_txt and erg.ziel_txt.exists()
    assert erg.ziel_bericht and erg.ziel_bericht.exists()

    with pymupdf.open(erg.ziel_pdf) as d:
        pdf_text = "\n".join(p.get_text("text") for p in d)
        assert d.metadata["title"] == "" and d.metadata["author"] == ""
        assert d.metadata["subject"].startswith("pseudonymisiert mit PseudonyMed")
        assert d.get_toc() == []
        assert d.embfile_count() == 0
        assert all(len(list(p.annots())) == 0 for p in d)
    assert gefundene_klardaten(pdf_text, patient) == []
    txt = erg.ziel_txt.read_text(encoding="utf-8")
    assert gefundene_klardaten(txt, patient) == []
    # the actual content stays untouched
    assert re.search(r"\[Straße [A-Z2-9]{6}\]", txt) and re.search(r"\[PLZ Ort [A-Z2-9]{6}\]", txt)
    assert "[Wohnort]" not in txt
    for erwartet in (f"Patient {k}", f"[geb. {patient.geburtsdatum.year}]", "Dr. Z1",
                     f"Angehörige {k}-1", "[Versichertennummer]", "max. 3 Tabletten",
                     "Ramipril 5 mg",
                     "Sehr geehrte Frau Kollegin Dr. Z1,", "=== Seite 2 ==="):
        assert erwartet in txt, erwartet
    assert f"Patient {k}" in pdf_text and "Ramipril 5 mg" in pdf_text
    # the report carries no clear data
    bericht = erg.ziel_bericht.read_text(encoding="utf-8")
    assert gefundene_klardaten(bericht, patient) == []
    daten = json.loads(bericht)
    assert daten["bestanden"] is True and daten["seiten"] == 2
    assert len(daten["fundstellen"]) >= 15
    # the key is in the local store only
    z = speicher.zuordnung(patient.schluessel())
    assert z[f"Patient {k}"] == patient.vollname
    assert z["[E-Mail]"] == patient.emails[0]
    assert patient.strasse in z.values() and f"{patient.plz} {patient.ort}" in z.values()
    # the referrer's practice address has its own street code
    assert sum(1 for p in z if p.startswith("[Straße ")) == 2


def test_kennungen_je_person(tmp_path: Path, patient: Patient, patientin: Patient,
                             speicher: Pseudonymspeicher) -> None:
    a = pseudonymisiere(erzeuge_befund(tmp_path / "a.pdf", patient), patient, speicher=speicher)
    b = pseudonymisiere(erzeuge_befund(tmp_path / "b.pdf", patientin), patientin, speicher=speicher)
    a2 = pseudonymisiere(erzeuge_befund(tmp_path / "a2.pdf", patient), patient, speicher=speicher)
    assert a.kennung == a2.kennung != b.kennung
    assert f"Patientin {b.kennung}" in b.text and f"Patient {a.kennung}" in a2.text


def test_buchstabenschema_im_ablauf(befund: Path, patient: Patient,
                                    buchstabenspeicher: Pseudonymspeicher) -> None:
    erg = pseudonymisiere(befund, patient, speicher=buchstabenspeicher)
    assert erg.kennung == "A" and "Patient A" in erg.text and "Angehörige A-1" in erg.text
    assert "[Straße " in erg.text  # address codes do not depend on the identifier scheme


def test_schreibvariante_bekommt_gleiche_kennung(tmp_path: Path, patient: Patient,
                                                 speicher: Pseudonymspeicher) -> None:
    import dataclasses

    erg = pseudonymisiere(erzeuge_befund(tmp_path / "a.pdf", patient), patient, speicher=speicher)
    variante = dataclasses.replace(patient, nachname=patient.nachname + "z",
                                   vorname=patient.vorname.upper())
    erg2 = pseudonymisiere(erzeuge_befund(tmp_path / "b.pdf", variante), variante,
                           speicher=speicher)
    assert erg2.kennung == erg.kennung
    assert any("zugeordnet" in w for w in erg2.bericht.warnungen)


def test_alter_statt_jahr(befund: Path, patient: Patient, speicher: Pseudonymspeicher) -> None:
    erg = pseudonymisiere(befund, patient, Ablaufoptionen(datumsform="alter"), speicher=speicher)
    assert erg.freigegeben
    heute = dt.date.today()
    alter = heute.year - patient.geburtsdatum.year - (
        (heute.month, heute.day) < (patient.geburtsdatum.month, patient.geburtsdatum.day))
    assert f"[Alter {alter}]" in erg.text


def test_rastern(befund: Path, patient: Patient, speicher: Pseudonymspeicher) -> None:
    opt = Ablaufoptionen(schwaerzen=Schwaerzoptionen(rastern=True, dpi=72))
    erg = pseudonymisiere(befund, patient, opt, speicher=speicher)
    assert erg.freigegeben
    with pymupdf.open(erg.ziel_pdf) as d:
        # rasterised, but with an invisible text layer: words and pseudonyms, no clear data
        texte = [p.get_text("text") for p in d]
        assert all(t.strip() for t in texte) and len(d) == 2
        assert gefundene_klardaten("\n".join(texte), patient) == []
    assert any("gerastert" in w for w in erg.bericht.hinweise)


def test_gegenprobe_blockiert(befund: Path, patient: Patient, speicher: Pseudonymspeicher,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abschluss, "schwaerze_seite", lambda *a, **k: Seitenergebnis(0, True))
    erg = pseudonymisiere(befund, patient, speicher=speicher)
    assert not erg.freigegeben
    assert erg.ziel_pdf is None and erg.ziel_txt is None
    assert not (befund.parent / "befund_pseudonym.pdf").exists()
    assert erg.bericht.gegenprobe is not None and erg.bericht.gegenprobe.resttreffer
    assert any("Nicht freigegeben" in w for w in erg.bericht.warnungen)
    assert "NICHT bestanden" in als_text(erg.bericht)


def test_scan_ohne_ocr_wird_nicht_freigegeben(tmp_path: Path, befund: Path, patient: Patient,
                                               speicher: Pseudonymspeicher) -> None:
    with pymupdf.open(befund) as d:
        scan = rastere(d, dpi=72)
        scan_pfad = tmp_path / "scan.pdf"
        scan.save(scan_pfad)
        scan.close()
    erg = pseudonymisiere(scan_pfad, patient, Ablaufoptionen(ocr="aus"), speicher=speicher)
    assert not erg.freigegeben
    assert not erg.bericht.textebene
    assert any("ohne Textebene" in w for w in erg.bericht.warnungen)


def test_bild_ohne_ocr_wird_nicht_freigegeben(tmp_path: Path, befund: Path, patient: Patient,
                                               speicher: Pseudonymspeicher) -> None:
    with pymupdf.open(befund) as d:
        bild = tmp_path / "foto.png"
        d[0].get_pixmap(dpi=72).save(bild)
    erg = pseudonymisiere(bild, patient, Ablaufoptionen(ocr="aus"), speicher=speicher)
    assert not erg.freigegeben
    assert erg.bericht.seiten == 1


def test_zwei_phasen_mit_verwerfen_und_kasten(befund: Path, patient: Patient,
                                             speicher: Pseudonymspeicher) -> None:
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    assert v.fundstellen and not v.ocr_seiten
    assert v.seitenbild(0)[:4] == b"\x89PNG"
    # drop the hit on the referrer's name on page 1 (a false positive, say)
    zuweiser = [f for f in v.fundstellen if f.regel == "zuweiser1.name"]
    assert zuweiser
    for f in zuweiser:
        v.verwerfe(f)
    # a manual box over the diagnosis line of page 1
    with pymupdf.open(befund) as d:
        rect = d[0].search_for("Leichte kognitive Störung")[0]
    neue = v.kasten(0, (rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1), "[Diagnose]")
    assert len(neue) == 1 and neue[0].text == "Leichte kognitive Störung"
    assert neue[0].quelle == "manuell"
    erg = abschliessen(v)
    assert erg.freigegeben, als_text(erg.bericht)
    assert "[Diagnose]" in erg.text and "Leichte kognitive Störung" not in erg.text
    assert patient.zuweiser[0].nachname in erg.text  # dropped hit stays in the text
    with pymupdf.open(erg.ziel_pdf) as d:
        text = d[0].get_text()
        assert "[Diagnose]" in text and "Leichte kognitive Störung" not in text
        assert "ICD-10 F06.7" in text  # the rest of the line survived the manual box
    assert not any("gerastert" in w for w in erg.bericht.hinweise)
    with pytest.raises(RuntimeError):
        abschliessen(v)


def test_kasten_auf_bildseite(tmp_path: Path, befund: Path, patient: Patient,
                              speicher: Pseudonymspeicher) -> None:
    with pymupdf.open(befund) as d:
        scan = rastere(d, dpi=72)
        scan_pfad = tmp_path / "scan.pdf"
        scan.save(scan_pfad)
        scan.close()
    v = vorbereiten(scan_pfad, patient, Ablaufoptionen(ocr="aus"), speicher=speicher)
    neue = v.kasten(0, (50, 60, 300, 120))
    assert len(neue) == 1 and neue[0].rects == [(50, 60, 300, 120)] and neue[0].text == ""
    v.schliessen()


def test_gegenprobe_schuetzt_pseudonyme(befund: Path, patient: Patient,
                                        speicher: Pseudonymspeicher) -> None:
    """Even a street code that reads like a label ("AZ2Q4W") passes the re-check."""
    from pseudonymed.pruefung.gegenprobe import in_pseudonym, pseudonymspannen

    text = "wohnt in [Straße AZ2Q4WX], Patient KAZ2Q4W"
    spannen = pseudonymspannen(text, ["[Straße AZ2Q4WX]", "Patient KAZ2Q4W"])
    assert spannen == [(9, 25), (27, 42)]
    assert in_pseudonym(17, 24, spannen) and not in_pseudonym(0, 5, spannen)


def test_fachbegriffe_werden_gemerkt(befund: Path, patient: Patient,
                                      speicher: Pseudonymspeicher) -> None:
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    assert v.fachbegriffe_anzahl >= 3 and v.fachbegriffe_rects(0)
    v.schliessen()
    ohne = vorbereiten(befund, patient, Ablaufoptionen(fachbegriffe=False), speicher=speicher)
    assert ohne.fachbegriffe_anzahl == 0 and ohne.fachbegriffe_rects(0) == []
    ohne.schliessen()


def test_maske_schwaerzt_schwarz_und_still(befund: Path, patient: Patient,
                                           speicher: Pseudonymspeicher) -> None:
    """A mask blackens the region, removes the text under it, writes no pseudonym and
    drops hits that lie inside it; the text file shows [geschwärzt]."""
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    vorher = len(v.fundstellen)
    # the line "Betr.: Herrn <name>, geb. <date>" on page one: both hits lie inside the box
    betr = next(f for f in v.seiten[0].fundstellen if f.regel == "datum.geburtsdatum")
    y0 = min(r[1] for r in betr.rects) - 2
    y1 = max(r[3] for r in betr.rects) + 2
    neue = v.maske(0, (50, y0, 560, y1))
    assert neue and all(f.regel == "maske" for f in neue)
    assert betr not in v.fundstellen and len(v.fundstellen) < vorher
    erg = abschliessen(v)
    assert erg.freigegeben, als_text(erg.bericht)
    with pymupdf.open(erg.ziel_pdf) as d:
        seite = d[0]
        clip = pymupdf.Rect(60, y0 + 1, 550, y1 - 1)
        assert "Betr" not in seite.get_text("text", clip=clip)
        pix = seite.get_pixmap(clip=clip, dpi=72, colorspace=pymupdf.csGRAY)
        mittel = sum(pix.samples) / len(pix.samples)
        assert mittel < 20, mittel  # solid black, no grey box and no text drawn into it
        assert "Betr" not in seite.get_text("text")
    txt = erg.ziel_txt.read_text(encoding="utf-8")
    assert "[geschwärzt]" in txt and "Betr.:" not in txt
    assert patient.nachname not in txt


def test_verwerfen_und_wiederherstellen(befund: Path, patient: Patient,
                                        speicher: Pseudonymspeicher) -> None:
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    f = next(x for x in v.fundstellen if x.regel == "zuweiser1.name")
    v.verwerfe(f)
    assert f in v.verworfen and f not in v.fundstellen and v.toleriert
    v.wiederherstelle(f)
    assert f not in v.verworfen and f in v.fundstellen and not v.toleriert
    assert v.unsichere_rects(0) == []  # a clean text layer has no uncertain OCR words
    v.schliessen()


def test_rastern_mit_textebene_und_markdown(befund: Path, patient: Patient,
                                            speicher: Pseudonymspeicher) -> None:
    opt = Ablaufoptionen(markdown=True, schwaerzen=Schwaerzoptionen(rastern=True, dpi=100))
    erg = pseudonymisiere(befund, patient, opt, speicher=speicher)
    assert erg.freigegeben, als_text(erg.bericht)
    with pymupdf.open(erg.ziel_pdf) as d:
        assert all(p.get_images() for p in d)  # rasterised: an image per page
        text = "\n".join(p.get_text("text") for p in d)
    assert "Ramipril" in text and f"Patient {erg.kennung}" in text  # invisible layer
    assert gefundene_klardaten(text, patient) == []
    assert erg.ziel_md and erg.ziel_md.exists()
    md = erg.ziel_md.read_text(encoding="utf-8")
    assert md.startswith("# Pseudonymisierter Text") and "## Seite 1" in md and "## Seite 2" in md
    assert gefundene_klardaten(md, patient) == [] and "Ramipril" in md
    assert erg.bericht.optionen["textebene"] is True and erg.bericht.optionen["markdown"]


def test_versteckter_text_bleibt_textebene(tmp_path: Path, patient: Patient,
                                           speicher: Pseudonymspeicher) -> None:
    """A scanned PDF with its own invisible OCR layer keeps a text layer: the hits in
    the hidden text are redacted, the rest stays searchable."""
    quelle = tmp_path / "ocr_pdf.pdf"
    with pymupdf.open() as d:
        page = d.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), False)
        pix.clear_with(255)
        page.insert_image(pymupdf.Rect(400, 700, 500, 800), pixmap=pix)  # small: no OCR needed
        page.insert_text((60, 100), f"Befund für {patient.vollname} vom 12.03.2026",
                         fontsize=11, render_mode=3)
        page.insert_text((60, 130), "Diagnose: arterielle Hypertonie, Ramipril 5 mg",
                         fontsize=11, render_mode=3)
        d.save(quelle)
    erg = pseudonymisiere(quelle, patient, Ablaufoptionen(ocr="aus"), speicher=speicher)
    assert erg.freigegeben, als_text(erg.bericht)
    with pymupdf.open(erg.ziel_pdf) as d:
        text = d[0].get_text("text")
    assert "Ramipril" in text and "Hypertonie" in text
    assert patient.nachname not in text and f"Patient {erg.kennung}" in text


def test_verworfene_treffer_merken(befund: Path, patient: Patient) -> None:
    """A discard stored for the patient is applied next time (green, restorable); restoring
    and exporting forgets it again."""
    with Pseudonymspeicher(":memory:", geheimnis=TESTSCHLUESSEL) as sp:
        v = vorbereiten(befund, patient, Ablaufoptionen(verworfene_merken=True), speicher=sp)
        f = next(x for x in v.fundstellen if x.regel == "zuweiser1.name")
        v.verwerfe(f)
        erg = abschliessen(v)
        assert erg.freigegeben and v.abgeschlossen
        bericht = als_text(erg.bericht)
        assert "Bewusst belassen (im Prüfschritt verworfen): zuweiser1.name ×1" in bericht
        zeile = next(z for z in bericht.splitlines() if "Erneut geprüft" in z)
        assert "Geburtsdatum" in zeile and "Zuweiser" in zeile and "nichts gefunden" in zeile
        assert erg.bericht.gegenprobe.geprueft[0] == "Name"
        assert sp.verworfene(patient) == {("zuweiser1.name", f.text.casefold())}
        v2 = vorbereiten(befund, patient, Ablaufoptionen(verworfene_merken=True), speicher=sp)
        assert any(x.regel == "zuweiser1.name" and x.text == f.text for x in v2.verworfen)
        assert not any(x.text == f.text for x in v2.fundstellen)  # other spellings stay hits
        assert any("gespeicherter Entscheidung" in w for w in v2.hinweise)
        g = next(x for x in v2.verworfen if x.regel == "zuweiser1.name")
        v2.wiederherstelle(g)
        assert v2.version == 1 and v2.wiederhergestellt
        erg2 = abschliessen(v2, endgueltig=False)  # a check run: nothing stored, v2 stays open
        assert erg2.freigegeben and not v2.abgeschlossen and sp.verworfene(patient)
        erg3 = abschliessen(v2)
        assert erg3.freigegeben and sp.verworfene(patient) == set()
        v3 = vorbereiten(befund, patient, Ablaufoptionen(), speicher=sp)
        assert any(x.text == f.text for x in v3.fundstellen) and not v3.verworfen
        v3.schliessen()


def test_abschliessen_mehrfach(befund: Path, patient: Patient, speicher: Pseudonymspeicher,
                               tmp_path: Path) -> None:
    """The finishing phase can run as a check into one folder and again for the export."""
    v = vorbereiten(befund, patient, Ablaufoptionen(ziel_ordner=tmp_path / "check"),
                    speicher=speicher)
    a = abschliessen(v, endgueltig=False)
    assert a.freigegeben and a.ziel_pdf.parent == tmp_path / "check"
    v.kasten(0, (56, 300, 400, 320), "[Nachtrag]")
    v.opt = Ablaufoptionen(ziel_ordner=tmp_path / "aus")
    b = abschliessen(v)
    assert b.freigegeben and b.ziel_pdf.parent == tmp_path / "aus" and v.abgeschlossen
    assert "[Nachtrag]" in b.ziel_txt.read_text(encoding="utf-8")
    assert "[Nachtrag]" not in a.ziel_txt.read_text(encoding="utf-8")


def test_kasten_knapp_unter_der_zeile(befund: Path, patient: Patient,
                                      speicher: Pseudonymspeicher) -> None:
    """A box drawn a little below the centre of a line still takes the line's text, so
    that the text file and the PDF agree (the user's boxes are rarely centred)."""
    v = vorbereiten(befund, patient, Ablaufoptionen(), speicher=speicher)
    text = v.seiten[0].text
    i = text.text.index("Sehr geehrte")
    box = text.boxen[i]
    mitte = (box[1] + box[3]) / 2
    neue = v.kasten(0, (56, mitte + 0.5, 300, mitte + 12), "[Anrede]")
    assert neue and neue[0].text.startswith("Sehr geehrte"), [n.text for n in neue]
    erg = abschliessen(v)
    assert erg.freigegeben and "[Anrede]" in erg.text and "Sehr geehrte" not in erg.text


def test_vokabular_nur_aus_rate_treffern() -> None:
    """Discarded guesses (NER, fuzzy) become vocabulary; discarded rule hits carry the
    patient's own identifiers and stay out."""
    from pseudonymed.ablauf import vokabular_aus
    from pseudonymed.kern.modell import Art, Fundstelle

    def f(regel: str, text: str, quelle: str = "text") -> Fundstelle:
        return Fundstelle(seite=0, start=0, ende=len(text), text=text, art=Art.NAME,
                          regel=regel, pseudonym="[X]", quelle=quelle)

    verworfen = [f("ner.person", "Konzentration"), f("ner.ort", "Nierenlager"),
                 f("unscharf.name.nachname", "Bauch", "unscharf"),
                 f("ner.person", "konzentration"), f("name.nachname", "Bauer"),
                 f("muster.email", "max@example.org"), f("ner.person", "A1"),
                 f("ner.person", "  Klinikum   Süd ")]
    assert vokabular_aus(verworfen) == ["Bauch", "Klinikum Süd", "Konzentration", "Nierenlager"]


def test_bericht_trennt_hinweise_von_warnungen(tmp_path: Path) -> None:
    from pseudonymed.pruefung.bericht import erstelle_bericht

    b = erstelle_bericht(tmp_path / "q.pdf", tmp_path / "q_pseudonym.pdf", 1, False, [], None,
                         warnungen=["OCR-Seiten: Handschrift wird nicht erkannt."],
                         hinweise=["Seite 1 vor der OCR aufbereitet: begradigt (1,5°)."])
    text = als_text(b)
    assert "Hinweise (durchgeführte Schritte):" in text and "Warnungen:" in text
    assert text.index("aufbereitet") < text.index("Warnungen:")
    assert text.index("Hinweise") < text.index("aufbereitet")
    ohne = erstelle_bericht(tmp_path / "q.pdf", None, 1, True, [], None,
                            hinweise=["Ausgabe gerastert."])
    assert "Warnungen:" not in als_text(ohne) and "gerastert" in als_text(ohne)
