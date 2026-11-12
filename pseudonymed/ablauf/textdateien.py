"""Phase one and two for text files (transcripts, notes, subtitles, e-mails): the
analysis copy, the spans the user marks, the replaced text and its check."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pseudonymed.ablauf.ausgabe import (
    bericht_schreiben,
    entscheidungen_merken,
    kapsel_schreiben,
    pseudonyme_von,
    restrisiko,
    vokabular_schreiben,
)
from pseudonymed.ablauf.erkennen import ergaenze_treffer, gemerkte_verwerfen, merke_fachbegriffe
from pseudonymed.ablauf.optionen import Ablaufoptionen, Ergebnis, zielpfad
from pseudonymed.ablauf.ruestzeug import Ruestzeug, analysehinweise
from pseudonymed.ablauf.seiten import Box, Seite, ohne_rand
from pseudonymed.ablauf.vorbereitung import Entscheidungen
from pseudonymed.dokument import aufbereitung as aufmodul
from pseudonymed.dokument import textdatei as textmodul
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import zahlwoerter
from pseudonymed.erkennung.deterministisch import Regel, finde
from pseudonymed.erkennung.unscharf import Ziel, finde_unscharf
from pseudonymed.kern.modell import Art, Fundstelle, Gegenprobe, Patient
from pseudonymed.pruefung.bericht import erstelle_bericht
from pseudonymed.pruefung.gegenprobe import (
    geprueft_aus,
    in_pseudonym,
    pseudonymspannen,
    toleranzschluessel,
)
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


@dataclass
class Textvorbereitung(Entscheidungen):
    """Phase one for a text file (transcript, note, subtitles).

    Same surface as :class:`Vorbereitung` as far as a user interface needs it:
    ``fundstellen``, ``verwerfe``, ``toleriert``, ``warnungen``; instead of
    boxes the user marks spans (``markiere``).  The hits' offsets refer to the
    analysis copy, which has the same length as ``text``.
    """

    ist_text: ClassVar[bool] = True

    quelle: Path
    patient: Patient
    opt: Ablaufoptionen
    speicher: Pseudonymspeicher
    eigener_speicher: bool
    kennung: str
    vergabe: Pseudonymvergabe
    regeln: list[Regel]
    ziele: list[Ziel]
    text: str
    seiten: list[Seite]
    ausgabe_endung: str = ".txt"  # the input's own for text and subtitles, .txt for JSON/e-mail
    ohne_text: list[int] = field(default_factory=list)
    warnungen: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    verworfen: list[Fundstelle] = field(default_factory=list)
    abgeschlossen: bool = False
    version: int = 0
    wiederhergestellt: set[tuple[str, str]] = field(default_factory=set)
    aufbereitet: dict[int, aufmodul.Aufbereitung] = field(default_factory=dict)

    @property
    def fundstellen(self) -> list[Fundstelle]:
        """Every hit, in text order."""
        return sorted((f for s in self.seiten for f in s.fundstellen), key=lambda f: f.start)

    @property
    def ocr_seiten(self) -> list[int]:
        """A text file has no OCR pages."""
        return []

    def fachbegriffe_rects(self, nummer: int) -> list[Box]:
        """A text file has no page geometry."""
        return []

    def fachbegriffe_spannen(self, nummer: int) -> list[tuple[int, int]]:
        """Offsets of the medical terms, not where a hit lies."""
        belegt = [(f.start, f.ende) for f in self.seiten[nummer].fundstellen]
        return [(a, b) for a, b in self.seiten[nummer].fachspannen
                if not any(a < y and b > x for x, y in belegt)]

    def unsichere_rects(self, nummer: int) -> list[Box]:
        """A text file has no uncertain OCR words."""
        return []

    def markiere(self, nummer: int, start: int, ende: int,
                 pseudonym: str = "[geschwärzt]") -> Fundstelle:
        """Mark ``[start, ende)`` by hand; hits it overlaps are replaced by it."""
        seite = self.seiten[nummer]
        text = seite.text.text  # the analysis copy, same length as ``self.text``
        start, ende = ohne_rand(text, start, ende)
        seite.fundstellen = [f for f in seite.fundstellen
                             if not (start < f.ende and ende > f.start)]
        f = Fundstelle(seite=nummer, start=start, ende=ende, text=self.text[start:ende],
                       art=Art.MANUELL, regel="manuell", pseudonym=pseudonym, quelle="manuell")
        seite.fundstellen.append(f)
        self.version += 1
        return f

    def aufnehmen(self, nummer: int, start: int, ende: int,
                  pseudonym: str | None = None) -> Fundstelle:
        """Like :meth:`markiere`, with a code of its own by default so that the
        re-identification can put the text back."""
        text = self.seiten[nummer].text.text
        start, ende = ohne_rand(text, start, ende)
        if pseudonym is None:
            pseudonym = self.vergabe.identifikator("Begriff", self.text[start:ende], Art.BEGRIFF)
        f = self.markiere(nummer, start, ende, pseudonym)
        f.art, f.regel = Art.BEGRIFF, "aufnahme"
        return f

    def schliessen(self) -> None:
        """Release the pseudonym store when it is ours."""
        if self.eigener_speicher:
            self.speicher.close()


def _erkenne_text(eintrag: Seite, r: Ruestzeug, opt: Ablaufoptionen,
                  verschont: list[str]) -> None:
    """Rules, vocabulary marks, fuzzy search, identifiers, medical content, NER and
    titles over the analysis copy, in that order: a misspelt known name must get the
    patient's pseudonym, not a NER code."""
    seite = eintrag.text
    eintrag.fundstellen = finde(seite, r.regeln)
    if r.fachbegriffe is not None:
        merke_fachbegriffe(eintrag, seite, r.fachbegriffe)
    if opt.unscharf_text:
        eintrag.fundstellen += finde_unscharf(seite, r.ziele, eintrag.fundstellen, r.fachbegriffe,
                                              grossschreibung=True, quelle="unscharf")
    ergaenze_treffer(seite, eintrag.fundstellen, "text", r, verschont)


def _unscharf_hinweis(fundstellen: list[Fundstelle]) -> list[str]:
    """How many hits the fuzzy search contributed, worth a look in the review."""
    unscharfe = sum(1 for f in fundstellen if f.quelle == "unscharf")
    if not unscharfe:
        return []
    return [f"Unscharfe Suche: {unscharfe} abweichend geschriebene Treffer"
            " (Transkript, Tippfehler); im Prüfschritt ansehen."]


def vorbereite_text(
    quelle: Path, patient: Patient, opt: Ablaufoptionen, speicher: Pseudonymspeicher,
    eigener_speicher: bool, r: Ruestzeug,
) -> Textvorbereitung:
    """Phase one over one text file.  Number words are rewritten as digits first; hits
    touching them are widened to the words."""
    text, endung = textmodul.lese_quelle(quelle)
    norm = zahlwoerter.normalisiere(textmodul.analysetext(text, endung))
    eintrag = Seite(0, textmodul.textseite(norm.text))
    verschont: list[str] = []
    _erkenne_text(eintrag, r, opt, verschont)
    eintrag.fundstellen = auf_zahlwoerter_erweitert(eintrag.fundstellen, norm)
    hinweise = analysehinweise(r, eintrag.fundstellen, verschont)
    hinweise += _unscharf_hinweis(eintrag.fundstellen)
    verworfen = gemerkte_verwerfen([eintrag], speicher.verworfene(patient), hinweise)
    return Textvorbereitung(
        quelle=quelle, patient=patient, opt=opt, speicher=speicher,
        eigener_speicher=eigener_speicher, kennung=r.kennung, vergabe=r.vergabe,
        regeln=r.regeln, ziele=r.ziele, text=text, seiten=[eintrag], ausgabe_endung=endung,
        warnungen=list(r.warnungen), hinweise=hinweise, verworfen=verworfen,
    )


def auf_zahlwoerter_erweitert(fundstellen: list[Fundstelle],
                              norm: zahlwoerter.Normalisiert) -> list[Fundstelle]:
    """A hit on "21. Juli 1959" that stands for "einundzwanzigsten Juli neunzehnhundert-
    neunundfünfzig" must cover all those words; after widening, overlapping hits are
    reduced to the longest."""
    for f in fundstellen:
        if f.ende > f.start:
            a, b = norm.erweitere(f.start, f.ende)
            if (a, b) != (f.start, f.ende):
                f.start, f.ende, f.text = a, b, norm.text[a:b]
    belegt: list[tuple[int, int]] = []
    behalten: list[Fundstelle] = []
    for f in sorted(fundstellen, key=lambda f: (-(f.ende - f.start), f.start)):
        if f.ende > f.start and any(f.start < y and f.ende > x for x, y in belegt):
            continue
        belegt.append((f.start, f.ende))
        behalten.append(f)
    return sorted(behalten, key=lambda f: f.start)


def gegenprobe_text(
    text: str, endung: str, regeln: list[Regel], ziele: list[Ziel] | None = None,
    fachbegriffe: fachmodul.Fachbegriffe | None = None,
    toleriert: set[tuple[str, str]] | None = None, pseudonyme: list[str] | None = None,
) -> Gegenprobe:
    """Re-run the rules (and, with ``ziele``, the fuzzy search) over a pseudonymised text."""
    toleriert = toleriert or set()
    analyse = zahlwoerter.normalisiere(textmodul.analysetext(text, endung)).text
    seite = textmodul.textseite(analyse)
    spannen = pseudonymspannen(analyse, pseudonyme or [])
    genau = finde(seite, regeln)
    rest = list(genau)
    if ziele is not None:
        rest += finde_unscharf(seite, ziele, genau, fachbegriffe, grossschreibung=True,
                               quelle="unscharf")
    g = Gegenprobe(geprueft=geprueft_aus(regeln))
    for f in rest:
        if in_pseudonym(f.start, f.ende, spannen):
            continue
        if toleranzschluessel(f.regel, f.text) in toleriert:
            g.belassen.append(f.regel)
            continue
        g.resttreffer.append(f.ohne_klartext())
    return g


def gegenprobe_textdatei(pfad: str | Path, regeln: list[Regel],
                         pseudonyme: list[str] | None = None) -> Gegenprobe:
    """``pseudonymed pruefen`` for a text file: the rules alone, nothing tolerated; inside
    ``pseudonyme`` (at least the patient's code) no rule counts."""
    text, endung = textmodul.lese_quelle(pfad)
    return gegenprobe_text(text, endung, regeln, pseudonyme=pseudonyme)


def _gegenprobe_ausgabe(v: Textvorbereitung, ausgabe: str,
                        fundstellen: list[Fundstelle]) -> Gegenprobe:
    """The check of the replaced text with everything the preparation tolerates."""
    fach = fachmodul.standard() if v.opt.fachbegriffe else None
    return gegenprobe_text(ausgabe, v.ausgabe_endung, v.regeln,
                           v.ziele if v.opt.unscharf_text else None, fach,
                           v.toleriert, pseudonyme_von(v, fundstellen))


def _schreibe_textausgabe(v: Textvorbereitung, ergebnis: Ergebnis, ausgabe: str,
                          hinweise: list[str]) -> None:
    """Keep the output: the text, a Markdown copy when asked for, vocabulary, capsule."""
    ziel = zielpfad(v.quelle, v.opt, v.ausgabe_endung)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(ausgabe, encoding="utf-8")
    ergebnis.ziel_txt = ziel
    if v.opt.markdown and ziel.suffix.lower() != ".md":
        ergebnis.ziel_md = zielpfad(v.quelle, v.opt, ".md")
        ergebnis.ziel_md.write_text(ausgabe, encoding="utf-8")
    vokabular_schreiben(v, ergebnis, hinweise)
    kapsel_schreiben(v, ergebnis, hinweise)


def abschliesse_text(v: Textvorbereitung, endgueltig: bool = True) -> Ergebnis:
    """Phase two for a text file: replace, check, write, report.  The caller closes
    the preparation."""
    opt, quelle = v.opt, v.quelle
    warnungen, hinweise = list(v.warnungen), list(v.hinweise)
    fundstellen = v.fundstellen
    v.vergabe.registriere_treffer(fundstellen)
    ausgabe = textmodul.ersetze(v.text, fundstellen)
    gegenprobe = _gegenprobe_ausgabe(v, ausgabe, fundstellen)
    v.speicher.merke(v.patient, v.vergabe)
    if endgueltig:
        entscheidungen_merken(v)
    ergebnis = Ergebnis(quelle=quelle, bericht=None, fundstellen=fundstellen,  # type: ignore[arg-type]
                        kennung=v.kennung, text=ausgabe, ist_text=True)
    ergebnis.restrisiko = restrisiko(v, ausgabe)
    if gegenprobe.bestanden:
        _schreibe_textausgabe(v, ergebnis, ausgabe, hinweise)
    else:
        warnungen.append("Nicht freigegeben: keine Ausgabe geschrieben.")
    ergebnis.bericht = erstelle_bericht(quelle, ergebnis.ziel_txt, 1, True, fundstellen,
                                        gegenprobe, opt.als_dict(), warnungen, hinweise,
                                        ergebnis.restrisiko.als_dict())
    bericht_schreiben(ergebnis, opt)
    return ergebnis
