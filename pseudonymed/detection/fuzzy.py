"""Fuzzy token matching for OCR text.

OCR misreads single characters ("Jull" for "Juli", "Fiantz" for "Flantz"),
which defeats exact patterns.  For pages read by OCR every identifier is also
searched as a sequence of tokens with a small Levenshtein tolerance that grows
with the token length (1 for 4–6 characters, 2 up to 11, 3 up to 19, 4 beyond).
A spurious hit on a harmless word is cheap; a misread date of birth left
visible is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from ocrdoc.pagetext import PageText

from pseudonymed.core.model import Hit, Kind, Patient, Person, Role
from pseudonymed.detection.variants import without_accents
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

if TYPE_CHECKING:
    import datetime as dt

    from pseudonymed.detection.medical_terms import MedicalTerms

_MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"]
_EDGE = ",.;:()*\"'[]«»"
_TOKEN = re.compile(r"\S+")
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


@dataclass
class Target:
    """One token sequence to search for, with the pseudonym, kind, rule name and role
    that its hits carry."""

    tokens: list[str]
    pseudonym: str
    kind: Kind
    rule: str
    role: Role | None = None


def _core(token: str) -> str:
    return token.strip(_EDGE).casefold()


def _forms(token: str) -> set[str]:
    k = _core(token)
    return {k, k.translate(_TRANS), without_accents(k)} - {""}


def tolerance(token: str) -> int:
    """Allowed edits by token length: 0 below 4, 1 up to 6, 2 up to 11, 3 up to 19, else 4."""
    n = len(_core(token))
    if n < 4:
        return 0
    if n < 7:
        return 1
    if n < 12:
        return 2
    return 3 if n < 20 else 4


def distance(a: str, b: str, maximum: int) -> int:
    """Levenshtein distance, stopping early once it exceeds ``maximum``."""
    if abs(len(a) - len(b)) > maximum:
        return maximum + 1
    before = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        lowest = i
        for j, cb in enumerate(b, 1):
            value = min(before[j] + 1, current[j - 1] + 1, before[j - 1] + (ca != cb))
            current.append(value)
            lowest = min(lowest, value)
        if lowest > maximum:
            return maximum + 1
        before = current
    return before[-1]


def _matches(target_token: str, ocr_token: str) -> bool:
    t = tolerance(target_token)
    target_forms = _forms(target_token)
    ocr_forms = _forms(ocr_token)
    if not target_forms or not ocr_forms:
        return False
    return any(distance(z, o, t) <= t for z in target_forms for o in ocr_forms)


# -- targets ---------------------------------------------------------------------------------------
def _surnames(surname: str) -> list[str]:
    """The surname and its parts split at spaces and hyphens, each once and only from
    four characters on."""
    parts = re.split(r"[\s\-]+", surname)
    return [t for t in dict.fromkeys([surname, *parts]) if len(t) >= 4]


def _name_targets(prefix: str, person: Person, pseudonym: str, role: Role) -> list[Target]:
    """Targets for one person's names: first name with surname, the surname and its parts,
    every first name and the maiden name, each only from four characters on."""
    first_names = [v for v in person.first_names if len(v) >= 4]
    targets: list[Target] = []
    if first_names and person.surname:
        targets.append(Target([first_names[0], person.surname], pseudonym, Kind.NAME,
                              f"{prefix}.vollname", role))
    targets += [Target([t], pseudonym, Kind.SURNAME, f"{prefix}.nachname", role)
                for t in _surnames(person.surname)]
    targets += [Target([v], pseudonym, Kind.FIRST_NAME, f"{prefix}.vorname",
                       role) for v in first_names]
    if len(person.birth_name) >= 4:
        targets.append(Target([person.birth_name], pseudonym, Kind.BIRTH_NAME,
                              f"{prefix}.geburtsname", role))
    return targets


def _address_targets(prefix: str, person: Person, assignment: PseudonymAssignment,
                     role: Role) -> list[Target]:
    """Targets for one person's address: the whole street line and its first word, the
    town, and postcode with town; a street whose name is shorter than four characters
    is skipped."""
    targets: list[Target] = []
    tokens = person.street.split()
    if tokens and len(tokens[0]) >= 4:
        pseudonym = assignment.street(person)
        targets.append(Target(tokens, pseudonym, Kind.ADDRESS, f"{prefix}.strasse", role))
        targets.append(Target([tokens[0]], pseudonym, Kind.ADDRESS, f"{prefix}.strasse", role))
    if len(person.place) >= 4:
        targets.append(Target([person.place], assignment.place(person), Kind.PLACE, f"{prefix}.ort",
                              role))
        if person.postcode:
            targets.append(Target([person.postcode, person.place],
                                  assignment.postcode_place(person), Kind.PLACE,
                                  f"{prefix}.plz_ort", role))
    return targets


def _date_targets(date: dt.date, pseudonym: str) -> list[Target]:
    """The date of birth in the forms a document writes it: numeric with and without
    leading zeros or the century, and with the month written out or abbreviated."""
    month = _MONTHS[date.month - 1]
    day = f"{date.day}."
    # no "%-d": that strftime flag is a glibc/BSD extension and raises on Windows
    forms = [[date.strftime("%d.%m.%Y")], [f"{date.day}.{date.month}.{date.year}"],
             [date.strftime("%d.%m.%y")], [day, month, str(date.year)],
             [day, month[:3] + ".", str(date.year)]]
    return [Target(tokens, pseudonym, Kind.BIRTH_DATE, "datum.geburtsdatum", Role.PATIENT)
            for tokens in forms]


def _code_targets(patient: Patient, assignment: PseudonymAssignment) -> list[Target]:
    """Targets for the patient's identifiers: insurance number and patient id from six
    characters on, e-mail addresses from eight, and every file number split into tokens."""
    targets: list[Target] = []
    if len(patient.insurance_number) >= 6:
        targets.append(Target([patient.insurance_number], assignment.insurance_number(),
                              Kind.INSURANCE_NUMBER, "versichertennummer", Role.PATIENT))
    if len(patient.patient_id) >= 6:
        targets.append(Target([patient.patient_id], assignment.patient_id(), Kind.PATIENT_ID,
                              "patienten_id", Role.PATIENT))
    targets += [Target([e], assignment.email(), Kind.EMAIL, "email", Role.PATIENT)
                for e in patient.emails if len(e) >= 8]
    targets += [Target(a.split(), assignment.case_numbers(), Kind.CASE_NUMBER, "aktenzeichen",
                       Role.PATIENT) for a in patient.case_numbers]
    return targets


def targets_for(patient: Patient, assignment: PseudonymAssignment) -> list[Target]:
    """Token sequences worth a fuzzy search, with their pseudonyms."""
    targets = _name_targets("name", patient, assignment.main_person(), Role.PATIENT)
    targets += _address_targets("adresse", patient, assignment, Role.PATIENT)
    if patient.birth_date:
        targets += _date_targets(patient.birth_date, assignment.birth_date())
    targets += _code_targets(patient, assignment)
    for i, person in enumerate(patient.relatives, 1):
        targets += _name_targets(f"angehoerige{i}", person, assignment.relatives(i, person),
                                 Role.RELATIVE)
        targets += _address_targets(f"angehoerige{i}", person, assignment, Role.RELATIVE)
    for i, person in enumerate(patient.referrers, 1):
        targets += _name_targets(f"zuweiser{i}", person, assignment.referrers(i, person),
                                 Role.REFERRER)
        targets += _address_targets(f"zuweiser{i}", person, assignment, Role.REFERRER)
    return targets


# -- the search ------------------------------------------------------------------------------------
_CAPITALISED = (Kind.NAME, Kind.SURNAME, Kind.FIRST_NAME, Kind.BIRTH_NAME, Kind.PLACE, Kind.ADDRESS)
_Span = tuple[int, int]
_Candidate = tuple[int, int, Target]


class _Token(NamedTuple):
    """One whitespace-delimited token of the page with its character offsets."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _SearchRun:
    """What every step of one page's fuzzy search shares: the page, its tokens, the
    spans of protected medical terms and whether names must be capitalised."""

    page: PageText
    tokens: list[_Token]
    medical_terms: MedicalTerms | None
    protection: list[_Span]
    capitalisation: bool


def _tokens(text: str) -> list[_Token]:
    """The page split at whitespace, every token with its offsets."""
    return [_Token(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]


def _searchable(target: Target) -> bool:
    """A target can be searched only when every token keeps characters after trimming."""
    return bool(target.tokens) and all(_core(t) for t in target.tokens)


def _windows(tokens: list[_Token], target: Target) -> Iterator[_Span]:
    """Index ranges (first, last) of consecutive tokens matching the target token by token."""
    n = len(target.tokens)
    for i in range(len(tokens) - n + 1):
        if all(_matches(target.tokens[k], tokens[i + k].text) for k in range(n)):
            yield i, i + n - 1


def _on_one_line(page: PageText, pieces: list[_Token]) -> bool:
    """Do all pieces sit on the line of the first one?"""
    line = page.line_of(pieces[0].start)
    return all(page.line_of(t.start) == line for t in pieces[1:])


def _split_windows(page: PageText, tokens: list[_Token],
                   target_token: str) -> Iterator[_Span]:
    """Index ranges of two or three neighbouring tokens on one line whose concatenation
    matches a long target token: OCR splits such tokens ("gotthard. flantz@example. org")."""
    length = len(_core(target_token))
    for width in (2, 3):
        for i in range(len(tokens) - width + 1):
            pieces = tokens[i:i + width]
            if not _on_one_line(page, pieces):
                continue
            joined = "".join(t.text for t in pieces)
            # a whole neighbouring word glued on is not a split token
            if len(_core(joined)) <= length + 2 and _matches(target_token, joined):
                yield i, i + width - 1


def _all_windows(run: _SearchRun, target: Target) -> list[_Span]:
    """Every token window matching the target: token by token, and for a single long
    token also across the pieces OCR may have split it into."""
    window = list(_windows(run.tokens, target))
    if len(target.tokens) == 1 and len(_core(target.tokens[0])) >= 12:
        window += _split_windows(run.page, run.tokens, target.tokens[0])
    return window


def _without_edges(text: str, a: int, b: int) -> _Span:
    """The span ``text[a:b]`` shrunk past the punctuation glued to either end."""
    while a < b and text[a] in _EDGE:
        a += 1
    while b > a and text[b - 1] in _EDGE:
        b -= 1
    return a, b


def _capitalised_enough(target: Target, found: str, capitalisation: bool) -> bool:
    """With ``capitalisation`` a name or place must start with a capital letter, which
    keeps "lang" or "hoch" safe in correctly cased text; OCR text is not trusted that far."""
    if capitalisation and target.kind in _CAPITALISED:
        return found[:1].isupper()
    return True


def _is_protected_term(run: _SearchRun, a: int, b: int, target: Target) -> bool:
    """Is the inexact match a protected medical term ("Bauch" for a patient called
    Bauer)?  An exact spelling of the target is never protected."""
    if run.medical_terms is None:
        return False
    text = run.page.text
    if _core(text[a:b]) == _core(" ".join(target.tokens)):
        return False
    return run.medical_terms.is_protected(text, a, b, run.protection, whole=True)


def _candidates(run: _SearchRun, targets: list[Target]) -> list[_Candidate]:
    """Trimmed spans of every matching window that passes the capitalisation rule and
    the protected-term guard, in the order the targets and windows were searched."""
    candidates: list[_Candidate] = []
    text = run.page.text
    for target in filter(_searchable, targets):
        for i, j in _all_windows(run, target):
            a, b = _without_edges(text, run.tokens[i].start, run.tokens[j].end)
            if (_capitalised_enough(target, text[a:b], run.capitalisation)
                    and not _is_protected_term(run, a, b, target)):
                candidates.append((a, b, target))
    return candidates


def _without_overlap(candidates: list[_Candidate], occupied: list[_Span]) -> list[_Candidate]:
    """Longest candidates first, each kept only where neither an exact hit nor an earlier
    candidate lies; ``occupied`` grows with every kept span."""
    selected: list[_Candidate] = []
    for a, b, target in sorted(candidates, key=lambda k: (-(k[1] - k[0]), k[0])):
        if any(a < y and b > x for x, y in occupied):
            continue
        occupied.append((a, b))
        selected.append((a, b, target))
    return selected


def _hit(page: PageText, a: int, b: int, target: Target, source: str) -> Hit:
    """The hit for one span, with its boxes and font size read from the page."""
    return Hit(
        page=page.number, start=a, end=b, text=page.text[a:b], kind=target.kind,
        rule=f"unscharf.{target.rule}", pseudonym=target.pseudonym,
        rects=[tuple(r) for r in page.rects(a, b)], font_size=page.font_size(a, b),
        role=target.role, source=source,
    )


def find_fuzzy(
    page: PageText, targets: list[Target], existing: list[Hit],
    medical_terms: MedicalTerms | None = None, capitalisation: bool = False,
    source: str = "ocr",
) -> list[Hit]:
    """Fuzzy hits on an OCR page (or a transcript) that do not overlap exact hits.

    An inexact match whose text is a medical term ("Bauch" for a patient
    called Bauer) is not a hit; exact spellings still are.  With
    ``capitalisation`` names and places must start with a capital letter,
    which keeps "lang" or "hoch" safe in correctly cased text (OCR text is
    not trusted that far).
    """
    protection = medical_terms.spans(page.text) if medical_terms is not None else []
    run = _SearchRun(page, _tokens(page.text), medical_terms, protection, capitalisation)
    occupied = [(f.start, f.end) for f in existing if f.end > f.start]
    selected = _without_overlap(_candidates(run, targets), occupied)
    matches = [_hit(page, a, b, target, source) for a, b, target in selected]
    matches.sort(key=lambda f: f.start)
    return matches
