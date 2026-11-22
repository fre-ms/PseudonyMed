"""Pattern builders: what must match and, just as important, what must not."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from pseudonymed.detection import variants as v


def _matches(muster: str | None, text: str) -> bool:
    assert muster is not None
    return re.search(muster, text) is not None


@pytest.mark.parametrize(
    "text", ["Müller", "MÜLLER", "Mueller", "MUELLER", "Muller", "Müllers", "Herr Müller,"]
)
def test_name_variants(text: str) -> None:
    assert _matches(v.name_pattern("Müller"), text)


@pytest.mark.parametrize("text", ["müller", "Müllerstraße", "Schmüller", "Müllerin"])
def test_name_no_false_positives(text: str) -> None:
    assert not _matches(v.name_pattern("Müller"), text)


def test_name_too_short() -> None:
    assert v.name_pattern("M") is None


@pytest.mark.parametrize(
    "text",
    [
        "Max Mustermann",
        "Mustermann, Max",
        "Mustermann Max",
        "Max Peter Mustermann",
        "Max-Peter Mustermann",
        "M. Mustermann",
        "MAX MUSTERMANN",
        "Max Mustermanns Befund",
        "Max\nMustermann",
    ],
)
def test_full_name(text: str) -> None:
    assert _matches(v.full_name_pattern(["Max", "Peter"], "Mustermann"), text)


def test_full_name_not_part_of_a_word() -> None:
    assert not _matches(v.full_name_pattern(["Max"], "Mustermann"), "Maxi Mustermanninger")


def test_title_name_for_referrers() -> None:
    muster = v.title_name_pattern(["Anna"], "Schmidt")
    m = re.search(muster, "Überweisung durch Dr. med. Anna Schmidt vom")
    assert m and m.group(0) == "Dr. med. Anna Schmidt"
    m = re.search(muster, "Praxis Dr. Schmidt")
    assert m and m.group(0) == "Dr. Schmidt"


def test_surname_parts() -> None:
    assert v.surname_parts("Müller-Lüdenscheidt") == ["Müller", "Lüdenscheidt"]
    assert v.surname_parts("von der Leyen") == ["Leyen"]
    assert v.surname_parts("Meier") == []


@pytest.mark.parametrize(
    "text",
    [
        "01.02.1990",
        "1.2.1990",
        "01.02.90",
        "01. 02. 1990",
        "1990-02-01",
        "01/02/1990",
        "1. Februar 1990",
        "01. Feb. 1990",
        "1 FEBRUAR 1990",
    ],
)
def test_date_formats(text: str) -> None:
    assert _matches(v.date_pattern(dt.date(1990, 2, 1)), text)


@pytest.mark.parametrize(
    "text", ["11.02.1990", "21.02.1990", "01.02.1991", "01.03.1990", "1.2.19900"]
)
def test_date_no_false_positives(text: str) -> None:
    assert not _matches(v.date_pattern(dt.date(1990, 2, 1)), text)


@pytest.mark.parametrize(
    "text",
    ["089 1234567", "089/1234567", "089-123 45 67", "+49 89 1234567", "0049 (0)89 1234567",
     "+49 (0)89 123 45 67", "(089) 1234567"],
)
def test_phone(text: str) -> None:
    assert _matches(v.phone_pattern("+49 (0)89 1234567"), text)


def test_phone_other_number() -> None:
    assert not _matches(v.phone_pattern("089 1234567"), "089 1234568")
    assert v.phone_pattern("123") is None


@pytest.mark.parametrize(
    "text",
    ["Musterstraße 12", "Musterstr. 12", "Musterstrasse 12a", "MUSTERSTRASSE 12", "Musterstraße",
     "Muster-Straße 12", "Musterstraße, 12"],
)
def test_address(text: str) -> None:
    assert _matches(v.address_pattern("Musterstraße 12"), text)


def test_address_without_suffix_and_false_positives() -> None:
    assert _matches(v.address_pattern("Am Alten Markt 3"), "wohnhaft Am Alten Markt 3")
    assert _matches(v.address_pattern("Lindenweg 5"), "Lindenweg 5b")
    assert not _matches(v.address_pattern("Musterstraße 12"), "Mustermann 12")


@pytest.mark.parametrize("text", ["München", "Muenchen", "MÜNCHEN", "in München,"])
def test_place(text: str) -> None:
    assert _matches(v.place_pattern("München"), text)


@pytest.mark.parametrize("text", ["80331 München", "D-80331 München", "D 80331 Muenchen"])
def test_postcode_place(text: str) -> None:
    assert _matches(v.postcode_place_pattern("80331", "München"), text)


def test_place_no_false_positives() -> None:
    assert not _matches(v.place_pattern("München"), "Münchener Kindl")
    assert not _matches(v.postcode_place_pattern("80331", "München"), "80333 München")
    assert v.postcode_place_pattern("", "München") is None


def test_codes() -> None:
    assert _matches(v.exact_pattern("C815908301"), "Nr. c815908301,")
    assert _matches(v.exact_pattern("AZ 2023/171"), "Zeichen AZ  2023/171")
    assert _matches(v.exact_pattern("AZ 2023/171"), "68202938AZ 2023/171")  # glued by OCR
    assert _matches(v.exact_pattern("C815908301"), "C8I59O83O1")  # OCR confusions
    assert not _matches(v.exact_pattern("AZ 2023/171"), "BAZ 2023/171")
    assert not _matches(v.exact_pattern("C815908301"), "C8159083012")
    assert not _matches(v.exact_pattern("C815908301"), "XC815908301")
    assert _matches(v.number_pattern("68202938"), "ID 682O2938.")
    assert _matches(v.number_pattern("68202938"), "ID 68202938.")
    assert not _matches(v.number_pattern("68202938"), "168202938")
    assert v.number_pattern("12") is None


def test_email() -> None:
    assert _matches(v.email_pattern("Max.Mustermann@example.org"), "an max.mustermann@example.org!")
    assert not _matches(v.email_pattern("max@example.org"), "a.max@example.org")


def test_term() -> None:
    assert _matches(v.term_pattern("Praxis Beispiel"), "in der PRAXIS  Beispiel")
    assert not _matches(v.term_pattern("Beispiel"), "Beispiele")
