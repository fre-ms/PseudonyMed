"""What detection needs for one patient (identity, pseudonyms, rules, helpers) and the
hints a detection run leaves for the review."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pseudonymed.core.model import Hit, Kind, Patient
from pseudonymed.detection import identifiers as ident_module
from pseudonymed.detection import medical_content as medical_module
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.detection import ner as ner_module
from pseudonymed.detection.fuzzy import Target, targets_for
from pseudonymed.detection.rules import Rule, rules_for
from pseudonymed.pipeline.options import PipelineOptions
from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.store import CodeResult, PseudonymStore

Lists = tuple[terms_module.MedicalTerms | None, ident_module.Recogniser | None,
              medical_module.MedicalSearch | None]


@dataclass
class Toolkit:
    """What detection needs for one patient: identity, pseudonyms, rules, helpers."""

    code: str
    assignment: PseudonymAssignment
    rules: list[Rule]
    targets: list[Target]
    warnings: list[str]
    ner: ner_module.NER | None
    medical_terms: terms_module.MedicalTerms | None
    identifiers: ident_module.Recogniser | None = None
    medical: medical_module.MedicalSearch | None = None  # medications and lab values, when chosen

    @classmethod
    def rebuilt(cls, code: str, assignment: PseudonymAssignment, rules: list[Rule],
                targets: list[Target], opt: PipelineOptions) -> Toolkit:
        """The gear of a preparation that exists already (a corrected OCR text is searched
        again): the lists follow the options, NER and warnings are left out."""
        return cls(code, assignment, rules, targets, [], None, *_lists(opt))


def _lists(opt: PipelineOptions) -> Lists:
    """The vocabulary, the identifier lists and the medical content the options ask for."""
    return (terms_module.standard() if opt.medical_terms else None,
            ident_module.standard() if opt.identifiers else None,
            medical_module.standard(medical_module.Selection.from_options(opt)))


def _matching_warning(code: CodeResult) -> list[str]:
    """Tell the user when the patient was matched to an existing entry by score."""
    if not (code.linked and code.matching is not None):
        return []
    a = code.matching
    fields = ", ".join(f"{k} {v:.2f}" for k, v in a.fields.items())
    return [f"Patient dem vorhandenen Eintrag {a.code} zugeordnet"
            f" (Abgleich {a.score:.2f}: {fields})."]


def _load_ner(opt: PipelineOptions, warnings: list[str]) -> ner_module.NER | None:
    """The NER model when asked for and installed; a warning when asked for and missing."""
    if not opt.ner:
        return None
    if ner_module.available():
        return ner_module.NER()
    warnings.append("NER gewünscht, aber spaCy oder das deutsche Modell fehlt"
                    " (Extra 'ner', siehe Anleitung); ohne NER fortgefahren.")
    return None


def _medical_warnings(medical: medical_module.MedicalSearch | None) -> list[str]:
    """A warning for every chosen medical level whose list is empty."""
    if medical is None:
        return []
    return [f"{category}e schwärzen gewählt, aber die Liste ist leer"
            " (Lexikon nicht geladen?); nichts davon geschwärzt."
            for category in medical.missing_lists()]


def toolkit(patient: Patient, opt: PipelineOptions, store: PseudonymStore) -> Toolkit:
    """Identity, pseudonyms, rules and helpers for one patient.  Pseudonyms for a
    recipient need the one-time store: the local database must not learn the mapping."""
    if opt.recipient and not store.is_one_time:
        raise ValueError("Pseudonyme für einen Empfänger brauchen den Einmalspeicher"
                         " (einmalspeicher()): die lokale Datenbank darf die Zuordnung nicht"
                         " erhalten, und die Codes dürfen nicht die eigenen sein.")
    code = store.code_for(patient, opt.matching)
    assignment = PseudonymAssignment(patient, code.code, opt.date_format,
                                     secret=store.secret)
    warnings = _matching_warning(code)
    ner = _load_ner(opt, warnings)
    medical_terms, identifiers, medical = _lists(opt)
    warnings += _medical_warnings(medical)
    return Toolkit(code.code, assignment, rules_for(patient, assignment),
                   targets_for(patient, assignment), warnings, ner, medical_terms,
                   identifiers, medical)


def _category_note(hits: list[Hit], kind: Kind, title: str,
                   extra: str) -> str | None:
    """A count of the hits of ``kind`` per class (the first word of the pseudonym)."""
    counts = Counter(f.pseudonym.strip("[]").split(" ")[0]
                     for f in hits if f.kind == kind)
    if not counts:
        return None
    parts = ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
    return f"{title}: {sum(counts.values())} Treffer ({parts}); {extra}"


def _identifier_note(hits: list[Hit]) -> str | None:
    """How many identifiers (hospitals, professions, studies, titles) were found, per class."""
    return _category_note(hits, Kind.IDENTIFIER, "Identifikatoren",
                          "standardmäßig geschwärzt, im Prüfschritt ansehen.")


def _medical_note(hits: list[Hit]) -> str | None:
    """How much medical content was redacted on request, per level."""
    return _category_note(hits, Kind.MEDICAL, "Medizinischer Inhalt",
                          "auf Wunsch geschwärzt (Optionen in „6 Analyse“).")


def _ner_warning(r: Toolkit, hits: list[Hit], spared: list[str]) -> str | None:
    """How many hits NER added and how many candidates the vocabulary protected."""
    if r.ner is None:
        return None
    ner_matches = sum(1 for f in hits if f.rule.startswith("ner."))
    protection = (f", {len(spared)} Kandidaten als Fachbegriffe verschont"
                  f" ({r.medical_terms.count} Begriffe)" if r.medical_terms is not None else "")
    return (f"NER ({r.ner.name}): {ner_matches} zusätzliche Personen/Orte gefunden{protection};"
            " NER rät, Treffer im Prüfschritt ansehen.")


def analysis_notes(r: Toolkit, hits: list[Hit],
                   spared: list[str]) -> list[str]:
    """The hints of one detection run, in their fixed order: NER, identifiers, medical
    content; empty ones left out."""
    candidates = (_ner_warning(r, hits, spared), _identifier_note(hits),
                  _medical_note(hits))
    return [h for h in candidates if h]
