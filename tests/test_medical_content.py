"""Medications and lab values in four levels: off by default, redacted on request, restored."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.checks.report import as_text
from pseudonymed.cli import app
from pseudonymed.core.model import Kind, Patient
from pseudonymed.detection import lexicon
from pseudonymed.detection import medical_content as medical_module
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection.medical_content import Selection
from pseudonymed.detection.vocabulary import Vocabulary
from pseudonymed.pipeline import PipelineOptions, finalise, prepare, pseudonymise
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.test_cli import _patient_file
from tests.test_gui import qapp  # noqa: F401

LINES = [  # begriff, klasse, quelle
    ("Levetiracetam", "Medikament", "Wikidata"),
    ("Lamotrigin", "Medikament", "Wikidata"),
    ("Ibuprofen", "Medikament", "Wikidata"),
    ("Paracetamol", "Medikament", "Wikidata"),
    ("Sauerstoff", "Medikament", "Wikidata"),
    ("ASS", "Medikament", "Wikidata"), ("ASS", "Erkrankung", "Wikidata"),  # also autism
    ("Kalium", "Medikament", "Wikidata"), ("Kalium", "Laborwert", "Laborwerte"),  # both
    ("TSH", "Laborwert", "Laborwerte"),
    ("Hb", "Laborwert", "Laborwerte"), ("HbA1c", "Laborwert", "Laborwerte"),
    ("Kreatinin", "Laborwert", "Laborwerte"), ("Ferritin", "Laborwert", "Laborwerte"),
    ("Zuname", "Laborwert", "Wikidata"),  # the noisy Wikidata lab rows stay unused
]
ALL = Selection(True, True, True, True)


def _clear_caches() -> None:
    medical_module.standard.cache_clear()
    terms_module.standard.cache_clear()


@pytest.fixture
def small_lexicon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    _clear_caches()
    monkeypatch.setattr(lexicon, "medical_rows", lambda: list(LINES))  # a small bundled list
    monkeypatch.setattr(lexicon, "medical_state", lambda: (("Probe", "1"),))
    with Vocabulary() as v:
        v.add(["Marcumar"], "Medikament")  # a trade name of the user's own
        v.add(["Levetiracetam"], "Nutzervokabular")
    yield tmp_path / "daten"
    _clear_caches()


def _spans(text: str, selection: Selection = ALL) -> list[tuple[str, str]]:
    search = medical_module.standard(selection)
    assert search is not None
    return [(text[a:b], kind) for a, b, kind in search.spans(text)]


# -- the selection and the lists -----------------------------------------------------------------
def test_selection() -> None:
    assert medical_module.standard(Selection()) is None and not Selection().active
    opt = PipelineOptions(redact_doses=True, redact_measurements=True)
    a = Selection.from_options(opt)
    assert a == Selection(doses=True, measurements=True)
    assert a.drug_names and a.lab_names and not a.drugs and not a.lab_tests


def test_lists(small_lexicon: Path) -> None:
    search = medical_module.standard(ALL)
    assert search is not None
    assert search.recogniser.per_class == {"Medikament": 6, "Laborwert": 6}  # no ASS, Sauerstoff
    assert search.missing_lists() == []
    both = search.recogniser.candidates("Kalium und Kaliums, TSH")
    assert [k.classes for k in both] == [("Laborwert", "Medikament")] * 2 + [("Laborwert",)]
    assert _spans("ASS 100 mg, Sauerstoff 2 l/min, Zuname 12 mg/l, Marcumar nach INR.") == [
        ("Marcumar", "Medikament")]


# -- medications: substance and dose as separate levels -------------------------------------------
def test_drugs_two_levels(small_lexicon: Path) -> None:
    text = ("Levetiracetam 500 mg 1-0-1, Lamotrigin 50 mg abends, 2 Tbl. Ibuprofen 400 mg bei"
            " Bedarf, Paracetamol. Donepezil 10 mg zur Nacht, p. o. Keppra seit 3 Jahren.")
    assert _spans(text, Selection(drugs=True)) == [
        ("Levetiracetam", "Medikament"), ("Lamotrigin", "Medikament"), ("Ibuprofen", "Medikament"),
        ("Paracetamol", "Medikament")]
    assert _spans(text, Selection(doses=True)) == [
        ("500 mg 1-0-1", "Dosierung"), ("50 mg abends", "Dosierung"), ("2 Tbl.", "Dosierung"),
        ("400 mg bei Bedarf", "Dosierung")]
    assert len(_spans(text, Selection(drugs=True, doses=True))) == 8


def test_dose_pattern(small_lexicon: Path) -> None:
    cases = {
        "Levetiracetam 500 mg 1-0-1, Lamotrigin 50 mg": ["500 mg 1-0-1", "50 mg"],
        "Levetiracetam\t500 mg\t1-0-1\nLamotrigin\t50 mg\t0-0-1": ["500 mg\t1-0-1", "50 mg\t0-0-1"],
        "Ibuprofen 400 mg bei Bedarf, 1 Tbl. Paracetamol 500 mg, 1-0-1":
            ["400 mg bei Bedarf", "1 Tbl.", "500 mg, 1-0-1"],
        "Levetiracetam 2019 begonnen, seit 3 Jahren, ab 8 Uhr, Lamotrigin 2× 1000 mg/d":
            ["2× 1000 mg/d"],
        "Ibuprofen ½-0-½, Paracetamol 3x1 Tbl. tgl., Lamotrigin 20 mg/kg KG i. v.":
            ["½-0-½", "3x1 Tbl. tgl.", "20 mg/kg KG i. v."],
    }
    for text, expected in cases.items():
        assert [t for t, _a in _spans(text, Selection(doses=True))] == expected, text


# -- lab values: examination and measurement as separate levels ----------------------------------
def test_lab_tests_two_levels(small_lexicon: Path) -> None:
    text = ("TSH 2,3 mU/l, Hb: 12,4 g/dl, HbA1c von 6,2 %, Kreatinin (i. S.) < 1,1 mg/dl."
            " Kontrolle des TSH in 3 Monaten; TSH 12.03.2026 abgenommen; Ferritin erniedrigt;"
            " HB 12 und hb 12 sind keine Namen; Zuname 12 mg/l.")
    assert [t for t, _a in _spans(text, Selection(lab_tests=True))] == [
        "TSH", "Hb", "HbA1c", "Kreatinin", "TSH", "TSH", "Ferritin"]
    assert _spans(text, Selection(measurements=True)) == [
        ("2,3 mU/l", "Messwert"), ("12,4 g/dl", "Messwert"), ("6,2 %", "Messwert"),
        ("< 1,1 mg/dl", "Messwert")]
    assert len(_spans(text, Selection(lab_tests=True, measurements=True))) == 11


def test_term_on_both_lists(small_lexicon: Path) -> None:
    text = "Kalium 4,1 mmol/l; Kalium 40 mmol 1-0-1; Kalium substituiert."
    assert _spans(text) == [
        ("Kalium", "Laborwert"), ("4,1 mmol/l", "Messwert"),
        ("Kalium", "Medikament"), ("40 mmol 1-0-1", "Dosierung"),
        ("Kalium", "Laborwert")]
    assert _spans(text, Selection(drugs=True)) == [("Kalium", "Medikament")] * 2
    assert _spans(text, Selection(measurements=True)) == [("4,1 mmol/l", "Messwert")]


# -- the pipeline ---------------------------------------------------------------------------------
def test_everything_stays_by_default(small_lexicon: Path, tmp_path: Path, patient: Patient,
                                     store: PseudonymStore) -> None:
    source = tmp_path / "notiz.txt"
    source.write_text(f"{patient.first_name} {patient.surname}: Levetiracetam 500 mg 1-0-1,"
                      " TSH 2,3 mU/l.\n", encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    text = res.target_txt.read_text(encoding="utf-8")
    assert res.released and "Levetiracetam 500 mg 1-0-1" in text and "TSH 2,3 mU/l" in text
    for name in ("medikamente", "dosierungen", "laborwerte", "messwerte"):
        assert res.report.options[name + "_schwaerzen"] is False


def test_text_file_every_level(small_lexicon: Path, tmp_path: Path, patient: Patient,
                               store: PseudonymStore) -> None:
    source = tmp_path / "notiz.txt"
    source.write_text(
        f"{patient.first_name} {patient.surname} nimmt Levetiracetam 500 mg 1-0-1 und Marcumar."
        " Labor: TSH 2,3 mU/l, Hb 12,4 g/dl. Levetiracetam wird fortgeführt.\n",
        encoding="utf-8")
    expectation = {  # option -> (gone, still there)
        "redact_drugs": (["Levetiracetam", "Marcumar"], ["500 mg 1-0-1", "TSH 2,3"]),
        "redact_doses": (["500 mg 1-0-1"], ["Levetiracetam", "TSH 2,3 mU/l"]),
        "redact_lab_tests": (["TSH", "Hb "], ["2,3 mU/l", "12,4 g/dl", "Levetiracetam"]),
        "redact_measurements": (["2,3 mU/l", "12,4 g/dl"], ["TSH ", "Hb ", "500 mg 1-0-1"]),
    }
    for option, (gone, kept) in expectation.items():
        opt = PipelineOptions(**{option: True}, suffix="_" + option)
        res = pseudonymise(source, patient, opt, store=store)
        assert res.released, as_text(res.report)
        text = res.target_txt.read_text(encoding="utf-8")
        for plaintext in gone:
            assert plaintext not in text, (option, text)
        for plaintext in kept:
            assert plaintext in text, (option, text)
    opt = PipelineOptions(redact_drugs=True, redact_doses=True,
                          redact_lab_tests=True, redact_measurements=True)
    res = pseudonymise(source, patient, opt, store=store)
    text = res.target_txt.read_text(encoding="utf-8")
    codes = [w for w in text.split("[")[1:] if w.startswith("Medikament ")]
    assert len(codes) == 3 and codes[0][:17] == codes[2][:17]  # same drug, same code
    for prefix in ("[Dosierung ", "[Laborwert ", "[Messwert "):
        assert prefix in text, text
    assert any(h.startswith("Medizinischer Inhalt: 8 Treffer") for h in res.report.notes)
    z = store.mapping(patient.key())
    back = reidentify(text, z, known_codes=store.codes())
    assert "Levetiracetam 500 mg 1-0-1" in back.text and "TSH 2,3 mU/l" in back.text


def test_pdf_without_green_mark_under_the_match(small_lexicon: Path, tmp_path: Path,
                                                patient: Patient,
                                                store: PseudonymStore) -> None:
    pdf = tmp_path / "brief.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"Patient: {patient.first_name} {patient.surname}", fontsize=11)
    page.insert_text((72, 120), "Medikation: Levetiracetam 500 mg", fontsize=11)
    page.insert_text((72, 140), "Levetiracetam gut vertragen", fontsize=11)
    doc.save(pdf)
    doc.close()
    opt = PipelineOptions(redact_drugs=True)
    v = prepare(pdf, patient, opt, store=store)
    matches = [f for f in v.hits if f.kind == Kind.MEDICAL]
    assert [f.rule for f in matches] == ["medizin.medikament"] * 2
    assert all(f.rects for f in matches)

    def under_matches() -> int:
        return sum(1 for r in v.medical_term_rects(0) for f in matches if f in v.hits
                   for b in f.rects if r[0] < b[2] and b[0] < r[2] and r[1] < b[3] and b[1] < r[3])

    green = len(v.medical_term_rects(0))  # "Medikation" stays green
    assert green >= 1 and under_matches() == 0
    v.discard(matches[1])  # discarded: the term is protected again and green
    assert len(v.medical_term_rects(0)) == green + 1 and under_matches() == 0
    res = finalise(v)
    assert res.released, as_text(res.report)
    with pymupdf.open(res.target_pdf) as out:
        rest = out[0].get_text()
    assert rest.count("Levetiracetam") == 1 and "[Medikament " in res.text


def test_warning_on_empty_list(tmp_path: Path, patient: Patient, store: PseudonymStore,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "leer"))
    _clear_caches()
    source = tmp_path / "notiz.txt"
    source.write_text("Levetiracetam 500 mg\n", encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(redact_doses=True),
                       store=store)
    assert any("Medikamente schwärzen gewählt, aber die Liste ist leer" in w
               for w in res.report.warnings)
    _clear_caches()


# -- interface and command line -------------------------------------------------------------------
def test_interface(qapp: QApplication, small_lexicon: Path) -> None:  # noqa: F811
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    window = MainWindow(Session())
    page = window.analyse
    boxes = (page.drugs, page.doses, page.lab_tests, page.measurements)
    assert not any(k.isChecked() for k in boxes)
    page.doses.setChecked(True)
    page.measurements.setChecked(True)
    page._apply_options()
    o = window.session.options
    assert Selection.from_options(o) == Selection(doses=True, measurements=True)
    window.close()


def test_command_line(small_lexicon: Path, tmp_path: Path, patient: Patient) -> None:
    source = tmp_path / "notiz.txt"
    source.write_text("Levetiracetam 500 mg, TSH 2,3 mU/l.\n", encoding="utf-8")
    r = CliRunner().invoke(app, [
        "pseudonymisieren", str(source), "--patient", str(_patient_file(tmp_path, patient)),
        "--speicher", str(tmp_path / "s.sqlite"), "--messwerte-schwaerzen",
        "--dosierungen-schwaerzen"])
    assert r.exit_code == 0, r.output
    text = (tmp_path / "notiz_pseudonym.txt").read_text(encoding="utf-8")
    assert "Levetiracetam [Dosierung " in text and "TSH [Messwert " in text
    assert "500 mg" not in text and "2,3" not in text


def test_dose_run_ends_at_new_amount_after_comma() -> None:
    """Inside a run, an amount with a unit behind a comma belongs to the next drug; at the
    start of a run the same token is taken."""
    text = "400 mg bei Bedarf, 1 Tbl. Paracetamol"
    first = medical_module._dose_token_after(text, 0, in_run=False)
    assert first is not None and first.group() == "400 mg"
    second = medical_module._dose_token_after(text, first.end(), in_run=True)
    assert second is not None and second.group() == "bei Bedarf"
    assert medical_module._dose_token_after(text, second.end(), in_run=True) is None
    third = medical_module._dose_token_after(text, second.end(), in_run=False)
    assert third is not None and third.group() == "1 Tbl."
    assert medical_module.dose_after(text, 0) == (0, len("400 mg bei Bedarf"))
