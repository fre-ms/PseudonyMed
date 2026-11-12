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

from pseudonymed.ablauf import Ablaufoptionen, pseudonymisiere
from pseudonymed.cli import app
from pseudonymed.kern.modell import Patient
from pseudonymed.pseudonyme import kapsel as kapselmodul
from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen
from pseudonymed.pseudonyme.speicher import Pseudonymspeicher, einmalspeicher
from tests.test_cli import _kennung, _patientendatei
from tests.test_gui import qapp  # noqa: F401

braucht_sequoia = pytest.mark.skipif(not pgpmodul.pysequoia_verfuegbar(),
                                     reason="Extra 'pgp' (pysequoia) fehlt")


@pytest.fixture
def schluesselpaar(tmp_path: Path) -> tuple[Path, Path]:
    if not pgpmodul.pysequoia_verfuegbar():
        pytest.skip("Extra 'pgp' fehlt")
    pub, sec = pgpmodul.erzeuge_schluesselpaar("Test Empfänger", "e@example.org")
    (tmp_path / "pub.asc").write_text(pub, encoding="utf-8")
    (tmp_path / "sec.asc").write_text(sec, encoding="utf-8")
    return tmp_path / "pub.asc", tmp_path / "sec.asc"


def test_kapsel_json_rundlauf() -> None:
    k = kapselmodul.Kapsel(kennung="K7Q4MX", patient={"nachname": "Test", "vorname": "Anna",
                                                      "geschlecht": "w"},
                           zuordnungen={"Patientin K7Q4MX": "Anna Test"}, arten={}, quelle="a.pdf")
    k2 = kapselmodul.Kapsel.aus_json(k.als_json())
    assert k2 == k and "Anna Test" in k.als_json().decode()
    assert "Test, Anna" in k2.beschreibung() and k2.info()["kennung"] == "K7Q4MX"
    with pytest.raises(kapselmodul.KapselFehler):
        kapselmodul.Kapsel.aus_json(b'{"foo": 1}')
    with pytest.raises(kapselmodul.KapselFehler):
        kapselmodul.Kapsel.aus_json(b"kein json")


@braucht_sequoia
def test_zertifikat_und_fingerabdruck(schluesselpaar: tuple[Path, Path]) -> None:
    pub, _sec = schluesselpaar
    z = pgpmodul.lade_zertifikat(pub)
    assert len(z.fingerabdruck) == 40 and z.gruppen.count(" ") == 9
    assert z.nutzer and "e@example.org" in z.nutzer[0] and z.kurz in z.gruppen
    assert pgpmodul.lade_zertifikat(z.armor).fingerabdruck == z.fingerabdruck  # armoured text
    with pytest.raises(pgpmodul.PGPFehler, match="https"):
        pgpmodul.lade_zertifikat("http://example.org/key.asc")
    with pytest.raises(pgpmodul.PGPFehler, match="nicht gefunden"):
        pgpmodul.lade_zertifikat(pub.parent / "fehlt.asc")
    with pytest.raises(pgpmodul.PGPFehler):
        pgpmodul.erzeuge_schluesselpaar("X", "keine-adresse")


@braucht_sequoia
def test_kapsel_rundlauf_pipeline(tmp_path: Path, befund: Path, patient: Patient,
                                  schluesselpaar: tuple[Path, Path]) -> None:
    """The sender pseudonymises with a one-time key and gets a capsule; the recipient
    opens it with the private key and gets everything back, including the referrer."""
    pub, sec = schluesselpaar
    z = pgpmodul.lade_zertifikat(pub)
    opt = Ablaufoptionen(empfaenger=z.armor, empfaenger_fingerabdruck=z.fingerabdruck)
    with Pseudonymspeicher(tmp_path / "lokal.sqlite") as lokal, \
            pytest.raises(ValueError, match="Einmalspeicher"):
        pseudonymisiere(befund, patient, opt, speicher=lokal)  # never with the local store
    with einmalspeicher() as sp:
        erg = pseudonymisiere(befund, patient, opt, speicher=sp)
    assert erg.freigegeben and erg.ziel_kapsel is not None
    assert erg.ziel_kapsel.name == "befund_pseudonym.kapsel.asc"
    assert erg.bericht.optionen["empfaenger"] == z.fingerabdruck
    assert any("Schlüsselkapsel" in h for h in erg.bericht.hinweise)
    roh = erg.ziel_kapsel.read_bytes()
    assert roh.startswith(b"-----BEGIN PGP MESSAGE-----")
    assert patient.nachname.encode() not in roh and erg.kennung.encode() not in roh
    k = kapselmodul.oeffne(erg.ziel_kapsel, sec)
    assert k.kennung == erg.kennung and k.empfaenger == z.fingerabdruck
    assert k.zuordnungen[f"Patient {erg.kennung}"] == patient.vollname
    assert k.patient["nachname"] == patient.nachname
    assert k.als_patient().geburtsdatum == patient.geburtsdatum
    r = rueckersetzen(erg.text, k.zuordnungen, k.info(), bekannte_kennungen=[k.kennung])
    assert patient.nachname in r.text and patient.zuweiser[0].nachname in r.text
    assert patient.geburtsdatum.strftime("%d.%m.%Y") in r.text and r.verbleibend == []
    # the wrong key opens nothing; a key file is not a capsule
    _pub2, sec2 = pgpmodul.erzeuge_schluesselpaar("Fremd", "f@example.org")
    (tmp_path / "fremd.asc").write_text(sec2, encoding="utf-8")
    with pytest.raises(pgpmodul.PGPFehler):
        kapselmodul.oeffne(erg.ziel_kapsel, tmp_path / "fremd.asc")
    with pytest.raises(kapselmodul.KapselFehler):
        kapselmodul.oeffne(pub, sec)


@braucht_sequoia
def test_kapsel_ueber_cli(tmp_path: Path, befund: Path, patient: Patient) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["schluessel", "erzeugen", "--name", "Test", "--email",
                            "t@example.org", "--ziel", str(tmp_path / "k")])
    assert r.exit_code == 0 and "Fingerabdruck" in r.output, r.output
    pub = tmp_path / "k" / "pseudonymed_oeffentlich.asc"
    sec = tmp_path / "k" / "pseudonymed_privat.asc"
    r = runner.invoke(app, ["schluessel", "zeigen", str(pub)])
    assert r.exit_code == 0 and "t@example.org" in r.output
    pdatei = _patientendatei(tmp_path, patient)
    speicher = tmp_path / "s.sqlite"
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(pdatei), "--speicher",
                            str(speicher), "--empfaenger", str(pub)])
    assert r.exit_code == 0 and "Kapsel:" in r.output and "Empfänger:" in r.output, r.output
    assert not speicher.exists()  # nothing local in recipient mode
    k = _kennung(r.output)
    kapsel = tmp_path / "befund_pseudonym.kapsel.asc"
    brief = tmp_path / "brief.txt"
    brief.write_text(f"Patient {k}, [geb. {patient.geburtsdatum.year}], von Dr. Z1 vorgestellt.",
                     encoding="utf-8")
    r = runner.invoke(app, ["rueckersetzen", str(brief), "--kapsel", str(kapsel),
                            "--privater-schluessel", str(sec), "--form", "anrede",
                            "--speicher", str(speicher)])
    assert r.exit_code == 0, r.output
    assert f"Herr {patient.nachname}" in r.output and patient.zuweiser[0].nachname in r.output
    assert "Kapsel: " in r.output and k in r.output
    r = runner.invoke(app, ["pseudonymisieren", str(befund), "--patient", str(pdatei),
                            "--empfaenger", str(tmp_path / "fehlt.asc")])
    assert r.exit_code == 1 and "nicht gefunden" in r.output
    r = runner.invoke(app, ["rueckersetzen", str(brief), "--kapsel", str(pub),
                            "--privater-schluessel", str(sec)])
    assert r.exit_code == 1 and "Kapsel" in r.output


@braucht_sequoia
def test_fenster_kapsel(qapp: QApplication, tmp_path: Path, befund: Path, patient: Patient,  # noqa: F811
                        monkeypatch: pytest.MonkeyPatch,
                        schluesselpaar: tuple[Path, Path]) -> None:
    """Sender side in the analysis and export steps, recipient side in step 7."""
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung
    from pseudonymed.konfig import lade_einstellungen

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    pub, sec = schluesselpaar
    sitzung = Sitzung(speicherpfad=tmp_path / "s.sqlite")
    fenster = Hauptfenster(sitzung)
    for seite in (fenster.dokumente, fenster.analysieren, fenster.export, fenster.rueckersetzen):
        seite.synchron = True
    fenster.patient.fuelle(patient)
    fenster.patient.uebernehmen()
    fenster.dokumente.hinzufuegen([befund])
    an = fenster.analysieren
    an.empfaenger_quelle.setText(str(pub))
    z = an.empfaenger_pruefen()
    assert z is not None and z.gruppen in an.empfaenger_info.text()
    an.weitergabe.setChecked(True)
    an.analysieren()
    qapp.processEvents()
    assert sitzung.empfaenger is not None and sitzung.optionen.empfaenger.startswith("-----BEGIN")
    fenster.export.ziel.setText(str(tmp_path / "aus"))
    fenster.export.aktualisiere()
    qapp.processEvents()
    assert fenster.export.farbe in ("gruen", "gelb"), fenster.export.ampel_text.text()
    fenster.export.exportieren()
    qapp.processEvents()
    kapsel = tmp_path / "aus" / "befund_pseudonym.kapsel.asc"
    assert kapsel.exists() and "Schlüsselkapsel" in fenster.export.ausgabe.toPlainText()
    with Pseudonymspeicher(tmp_path / "s.sqlite") as sp:
        assert sp.finde() == []  # the local database saw nothing
    # recipient side
    rs = fenster.rueckersetzen
    rs.privater_schluessel.setText(str(sec))
    rs._schluessel_merken()
    k = rs.kapsel_oeffnen(kapsel)
    assert k is not None and not rs.person.isEnabled() and k.kennung in rs.kapsel_info.text()
    rs.eingabe.setPlainText(f"Patient {k.kennung} kam mit Angehörige {k.kennung}-1.")
    rs.rueckersetzen()
    assert patient.vollname in rs.ausgabe.toPlainText()
    assert patient.angehoerige[0].nachname in rs.ausgabe.toPlainText()
    rs.kapsel_schliessen()
    assert rs.person.isEnabled()
    e = lade_einstellungen()
    assert e.empfaenger_fingerabdruck == z.fingerabdruck and e.pgp_privater_schluessel == str(sec)
    with pytest.raises(RuntimeError):
        rs.kapsel_oeffnen(pub)  # not a capsule
    fenster.close()


def _gnupg_schluessel() -> tuple[Path, Path] | None:
    """A throw-away GnuPG home with an Ed25519/Cv25519 key; None when GnuPG or its
    agent is not usable here."""
    gpg = pgpmodul.gnupg_programm()
    if not gpg:
        return None
    home = Path(tempfile.mkdtemp(prefix="ag"))  # short: the agent's socket path is limited
    os.chmod(home, 0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}

    def lauf(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([gpg, "--batch", "--quiet", "--pinentry-mode", "loopback",
                               "--passphrase", "pw", *args], env=env, capture_output=True,
                              text=True, timeout=120, check=False)

    if lauf("--quick-generate-key", "Test <t@example.org>", "ed25519", "cert", "never").returncode:
        shutil.rmtree(home, ignore_errors=True)
        return None
    liste = subprocess.run([gpg, "--batch", "--list-keys", "--with-colons"], env=env,
                           capture_output=True, text=True, check=False).stdout
    fpr = next((z.split(":")[9] for z in liste.splitlines() if z.startswith("fpr")), "")
    if not fpr or lauf("--quick-add-key", fpr, "cv25519", "encr", "never").returncode:
        shutil.rmtree(home, ignore_errors=True)
        return None
    pub = home / "pub.asc"
    pub.write_bytes(subprocess.run([gpg, "--batch", "--armor", "--export", "t@example.org"],
                                   env=env, capture_output=True, check=False).stdout)
    return home, pub


@pytest.mark.skipif(pgpmodul.gnupg_programm() is None, reason="GnuPG fehlt")
def test_gnupg_schluesselbund(tmp_path: Path) -> None:
    """The recipient keeps the key in GnuPG: the capsule opens through the keyring."""
    erzeugt = _gnupg_schluessel()
    if erzeugt is None:
        pytest.skip("GnuPG konnte hier keinen Schlüssel erzeugen (Agent)")
    home, pub = erzeugt
    try:
        z = pgpmodul.lade_zertifikat(pub)
        k = kapselmodul.Kapsel(kennung="ABCDEF", patient={"nachname": "Test"},
                               zuordnungen={"Patient ABCDEF": "Max Test"})
        pfad = kapselmodul.schreibe(k, z, tmp_path / "t.kapsel.asc")
        with pytest.raises(pgpmodul.PGPFehler):  # first: afterwards the agent caches it
            kapselmodul.oeffne(pfad, None, "falsch", gnupg_home=home)
        k2 = kapselmodul.oeffne(pfad, None, "pw", gnupg_home=home)
        assert k2.zuordnungen == k.zuordnungen
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env={**os.environ,
                                                               "GNUPGHOME": str(home)},
                       capture_output=True, check=False)
        shutil.rmtree(home, ignore_errors=True)


def test_kapsel_json_felder() -> None:
    """Pre-rename capsules open, null fields fall back to the defaults, and a newer
    version is refused."""
    import json

    k = kapselmodul.Kapsel(kennung="K7Q4MX", patient={"nachname": "Test"},
                           zuordnungen={"Patient K7Q4MX": "Test"}, quelle="a.pdf")
    roh = json.loads(k.als_json())
    alt = {"anonymed_kapsel": roh.pop("pseudonymed_kapsel"), **roh}
    assert kapselmodul.Kapsel.aus_json(json.dumps(alt).encode()) == k
    mit_null = dict(roh, pseudonymed_kapsel=1, arten=None, werkzeug=None, einmalschluessel=None)
    k2 = kapselmodul.Kapsel.aus_json(json.dumps(mit_null).encode())
    assert k2.arten == {} and k2.werkzeug == k.werkzeug and k2.einmalschluessel == ""
    with pytest.raises(kapselmodul.KapselFehler, match="neuer"):
        kapselmodul.Kapsel.aus_json(json.dumps(dict(roh, pseudonymed_kapsel=99)).encode())
    with pytest.raises(kapselmodul.KapselFehler, match="Zuordnungen"):
        kapselmodul.Kapsel.aus_json(json.dumps(dict(roh, pseudonymed_kapsel=1,
                                                    zuordnungen=[])).encode())
