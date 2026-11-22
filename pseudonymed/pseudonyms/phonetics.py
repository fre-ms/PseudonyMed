"""Kölner Phonetik (Postel 1969): a phonetic code for German names.

"Müller", "Mueller", "Muller" and "Miller" share the code 657; "Meier",
"Maier", "Mayer" and "Meyer" share 67.  Used to canonicalise names before
hashing and as one similarity signal in the record linkage.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import NamedTuple

_REPLACEMENTS = {"Ä": "A", "Ö": "O", "Ü": "U", "ß": "S", "É": "E", "È": "E", "Ç": "C", "Ñ": "N"}

# Letters whose code never depends on their neighbours (H is silent).
_FIXED_GROUPS = (("AEIJOUY", "0"), ("H", ""), ("B", "1"), ("FVW", "3"), ("GKQ", "4"),
                 ("L", "5"), ("MN", "6"), ("R", "7"), ("SZ", "8"))
_FIXED_CODES: dict[str, str] = {
    letter: code for letters, code in _FIXED_GROUPS for letter in letters
}

# Neighbour sets of the context-dependent rules.  Frozensets of single letters,
# so the empty string that marks a word edge is never a member.
_SIBILANTS_AFTER_D_T = frozenset("CSZ")
_SIBILANTS_BEFORE_C = frozenset("SZ")
_HARD_AFTER_C_AT_START = frozenset("AHKLOQRUX")
_HARD_AFTER_C = frozenset("AHKOQUX")
_K_SOUNDS_BEFORE_X = frozenset("CKQ")


class _Neighbours(NamedTuple):
    """The letters on either side of the one being coded; "" where the word ends."""

    before: str
    after: str

    @property
    def at_word_start(self) -> bool:
        """Whether the letter is the first of the word (nothing precedes it)."""
        return self.before == ""


def _code_p(neighbours: _Neighbours) -> str:
    """P sounds like F before H ("Philipp"), otherwise like B."""
    return "3" if neighbours.after == "H" else "1"


def _code_d_t(neighbours: _Neighbours) -> str:
    """D and T merge into the sibilant before C, S or Z ("Dietz"), otherwise they are 2."""
    return "8" if neighbours.after in _SIBILANTS_AFTER_D_T else "2"


def _code_c(neighbours: _Neighbours) -> str:
    """C is hard (4) before back vowels, H, K, Q and X and soft (8) elsewhere.

    After S or Z it is always soft ("Scott"); at the start of a word L and R
    also make it hard ("Claudia", "Christian"), inside a word they do not.
    """
    if neighbours.at_word_start:
        return "4" if neighbours.after in _HARD_AFTER_C_AT_START else "8"
    if neighbours.before in _SIBILANTS_BEFORE_C:
        return "8"
    return "4" if neighbours.after in _HARD_AFTER_C else "8"


def _code_x(neighbours: _Neighbours) -> str:
    """X is KS (48) unless a C, K or Q precedes it, which already carries the K."""
    return "8" if neighbours.before in _K_SOUNDS_BEFORE_X else "48"


_CONTEXT_RULES: dict[str, Callable[[_Neighbours], str]] = {
    "P": _code_p, "D": _code_d_t, "T": _code_d_t, "C": _code_c, "X": _code_x,
}


def _prepare(text: str) -> str:
    """Return ``text`` in capitals A-Z only: umlauts and accents folded, everything else dropped."""
    text = text.upper()
    text = "".join(_REPLACEMENTS.get(c, c) for c in text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Z]", "", text)


def _letter_code(word: str, i: int) -> str:
    """Return the code digits of the letter at ``i``; context rules see its neighbours."""
    letter = word[i]
    rule = _CONTEXT_RULES.get(letter)
    if rule is None:
        return _FIXED_CODES.get(letter, "")
    return rule(_Neighbours(before=word[i - 1] if i else "", after=word[i + 1 : i + 2]))


def cologne_phonetics(text: str) -> str:
    """Return the Kölner Phonetik code of ``text`` (digits), "" for empty input."""
    word = _prepare(text)
    raw = "".join(_letter_code(word, i) for i in range(len(word)))
    if not raw:
        return ""
    # collapse runs of the same digit, then drop "0" except at the start
    joined = re.sub(r"(.)\1+", r"\1", raw)
    return joined[0] + joined[1:].replace("0", "")
