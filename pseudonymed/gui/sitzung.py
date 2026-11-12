"""State of one desktop session, kept free of Qt so that it can be tested on its own."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class Dokument:
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
        return self.vorlage is not None and bool(self.vorlage.kandidaten)

    @property
    def lesbar(self) -> bool:
        """PDFs and images go through the reading step; text files do not."""
        return not ist_textdatei(self.pfad)

    @property
    def pruefbereit(self) -> bool:
        return self.vorbereitung is not None and not self.vorbereitung.abgeschlossen

    @property
    def vorpruefung_aktuell(self) -> bool:
        return (self.pruefbereit and self.vorpruefung is not None
                and self.vorpruefung_version == self.vorbereitung.version)

    @property
    def zustand(self) -> str:
        if self.fehler:
            return f"Fehler: {self.fehler}"
        if self.ergebnis is not None:
            return "freigegeben" if self.ergebnis.freigegeben else "nicht freigegeben"
        if self.vorbereitung is not None:
            v = self.vorbereitung
            if v.ist_text:
                return f"Text, {len(v.fundstellen)} Treffer"
            ocr = f", OCR auf Seite(n) {', '.join(map(str, v.ocr_seiten))}" if v.ocr_seiten else ""
            return f"{len(v.seiten)} Seite(n), {len(v.fundstellen)} Treffer{ocr}"
        if self.lesung is not None:
            ocr = (f", OCR auf Seite(n) {', '.join(map(str, self.lesung.ocr_seiten))}"
                   if self.lesung.ocr_seiten else "")
            return f"{self.lesung.seitenzahl} Seite(n) gelesen{ocr}"
        if self.vorlage is not None:
            n = len(self.vorlage.kandidaten)
            return (f"{self.vorlage.seitenzahl} Seite(n), {n} ohne Textebene (aufbereiten)"
                    if n else f"{self.vorlage.seitenzahl} Seite(n), geladen")
        return "noch nicht analysiert"


@dataclass
class Sitzung:
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
            return _Ungeschlossen(self._einmal)
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
        alt: standmodul.Arbeitsstand | None = None
        if d.vorbereitung is not None and not d.vorbereitung.abgeschlossen:
            try:
                alt = standmodul.erfasse(d.vorbereitung)
            except standmodul.ArbeitsstandFehler:
                alt = None  # recipient mode: read again from the file
            d.vorbereitung.schliessen()
        d.vorbereitung, d.ergebnis, d.fehler = None, None, ""
        opt = Ablaufoptionen(**{**self.optionen.__dict__, "abgleich": self.abgleich})
        aufbereitung = dict(d.aufbereitung) if d.vorlage is not None else None
        speicher = self.speicher()
        try:
            if alt is not None and not alt.ist_text:
                lesung = standmodul.lesung_aus(alt, opt)
                try:
                    d.vorbereitung = analysiere(lesung, self.patient, opt, speicher=speicher)
                except Exception:
                    lesung.schliessen()
                    raise
            elif alt is not None:
                d.vorbereitung = vorbereiten(d.textquelle, self.patient, opt, speicher=speicher)
            elif d.lesung is not None and not d.lesung.verbraucht:
                d.vorbereitung = analysiere(d.lesung, self.patient, opt, speicher=speicher)
            else:
                d.vorbereitung = vorbereiten(d.pfad, self.patient, opt, speicher=speicher,
                                             aufbereitung=aufbereitung)
            if alt is not None:
                standmodul.uebernehme_entscheidungen(alt, d.vorbereitung)
        except UnsichererAbgleich:
            speicher.close()
            raise
        except Exception as e:
            speicher.close()
            d.fehler = str(e)
            raise
        d.vorbereitung.eigener_speicher = True  # the preparation owns and closes it
        d.lesung = None  # its documents now belong to the preparation
        if d.vorlage is not None:
            d.vorlage.schliessen()
            d.vorlage = None

    def _optionen_fuer(self, d: Dokument, ziel_ordner: Path | None) -> Ablaufoptionen:
        return Ablaufoptionen(**{**self.optionen.__dict__, "abgleich": self.abgleich,
                                 "datumsform": d.vorbereitung.opt.datumsform,
                                 "ziel_ordner": ziel_ordner})

    def pruefe(self, d: Dokument) -> Ergebnis:
        """The whole finishing phase into a scratch folder: redaction, re-check, report.
        Nothing is exported; the result tells whether the export may happen."""
        if not d.pruefbereit:
            raise ValueError("Dokument ist nicht analysiert.")
        ordner = self.scratch() / str(id(d))
        if ordner.exists():
            shutil.rmtree(ordner)
        d.vorbereitung.opt = self._optionen_fuer(d, ordner)
        d.vorpruefung = abschliessen(d.vorbereitung, endgueltig=False)
        d.vorpruefung_version = d.vorbereitung.version
        return d.vorpruefung

    def exportiere(self, d: Dokument) -> Ergebnis:
        """Export: copy the checked files when the pre-check is current, otherwise run
        the finishing phase again into the target.  A failed re-check exports nothing."""
        if not d.pruefbereit:
            raise ValueError("Dokument ist nicht analysiert.")
        if d.vorpruefung_aktuell and d.vorpruefung is not None and self.optionen.ziel_ordner \
                is not None or (d.vorpruefung_aktuell and d.vorpruefung is not None):
            quelle = d.vorpruefung
            if not quelle.freigegeben:
                d.ergebnis = quelle
                d.vorbereitung.opt = self._optionen_fuer(d, self.optionen.ziel_ordner)
                abschliessen(d.vorbereitung, endgueltig=True)  # stores decisions, closes
                return d.ergebnis
            ziel_ordner = self.optionen.ziel_ordner or d.pfad.parent
            ziel_ordner.mkdir(parents=True, exist_ok=True)
            kopie = Ergebnis(quelle=quelle.quelle, bericht=quelle.bericht,
                             fundstellen=quelle.fundstellen, kennung=quelle.kennung,
                             text=quelle.text, ist_text=quelle.ist_text)
            for feld in ("ziel_pdf", "ziel_txt", "ziel_md", "ziel_bericht", "ziel_vokabular",
                         "ziel_kapsel"):
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
            d.vorbereitung.opt = self._optionen_fuer(d, self.optionen.ziel_ordner)
            self._abschluss_ohne_ausgabe(d)
            kopie.restrisiko = quelle.restrisiko
            d.ergebnis = kopie
            self._restrisiko_merken(d)
            return d.ergebnis
        d.vorbereitung.opt = self._optionen_fuer(d, self.optionen.ziel_ordner)
        d.ergebnis = abschliessen(d.vorbereitung, endgueltig=True)
        self._restrisiko_merken(d)
        return d.ergebnis

    def _abschluss_ohne_ausgabe(self, d: Dokument) -> None:
        """Close a preparation whose files were copied: store the mapping and the
        discard decisions as the final run would, without redacting again."""
        v = d.vorbereitung
        from pseudonymed.ablauf import entscheidungen_merken

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
            fertig = [d for d in self.dokumente if d.ergebnis is not None]
            if fertig and all(d.ergebnis.freigegeben for d in fertig):
                return "gruen", "Exportiert."
            return "grau", "Keine analysierten Dokumente."
        if any(not d.vorpruefung_aktuell for d in offen):
            return "grau", "Erneute Prüfung steht aus."
        rot = [d for d in offen if not d.vorpruefung.freigegeben]
        if rot:
            return "rot", (f"Identifikatoren noch gefunden in {len(rot)} Dokument(en):"
                           " nichts wird exportiert.")
        gelb = [d for d in offen if d.vorpruefung.bericht.warnungen]
        if gelb:
            return "gelb", (f"Erneute Prüfung bestanden; {len(gelb)} Dokument(e) mit"
                            " Warnungen, bitte lesen.")
        return "gruen", "Erneute Prüfung bestanden: nichts mehr gefunden, bereit zum Export."

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
        self._speicher = speicher

    def __getattr__(self, name: str):
        return getattr(self._speicher, name)

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None
