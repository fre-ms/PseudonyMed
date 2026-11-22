"""Reference data the user loads on purpose, straight from the publisher.

Some lists may not travel with PseudonyMed under its free licence.  The Austrian ministry of
health permits reprinting its directory of hospitals "except for commercial purposes", which
rules out bundling it in an AGPL repository that anyone may sell.  PseudonyMed therefore does not
ship it: on the user's request it fetches the current workbook from the ministry and writes
it into the user's own lexicon under a source of its own, which the reloading of the bundled
data leaves alone.  Nothing is passed on.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pseudonymed import __version__
from pseudonymed.detection import lexicon

SOURCE_HOSPITALS_AT = "Kliniken AT (BMASGPK)"
PAGE_HOSPITALS_AT = (
    "https://www.sozialministerium.gv.at/Themen/Gesundheit/Gesundheitssystem/Krankenanstalten/"
    "Krankenanstalten-und-selbststaendige-Ambulatorien-in-Oesterreich/"
    "Krankenanstalten-in-Oesterreich.html")
CLINICS_AT_NOTE = (
    "Quelle: Bundesministerium für Arbeit, Soziales, Gesundheit, Pflege und Konsumentenschutz"
    " (BMASGPK), Liste der Krankenanstalten in Österreich. Das Ministerium gestattet den"
    " Nachdruck mit Quellenangabe außer zu kommerziellen Zwecken. PseudonyMed lädt die Liste nur"
    " in Ihre lokale Datenbank und gibt sie nicht weiter.")
AGENT = f"PseudonyMed/{__version__} (https://github.com/fre-ms/PseudonyMed)"
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_TEXT_RUN = f"{{{_NS['m']}}}t"
_POSTCODE_PLACE = re.compile(r"(?:^|,\s*)(\d{4})\s+([^,]+?)\s*$")


class ExtraDataError(RuntimeError):
    """A supplementary data set could not be found or read; the message is meant for the user."""


@dataclass
class LoadReport:
    """What loading one supplementary source achieved, for the CLI and the status line."""
    source: str
    count: int
    without_municipality: int
    state: str
    origin: str

    def description(self) -> str:
        """One German sentence with the count, the rows without a Gemeinde, date and origin."""
        part = f", {self.without_municipality} ohne Gemeinde" if self.without_municipality else ""
        return (f"{self.source}: {self.count} Einträge geladen{part}"
                f" (Stand {self.state or 'unbekannt'}, {self.origin}).")


def fetch(url: str, time: int = 60) -> bytes:
    """Plain HTTPS GET with PseudonyMed's own user agent; nothing else is sent."""
    if not url.startswith("https://"):
        raise ExtraDataError(f"nur https erlaubt: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=time) as response:
        return response.read()


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    """The workbook's shared-string table in index order; empty when the workbook has none."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    return ["".join(t.text or "" for t in si.iter(_TEXT_RUN))
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]


def _sheet_paths(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, path of its part in the archive) in workbook order, via the relationships."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target", "")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheets = wb.find("m:sheets", _NS)
    out: list[tuple[str, str]] = []
    for sheet in (list(sheets) if sheets is not None else []):
        target = rels.get(sheet.get(f"{{{_NS['r']}}}id"), "").lstrip("/")
        out.append((sheet.get("name", ""), target if target.startswith("xl/") else "xl/" + target))
    return out


def _column_index(ref: str) -> int:
    """Zero-based column of a cell reference such as ``C12``; no letters means column A."""
    column = 0
    for ch in re.sub(r"\d", "", ref):
        column = column * 26 + ord(ch) - 64
    return max(column, 1) - 1


def _cell_text(c: ET.Element, strings: list[str]) -> str:
    """Text of one cell (inline string, shared string or stored value), whitespace collapsed."""
    if c.get("t") == "inlineStr":
        v = "".join(t.text or "" for t in c.iter(_TEXT_RUN))
    else:
        v = c.findtext("m:v", default="", namespaces=_NS)
        if c.get("t") == "s" and v:
            v = strings[int(v)]
    return " ".join(v.split())


def _row_from(row: ET.Element, strings: list[str]) -> list[str]:
    """Cell texts of one row from column A to its last cell; gaps become empty strings."""
    values = {_column_index(c.get("r", "")): _cell_text(c, strings)
              for c in row.findall("m:c", _NS)}
    return [values.get(i, "") for i in range(max(values, default=-1) + 1)]


def _sheet_rows(z: zipfile.ZipFile, path: str, strings: list[str]) -> list[list[str]]:
    """All rows of one worksheet part."""
    sheet_data = ET.fromstring(z.read(path)).find("m:sheetData", _NS)
    if sheet_data is None:
        return []
    return [_row_from(row, strings) for row in sheet_data.findall("m:row", _NS)]


def xlsx_sheets(data: bytes) -> dict[str, list[list[str]]]:
    """Every sheet of a workbook as rows of cell texts (shared and inline strings, values)."""
    z = zipfile.ZipFile(BytesIO(data))
    strings = _shared_strings(z)
    return {name: _sheet_rows(z, path, strings) for name, path in _sheet_paths(z)}


def find_workbook(html: str, base: str = PAGE_HOSPITALS_AT) -> str:
    """The link to the current workbook on the ministry's page."""
    hrefs = re.findall(r'href="([^"]+\.xlsx[^"]*)"', html, flags=re.IGNORECASE)
    matching = [u for u in hrefs if re.search(r"verzeichnis|krankenanstalt",
                                              urllib.parse.unquote(u),
                                              flags=re.IGNORECASE)] or hrefs
    if not matching:
        raise ExtraDataError("Auf der Seite des Ministeriums wurde keine Excel-Mappe gefunden.")
    return urllib.parse.urljoin(base, matching[0].replace("&amp;", "&"))


def _norm(text: str) -> str:
    """Lower-case ASCII form of a name, accents stripped, one space between the words."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c))
                  ).strip()


@dataclass(frozen=True)
class _AtTables:
    """The bundled Austrian look-up tables that resolve a site to its Gemeinde code."""
    municipalities: dict[str, str]  # Gemeinde code → name
    districts: dict[str, str]  # Bezirk code → name
    by_postcode: dict[str, str]  # postcode → Gemeinde code with the largest share


def _bundled_rows(name: str) -> Iterator[dict[str, str]]:
    """Rows of a bundled gzip CSV, comment lines skipped; nothing when the file is absent."""
    path = lexicon.DATA / name
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        yield from csv.DictReader(x for x in f if not x.startswith("#"))


def _at_names(lines: Iterator[dict[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """Gemeinde code → name and Bezirk code → name from the Austrian rows of the demography."""
    municipalities: dict[str, str] = {}
    districts: dict[str, str] = {}
    by_level = {"gemeinde": municipalities, "kreis": districts}
    for z in lines:
        target = by_level.get(z["ebene"]) if z["land"] == "AT" else None
        if target is not None:
            target[z["kennung"]] = z["name"]
    return municipalities, districts


def _share(z: dict[str, str]) -> float:
    """Share of a postcode row as a number; a missing or unreadable share counts as zero."""
    try:
        return float(z.get("anteil") or 0)
    except ValueError:
        return 0.0


def _at_postcode_municipalities(lines: Iterator[dict[str, str]]) -> dict[str, str]:
    """Postcode → Gemeinde code with the largest share, from the Austrian rows of the table."""
    best: dict[str, tuple[float, str]] = {}
    for z in lines:
        if z.get("land") != "AT":
            continue
        share = _share(z)
        if share > best.get(z["plz"], (-1.0, ""))[0]:
            best[z["plz"]] = (share, z["gemeinde_kennung"])
    return {postcode: code for postcode, (_a, code) in best.items()}


def _at_tables() -> _AtTables:
    """Gemeinde code → name, Bezirk code → name (bundled demography, Statistik Austria) and
    postcode → Gemeinde code with the largest share (bundled postcode table, if present)."""
    municipalities, districts = _at_names(_bundled_rows("demografie_dach.csv.gz"))
    return _AtTables(municipalities, districts,
                     _at_postcode_municipalities(_bundled_rows("plz_dach.csv.gz")))


def _words(text: str) -> set[str]:
    """The distinctive words of a place name: normalised, four letters or more, no Stadt/Land."""
    return {w for w in _norm(text).split() if len(w) >= 4 and w not in ("stadt", "land")}


def _vienna_district(postcode: str, municipalities: dict[str, str]) -> str:
    """Gemeinde code of a Viennese district from its postcode (1010 to 1230), if known."""
    if not (re.fullmatch(r"1\d{2}0", postcode) and 1 <= int(postcode[1:3]) <= 23):
        return ""
    code = f"9{int(postcode[1:3]):02d}01"
    return code if code in municipalities or not municipalities else ""


def _district_codes(district: str, districts: dict[str, str]) -> set[str]:
    """Codes of the Bezirke with exactly that name, else of those sharing a distinctive word."""
    return ({k for k, n in districts.items() if _norm(n) == _norm(district)}
            or {k for k, n in districts.items() if _words(n) & _words(district)})


def _best_word_match(place: str, codes: set[str], municipalities: dict[str, str]) -> str:
    """The one Gemeinde inside those Bezirke sharing the most words with the place name;
    nothing when no word is shared or two Gemeinden tie for first."""
    words = _words(place)
    best = sorted(((len(words & _words(n)), k) for k, n in municipalities.items()
                   if k[:3] in codes), reverse=True)
    if best and best[0][0] > 0 and (len(best) == 1 or best[1][0] < best[0][0]):
        return best[0][1]
    return ""


def _unique_name(place: str, municipalities: dict[str, str]) -> str:
    """Code of the single Gemeinde whose name equals the place name, else nothing."""
    same = [k for k, n in municipalities.items() if _norm(n) == _norm(place)]
    return same[0] if len(same) == 1 else ""


def _municipality_at(place: str, postcode: str, district: str, tables: _AtTables) -> str:
    """Gemeinde code for a site without one: Vienna's district from the postcode, the
    postcode table, then the best word match inside the Bezirk, then a unique name."""
    code = _vienna_district(postcode, tables.municipalities)
    if code:
        return code
    if postcode in tables.by_postcode:
        return tables.by_postcode[postcode]
    codes = _district_codes(district, tables.districts)
    return (_best_word_match(place, codes, tables.municipalities)
            or _unique_name(place, tables.municipalities))


def _columns(header: list[str], *names: str) -> list[int | None]:
    """Index of the first header starting with each name (case-insensitive), None if absent."""
    out: list[int | None] = []
    for name in names:
        out.append(next((i for i,
                         k in enumerate(header) if k.casefold().startswith(name.casefold())),
                        None))
    return out


def _value(line: list[str], i: int | None) -> str:
    """The cell at column ``i``, empty when the column is unknown or the row is shorter."""
    return line[i] if i is not None and i < len(line) else ""


@dataclass(frozen=True)
class _Columns:
    """Column indices of the sheet "KA gesamt"; None where the workbook lacks a column."""
    num: int
    name: int
    sector: int | None
    care_area: int | None
    district: int | None
    address: int | None
    beds: int | None
    country: int | None
    owner: int | None


@dataclass(frozen=True)
class _Site:
    """One site of a hospital as the raw-data sheet lists it."""
    code: str  # KA number with site suffix, e.g. K901.1
    municipality: str  # Gemeinde code, empty when the sheet gives none
    postcode: str
    place: str


@dataclass(frozen=True)
class _Hospital:
    """One hospital of the sheet "KA gesamt" with the fields its lexicon rows carry."""
    num: str
    name: str
    type_name: str
    district: str
    postcode: str
    place: str
    beds: int | None
    federal_state: str
    owner: str


def _sheet(sheets: dict[str, list[list[str]]], begin: str) -> list[list[str]]:
    """Rows of the first sheet whose name starts with ``begin`` (case-insensitive), else none."""
    return next((z for n, z in sheets.items() if n.casefold().startswith(begin)), [])


def _header_columns(header: list[str]) -> _Columns:
    """Where the expected columns sit in the header row; KA-Nr. and Bezeichnung must exist."""
    num, name, sector, care_area, district, address, beds, country = _columns(
        header, "KA-Nr", "Bezeichnung", "Versorgungssektor", "Versorgungsbereich", "Bezirk",
        "Adresse", "Bettenanzahl", "Bundesland")
    owner = next((i for i, k in enumerate(header) if "träger" in k.casefold()
                  and "nr" not in k.casefold()), None)
    if num is None or name is None:
        raise ExtraDataError("Die Mappe hat nicht die erwarteten Spalten (KA-Nr., Bezeichnung).")
    return _Columns(num, name, sector, care_area, district, address, beds, country, owner)


def _site_from(code: str, z: list[str], s_mun: int | None, s_place: int | None) -> _Site:
    """A raw-data row as a site: Gemeinde code from "12345 Name", postcode and place from
    "1234 Ort", the Gemeinde's name standing in when the Ort column gives no place."""
    mun = re.match(r"(\d{5})\s+(.*)", _value(z, s_mun))
    postcode_place = re.match(r"(\d{4})\s+(.*)", _value(z, s_place))
    return _Site(code, mun.group(1) if mun else "",
                 postcode_place.group(1) if postcode_place else "",
                 postcode_place.group(2) if postcode_place else (mun.group(2) if mun else ""))


def _sites(raw: list[list[str]]) -> dict[str, list[_Site]]:
    """Sites per KA number from the raw-data sheet in sheet order; a site code listed several
    times (one row per department) counts once."""
    if not raw:
        return {}
    s_code, s_mun, s_place = _columns(raw[0], "KA-Nr", "Gemeinde", "Ort")
    out: dict[str, list[_Site]] = {}
    seen: set[str] = set()
    for z in raw[1:]:
        code = _value(z, s_code)
        if not code or code in seen:
            continue
        seen.add(code)
        out.setdefault(code.split(".")[0], []).append(_site_from(code, z, s_mun, s_place))
    return out


def _hospital_type(sector: str, care_area: str, name: str) -> str:
    """Rehabilitation by sector, psychiatry by area or name, otherwise a general hospital."""
    if "rehab" in sector.casefold():
        return "reha"
    return "psychiatrie" if "psych" in (care_area + " " + name).casefold() else "krankenhaus"


def _beds(text: str) -> int | None:
    """Bed count from a cell that may hold a decimal ("12.0"); None when empty or unreadable."""
    try:
        return int(float(text)) if text else None
    except ValueError:
        return None


def _postcode_place(address: str) -> tuple[str, str]:
    """Postcode and place from the last part of an address, else two empty strings."""
    m = _POSTCODE_PLACE.search(address)
    return (m.group(1), m.group(2)) if m else ("", "")


def _hospital_from(z: list[str], s: _Columns) -> _Hospital | None:
    """The hospital a row describes, or None for rows without number or name (notes, totals)."""
    num, name = _value(z, s.num), _value(z, s.name)
    if not num or not name:
        return None
    postcode, place = _postcode_place(_value(z, s.address))
    return _Hospital(num, name, _hospital_type(_value(z, s.sector), _value(z, s.care_area), name),
                     _value(z, s.district), postcode, place, _beds(_value(z, s.beds)),
                     _value(z, s.country), _value(z, s.owner))


def _sites_with_municipality(hospital: _Hospital, sites: dict[str, list[_Site]],
                             tables: _AtTables) -> list[_Site]:
    """The hospital's sites (its own address when the raw data lists none), each with a
    Gemeinde code where one can be found."""
    places = sites.get(hospital.num) or [_Site(hospital.num, "", hospital.postcode, hospital.place)]
    return [s if s.municipality else dataclasses.replace(
        s, municipality=_municipality_at(s.place, s.postcode, hospital.district,
                                         tables)) for s in places]


def _lexicon_row(hospital: _Hospital, site: _Site) -> lexicon.Row:
    """The lexicon row of one site: name as term, ``AT:<code>`` as key, details as JSON."""
    fields = {"typ": hospital.type_name, "ort": site.place, "plz": site.postcode,
              "gemeinde": site.municipality, "land": "AT", "bundesland": hospital.federal_state,
              "betten": hospital.beds, "traeger": hospital.owner}
    extra = json.dumps({k: v for k, v in fields.items() if v not in ("", None)},
                       ensure_ascii=False, separators=(",", ":"))
    return (hospital.name, "Klinik", lexicon.FEATURE, f"AT:{site.code}", extra)


def hospitals_at_rows(data: bytes) -> tuple[list[lexicon.Row], int]:
    """Lexicon rows from the ministry's workbook: one per site with its Gemeinde code, or one
    per hospital when the workbook lists no site; returns (rows, rows without Gemeinde)."""
    sheets = xlsx_sheets(data)
    total = _sheet(sheets, "ka gesamt")
    if not total:
        raise ExtraDataError("Die Mappe hat kein Blatt „KA gesamt“.")
    columns = _header_columns(total[0])
    sites = _sites(_sheet(sheets, "rohdaten"))
    tables = _at_tables()
    lines: list[lexicon.Row] = []
    without = 0
    for z in total[1:]:
        hospital = _hospital_from(z, columns)
        if hospital is None:
            continue
        places = _sites_with_municipality(hospital, sites, tables)
        without += sum(1 for s in places if not s.municipality)
        lines.extend(_lexicon_row(hospital, s) for s in places)
    return lines, without


def load_clinics_at(connection: sqlite3.Connection, file: Path | None = None,
                    fetcher: Callable[[str], bytes] = fetch) -> LoadReport:
    """Fetch (or read) the ministry's workbook and replace the source in the lexicon."""
    if file is None:
        page = fetcher(PAGE_HOSPITALS_AT).decode("utf-8", errors="replace")
        url = find_workbook(page)
        data, origin = fetcher(url), url
    else:
        data, origin = Path(file).read_bytes(), str(file)
    lines, without = hospitals_at_rows(data)
    if not lines:
        raise ExtraDataError("Die Mappe enthält keine Krankenanstalten.")
    date = re.search(r"(\d{4}-\d{2}-\d{2})", urllib.parse.unquote(origin))
    state = date.group(1) if date else ""
    connection.executescript(lexicon.SCHEMA)
    connection.execute("DELETE FROM lexikon WHERE quelle = ?", (SOURCE_HOSPITALS_AT,))
    before = connection.total_changes
    connection.executemany(
        "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ((b, k, r, SOURCE_HOSPITALS_AT, code, extra) for b, k, r, code, extra in lines))
    n = connection.total_changes - before
    connection.execute(
        "INSERT OR REPLACE INTO lexikon_meta (quelle, stand, anzahl, geladen) VALUES (?, ?, ?, ?)",
        (SOURCE_HOSPITALS_AT, f"{state} {hashlib.sha256(data).hexdigest()[:16]}".strip(), n,
         dt.datetime.now().isoformat(timespec="seconds")))
    connection.commit()
    return LoadReport(SOURCE_HOSPITALS_AT, n, without, state, origin)


def loaded_sources(connection: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """User-loaded sources: (source, rows, loaded at)."""
    bundled = {q.name for q in lexicon.SOURCES}
    try:
        return [(q, n, g) for q, n, g in connection.execute(
            "SELECT quelle, anzahl, geladen FROM lexikon_meta ORDER BY quelle")
            if q not in bundled]
    except sqlite3.OperationalError:
        return []
