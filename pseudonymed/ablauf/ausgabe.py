"""Side outputs and bookkeeping both paths share: the vocabulary of discarded guesses,
the capsule for a recipient, the residual-risk estimate, the stored decisions, the
report file."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pseudonymed.ablauf.optionen import Ablaufoptionen, Ergebnis, zielpfad
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.kern.modell import Fundstelle, Patient
from pseudonymed.pruefung import restrisiko as risikomodul
from pseudonymed.pruefung.bericht import speichere_bericht
from pseudonymed.pseudonyme import kapsel as kapselmodul
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe


class Vorbereitet(Protocol):
    """What the side outputs need from a preparation, PDF or text alike."""

    quelle: Path
    patient: Patient
    opt: Ablaufoptionen
    speicher: Pseudonymspeicher
    kennung: str
    vergabe: Pseudonymvergabe
    verworfen: list[Fundstelle]
    wiederhergestellt: set[tuple[str, str]]

    @property
    def toleriert(self) -> set[tuple[str, str]]:
        """The rule/text pairs of the discarded hits."""
        ...

VOKABULAR_KOPF = ("# PseudonyMed: im Prüfschritt verworfene Rate-Treffer (NER, unscharfe Suche),"
                  " ein Begriff je Zeile.\n# In „3 Fachvokabular“ als Nutzervokabular importierbar;"
                  " Zeilen mit # werden überlesen.\n")


def vokabular_aus(verworfen: Iterable[Fundstelle]) -> list[str]:
    """Terms for the user's vocabulary: the texts of the discarded guesses (NER, fuzzy
    search), one per entry.  Discarded hits of the fixed rules are left out on purpose:
    their text is one of the patient's own identifiers and must not land in a side file."""
    gesehen: dict[str, str] = {}
    for f in verworfen:
        if not (f.regel.startswith(("ner.", "unscharf.")) or f.quelle == "unscharf"):
            continue
        begriff = " ".join(f.text.split())
        if len(begriff) < 3 or not any(c.isalpha() for c in begriff):
            continue
        gesehen.setdefault(begriff.casefold(), begriff)
    return sorted(gesehen.values(), key=str.casefold)


def pseudonyme_von(v: Vorbereitet, fundstellen: list[Fundstelle]) -> list[str]:
    """Every pseudonym the output may contain: the patient's mapping and the hits' own."""
    return list(v.vergabe.zuordnung) + [f.pseudonym for f in fundstellen]


def kapsel_schreiben(v: Vorbereitet, ergebnis: Ergebnis, hinweise: list[str]) -> None:
    """For a recipient: the mapping of this document, encrypted to their key."""
    if not v.opt.empfaenger:
        return
    zertifikat = pgpmodul.lade_zertifikat(v.opt.empfaenger)
    kapsel = kapselmodul.erstelle(v.patient, v.kennung, v.vergabe, v.quelle.name,
                                  zertifikat.fingerabdruck, v.speicher.geheimnis)
    ziel = kapselmodul.schreibe(kapsel, zertifikat, zielpfad(v.quelle, v.opt, kapselmodul.ENDUNG))
    ergebnis.ziel_kapsel = ziel
    hinweise.append(f"Schlüsselkapsel für den Empfänger (Fingerabdruck … {zertifikat.kurz})"
                    f" geschrieben: {ziel.name}. Die Zuordnung der Pseudonyme steht nur dort,"
                    " verschlüsselt; die lokale Datenbank bleibt unberührt. Die Kapsel geht an"
                    " den Empfänger, nie an das Sprachmodell.")


def restrisiko(v: Vorbereitet, text: str) -> risikomodul.Restrisiko:
    """The k-map estimate of the pseudonymised text (see :mod:`pseudonymed.pruefung.restrisiko`)."""
    fach = fachmodul.standard() if v.opt.fachbegriffe else None
    return risikomodul.schaetze(text, v.patient.geschlecht, v.opt.referenzpopulation,
                                [o for o in v.opt.eigene_orte.split(",") if o.strip()], fach)


def vokabular_schreiben(v: Vorbereitet, ergebnis: Ergebnis, hinweise: list[str]) -> None:
    """The discarded guesses as a vocabulary file next to the output, when asked for."""
    if not v.opt.vokabular:
        return
    begriffe = vokabular_aus(v.verworfen)
    if not begriffe:
        return
    ziel = zielpfad(v.quelle, v.opt, ".vokabular.txt")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(VOKABULAR_KOPF + "\n".join(begriffe) + "\n", encoding="utf-8")
    ergebnis.ziel_vokabular = ziel
    hinweise.append(f"{len(begriffe)} verworfene Rate-Treffer als Vokabular geschrieben"
                    f" ({ziel.name}); in „3 Fachvokabular“ importierbar.")


def entscheidungen_merken(v: Vorbereitet) -> None:
    """Store (or forget) the user's discard decisions for this patient when asked to."""
    if not v.opt.verworfene_merken:
        return
    if v.toleriert:
        v.speicher.merke_verworfen(v.patient, v.toleriert)
    if v.wiederhergestellt:
        v.speicher.vergiss_verworfen(v.patient, v.wiederhergestellt)


def bericht_schreiben(ergebnis: Ergebnis, opt: Ablaufoptionen) -> None:
    """The report as JSON next to the output, when asked for."""
    if not opt.bericht:
        return
    ziel = zielpfad(ergebnis.quelle, opt, ".bericht.json")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    speichere_bericht(ergebnis.bericht, ziel)
    ergebnis.ziel_bericht = ziel
