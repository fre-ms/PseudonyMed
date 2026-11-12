"""Unit tests for the helpers behind backup, restore and relocation.  The round trips
live in ``test_sicherung.py``; here are the branches those only reach by accident:
archive naming, decryption guards, manifest checks, key validation, relocation refusals
and merging an archive that predates a table."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pseudonymed.pseudonyme import pgp as pgpmodul
from pseudonymed.pseudonyme import sicherung as sichmodul
from pseudonymed.pseudonyme.speicher import _SCHEMA


def _zip(dateien: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, daten in dateien.items():
            z.writestr(name, daten)
    return puffer.getvalue()


def _manifest(dateien: dict[str, bytes]) -> bytes:
    return json.dumps({sichmodul._MARKE: sichmodul.VERSION, "version": sichmodul.VERSION,
                       "dateien": {n: {"sha256": sichmodul._sha(d), "bytes": len(d)}
                                   for n, d in dateien.items()}}).encode()


def test_statistik_zeilen_reihenfolge() -> None:
    st = sichmodul.Statistik(
        pfad=Path("/x/p.sqlite"), existiert=True, je_kategorie=[("Patient", 1)], verworfen=2,
        verworfen_je_regel=[("ner.person", 2)], vokabular_kategorien=[("Test", 1)],
        lexikon_klassen=[("Klinik", "merkmal", 5), ("Anatomie", "fach", 7)],
        lexikon_zusatz=[("Kliniken AT", 3, "2026-11-12T10:00:00")], konfig_vorhanden=True)
    assert [k for k, _ in st.zeilen()] == [
        "Datenbank", "Vorhanden", "Größe", "Schema", "Schlüsseldatei", "Schlüssel vorhanden",
        "Schlüssel-Kennung", "Patienten", "Verknüpfte Schreibweisen", "Zuordnungen gesamt",
        "    Patient", "Gemerkte Verwerfungen", "    ner.person", "Erster Eintrag",
        "Letzter Eintrag", "Integrität", "Seiten / frei", "Vokabular (Begriffe)", "    Test",
        "Lexikon (Einträge)", "    Klinik (Identifikator)", "    Anatomie (Fachklasse)",
        "Zusatzdaten (selbst geladen)", "Konfiguration"]
    werte = dict(st.zeilen())
    assert werte["Lexikon (Einträge)"] == "12" and werte["Schema"] == "–"
    assert werte["Zusatzdaten (selbst geladen)"] == "Kliniken AT: 3 Einträge, 2026-11-12"
    assert "Lexikon (Einträge)" not in dict(sichmodul.Statistik(pfad=Path("/x")).zeilen())


def test_archivpfad(tmp_path: Path) -> None:
    ordner = tmp_path / "s"
    ordner.mkdir()
    p = sichmodul._archivpfad(ordner, verschluesselt=False)
    assert p.parent == ordner and p.name.startswith("pseudonymed_sicherung_") and p.suffix == ".zip"
    assert sichmodul._archivpfad(tmp_path / "ohne", verschluesselt=True).name.endswith(".zip.gpg")
    gpg = tmp_path / "a.zip.gpg"
    assert sichmodul._archivpfad(tmp_path / "a.zip", verschluesselt=True) == gpg
    assert sichmodul._archivpfad(gpg, verschluesselt=True) == gpg  # never a second suffix
    assert sichmodul._archivpfad(tmp_path / "a.zip", verschluesselt=False) == tmp_path / "a.zip"


def test_als_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sichmodul._als_zip(b"PK\x03\x04rest", None) == b"PK\x03\x04rest"
    with pytest.raises(sichmodul.SicherungFehler, match="Passphrase"):
        sichmodul._als_zip(b"\x85\x02", None)
    monkeypatch.setattr(pgpmodul, "entschluessele_mit_passphrase", lambda daten, pw: b"kein zip")
    with pytest.raises(sichmodul.SicherungFehler, match="kein ZIP"):
        sichmodul._als_zip(b"\x85\x02", "pw")

    def scheitert(daten: bytes, pw: str) -> bytes:
        raise pgpmodul.PGPFehler("falsche Passphrase")

    monkeypatch.setattr(pgpmodul, "entschluessele_mit_passphrase", scheitert)
    with pytest.raises(sichmodul.SicherungFehler, match="falsche Passphrase"):
        sichmodul._als_zip(b"\x85\x02", "pw")
    monkeypatch.setattr(pgpmodul, "entschluessele_mit_passphrase", lambda daten, pw: b"PK ok")
    assert sichmodul._als_zip(b"\x85\x02", "pw") == b"PK ok"


def test_manifest_aus() -> None:
    with pytest.raises(sichmodul.SicherungFehler, match="Kein PseudonyMed"):
        sichmodul._manifest_aus(b'{"version": 1}')
    neuer = json.dumps({sichmodul._MARKE: 1, "version": sichmodul.VERSION + 1}).encode()
    with pytest.raises(sichmodul.SicherungFehler, match="neueren"):
        sichmodul._manifest_aus(neuer)
    alt = json.dumps({sichmodul._FRUEHERE_MARKE: 1, "version": 1, "patienten": 3,
                      "unbekannt": "x"}).encode()
    m = sichmodul._manifest_aus(alt)
    assert m.patienten == 3 and m.version == 1 and not m.konfig


def test_lies_archiv_und_vollstaendigkeit() -> None:
    with pytest.raises(sichmodul.SicherungFehler, match="Beschädigtes"):
        sichmodul._lies_archiv(b"PK kein zip")
    with pytest.raises(sichmodul.SicherungFehler, match="manifest.json fehlt"):
        sichmodul._lies_archiv(_zip({"a.txt": b"x"}))
    dateien = {sichmodul.NAME_DB: b"db", sichmodul.NAME_SCHLUESSEL: b"00ff\n"}
    manifest, inhalte = sichmodul._lies_archiv(
        _zip({sichmodul.NAME_MANIFEST: _manifest(dateien), **dateien}))
    assert inhalte == dateien and set(manifest.dateien) == set(dateien)
    sichmodul._pruefe_vollstaendigkeit(manifest, inhalte)
    with pytest.raises(sichmodul.SicherungFehler, match="fehlt im Archiv"):
        sichmodul._pruefe_vollstaendigkeit(manifest, {sichmodul.NAME_DB: b"db"})
    with pytest.raises(sichmodul.SicherungFehler, match="Prüfsumme"):
        sichmodul._pruefe_vollstaendigkeit(manifest, {**inhalte, sichmodul.NAME_DB: b"anders"})
    with pytest.raises(sichmodul.SicherungFehler, match="ohne Datenbank"):
        sichmodul._pruefe_vollstaendigkeit(sichmodul.Manifest(), {sichmodul.NAME_KONFIG: b""})


def test_schluessel_aus() -> None:
    assert sichmodul._schluessel_aus({sichmodul.NAME_SCHLUESSEL: b" 00ff \n"}) == b"00ff"
    for unlesbar in (b"kein hex", "ü".encode()):
        with pytest.raises(sichmodul.SicherungFehler, match="nicht lesbar"):
            sichmodul._schluessel_aus({sichmodul.NAME_SCHLUESSEL: unlesbar})


def test_verlege_schluessel(tmp_path: Path) -> None:
    alt = tmp_path / "alt" / "k.txt"
    alt.parent.mkdir()
    alt.write_bytes(b"00ff\n")
    neu = tmp_path / "neu" / "k.txt"
    assert sichmodul._verlege_schluessel(alt, alt, "s") is None  # same place: nothing to do
    assert sichmodul._verlege_schluessel(tmp_path / "fehlt", neu, "s") is None
    neu.parent.mkdir()
    neu.write_bytes(b"ffff\n")
    with pytest.raises(sichmodul.SicherungFehler, match="anderen Schlüssel"):
        sichmodul._verlege_schluessel(alt, neu, "s")
    neu.write_bytes(b"00ff")  # the same key, other whitespace: accepted
    beiseite = sichmodul._verlege_schluessel(alt, neu, "s")
    assert beiseite == tmp_path / "alt" / "k.txt.verlegt-s" and beiseite.exists()
    assert not alt.exists() and neu.read_bytes() == b"00ff\n"


def _datenbank_mit_patient(pfad: Path) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    v = sqlite3.connect(pfad)
    v.executescript(_SCHEMA)
    v.execute("INSERT INTO patienten (schluessel, kennung, angelegt) VALUES ('s1', 'K1', 't')")
    v.commit()
    v.close()


def _anzahl(pfad: Path, tabelle: str) -> int:
    v = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
    try:
        return int(v.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0])
    finally:
        v.close()


def test_verlege_datenbank(tmp_path: Path) -> None:
    alt = tmp_path / "alt" / "p.sqlite"
    _datenbank_mit_patient(alt)
    assert sichmodul._verlege_datenbank(alt, alt, "s") is None
    assert sichmodul._verlege_datenbank(tmp_path / "fehlt.sqlite", tmp_path / "x", "s") is None
    voll = tmp_path / "voll.sqlite"
    shutil.copy2(alt, voll)
    with pytest.raises(sichmodul.SicherungFehler, match="schon eine Datenbank"):
        sichmodul._verlege_datenbank(alt, voll, "s")
    assert alt.exists()  # a refusal moves nothing
    neu = tmp_path / "neu" / "tief" / "p.sqlite"
    beiseite = sichmodul._verlege_datenbank(alt, neu, "s")
    assert beiseite == alt.with_name("p.sqlite.verlegt-s") and beiseite.exists()
    assert not alt.exists() and _anzahl(neu, "patienten") == 1


def test_uebernimm_eintraege_toleriert_fehlende_tabellen(tmp_path: Path) -> None:
    archiv = tmp_path / "archiv.sqlite"
    _datenbank_mit_patient(archiv)
    v = sqlite3.connect(archiv)
    v.executescript("DROP TABLE vokabular; DROP TABLE restrisiko_dokumente;")
    v.close()
    lokal = tmp_path / "lokal.sqlite"
    zaehler = sichmodul._uebernimm_eintraege(lokal, archiv)
    assert zaehler["patienten"] == 1 and zaehler["vokabular"] == 0
    assert zaehler["restrisiko_dokumente"] == 0 and _anzahl(lokal, "patienten") == 1
    assert sichmodul._uebernimm_eintraege(lokal, archiv)["patienten"] == 0  # idempotent
    assert _anzahl(lokal, "vokabular_meta") == 0  # no words, no version bump
