"""The working state of a document: everything up to the visual review, saved and reloaded.

Cleaning up a scan, reading it (OCR, corrections) and going through the review take time
and judgement.  The working state keeps all of it, so that another pseudonymisation
variant of the same document (other options, other hits kept or discarded) does not start
from scratch.  It is two files next to each other:

* ``<name>.arbeitsstand.pdf`` (or ``.txt``, ``.srt`` … for text files): the document as
  prepared, that is after the clean-up and in clear text, **not** redacted;
* ``<name>.arbeitsstand.json``: the patient, the options, the clean-up plan, every page
  (text layer, OCR words with their boxes, the corrected OCR text), the hits with their
  pseudonyms, the discarded hits, the manual boxes and marks.

Both files hold clear data and are to be kept like the original.  Dropping either of them
on the documents step restores the review: patient, reading and hits are filled in, the
review can change the selection, and a new analysis keeps the reading and the decisions
(:func:`uebernehme_entscheidungen`).  A state saved in recipient mode (one-time key) cannot
be restored, so it is refused.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from pseudonymed import WERKZEUG
from pseudonymed.ablauf import (
    Ablaufoptionen,
    Lesung,
    Ruestzeug,
    Seite,
    Textvorbereitung,
    Vorbereitung,
    merke_fachbegriffe,
    ruestzeug,
)
from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import gliederung as gliedmodul
from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.kern.modell import Fundstelle, Patient
from pseudonymed.pruefung.gegenprobe import toleranzschluessel
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.schwaerzen.pdf import Schwaerzoptionen

MARKE = "pseudonymed_arbeitsstand"
VERSION = 1
KENNZEICHEN = ".arbeitsstand"
ENDUNG = KENNZEICHEN + ".json"
_OPTIONEN_AUSSEN = ("aufbereitung", "empfaenger", "empfaenger_fingerabdruck", "abgleich")


class ArbeitsstandFehler(Exception):
    """A state cannot be captured, read or restored; the message says why."""


@dataclass
class Arbeitsstand:
    """A parsed state: the JSON data plus the document bytes."""

    daten: dict[str, Any]
    dokument: bytes
    pfad: Path | None = None  # the JSON file, None for an in-memory capture
    ordner: Path | None = None  # in-memory capture: the folder of the source

    @property
    def ist_text(self) -> bool:
        """True for a text file (transcript, note, subtitles), False for a PDF."""
        return bool(self.daten["dokument"]["ist_text"])

    @property
    def patient(self) -> Patient:
        """The patient the state was saved for."""
        return Patient.aus_dict(self.daten["patient"])

    @property
    def kennung(self) -> str:
        """The identifier the pseudonyms were issued under; empty when the state has none."""
        return str(self.daten.get("kennung", ""))

    @property
    def optionen(self) -> Ablaufoptionen:
        """The pipeline options as they were saved."""
        return _optionen_aus(self.daten["optionen"])

    @property
    def quelle(self) -> Path:
        """Path the outputs are named after: the original file name in the state's folder
        (an in-memory capture keeps the source's folder)."""
        ordner = self.pfad.parent if self.pfad is not None else (self.ordner or Path.cwd())
        return ordner / str(self.daten["dokument"]["quelle"])

    @property
    def dokumentpfad(self) -> Path | None:
        """The prepared document next to the JSON file; None for an in-memory capture."""
        if self.pfad is None:
            return None
        return self.pfad.parent / str(self.daten["dokument"]["datei"])


# -- recognising the files -------------------------------------------------------------------------
def ist_arbeitsstand(pfad: str | Path) -> Path | None:
    """The JSON file of a working state when ``pfad`` is that file or the document next
    to it (``x.arbeitsstand.pdf`` beside ``x.arbeitsstand.json``), else None."""
    pfad = Path(pfad)
    if pfad.name.endswith(ENDUNG):
        try:
            with pfad.open("rb") as f:
                return pfad if MARKE.encode() in f.read(4096) else None
        except OSError:
            return None
    name = pfad.name
    if KENNZEICHEN + "." in name:
        stamm = name[:name.index(KENNZEICHEN)]
        kandidat = pfad.with_name(stamm + ENDUNG)
        if kandidat.is_file():
            return ist_arbeitsstand(kandidat)
    return None


# -- capturing -------------------------------------------------------------------------------------
def _optionen_als_dict(opt: Ablaufoptionen) -> dict[str, Any]:
    """The options as JSON data: the target folder as a string, the redaction options
    nested, the recipient and matching fields left out (they are not restored)."""
    d = {k: v for k, v in opt.__dict__.items() if k not in _OPTIONEN_AUSSEN}
    d["ziel_ordner"] = str(opt.ziel_ordner) if opt.ziel_ordner else None
    d["schwaerzen"] = dataclasses.asdict(opt.schwaerzen)
    return d


def _optionen_aus(d: dict[str, Any]) -> Ablaufoptionen:
    """The options read back: unknown keys and the outer fields are ignored, the
    colours become tuples again."""
    felder = {k: v for k, v in d.items() if k in Ablaufoptionen.__dataclass_fields__
              and k not in _OPTIONEN_AUSSEN}
    felder["ziel_ordner"] = Path(d["ziel_ordner"]) if d.get("ziel_ordner") else None
    sw = dict(d.get("schwaerzen", {}))
    for farbe in ("fuellfarbe", "textfarbe"):
        if farbe in sw:
            sw[farbe] = tuple(sw[farbe])
    felder["schwaerzen"] = Schwaerzoptionen(
        **{k: v for k, v in sw.items() if k in Schwaerzoptionen.__dataclass_fields__})
    return Ablaufoptionen(**felder)


def _seite_als_dict(s: Seite) -> dict[str, Any]:
    """A page as JSON data: text layer, OCR reading, correction, resolution and hits."""
    return {
        "nummer": s.nummer, "text": s.text.als_dict(),
        "ocr": s.ocr.als_dict() if s.ocr is not None else None,
        "korrektur": s.korrektur, "ocr_dpi": s.ocr_dpi, "ocr_max_pixel": s.ocr_max_pixel,
        "fundstellen": [f.als_dict() for f in s.fundstellen],
        "korrektur_treffer": [f.als_dict() for f in s.korrektur_treffer],
    }


def erfasse(v: Vorbereitung | Textvorbereitung) -> Arbeitsstand:
    """Capture a preparation as it stands (nothing is written)."""
    if v.opt.empfaenger:
        raise ArbeitsstandFehler(
            "Im Empfängermodus (Einmalschlüssel) lässt sich kein Arbeitsstand speichern:"
            " die Codes wären beim Wiederaufnehmen nicht mehr dieselben.")
    if v.abgeschlossen:
        raise ArbeitsstandFehler("Diese Vorbereitung ist schon abgeschlossen.")
    if isinstance(v, Textvorbereitung):
        dokument = v.text.encode("utf-8")
        endung = v.ausgabe_endung
    else:
        dokument = v.doc.tobytes()
        endung = ".pdf"
    daten: dict[str, Any] = {
        MARKE: VERSION,
        "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
        "werkzeug": WERKZEUG,
        "dokument": {"datei": "", "endung": endung, "sha256": hashlib.sha256(dokument).hexdigest(),
                     "quelle": v.quelle.name, "ist_text": v.ist_text,
                     "ausgabe_endung": getattr(v, "ausgabe_endung", ".txt")},
        "patient": v.patient.als_dict(),
        "kennung": v.kennung,
        "optionen": _optionen_als_dict(v.opt),
        "aufbereitung": {str(n): a.vollstaendig() for n, a in v.aufbereitet.items()},
        "seiten": [_seite_als_dict(s) for s in v.seiten],
        "ohne_text": list(v.ohne_text),
        "verworfen": [f.als_dict() for f in v.verworfen],
        "wiederhergestellt": [list(k) for k in sorted(v.wiederhergestellt)],
        "warnungen": list(v.warnungen),
        "hinweise": list(v.hinweise),
        "version": v.version,
    }
    return Arbeitsstand(daten, dokument, ordner=v.quelle.parent)


def speichere(v: Vorbereitung | Textvorbereitung, ziel: str | Path) -> tuple[Path, Path]:
    """Write the two files; ``ziel`` is the JSON file (``….arbeitsstand.json``), a file name
    with another extension, or a folder (existing, or without an extension: created and the
    files named after the source).  Returns (document, json)."""
    a = erfasse(v)
    ziel = Path(ziel)
    if ziel.is_dir() or (not ziel.suffix and not ziel.exists()):
        ziel = ziel / (v.quelle.stem + ENDUNG)
    elif not ziel.name.endswith(ENDUNG):
        name = ziel.name
        if KENNZEICHEN + "." in name:
            name = name[:name.index(KENNZEICHEN)]
        elif "." in name:
            name = name[:name.rindex(".")]
        ziel = ziel.with_name(name + ENDUNG)
    stamm = ziel.name[:-len(ENDUNG)]
    dokument = ziel.with_name(stamm + KENNZEICHEN + a.daten["dokument"]["endung"])
    a.daten["dokument"]["datei"] = dokument.name
    ziel.parent.mkdir(parents=True, exist_ok=True)
    dokument.write_bytes(a.dokument)
    ziel.write_text(json.dumps(a.daten, ensure_ascii=False, indent=1), encoding="utf-8")
    for p in (dokument, ziel):
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return dokument, ziel


# -- reading ---------------------------------------------------------------------------------------
def lies(pfad: str | Path) -> Arbeitsstand:
    """Read a state from its JSON file and the document next to it; the document's
    checksum has to match."""
    pfad = Path(pfad)
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ArbeitsstandFehler(f"{pfad.name}: kein lesbarer Arbeitsstand ({e}).") from e
    if not isinstance(daten, dict) or MARKE not in daten:
        raise ArbeitsstandFehler(f"{pfad.name}: kein Arbeitsstand von PseudonyMed.")
    if int(daten[MARKE]) > VERSION:
        raise ArbeitsstandFehler(f"{pfad.name}: stammt von einem neueren PseudonyMed.")
    dokument = pfad.parent / str(daten["dokument"]["datei"])
    try:
        inhalt = dokument.read_bytes()
    except OSError as e:
        raise ArbeitsstandFehler(
            f"Das Dokument {dokument.name} fehlt neben {pfad.name}.") from e
    if hashlib.sha256(inhalt).hexdigest() != daten["dokument"]["sha256"]:
        raise ArbeitsstandFehler(
            f"{dokument.name} wurde seit dem Speichern verändert; der Arbeitsstand passt nicht"
            " mehr dazu.")
    return Arbeitsstand(daten, inhalt, pfad)


def _seite_aus(d: dict[str, Any], plan: aufmodul.Aufbereitung | None,
               page: pymupdf.Page | None) -> Seite:
    """A page as it was read, without its hits: text layer, OCR reading, correction and
    resolution.  The letter-aware finish follows the clean-up plan; the layout roles are
    rebuilt from the PDF page when there is an OCR reading to place them on."""
    s = Seite(int(d["nummer"]), Seitentext.aus_dict(d["text"]))
    s.ocr = Seitentext.aus_dict(d["ocr"]) if d.get("ocr") else None
    s.korrektur = d.get("korrektur")
    s.ocr_dpi = int(d.get("ocr_dpi", s.ocr_dpi))
    s.ocr_max_pixel = int(d.get("ocr_max_pixel", s.ocr_max_pixel))
    if plan is not None and plan.textmodus:
        s.ocr_filter = functools.partial(aufmodul.textverfeinerung,
                                         faktor=max(1, plan.hochskalieren))
    if s.ocr is not None and page is not None:
        s.gliederung = gliedmodul.gliedere_seite(page, s.ocr)
    return s


def _plan_aus(daten: dict[str, Any]) -> dict[int, aufmodul.Aufbereitung]:
    """The clean-up plan read back, keyed by page number."""
    return {int(n): aufmodul.Aufbereitung.aus_dict(a)
            for n, a in daten.get("aufbereitung", {}).items()}


def _pdfseiten_aus(seiten: list[dict[str, Any]], plan: dict[int, aufmodul.Aufbereitung],
                   doc: pymupdf.Document) -> list[Seite]:
    """The saved pages of a PDF, each rebuilt against its page in the opened document."""
    return [_seite_aus(d, plan.get(int(d["nummer"])), doc[int(d["nummer"])]) for d in seiten]


def _meldungen_der_lesung(a: Arbeitsstand) -> tuple[list[str], list[str]]:
    """The warnings and hints a rebuilt reading keeps: those about the text layer, the OCR
    and the clean-up, plus where the state came from."""
    warnungen = [w for w in a.daten.get("warnungen", []) if "Textebene" in w]
    hinweise = [h for h in a.daten.get("hinweise", []) if "OCR" in h or "aufbereitet" in h]
    hinweise.append(_ladehinweis(a))
    return warnungen, hinweise


def lesung_aus(a: Arbeitsstand, opt: Ablaufoptionen | None = None) -> Lesung:
    """The reading phase rebuilt from a state, nothing searched yet: for a new
    analysis that keeps the clean-up, the OCR and the corrections."""
    if a.ist_text:
        raise ArbeitsstandFehler("Textdateien haben keine Lesung; vorbereiten() aufrufen.")
    opt = opt or a.optionen
    plan = _plan_aus(a.daten)
    doc = pymupdf.open("pdf", a.dokument)
    original = pymupdf.open("pdf", a.dokument)
    try:
        seiten = _pdfseiten_aus(a.daten["seiten"], plan, doc)
    except Exception:
        doc.close()
        original.close()
        raise
    warnungen, hinweise = _meldungen_der_lesung(a)
    return Lesung(quelle=a.quelle, opt=opt, doc=doc, original=original, backend=None,
                  seiten=seiten, ohne_text=[int(n) for n in a.daten.get("ohne_text", [])],
                  plan=plan, warnungen=warnungen, hinweise=hinweise)


def _ladehinweis(a: Arbeitsstand) -> str:
    """The hint naming the state and when, and by which tool version, it was saved."""
    name = a.pfad.name if a.pfad is not None else "dem Speicher"
    return (f"Arbeitsstand aus {name} übernommen (gespeichert {a.daten.get('erstellt', '?')},"
            f" {a.daten.get('werkzeug', '?')}).")


def _meldungen_der_vorbereitung(a: Arbeitsstand, r: Ruestzeug) -> tuple[list[str], list[str]]:
    """The saved warnings and hints without an earlier load hint, the current one added, and
    a warning when this database issues another identifier than the state was saved under."""
    warnungen = [w for w in a.daten.get("warnungen", []) if "Arbeitsstand" not in w]
    hinweise = [h for h in a.daten.get("hinweise", []) if "Arbeitsstand" not in h]
    hinweise.append(_ladehinweis(a))
    if a.kennung and r.kennung != a.kennung:
        warnungen.append(
            f"Der Arbeitsstand wurde mit der Kennung {a.kennung} angelegt, diese Datenbank"
            f" vergibt {r.kennung} (anderer Schlüssel oder andere Datenbank): gespeicherte"
            " Pseudonyme bleiben und werden der Zuordnung hinzugefügt, neue Treffer"
            " bekommen Codes dieser Datenbank.")
    return warnungen, hinweise


def _gemeinsam_aus(a: Arbeitsstand, r: Ruestzeug, patient: Patient, opt: Ablaufoptionen,
                   speicher: Pseudonymspeicher, eigener_speicher: bool) -> dict[str, Any]:
    """The keyword fields of :class:`Vorbereitungsbasis` both preparations take from a
    state, all but ``seiten``: whose document, with which options, store and gear, the
    messages, and what the review had decided (discards, restores, version, plan)."""
    warnungen, hinweise = _meldungen_der_vorbereitung(a, r)
    return {
        "quelle": a.quelle, "patient": patient, "opt": opt, "speicher": speicher,
        "eigener_speicher": eigener_speicher, "kennung": r.kennung, "vergabe": r.vergabe,
        "regeln": r.regeln, "ziele": r.ziele, "warnungen": warnungen, "hinweise": hinweise,
        "verworfen": [Fundstelle.aus_dict(f) for f in a.daten.get("verworfen", [])],
        "version": int(a.daten.get("version", 0)),
        "wiederhergestellt": {(str(k[0]), str(k[1]))
                              for k in a.daten.get("wiederhergestellt", [])},
        "aufbereitet": _plan_aus(a.daten),
    }


def _textvorbereitung_aus(a: Arbeitsstand, gemeinsam: dict[str, Any]) -> Textvorbereitung:
    """The text variant: the saved clear text, its output extension, and pages that have no
    PDF page behind them."""
    return Textvorbereitung(
        **gemeinsam, text=a.dokument.decode("utf-8"),
        seiten=[_seite_aus(d, None, None) for d in a.daten["seiten"]],
        ausgabe_endung=str(a.daten["dokument"].get("ausgabe_endung", ".txt")))


def _pdfvorbereitung_aus(a: Arbeitsstand, gemeinsam: dict[str, Any]) -> Vorbereitung:
    """The PDF variant: pages and documents come from a rebuilt reading, which is thereby
    used up."""
    lesung = lesung_aus(a, gemeinsam["opt"])
    lesung.verbraucht = True
    return Vorbereitung(**gemeinsam, backend=None, doc=lesung.doc, original=lesung.original,
                        seiten=lesung.seiten, ohne_text=lesung.ohne_text)


def _seite_wiederherstellen(s: Seite, d: dict[str, Any], ist_text: bool,
                            fachbegriffe: fachmodul.Fachbegriffe | None) -> None:
    """Put the saved hits and correction hits back on a restored page and mark the medical
    terms afresh: on the text layer where the page has one (always for a text file) and on
    the OCR reading."""
    s.fundstellen = [Fundstelle.aus_dict(f) for f in d.get("fundstellen", [])]
    s.korrektur_treffer = [Fundstelle.aus_dict(f) for f in d.get("korrektur_treffer", [])]
    s.fachrects, s.fachspannen, s.fachanzahl = [], [], 0
    if fachbegriffe is None:
        return
    if ist_text or s.text.hat_textebene:
        merke_fachbegriffe(s, s.text, fachbegriffe)
    if s.ocr is not None:
        merke_fachbegriffe(s, s.ocr, fachbegriffe)


def vorbereitung_aus(a: Arbeitsstand, speicher: Pseudonymspeicher | None = None,
                     opt: Ablaufoptionen | None = None) -> Vorbereitung | Textvorbereitung:
    """The preparation as it was saved: pages, hits, discards and marks, ready for the
    review, without reading or searching anything again.  ``opt`` replaces the saved
    options (the detection options have no effect here, the output options do)."""
    opt = opt or a.optionen
    patient = a.patient
    eigener_speicher = speicher is None
    speicher = speicher or Pseudonymspeicher()
    try:
        r = ruestzeug(patient, opt, speicher)
        gemeinsam = _gemeinsam_aus(a, r, patient, opt, speicher, eigener_speicher)
        v: Vorbereitung | Textvorbereitung = (
            _textvorbereitung_aus(a, gemeinsam) if a.ist_text
            else _pdfvorbereitung_aus(a, gemeinsam))
        for s, d in zip(v.seiten, a.daten["seiten"], strict=True):
            _seite_wiederherstellen(s, d, v.ist_text, r.fachbegriffe)
    except Exception:
        if eigener_speicher:
            speicher.close()
        raise
    return v


# -- a new analysis that keeps the decisions -------------------------------------------------------
def _spannen_ueberlappen(f: Fundstelle, g: Fundstelle) -> bool:
    """True when the two hits share at least one character of the page text."""
    return f.start < g.ende and f.ende > g.start


def _schon_markiert(f: Fundstelle, fundstellen: list[Fundstelle]) -> bool:
    """True when the same manual hit (same boxes and span) is already among the hits."""
    return any(g.quelle == "manuell" and g.rects == f.rects and g.start == f.start
               and g.ende == f.ende for g in fundstellen)


def _markiere_erneut(seite: Seite, f: Fundstelle) -> None:
    """Add a saved manual hit to the page.  A mark with a span replaces what the analysis
    found underneath it; the other manual hits stay."""
    if f.ende > f.start:
        seite.fundstellen = [g for g in seite.fundstellen
                             if g.quelle == "manuell" or not _spannen_ueberlappen(f, g)]
    seite.fundstellen.append(f)


def _manuelle_treffer_uebernehmen(d: dict[str, Any], neu: Vorbereitung | Textvorbereitung) -> int:
    """Re-add the manual hits (boxes, masks, take-ups) of one saved page to the same page
    of the new preparation; returns how many were not there yet.  A page the new
    preparation does not have is skipped."""
    nummer = int(d["nummer"])
    if nummer >= len(neu.seiten):
        return 0
    seite = neu.seiten[nummer]
    n = 0
    for f in (Fundstelle.aus_dict(x) for x in d.get("fundstellen", [])):
        if f.quelle != "manuell" or _schon_markiert(f, seite.fundstellen):
            continue
        _markiere_erneut(seite, f)
        n += 1
    return n


def _erneut_verwerfen(alt: Arbeitsstand, neu: Vorbereitung | Textvorbereitung) -> int:
    """Discard again every hit of the new preparation whose rule and text the saved state
    had discarded; returns how many."""
    verworfen = {toleranzschluessel(str(f["regel"]), str(f.get("text", "")))
                 for f in alt.daten.get("verworfen", []) if f.get("text")}
    treffer = [f for f in neu.fundstellen if toleranzschluessel(f.regel, f.text) in verworfen]
    for f in treffer:
        neu.verwerfe(f)
    return len(treffer)


def uebernehme_entscheidungen(alt: Arbeitsstand, neu: Vorbereitung | Textvorbereitung) -> int:
    """After a new analysis: put the manual boxes, masks and marks of the saved state
    back and discard again what was discarded (same rule and text).  Returns how many
    hits were taken over or discarded."""
    n = sum(_manuelle_treffer_uebernehmen(d, neu) for d in alt.daten["seiten"])
    n += _erneut_verwerfen(alt, neu)
    neu.hinweise.append(f"{n} Entscheidungen aus dem Arbeitsstand übernommen (Kästen, Masken,"
                        " Markierungen, verworfene Treffer).")
    return n
