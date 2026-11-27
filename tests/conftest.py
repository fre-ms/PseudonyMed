"""Shared fixtures.  All data is synthetic (Faker de_DE, fixed seeds)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import lexcor.store
import pytest

from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.store import PseudonymStore
from tests.carddav_fake import FakeCardDAV
from tests.synthetic import create_report_pdf, synthetic_patient, vcard_from_patient

_SAVED: dict[str, str | None] = {}
_LEX_FOLDER = lexcor.store.data_folder


def pytest_configure(config: pytest.Config) -> None:
    """No test touches the user's real data folder: database, key, vocabulary and
    configuration go to a folder of the run (tests may override per test).  Set here and
    not in a fixture, because collection already asks for the OCR programs, and that
    reads the configuration (and would move a folder of the former name).  The stores of
    lexterm and lexident follow PseudonyMed's data folder; a ``LEXCOR_DATA`` of the
    developer's must not lead them elsewhere, and where a test unsets the data folder the
    libraries' own default is a folder of the run as well."""
    import tempfile

    folder = tempfile.mkdtemp(prefix="pseudonymed-daten-")
    _SAVED["LEXCOR_DATA"] = os.environ.pop("LEXCOR_DATA", None)
    lexcor.store.data_folder = lambda: Path(  # type: ignore[assignment]
        os.environ.get("LEXCOR_DATA") or Path(folder) / "lexcor")
    values = {"PSEUDONYMED_DATENORDNER": folder,
              "PSEUDONYMED_LEXIKON": "0",  # the lexicon tests switch it on for themselves
              "PSEUDONYMED_SPRACHE": "de"}  # German, whatever the machine's locale says
    for name, value in values.items():
        _SAVED[name] = os.environ.get(name)
        os.environ[name] = value
    for name in ("ANONYMED_DATENORDNER", "ANONYMED_LEXIKON"):
        _SAVED[name] = os.environ.pop(name, None)


def pytest_unconfigure(config: pytest.Config) -> None:
    import shutil

    folder = os.environ.get("PSEUDONYMED_DATENORDNER", "")
    if Path(folder).name.startswith("pseudonymed-daten-"):
        shutil.rmtree(folder, ignore_errors=True)
    lexcor.store.data_folder = _LEX_FOLDER
    for name, value in _SAVED.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session")
def isolated_data_folder() -> Path:
    return Path(os.environ["PSEUDONYMED_DATENORDNER"])


@pytest.fixture
def patient() -> Patient:
    return synthetic_patient(7, "m")


@pytest.fixture
def female_patient() -> Patient:
    return synthetic_patient(11, "w")


@pytest.fixture
def report_pdf(tmp_path: Path, patient: Patient) -> Path:
    return create_report_pdf(tmp_path / "befund.pdf", patient)


TEST_KEY = bytes(range(32))


@pytest.fixture
def store() -> Iterator[PseudonymStore]:
    with PseudonymStore(":memory:", secret=TEST_KEY) as s:
        yield s


@pytest.fixture
def letter_store() -> Iterator[PseudonymStore]:
    with PseudonymStore(":memory:", schema="buchstabe") as s:
        yield s


@pytest.fixture
def fake_carddav(patient: Patient, female_patient: Patient) -> Iterator[FakeCardDAV]:
    """Two address books: an empty 'contacts' and 'medifed-patients' with two synthetic cards."""
    server = FakeCardDAV(
        books={
            "contacts": ("Kontakte", []),
            "medifed-patients": ("Patienten", [vcard_from_patient(patient, "uid-a"),
                                               vcard_from_patient(female_patient, "uid-b")]),
        }
    ).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def carddav_environment(fake_carddav: FakeCardDAV, monkeypatch: pytest.MonkeyPatch) -> FakeCardDAV:
    """Point the CLI at the fake server through environment variables."""
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_URL", fake_carddav.url)
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_BENUTZER", fake_carddav.user)
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_ADRESSBUCH", "medifed-patients")
    monkeypatch.setenv("PSEUDONYMED_CARDDAV_PASSWORT", fake_carddav.password)
    return fake_carddav
