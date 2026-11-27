"""Scans, photos, noise, HEIC and barcodes: read by OCR, blanked in the pixels, re-checked."""

from __future__ import annotations

import io
import random
from pathlib import Path

import pymupdf
import pytest
from ocrdoc import ocr as ocr_module
from ocrdoc.ocr import OCRWord, page_text_from_words
from optdoc import cleanup as cleanup_module

from pseudonymed.checks.report import as_text
from pseudonymed.core.model import Kind, Patient
from pseudonymed.detection import codes as codes_module
from pseudonymed.document import docrepro_adapter
from pseudonymed.document.chain import DocumentChain
from pseudonymed.pipeline import PipelineOptions, chain_settings, pseudonymise
from pseudonymed.pseudonyms.store import PseudonymStore
from pseudonymed.redaction.pdf import RedactionOptions, rasterise_document
from tests.synthetic import long_date
from tests.test_pipeline import found_plain_data

BACKENDS = ocr_module.available_backends()
needs_backend = pytest.mark.skipif(not BACKENDS, reason="kein OCR-Backend installiert")


def _scan(report_pdf: Path, target: Path, dpi: int = 200, noise: float = 0.0,
          rotation: float = 0.0) -> Path:
    """Image-only PDF of the synthetic report, optionally with salt-and-pepper noise and a tilt."""
    with pymupdf.open(report_pdf) as d:
        scan = rasterise_document(d, dpi=dpi)
    if noise or rotation:
        from PIL import Image

        new = pymupdf.open()
        rng = random.Random(1)
        for pdf_page in scan:
            pix = pdf_page.get_pixmap(dpi=dpi, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
            px = image.load()
            if noise:
                for _ in range(int(noise * image.width * image.height)):
                    xy = rng.randrange(image.width), rng.randrange(image.height)
                    px[xy] = rng.choice((0, 255))
            if rotation:
                image = image.rotate(rotation, expand=False, fillcolor=255)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            page = new.new_page(width=pdf_page.rect.width, height=pdf_page.rect.height)
            page.insert_image(page.rect, stream=buffer.getvalue())
        scan.close()
        scan = new
    scan.save(target)
    scan.close()
    return target


@needs_backend
@pytest.mark.parametrize("backend", BACKENDS)
def test_scan_is_read_and_redacted(tmp_path: Path, report_pdf: Path, patient: Patient,
                                   store: PseudonymStore, backend: str) -> None:
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    res = pseudonymise(scan, patient, PipelineOptions(ocr=backend), store=store)
    assert res.released, as_text(res.report)
    assert not res.report.text_layer
    assert any("per OCR gelesen" in w for w in res.report.notes)
    assert sum(1 for f in res.hits if f.source == "ocr") >= 10
    assert res.preview and all(p.exists() for p in res.preview)
    assert "--- Bildtext (OCR" in res.text
    assert found_plain_data(res.text, patient) == []
    # the output has no text layer with clear data, and OCR of the output finds none either
    with pymupdf.open(res.target_pdf) as d:
        assert found_plain_data("\n".join(p.get_text() for p in d), patient) == []
        b = docrepro_adapter.backend(backend)
        after = "\n".join(ocr_module.recognise_page(p, b, i).text for i, p in enumerate(d))
    assert found_plain_data(after, patient) == []
    assert len(after) > 400 and ("Befund" in after or "Kontrolle" in after)


@needs_backend
def test_noisy_skewed_scan(tmp_path: Path, report_pdf: Path, patient: Patient,
                           store: PseudonymStore) -> None:
    scan = _scan(report_pdf, tmp_path / "scan.pdf", dpi=150, noise=0.01, rotation=0.7)
    res = pseudonymise(scan, patient, store=store)
    assert res.released, as_text(res.report)
    assert found_plain_data(res.text, patient) == []
    assert sum(1 for f in res.hits if f.source == "ocr") >= 6


@needs_backend
def test_photo_as_png(tmp_path: Path, report_pdf: Path, patient: Patient,
                      store: PseudonymStore) -> None:
    with pymupdf.open(report_pdf) as d:
        image = tmp_path / "foto.png"
        d[0].get_pixmap(dpi=150).save(image)
    res = pseudonymise(image, patient, store=store)
    assert res.released, as_text(res.report)
    assert res.report.pages == 1 and found_plain_data(res.text, patient) == []


def test_without_backend_no_release(tmp_path: Path, report_pdf: Path, patient: Patient,
                                    store: PseudonymStore) -> None:
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    res = pseudonymise(scan, patient, PipelineOptions(ocr="aus"), store=store)
    assert not res.released
    assert any("ohne OCR" in w for w in res.report.warnings)


@pytest.mark.skipif(not codes_module.available(), reason="zxing-cpp nicht installiert")
def test_barcode_is_redacted(tmp_path: Path, report_pdf: Path, patient: Patient,
                             store: PseudonymStore) -> None:
    import zxingcpp
    from PIL import Image

    with pymupdf.open(report_pdf) as d:
        qr = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, f"PAT {patient.patient_id}",
                                    width=300, height=300)
        buffer = io.BytesIO()
        Image.fromarray(qr).save(buffer, format="PNG")
        d[0].insert_image(pymupdf.Rect(400, 600, 520, 720), stream=buffer.getvalue())
        with_code = tmp_path / "mit_code.pdf"
        d.save(with_code)
    with pymupdf.open(with_code) as d:
        assert len(codes_module.find_codes(d[0])) == 1
    res = pseudonymise(with_code, patient, store=store)
    assert res.released, as_text(res.report)
    assert any(f.source == "code" and f.pseudonym == "[Code]" for f in res.hits)
    with pymupdf.open(res.target_pdf) as d:
        assert codes_module.find_codes(d[0]) == []
    res2 = pseudonymise(with_code, patient, PipelineOptions(codes=False), store=store)
    assert not any(f.source == "code" for f in res2.hits)


def _true_boxes(report_pdf: Path, patient: Patient) -> list[tuple[int, pymupdf.Rect]]:
    """Rectangles of the identifier words as the source text layer knows them."""
    born = patient.birth_date
    wanted = {patient.surname, patient.first_name, patient.surname.upper(),
              patient.first_name.upper(),
              born.strftime("%d.%m.%Y"), born.strftime("%d.%m.%y"), patient.insurance_number,
              patient.patient_id, patient.postcode, patient.place, patient.emails[0],
              patient.relatives[0].first_name, patient.referrers[0].surname,
              str(born.day) + ".", str(born.year)}
    wanted |= set(long_date(born).split())
    boxes = []
    with pymupdf.open(report_pdf) as d:
        for i, pdf_page in enumerate(d):
            for term in wanted:
                if len(term) < 3:
                    continue
                for rect in pdf_page.search_for(term):  # character-precise, no punctuation
                    boxes.append((i, rect))
    assert len(boxes) >= 15
    return boxes


def _ink_in(doc: pymupdf.Document, page: int, rect: pymupdf.Rect, dpi: int = 200) -> int:
    pix = doc[page].get_pixmap(dpi=dpi, clip=rect, colorspace=pymupdf.csGRAY, alpha=False)
    return sum(1 for v in pix.samples if v < 100)


@needs_backend
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("distortion", ["sauber", "verrauscht"])
def test_no_ink_under_the_matches(tmp_path: Path, report_pdf: Path, patient: Patient,
                                  store: PseudonymStore, backend: str,
                                  distortion: str) -> None:
    """Pseudonyms are drawn in the fill colour here, so any dark pixel inside a
    known identifier box is a remnant of the original glyphs."""
    kw = dict(dpi=150, noise=0.01, rotation=0.0) if distortion == "verrauscht" else {}
    scan = _scan(report_pdf, tmp_path / "scan.pdf", **kw)
    opt = PipelineOptions(ocr=backend, redaction=RedactionOptions(text_colour=(0.88, 0.88, 0.88)))
    res = pseudonymise(scan, patient, opt, store=store)
    if not res.released:
        # a block is a legitimate outcome on a noisy scan, as long as it is a block for cause
        assert distortion == "verrauscht", as_text(res.report)
        assert res.report.recheck is not None and res.report.recheck.residual_matches
        return
    remaining = []
    with pymupdf.open(res.target_pdf) as d:
        for page, rect in _true_boxes(report_pdf, patient):
            inside = pymupdf.Rect(rect.x0 + 0.5, rect.y0 + 2, rect.x1 - 0.5, rect.y1 - 2)
            ink = _ink_in(d, page, inside)
            # an edge fringe or noise specks are allowed; a digit leaves 40+ dark pixels
            threshold = 25 if distortion == "verrauscht" else 8
            if ink > threshold:
                remaining.append((page + 1, [round(v) for v in rect], ink))
    assert remaining == [], remaining


def test_fuzzy_search() -> None:
    from pseudonymed.detection.fuzzy import distance, find_fuzzy, targets_for
    from pseudonymed.pseudonyms.assignment import PseudonymAssignment
    from tests.synthetic import synthetic_patient

    assert distance("Juli", "Jull", 1) == 1 and distance("Flantz", "Fiantz", 1) == 1
    assert distance("Flantz", "Zahn", 2) > 2
    p = synthetic_patient(7)
    v = PseudonymAssignment(p, "K7Q4MX", secret=bytes(32))
    targets = targets_for(p, v)
    words = [OCRWord(w, 50 + 40 * i, 100, 85 + 40 * i, 112)
             for i, w in enumerate(["Geboren", "am", "21.", "Jull", "1959", "in", "Rlesa."])]
    words += [OCRWord(w, 50 + 40 * i, 130, 85 + 40 * i, 142)
              for i, w in enumerate(["Herr", "Fiantz,", "Gotthard", "und", "Zahn"])]
    s = page_text_from_words(words, 0, 595, 842)
    matches = find_fuzzy(s, targets, [])
    texts = {f.text: f.rule for f in matches}
    assert texts["21. Jull 1959"] == "unscharf.datum.geburtsdatum"
    assert texts["Rlesa"] == "unscharf.adresse.ort"
    assert "Fiantz, Gotthard" in texts or "Fiantz" in texts
    assert "Zahn" not in texts and "Geboren" not in texts
    # a long token split by OCR is found across the pieces
    split = [OCRWord(w, 50 + 60 * i, 160, 100 + 60 * i, 172)
             for i, w in enumerate(["Mail", "gotthard.", "flantz@example.", "org", "Ende"])]
    s2 = page_text_from_words(split, 0, 595, 842)
    fuzzy = [f.text for f in find_fuzzy(s2, targets, [])]
    assert "gotthard. flantz@example. org" in fuzzy


@needs_backend
def test_scan_gets_text_layer(tmp_path: Path, report_pdf: Path, patient: Patient,
                              store: PseudonymStore) -> None:
    """The pseudonymised scan carries an invisible text layer with the OCR words minus
    the hits; without the option only the drawn pseudonyms are text."""
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    res = pseudonymise(scan, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    with pymupdf.open(res.target_pdf) as d:
        text = "\n".join(p.get_text("text") for p in d)
    assert "Ramipril" in text and "Diagnosen" in text and "Atorvastatin" in text
    assert f"Patient {res.code}" in text  # the visible pseudonyms are text too
    assert found_plain_data(text, patient) == []
    assert not any(k in text for k in (patient.surname, patient.first_name))
    without = pseudonymise(scan, patient, PipelineOptions(
        suffix="_ohne", redaction=RedactionOptions(text_layer=False)), store=store)
    with pymupdf.open(without.target_pdf) as d:
        text2 = "\n".join(p.get_text("text") for p in d)
    assert "Ramipril" not in text2 and f"Patient {res.code}" in text2


@needs_backend
def test_correct_ocr_text(tmp_path: Path, report_pdf: Path, patient: Patient,
                          store: PseudonymStore) -> None:
    """The user corrects the recognised text; the correction feeds the text file and
    the text layer and is searched again, the image redaction keeps the OCR boxes."""
    from pseudonymed.pipeline import finalise, prepare

    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    v = prepare(scan, patient, PipelineOptions(), store=store)
    assert v.pages[0].ocr is not None
    ocr = v.ocr_text(0)
    assert "Diagnosen" in ocr
    new = (ocr.rstrip("\n")
           + f"\nZusatz: {patient.first_name} {patient.surname} aus {patient.place}.\n")
    matches = v.set_ocr_text(0, new)
    assert matches and all(f.source == "korrektur" and not f.rects for f in matches)
    assert {f.kind for f in matches} >= {Kind.NAME, Kind.PLACE}
    assert v.ocr_text(0) == new and v.correction_matches
    assert v.set_ocr_text(0, ocr) == [] and v.pages[0].correction is None  # back to OCR
    v.set_ocr_text(0, new)
    ocr_page = v.pages[0].ocr
    a = ocr_page.text.index("Diagnosen")
    ocr_box = ocr_page.rects(a, a + len("Diagnosen"))[0]
    res = finalise(v)
    assert res.released, as_text(res.report)
    txt = res.target_txt.read_text(encoding="utf-8")
    assert f"Zusatz: Patient {res.code} aus [Ort " in txt, txt
    assert found_plain_data(txt, patient) == []
    with pymupdf.open(res.target_pdf) as d:
        text = d[0].get_text("text")
        found = d[0].search_for("Diagnosen")
    assert "Zusatz:" in text and patient.place not in text  # the invisible layer is the correction
    # the corrected text sits on the image words: selecting "Diagnosen" covers the printed word
    assert any(abs(r.x0 - ocr_box.x0) < 3 and r.y0 < ocr_box.y1 and r.y1 > ocr_box.y0
               for r in found), (found, ocr_box)
    assert any(f["quelle"] == "korrektur" for f in res.report.hits)


@needs_backend
@pytest.mark.skipif(not cleanup_module.available(), reason="OpenCV fehlt")
def test_cleanup_before_the_ocr(tmp_path: Path, report_pdf: Path, patient: Patient,
                                store: PseudonymStore) -> None:
    """A tilted, shadowed scan: optdoc suggests straightening and background clean-up,
    the pipeline applies it before OCR and records it; the hits are found."""
    chain = DocumentChain.open(scan := _scan(report_pdf, tmp_path / "scan.pdf", rotation=2.5),
                               chain_settings(PipelineOptions()))
    project = chain.ensure("optdoc")
    assert [n for n in range(len(project.pages)) if project.cleanable(n)] == [0, 1]
    a = project.suggestion(0)
    assert a.straighten and abs(abs(a.angle) - 2.5) < 0.5, a
    assert project.preview(0, a)[0].startswith(b"\x89PNG")
    born_digital = DocumentChain.open(report_pdf, chain_settings(PipelineOptions()))
    plain = born_digital.ensure("optdoc")
    assert not any(plain.cleanable(n) for n in range(len(plain.pages)))  # a text layer stays
    born_digital.close()
    res = pseudonymise(scan, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    assert any("aufbereitet" in w and "begradigt" in w for w in res.report.notes)
    assert res.report.options["aufbereitung"]["1"]["straighten"] is True
    assert found_plain_data(res.text, patient) == [] and "Ramipril" in res.text
    without = pseudonymise(scan, patient, PipelineOptions(clean_up="aus", suffix="_roh"),
                        store=store)
    assert not any("aufbereitet" in w for w in without.report.notes)
    assert "aufbereitung" not in without.report.options
    # a person's choice wins over the suggestion, and comes in with the chain document
    from optdoc.cleanup import CleanupPlan

    from pseudonymed.pipeline import finalise, prepare

    project.set_plan(0, CleanupPlan(masks=[(56, 60, 400, 95)]))
    project.set_plan(1, None)
    v = prepare(scan, patient, PipelineOptions(suffix="_eigen"), store=store,
                chain=chain.document())
    chain.close()
    assert set(v.cleaned_up) == {0} and v.cleaned_up[0].masks
    res3 = finalise(v)
    assert res3.released


@needs_backend
def test_read_then_analyse(tmp_path: Path, report_pdf: Path, patient: Patient,
                           store: PseudonymStore) -> None:
    """Reading and detection as two phases: the text can be corrected in between, the
    detection then runs over the corrected text as well."""
    from pseudonymed.pipeline import analyse, finalise, read

    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    reading = read(scan, PipelineOptions())
    assert reading.ocr_pages == [1, 2] and reading.has_ocr(0) and not reading.used_up
    assert "Diagnosen" in reading.text(0) and reading.page_image(0).startswith(b"\x89PNG")
    reading.set_ocr_text(0, reading.ocr_text(0).rstrip("\n")
                         + f"\nZusatz: {patient.first_name} {patient.surname}.\n")
    assert reading.pages[0].correction is not None
    reading.set_ocr_text(1, reading.ocr_text(1))  # unchanged text: no correction
    assert reading.pages[1].correction is None
    v = analyse(reading, patient, PipelineOptions(), store=store)
    assert reading.used_up and v.pages[0].correction_matches
    assert any(f.source == "korrektur" for f in v.correction_matches)
    with pytest.raises(RuntimeError):
        analyse(reading, patient, PipelineOptions(), store=store)
    res = finalise(v)
    assert res.released, as_text(res.report)
    txt = res.target_txt.read_text(encoding="utf-8")
    assert f"Zusatz: Patient {res.code}." in txt and found_plain_data(txt, patient) == []


@needs_backend
def test_structured_ocr_text(tmp_path: Path, report_pdf: Path, patient: Patient,
                             store: PseudonymStore) -> None:
    """The OCR text goes out as Markdown with paragraphs joined; ``gliedern=False`` keeps
    the raw lines; the structured text is what the text-recognition step shows."""
    from pseudonymed.pipeline import finalise, prepare

    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    v = prepare(scan, patient, PipelineOptions(), store=store)
    raw = v.pages[0].ocr.text
    structured = v.ocr_text(0)
    assert v.pages[0].structure is not None and structured != raw
    # a line that ran to the margin continues in the next one: joined with a space
    roles, lines = v.pages[0].structure.roles, raw.split("\n")
    k = next(i for i, r in enumerate(roles) if r.separator == "word")
    assert lines[k - 1].strip() + " " + lines[k].strip() in structured
    assert "\n\n" in structured  # and the blocks are kept apart
    assert v.set_ocr_text(0, structured) == [] and v.pages[0].correction is None
    res = finalise(v)
    assert res.released, as_text(res.report)
    assert "Diagnosen" in res.text and found_plain_data(res.text, patient) == []
    assert res.report.options["gliedern"] is True
    raw_res = pseudonymise(scan, patient, PipelineOptions(structured=False, suffix="_roh"),
                           store=store)
    assert raw_res.released and raw_res.report.options["gliedern"] is False
    # without structuring the continuation line still starts a line of its own
    starts = [" ".join(lines[i].split()[:2]) for i, r in enumerate(roles)
              if r.separator == "word" and len(lines[i].split()) >= 2]
    begin = next(a for a in starts if "\n" + a in raw_res.text)
    assert "\n" + begin not in res.text and " " + begin in res.text


# -- steps of the OCR layer on synthetic engine output ---------------------------------------------
