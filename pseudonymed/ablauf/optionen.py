"""What a run is told and what it hands back: options, result, output paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.kern.modell import Fundstelle, Pruefbericht
from pseudonymed.pruefung import restrisiko as risikomodul
from pseudonymed.schwaerzen.pdf import Schwaerzoptionen


@dataclass
class Ablaufoptionen:
    """Every switch of a run; the CLI options and the desktop pages map onto it."""

    datumsform: str = "jahr"  # "jahr" -> [geb. 1990], "alter" -> [Alter 36]
    textdatei: bool = True  # write <name>_pseudonym.txt in reading order
    markdown: bool = False  # write <name>_pseudonym.md as well
    bericht: bool = True  # write <name>_pseudonym.bericht.json
    suffix: str = "_pseudonym"
    ziel_ordner: Path | None = None
    abgleich: str | None = None  # decision for an uncertain match: "neu" or "gleich:<KENNUNG>"
    ocr: str = "auto"  # "auto", "rapidocr", "tesseract" or "aus"
    ocr_sprachen: str = ""  # language packs: "deu+eng" (Tesseract) or "de", "latin" (RapidOCR)
    ocr_dpi: int = ocrmodul.OCR_DPI
    codes: bool = True  # blank barcodes and QR codes (extra "codes")
    unlesbar: str = "schwaerzen"  # lines OCR could not read: "schwaerzen" or "lassen"
    ner: bool = False  # people and places the rules do not know (extra "ner")
    fachbegriffe: bool = True  # protect medical terms from NER and fuzzy OCR matching
    identifikatoren: bool = True  # professions, hospitals, studies, titles (identifiers step)
    # medical content, four levels, all off unless asked for (see erkennung.medizininhalte)
    medikamente_schwaerzen: bool = False  # the drug name
    dosierungen_schwaerzen: bool = False  # dose and schedule next to a drug name
    laborwerte_schwaerzen: bool = False  # the lab name, the examination
    messwerte_schwaerzen: bool = False  # the measurement after a lab name
    unscharf_text: bool = True  # text files: fuzzy search for misspelt identifiers (transcripts)
    aufbereiten: str = "auto"  # image-only pages: "auto" (suggested clean-up) or "aus"
    verworfene_merken: bool = False  # remember discarded hits per patient in the store
    aufbereitung: dict[int, aufmodul.Aufbereitung] | None = None  # explicit choice per page
    vorschau: str = "auto"  # "auto" (pages that needed OCR), "immer", "nie"
    papierformat: str = "a4"  # page size for photos and images: a4, a5, a3, letter, legal, bild
    ocr_tabellen: bool = True  # Tesseract: second pass for sparse text (tables), merged in
    vokabular: bool = True  # write <name>_pseudonym.vokabular.txt with the discarded guesses
    gliedern: bool = True  # OCR text as Markdown: paragraphs joined, headings, lists, tables
    empfaenger: str = ""  # armoured public key of a recipient: one-time pseudonyms + capsule
    empfaenger_fingerabdruck: str = ""
    referenzpopulation: int = risikomodul.REFERENZ_DEUTSCHLAND  # people the attacker chooses from
    eigene_orte: str = ""  # places of the sender (practice, clinic), comma-separated, not counted
    schwaerzen: Schwaerzoptionen = field(default_factory=Schwaerzoptionen)

    def als_dict(self) -> dict[str, object]:
        """The options as the report records them (no paths, no key material)."""
        return {
            "datumsform": self.datumsform,
            "textdatei": self.textdatei,
            "markdown": self.markdown,
            "textebene": self.schwaerzen.textebene,
            "rastern": self.schwaerzen.rastern,
            "dpi": self.schwaerzen.dpi if self.schwaerzen.rastern else None,
            "vermerk": self.schwaerzen.vermerk,
            "ocr": self.ocr,
            "ocr_sprachen": self.ocr_sprachen,
            "codes": self.codes,
            "ner": self.ner,
            "fachbegriffe": self.fachbegriffe,
            "identifikatoren": self.identifikatoren,
            "medikamente_schwaerzen": self.medikamente_schwaerzen,
            "dosierungen_schwaerzen": self.dosierungen_schwaerzen,
            "laborwerte_schwaerzen": self.laborwerte_schwaerzen,
            "messwerte_schwaerzen": self.messwerte_schwaerzen,
            "unscharf_text": self.unscharf_text,
            "aufbereiten": self.aufbereiten,
            "papierformat": self.papierformat,
            "ocr_tabellen": self.ocr_tabellen,
            "gliedern": self.gliedern,
            "empfaenger": self.empfaenger_fingerabdruck or None,
            "referenzpopulation": self.referenzpopulation,
        }


@dataclass
class Ergebnis:
    """What a finished run hands back: the files written, the report, the hits."""

    quelle: Path
    bericht: Pruefbericht
    fundstellen: list[Fundstelle]
    kennung: str
    ziel_pdf: Path | None = None
    ziel_txt: Path | None = None
    ziel_md: Path | None = None
    ziel_bericht: Path | None = None
    ziel_vokabular: Path | None = None  # discarded guesses, one term per line
    ziel_kapsel: Path | None = None  # the mapping, encrypted to the recipient
    restrisiko: risikomodul.Restrisiko | None = None  # what the text still reveals
    vorschau: list[Path] = field(default_factory=list)
    text: str = ""
    ist_text: bool = False  # the source was a text file; ``ziel_txt`` is the output itself

    @property
    def freigegeben(self) -> bool:
        """True when the check passed and the output was kept."""
        return self.ziel_pdf is not None or (self.ist_text and self.ziel_txt is not None)

    @property
    def ziel(self) -> Path | None:
        """The released file: the PDF, or the text for a text source."""
        return self.ziel_txt if self.ist_text else self.ziel_pdf


def zielpfad(quelle: Path, opt: Ablaufoptionen, endung: str = ".pdf") -> Path:
    """The output path next to the source (or in the chosen folder) with the suffix."""
    ordner = opt.ziel_ordner or quelle.parent
    return ordner / f"{quelle.stem}{opt.suffix}{endung}"
