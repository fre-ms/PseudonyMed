"""Messages in both interface languages.

The core writes its messages in German: the command line shows them, a working state stores
them, and the code and the tests look for words in them.  A message the desktop also shows
in English carries its English form along: :func:`say` returns a :class:`Text`, a ``str``
whose value is the German message with ``en`` attached, so that it prints, compares, joins
and stores exactly as before.  :func:`in_language` picks the form for the interface.

Text that comes back without its English form goes through a short table: the fixed labels
of categories and kinds (``_WORDS``) and the weight descriptions of the residual-risk
estimate, which the store keeps for later documents (``_PATTERNS``).  A working state keeps
the English form of its messages itself.  Anything else stays as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


class Text(str):
    """A German message with its English form in ``en``."""

    en: str

    def __new__(cls, de: str, en: str) -> Text:
        text = super().__new__(cls, de)
        text.en = en
        return text

    def __getnewargs__(self) -> tuple[str, str]:  # type: ignore[override]  # copies keep en
        return str(self), self.en


def say(de: str, en: str) -> Text:
    """The message ``de`` with its English form ``en``."""
    return Text(de, en)


def english(language: str) -> bool:
    """Whether a language code asks for English ("en", "en_GB" …)."""
    return language.lower().startswith("en")


def in_language(text: object, language: str) -> str:
    """The text in the interface language: German as it is; English from the attached
    form, else from the read-back table, else unchanged."""
    if not english(language):
        return str(text)
    if isinstance(text, Text):
        return text.en
    return read_back(str(text))


def joined(parts: Iterable[object], language: str, separator: str = ", ") -> str:
    """Several texts in the interface language, joined."""
    return separator.join(in_language(p, language) for p in parts)


def error_text(error: BaseException, language: str) -> str:
    """The message of an exception in the interface language (its first argument when that
    is a :class:`Text`)."""
    first = error.args[0] if error.args else None
    if isinstance(first, Text) and len(error.args) == 1:
        return in_language(first, language)
    return in_language(str(error), language)


# -- German text without its English form ----------------------------------------------------------
_WORDS = {  # fixed labels of categories and kinds (code-defined, not user data)
    "Patient": "Patient", "Angehörige": "Relatives", "Zuweiser": "Referrers",
    "Geburtsdatum": "Date of birth", "Geburtsname": "Birth name", "Straße": "Street",
    "PLZ und Ort": "Postcode and place", "Ort": "Place", "Person (NER)": "Person (NER)",
    "Telefon": "Phone", "E-Mail": "E-mail", "Versichertennummer": "Insurance number",
    "Patienten-ID": "Patient ID", "Aktenzeichen": "Case number", "IBAN": "IBAN",
    "Barcode/QR": "Barcode/QR", "Beruf": "Occupation", "Klinik": "Hospital", "Studie": "Study",
    "Titel": "Title", "Identifikator": "Identifier", "Medikament": "Drug",
    "Dosierung": "Dose", "Laborwert": "Lab test", "Messwert": "Measurement", "Name": "Name",
    "Adresse": "Address", "Begriff": "Term", "Manuell": "Manual",
    "Medizinischer Inhalt": "Medical content", "Sonstige": "Other", "Geschlecht": "Sex",
    "Diagnose": "Diagnosis", "Datum": "Date",
}
_PATTERNS = tuple((re.compile(p), e) for p, e in (
    # the weights of the residual-risk estimate as the store keeps them for later documents
    (r"Geschlecht \(Anrede, Pseudonym\)", "Sex (salutation, pseudonym)"),
    (r"Geburtsjahr bzw\. Alter", "Year of birth or age"),
    (r"Geburtsjahr und Alter neben exakten Daten engen den Geburtstag ein",
     "Year of birth and age next to exact dates narrow down the birthday"),
    (r"Ort (?P<name>.+) \((?P<n>[\d.]+) Einwohner, (?P<k>\d+) gleichnamige, größte gezählt\)",
     "Place {name} ({n} inhabitants, {k} of that name, the largest counted)"),
    (r"Ort (?P<name>.+) \((?P<n>[\d.]+) Einwohner\)", "Place {name} ({n} inhabitants)"),
    (r"Seltene Erkrankung (?P<name>.+) \(ORPHA:(?P<o>\w+), Prävalenz (?P<c>.+), (?P<s>[^,]+)\)",
     "Rare disease {name} (ORPHA:{o}, prevalence {c}, {s})"),
))


def read_back(text: str) -> str:
    """English for German text that lost its English form, where the table knows it."""
    if text in _WORDS:
        return _WORDS[text]
    for pattern, template in _PATTERNS:
        m = pattern.fullmatch(text)
        if m:  # numbers of inhabitants with English separators
            groups = {k: v.replace(".", ",") if k in ("n", "k") else v
                      for k, v in m.groupdict().items()}
            return template.format(**groups)
    return text
