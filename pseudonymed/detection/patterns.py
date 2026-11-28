"""Generic patterns that identify a person regardless of who the patient is.

Two kinds: values that are identifying by their shape (insurance number, IBAN,
e-mail address) and values that follow a telling label ("Versicherten-Nr.",
"Pat.-ID", "Aktenzeichen").  The latter use a named group ``value`` so that only
the value, not the label, is replaced; they catch numbers that OCR misread.
"""

from __future__ import annotations

import re

from pseudonymed.core.model import Kind

INSURANCE_NUMBER = re.compile(r"(?<![A-Z0-9])[A-Z]\d{9}(?!\d)")
IBAN = re.compile(r"(?<![A-Z0-9])DE\d{2}(?:\s?\d{4}){4}\s?\d{2}(?![0-9])")
# OCR may put a space around dots and the @; a line break is never part of an address,
# and a full stop that ends the sentence is not part of it either.
# After a dot a space is only tolerated before a short lower-case segment ("example. org"),
# so that a sentence going on after the address ("….org. Arzt") is not swallowed.
EMAIL = re.compile(
    r"(?<![\w.+-])[\w+-]+(?:\.[ \t]?[\w+-]+)*[ \t]?@[ \t]?[\w-]+"
    r"(?:[ \t]?\.(?:[\w-]+|[ \t][a-z0-9-]{2,6}))+"
    r"(?![\w-])(?!\.[\w-])"
)

_SEP = r"\s*[:.]?\s*"
_START = r"(?<![A-Za-z0-9])"  # the label starts a word; "KAZ2Q4W" is not "AZ 2Q4W"
# Labels are matched case-insensitively (scoped flag); the values stay upper-case and
# digits, otherwise any word after "AZ" would pass as a file number.
_KVNR_LABEL = (r"(?i:Versicherten(?:-|\s)?(?:Nr|Nummer)\.?|Versichertennr\.?|KVNR|KV-?Nr\.?"
               r"|Vers\.?-?Nr\.?)")
_ID_LABEL = r"(?i:Pat(?:\.|ienten)?\s?-?\s?(?:ID|Nr|Nummer)\.?|Patientennummer)"
_CASE_NUMBER_LABEL = (r"(?:Aktenzeichen|Fall(?:-|\s)?(?:Nr|Nummer)\.?|Fallzahl|Az\.|AZ"
                      r"|Vorgangs(?:-|\s)?(?:Nr|Nummer)\.?)")
INSURANCE_NUMBER_WITH_LABEL = re.compile(
    _START + _KVNR_LABEL + _SEP + r"(?P<value>[A-Z0-9]{8,12})(?![A-Z0-9])"
)
PATIENT_ID_WITH_LABEL = re.compile(
    _START + _ID_LABEL + _SEP + r"(?P<value>\d[\d\s]{3,14}\d)(?!\d)"
)
CASE_NUMBER_WITH_LABEL = re.compile(
    _START + _CASE_NUMBER_LABEL + _SEP
    + r"(?P<value>(?=[A-Z0-9/\-.]*\d)[A-Z0-9][A-Z0-9/\-.]{3,24})(?![A-Z0-9/])"
)

GLOBAL_PATTERNS: list[tuple[str, re.Pattern[str], Kind]] = [
    ("pattern.insurance_number", INSURANCE_NUMBER, Kind.INSURANCE_NUMBER),
    ("pattern.iban", IBAN, Kind.IBAN),
    ("pattern.email", EMAIL, Kind.EMAIL),
    ("pattern.insurance_number.label", INSURANCE_NUMBER_WITH_LABEL, Kind.INSURANCE_NUMBER),
    ("pattern.patient_id.label", PATIENT_ID_WITH_LABEL, Kind.PATIENT_ID),
    ("pattern.case_number.label", CASE_NUMBER_WITH_LABEL, Kind.CASE_NUMBER),
]
