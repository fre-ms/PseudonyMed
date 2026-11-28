"""The English names of classes and lists, their labels and their German equivalents."""

from __future__ import annotations

from pseudonymed.core import names


def test_label_in_either_language() -> None:
    assert names.label("hospital", "de") == "Klinik"
    assert names.label("hospital", "en_GB") == "Hospital"
    assert names.label("body parts", "de_DE") == "Körperteile"
    assert names.label("academic", "en") == "academic"


def test_a_name_of_the_user_stays_as_typed() -> None:
    assert names.label("Sportverein", "de") == "Sportverein"
    assert names.class_name("  Sport  verein ") == "Sport verein"


def test_the_title_of_a_list_of_the_user() -> None:
    assert names.label("user: hospital", "de") == "eigene: Klinik"
    assert names.label("user: Sportverein", "en") == "own: Sportverein"


def test_german_and_english_labels_name_the_stored_class() -> None:
    for typed in ("Klinik", "klinik", "Hospital", "hospital"):
        assert names.class_name(typed) == "hospital"
    assert names.class_name("Sonstiges") == "other"
    assert names.class_name("Laborwert") == "lab_test"


def test_lists_and_vocabulary_categories() -> None:
    assert names.list_name("Berufe") == "occupations"
    assert names.list_name("Eponyme") == "eponyms"
    assert names.category_name("Körperteile") == "body parts"
    assert names.category_name("Nutzervokabular") == "user vocabulary"
    # a vocabulary category is never taken for a list of identifiers
    assert names.category_name("Titel") == "Titel"
    assert names.list_name("Titel") == "titles"


def test_every_name_has_two_labels() -> None:
    for table in (names.CLASSES, names.LISTS, names.TITLE_KINDS):
        for name, (german, english) in table.items():
            assert name and german and english
    assert set(names.VOCABULARY) <= set(names.LISTS)
