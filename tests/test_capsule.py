"""Key capsules: pseudonyms for a recipient, opened with the recipient's private key."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.cli import app
from pseudonymed.core.model import Patient
from pseudonymed.pipeline import PipelineOptions, pseudonymise
from pseudonymed.pseudonyms import capsule as capsule_module
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.reidentification import reidentify
from pseudonymed.pseudonyms.store import PseudonymStore, one_time_store
from tests.test_cli import _code, _patient_file
from tests.test_gui import qapp  # noqa: F401

needs_sequoia = pytest.mark.skipif(not pgp_module.pysequoia_available(),
                                   reason="Extra 'pgp' (pysequoia) fehlt")


@pytest.fixture
def key_pair(tmp_path: Path) -> tuple[Path, Path]:
    if not pgp_module.pysequoia_available():
        pytest.skip("Extra 'pgp' fehlt")
    pub, sec = pgp_module.create_key_pair("Test Empfänger", "e@example.org")
    (tmp_path / "pub.asc").write_text(pub, encoding="utf-8")
    (tmp_path / "sec.asc").write_text(sec, encoding="utf-8")
    return tmp_path / "pub.asc", tmp_path / "sec.asc"


def test_capsule_json_round_trip() -> None:
    k = capsule_module.Capsule(code="K7Q4MX", patient={"nachname": "Test", "vorname": "Anna",
                                                       "geschlecht": "w"},
                               mappings={"Patientin K7Q4MX": "Anna Test"}, kinds={}, source="a.pdf")
    k2 = capsule_module.Capsule.from_json(k.as_json())
    assert k2 == k and "Anna Test" in k.as_json().decode()
    assert "Test, Anna" in k2.description() and k2.info()["kennung"] == "K7Q4MX"
    with pytest.raises(capsule_module.CapsuleError):
        capsule_module.Capsule.from_json(b'{"foo": 1}')
    with pytest.raises(capsule_module.CapsuleError):
        capsule_module.Capsule.from_json(b"kein json")


@needs_sequoia
def test_certificate_and_fingerprint(key_pair: tuple[Path, Path]) -> None:
    pub, _sec = key_pair
    z = pgp_module.load_certificate(pub)
    assert len(z.fingerprint) == 40 and z.groups.count(" ") == 9
    assert z.user_ids and "e@example.org" in z.user_ids[0] and z.short in z.groups
    assert pgp_module.load_certificate(z.armor).fingerprint == z.fingerprint  # armoured text
    with pytest.raises(pgp_module.PGPError, match="https"):
        pgp_module.load_certificate("http://example.org/key.asc")
    with pytest.raises(pgp_module.PGPError, match="nicht gefunden"):
        pgp_module.load_certificate(pub.parent / "fehlt.asc")
    with pytest.raises(pgp_module.PGPError):
        pgp_module.create_key_pair("X", "keine-adresse")


@needs_sequoia
def test_capsule_round_trip_pipeline(tmp_path: Path, report_pdf: Path, patient: Patient,
                                     key_pair: tuple[Path, Path]) -> None:
    """The sender pseudonymises with a one-time key and gets a capsule; the recipient
    opens it with the private key and gets everything back, including the referrer."""
    pub, sec = key_pair
    z = pgp_module.load_certificate(pub)
    opt = PipelineOptions(recipient=z.armor, recipient_fingerprint=z.fingerprint)
    with PseudonymStore(tmp_path / "lokal.sqlite") as local, \
            pytest.raises(ValueError, match="Einmalspeicher"):
        pseudonymise(report_pdf, patient, opt, store=local)  # never with the local store
    with one_time_store() as sp:
        res = pseudonymise(report_pdf, patient, opt, store=sp)
    assert res.released and res.target_capsule is not None
    assert res.target_capsule.name == "befund_pseudonym.kapsel.asc"
    assert res.report.options["empfaenger"] == z.fingerprint
    assert any("Schlüsselkapsel" in h for h in res.report.notes)
    raw = res.target_capsule.read_bytes()
    assert raw.startswith(b"-----BEGIN PGP MESSAGE-----")
    assert patient.surname.encode() not in raw and res.code.encode() not in raw
    k = capsule_module.open_file(res.target_capsule, sec)
    assert k.code == res.code and k.recipient == z.fingerprint
    assert k.mappings[f"Patient {res.code}"] == patient.full_name
    assert k.patient["nachname"] == patient.surname
    assert k.as_patient().birth_date == patient.birth_date
    r = reidentify(res.text, k.mappings, k.info(), known_codes=[k.code])
    assert patient.surname in r.text and patient.referrers[0].surname in r.text
    assert patient.birth_date.strftime("%d.%m.%Y") in r.text and r.remaining == []
    # the wrong key opens nothing; a key file is not a capsule
    _pub2, sec2 = pgp_module.create_key_pair("Fremd", "f@example.org")
    (tmp_path / "fremd.asc").write_text(sec2, encoding="utf-8")
    with pytest.raises(pgp_module.PGPError):
        capsule_module.open_file(res.target_capsule, tmp_path / "fremd.asc")
    with pytest.raises(capsule_module.CapsuleError):
        capsule_module.open_file(pub, sec)


@needs_sequoia
def test_capsule_via_cli(tmp_path: Path, report_pdf: Path, patient: Patient) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["schluessel", "erzeugen", "--name", "Test", "--email",
                            "t@example.org", "--ziel", str(tmp_path / "k")])
    assert r.exit_code == 0 and "Fingerabdruck" in r.output, r.output
    pub = tmp_path / "k" / "pseudonymed_oeffentlich.asc"
    sec = tmp_path / "k" / "pseudonymed_privat.asc"
    r = runner.invoke(app, ["schluessel", "zeigen", str(pub)])
    assert r.exit_code == 0 and "t@example.org" in r.output
    pfile = _patient_file(tmp_path, patient)
    store = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                            "--speicher",
                            str(store), "--empfaenger", str(pub)])
    assert r.exit_code == 0 and "Kapsel:" in r.output and "Empfänger:" in r.output, r.output
    assert not store.exists()  # nothing local in recipient mode
    k = _code(r.output)
    capsule = tmp_path / "befund_pseudonym.kapsel.asc"
    letter = tmp_path / "brief.txt"
    letter.write_text(f"Patient {k}, [geb. {patient.birth_date.year}], von Dr. Z1 vorgestellt.",
                      encoding="utf-8")
    r = runner.invoke(app, ["rueckersetzen", str(letter), "--kapsel", str(capsule),
                            "--privater-schluessel", str(sec), "--form", "anrede",
                            "--speicher", str(store)])
    assert r.exit_code == 0, r.output
    assert f"Herr {patient.surname}" in r.output and patient.referrers[0].surname in r.output
    assert "Kapsel: " in r.output and k in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(report_pdf), "--patient", str(pfile),
                            "--empfaenger", str(tmp_path / "fehlt.asc")])
    assert r.exit_code == 1 and "nicht gefunden" in r.output
    r = runner.invoke(app, ["rueckersetzen", str(letter), "--kapsel", str(pub),
                            "--privater-schluessel", str(sec)])
    assert r.exit_code == 1 and "Kapsel" in r.output


@needs_sequoia
def test_window_capsule(qapp: QApplication, tmp_path: Path, report_pdf: Path, patient: Patient,  # noqa: F811
                        monkeypatch: pytest.MonkeyPatch,
                        key_pair: tuple[Path, Path]) -> None:
    """Sender side in the analysis and export steps, recipient side in step 7."""
    from pseudonymed.config import load_settings
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    pub, sec = key_pair
    session = Session(store_path=tmp_path / "s.sqlite")
    window = MainWindow(session)
    for page in (window.documents, window.analyse, window.export, window.reidentify):
        page.synchronous = True
    window.patient.fill_in(patient)
    window.patient.adopt()
    window.documents.add([report_pdf])
    on = window.analyse
    on.recipient_source.setText(str(pub))
    z = on.check_recipient()
    assert z is not None and z.groups in on.recipient_info.text()
    on.sharing.setChecked(True)
    on.analyse()
    qapp.processEvents()
    assert session.recipient is not None and session.options.recipient.startswith("-----BEGIN")
    window.export.target.setText(str(tmp_path / "aus"))
    window.export.refresh()
    qapp.processEvents()
    assert window.export.colour in ("gruen", "gelb"), window.export.traffic_light_text.text()
    window.export.run_export()
    qapp.processEvents()
    capsule = tmp_path / "aus" / "befund_pseudonym.kapsel.asc"
    assert capsule.exists() and "Schlüsselkapsel" in window.export.output.toPlainText()
    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        assert sp.find() == []  # the local database saw nothing
    # recipient side
    ri = window.reidentify
    ri.private_key.setText(str(sec))
    ri._remember_key()
    k = ri.open_capsule(capsule)
    assert k is not None and not ri.person.isEnabled() and k.code in ri.capsule_info.text()
    ri.input_text.setPlainText(f"Patient {k.code} kam mit Angehörige {k.code}-1.")
    ri.reidentify()
    assert patient.full_name in ri.output.toPlainText()
    assert patient.relatives[0].surname in ri.output.toPlainText()
    ri.close_capsule()
    assert ri.person.isEnabled()
    e = load_settings()
    assert e.recipient_fingerprint == z.fingerprint and e.pgp_private_key == str(sec)
    with pytest.raises(RuntimeError):
        ri.open_capsule(pub)  # not a capsule
    window.close()


def _gnupg_keys() -> tuple[Path, Path] | None:
    """A throw-away GnuPG home with an Ed25519/Cv25519 key; None when GnuPG or its
    agent is not usable here."""
    gpg = pgp_module.gnupg_program()
    if not gpg:
        return None
    home = Path(tempfile.mkdtemp(prefix="ag"))  # short: the agent's socket path is limited
    os.chmod(home, 0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([gpg, "--batch", "--quiet", "--pinentry-mode", "loopback",
                               "--passphrase", "pw", *args], env=env, capture_output=True,
                              text=True, timeout=120, check=False)

    if run("--quick-generate-key", "Test <t@example.org>", "ed25519", "cert", "never").returncode:
        shutil.rmtree(home, ignore_errors=True)
        return None
    listing = subprocess.run([gpg, "--batch", "--list-keys", "--with-colons"], env=env,
                             capture_output=True, text=True, check=False).stdout
    fpr = next((z.split(":")[9] for z in listing.splitlines() if z.startswith("fpr")), "")
    if not fpr or run("--quick-add-key", fpr, "cv25519", "encr", "never").returncode:
        shutil.rmtree(home, ignore_errors=True)
        return None
    pub = home / "pub.asc"
    pub.write_bytes(subprocess.run([gpg, "--batch", "--armor", "--export", "t@example.org"],
                                   env=env, capture_output=True, check=False).stdout)
    return home, pub


@pytest.mark.skipif(pgp_module.gnupg_program() is None, reason="GnuPG fehlt")
def test_gnupg_keyring(tmp_path: Path) -> None:
    """The recipient keeps the key in GnuPG: the capsule opens through the keyring."""
    created = _gnupg_keys()
    if created is None:
        pytest.skip("GnuPG konnte hier keinen Schlüssel erzeugen (Agent)")
    home, pub = created
    try:
        z = pgp_module.load_certificate(pub)
        k = capsule_module.Capsule(code="ABCDEF", patient={"nachname": "Test"},
                                   mappings={"Patient ABCDEF": "Max Test"})
        path = capsule_module.write(k, z, tmp_path / "t.kapsel.asc")
        with pytest.raises(pgp_module.PGPError):  # first: afterwards the agent caches it
            capsule_module.open_file(path, None, "falsch", gnupg_home=home)
        k2 = capsule_module.open_file(path, None, "pw", gnupg_home=home)
        assert k2.mappings == k.mappings
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env={**os.environ,
                                                               "GNUPGHOME": str(home)},
                       capture_output=True, check=False)
        shutil.rmtree(home, ignore_errors=True)


def test_capsule_json_fields() -> None:
    """Pre-rename capsules open, null fields fall back to the defaults, and a newer
    version is refused."""
    import json

    k = capsule_module.Capsule(code="K7Q4MX", patient={"nachname": "Test"},
                               mappings={"Patient K7Q4MX": "Test"}, source="a.pdf")
    raw = json.loads(k.as_json())
    old = {"anonymed_kapsel": raw.pop("pseudonymed_kapsel"), **raw}
    assert capsule_module.Capsule.from_json(json.dumps(old).encode()) == k
    with_null = dict(raw, pseudonymed_kapsel=1, arten=None, werkzeug=None, einmalschluessel=None)
    k2 = capsule_module.Capsule.from_json(json.dumps(with_null).encode())
    assert k2.kinds == {} and k2.tool == k.tool and k2.one_time_key == ""
    with pytest.raises(capsule_module.CapsuleError, match="neuer"):
        capsule_module.Capsule.from_json(json.dumps(dict(raw, pseudonymed_kapsel=99)).encode())
    with pytest.raises(capsule_module.CapsuleError, match="Zuordnungen"):
        capsule_module.Capsule.from_json(json.dumps(dict(raw, pseudonymed_kapsel=1,
                                                    zuordnungen=[])).encode())
