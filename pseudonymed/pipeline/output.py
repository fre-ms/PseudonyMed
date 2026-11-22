"""Side outputs and bookkeeping both paths share: the vocabulary of discarded guesses,
the capsule for a recipient, the residual-risk estimate, the stored decisions, the
report file."""

from __future__ import annotations

from collections.abc import Iterable

from pseudonymed.checks import residual_risk as risk_module
from pseudonymed.checks.report import save_report
from pseudonymed.core.model import Hit
from pseudonymed.detection import medical_terms as terms_module
from pseudonymed.pipeline.options import PipelineOptions, Result, target_path
from pseudonymed.pipeline.preparation import PreparationBase
from pseudonymed.pseudonyms import capsule as capsule_module
from pseudonymed.pseudonyms import pgp as pgp_module

VOCABULARY_HEADER = ("# PseudonyMed: im Prüfschritt verworfene Rate-Treffer (NER, unscharfe"
                     " Suche), ein Begriff je Zeile.\n# In „3 Fachvokabular“ als Nutzervokabular"
                     " importierbar; Zeilen mit # werden überlesen.\n")


def vocabulary_from(discarded: Iterable[Hit]) -> list[str]:
    """Terms for the user's vocabulary: the texts of the discarded guesses (NER, fuzzy
    search), one per entry.  Discarded hits of the fixed rules are left out on purpose:
    their text is one of the patient's own identifiers and must not land in a side file."""
    seen: dict[str, str] = {}
    for f in discarded:
        if not (f.rule.startswith(("ner.", "unscharf.")) or f.source == "unscharf"):
            continue
        term = " ".join(f.text.split())
        if len(term) < 3 or not any(c.isalpha() for c in term):
            continue
        seen.setdefault(term.casefold(), term)
    return sorted(seen.values(), key=str.casefold)


def pseudonyms_of(v: PreparationBase, hits: list[Hit]) -> list[str]:
    """Every pseudonym the output may contain: the patient's mapping and the hits' own."""
    return list(v.assignment.mapping) + [f.pseudonym for f in hits]


def write_capsule(v: PreparationBase, result: Result, notes: list[str]) -> None:
    """For a recipient: the mapping of this document, encrypted to their key."""
    if not v.opt.recipient:
        return
    certificate = pgp_module.load_certificate(v.opt.recipient)
    capsule = capsule_module.create(v.patient, v.code, v.assignment, v.source.name,
                                    certificate.fingerprint, v.store.secret)
    target = capsule_module.write(capsule, certificate,
                                  target_path(v.source, v.opt, capsule_module.EXTENSION))
    result.target_capsule = target
    notes.append(f"Schlüsselkapsel für den Empfänger (Fingerabdruck … {certificate.short})"
                 f" geschrieben: {target.name}. Die Zuordnung der Pseudonyme steht nur dort,"
                 " verschlüsselt; die lokale Datenbank bleibt unberührt. Die Kapsel geht an"
                 " den Empfänger, nie an das Sprachmodell.")


def residual_risk(v: PreparationBase, text: str) -> risk_module.ResidualRisk:
    """The k-map estimate of the pseudonymised text (see
    :mod:`pseudonymed.checks.residual_risk`)."""
    terms = terms_module.standard() if v.opt.medical_terms else None
    return risk_module.estimate(text, v.patient.sex, v.opt.reference_population,
                                [o for o in v.opt.own_places.split(",") if o.strip()], terms)


def write_vocabulary(v: PreparationBase, result: Result, notes: list[str]) -> None:
    """The discarded guesses as a vocabulary file next to the output, when asked for."""
    if not v.opt.vocabulary:
        return
    terms = vocabulary_from(v.discarded)
    if not terms:
        return
    target = target_path(v.source, v.opt, ".vokabular.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(VOCABULARY_HEADER + "\n".join(terms) + "\n", encoding="utf-8")
    result.target_vocabulary = target
    notes.append(f"{len(terms)} verworfene Rate-Treffer als Vokabular geschrieben"
                 f" ({target.name}); in „3 Fachvokabular“ importierbar.")


def remember_decisions(v: PreparationBase) -> None:
    """Store (or forget) the user's discard decisions for this patient when asked to."""
    if not v.opt.remember_discarded:
        return
    if v.tolerated:
        v.store.remember_discarded(v.patient, v.tolerated)
    if v.restored:
        v.store.forget_discarded(v.patient, v.restored)


def write_report(result: Result, opt: PipelineOptions) -> None:
    """The report as JSON next to the output, when asked for."""
    if not opt.report:
        return
    target = target_path(result.source, opt, ".bericht.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    save_report(result.report, target)
    result.target_report = target
