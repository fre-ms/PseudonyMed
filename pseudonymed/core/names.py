"""The English names PseudonyMed stores for classes and lists, and their labels.

The classes of identifiers and of medical terms, the kinds of titles and the names of the
bundled lists are stored in English (``hospital``, ``body parts``).  This table gives each
its label in the interface language (:func:`label`) and understands the German name where
a person types one: in an imported file, on the command line, in the class box
(:func:`class_name`, :func:`list_name`).  A name of the user's own is neither: it is shown
and kept as typed.  The words in pseudonyms ("[Klinik …]") are no labels: they are in the
language of the document (:mod:`pseudonymed.detection.identifiers`).
"""

from __future__ import annotations

# English name: (German label, English label)
CLASSES = {
    # identifiers
    "hospital": ("Klinik", "Hospital"),
    "occupation": ("Beruf", "Occupation"),
    "study": ("Studie", "Study"),
    "title": ("Titel", "Title"),
    "other": ("Sonstiges", "Other"),
    "employment_status": ("Erwerbsstatus", "Employment status"),
    # medical terms
    "anatomy": ("Anatomie", "Anatomy"),
    "drug": ("Medikament", "Drug"),
    "disease": ("Erkrankung", "Disease"),
    "symptom": ("Symptom", "Symptom"),
    "substance": ("Substanz", "Substance"),
    "procedure": ("Verfahren", "Procedure"),
    "psyche": ("Psyche", "Psyche"),
    "physiology": ("Physiologie", "Physiology"),
    "organism": ("Organismus", "Organism"),
    "lab_test": ("Laborwert", "Lab test"),
    "finding": ("Befund", "Finding"),
    "eponym": ("Eponym", "Eponym"),
}
LISTS = {
    "wikidata": ("Wikidata", "Wikidata"),
    "medicine (Wikidata)": ("Medizin (Wikidata)", "Medicine (Wikidata)"),
    "body parts": ("Körperteile", "Body parts"),
    "findings terms": ("Befundbegriffe", "Findings terms"),
    "lab tests": ("Laborwerte", "Lab tests"),
    "eponyms": ("Eponyme", "Eponyms"),
    "occupations": ("Berufe", "Occupations"),
    "titles": ("Titel", "Titles"),
    "hospitals": ("Kliniken", "Hospitals"),
    "studies": ("Studien", "Studies"),
    "hospital towns": ("Klinikorte", "Hospital towns"),
    "hospitals AT (BMASGPK)": ("Kliniken AT (BMASGPK)", "Hospitals AT (BMASGPK)"),
    "user vocabulary": ("Nutzervokabular", "User vocabulary"),
    "user": ("eigene", "own"),
}
# the categories of the medical vocabulary among the lists (lexterm's store)
VOCABULARY = ("medicine (Wikidata)", "body parts", "findings terms", "eponyms", "user vocabulary")
TITLE_KINDS = {
    "academic": ("akademisch", "academic"),
    "office": ("Amt", "office"),
    "nobility": ("Adel", "nobility"),
    "honorary": ("Ehrentitel", "honorary"),
    "religious": ("kirchlich", "religious"),
    "all": ("alle", "all"),
}
_LABELS = {**TITLE_KINDS, **LISTS, **CLASSES}
USER_PREFIX = "user: "  # the title of a list of the user's own: "user: hospital"


def label(name: str, language: str) -> str:
    """The label of a stored name in the interface language; a name of the user's own (and
    the class in the title of a list of the user's own) as it is."""
    if name.startswith(USER_PREFIX):
        return f"{label('user', language)}: {label(name[len(USER_PREFIX):], language)}"
    labels = _LABELS.get(name)
    if labels is None:
        return name
    return labels[0] if language.startswith("de") else labels[1]


def _english(typed: str, table: dict[str, tuple[str, str]]) -> str:
    """The English name of a typed name: itself, its German or English label (any case);
    a name the table does not know as typed, its whitespace collapsed."""
    text = " ".join(typed.split())
    wanted = text.casefold()
    for name, (german, english) in table.items():
        if wanted in (name.casefold(), german.casefold(), english.casefold()):
            return name
    return text


def class_name(typed: str) -> str:
    """The class a person means: ``Klinik`` and ``Hospital`` are ``hospital``."""
    return _english(typed, CLASSES)


def list_name(typed: str) -> str:
    """The list a person means: ``Körperteile`` and ``Body parts`` are ``body parts``."""
    return _english(typed, LISTS)


def category_name(typed: str) -> str:
    """The category of the medical vocabulary a person means: ``Körperteile`` is
    ``body parts``; only the vocabulary's own categories are known here."""
    return _english(typed, {name: LISTS[name] for name in VOCABULARY})
