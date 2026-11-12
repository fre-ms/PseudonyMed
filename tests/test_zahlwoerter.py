"""German number words: cardinals, ordinals, runs, dates; offsets are preserved."""

from __future__ import annotations

import pytest

from pseudonymed.erkennung.zahlwoerter import (
    _datumsjahr,
    _jahrespaar,
    _Kopie,
    _tokens,
    _ziffernlauf,
    kardinal,
    normalisiere,
    ordinal,
)


@pytest.mark.parametrize("wort, wert", [
    ("null", 0), ("eins", 1), ("zwo", 2), ("zwölf", 12), ("einundzwanzig", 21),
    ("dreißig", 30), ("dreissig", 30), ("hundert", 100), ("einhundertfünf", 105),
    ("neunzehnhundertneunundfünfzig", 1959), ("zweitausend", 2000),
    ("zweitausendsechsundzwanzig", 2026), ("Siebenhundert", 700), ("tausend", 1000),
    ("ein", None), ("eine", None), ("einsundzwanzig", None), ("dreist", None),
    ("Zwanziger", None), ("Tabletten", None), ("hunderte", None), ("elfen", None),
])
def test_kardinal(wort: str, wert: int | None) -> None:
    assert kardinal(wort) == wert


@pytest.mark.parametrize("wort, wert", [
    ("erste", 1), ("Ersten", 1), ("zweite", 2), ("dritten", 3), ("siebte", 7), ("siebente", 7),
    ("achte", 8), ("achten", 8), ("neunte", 9), ("elfte", 11), ("zwanzigste", 20),
    ("einundzwanzigsten", 21), ("dreißigster", 30), ("hundertste", 100),
    ("liste", None), ("beste", None), ("bitte", None), ("heute", None), ("letzte", None),
    ("Minute", None), ("Tante", None), ("Mitte", None), ("eine", None), ("Rente", None),
])
def test_ordinal(wort: str, wert: int | None) -> None:
    assert ordinal(wort) == wert


@pytest.mark.parametrize("text, erwartet", [
    ("Geboren am einundzwanzigsten Juli neunzehnhundertneunundfünfzig in Riesa.",
     "Geboren am 21. Juli 1959 in Riesa."),
    ("am einundzwanzigsten siebten neunundfünfzig", "am 21.7.59"),
    ("am einundzwanzigsten siebten neunzehn neunundfünfzig", "am 21.7.1959"),
    ("am einundzwanzigsten Juli 1959", "am 21. Juli 1959"),
    ("Postleitzahl acht zwei vier sechs zwei, Sieringgasse zweiundvierzig.",
     "Postleitzahl 82462 , Sieringgasse 42 ."),
    ("Jahrgang neunzehn neunundfünfzig", "Jahrgang 1959"),
    ("Ich nehme zwei Tabletten, ein Jahr lang.", "Ich nehme 2 Tabletten, ein Jahr lang."),
    ("zum ersten Mal, achte darauf", "zum 1. Mal, 8. darauf"),
    ("Telefon null acht zwei eins neun", "Telefon 08219"),
    ("Am 21. Juli 1959.", "Am 21. Juli 1959."),
])
def test_normalisiere(text: str, erwartet: str) -> None:
    n = normalisiere(text)
    assert len(n.text) == len(text)
    assert " ".join(n.text.split()) == erwartet
    assert all(0 <= a < b <= len(text) for a, b in n.spannen)


def test_erweitern_und_zeilen() -> None:
    text = "Tag: einundzwanzigster\nJuli neunzehnhundertneunundfünfzig"
    n = normalisiere(text)
    assert n.text.count("\n") == 1 and len(n.text) == len(text)
    # the line break stops the date group: day alone, year alone
    assert " ".join(n.text.split()) == "Tag: 21. Juli 1959"
    a = text.index("neunzehn")
    assert n.erweitere(a, a + 4) == (a, len(text))


@pytest.mark.parametrize("text, erwartet", [
    ("neunzehn neunundfünfzig", "1959"),
    ("zwanzig zehn", "2010"),
    ("zehn\tneunundneunzig", "1099"),
    ("neunzehn neun", None),  # the second word needs two digits
    ("neunzehn hundert", None),  # and must stay below 100
    ("einundzwanzig fünfzig", None),  # the first word runs from 10 to 20
    ("neunzehn\nneunundfünfzig", None),  # a line break ends the pair
    ("neunzehn, neunundfünfzig", None),
])
def test_jahrespaar(text: str, erwartet: str | None) -> None:
    tokens = _tokens(text)
    paar = _jahrespaar(tokens[0], tokens[1], text)
    assert (None if paar is None else paar.ziffern) == erwartet
    assert _jahrespaar(tokens[0], None, text) is None


@pytest.mark.parametrize("text, erwartet", [
    ("acht zwei vier sechs zwei", (5, "82462")),
    ("null acht", (2, "08")),
    ("acht", None),  # one word is no run
    ("acht zwölf", None),  # only single digits join
    ("zwölf acht", None),
    ("acht\nzwei", None),  # a line break ends the run
    ("acht zwei vier, sechs", (3, "824")),
])
def test_ziffernlauf(text: str, erwartet: tuple[int, str] | None) -> None:
    tokens = _tokens(text)
    lauf = _ziffernlauf(tokens, 0, text)
    assert (None if lauf is None else (lauf.anzahl, lauf.ziffern)) == erwartet


@pytest.mark.parametrize("text, erwartet", [
    ("Juli 1959", (1, "1959")),
    ("Juli 59", (1, "59")),
    ("Juli 195", None),  # digits count only as two or four
    ("Juli 19590", None),
    ("Juli neunzehn neunundfünfzig", (2, "1959")),  # the pair beats the single word
    ("Juli neunzehnhundertneunundfünfzig", (1, "1959")),
    ("Juli neunundfünfzig", (1, "59")),
    ("Juli neunzehn", (1, "19")),
    ("Juli dreitausend", None),
    ("Juli\n1959", None),  # a line break ends the date
    ("Juli", None),
])
def test_datumsjahr(text: str, erwartet: tuple[int, str] | None) -> None:
    tokens = _tokens(text)
    jahr = _datumsjahr(tokens, 1, text)
    assert (None if jahr is None else (jahr.anzahl, jahr.ziffern)) == erwartet


def test_kopie_schreibt_laengengleich() -> None:
    text = "ab\ncd"
    kopie = _Kopie(text)
    kopie.schreibe(0, 5, "12")
    assert kopie.ergebnis().text == "12\n  " and kopie.spannen == [(0, 5)]
    kopie.schreibe(3, 5, "123")  # too long for the region: dropped, offsets stay intact
    assert kopie.ergebnis().text == "12\n  " and kopie.spannen == [(0, 5)]
