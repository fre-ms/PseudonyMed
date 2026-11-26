"""The vocabulary and the identifiers go to the stores of lexterm and lexident as packages."""

from __future__ import annotations

from pathlib import Path

import lexcor
import pytest
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.detection import handover, identifiers, vocabulary
from pseudonymed.detection import medical_terms as terms_module


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pseudonym database with a small vocabulary and a few identifier rows of the user."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "0")  # no bundled lexicon: the rows below only
    file = tmp_path / "pseudonyme.sqlite"
    with vocabulary.Vocabulary(file, seed=False) as words:
        words.add(["Bluthochdruck", "Morbus Parkinson"], vocabulary.CATEGORY_MEDICINE)
        words.add(["Babinski-Zeichen", "Ohr"], vocabulary.CATEGORY_USER)
    with identifiers.IdentifierTable(file) as rows:
        rows.add(["Waldklinik Tannengrund", "Sportverein Blau-Weiß"], identifiers.CATEGORY_OTHER)
        rows.add(["Waldklinik Tannengrund"], identifiers.CATEGORY_HOSPITAL)
        rows.add(["Dipl.-Forstwirt"], identifiers.CATEGORY_TITLE)
        rows.add(["Kunstschmiedin"], "Handwerk")
    terms_module.standard.cache_clear()
    return file


def test_everything_arrives_and_is_checked(database: Path, tmp_path: Path) -> None:
    report = handover.hand_over(tmp_path / "ablagen", database)
    assert report.vocabulary.store == tmp_path / "ablagen" / "lexterm.sqlite"
    assert report.vocabulary.kept is None and report.identifiers.kept is None
    assert report.vocabulary.packages == [(vocabulary.CATEGORY_MEDICINE, 2, 2),
                                          (vocabulary.CATEGORY_USER, 2, 2)]
    with lexcor.open_store(report.vocabulary.store, "lexterm") as store:
        free = {info.title: info.redistributable for info in store.packages()}
        assert free == {vocabulary.CATEGORY_MEDICINE: True, vocabulary.CATEGORY_USER: False}
        cover = lexcor.cover(store, heads=terms_module._EPONYM_HEADS)
        assert cover.is_term("Babinski") and cover.is_term("Bluthochdrucks")
        assert cover.spans("Morbus Parkinson, das Ohr") == [(0, 16), (22, 25)]
    with lexcor.open_store(report.identifiers.store, "lexident") as store:
        assert all(info.url.startswith("urn:uuid:") and not info.redistributable
                   for info in store.packages())
        assert sorted((e.term, e.cls) for e in store.entries()) == [
            ("Dipl.-Forstwirt", "NAME_TITLE"), ("Kunstschmiedin", "Handwerk"),
            ("Sportverein Blau-Weiß", "OTHER"), ("Waldklinik Tannengrund", "LOCATION_HOSPITAL"),
            ("Waldklinik Tannengrund", "OTHER")]
    with vocabulary.Vocabulary(database, seed=False) as words:  # the tables are as they were
        assert words.count() == 4
    with identifiers.IdentifierTable(database) as rows:
        assert rows.count() == 5


def test_a_second_run_replaces_and_keeps_the_store_as_it_was(database: Path,
                                                             tmp_path: Path) -> None:
    folder = tmp_path / "ablagen"
    handover.hand_over(folder, database)
    with vocabulary.Vocabulary(database, seed=False) as words:
        words.remove(["Ohr"])
        words.add(["Trömner-Reflex"])
    again = handover.hand_over(folder, database)
    assert again.vocabulary.kept is not None and again.vocabulary.kept.exists()
    assert again.vocabulary.kept.name.startswith("lexterm.sqlite.before-")
    with lexcor.open_store(again.vocabulary.store, "lexterm") as store:
        assert len(store.packages()) == 2  # the same lists again, not beside the old ones
        terms = {e.term for e in store.entries()}
        assert "Trömner-Reflex" in terms and "Ohr" not in terms
    with lexcor.open_store(again.vocabulary.kept, "lexterm") as before:
        assert "Ohr" in {e.term for e in before.entries()}


def test_what_does_not_agree_is_refused(database: Path, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    whole = handover.vocabulary_packages
    monkeypatch.setattr(handover, "vocabulary_packages", lambda database=None: whole(database)[:1])
    with pytest.raises(handover.HandoverError):
        handover.hand_over(tmp_path / "ablagen", database)


def test_the_command_lists_what_it_wrote(database: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    folder = tmp_path / "ablagen"
    declined = runner.invoke(app, ["datenbank", "auslagern", "--ziel", str(folder), "--speicher",
                                   str(database)], input="n\n")
    assert declined.exit_code == 1 and not folder.exists()
    r = runner.invoke(app, ["database", "hand-over", "--target", str(folder), "--store",
                            str(database), "--yes"])
    assert r.exit_code == 0, r.output
    assert "lexterm.sqlite" in r.output and "lexident.sqlite" in r.output
    assert vocabulary.CATEGORY_USER in r.output and "Geprüft" in r.output
