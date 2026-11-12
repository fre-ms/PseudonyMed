"""Character-level view of a page's text layer.

PyMuPDF's ``rawdict`` gives every character with its bounding box and
baseline.  We flatten that into one string per page plus parallel lists (box,
line id, font size, baseline per character) so that ordinary regular
expressions can be run over the page and every match can be mapped back to
rectangles for redaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pymupdf

Box = tuple[float, float, float, float]

# Expand ligatures ("ﬁ" -> "f", "i") so that names match letter by letter.
_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES


@dataclass
class Seitentext:
    """Reconstructed text of one page with a box for every character."""

    nummer: int
    breite: float
    hoehe: float
    text: str = ""
    boxen: list[Box | None] = field(default_factory=list)
    zeilen: list[int] = field(default_factory=list)
    groessen: list[float] = field(default_factory=list)
    basislinien: list[float] = field(default_factory=list)
    zeilenboxen: dict[int, Box] = field(default_factory=dict)
    quelle: str = "text"  # "text" (PDF text layer) or "ocr"
    konfidenzen: list[float] = field(default_factory=list)  # OCR word confidences
    wortspannen: list[tuple[int, int]] = field(default_factory=list)  # OCR word offsets

    MIN_ZEICHEN_TEXTEBENE = 20

    @property
    def hat_textebene(self) -> bool:
        """True when the page carries enough real text to work without OCR."""
        return len("".join(self.text.split())) >= self.MIN_ZEICHEN_TEXTEBENE

    @property
    def unsichere_woerter(self) -> int:
        """Number of OCR words below 60 % confidence (0 for a text layer)."""
        return sum(1 for k in self.konfidenzen if k < 0.6)

    def rects(self, start: int, ende: int) -> list[pymupdf.Rect]:
        """Union of character boxes in ``[start, ende)``, one rectangle per line."""
        je_zeile: dict[int, pymupdf.Rect] = {}
        for i in range(start, min(ende, len(self.text))):
            box = self.boxen[i]
            if box is None:
                continue
            r = pymupdf.Rect(box)
            z = self.zeilen[i]
            if z in je_zeile:
                je_zeile[z] |= r
            else:
                je_zeile[z] = r
        return [r for _, r in sorted(je_zeile.items())]

    def woerter(self) -> list[tuple[int, int]]:
        """Word spans: the OCR words, or runs of printed characters between whitespace."""
        if self.wortspannen:
            return list(self.wortspannen)
        spannen: list[tuple[int, int]] = []
        start: int | None = None
        for i, c in enumerate(self.text + " "):
            gedruckt = i < len(self.text) and not c.isspace() and self.boxen[i] is not None
            if gedruckt and start is None:
                start = i
            elif not gedruckt and start is not None:
                spannen.append((start, i))
                start = None
        return spannen

    def zeile_von(self, index: int) -> int:
        return self.zeilen[index] if 0 <= index < len(self.zeilen) else -1

    def schriftgroesse(self, start: int, ende: int) -> float:
        groessen = sorted(
            self.groessen[i]
            for i in range(start, min(ende, len(self.text)))
            if self.boxen[i] is not None
        )
        return groessen[len(groessen) // 2] if groessen else 10.0

    def basislinie(self, start: int, ende: int) -> float | None:
        """Baseline (y) of the first printed character of the match."""
        for i in range(start, min(ende, len(self.text))):
            if self.boxen[i] is not None:
                return self.basislinien[i]
        return None

    def freier_raum_rechts(self, ende: int) -> float:
        """x coordinate of the next printed character on the same line after ``ende``.

        Used to widen a redaction box so that the pseudonym stays legible when
        it is longer than the text it replaces.  Spaces count as free.
        """
        if ende <= 0 or ende > len(self.text):
            return self.breite
        zeile = self.zeilen[ende - 1]
        for k in range(ende, len(self.text)):
            if self.zeilen[k] != zeile:
                break
            box = self.boxen[k]
            if box is None or self.text[k].isspace():
                continue
            return box[0]
        return self.breite - 18.0

    def als_dict(self) -> dict[str, Any]:
        """Everything needed to rebuild the page without reading it again (the working
        state); boxes rounded to a hundredth of a point."""
        def runde(b: Box | None) -> list[float] | None:
            return [round(v, 2) for v in b] if b is not None else None

        return {
            "nummer": self.nummer, "breite": round(self.breite, 2), "hoehe": round(self.hoehe, 2),
            "text": self.text, "quelle": self.quelle,
            "boxen": [runde(b) for b in self.boxen], "zeilen": self.zeilen,
            "groessen": [round(g, 2) for g in self.groessen],
            "basislinien": [round(b, 2) for b in self.basislinien],
            "zeilenboxen": {str(z): runde(b) for z, b in self.zeilenboxen.items()},
            "konfidenzen": [round(k, 3) for k in self.konfidenzen],
            "wortspannen": [list(s) for s in self.wortspannen],
        }

    @classmethod
    def aus_dict(cls, d: dict[str, Any]) -> Seitentext:
        seite = cls(nummer=int(d["nummer"]), breite=float(d["breite"]), hoehe=float(d["hoehe"]),
                    text=str(d["text"]), quelle=str(d.get("quelle", "text")))
        seite.boxen = [tuple(b) if b is not None else None for b in d.get("boxen", [])]
        seite.zeilen = [int(z) for z in d.get("zeilen", [])]
        seite.groessen = [float(g) for g in d.get("groessen", [])]
        seite.basislinien = [float(b) for b in d.get("basislinien", [])]
        seite.zeilenboxen = {int(z): tuple(b) for z, b in d.get("zeilenboxen", {}).items()
                             if b is not None}
        seite.konfidenzen = [float(k) for k in d.get("konfidenzen", [])]
        seite.wortspannen = [(int(a), int(b)) for a, b in d.get("wortspannen", [])]
        n = len(seite.text)
        if any(len(liste) != n for liste in (seite.boxen, seite.zeilen, seite.groessen,
                                              seite.basislinien)):
            raise ValueError(f"Seitentext {seite.nummer + 1}: Zeichenlisten passen nicht zum Text.")
        return seite

    def _fuege_an(
        self, zeichen: str, box: Box | None, zeile: int, groesse: float, basislinie: float
    ) -> None:
        for c in zeichen:
            self.text += c
            self.boxen.append(box)
            self.zeilen.append(zeile)
            self.groessen.append(groesse)
            self.basislinien.append(basislinie)
        if box is not None:
            alt = self.zeilenboxen.get(zeile)
            if alt is None:
                self.zeilenboxen[zeile] = box
            else:
                self.zeilenboxen[zeile] = (
                    min(alt[0], box[0]), min(alt[1], box[1]),
                    max(alt[2], box[2]), max(alt[3], box[3]),
                )


def lese_seite(page: pymupdf.Page, nummer: int | None = None) -> Seitentext:
    """Flatten the page's text layer into a :class:`Seitentext`."""
    nummer = page.number if nummer is None else nummer
    seite = Seitentext(nummer=nummer, breite=page.rect.width, hoehe=page.rect.height)
    roh = page.get_text("rawdict", flags=_FLAGS, sort=True)
    zeile = 0
    for block in roh.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            zeile += 1
            letzte_box: Box | None = None
            for span in line.get("spans", []):
                groesse = float(span.get("size", 10.0))
                span_basis = float(span.get("origin", (0.0, 0.0))[1])
                for ch in span.get("chars", []):
                    box = tuple(float(v) for v in ch["bbox"])
                    basis = float(ch.get("origin", (0.0, span_basis))[1]) or span_basis
                    if letzte_box is not None:
                        luecke = box[0] - letzte_box[2]
                        getrennt = not seite.text.endswith(" ")
                        if luecke > 0.25 * groesse and getrennt:
                            seite._fuege_an(" ", None, zeile, groesse, basis)
                    seite._fuege_an(ch["c"], box, zeile, groesse, basis)
                    letzte_box = box
            seite._fuege_an("\n", None, zeile, 0.0, 0.0)
        seite._fuege_an("\n", None, zeile, 0.0, 0.0)
    return seite


def lese_dokument(doc: pymupdf.Document) -> list[Seitentext]:
    return [lese_seite(page, i) for i, page in enumerate(doc)]
