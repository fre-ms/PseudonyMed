"""The identifiers go to the store of lexident as packages."""

from __future__ import annotations

from pathlib import Path

import lexcor
import pytest
from typer.testing import CliRunner

from pseudonymed import environment
from pseudonymed.cli import app
from pseudonymed.detection import handover, identifiers


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pseudonym database with a few identifier rows of the user."""
    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "0")  # no bundled lexicon: the rows below only
    file = tmp_path / "pseudonyme.sqlite"
    with identifiers.IdentifierTable(file) as rows:
        rows.add(["Waldklinik Tannengrund", "Sportverein Blau-Weiß"], identifiers.CATEGORY_OTHER)
        rows.add(["Waldklinik Tannengrund"], identifiers.CATEGORY_HOSPITAL)
        rows.add(["Dipl.-Forstwirt"], identifiers.CATEGORY_TITLE)
        rows.add(["Kunstschmiedin"], "Handwerk")
    return file


def test_everything_arrives_and_is_checked(database: Path, tmp_path: Path) -> None:
    written = handover.hand_over(tmp_path / "ablagen", database)
    assert written.store == tmp_path / "ablagen" / "lexident.sqlite" and written.kept is None
    assert not (tmp_path / "ablagen" / "lexterm.sqlite").exists()  # the identifiers only
    with lexcor.open_store(written.store, "lexident") as store:
        assert all(info.url.startswith("urn:uuid:") and not info.redistributable
                   for info in store.packages())
        assert sorted((e.term, e.cls) for e in store.entries()) == [
            ("Dipl.-Forstwirt", "NAME_TITLE"), ("Kunstschmiedin", "Handwerk"),
            ("Sportverein Blau-Weiß", "OTHER"), ("Waldklinik Tannengrund", "LOCATION_HOSPITAL"),
            ("Waldklinik Tannengrund", "OTHER")]
    with identifiers.IdentifierTable(database) as rows:  # the table is as it was
        assert rows.count() == 5


def test_a_second_run_replaces_and_keeps_the_store_as_it_was(database: Path,
                                                             tmp_path: Path) -> None:
    folder = tmp_path / "ablagen"
    first = handover.hand_over(folder, database)
    with identifiers.IdentifierTable(database) as rows:
        rows.add(["Flantzwerke GmbH"])
    again = handover.hand_over(folder, database)
    assert again.kept is not None and again.kept.exists()
    assert again.kept.name.startswith("lexident.sqlite.before-")
    with lexcor.open_store(again.store, "lexident") as store:
        assert "Flantzwerke GmbH" in {e.term for e in store.entries()}
        assert len(store.packages()) == len(first.packages)  # the same lists, not beside them
    with lexcor.open_store(again.kept, "lexident") as before:
        assert "Flantzwerke GmbH" not in {e.term for e in before.entries()}


def test_what_does_not_agree_is_refused(database: Path, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    whole = handover._write

    def lossy(file: Path, packages: list[lexcor.Package]) -> tuple[handover.Written, list[str]]:
        return whole(file, packages[:1])

    monkeypatch.setattr(handover, "_write", lossy)
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
    assert "lexident.sqlite" in r.output and "lexterm.sqlite" not in r.output
    assert "Handwerk" in r.output and "Geprüft" in r.output
    # without a target the store goes where the vocabulary's is: the folder of the lex stores
    r = runner.invoke(app, ["datenbank", "auslagern", "--speicher", str(database), "--ja"])
    assert r.exit_code == 0, r.output
    assert (environment.lex_folder() / "lexident.sqlite").exists()
    assert environment.lex_folder() == tmp_path / "daten"
