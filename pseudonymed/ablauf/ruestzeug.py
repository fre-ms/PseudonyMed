"""What detection needs for one patient (identity, pseudonyms, rules, helpers) and the
hints a detection run leaves for the review."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pseudonymed.ablauf.optionen import Ablaufoptionen
from pseudonymed.erkennung import fachbegriffe as fachmodul
from pseudonymed.erkennung import identifikatoren as identmodul
from pseudonymed.erkennung import medizininhalte as medmodul
from pseudonymed.erkennung import ner as nermodul
from pseudonymed.erkennung.deterministisch import Regel, regeln_fuer
from pseudonymed.erkennung.unscharf import Ziel, ziele_fuer
from pseudonymed.kern.modell import Art, Fundstelle, Patient
from pseudonymed.pseudonyme.speicher import Kennungsergebnis, Pseudonymspeicher
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

Listen = tuple[fachmodul.Fachbegriffe | None, identmodul.Erkenner | None,
               medmodul.Medizinsuche | None]


@dataclass
class Ruestzeug:
    """What detection needs for one patient: identity, pseudonyms, rules, helpers."""

    kennung: str
    vergabe: Pseudonymvergabe
    regeln: list[Regel]
    ziele: list[Ziel]
    warnungen: list[str]
    ner: nermodul.NER | None
    fachbegriffe: fachmodul.Fachbegriffe | None
    identifikatoren: identmodul.Erkenner | None = None
    medizin: medmodul.Medizinsuche | None = None  # medications and lab values, when chosen

    @classmethod
    def nachgebaut(cls, kennung: str, vergabe: Pseudonymvergabe, regeln: list[Regel],
                   ziele: list[Ziel], opt: Ablaufoptionen) -> Ruestzeug:
        """The gear of a preparation that exists already (a corrected OCR text is searched
        again): the lists follow the options, NER and warnings are left out."""
        return cls(kennung, vergabe, regeln, ziele, [], None, *_listen(opt))


def _listen(opt: Ablaufoptionen) -> Listen:
    """The vocabulary, the identifier lists and the medical content the options ask for."""
    return (fachmodul.standard() if opt.fachbegriffe else None,
            identmodul.standard() if opt.identifikatoren else None,
            medmodul.standard(medmodul.Auswahl.aus(opt)))


def _abgleich_warnung(kennung: Kennungsergebnis) -> list[str]:
    """Tell the user when the patient was matched to an existing entry by score."""
    if not (kennung.verknuepft and kennung.abgleich is not None):
        return []
    a = kennung.abgleich
    felder = ", ".join(f"{k} {v:.2f}" for k, v in a.felder.items())
    return [f"Patient dem vorhandenen Eintrag {a.kennung} zugeordnet"
            f" (Abgleich {a.score:.2f}: {felder})."]


def _ner_laden(opt: Ablaufoptionen, warnungen: list[str]) -> nermodul.NER | None:
    """The NER model when asked for and installed; a warning when asked for and missing."""
    if not opt.ner:
        return None
    if nermodul.verfuegbar():
        return nermodul.NER()
    warnungen.append("NER gewünscht, aber spaCy oder das deutsche Modell fehlt"
                     " (Extra 'ner', siehe Anleitung); ohne NER fortgefahren.")
    return None


def _medizin_warnungen(medizin: medmodul.Medizinsuche | None) -> list[str]:
    """A warning for every chosen medical level whose list is empty."""
    if medizin is None:
        return []
    return [f"{klasse}e schwärzen gewählt, aber die Liste ist leer"
            " (Lexikon nicht geladen?); nichts davon geschwärzt."
            for klasse in medizin.fehlende_listen()]


def ruestzeug(patient: Patient, opt: Ablaufoptionen, speicher: Pseudonymspeicher) -> Ruestzeug:
    """Identity, pseudonyms, rules and helpers for one patient.  Pseudonyms for a
    recipient need the one-time store: the local database must not learn the mapping."""
    if opt.empfaenger and not speicher.ist_einmal:
        raise ValueError("Pseudonyme für einen Empfänger brauchen den Einmalspeicher"
                         " (einmalspeicher()): die lokale Datenbank darf die Zuordnung nicht"
                         " erhalten, und die Codes dürfen nicht die eigenen sein.")
    kennung = speicher.kennung_fuer(patient, opt.abgleich)
    vergabe = Pseudonymvergabe(patient, kennung.kennung, opt.datumsform,
                               geheimnis=speicher.geheimnis)
    warnungen = _abgleich_warnung(kennung)
    ner = _ner_laden(opt, warnungen)
    fachbegriffe, identifikatoren, medizin = _listen(opt)
    warnungen += _medizin_warnungen(medizin)
    return Ruestzeug(kennung.kennung, vergabe, regeln_fuer(patient, vergabe),
                     ziele_fuer(patient, vergabe), warnungen, ner, fachbegriffe,
                     identifikatoren, medizin)


def _klassen_hinweis(fundstellen: list[Fundstelle], art: Art, titel: str,
                     zusatz: str) -> str | None:
    """A count of the hits of ``art`` per class (the first word of the pseudonym)."""
    zaehlung = Counter(f.pseudonym.strip("[]").split(" ")[0]
                       for f in fundstellen if f.art == art)
    if not zaehlung:
        return None
    teile = ", ".join(f"{k} {n}" for k, n in sorted(zaehlung.items()))
    return f"{titel}: {sum(zaehlung.values())} Treffer ({teile}); {zusatz}"


def _identifikator_hinweis(fundstellen: list[Fundstelle]) -> str | None:
    """How many identifiers (hospitals, professions, studies, titles) were found, per class."""
    return _klassen_hinweis(fundstellen, Art.IDENTIFIKATOR, "Identifikatoren",
                            "standardmäßig geschwärzt, im Prüfschritt ansehen.")


def _medizin_hinweis(fundstellen: list[Fundstelle]) -> str | None:
    """How much medical content was redacted on request, per level."""
    return _klassen_hinweis(fundstellen, Art.MEDIZIN, "Medizinischer Inhalt",
                            "auf Wunsch geschwärzt (Optionen in „6 Analyse“).")


def _ner_warnung(r: Ruestzeug, fundstellen: list[Fundstelle], verschont: list[str]) -> str | None:
    """How many hits NER added and how many candidates the vocabulary protected."""
    if r.ner is None:
        return None
    ner_treffer = sum(1 for f in fundstellen if f.regel.startswith("ner."))
    schutz = (f", {len(verschont)} Kandidaten als Fachbegriffe verschont"
              f" ({r.fachbegriffe.anzahl} Begriffe)" if r.fachbegriffe is not None else "")
    return (f"NER ({r.ner.name}): {ner_treffer} zusätzliche Personen/Orte gefunden{schutz};"
            " NER rät, Treffer im Prüfschritt ansehen.")


def analysehinweise(r: Ruestzeug, fundstellen: list[Fundstelle],
                    verschont: list[str]) -> list[str]:
    """The hints of one detection run, in their fixed order: NER, identifiers, medical
    content; empty ones left out."""
    kandidaten = (_ner_warnung(r, fundstellen, verschont), _identifikator_hinweis(fundstellen),
                  _medizin_hinweis(fundstellen))
    return [h for h in kandidaten if h]
