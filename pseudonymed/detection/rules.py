"""Deterministic rules built from a patient's identifiers, and the matcher.

Matching is longest-match-first across all rules, so "Max Mustermann" wins
over the separate surname and first-name rules and yields one pseudonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from optdoc.pagetext import Box, PageText

from pseudonymed.core.model import Hit, Kind, Patient, Person, Role
from pseudonymed.detection import variants as v
from pseudonymed.detection.patterns import GLOBAL_PATTERNS
from pseudonymed.pseudonyms.assignment import PseudonymAssignment


@dataclass
class Rule:
    """One compiled pattern with the pseudonym it yields and its priority.

    Among candidates of equal length the higher priority wins.  ``role`` says
    whose identifier the rule stands for (patient, relative, referrer) and is
    None for the user's own terms and the global patterns.
    """

    id: str
    muster: re.Pattern[str]
    kind: Kind
    pseudonym: str
    priority: int = 0
    role: Role | None = None


RuleMatch = tuple[int, int, Rule]


# -- the rule families ----------------------------------------------------------------------------
def _rule(id_: str, muster: str | None, kind: Kind, pseudonym: str, priority: int,
          role: Role | None) -> list[Rule]:
    """The compiled rule as a one-element list, empty when the pattern builder refused
    the input as too short to be safe.  The pseudonym is handed in ready-made, so the
    assignment has recorded it even when no rule results."""
    if not muster:
        return []
    return [Rule(id_, re.compile(muster), kind, pseudonym, priority, role)]


def _name_rules(prefix: str, person: Person, pseudonym: str, role: Role) -> list[Rule]:
    """Full name, surname, the surname shortened to its initial after a form of address,
    parts of a compound surname, first names and the birth name."""
    rules = _rule(f"{prefix}.vollname", v.full_name_pattern(person.first_names, person.surname),
                  Kind.NAME, pseudonym, 30, role)
    rules += _rule(f"{prefix}.nachname", v.name_pattern(person.surname), Kind.SURNAME,
                   pseudonym, 20, role)
    rules += _rule(f"{prefix}.nachname.initial", v.surname_initial_pattern(person.surname),
                   Kind.SURNAME, pseudonym, 5, role)
    for part in v.surname_parts(person.surname):
        rules += _rule(f"{prefix}.nachname.teil", v.name_pattern(part), Kind.SURNAME, pseudonym,
                       10, role)
    for first_name in person.first_names:
        if len(first_name) >= 3:
            rules += _rule(f"{prefix}.vorname", v.name_pattern(first_name), Kind.FIRST_NAME,
                           pseudonym,
                           10, role)
    if person.birth_name:
        rules += _rule(f"{prefix}.geburtsname.vollname",
                       v.full_name_pattern(person.first_names, person.birth_name), Kind.NAME,
                       pseudonym, 30, role)
        rules += _rule(f"{prefix}.geburtsname", v.name_pattern(person.birth_name),
                       Kind.BIRTH_NAME, pseudonym, 20, role)
    return rules


def _birth_date_rule(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """The birth date in every common spelling; nothing when the date is unknown."""
    if not patient.birth_date:
        return []
    return _rule("datum.geburtsdatum", v.date_pattern(patient.birth_date), Kind.BIRTH_DATE,
                 assignment.birth_date(), 20, Role.PATIENT)


def _address_rules(prefix: str, person: Person, role: Role,
                   assignment: PseudonymAssignment) -> list[Rule]:
    """Street, postcode with town, and the town on its own, each with its own code."""
    rules: list[Rule] = []
    if person.street:
        rules += _rule(f"{prefix}.strasse", v.address_pattern(person.street), Kind.ADDRESS,
                       assignment.street(person), 20, role)
    if person.place:
        rules += _rule(f"{prefix}.plz_ort", v.postcode_place_pattern(person.postcode, person.place),
                       Kind.PLACE,
                       assignment.postcode_place(person), 20, role)
        rules += _rule(f"{prefix}.ort", v.place_pattern(person.place), Kind.PLACE,
                       assignment.place(person),
                       15, role)
    return rules


def _number_rules(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """The patient's phone numbers, e-mail addresses, insurance number, patient id and
    file numbers."""
    rules: list[Rule] = []
    for t in patient.phones:
        rules += _rule("telefon", v.phone_pattern(t), Kind.PHONE, assignment.phone(), 20,
                       Role.PATIENT)
    for e in patient.emails:
        rules += _rule("email", v.email_pattern(e), Kind.EMAIL, assignment.email(), 20,
                       Role.PATIENT)
    if patient.insurance_number:
        rules += _rule("versichertennummer", v.exact_pattern(patient.insurance_number),
                       Kind.INSURANCE_NUMBER, assignment.insurance_number(), 25, Role.PATIENT)
    if patient.patient_id:
        rules += _rule("patienten_id", v.number_pattern(patient.patient_id), Kind.PATIENT_ID,
                       assignment.patient_id(), 25, Role.PATIENT)
    for a in patient.case_numbers:
        rules += _rule("aktenzeichen", v.exact_pattern(a), Kind.CASE_NUMBER,
                       assignment.case_numbers(), 25, Role.PATIENT)
    return rules


def _contact_rules(prefix: str, person: Person, role: Role) -> list[Rule]:
    """Phone numbers and e-mail addresses of a relative or referrer; they share one
    placeholder each and get no code of their own."""
    rules: list[Rule] = []
    for t in person.phones:
        rules += _rule(f"{prefix}.telefon", v.phone_pattern(t), Kind.PHONE, "[Telefon]", 20,
                       role)
    for e in person.emails:
        rules += _rule(f"{prefix}.email", v.email_pattern(e), Kind.EMAIL, "[E-Mail]", 20, role)
    return rules


def _relative_rules(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """Names, address and contact details of every relative, numbered in order."""
    rules: list[Rule] = []
    for i, person in enumerate(patient.relatives, 1):
        pseudonym = assignment.relatives(i, person)
        prefix = f"angehoerige{i}"
        rules += _name_rules(prefix, person, pseudonym, Role.RELATIVE)
        rules += _address_rules(prefix, person, Role.RELATIVE, assignment)
        rules += _contact_rules(prefix, person, Role.RELATIVE)
    return rules


def _referrer_rules(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """Referring physicians: the name with its academic titles (so that "Dr. Z1" does not
    end up as "Dr. Dr. Z1"), parts of a compound surname, address and contact details."""
    rules: list[Rule] = []
    for i, person in enumerate(patient.referrers, 1):
        pseudonym = assignment.referrers(i, person)
        prefix = f"zuweiser{i}"
        rules += _rule(f"{prefix}.name", v.title_name_pattern(person.first_names, person.surname),
                       Kind.NAME, pseudonym, 30, Role.REFERRER)
        for part in v.surname_parts(person.surname):
            rules += _rule(f"{prefix}.nachname.teil", v.name_pattern(part), Kind.SURNAME,
                           pseudonym, 10, Role.REFERRER)
        rules += _address_rules(prefix, person, Role.REFERRER, assignment)
        rules += _contact_rules(prefix, person, Role.REFERRER)
    return rules


def _term_rules(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """The user's own terms (employer, nursing service, …), numbered in order."""
    rules: list[Rule] = []
    for i, b in enumerate(patient.terms, 1):
        rules += _rule("begriff", v.term_pattern(b), Kind.TERM, assignment.term(i, b), 15,
                       None)
    return rules


def _global_rules(assignment: PseudonymAssignment) -> list[Rule]:
    """The patient-independent patterns (insurance number, IBAN, e-mail, labelled values)
    with the lowest priority, so that a patient rule wins at equal length."""
    return [Rule(id_, muster, kind, assignment.generic(kind), 5, None)
            for id_, muster, kind in GLOBAL_PATTERNS]


def rules_for(patient: Patient, assignment: PseudonymAssignment) -> list[Rule]:
    """Compile the rule set for one patient; pseudonyms come from ``assignment``.

    The families keep this order: at equal length and priority the earlier rule
    wins (see :func:`find_in_text`), and the assignment records the pseudonyms
    in the order they are asked for.
    """
    main_person = assignment.main_person()
    return [
        *_name_rules("name", patient, main_person, Role.PATIENT),
        *_birth_date_rule(patient, assignment),
        *_address_rules("adresse", patient, Role.PATIENT, assignment),
        *_number_rules(patient, assignment),
        *_relative_rules(patient, assignment),
        *_referrer_rules(patient, assignment),
        *_term_rules(patient, assignment),
        *_global_rules(assignment),
    ]


# -- matching -------------------------------------------------------------------------------------
def _match_span(text: str, rule: Rule, m: re.Match[str]) -> tuple[int, int]:
    """The span a match stands for: the ``value`` group of a labelled pattern (the value
    behind "KVNR", not the label), otherwise the whole match; trimmed of the whitespace
    a pattern may have swallowed at either end."""
    if "wert" in rule.muster.groupindex and m.group("wert"):
        a, b = m.span("wert")
    else:
        a, b = m.start(), m.end()
    part = text[a:b]
    a += len(part) - len(part.lstrip())
    return a, a + len(part.strip())


def _candidates(text: str, rules: list[Rule]) -> list[RuleMatch]:
    """Every non-empty match of every rule, in rule order; overlaps not yet resolved."""
    candidates: list[RuleMatch] = []
    for rule in rules:
        for m in rule.muster.finditer(text):
            a, b = _match_span(text, rule, m)
            if b > a:
                candidates.append((a, b, rule))
    return candidates


def _without_overlap(candidates: list[RuleMatch]) -> list[RuleMatch]:
    """Greedy choice in text order: the longest candidate first, at equal length the
    higher priority, then the earlier one; whatever overlaps a chosen hit is dropped."""
    occupied: list[tuple[int, int]] = []
    selected: list[RuleMatch] = []
    for a, b, rule in sorted(candidates, key=lambda k: (-(k[1] - k[0]), -k[2].priority, k[0])):
        if any(a < y and b > x for x, y in occupied):
            continue
        occupied.append((a, b))
        selected.append((a, b, rule))
    return sorted(selected, key=lambda k: k[0])


def find_in_text(text: str, rules: list[Rule]) -> list[tuple[int, int, Rule]]:
    """Non-overlapping matches, longest first, then by rule priority."""
    return _without_overlap(_candidates(text, rules))


def merge(matches: list[tuple[int, int, Rule]], text: str) -> list[tuple[int, int, Rule]]:
    """Join neighbouring hits with the same pseudonym ("Street 1, 12345 Town" -> one
    "[Wohnort]") when only commas or whitespace lie between them."""
    result: list[tuple[int, int, Rule]] = []
    for a, b, rule in matches:
        if result:
            pa, pb, prev_rule = result[-1]
            between = text[pb:a]
            if (
                prev_rule.pseudonym == rule.pseudonym
                and "\n" not in between
                and re.fullmatch(r"[\s,]*", between)
            ):
                result[-1] = (pa, b, prev_rule if prev_rule.priority >= rule.priority else rule)
                continue
        result.append((a, b, rule))
    return result


def find(page: PageText, rules: list[Rule]) -> list[Hit]:
    """Run all rules over one page and return hits with rectangles."""
    hits: list[Hit] = []
    for a, b, rule in merge(find_in_text(page.text, rules), page.text):
        hits.append(
            Hit(
                page=page.number,
                start=a,
                end=b,
                text=page.text[a:b],
                kind=rule.kind,
                rule=rule.id,
                pseudonym=rule.pseudonym,
                rects=[tuple(r) for r in page.rects(a, b)],
                font_size=page.font_size(a, b),
                role=rule.role,
            )
        )
    return hits


# -- boxes drawn by the user ----------------------------------------------------------------------
def _as_box(rect: tuple[float, float, float, float]) -> Box:
    """The rectangle as a plain four-tuple, whatever sequence the caller handed in."""
    x0, y0, x1, y1 = rect
    return (x0, y0, x1, y1)


def _covers(box: Box, glyph_box: Box) -> bool:
    """Whether the drawn box covers at least two fifths of the glyph box, so that a box
    drawn a little below the line's centre still takes the line."""
    x0, y0, x1, y1 = box
    width = min(x1, glyph_box[2]) - max(x0, glyph_box[0])
    height = min(y1, glyph_box[3]) - max(y0, glyph_box[1])
    if width <= 0 or height <= 0:
        return False
    area = max((glyph_box[2] - glyph_box[0]) * (glyph_box[3] - glyph_box[1]), 1e-6)
    return width * height >= 0.4 * area


def _covered_chars(page: PageText, box: Box) -> list[int]:
    """Indices of the printed characters the drawn box covers."""
    return [i for i, glyph_box in enumerate(page.boxes)
            if glyph_box is not None and _covers(box, glyph_box)]


def _continues(page: PageText, start: int, before: int, i: int) -> bool:
    """Whether character ``i`` continues the run begun at ``start``: the same line, and
    nothing but whitespace between the previous covered character and this one."""
    return page.lines[i] == page.lines[start] and all(
        page.text[k].isspace() for k in range(before + 1, i))


def _line_runs(page: PageText, inside: list[int]) -> list[tuple[int, int]]:
    """The covered characters as ``(start, ende)`` runs, one per line of the box."""
    runs: list[tuple[int, int]] = []
    start = before = inside[0]
    for i in inside[1:]:
        if not _continues(page, start, before, i):
            runs.append((start, before + 1))
            start = i
        before = i
    runs.append((start, before + 1))
    return runs


def _manual_match(page: PageText, start: int, end: int, pseudonym: str,
                  source: str) -> Hit:
    """The hit for one run of covered characters, with its rectangles and font size."""
    return Hit(
        page=page.number, start=start, end=end, text=page.text[start:end],
        kind=Kind.MANUAL, rule=source, pseudonym=pseudonym,
        rects=[tuple(r) for r in page.rects(start, end)],
        font_size=page.font_size(start, end), source=source,
    )


def _box_without_text(page: PageText, box: Box, pseudonym: str, source: str) -> Hit:
    """The hit for a box over an image page: no text span, the rectangle only."""
    return Hit(page=page.number, start=0, end=0, text="", kind=Kind.MANUAL,
               rule=source, pseudonym=pseudonym, rects=[box], font_size=9.0,
               source=source)


def box_to_hits(
    page: PageText, rect: tuple[float, float, float, float], pseudonym: str = "[geschwärzt]",
    source: str = "manuell",
) -> list[Hit]:
    """A box drawn by the user becomes hits with text spans where the page has text.

    Characters that the box covers (at least two fifths of the glyph box, so a
    box drawn a little below the line's centre still takes the line) are grouped
    per line into one hit each, so that the text file, the layout check and the
    re-check see the same thing as the redaction.  Without characters (image
    page) the box is a hit of its own with the rectangle only.
    """
    box = _as_box(rect)
    inside = _covered_chars(page, box)
    if not inside:
        return [_box_without_text(page, box, pseudonym, source)]
    matches = [_manual_match(page, start, end, pseudonym, source)
               for start, end in _line_runs(page, inside)]
    # the drawn box itself is covered too, for ink that is not text (image, stamp)
    matches[0].rects = matches[0].rects + [box]
    return matches
