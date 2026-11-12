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

_ERSATZ = {"Ä": "A", "Ö": "O", "Ü": "U", "ß": "S", "É": "E", "È": "E", "Ç": "C", "Ñ": "N"}

# Letters whose code never depends on their neighbours (H is silent).
_FESTE_GRUPPEN = (("AEIJOUY", "0"), ("H", ""), ("B", "1"), ("FVW", "3"), ("GKQ", "4"),
                  ("L", "5"), ("MN", "6"), ("R", "7"), ("SZ", "8"))
_FESTE_CODES: dict[str, str] = {
    buchstabe: code for buchstaben, code in _FESTE_GRUPPEN for buchstabe in buchstaben
}

# Neighbour sets of the context-dependent rules.  Frozensets of single letters,
# so the empty string that marks a word edge is never a member.
_ZISCHLAUTE_NACH_D_T = frozenset("CSZ")
_ZISCHLAUTE_VOR_C = frozenset("SZ")
_HART_NACH_C_AM_ANFANG = frozenset("AHKLOQRUX")
_HART_NACH_C = frozenset("AHKOQUX")
_K_LAUTE_VOR_X = frozenset("CKQ")


class _Umfeld(NamedTuple):
    """The letters on either side of the one being coded; "" where the word ends."""

    vorher: str
    nachher: str

    @property
    def am_wortanfang(self) -> bool:
        """Whether the letter is the first of the word (nothing precedes it)."""
        return self.vorher == ""


def _code_p(umfeld: _Umfeld) -> str:
    """P sounds like F before H ("Philipp"), otherwise like B."""
    return "3" if umfeld.nachher == "H" else "1"


def _code_d_t(umfeld: _Umfeld) -> str:
    """D and T merge into the sibilant before C, S or Z ("Dietz"), otherwise they are 2."""
    return "8" if umfeld.nachher in _ZISCHLAUTE_NACH_D_T else "2"


def _code_c(umfeld: _Umfeld) -> str:
    """C is hard (4) before back vowels, H, K, Q and X and soft (8) elsewhere.

    After S or Z it is always soft ("Scott"); at the start of a word L and R
    also make it hard ("Claudia", "Christian"), inside a word they do not.
    """
    if umfeld.am_wortanfang:
        return "4" if umfeld.nachher in _HART_NACH_C_AM_ANFANG else "8"
    if umfeld.vorher in _ZISCHLAUTE_VOR_C:
        return "8"
    return "4" if umfeld.nachher in _HART_NACH_C else "8"


def _code_x(umfeld: _Umfeld) -> str:
    """X is KS (48) unless a C, K or Q precedes it, which already carries the K."""
    return "8" if umfeld.vorher in _K_LAUTE_VOR_X else "48"


_KONTEXTREGELN: dict[str, Callable[[_Umfeld], str]] = {
    "P": _code_p, "D": _code_d_t, "T": _code_d_t, "C": _code_c, "X": _code_x,
}


def _vorbereiten(text: str) -> str:
    """Return ``text`` in capitals A-Z only: umlauts and accents folded, everything else dropped."""
    text = text.upper()
    text = "".join(_ERSATZ.get(c, c) for c in text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Z]", "", text)


def _buchstabencode(wort: str, i: int) -> str:
    """Return the code digits of the letter at ``i``; context rules see its neighbours."""
    buchstabe = wort[i]
    regel = _KONTEXTREGELN.get(buchstabe)
    if regel is None:
        return _FESTE_CODES.get(buchstabe, "")
    return regel(_Umfeld(vorher=wort[i - 1] if i else "", nachher=wort[i + 1 : i + 2]))


def koelner_phonetik(text: str) -> str:
    """Return the Kölner Phonetik code of ``text`` (digits), "" for empty input."""
    wort = _vorbereiten(text)
    roh = "".join(_buchstabencode(wort, i) for i in range(len(wort)))
    if not roh:
        return ""
    # collapse runs of the same digit, then drop "0" except at the start
    zusammen = re.sub(r"(.)\1+", r"\1", roh)
    return zusammen[0] + zusammen[1:].replace("0", "")
