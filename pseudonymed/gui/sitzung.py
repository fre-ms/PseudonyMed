"""State of one desktop session, kept free of Qt so that it can be tested on its own."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pseudonymed import arbeitsstand as standmodul
from pseudonymed.ablauf import (
    Ablaufoptionen,
    Ergebnis,
    Lesung,
    Textvorbereitung,
    Vorbereitung,
    Vorlage,
    abschliessen,
    analysiere,
    entscheidungen_merken,
    lade,
    lese,
    vorbereiten,
)
from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument.textdatei import TEXTENDUNGEN, ist_textdatei
from pseudonymed.kern.modell import Patient
from pseudonymed.pruefung import restrisiko as risikomodul
from pseudonymed.pseudonyme.pgp import Zertifikat
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher, UnsichererAbgleich, einmalspeicher


def _ocr_vermerk(seiten: list[int]) -> str:
    """The ", OCR auf Seite(n) …" suffix of a state line; empty when no page needed OCR."""
    return f", OCR auf Seite(n) {', '.join(map(str, seiten))}" if seiten else ""


def _zustand_analysiert(v: Vorbereitung | Textvorbereitung) -> str:
    """State line of an analysed document: pages, hits and the OCR pages; a text file
    has no pages to count."""
    if v.ist_text:
        return f"Text, {len(v.fundstellen)} Treffer"
    return f"{len(v.seiten)} Seite(n), {len(v.fundstellen)} Treffer{_ocr_vermerk(v.ocr_seiten)}"


def _zustand_geladen(vorlage: Vorlage) -> str:
    """State line of an opened document: its pages and how many lack a text layer."""
    n = len(vorlage.kandidaten)
    if n:
        return f"{vorlage.seitenzahl} Seite(n), {n} ohne Textebene (aufbereiten)"
    return f"{vorlage.seitenzahl} Seite(n), geladen"


@dataclass
class Dokument:
    """One document of the session with everything it has reached so far: the opened
    template and its clean-up plan, the reading, the preparation, the pre-check, the
    result; ``fehler`` holds the message of the step that failed."""

    pfad: Path
    vorlage: Vorlage | None = None  # phase zero: open, pages known, clean-up suggested
    aufbereitung: dict[int, aufmodul.Aufbereitung] = field(default_factory=dict)
    lesung: Lesung | None = None  # reading phase: text layer and OCR, correctable
    vorbereitung: Vorbereitung | Textvorbereitung | None = None
    vorpruefung: Ergebnis | None = None  # the re-check run into a scratch folder
    vorpruefung_version: int = -1  # review state the pre-check belongs to
    ergebnis: Ergebnis | None = None
    fehler: str = ""
    arbeitsstand: Path | None = None  # the JSON file the document was restored from

    @property
    def textquelle(self) -> Path:
        """The text file a new analysis of a text document reads: the saved one of a
        working state, else the file itself."""
        if self.arbeitsstand is not None:
            return standmodul.lies(self.arbeitsstand).dokumentpfad or self.pfad
        return self.pfad

    @property
    def aufzubereiten(self) -> bool:
        """True when the opened document has pages without a text layer to clean up."""
        return self.vorlage is not None and bool(self.vorlage.kandidaten)

    @property
    def lesbar(self) -> bool:
        """PDFs and images go through the reading step; text files do not."""
        return not ist_textdatei(self.pfad)

    @property
    def pruefbereit(self) -> bool:
        """True while an analysed document waits for its export: the preparation is open."""
        return self.vorbereitung is not None and not self.vorbereitung.abgeschlossen

    @property
    def vorpruefung_aktuell(self) -> bool:
        """True when the pre-check was run on the review state as it is now."""
        v = self.vorbereitung
        if v is None or self.vorpruefung is None:
            return False
        return self.pruefbereit and self.vorpruefung_version == v.version

    @property
    def zustand(self) -> str:
        """One line for the document list: the error, else the furthest phase reached."""
        if self.fehler:
            return f"Fehler: {self.fehler}"
        if self.ergebnis is not None:
            return "freigegeben" if self.ergebnis.freigegeben else "nicht freigegeben"
        if self.vorbereitung is not None:
            return _zustand_analysiert(self.vorbereitung)
        if self.lesung is not None:
            ocr = _ocr_vermerk(self.lesung.ocr_seiten)
            return f"{self.lesung.seitenzahl} Seite(n) gelesen{ocr}"
        if self.vorlage is not None:
            return _zustand_geladen(self.vorlage)
        return "noch nicht analysiert"


def _offene_vorbereitung(d: Dokument) -> Vorbereitung | Textvorbereitung:
    """The open preparation of an analysed document; ValueError for any other state."""
    if d.vorbereitung is None or not d.pruefbereit:
        raise ValueError("Dokument ist nicht analysiert.")
    return d.vorbereitung


def _stand_erfassen(d: Dokument) -> standmodul.Arbeitsstand | None:
    """Capture the review of a document analysed already and close its preparation;
    None for a fresh document, and in recipient mode, where no state can be captured
    and the file is read again."""
    v = d.vorbereitung
    if v is None or v.abgeschlossen:
        return None
    alt: standmodul.Arbeitsstand | None
    try:
        alt = standmodul.erfasse(v)
    except standmodul.ArbeitsstandFehler:
        alt = None  # recipient mode: read again from the file
    v.schliessen()
    return alt


def _frische_analyse(d: Dokument, patient: Patient, opt: Ablaufoptionen,
                     speicher: Pseudonymspeicher) -> Vorbereitung | Textvorbereitung:
    """Phase one of a document not analysed before: over its reading when the user went
    through the text-recognition step, else from the file with the clean-up plan."""
    if d.lesung is not None and not d.lesung.verbraucht:
        return analysiere(d.lesung, patient, opt, speicher=speicher)
    aufbereitung = dict(d.aufbereitung) if d.vorlage is not None else None
    return vorbereiten(d.pfad, patient, opt, speicher=speicher, aufbereitung=aufbereitung)


def _analyse_aus_stand(d: Dokument, alt: standmodul.Arbeitsstand, patient: Patient,
                       opt: Ablaufoptionen, speicher: Pseudonymspeicher,
                       ) -> Vorbereitung | Textvorbereitung:
    """Phase one over a saved working state: a text file is read again from its saved
    text, a PDF or image from the saved reading, so clean-up, OCR and corrections stay."""
    if alt.ist_text:
        return vorbereiten(d.textquelle, patient, opt, speicher=speicher)
    lesung = standmodul.lesung_aus(alt, opt)
    try:
        return analysiere(lesung, patient, opt, speicher=speicher)
    except Exception:
        lesung.schliessen()
        raise


_AUSGABEFELDER = ("ziel_pdf", "ziel_txt", "ziel_md", "ziel_bericht", "ziel_vokabular",
                  "ziel_kapsel")


def _kopiere_ausgaben(quelle: Ergebnis, ziel_ordner: Path) -> Ergebnis:
    """Copy the files of a passed pre-check into the target folder and describe the
    copies as a result of their own (the residual risk is attached by the caller)."""
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    kopie = Ergebnis(quelle=quelle.quelle, bericht=quelle.bericht,
                     fundstellen=quelle.fundstellen, kennung=quelle.kennung,
                     text=quelle.text, ist_text=quelle.ist_text)
    for feld in _AUSGABEFELDER:
        pfad = getattr(quelle, feld)
        if pfad is not None:
            neu = ziel_ordner / pfad.name
            shutil.copy2(pfad, neu)
            setattr(kopie, feld, neu)
    for bild in quelle.vorschau:
        neu_ordner = ziel_ordner / bild.parent.name
        neu_ordner.mkdir(exist_ok=True)
        shutil.copy2(bild, neu_ordner / bild.name)
        kopie.vorschau.append(neu_ordner / bild.name)
    return kopie


_AMPEL_GRUEN = ("gruen", "Erneute Prüfung bestanden: nichts mehr gefunden, bereit zum Export.")


def _ampel_pruefung_aussteht(offen: list[Dokument]) -> tuple[str, str] | None:
    """Grey while a document's pre-check is missing or older than its review."""
    if all(d.vorpruefung_aktuell for d in offen):
        return None
    return "grau", "Erneute Prüfung steht aus."


def _ampel_resttreffer(offen: list[Dokument]) -> tuple[str, str] | None:
    """Red while a pre-check still found identifiers: nothing will be exported."""
    rot = sum(1 for d in offen if d.vorpruefung is not None and not d.vorpruefung.freigegeben)
    if not rot:
        return None
    return "rot", f"Identifikatoren noch gefunden in {rot} Dokument(en): nichts wird exportiert."


def _ampel_warnungen(offen: list[Dokument]) -> tuple[str, str] | None:
    """Yellow when a passed pre-check carries warnings the user should read first."""
    gelb = sum(1 for d in offen if d.vorpruefung is not None and d.vorpruefung.bericht.warnungen)
    if not gelb:
        return None
    return "gelb", f"Erneute Prüfung bestanden; {gelb} Dokument(e) mit Warnungen, bitte lesen."


@dataclass
class Sitzung:
    """The session behind the desktop: the patient, the documents in their phases, the
    options every step reads and the store the pseudonyms come from.  Nothing in here
    touches Qt; the pages call it from their worker threads."""

    speicherpfad: Path | None = None
    schema: str = "hash"
    patient: Patient | None = None
    optionen: Ablaufoptionen = field(default_factory=Ablaufoptionen)
    dokumente: list[Dokument] = field(default_factory=list)
    abgleich: str | None = None  # decision for an uncertain match, set by the dialog
    empfaenger: Zertifikat | None = None  # pseudonyms for someone else: one-time key, capsule
    _scratch: Path | None = None
    _einmal: Pseudonymspeicher | None = None

    def speicher(self) -> Pseudonymspeicher:
        """The local store, or in recipient mode one in-memory store for the whole session
        (the same one-time key for every document, nothing written locally)."""
        if self.empfaenger is not None:
            if self._einmal is None:
                self._einmal = einmalspeicher(self.schema)
            # the wrapper answers every call of the store and only ignores close()
            return cast(Pseudonymspeicher, _Ungeschlossen(self._einmal))
        return Pseudonymspeicher(self.speicherpfad, schema=self.schema)

    def setze_empfaenger(self, zertifikat: Zertifikat | None) -> None:
        """Switch recipient mode on (a fresh one-time key) or off."""
        self.empfaenger = zertifikat
        self._einmal = None
        self.optionen.empfaenger = zertifikat.armor if zertifikat else ""
        self.optionen.empfaenger_fingerabdruck = zertifikat.fingerabdruck if zertifikat else ""

    def scratch(self) -> Path:
        """Folder for the pre-check output, removed when the session closes."""
        if self._scratch is None or not self._scratch.exists():
            self._scratch = Path(tempfile.mkdtemp(prefix="pseudonymed-"))
        return self._scratch

    def fuege_hinzu(self, pfad: str | Path) -> Dokument | None:
        """Add a file: a document, or a saved working state by its JSON or the document
        beside it.  None when the type is unknown or the file is in the list already."""
        pfad = Path(pfad)
        stand = standmodul.ist_arbeitsstand(pfad)
        if stand is not None:  # a saved working state, by its JSON or the document beside it
            if any(d.pfad == stand for d in self.dokumente):
                return None
            d = Dokument(stand, arbeitsstand=stand)
            self.dokumente.append(d)
            return d
        if pfad.suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp",
                                       ".heic", ".heif", ".webp", *TEXTENDUNGEN}:
            return None
        if any(d.pfad == pfad for d in self.dokumente):
            return None
        d = Dokument(pfad)
        self.dokumente.append(d)
        return d

    def entferne(self, d: Dokument) -> None:
        """Drop a document from the session and release what it holds open."""
        if d.vorbereitung is not None and not d.vorbereitung.abgeschlossen:
            d.vorbereitung.schliessen()
        if d.vorlage is not None:
            d.vorlage.schliessen()
        if d.lesung is not None:
            d.lesung.schliessen()
        self.dokumente.remove(d)

    def lese(self, d: Dokument) -> None:
        """Reading phase for one document with the clean-up the user chose; text files
        have nothing to read."""
        if d.lesung is not None:
            d.lesung.schliessen()
        d.lesung, d.vorbereitung, d.ergebnis, d.fehler = None, None, None, ""
        if not d.lesbar:
            return
        try:
            d.lesung = lese(d.pfad, self.optionen, dict(d.aufbereitung) if d.vorlage else None)
        except Exception as e:
            d.fehler = str(e)
            raise

    def lade(self, d: Dokument) -> None:
        """Phase zero for one document: open it and suggest the clean-up of its
        image-only pages.  Text files have nothing to clean up."""
        if d.vorlage is not None:
            d.vorlage.schliessen()
        d.vorlage, d.aufbereitung, d.vorbereitung, d.ergebnis, d.fehler = None, {}, None, None, ""
        d.lesung, d.vorpruefung = None, None
        if d.arbeitsstand is not None:
            self._arbeitsstand_laden(d)
            return
        if ist_textdatei(d.pfad):
            return
        try:
            d.vorlage = lade(d.pfad, self.optionen)
        except Exception as e:
            d.fehler = str(e)
            raise
        d.aufbereitung = {n: aufmodul.Aufbereitung(**{**a.__dict__, "masken": list(a.masken)})
                          for n, a in d.vorlage.vorschlaege.items()}

    def _arbeitsstand_laden(self, d: Dokument) -> None:
        """Restore a saved working state: the review is filled in, the patient taken over."""
        assert d.arbeitsstand is not None
        if self.empfaenger is not None:
            d.fehler = "Im Empfängermodus lässt sich kein Arbeitsstand laden."
            raise ValueError(d.fehler)
        try:
            a = standmodul.lies(d.arbeitsstand)
            opt = a.optionen
            opt.abgleich = self.abgleich
            opt.verworfene_merken = self.optionen.verworfene_merken
            speicher = self.speicher()
            try:
                d.vorbereitung = standmodul.vorbereitung_aus(a, speicher=speicher, opt=opt)
            except Exception:
                speicher.close()
                raise
        except Exception as e:
            d.fehler = str(e)
            raise
        d.vorbereitung.eigener_speicher = True
        if self.patient is None or self.patient.schluessel() != a.patient.schluessel():
            self.patient = a.patient

    def analysiere(self, d: Dokument) -> None:
        """Phase one for one document; raises UnsichererAbgleich for the dialog.  A document
        analysed already is analysed again on its saved reading (clean-up, OCR and
        corrections stay), and the boxes, masks, marks and discards are taken over."""
        if self.patient is None:
            raise ValueError("Kein Patient gewählt.")
        patient = self.patient
        alt = _stand_erfassen(d)
        d.vorbereitung, d.ergebnis, d.fehler = None, None, ""
        opt = Ablaufoptionen(**{**self.optionen.__dict__, "abgleich": self.abgleich})
        speicher = self.speicher()
        try:
            if alt is None:
                neu = _frische_analyse(d, patient, opt, speicher)
            else:
                neu = _analyse_aus_stand(d, alt, patient, opt, speicher)
            d.vorbereitung = neu
            if alt is not None:
                standmodul.uebernehme_entscheidungen(alt, neu)
        except UnsichererAbgleich:
            speicher.close()
            raise
        except Exception as e:
            speicher.close()
            d.fehler = str(e)
            raise
        neu.eigener_speicher = True  # the preparation owns and closes it
        d.lesung = None  # its documents now belong to the preparation
        if d.vorlage is not None:
            d.vorlage.schliessen()
            d.vorlage = None

    def _optionen_fuer(self, v: Vorbereitung | Textvorbereitung,
                       ziel_ordner: Path | None) -> Ablaufoptionen:
        """The options for finishing ``v``: the session's, with the match decision, the
        date form the analysis used and the folder the files go to."""
        return Ablaufoptionen(**{**self.optionen.__dict__, "abgleich": self.abgleich,
                                 "datumsform": v.opt.datumsform,
                                 "ziel_ordner": ziel_ordner})

    def pruefe(self, d: Dokument) -> Ergebnis:
        """The whole finishing phase into a scratch folder: redaction, re-check, report.
        Nothing is exported; the result tells whether the export may happen."""
        v = _offene_vorbereitung(d)
        ordner = self.scratch() / str(id(d))
        if ordner.exists():
            shutil.rmtree(ordner)
        v.opt = self._optionen_fuer(v, ordner)
        d.vorpruefung = abschliessen(v, endgueltig=False)
        d.vorpruefung_version = v.version
        return d.vorpruefung

    def exportiere(self, d: Dokument) -> Ergebnis:
        """Export: copy the checked files when the pre-check is current, otherwise run
        the finishing phase again into the target.  A failed re-check exports nothing."""
        v = _offene_vorbereitung(d)
        if d.vorpruefung_aktuell and d.vorpruefung is not None:
            return self._exportiere_vorpruefung(d, v, d.vorpruefung)
        v.opt = self._optionen_fuer(v, self.optionen.ziel_ordner)
        d.ergebnis = abschliessen(v, endgueltig=True)
        self._restrisiko_merken(d)
        return d.ergebnis

    def _exportiere_vorpruefung(self, d: Dokument, v: Vorbereitung | Textvorbereitung,
                                quelle: Ergebnis) -> Ergebnis:
        """Export from a current pre-check.  A failed one becomes the result and only
        closes the preparation (decisions stored, nothing written); a passed one has its
        files copied to the target and the mapping stored as the final run would."""
        if not quelle.freigegeben:
            d.ergebnis = quelle
            v.opt = self._optionen_fuer(v, self.optionen.ziel_ordner)
            abschliessen(v, endgueltig=True)  # stores decisions, closes
            return d.ergebnis
        kopie = _kopiere_ausgaben(quelle, self.optionen.ziel_ordner or d.pfad.parent)
        v.opt = self._optionen_fuer(v, self.optionen.ziel_ordner)
        self._abschluss_ohne_ausgabe(v)
        kopie.restrisiko = quelle.restrisiko
        d.ergebnis = kopie
        self._restrisiko_merken(d)
        return d.ergebnis

    def _abschluss_ohne_ausgabe(self, v: Vorbereitung | Textvorbereitung) -> None:
        """Close a preparation whose files were copied: store the mapping and the
        discard decisions as the final run would, without redacting again."""
        v.speicher.merke(v.patient, v.vergabe)
        entscheidungen_merken(v)
        v.abgeschlossen = True
        v.schliessen()

    def ampel(self) -> tuple[str, str]:
        """Traffic light for the export step: "gruen" (re-check clean, ready), "gelb"
        (released with warnings), "rot" (identifiers still found, nothing exported),
        "grau" (not checked yet)."""
        offen = [d for d in self.dokumente if d.pruefbereit]
        if not offen:
            return self._ampel_nach_export()
        return (_ampel_pruefung_aussteht(offen) or _ampel_resttreffer(offen)
                or _ampel_warnungen(offen) or _AMPEL_GRUEN)

    def _ampel_nach_export(self) -> tuple[str, str]:
        """Without open documents: green once every finished one was released, else grey."""
        fertig = [d.ergebnis for d in self.dokumente if d.ergebnis is not None]
        if fertig and all(e.freigegeben for e in fertig):
            return "gruen", "Exportiert."
        return "grau", "Keine analysierten Dokumente."

    def kontrolle(self) -> tuple[risikomodul.Restrisiko | None, int]:
        """All checked documents of the session together with what earlier released
        documents of the same patient revealed; (None, 0) before any check."""
        risiken = [d.vorpruefung.restrisiko for d in self.dokumente
                   if d.vorpruefung is not None and d.vorpruefung.restrisiko is not None]
        if not risiken:
            return None, 0
        fruehere: list = []
        anzahl = 0
        if self.patient is not None and self.empfaenger is None:
            try:
                with self.speicher() as sp:
                    gespeichert, anzahl = sp.restrisiko_fruehere(self.patient)
                fruehere = [risikomodul.Merkmal(**m) for m in gespeichert]
            except Exception:  # noqa: BLE001
                fruehere, anzahl = [], 0
        return risikomodul.vereinige(risiken, fruehere, anzahl), anzahl

    def _restrisiko_merken(self, d: Dokument) -> None:
        """After a release: remember categories and weights for the patient (never clear
        text), so that later documents are judged together with this one."""
        e = d.ergebnis
        if e is None or e.restrisiko is None or not e.freigegeben or self.patient is None \
                or self.empfaenger is not None:
            return
        r = e.restrisiko
        try:
            with self.speicher() as sp:
                sp.merke_restrisiko(self.patient, d.pfad.name,
                                    [{"schluessel": m.schluessel, "kategorie": m.kategorie,
                                      "beschreibung": m.beschreibung, "bits": m.bits}
                                     for m in r.merkmale], r.bits, r.lam)
        except Exception:  # noqa: BLE001
            pass

    def schliessen(self) -> None:
        """Release every document and remove the pre-check folder."""
        for d in self.dokumente:
            if d.vorbereitung is not None and not d.vorbereitung.abgeschlossen:
                d.vorbereitung.schliessen()
            if d.vorlage is not None:
                d.vorlage.schliessen()
            if d.lesung is not None:
                d.lesung.schliessen()
        if self._scratch is not None and self._scratch.exists():
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


class _Ungeschlossen:
    """The session's one-time store handed out to callers that close what they get:
    closing is a no-op, the store lives as long as the recipient mode does."""

    def __init__(self, speicher: Pseudonymspeicher) -> None:
        """Wrap the session's one-time store."""
        self._speicher = speicher

    def __getattr__(self, name: str) -> Any:
        """Everything but closing is the store's."""
        return getattr(self._speicher, name)

    def close(self) -> None:
        """Closing is left to the session: the store outlives every single document."""
        return None

    def __enter__(self) -> _Ungeschlossen:
        """Usable as a context manager like the store itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Leaving the context does not close the store either."""
        return None
