"""Synthetic patients and documents for the tests.  Nothing here is real."""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pymupdf
from faker import Faker

from pseudonymed.core.model import Patient, Person

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]


def kvnr(rng: random.Random) -> str:
    digits = "".join(rng.choice("0123456789") for _ in range(9))
    return rng.choice("ABCDEFGHKLMNPQRSTUVWXYZ") + digits


def synthetic_patient(seed: int = 7, sex: str = "m") -> Patient:
    fake = Faker("de_DE")
    Faker.seed(seed)
    rng = random.Random(seed)
    first_name = fake.first_name_male() if sex == "m" else fake.first_name_female()
    surname = fake.last_name()
    while surname == first_name:
        surname = fake.last_name()
    street = f"{fake.street_name()} {rng.randint(1, 120)}"
    born = dt.date(rng.randint(1940, 2005), rng.randint(1, 12), rng.randint(1, 28))
    referrers = Person(surname=fake.last_name(), first_name=fake.first_name(), title="Dr. med.",
                       street=f"{fake.street_name()} {rng.randint(1, 50)}",
                       phones=[fake.phone_number()])
    partner = Person(surname=surname, first_name=fake.first_name_female() if sex == "m"
                     else fake.first_name_male(), phones=[fake.phone_number()])
    return Patient(
        surname=surname,
        first_name=first_name,
        sex=sex,
        birth_date=born,
        street=street,
        postcode=fake.postcode(),
        place=fake.city(),
        phones=[fake.phone_number()],
        emails=[f"{first_name}.{surname}@example.org".lower().replace("ß", "ss")],
        insurance_number=kvnr(rng),
        patient_id=str(rng.randint(10_000_000, 99_999_999)),
        case_numbers=[f"AZ {rng.randint(2020, 2026)}/{rng.randint(100, 999)}"],
        relatives=[partner],
        referrers=[referrers],
    )


def long_date(d: dt.date) -> str:
    return f"{d.day}. {MONTHS[d.month - 1]} {d.year}"


def clinical_report_text(p: Patient) -> list[list[str]]:
    """Two pages of a plausible neurological report mentioning the patient in many forms."""
    born = p.birth_date
    assert born is not None
    z = p.referrers[0]
    a = p.relatives[0]
    salutation = "Herrn" if p.male else "Frau"
    page1 = [
        "Neurologische Gemeinschaftspraxis am Stadtpark",
        "Dr. med. Erika Beispiel · Fachärztin für Neurologie",
        f"Praxis {z.title} {z.full_name}",
        f"{z.street}",
        "",
        f"Betr.: {salutation} {p.full_name}, geb. {born.strftime('%d.%m.%Y')}",
        f"{p.street}, {p.postcode} {p.place}",
        f"Tel. {p.phones[0]}, E-Mail {p.emails[0]}",
        f"Versicherten-Nr. {p.insurance_number}   Pat.-ID {p.patient_id}   {p.case_numbers[0]}",
        "",
        f"Sehr geehrte Frau Kollegin {z.surname},",
        "",
        f"vielen Dank für die Überweisung von {salutation} {p.surname}, der sich am 12.03.2026",
        "erstmals in unserer Sprechstunde vorstellte. Die Anamnese wurde gemeinsam mit",
        f"der Ehefrau, Frau {a.full_name}, erhoben.",
        "",
        "Diagnosen: Leichte kognitive Störung (ICD-10 F06.7), arterielle Hypertonie.",
        "Medikation: Ramipril 5 mg 1-0-0, Atorvastatin 20 mg 0-0-1, max. 3 Tabletten.",
        "Befund: MMST 26/30, MoCA 22/30, Uhrentest unauffällig.",
    ]
    page2 = [
        f"Fortsetzung Befund {p.surname.upper()}, {p.first_name.upper()}"
        f" (*{born.strftime('%d.%m.%y')})",
        "",
        f"{p.first_name}s Beschwerden bestehen seit etwa einem Jahr. Geboren am {long_date(born)}",
        f"in {p.place}. {p.surname}, {p.first_name} berichtet über Wortfindungsstörungen.",
        f"Im Verlauf zeigt {salutation.replace('Herrn', 'Herr')} {p.first_name[0]}. {p.surname}",
        "eine stabile Befundlage. Kontrolle in 6 Monaten empfohlen.",
        "",
        "Mit freundlichen kollegialen Grüßen",
        "Dr. med. Erika Beispiel",
    ]
    return [page1, page2]


def misspelt(name: str) -> str:
    """The name as a speech recogniser might write it: one letter dropped."""
    return name[:2] + name[3:] if len(name) >= 5 else name + "n"


def transcript_text(p: Patient) -> list[str]:
    """A consultation as a local transcriber would write it: the patient in many
    forms, once misspelt, plus medical terms that must survive."""
    born = p.birth_date
    assert born is not None
    z = p.referrers[0]
    a = p.relatives[0]
    salutation = "Herr" if p.male else "Frau"
    return [
        f"Arzt: Guten Tag, {salutation} {p.surname}. Sie sind am {long_date(born)} geboren?",
        f"Patient: Ja, am {born.strftime('%d.%m.%Y')}. Ich wohne in der {p.street} in {p.place}.",
        f"Arzt: {salutation} {misspelt(p.surname)}, was führt Sie zu mir?",
        f"Patient: Meine Frau, {a.full_name}, hat gemerkt, dass ich Wörter vergesse.",
        f"Arzt: Überwiesen hat Sie {z.title} {z.surname}. Nehmen Sie Medikamente?",
        "Patient: Ramipril und Atorvastatin. Arterielle Hypertonie habe ich seit zehn Jahren.",
        "Arzt: Der MoCA ergab 22 von 30 Punkten, das passt zu einer leichten kognitiven Störung.",
        f"Patient: Erreichen können Sie mich unter {p.phones[0]} oder {p.emails[0]}.",
        f"Arzt: Danke, {p.first_name}. Wir sehen uns in sechs Monaten wieder.",
    ]


def create_transcript(path: Path, p: Patient, format_: str = "txt") -> Path:
    """Write the transcript as plain text, SRT or WebVTT (one cue per line)."""
    lines = transcript_text(p)
    if format_ == "txt":
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif format_ == "md":
        path.write_text("# Gespräch\n\n" + "\n\n".join(f"- {z}" for z in lines) + "\n",
                        encoding="utf-8")
    elif format_ in ("srt", "vtt"):
        separator = "," if format_ == "srt" else "."
        blocks = ["WEBVTT", ""] if format_ == "vtt" else []
        for i, line in enumerate(lines, 1):
            begin, end = 4 * (i - 1), 4 * i - 1
            time = (f"00:00:{begin:02d}{separator}000 --> 00:00:{end:02d}{separator}500")
            blocks += ([str(i)] if format_ == "srt" else []) + [time, line, ""]
        path.write_text("\n".join(blocks), encoding="utf-8")
    else:
        raise ValueError(format_)
    return path


def create_report_pdf(path: Path, p: Patient, with_metadata: bool = True) -> Path:
    doc = pymupdf.open()
    for lines in clinical_report_text(p):
        page = doc.new_page()
        y = 72.0
        for line in lines:
            page.insert_text((56, y), line, fontsize=10.5, fontname="helv")
            y += 14.5
    if with_metadata:
        doc.set_metadata({"title": f"Befund {p.full_name}", "author": "Dr. Erika Beispiel",
                          "subject": p.full_name, "keywords": p.insurance_number})
        doc.set_toc([[1, f"Befund {p.surname}", 1]])
        doc[0].add_text_annot((400, 60), f"Rückfrage {p.surname}")
        doc.embfile_add("notiz.txt", f"Notiz zu {p.full_name}".encode())
    doc.save(path, garbage=0)
    doc.close()
    return path


def _fold(line: str) -> str:
    """Fold at 75 octets as vCard writers do, to exercise unfolding."""
    raw = line.encode("utf-8")
    parts = []
    while len(raw) > 75:
        cut = 75
        while cut > 1 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    parts.append(raw.decode("utf-8"))
    return "\r\n".join(parts)


def _vcard_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def vcard_from_patient(p: Patient, uid: str | None = None) -> str:
    """A card as DoctoNext writes it (vCard 4.0, salutation in N, GENDER, X fields)."""
    salutation = "Herr" if p.male else "Frau" if p.female else ""
    uid = uid or f"synthetisch-{abs(hash(p.key())) % 10**8}"
    lines = [
        "BEGIN:VCARD",
        "VERSION:4.0",
        f"UID:{uid}",
        f"FN:{_vcard_escape(p.full_name)}",
        f"N:{_vcard_escape(p.surname)};{_vcard_escape(p.first_name)};;{salutation};",
    ]
    if p.birth_date:
        lines.append(f"BDAY:{p.birth_date.strftime('%Y%m%d')}")
    if p.sex:
        lines.append(f"GENDER:{'M' if p.male else 'F'}")
    for i, t in enumerate(p.phones):
        lines.append(f"TEL;TYPE={'cell' if i == 0 else 'voice'}:{t}")
    for e in p.emails:
        lines.append(f"EMAIL;TYPE=home:{e}")
    if p.street or p.place:
        lines.append(f"ADR;TYPE=home:;;{_vcard_escape(p.street)};{_vcard_escape(p.place)};;{p.postcode};")
    if p.birth_name:
        lines.append(f"X-MAIDENNAME:{_vcard_escape(p.birth_name)}")
    if p.patient_id:
        lines.append(f"X-DOCTOLIB-PATIENT-ID:{p.patient_id}")
    note = []
    if p.insurance_number:
        note.append(f"KVNR {p.insurance_number}")
    if p.relatives:
        a = p.relatives[0]
        note.append(f"Ehepartner: {a.full_name}, erreichbar unter {a.phones[0]}")
    if note:
        lines.append("NOTE:" + _vcard_escape(", ".join(note)))
    lines.append("X-DOCTOLIB-INSURANCE-SECTOR:private")
    lines.append("CATEGORIES:Doctolib,Privatpatient")
    lines.append("REV:20261030T120000Z")
    lines.append("END:VCARD")
    return "\r\n".join(_fold(z) for z in lines) + "\r\n"
