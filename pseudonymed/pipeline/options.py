"""What a run is told and what it hands back: options, result, output paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import identicalc
from ocrdoc import ocr as ocr_module

from pseudonymed.core.model import Hit, Report
from pseudonymed.redaction.pdf import RedactionOptions


@dataclass
class PipelineOptions:
    """Every switch of a run; the CLI options and the desktop pages map onto it."""

    date_format: str = "year"  # "year" -> [geb. 1990], "age" -> [Alter 36]
    text_file: bool = True  # write <name>_pseudonym.txt in reading order
    markdown: bool = False  # write <name>_pseudonym.md as well
    report: bool = True  # write <name>_pseudonym.report.json
    suffix: str = "_pseudonym"
    target_folder: Path | None = None
    matching: str | None = None  # decision for an uncertain match: "new" or "same:<CODE>"
    ocr: str = "auto"  # "auto", "rapidocr", "tesseract" or "off"
    ocr_languages: str = ""  # language packs: "deu+eng" (Tesseract) or "de", "latin" (RapidOCR)
    ocr_dpi: int = ocr_module.OCR_DPI
    codes: bool = True  # blank barcodes and QR codes (zxing-cpp, with ocrdoc)
    unreadable: str = "redact"  # lines OCR could not read: "redact" or "keep"
    ner: bool = False  # people and places the rules do not know (extra "ner")
    medical_terms: bool = True  # protect medical terms from NER and fuzzy OCR matching
    identifiers: bool = True  # professions, hospitals, studies, titles (identifiers step)
    # medical content, four levels, all off unless asked for (see detection.medical_content)
    redact_drugs: bool = False  # the drug name
    redact_doses: bool = False  # dose and schedule next to a drug name
    redact_lab_tests: bool = False  # the lab name, the examination
    redact_measurements: bool = False  # the measurement after a lab name
    fuzzy_text: bool = True  # text files: fuzzy search for misspelt identifiers (transcripts)
    clean_up: str = "auto"  # image-only pages: "auto" (optdoc's suggestion) or "off"
    remember_discarded: bool = False  # remember discarded hits per patient in the store
    preview: str = "auto"  # "auto" (pages that needed OCR), "always", "never"
    ocr_tables: bool = True  # Tesseract: second pass for sparse text (tables), merged in
    vocabulary: bool = True  # write <name>_pseudonym.vocabulary.txt with the discarded guesses
    structured: bool = True  # OCR text as Markdown: paragraphs joined, headings, lists, tables
    recipient: str = ""  # armoured public key of a recipient: one-time pseudonyms + capsule
    recipient_fingerprint: str = ""
    reference_population: int = identicalc.REFERENCE_GERMANY  # people the attacker chooses from
    own_places: str = ""  # places of the sender (practice, clinic), comma-separated, not counted
    redaction: RedactionOptions = field(default_factory=RedactionOptions)
    language: str = "en"  # of the report and the vocabulary file (the interface's language)

    def as_dict(self) -> dict[str, object]:
        """The options as the report records them (no paths, no key material)."""
        return {
            "date_format": self.date_format,
            "text_file": self.text_file,
            "markdown": self.markdown,
            "text_layer": self.redaction.text_layer,
            "rasterise": self.redaction.rasterise,
            "dpi": self.redaction.dpi if self.redaction.rasterise else None,
            "notice": self.redaction.notice,
            "ocr": self.ocr,
            "ocr_languages": self.ocr_languages,
            "codes": self.codes,
            "ner": self.ner,
            "medical_terms": self.medical_terms,
            "identifiers": self.identifiers,
            "redact_drugs": self.redact_drugs,
            "redact_doses": self.redact_doses,
            "redact_lab_tests": self.redact_lab_tests,
            "redact_measurements": self.redact_measurements,
            "fuzzy_text": self.fuzzy_text,
            "clean_up": self.clean_up,
            "ocr_tables": self.ocr_tables,
            "structured": self.structured,
            "recipient": self.recipient_fingerprint or None,
            "reference_population": self.reference_population,
        }


@dataclass
class Result:
    """What a finished run hands back: the files written, the report, the hits."""

    source: Path
    report: Report
    hits: list[Hit]
    code: str
    target_pdf: Path | None = None
    target_txt: Path | None = None
    target_md: Path | None = None
    target_report: Path | None = None
    target_vocabulary: Path | None = None  # discarded guesses, one term per line
    target_capsule: Path | None = None  # the mapping, encrypted to the recipient
    residual_risk: identicalc.Estimate | None = None  # what the text still reveals
    preview: list[Path] = field(default_factory=list)
    text: str = ""
    is_text: bool = False  # the source was a text file; ``target_txt`` is the output itself

    @property
    def released(self) -> bool:
        """True when the check passed and the output was kept."""
        return self.target_pdf is not None or (self.is_text and self.target_txt is not None)

    @property
    def target(self) -> Path | None:
        """The released file: the PDF, or the text for a text source."""
        return self.target_txt if self.is_text else self.target_pdf


def target_path(source: Path, opt: PipelineOptions, extension: str = ".pdf") -> Path:
    """The output path next to the source (or in the chosen folder) with the suffix."""
    folder = opt.target_folder or source.parent
    return folder / f"{source.stem}{opt.suffix}{extension}"
