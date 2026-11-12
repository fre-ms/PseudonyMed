"""Phase one for PDFs and images: the document is read and the hits are known, and
the review can change them before :func:`pseudonymed.ablauf.abschliessen`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pymupdf

from pseudonymed.ablauf.erkennen import erkenne_seiten, gemerkte_verwerfen, korrektur_treffer
from pseudonymed.ablauf.lesen import Lesung, dokumente_schliessen
from pseudonymed.ablauf.optionen import Ablaufoptionen
from pseudonymed.ablauf.ruestzeug import Ruestzeug, analysehinweise, ruestzeug
from pseudonymed.ablauf.seiten import (
    Seite,
    lesetext,
    ohne_rand,
    ueberlappen,
    unveraendert,
    wort_bei,
)
from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import ocr as ocrmodul
from pseudonymed.dokument.textebene import Box
from pseudonymed.erkennung.deterministisch import Regel, kasten_zu_fundstellen
from pseudonymed.erkennung.unscharf import Ziel
from pseudonymed.kern.modell import Art, Fundstelle, Patient
from pseudonymed.pruefung.gegenprobe import toleranzschluessel
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


class Entscheidungen:
    """What the review changes, shared by the PDF and the text preparation: hits are
    dropped and restored, the tolerated pairs follow, every change bumps ``version``."""

    seiten: list[Seite]
    verworfen: list[Fundstelle]
    wiederhergestellt: set[tuple[str, str]]
    version: int

    def verwerfe(self, fundstelle: Fundstelle) -> None:
        """Drop a hit the user judged a false positive; the re-check will tolerate
        this rule/text pair.  The hit stays known (``verworfen``) and can be restored."""
        for s in self.seiten:
            if fundstelle in s.fundstellen:
                s.fundstellen.remove(fundstelle)
                self.verworfen.append(fundstelle)
                self.wiederhergestellt.discard(toleranzschluessel(fundstelle.regel,
                                                                  fundstelle.text))
                self.version += 1
                return

    def wiederherstelle(self, fundstelle: Fundstelle) -> None:
        """Take a discarded hit back."""
        if fundstelle in self.verworfen:
            self.verworfen.remove(fundstelle)
            self.seiten[fundstelle.seite].fundstellen.append(fundstelle)
            self.wiederhergestellt.add(toleranzschluessel(fundstelle.regel, fundstelle.text))
            self.version += 1

    @property
    def toleriert(self) -> set[tuple[str, str]]:
        """The rule/text pairs of the discarded hits, which the re-check lets pass."""
        return {toleranzschluessel(f.regel, f.text) for f in self.verworfen if f.text}

    @property
    def fachbegriffe_anzahl(self) -> int:
        """How many medical terms the vocabulary marked over all pages."""
        return sum(s.fachanzahl for s in self.seiten)


@dataclass
class Vorbereitung(Entscheidungen):
    """Phase one: the document is read and the hits are known, nothing is changed yet.

    A user interface can drop hits (``verwerfe``) and add boxes (``kasten``)
    before :func:`abschliessen` applies them.  Call ``schliessen`` when the
    object is discarded without finishing.
    """

    ist_text: ClassVar[bool] = False

    quelle: Path
    patient: Patient
    opt: Ablaufoptionen
    speicher: Pseudonymspeicher
    eigener_speicher: bool
    kennung: str
    vergabe: Pseudonymvergabe
    regeln: list[Regel]
    ziele: list[Ziel]
    backend: ocrmodul.OCRBackend | None
    doc: pymupdf.Document
    original: pymupdf.Document
    seiten: list[Seite]
    ohne_text: list[int]
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    verworfen: list[Fundstelle] = field(default_factory=list)
    aufbereitet: dict[int, aufmodul.Aufbereitung] = field(default_factory=dict)
    abgeschlossen: bool = False
    version: int = 0  # grows with every change the user makes in the review
    wiederhergestellt: set[tuple[str, str]] = field(default_factory=set)

    @property
    def fundstellen(self) -> list[Fundstelle]:
        """Every hit of every page, in page order."""
        return [f for s in self.seiten for f in s.fundstellen]

    @property
    def ocr_seiten(self) -> list[int]:
        """Numbers (1-based) of the pages that were read by OCR."""
        return [s.nummer + 1 for s in self.seiten if s.ocr is not None]

    def fachbegriffe_rects(self, nummer: int) -> list[Box]:
        """Rectangles of the medical terms on page ``nummer`` (0-based), for display; not
        where a hit lies (a medication redacted on request, a patient called Herz)."""
        belegt = [r for f in self.seiten[nummer].fundstellen for r in f.rects]
        return [r for r in self.seiten[nummer].fachrects
                if not any(ueberlappen(r, b) for b in belegt)]

    def fachbegriffe_spannen(self, nummer: int) -> list[tuple[int, int]]:
        """Text offsets of the medical terms on page ``nummer`` (0-based)."""
        return list(self.seiten[nummer].fachspannen)

    def seitenbild(self, nummer: int, dpi: int = 100) -> bytes:
        """PNG of the original page for the preview."""
        return self.original[nummer].get_pixmap(dpi=dpi, alpha=False).tobytes("png")

    def ocr_text(self, nummer: int) -> str:
        """The recognised text of page ``nummer`` as it will be written out: the user's
        correction when there is one, else the OCR reading (structured as Markdown when
        asked for); "" without OCR."""
        return lesetext(self.seiten[nummer], self.opt.gliedern)

    def setze_ocr_text(self, nummer: int, text: str) -> list[Fundstelle]:
        """Replace the OCR text of page ``nummer`` by the user's correction.

        The correction feeds the text file, the Markdown and the invisible text layer;
        the redaction in the image keeps the OCR boxes (mask what the OCR misread).
        The rules and the fuzzy search run over the corrected text; their hits are
        returned and shown with source ``korrektur``.
        """
        s = self.seiten[nummer]
        if s.ocr is None:
            raise ValueError("Diese Seite hat keinen OCR-Text.")
        if unveraendert(s, text):
            s.korrektur, s.korrektur_treffer = None, []
            return []
        s.korrektur = text
        r = Ruestzeug.nachgebaut(self.kennung, self.vergabe, self.regeln, self.ziele, self.opt)
        s.korrektur_treffer = korrektur_treffer(s, r, self.opt)
        self.version += 1
        return s.korrektur_treffer

    @property
    def korrektur_treffer(self) -> list[Fundstelle]:
        """The text-only hits over the corrected OCR texts of all pages."""
        return [f for s in self.seiten for f in s.korrektur_treffer]

    def unsichere_rects(self, nummer: int) -> list[Box]:
        """Boxes of OCR words read with low confidence on page ``nummer``: uncertain,
        worth a look, not hits."""
        seite = self.seiten[nummer]
        if seite.ocr is None:
            return []
        rects: list[Box] = []
        for (a, b), konf in zip(seite.ocr.wortspannen, seite.ocr.konfidenzen, strict=True):
            if konf < ocrmodul.UNSICHER_KONFIDENZ:
                rects += [tuple(r) for r in seite.ocr.rects(a, b)]
        return rects

    def kasten(self, nummer: int, rect: Box, pseudonym: str = "[geschwärzt]") -> list[Fundstelle]:
        """Add a manual box on page ``nummer`` (0-based); returns the hits it became."""
        seite = self.seiten[nummer]
        neue = kasten_zu_fundstellen(seite.textbasis, rect, pseudonym)
        for f in neue:
            f.quelle = "manuell"
        seite.fundstellen += neue
        self.version += 1
        return neue

    def maske(self, nummer: int, rect: Box) -> list[Fundstelle]:
        """Black out a region of page ``nummer``: text and pixels under it vanish, nothing
        is written into it, and hits lying wholly inside it are dropped as redundant.
        The text output shows ``[geschwärzt]`` in its place."""
        seite = self.seiten[nummer]
        innen = pymupdf.Rect(rect)
        seite.fundstellen = [
            f for f in seite.fundstellen
            if f.regel == "maske" or not f.rects
            or not all(innen.contains(pymupdf.Rect(r)) for r in f.rects)
        ]
        neue = self.kasten(nummer, rect, "[geschwärzt]")
        for f in neue:
            f.regel = "maske"
        return neue

    def wort_bei(self, nummer: int, x: float, y: float) -> tuple[int, int] | None:
        """Offsets of the word under the point (page points) on page ``nummer``, or None
        where no printed character lies within two points."""
        return wort_bei(self.seiten[nummer].textbasis, x, y)

    def aufnehmen(self, nummer: int, start: int, ende: int,
                  pseudonym: str | None = None) -> Fundstelle:
        """Take a span of recognised text into the hit list by hand (a word the analysis
        left alone, a term from the protected vocabulary); hits it overlaps are replaced.
        Without ``pseudonym`` the text gets a code of its own, "[Begriff <code>]", so the
        re-identification can put it back."""
        seite = self.seiten[nummer]
        basis = seite.textbasis
        start, ende = ohne_rand(basis.text, start, ende)
        text = basis.text[start:ende]
        quelle = "ocr" if basis is seite.ocr else "text"
        seite.fundstellen = [f for f in seite.fundstellen
                             if f.quelle not in (quelle, "manuell")
                             or not (start < f.ende and ende > f.start)]
        f = Fundstelle(seite=nummer, start=start, ende=ende, text=text, art=Art.BEGRIFF,
                       regel="aufnahme",
                       pseudonym=pseudonym or self.vergabe.identifikator("Begriff", text,
                                                                         Art.BEGRIFF),
                       rects=[tuple(r) for r in basis.rects(start, ende)],
                       schriftgroesse=basis.schriftgroesse(start, ende), quelle="manuell")
        seite.fundstellen.append(f)
        self.version += 1
        return f

    def schliessen(self) -> None:
        """Release the documents and, when it is ours, the pseudonym store."""
        dokumente_schliessen(self.doc, self.original)
        if self.eigener_speicher:
            self.speicher.close()


def _seitenhinweise(r: Ruestzeug, seiten: list[Seite], verschont: list[str]) -> list[str]:
    """The hints of a detection over pages: NER, identifiers, medical content, and the
    unreadable OCR lines that were redacted as a precaution."""
    alle = [f for s in seiten for f in s.fundstellen]
    hinweise = analysehinweise(r, alle, verschont)
    unlesbar = sum(1 for f in alle if f.regel == "ocr.unlesbar")
    if unlesbar:
        hinweise.append(f"{unlesbar} unlesbare OCR-Zeile(n) vorsorglich geschwärzt.")
    return hinweise


def analysiere(
    lesung: Lesung,
    patient: Patient,
    opt: Ablaufoptionen | None = None,
    speicher: Pseudonymspeicher | None = None,
) -> Vorbereitung:
    """Detection phase over a :class:`Lesung`: rules, vocabulary, fuzzy search, NER,
    codes, unreadable lines, the corrected OCR text.  Takes over the documents of the
    reading (which must not be used afterwards)."""
    if lesung.verbraucht:
        raise RuntimeError("Diese Lesung wurde bereits analysiert.")
    opt = opt or lesung.opt
    eigener_speicher = speicher is None
    speicher = speicher or Pseudonymspeicher()
    verschont: list[str] = []
    try:
        r = ruestzeug(patient, opt, speicher)
        erkenne_seiten(lesung.doc, lesung.seiten, r, opt, verschont)
    except Exception:
        if eigener_speicher:
            speicher.close()
        raise
    lesung.verbraucht = True
    hinweise = list(lesung.hinweise) + _seitenhinweise(r, lesung.seiten, verschont)
    verworfen = gemerkte_verwerfen(lesung.seiten, speicher.verworfene(patient), hinweise)
    return Vorbereitung(
        quelle=lesung.quelle, patient=patient, opt=opt, speicher=speicher,
        eigener_speicher=eigener_speicher, kennung=r.kennung, vergabe=r.vergabe,
        regeln=r.regeln, ziele=r.ziele, backend=lesung.backend, doc=lesung.doc,
        original=lesung.original, seiten=lesung.seiten, ohne_text=lesung.ohne_text,
        warnungen=list(r.warnungen) + list(lesung.warnungen), hinweise=hinweise,
        aufbereitet=lesung.plan, verworfen=verworfen,
    )
