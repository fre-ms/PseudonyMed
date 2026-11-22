"""Text files (transcripts, notes, subtitles): rules, fuzzy search, marking, output."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pseudonymed.checks.report import as_text
from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.detection import ner as ner_module
from pseudonymed.document.text_file import analysis_text, is_text_file, read_text_file
from pseudonymed.pipeline import (
    PipelineOptions,
    TextPreparation,
    finalise,
    prepare,
    pseudonymise,
)
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.synthetic import create_transcript, misspelt
from tests.test_pipeline import found_plain_data

_TIMING = re.compile(r"^\d\d:\d\d:\d\d[,.]\d{3} --> .*$", re.MULTILINE)


@pytest.fixture
def transcript(tmp_path: Path, patient: Patient) -> Path:
    return create_transcript(tmp_path / "gespraech.txt", patient)


def test_text_file_round_trip(transcript: Path, patient: Patient,
                              store: PseudonymStore) -> None:
    res = pseudonymise(transcript, patient, PipelineOptions(), store=store)
    assert res.released and res.is_text and res.target_pdf is None, as_text(res.report)
    assert res.target == res.target_txt == transcript.with_name("gespraech_pseudonym.txt")
    out = res.target.read_text(encoding="utf-8")
    assert found_plain_data(out, patient) == []
    assert misspelt(patient.surname) not in out  # the misspelt surname: fuzzy search
    assert any(f.source == "unscharf" for f in res.hits)
    for kept in ("Ramipril", "Atorvastatin", "Arterielle Hypertonie", "MoCA ergab 22 von 30"):
        assert kept in out
    assert f"Patient {res.code}" in out and f"Angehörige {res.code}-1" in out
    assert "[geb. " in out and "[Straße " in out and "[Ort " in out
    assert "[Telefon] oder [E-Mail]." in out  # sentence end after an address is kept
    assert out.count("\n") == transcript.read_text(encoding="utf-8").count("\n")
    report = json.loads(res.target_report.read_text(encoding="utf-8"))
    assert report["bestanden"] and report["optionen"]["unscharf_text"] is True
    assert found_plain_data(json.dumps(report), patient) == []


@pytest.mark.parametrize("format_", ["srt", "vtt", "md"])
def test_subtitles_and_markdown_stay_intact(tmp_path: Path, patient: Patient,
                                            store: PseudonymStore, format_: str) -> None:
    source = create_transcript(tmp_path / f"gespraech.{format_}", patient, format_)
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    assert res.target.suffix == f".{format_}"
    before = source.read_text(encoding="utf-8")
    out = res.target.read_text(encoding="utf-8")
    assert found_plain_data(out, patient) == []
    assert _TIMING.findall(before) == _TIMING.findall(out)
    if format_ == "srt":
        assert re.findall(r"^\d+$", before, re.MULTILINE) == re.findall(r"^\d+$", out, re.MULTILINE)
    if format_ == "vtt":
        assert out.startswith("WEBVTT")
    if format_ == "md":
        assert out.startswith("# Gespräch") and out.count("\n- ") == before.count("\n- ")


def test_analysis_text_hides_only_timestamps() -> None:
    srt = ("1\n00:00:01,000 --> 00:00:03,500 X:10\nHallo Herr Flantz.\n\n"
           "2\n00:00:04,000 --> 00:00:05,000\n")
    a = analysis_text(srt, ".srt")
    assert len(a) == len(srt) and a.count("\n") == srt.count("\n")
    assert "Hallo Herr Flantz." in a and "-->" not in a and a.split("\n")[0].strip() == ""
    txt = "[00:00.000 --> 00:04.000]  Guten Tag"
    assert analysis_text(txt, ".txt").strip() == "Guten Tag"
    assert analysis_text("3\nZeile 3\n", ".txt") == "3\nZeile 3\n"  # numbers stay outside subtitles
    assert is_text_file("a.TXT") and is_text_file("b.vtt") and not is_text_file("c.pdf")


def test_exact_leaves_misspellings_standing(transcript: Path, patient: Patient,
                                            store: PseudonymStore) -> None:
    res = pseudonymise(transcript, patient, PipelineOptions(fuzzy_text=False),
                       store=store)
    assert res.released
    out = res.target.read_text(encoding="utf-8")
    assert misspelt(patient.surname) in out and patient.surname not in out
    assert not any(f.source == "unscharf" for f in res.hits)


def test_capitalisation_protects_small_words(tmp_path: Path,
                                             store: PseudonymStore) -> None:
    p = Patient(surname="Lang", first_name="Erwin", sex="m")
    source = tmp_path / "n.txt"
    source.write_text("Herr Lanq wartet schon lang. Land und Leute. Herr Lang kommt.\n",
                      encoding="utf-8")
    res = pseudonymise(source, p, PipelineOptions(), store=store)
    out = res.target.read_text(encoding="utf-8")
    assert "lang." in out and "Lanq" not in out and "Herr Lang" not in out
    # "Land" is capitalised and within tolerance: a guess the review step shows in orange
    assert any(f.source == "unscharf" and f.text == "Land" for f in res.hits)


def test_discard_mark_and_recheck(transcript: Path, patient: Patient,
                                  store: PseudonymStore) -> None:
    v = prepare(transcript, patient, PipelineOptions(), store=store)
    assert isinstance(v, TextPreparation) and v.is_text and v.text
    assert v.medical_term_count >= 3 and v.medical_term_spans(0)
    assert v.medical_term_rects(0) == [] and v.ocr_pages == []
    referrers = next(f for f in v.hits if f.rule == "zuweiser1.name")
    v.discard(referrers)
    assert referrers.text not in [f.text for f in v.hits]
    a = v.text.index("Wörter vergesse")
    f = v.mark(0, a, a + len("Wörter vergesse"), "[Symptom]")
    assert f.source == "manuell" and f.text == "Wörter vergesse"
    with pytest.raises(ValueError):
        v.mark(0, 0, 0)
    res = finalise(v)
    assert res.released, as_text(res.report)
    out = res.target.read_text(encoding="utf-8")
    assert referrers.text in out and "[Symptom]" in out and "Wörter vergesse" not in out
    assert found_plain_data(out.replace(referrers.text, ""), patient) == []


def test_marking_replaces_overlapping_matches(transcript: Path, patient: Patient,
                                              store: PseudonymStore) -> None:
    v = prepare(transcript, patient, PipelineOptions(), store=store)
    a = v.text.index("Meine Frau")
    b = v.text.index("hat gemerkt")
    v.mark(0, a, b, "[Angabe]")
    assert not any(f.rule == "angehoerige1.vollname" for f in v.hits)
    res = finalise(v)
    assert res.released and "[Angabe]" in res.text


def test_encoding_windows(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    source = tmp_path / "notiz.txt"
    source.write_bytes(f"Herr {patient.surname} klagt über Schwäche.\r\n".encode("cp1252"))
    assert read_text_file(source) == f"Herr {patient.surname} klagt über Schwäche.\n"
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released and "Schwäche" in res.target.read_text(encoding="utf-8")


@pytest.mark.skipif(not ner_module.available(), reason="Extra 'ner' nicht installiert")
def test_ner_in_text_file(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    source = tmp_path / "n.txt"
    source.write_text("Die Nachbarin Erika Hoffmann brachte ihn. Verdacht auf Morbus Parkinson,"
                      " Babinski negativ. Er lebt in Dresden.\n", encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(ner=True), store=store)
    out = res.target.read_text(encoding="utf-8")
    assert "Erika Hoffmann" not in out and "Dresden" not in out
    assert "Morbus Parkinson" in out and "Babinski" in out
    assert any(f.rule == "ner.person" for f in res.hits)
    source.write_text("Überwiesen hat Sie die Hausärztin. Geboren ist er in Dresden.\n",
                      encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(ner=True), store=store)
    out = res.target.read_text(encoding="utf-8")
    assert out.startswith("Überwiesen hat") and "Dresden" not in out  # a verb is not a place


def test_cli_text_file(tmp_path: Path, patient: Patient) -> None:
    from tests.test_cli import _patient_file

    runner = CliRunner()
    pfile = _patient_file(tmp_path, patient)
    source = create_transcript(tmp_path / "gespraech.txt", patient)
    sp = str(tmp_path / "p.sqlite")
    r = runner.invoke(app, ["pseudonymisieren", str(source), "--patient", str(pfile),
                            "--speicher", sp])
    assert r.exit_code == 0, r.output
    target = tmp_path / "gespraech_pseudonym.txt"
    assert target.exists() and "Freigegeben" in r.output and "Text:" not in r.output
    assert found_plain_data(r.output, patient) == []
    r = runner.invoke(app, ["pruefen", str(target), "--patient", str(pfile), "--speicher", sp])
    assert r.exit_code == 0 and "bestanden" in r.output, r.output
    r = runner.invoke(app, ["pruefen", str(source), "--patient", str(pfile), "--speicher", sp])
    assert r.exit_code == 2 and "NICHT" in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(source), "--patient", str(pfile),
                            "--speicher", sp, "--genau"])
    assert r.exit_code == 0 and misspelt(patient.surname) in target.read_text(encoding="utf-8")


# -- Whisper JSON and e-mail ------------------------------------------------------------------


def _whisper_json(lines: list[str], dialect: str) -> dict | list:
    """The same transcript as openai-whisper, whisper.cpp or WhisperX would write it."""
    if dialect == "openai":
        segments = [{"id": i, "seek": 0, "start": 4.0 * i, "end": 4.0 * i + 3.5, "text": " " + z,
                     "tokens": [50364, 1000 + i, 50540], "temperature": 0.0,
                     "avg_logprob": -0.2, "compression_ratio": 1.3, "no_speech_prob": 0.01,
                     "words": [{"word": " " + w, "start": 4.0 * i, "end": 4.0 * i + 0.3,
                                "probability": 0.9} for w in z.split()]}
                    for i, z in enumerate(lines)]
        return {"text": " ".join(lines), "segments": segments, "language": "de"}
    if dialect == "cpp":
        return {"systeminfo": "AVX = 1", "model": {"type": "large"}, "params": {"language": "de"},
                "result": {"language": "de"},
                "transcription": [{"timestamps": {"from": f"00:00:{4 * i:02d},000",
                                                  "to": f"00:00:{4 * i + 3:02d},500"},
                                   "offsets": {"from": 4000 * i, "to": 4000 * i + 3500},
                                   "text": " " + z} for i, z in enumerate(lines)]}
    if dialect == "whisperx":
        return {"segments": [{"start": 4.0 * i, "end": 4.0 * i + 3.5, "text": z,
                              "speaker": f"SPEAKER_0{i % 2}",
                              "words": [{"word": w, "score": 0.9} for w in z.split()]}
                             for i, z in enumerate(lines)],
                "word_segments": []}
    raise ValueError(dialect)


@pytest.mark.parametrize("dialect", ["openai", "cpp", "whisperx"])
def test_whisper_json(tmp_path: Path, patient: Patient, store: PseudonymStore,
                      dialect: str) -> None:
    from tests.synthetic import transcript_text

    lines = transcript_text(patient)
    source = tmp_path / "gespraech.json"
    source.write_text(json.dumps(_whisper_json(lines, dialect), ensure_ascii=False),
                      encoding="utf-8")
    assert is_text_file(source)
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    assert res.target == tmp_path / "gespraech_pseudonym.txt"
    out = res.target.read_text(encoding="utf-8")
    assert found_plain_data(out, patient) == []
    assert misspelt(patient.surname) not in out
    assert out.count("\n") == len(lines)
    assert "[00:00:00.000 --> 00:00:03.500] " in out
    assert "[00:00:04.000 --> 00:00:07.500] " in out
    assert "Ramipril und Atorvastatin" in out and "50364" not in out
    if dialect == "whisperx":
        assert "SPEAKER_00: " in out and "SPEAKER_01: " in out
    assert f"Patient {res.code}" in out


def test_json_without_transcript(tmp_path: Path, patient: Patient,
                                 store: PseudonymStore) -> None:
    from pseudonymed.document.text_file import render_whisper

    assert render_whisper({"text": " Nur Text. "}) == "Nur Text.\n"
    assert render_whisper([{"text": "a"}, {"text": "b"}]) == "a\nb\n"
    with pytest.raises(ValueError):
        render_whisper({"foo": 1})
    source = tmp_path / "x.json"
    source.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        prepare(source, patient, PipelineOptions(), store=store)


def _mail(patient: Patient, html_only: bool = False) -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = f"{patient.full_name} <{patient.emails[0]}>"
    m["To"] = "Sprechstunde Kognitive Neurologie <sprechstunde@example.org>"
    m["Cc"] = f"{patient.relatives[0].full_name} <partner@example.net>"
    m["Subject"] = f"Rückfrage zu meinem Termin – {patient.surname}"
    m["Date"] = "Tue, 21 Jul 2026 10:00:00 +0200"
    m["Message-ID"] = "<geheim-123@mail.example.org>"
    m["Received"] = "from mx.example.org by mail.example.org"
    text = (f"Sehr geehrte Frau Doktor,\n\nmein Name ist {patient.full_name}, geboren am "
            f"{patient.birth_date.strftime('%d.%m.%Y')}, wohnhaft {patient.street}, "
            f"{patient.postcode} {patient.place}. Meine Versichertennummer lautet "
            f"{patient.insurance_number}.\nIch nehme Ramipril 5 mg und habe Schwäche im "
            f"linken Arm.\n\nViele Grüße\n{patient.first_name} {patient.surname}\n"
            f"Tel. {patient.phones[0]}\n")
    html = "<html><head><style>p{color:red}</style></head><body>" + "".join(
        f"<p>{z}</p>" for z in text.split("\n\n")) + "<br>&Uuml;bermittelt per Portal</body></html>"
    if html_only:
        m.set_content(html, subtype="html", charset="utf-8")
    else:
        m.set_content(text, charset="utf-8", cte="quoted-printable")
        m.add_alternative(html, subtype="html", charset="utf-8")
    m.add_attachment(b"%PDF-1.4 synthetisch", maintype="application", subtype="pdf",
                     filename=f"Befund_{patient.surname}_2026.pdf")
    return bytes(m)


def test_eml(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    from pseudonymed.document.text_file import render_mail

    raw = _mail(patient)
    assert "quoted-printable" in raw.decode("ascii", "replace")
    text = render_mail(raw)
    assert text.startswith("Von: ") and "\nBetreff: Rückfrage zu meinem Termin" in text
    assert "Message-ID" not in text and "geheim-123" not in text and "Received" not in text
    assert "Sehr geehrte Frau Doktor," in text and "<p>" not in text and "color:red" not in text
    assert f"[Anhänge, nicht enthalten: Befund {patient.surname} 2026 (pdf)]" in text
    source = tmp_path / "anfrage.eml"
    source.write_bytes(raw)
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    assert res.target == tmp_path / "anfrage_pseudonym.txt"
    out = res.target.read_text(encoding="utf-8")
    assert found_plain_data(out, patient) == []
    assert "partner@example.net" not in out and "sprechstunde@example.org" not in out
    assert "Ramipril 5 mg" in out and "Schwäche im linken Arm" in out and "Viele Grüße" in out
    assert f"Patient {res.code}" in out and "[E-Mail]" in out
    assert "[Anhänge, nicht enthalten: Befund Patient" in out


def test_eml_html_only(tmp_path: Path, patient: Patient, store: PseudonymStore) -> None:
    from pseudonymed.document.text_file import html_to_text, render_mail

    text = render_mail(_mail(patient, html_only=True))
    assert "Sehr geehrte Frau Doktor,\n" in text and "Übermittelt per Portal" in text
    assert "<" not in text.split("\n\n", 1)[1] and "color:red" not in text
    assert html_to_text("<ul><li>a</li><li>b &amp; c</li></ul><script>x()</script>") == "a\nb & c"
    source = tmp_path / "html.eml"
    source.write_bytes(_mail(patient, html_only=True))
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released and found_plain_data(res.text, patient) == []


def test_number_words_in_the_transcript(tmp_path: Path, patient: Patient,
                                        store: PseudonymStore) -> None:
    """Spoken numbers: date of birth, house number, postcode as words."""
    from pseudonymed.detection.number_words import cardinal
    from tests.synthetic import MONTHS

    born = patient.birth_date
    assert born is not None
    tag = {1: "ersten", 2: "zweiten", 3: "dritten", 7: "siebten", 8: "achten"}.get(
        born.day, f"{_number_word(born.day)}{'sten' if born.day >= 20 else 'ten'}")
    year = (f"neunzehnhundert{_number_word(born.year % 100)}" if born.year < 2000
            else _number_word(born.year))
    month = MONTHS[born.month - 1]
    street, number = patient.street.rsplit(" ", 1)
    postcode = " ".join(_number_word(int(z)) for z in patient.postcode)
    text = (f"Arzt: Wann sind Sie geboren?\nPatient: Am {tag} {month} {year}.\n"
            f"Arzt: Und Ihre Anschrift?\nPatient: {street} {_number_word(int(number))}, "
            f"Postleitzahl {postcode} {patient.place}.\n"
            "Arzt: Sie nehmen zwei Tabletten Ramipril.\n")
    assert cardinal(year) == born.year
    source = tmp_path / "zahlen.txt"
    source.write_text(text, encoding="utf-8")
    res = pseudonymise(source, patient, PipelineOptions(), store=store)
    assert res.released, as_text(res.report)
    out = res.target.read_text(encoding="utf-8")
    assert f"Patient: Am [geb. {born.year}].\n" in out, out
    assert "Patient: [Straße " in out and ", Postleitzahl [PLZ Ort " in out, out
    assert street not in out and patient.place not in out and year not in out and tag not in out
    assert "zwei Tabletten Ramipril" in out
    assert found_plain_data(out, patient) == []


def _number_word(n: int) -> str:
    ones = ["null", "ein", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun",
            "zehn", "elf", "zwölf", "dreizehn", "vierzehn", "fünfzehn", "sechzehn", "siebzehn",
            "achtzehn", "neunzehn"]
    tens = {2: "zwanzig", 3: "dreißig", 4: "vierzig", 5: "fünfzig", 6: "sechzig", 7: "siebzig",
            8: "achtzig", 9: "neunzig"}
    if n == 1:
        return "eins"
    if n < 20:
        return ones[n]
    if n < 100:
        z, e = divmod(n, 10)
        return (f"{ones[e]}und" if e else "") + tens[z]
    if n < 1000:
        h, r = divmod(n, 100)
        return f"{ones[h]}hundert" + (_number_word(r).replace("eins", "eins") if r else "")
    t, r = divmod(n, 1000)
    return f"{ones[t]}tausend" + (_number_word(r) if r else "")
