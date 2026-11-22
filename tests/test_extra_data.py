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

from pseudonymed.detection import extra_data as extra
from pseudonymed.detection import lexicon
from tests.test_gui import qapp  # noqa: F401

HEADER = ["Bundesland", "KA-Nr.", "Bezeichnung", "Kurzbezeichnung", "Versorgungssektor",
          "Versorgungsbereich", "Bezirk", "Adresse", "Bettenanzahl", "Träger-Nr.",
          "Trägerbezeichnung"]
HOSPITALS = [
    ["Burgenland", "K901", "Musterklinik am Bach", "Bach KL", "Akutversorgung",
     "Allgemeinversorgung", "Eisenstadt(Stadt)", "Hauptstraße 1, 7000 Eisenstadt", "120", "T1",
     "Musterträger GmbH"],
    ["Burgenland", "K902", "Reha-Zentrum Beispielhof", "Beispielhof REHA", "Rehabilitation",
     "Bewegungsapparat", "Oberwart", "Kurweg 2, 7431 Bad Tatzmannsdorf", "80", "T2",
     "Reha Muster GmbH"],
    ["Wien", "K903", "Beispielinstitut für Psychiatrie", "Beispiel PSY", "Akutversorgung",
     "Psychiatrie", "Wien 23.,Liesing", "Gasse 3, 1230 Wien", "200", "T3", "Verein Muster"],
]
SITES_HEADER = ["KA-Nr., Standort-Code", "Standort Bezeichnung", "Gemeinde", "Ort", "Straße",
                "ÖSG Abteilung", "Bettenanzahl"]
SITES = [
    ["K901.1", "Bach KL", "10101 Eisenstadt", "7000 Eisenstadt", "Hauptstraße 1",
     "03 Chirurgie", "60"],
    ["K901.1", "Bach KL", "10101 Eisenstadt", "7000 Eisenstadt", "Hauptstraße 1",
     "05 Innere Medizin", "60"],
    ["K901.2", "Bach KL Nord", "10901 Bad Tatzmannsdorf", "7431 Bad Tatzmannsdorf",
     "Nordweg 5", "05 Innere Medizin", "10"],
]


def _sheet(lines: list[list[str]]) -> str:
    parts = []
    for r, line in enumerate(lines, 1):
        cells = "".join(
            f'<c r="{chr(65 + i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
            for i, v in enumerate(line))
        parts.append(f'<row r="{r}">{cells}</row>')
    return ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(parts)}</sheetData></worksheet>')


def workbook(path: Path) -> Path:
    ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   f'{ns}><sheets><sheet name="KA gesamt" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Rohdaten Standorte" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", _sheet([HEADER] + HOSPITALS))
        z.writestr("xl/worksheets/sheet2.xml", _sheet([SITES_HEADER] + SITES))
    return path


def _rows(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {k: (b, z) for b, k, z in con.execute(
        "SELECT begriff, kennung, zusatz FROM lexikon WHERE quelle = ?",
        (extra.SOURCE_HOSPITALS_AT, ))}


def test_workbook_is_read_and_mapped(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "p.sqlite")
    report = extra.load_clinics_at(con, file=workbook(tmp_path / "KA-Verzeichnis 2026-09-18.xlsx"))
    assert report.count == 4 and report.without_municipality == 0 and report.state == "2026-09-18"
    z = _rows(con)
    assert set(z) == {"AT:K901.1", "AT:K901.2", "AT:K902", "AT:K903"}
    assert '"gemeinde":"10101"' in z["AT:K901.1"][1]
    assert '"gemeinde":"10901"' in z["AT:K901.2"][1]  # second site, own municipality
    assert '"gemeinde":"10901"' in z["AT:K902"][1] and '"typ":"reha"' in z["AT:K902"][1]
    assert '"gemeinde":"92301"' in z["AT:K903"][1] and '"typ":"psychiatrie"' in z["AT:K903"][1]
    assert extra.loaded_sources(con) == [(extra.SOURCE_HOSPITALS_AT, 4,
                                          extra.loaded_sources(con)[0][2])]


def test_find_workbook_on_the_page() -> None:
    html = ('<a href="/dam/jcr:abc/Logo.png">Logo</a> <a href="/dam/jcr:123/'
            'KA-Verzeichnis%202026-09-18.xlsx">Liste der Krankenanstalten</a>')
    url = extra.find_workbook(html)
    assert url.startswith("https://www.sozialministerium.gv.at/dam/jcr:123/")
    with pytest.raises(extra.ExtraDataError):
        extra.find_workbook("<p>keine Mappe</p>")


def test_survives_reloading_the_bundled_data(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSEUDONYMED_LEXIKON", "1")
    con = sqlite3.connect(tmp_path / "p.sqlite")
    extra.load_clinics_at(con, file=workbook(tmp_path / "m.xlsx"))
    monkeypatch.setattr(lexicon, "SOURCES", (lexicon.SOURCES[2],))  # a small bundled source
    lexicon.ensure(con, force=True)
    assert len(_rows(con)) == 4


def test_window_loads_from_the_ministry(qapp: QApplication, tmp_path: Path,  # noqa: F811
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    data = workbook(tmp_path / "m.xlsx").read_bytes()
    fetches: list[str] = []

    def fetcher(url: str) -> bytes:
        fetches.append(url)
        return (b'<a href="/dam/KA-Verzeichnis%202026-09-18.xlsx">Liste</a>'
                if url == extra.PAGE_HOSPITALS_AT else data)

    window = MainWindow(Session(store_path=tmp_path / "p.sqlite"))
    page = window.database
    page.synchronous = True
    page.fetcher = fetcher
    report = page.load_clinics_at()
    assert report is not None and report.count == 4 and len(fetches) == 2
    assert "4 Einträge geladen" in page.extra_status.text()
    lines = dict(page.refresh().lines())
    assert lines["Zusatzdaten (selbst geladen)"].startswith(
        f"{extra.SOURCE_HOSPITALS_AT}: 4 Einträge")


def test_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pseudonymed.cli import app

    monkeypatch.setenv("PSEUDONYMED_DATENORDNER", str(tmp_path / "daten"))
    result = CliRunner().invoke(app, ["datenbank", "zusatzdaten", "kliniken-at", "--datei",
                                      str(workbook(tmp_path / "m.xlsx")), "--speicher",
                                      str(tmp_path / "p.sqlite")])
    assert result.exit_code == 0, result.output
    assert "außer zu kommerziellen Zwecken" in result.output and "4 Einträge" in result.output
    wrong = CliRunner().invoke(app, ["datenbank", "zusatzdaten", "unbekannt"])
    assert wrong.exit_code != 0


def test_broken_file(tmp_path: Path) -> None:
    file = tmp_path / "kaputt.xlsx"
    file.write_bytes(b"kein zip")
    with pytest.raises(zipfile.BadZipFile):
        extra.load_clinics_at(sqlite3.connect(":memory:"), file=file)
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main"><sheets/></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"/>')
    (tmp_path / "leer.xlsx").write_bytes(empty.getvalue())
    with pytest.raises(extra.ExtraDataError):
        extra.load_clinics_at(sqlite3.connect(":memory:"), file=tmp_path / "leer.xlsx")


def test_cells_from_shared_strings_and_values() -> None:
    """Shared strings, stored numbers, a gap, a cell beyond column Z and an empty sheet."""
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
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
    sheets = extra.xlsx_sheets(buffer.getvalue())
    assert sheets["Daten"][0] == ["Haus", "", "12.0"] + [""] * 23 + ["am Bach"]
    assert sheets["Daten"][1] == ["", "zwei Worte"]
    assert sheets["Leer"] == []


def test_municipality_without_code_is_derived() -> None:
    """Vienna from the postcode, the postcode table, the word match inside the Bezirk, a
    unique name, and nothing when the evidence is ambiguous."""
    tab = extra._AtTables(
        municipalities={"92301": "Wien 23., Liesing", "10101": "Eisenstadt", "60101": "Graz",
                        "61207": "Bad Aussee", "61235": "Bad Mitterndorf", "70101": "Innsbruck"},
        districts={"101": "Eisenstadt(Stadt)", "601": "Graz(Stadt)", "612": "Liezen",
                   "701": "Innsbruck-Stadt"},
        by_postcode={"8010": "60101"})
    assert extra._municipality_at("Wien", "1230", "", tab) == "92301"
    assert extra._municipality_at("", "1100", "", extra._AtTables({}, {}, {})) == "91001"
    assert extra._municipality_at("", "1240", "", tab) == ""  # no 24th district
    assert extra._municipality_at("Nirgendwo", "8010", "", tab) == "60101"  # postcode table
    assert extra._municipality_at("Bad Aussee", "", "Liezen", tab) == "61207"  # word match
    assert extra._municipality_at("Bad", "", "Liezen", tab) == ""  # "bad" is too short to count
    assert extra._municipality_at("Bad Aussee Mitterndorf", "", "Liezen", tab) == ""  # a tie
    assert extra._municipality_at("Graz", "", "Graz(Stadt)", tab) == "60101"  # exact Bezirk name
    assert extra._municipality_at("Innsbruck", "", "", tab) == "70101"  # unique name, no Bezirk
    assert extra._municipality_at("Unbekannt", "9999", "Liezen", tab) == ""


def test_tables_from_lines() -> None:
    """Only Austrian rows count; the postcode keeps the Gemeinde with the largest share."""
    demographics = [{"land": "AT", "ebene": "kreis", "kennung": "612", "name": "Liezen"},
                    {"land": "AT", "ebene": "gemeinde", "kennung": "61207", "name": "Bad Aussee"},
                    {"land": "AT", "ebene": "staat", "kennung": "AT", "name": "Österreich"},
                    {"land": "DE", "ebene": "gemeinde", "kennung": "09761000", "name": "Augsburg"}]
    assert extra._at_names(iter(demographics)) == ({"61207": "Bad Aussee"}, {"612": "Liezen"})
    postcode = [{"land": "AT", "plz": "8990", "gemeinde_kennung": "61207", "anteil": "0.4"},
                {"land": "AT", "plz": "8990", "gemeinde_kennung": "61235", "anteil": "0.6"},
                {"land": "AT", "plz": "8991", "gemeinde_kennung": "61235", "anteil": "x"},
                {"land": "AT", "plz": "8991", "gemeinde_kennung": "61207", "anteil": "0.1"},
                {"land": "DE", "plz": "8990", "gemeinde_kennung": "0", "anteil": "1"}]
    assert extra._at_postcode_municipalities(iter(postcode)) == {"8990": "61235", "8991": "61207"}


def test_sites_from_raw_data() -> None:
    """One site per code despite one row per department; the Gemeinde's name stands in when
    the Ort column has no postcode; rows without a code are skipped."""
    raw = [SITES_HEADER,
           ["K1.1", "A", "10101 Eisenstadt", "7000 Eisenstadt", "", "01", "1"],
           ["K1.1", "A", "10101 Eisenstadt", "7000 Eisenstadt", "", "02", "1"],
           ["K1.2", "B", "60101 Graz", "Graz", "", "", ""],
           ["", "C", "", "", "", "", ""],
           ["K2", "D", "", "8990 Bad Aussee", "", "", ""]]
    assert extra._sites(raw) == {
        "K1": [extra._Site("K1.1", "10101", "7000", "Eisenstadt"),
               extra._Site("K1.2", "60101", "", "Graz")],
        "K2": [extra._Site("K2", "", "8990", "Bad Aussee")]}
    assert extra._sites([]) == {}


def test_fields_of_a_hospital() -> None:
    assert extra._hospital_type("Rehabilitation", "", "Haus") == "reha"
    assert extra._hospital_type("Akut", "", "Zentrum für Psychosomatik") == "psychiatrie"
    assert extra._hospital_type("Akut", "Allgemein", "Haus") == "krankenhaus"
    assert (extra._beds("12.0"), extra._beds(""), extra._beds("viele")) == (12, None, None)
    assert extra._postcode_place("Weg 1, 8990 Bad Aussee") == ("8990", "Bad Aussee")
    assert extra._postcode_place("ohne Postleitzahl") == ("", "")
    columns = extra._header_columns(HEADER)
    assert (columns.num, columns.name, columns.owner) == (1, 2, 10)
    with pytest.raises(extra.ExtraDataError):
        extra._header_columns(["Bundesland", "Name"])
