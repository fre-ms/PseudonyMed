"""Optional reference data loaded from the publisher: the Austrian hospital directory.

The workbook here is synthetic but follows the ministry's layout (sheets "KA gesamt" and
"Rohdaten Standorte"); place names are real Austrian municipalities from the bundled
demography table, hospital names are invented."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from pseudonymed.erkennung import lexikon
from pseudonymed.erkennung import zusatzdaten as zd
from tests.test_gui import qapp  # noqa: F401

KOPF = ["Bundesland", "KA-Nr.", "Bezeichnung", "Kurzbezeichnung", "Versorgungssektor",
        "Versorgungsbereich", "Bezirk", "Adresse", "Bettenanzahl", "Träger-Nr.",
        "Trägerbezeichnung"]
KAS = [
    ["Burgenland", "K901", "Musterklinik am Bach", "Bach KL", "Akutversorgung",
     "Allgemeinversorgung", "Eisenstadt(Stadt)", "Hauptstraße 1, 7000 Eisenstadt", "120", "T1",
     "Musterträger GmbH"],
    ["Burgenland", "K902", "Reha-Zentrum Beispielhof", "Beispielhof REHA", "Rehabilitation",
     "Bewegungsapparat", "Oberwart", "Kurweg 2, 7431 Bad Tatzmannsdorf", "80", "T2",
     "Reha Muster GmbH"],
    ["Wien", "K903", "Beispielinstitut für Psychiatrie", "Beispiel PSY", "Akutversorgung",
     "Psychiatrie", "Wien 23.,Liesing", "Gasse 3, 1230 Wien", "200", "T3", "Verein Muster"],
]
STANDORTE_KOPF = ["KA-Nr., Standort-Code", "Standort Bezeichnung", "Gemeinde", "Ort", "Straße",
                  "ÖSG Abteilung", "Bettenanzahl"]
STANDORTE = [
    ["K901.1", "Bach KL", "10101 Eisenstadt", "7000 Eisenstadt", "Hauptstraße 1",
     "03 Chirurgie", "60"],
    ["K901.1", "Bach KL", "10101 Eisenstadt", "7000 Eisenstadt", "Hauptstraße 1",
     "05 Innere Medizin", "60"],
    ["K901.2", "Bach KL Nord", "10901 Bad Tatzmannsdorf", "7431 Bad Tatzmannsdorf",
     "Nordweg 5", "05 Innere Medizin", "10"],
]


def _blatt(zeilen: list[list[str]]) -> str:
    teile = []
    for r, zeile in enumerate(zeilen, 1):
        zellen = "".join(
            f'<c r="{chr(65 + i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for i, v in enumerate(zeile))
        teile.append(f'<row r="{r}">{zellen}</row>')
    return ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(teile)}</sheetData></worksheet>')


def mappe(pfad: Path) -> Path:
    ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(pfad, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   f'{ns}><sheets><sheet name="KA gesamt" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Rohdaten Standorte" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", _blatt([KOPF] + KAS))
        z.writestr("xl/worksheets/sheet2.xml", _blatt([STANDORTE_KOPF] + STANDORTE))
    return pfad


def _zeilen(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {k: (b, z) for b, k, z in con.execute(
        "SELECT begriff, kennung, zusatz FROM lexikon WHERE quelle = ?", (zd.QUELLE_KLINIKEN_AT,))}


def test_mappe_wird_gelesen_und_zugeordnet(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "p.sqlite")
    bericht = zd.lade_kliniken_at(con, datei=mappe(tmp_path / "KA-Verzeichnis 2026-09-18.xlsx"))
    assert bericht.anzahl == 4 and bericht.ohne_gemeinde == 0 and bericht.stand == "2026-09-18"
    z = _zeilen(con)
    assert set(z) == {"AT:K901.1", "AT:K901.2", "AT:K902", "AT:K903"}
    assert '"gemeinde":"10101"' in z["AT:K901.1"][1]
    assert '"gemeinde":"10901"' in z["AT:K901.2"][1]  # second site, own municipality
    assert '"gemeinde":"10901"' in z["AT:K902"][1] and '"typ":"reha"' in z["AT:K902"][1]
    assert '"gemeinde":"92301"' in z["AT:K903"][1] and '"typ":"psychiatrie"' in z["AT:K903"][1]
    assert zd.geladene(con) == [(zd.QUELLE_KLINIKEN_AT, 4, zd.geladene(con)[0][2])]


def test_mappe_finden_auf_der_seite() -> None:
    html = ('<a href="/dam/jcr:abc/Logo.png">Logo</a> <a href="/dam/jcr:123/'
            'KA-Verzeichnis%202026-09-18.xlsx">Liste der Krankenanstalten</a>')
    url = zd.mappe_finden(html)
    assert url.startswith("https://www.sozialministerium.gv.at/dam/jcr:123/")
    with pytest.raises(zd.ZusatzFehler):
        zd.mappe_finden("<p>keine Mappe</p>")


def test_bleibt_beim_neuladen_der_mitgelieferten_daten(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")
    con = sqlite3.connect(tmp_path / "p.sqlite")
    zd.lade_kliniken_at(con, datei=mappe(tmp_path / "m.xlsx"))
    monkeypatch.setattr(lexikon, "QUELLEN", (lexikon.QUELLEN[2],))  # a small bundled source
    lexikon.sicherstellen(con, erzwingen=True)
    assert len(_zeilen(con)) == 4


def test_fenster_laedt_beim_ministerium(qapp: QApplication, tmp_path: Path,  # noqa: F811
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.sitzung import Sitzung

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    daten = mappe(tmp_path / "m.xlsx").read_bytes()
    abrufe: list[str] = []

    def holen(url: str) -> bytes:
        abrufe.append(url)
        return (b'<a href="/dam/KA-Verzeichnis%202026-09-18.xlsx">Liste</a>'
                if url == zd.SEITE_KLINIKEN_AT else daten)

    fenster = Hauptfenster(Sitzung(speicherpfad=tmp_path / "p.sqlite"))
    seite = fenster.datenbank
    seite.synchron = True
    seite.holen = holen
    bericht = seite.kliniken_at_laden()
    assert bericht is not None and bericht.anzahl == 4 and len(abrufe) == 2
    assert "4 Einträge geladen" in seite.zusatz_status.text()
    zeilen = dict(seite.aktualisiere().zeilen())
    assert zeilen["Zusatzdaten (selbst geladen)"].startswith(f"{zd.QUELLE_KLINIKEN_AT}: 4 Einträge")


def test_kommandozeile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.cli import app

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    ergebnis = CliRunner().invoke(app, ["datenbank", "zusatzdaten", "kliniken-at", "--datei",
                                        str(mappe(tmp_path / "m.xlsx")), "--speicher",
                                        str(tmp_path / "p.sqlite")])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "außer zu kommerziellen Zwecken" in ergebnis.output and "4 Einträge" in ergebnis.output
    falsch = CliRunner().invoke(app, ["datenbank", "zusatzdaten", "unbekannt"])
    assert falsch.exit_code != 0


def test_kaputte_datei(tmp_path: Path) -> None:
    datei = tmp_path / "kaputt.xlsx"
    datei.write_bytes(b"kein zip")
    with pytest.raises(zipfile.BadZipFile):
        zd.lade_kliniken_at(sqlite3.connect(":memory:"), datei=datei)
    leer = io.BytesIO()
    with zipfile.ZipFile(leer, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><sheets/></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"/>')
    (tmp_path / "leer.xlsx").write_bytes(leer.getvalue())
    with pytest.raises(zd.ZusatzFehler):
        zd.lade_kliniken_at(sqlite3.connect(":memory:"), datei=tmp_path / "leer.xlsx")


def test_zellen_aus_gemeinsamen_zeichenketten_und_werten() -> None:
    """Shared strings, stored numbers, a gap, a cell beyond column Z and an empty sheet."""
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{m}" xmlns:r="{r}"><sheets>'
                   '<sheet name="Daten" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Leer" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{m}"><si><t>Haus</t></si><si><t>am </t><t>Bach</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{m}"><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>12.0</v></c>'
                   '<c r="AA1" t="s"><v>1</v></c></row>'
                   '<row r="2"><c r="B2" t="inlineStr"><is><t>  zwei  Worte </t></is></c></row>'
                   '</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{m}"/>')
    blaetter = zd.xlsx_blaetter(puffer.getvalue())
    assert blaetter["Daten"][0] == ["Haus", "", "12.0"] + [""] * 23 + ["am Bach"]
    assert blaetter["Daten"][1] == ["", "zwei Worte"]
    assert blaetter["Leer"] == []


def test_gemeinde_ohne_kennziffer_wird_hergeleitet() -> None:
    """Vienna from the postcode, the postcode table, the word match inside the Bezirk, a
    unique name, and nothing when the evidence is ambiguous."""
    tab = zd._AtTabellen(
        gemeinden={"92301": "Wien 23., Liesing", "10101": "Eisenstadt", "60101": "Graz",
                   "61207": "Bad Aussee", "61235": "Bad Mitterndorf", "70101": "Innsbruck"},
        bezirke={"101": "Eisenstadt(Stadt)", "601": "Graz(Stadt)", "612": "Liezen",
                 "701": "Innsbruck-Stadt"},
        nach_plz={"8010": "60101"})
    assert zd._gemeinde_at("Wien", "1230", "", tab) == "92301"
    assert zd._gemeinde_at("", "1100", "", zd._AtTabellen({}, {}, {})) == "91001"
    assert zd._gemeinde_at("", "1240", "", tab) == ""  # no 24th district
    assert zd._gemeinde_at("Nirgendwo", "8010", "", tab) == "60101"  # postcode table
    assert zd._gemeinde_at("Bad Aussee", "", "Liezen", tab) == "61207"  # word match
    assert zd._gemeinde_at("Bad", "", "Liezen", tab) == ""  # "bad" is too short to count
    assert zd._gemeinde_at("Bad Aussee Mitterndorf", "", "Liezen", tab) == ""  # a tie
    assert zd._gemeinde_at("Graz", "", "Graz(Stadt)", tab) == "60101"  # exact Bezirk name
    assert zd._gemeinde_at("Innsbruck", "", "", tab) == "70101"  # unique name, no Bezirk
    assert zd._gemeinde_at("Unbekannt", "9999", "Liezen", tab) == ""


def test_tabellen_aus_zeilen() -> None:
    """Only Austrian rows count; the postcode keeps the Gemeinde with the largest share."""
    demografie = [{"land": "AT", "ebene": "kreis", "kennung": "612", "name": "Liezen"},
                  {"land": "AT", "ebene": "gemeinde", "kennung": "61207", "name": "Bad Aussee"},
                  {"land": "AT", "ebene": "staat", "kennung": "AT", "name": "Österreich"},
                  {"land": "DE", "ebene": "gemeinde", "kennung": "09761000", "name": "Augsburg"}]
    assert zd._at_namen(iter(demografie)) == ({"61207": "Bad Aussee"}, {"612": "Liezen"})
    plz = [{"land": "AT", "plz": "8990", "gemeinde_kennung": "61207", "anteil": "0.4"},
           {"land": "AT", "plz": "8990", "gemeinde_kennung": "61235", "anteil": "0.6"},
           {"land": "AT", "plz": "8991", "gemeinde_kennung": "61235", "anteil": "x"},
           {"land": "AT", "plz": "8991", "gemeinde_kennung": "61207", "anteil": "0.1"},
           {"land": "DE", "plz": "8990", "gemeinde_kennung": "0", "anteil": "1"}]
    assert zd._at_plz_gemeinden(iter(plz)) == {"8990": "61235", "8991": "61207"}


def test_standorte_aus_rohdaten() -> None:
    """One site per code despite one row per department; the Gemeinde's name stands in when
    the Ort column has no postcode; rows without a code are skipped."""
    roh = [STANDORTE_KOPF,
           ["K1.1", "A", "10101 Eisenstadt", "7000 Eisenstadt", "", "01", "1"],
           ["K1.1", "A", "10101 Eisenstadt", "7000 Eisenstadt", "", "02", "1"],
           ["K1.2", "B", "60101 Graz", "Graz", "", "", ""],
           ["", "C", "", "", "", "", ""],
           ["K2", "D", "", "8990 Bad Aussee", "", "", ""]]
    assert zd._standorte(roh) == {
        "K1": [zd._Standort("K1.1", "10101", "7000", "Eisenstadt"),
               zd._Standort("K1.2", "60101", "", "Graz")],
        "K2": [zd._Standort("K2", "", "8990", "Bad Aussee")]}
    assert zd._standorte([]) == {}


def test_felder_einer_krankenanstalt() -> None:
    assert zd._typ("Rehabilitation", "", "Haus") == "reha"
    assert zd._typ("Akut", "", "Zentrum für Psychosomatik") == "psychiatrie"
    assert zd._typ("Akut", "Allgemein", "Haus") == "krankenhaus"
    assert (zd._betten("12.0"), zd._betten(""), zd._betten("viele")) == (12, None, None)
    assert zd._plz_ort("Weg 1, 8990 Bad Aussee") == ("8990", "Bad Aussee")
    assert zd._plz_ort("ohne Postleitzahl") == ("", "")
    spalten = zd._kopfspalten(KOPF)
    assert (spalten.nr, spalten.name, spalten.traeger) == (1, 2, 10)
    with pytest.raises(zd.ZusatzFehler):
        zd._kopfspalten(["Bundesland", "Name"])
