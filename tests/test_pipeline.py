"""End-to-end: synthetic report in, pseudonymised PDF plus text and report out, check passes."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pymupdf
import pytest

from pseudonymed.checks.report import as_text
from pseudonymed.core.model import Patient
from pseudonymed.pipeline import PipelineOptions, finalise, finish, prepare, pseudonymise
from pseudonymed.pseudonyms.patient_code import is_valid_code
from pseudonymed.pseudonyms.store import PseudonymStore
from pseudonymed.redaction.pdf import PageResult, RedactionOptions, rasterise_document
from tests.conftest import TEST_KEY
from tests.synthetic import create_report_pdf, long_date


def plain_data(p: Patient) -> list[str]:
    born = p.birth_date
    assert born is not None
    return [
        p.surname, p.first_name, p.surname.upper(), p.street, p.place, p.postcode + " " + p.place,
        born.strftime("%d.%m.%Y"), born.strftime("%d.%m.%y"), long_date(born),
        p.insurance_number, p.patient_id, p.emails[0], p.case_numbers[0],
        re.sub(r"\D", "", p.phones[0])[-6:],
        p.relatives[0].first_name, p.referrers[0].surname, p.referrers[0].street,
    ]


def found_plain_data(text: str, p: Patient) -> list[str]:
    flat = re.sub(r"[\s\-/().]+", "", text).casefold()
    return [k for k in plain_data(p) if re.sub(r"[\s\-/().]+", "", k).casefold() in flat]


def test_round_trip(report_pdf: Path, patient: Patient, store: PseudonymStore) -> None:
    res = pseudonymise(report_pdf, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    assert res.report.passed
    k = res.code
    assert len(k) == 6 and is_valid_code(k)
    assert res.target_pdf and res.target_pdf.exists()
    assert res.target_txt and res.target_txt.exists()
    assert res.target_report and res.target_report.exists()

    with pymupdf.open(res.target_pdf) as d:
        pdf_text = "\n".join(p.get_text("text") for p in d)
        assert d.metadata["title"] == "" and d.metadata["author"] == ""
        assert d.metadata["subject"].startswith("pseudonymisiert mit PseudonyMed")
        assert d.get_toc() == []
        assert d.embfile_count() == 0
        assert all(len(list(p.annots())) == 0 for p in d)
    assert found_plain_data(pdf_text, patient) == []
    txt = res.target_txt.read_text(encoding="utf-8")
    assert found_plain_data(txt, patient) == []
    # the actual content stays untouched
    assert re.search(r"\[Straße [A-Z2-9]{6}\]", txt) and re.search(r"\[PLZ Ort [A-Z2-9]{6}\]", txt)
    assert "[Wohnort]" not in txt
    for expected in (f"Patient {k}", f"[geb. {patient.birth_date.year}]", "Dr. Z1",
                     f"Angehörige {k}-1", "[Versichertennummer]", "max. 3 Tabletten",
                     "Ramipril 5 mg",
                     "Sehr geehrte Frau Kollegin Dr. Z1,", "=== Seite 2 ==="):
        assert expected in txt, expected
    assert f"Patient {k}" in pdf_text and "Ramipril 5 mg" in pdf_text
    # the report carries no clear data
    report = res.target_report.read_text(encoding="utf-8")
    assert found_plain_data(report, patient) == []
    data = json.loads(report)
    assert data["bestanden"] is True and data["seiten"] == 2
    assert len(data["fundstellen"]) >= 15
    # the key is in the local store only
    z = store.mapping(patient.key())
    assert z[f"Patient {k}"] == patient.full_name
    assert z["[E-Mail]"] == patient.emails[0]
    assert patient.street in z.values() and f"{patient.postcode} {patient.place}" in z.values()
    # the referrer's practice address has its own street code
    assert sum(1 for p in z if p.startswith("[Straße ")) == 2


def test_codes_per_person(tmp_path: Path, patient: Patient, female_patient: Patient,
                          store: PseudonymStore) -> None:
    a = pseudonymise(create_report_pdf(tmp_path / "a.pdf", patient), patient, store=store)
    b = pseudonymise(create_report_pdf(tmp_path / "b.pdf", female_patient), female_patient,
                     store=store)
    a2 = pseudonymise(create_report_pdf(tmp_path / "a2.pdf", patient), patient, store=store)
    assert a.code == a2.code != b.code
    assert f"Patientin {b.code}" in b.text and f"Patient {a.code}" in a2.text


def test_letter_scheme_in_the_pipeline(report_pdf: Path, patient: Patient,
                                       letter_store: PseudonymStore) -> None:
    res = pseudonymise(report_pdf, patient, store=letter_store)
    assert res.code == "A" and "Patient A" in res.text and "Angehörige A-1" in res.text
    assert "[Straße " in res.text  # address codes do not depend on the identifier scheme


def test_spelling_variant_gets_same_code(tmp_path: Path, patient: Patient,
                                         store: PseudonymStore) -> None:
    import dataclasses

    res = pseudonymise(create_report_pdf(tmp_path / "a.pdf", patient), patient, store=store)
    variant = dataclasses.replace(patient, surname=patient.surname + "z",
                                  first_name=patient.first_name.upper())
    res2 = pseudonymise(create_report_pdf(tmp_path / "b.pdf", variant), variant,
                        store=store)
    assert res2.code == res.code
    assert any("zugeordnet" in w for w in res2.report.warnings)


def test_age_instead_of_year(report_pdf: Path, patient: Patient, store: PseudonymStore) -> None:
    res = pseudonymise(report_pdf, patient, PipelineOptions(date_format="alter"), store=store)
    assert res.released
    today = dt.date.today()
    age = today.year - patient.birth_date.year - (
        (today.month, today.day) < (patient.birth_date.month, patient.birth_date.day))
    assert f"[Alter {age}]" in res.text


def test_rasterise(report_pdf: Path, patient: Patient, store: PseudonymStore) -> None:
    opt = PipelineOptions(redaction=RedactionOptions(rasterise=True, dpi=72))
    res = pseudonymise(report_pdf, patient, opt, store=store)
    assert res.released
    with pymupdf.open(res.target_pdf) as d:
        # rasterised, but with an invisible text layer: words and pseudonyms, no clear data
        texts = [p.get_text("text") for p in d]
        assert all(t.strip() for t in texts) and len(d) == 2
        assert found_plain_data("\n".join(texts), patient) == []
    assert any("gerastert" in w for w in res.report.notes)


def test_recheck_blocks(report_pdf: Path, patient: Patient, store: PseudonymStore,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finish, "redact_page", lambda *a, **k: PageResult(0, True))
    res = pseudonymise(report_pdf, patient, store=store)
    assert not res.released
    assert res.target_pdf is None and res.target_txt is None
    assert not (report_pdf.parent / "befund_pseudonym.pdf").exists()
    assert res.report.recheck is not None and res.report.recheck.residual_matches
    assert any("Nicht freigegeben" in w for w in res.report.warnings)
    assert "NICHT bestanden" in as_text(res.report)


def test_scan_without_ocr_is_not_released(tmp_path: Path, report_pdf: Path, patient: Patient,
                                          store: PseudonymStore) -> None:
    with pymupdf.open(report_pdf) as d:
        scan = rasterise_document(d, dpi=72)
        scan_path = tmp_path / "scan.pdf"
        scan.save(scan_path)
        scan.close()
    res = pseudonymise(scan_path, patient, PipelineOptions(ocr="aus"), store=store)
    assert not res.released
    assert not res.report.text_layer
    assert any("ohne Textebene" in w for w in res.report.warnings)


def test_image_without_ocr_is_not_released(tmp_path: Path, report_pdf: Path, patient: Patient,
                                           store: PseudonymStore) -> None:
    with pymupdf.open(report_pdf) as d:
        image = tmp_path / "foto.png"
        d[0].get_pixmap(dpi=72).save(image)
    res = pseudonymise(image, patient, PipelineOptions(ocr="aus"), store=store)
    assert not res.released
    assert res.report.pages == 1


def test_two_phases_with_discard_and_box(report_pdf: Path, patient: Patient,
                                         store: PseudonymStore) -> None:
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    assert v.hits and not v.ocr_pages
    assert v.page_image(0)[:4] == b"\x89PNG"
    # drop the hit on the referrer's name on page 1 (a false positive, say)
    referrers = [f for f in v.hits if f.rule == "zuweiser1.name"]
    assert referrers
    for f in referrers:
        v.discard(f)
    # a manual box over the diagnosis line of page 1
    with pymupdf.open(report_pdf) as d:
        rect = d[0].search_for("Leichte kognitive Störung")[0]
    added = v.box(0, (rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1), "[Diagnose]")
    assert len(added) == 1 and added[0].text == "Leichte kognitive Störung"
    assert added[0].source == "manuell"
    res = finalise(v)
    assert res.released, as_text(res.report)
    assert "[Diagnose]" in res.text and "Leichte kognitive Störung" not in res.text
    assert patient.referrers[0].surname in res.text  # dropped hit stays in the text
    with pymupdf.open(res.target_pdf) as d:
        text = d[0].get_text()
        assert "[Diagnose]" in text and "Leichte kognitive Störung" not in text
        assert "ICD-10 F06.7" in text  # the rest of the line survived the manual box
    assert not any("gerastert" in w for w in res.report.notes)
    with pytest.raises(RuntimeError):
        finalise(v)


def test_box_on_image_page(tmp_path: Path, report_pdf: Path, patient: Patient,
                           store: PseudonymStore) -> None:
    with pymupdf.open(report_pdf) as d:
        scan = rasterise_document(d, dpi=72)
        scan_path = tmp_path / "scan.pdf"
        scan.save(scan_path)
        scan.close()
    v = prepare(scan_path, patient, PipelineOptions(ocr="aus"), store=store)
    added = v.box(0, (50, 60, 300, 120))
    assert len(added) == 1 and added[0].rects == [(50, 60, 300, 120)] and added[0].text == ""
    v.close()


def test_recheck_protects_pseudonyms(report_pdf: Path, patient: Patient,
                                     store: PseudonymStore) -> None:
    """Even a street code that reads like a label ("AZ2Q4W") passes the re-check."""
    from pseudonymed.checks.recheck import in_pseudonym, pseudonym_spans

    text = "wohnt in [Straße AZ2Q4WX], Patient KAZ2Q4W"
    spans = pseudonym_spans(text, ["[Straße AZ2Q4WX]", "Patient KAZ2Q4W"])
    assert spans == [(9, 25), (27, 42)]
    assert in_pseudonym(17, 24, spans) and not in_pseudonym(0, 5, spans)


def test_medical_terms_are_remembered(report_pdf: Path, patient: Patient,
                                      store: PseudonymStore) -> None:
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    assert v.medical_term_count >= 3 and v.medical_term_rects(0)
    v.close()
    without = prepare(report_pdf, patient, PipelineOptions(medical_terms=False), store=store)
    assert without.medical_term_count == 0 and without.medical_term_rects(0) == []
    without.close()


def test_mask_redacts_black_and_silent(report_pdf: Path, patient: Patient,
                                       store: PseudonymStore) -> None:
    """A mask blackens the region, removes the text under it, writes no pseudonym and
    drops hits that lie inside it; the text file shows [geschwärzt]."""
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    before = len(v.hits)
    # the line "Betr.: Herrn <name>, geb. <date>" on page one: both hits lie inside the box
    affected = next(f for f in v.pages[0].hits if f.rule == "datum.geburtsdatum")
    y0 = min(r[1] for r in affected.rects) - 2
    y1 = max(r[3] for r in affected.rects) + 2
    added = v.add_mask(0, (50, y0, 560, y1))
    assert added and all(f.rule == "maske" for f in added)
    assert affected not in v.hits and len(v.hits) < before
    res = finalise(v)
    assert res.released, as_text(res.report)
    with pymupdf.open(res.target_pdf) as d:
        page = d[0]
        clip = pymupdf.Rect(60, y0 + 1, 550, y1 - 1)
        assert "Betr" not in page.get_text("text", clip=clip)
        pix = page.get_pixmap(clip=clip, dpi=72, colorspace=pymupdf.csGRAY)
        mean = sum(pix.samples) / len(pix.samples)
        assert mean < 20, mean  # solid black, no grey box and no text drawn into it
        assert "Betr" not in page.get_text("text")
    txt = res.target_txt.read_text(encoding="utf-8")
    assert "[geschwärzt]" in txt and "Betr.:" not in txt
    assert patient.surname not in txt


def test_discard_and_restore(report_pdf: Path, patient: Patient,
                             store: PseudonymStore) -> None:
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    f = next(x for x in v.hits if x.rule == "zuweiser1.name")
    v.discard(f)
    assert f in v.discarded and f not in v.hits and v.tolerated
    v.restore(f)
    assert f not in v.discarded and f in v.hits and not v.tolerated
    assert v.uncertain_rects(0) == []  # a clean text layer has no uncertain OCR words
    v.close()


def test_rasterise_with_text_layer_and_markdown(report_pdf: Path, patient: Patient,
                                                store: PseudonymStore) -> None:
    opt = PipelineOptions(markdown=True, redaction=RedactionOptions(rasterise=True, dpi=100))
    res = pseudonymise(report_pdf, patient, opt, store=store)
    assert res.released, as_text(res.report)
    with pymupdf.open(res.target_pdf) as d:
        assert all(p.get_images() for p in d)  # rasterised: an image per page
        text = "\n".join(p.get_text("text") for p in d)
    assert "Ramipril" in text and f"Patient {res.code}" in text  # invisible layer
    assert found_plain_data(text, patient) == []
    assert res.target_md and res.target_md.exists()
    md = res.target_md.read_text(encoding="utf-8")
    assert md.startswith("# Pseudonymisierter Text") and "## Seite 1" in md and "## Seite 2" in md
    assert found_plain_data(md, patient) == [] and "Ramipril" in md
    assert res.report.options["textebene"] is True and res.report.options["markdown"]


def test_hidden_text_stays_text_layer(tmp_path: Path, patient: Patient,
                                      store: PseudonymStore) -> None:
    """A scanned PDF with its own invisible OCR layer keeps a text layer: the hits in
    the hidden text are redacted, the rest stays searchable."""
    source = tmp_path / "ocr_pdf.pdf"
    with pymupdf.open() as d:
        page = d.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), False)
        pix.clear_with(255)
        page.insert_image(pymupdf.Rect(400, 700, 500, 800), pixmap=pix)  # small: no OCR needed
        page.insert_text((60, 100), f"Befund für {patient.full_name} vom 12.03.2026",
                         fontsize=11, render_mode=3)
        page.insert_text((60, 130), "Diagnose: arterielle Hypertonie, Ramipril 5 mg",
                         fontsize=11, render_mode=3)
        d.save(source)
    res = pseudonymise(source, patient, PipelineOptions(ocr="aus"), store=store)
    assert res.released, as_text(res.report)
    with pymupdf.open(res.target_pdf) as d:
        text = d[0].get_text("text")
    assert "Ramipril" in text and "Hypertonie" in text
    assert patient.surname not in text and f"Patient {res.code}" in text


def test_remember_discarded_matches(report_pdf: Path, patient: Patient) -> None:
    """A discard stored for the patient is applied next time (green, restorable); restoring
    and exporting forgets it again."""
    with PseudonymStore(":memory:", secret=TEST_KEY) as sp:
        v = prepare(report_pdf, patient, PipelineOptions(remember_discarded=True), store=sp)
        f = next(x for x in v.hits if x.rule == "zuweiser1.name")
        v.discard(f)
        res = finalise(v)
        assert res.released and v.finalised
        report = as_text(res.report)
        assert "Bewusst belassen (im Prüfschritt verworfen): zuweiser1.name ×1" in report
        line = next(z for z in report.splitlines() if "Erneut geprüft" in z)
        assert "Geburtsdatum" in line and "Zuweiser" in line and "nichts gefunden" in line
        assert res.report.recheck.checked[0] == "Name"
        assert sp.discarded_for(patient) == {("zuweiser1.name", f.text.casefold())}
        v2 = prepare(report_pdf, patient, PipelineOptions(remember_discarded=True), store=sp)
        assert any(x.rule == "zuweiser1.name" and x.text == f.text for x in v2.discarded)
        assert not any(x.text == f.text for x in v2.hits)  # other spellings stay hits
        assert any("gespeicherter Entscheidung" in w for w in v2.notes)
        g = next(x for x in v2.discarded if x.rule == "zuweiser1.name")
        v2.restore(g)
        assert v2.version == 1 and v2.restored
        res2 = finalise(v2, final=False)  # a check run: nothing stored, v2 stays open
        assert res2.released and not v2.finalised and sp.discarded_for(patient)
        res3 = finalise(v2)
        assert res3.released and sp.discarded_for(patient) == set()
        v3 = prepare(report_pdf, patient, PipelineOptions(), store=sp)
        assert any(x.text == f.text for x in v3.hits) and not v3.discarded
        v3.close()


def test_finish_repeatedly(report_pdf: Path, patient: Patient, store: PseudonymStore,
                           tmp_path: Path) -> None:
    """The finishing phase can run as a check into one folder and again for the export."""
    v = prepare(report_pdf, patient, PipelineOptions(target_folder=tmp_path / "check"),
                store=store)
    a = finalise(v, final=False)
    assert a.released and a.target_pdf.parent == tmp_path / "check"
    v.box(0, (56, 300, 400, 320), "[Nachtrag]")
    v.opt = PipelineOptions(target_folder=tmp_path / "aus")
    b = finalise(v)
    assert b.released and b.target_pdf.parent == tmp_path / "aus" and v.finalised
    assert "[Nachtrag]" in b.target_txt.read_text(encoding="utf-8")
    assert "[Nachtrag]" not in a.target_txt.read_text(encoding="utf-8")


def test_box_just_below_the_line(report_pdf: Path, patient: Patient,
                                 store: PseudonymStore) -> None:
    """A box drawn a little below the centre of a line still takes the line's text, so
    that the text file and the PDF agree (the user's boxes are rarely centred)."""
    v = prepare(report_pdf, patient, PipelineOptions(), store=store)
    text = v.pages[0].text
    i = text.text.index("Sehr geehrte")
    box = text.boxes[i]
    middle = (box[1] + box[3]) / 2
    added = v.box(0, (56, middle + 0.5, 300, middle + 12), "[Anrede]")
    assert added and added[0].text.startswith("Sehr geehrte"), [n.text for n in added]
    res = finalise(v)
    assert res.released and "[Anrede]" in res.text and "Sehr geehrte" not in res.text


def test_vocabulary_only_from_guessed_matches() -> None:
    """Discarded guesses (NER, fuzzy) become vocabulary; discarded rule hits carry the
    patient's own identifiers and stay out."""
    from pseudonymed.core.model import Hit, Kind
    from pseudonymed.pipeline import vocabulary_from

    def f(rule: str, text: str, source: str = "text") -> Hit:
        return Hit(page=0, start=0, end=len(text), text=text, kind=Kind.NAME,
                   rule=rule, pseudonym="[X]", source=source)

    discarded = [f("ner.person", "Konzentration"), f("ner.ort", "Nierenlager"),
                 f("unscharf.name.nachname", "Bauch", "unscharf"),
                 f("ner.person", "konzentration"), f("name.nachname", "Bauer"),
                 f("muster.email", "max@example.org"), f("ner.person", "A1"),
                 f("ner.person", "  Klinikum   Süd ")]
    assert vocabulary_from(discarded) == ["Bauch", "Klinikum Süd", "Konzentration", "Nierenlager"]


def test_report_separates_notes_from_warnings(tmp_path: Path) -> None:
    from pseudonymed.checks.report import create_report
    from pseudonymed.core.model import RunLog

    log = RunLog(warnings=["OCR-Seiten: Handschrift wird nicht erkannt."],
                 notes=["Seite 1 vor der OCR aufbereitet: begradigt (1,5°)."])
    b = create_report(tmp_path / "q.pdf", tmp_path / "q_pseudonym.pdf", 1, False, [], None,
                      log)
    text = as_text(b)
    assert "Hinweise (durchgeführte Schritte):" in text and "Warnungen:" in text
    assert text.index("aufbereitet") < text.index("Warnungen:")
    assert text.index("Hinweise") < text.index("aufbereitet")
    without = create_report(tmp_path / "q.pdf", None, 1, True, [], None,
                            RunLog(notes=["Ausgabe gerastert."]))
    assert "Warnungen:" not in as_text(without) and "gerastert" in as_text(without)
