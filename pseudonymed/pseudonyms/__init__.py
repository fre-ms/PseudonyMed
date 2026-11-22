"""Stable pseudonyms, their local store and re-identification of model output."""

from pseudonymed.pseudonyms.assignment import PseudonymAssignment
from pseudonymed.pseudonyms.matching import Comparison, match_candidates
from pseudonymed.pseudonyms.patient_code import canonical_identity, hash_code, is_valid_code
from pseudonymed.pseudonyms.phonetics import cologne_phonetics
from pseudonymed.pseudonyms.reidentification import Reidentification, reidentify
from pseudonymed.pseudonyms.store import CodeResult, PseudonymStore, UncertainMatch

__all__ = [
    "CodeResult",
    "PseudonymStore",
    "PseudonymAssignment",
    "Reidentification",
    "UncertainMatch",
    "Comparison",
    "match_candidates",
    "hash_code",
    "canonical_identity",
    "is_valid_code",
    "cologne_phonetics",
    "reidentify",
]
