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
from pseudonymed.erkennung import lexikon

QUELLE_KLINIKEN_AT = "Kliniken AT (BMASGPK)"
SEITE_KLINIKEN_AT = (
    "https://www.sozialministerium.gv.at/Themen/Gesundheit/Gesundheitssystem/Krankenanstalten/"
    "Krankenanstalten-und-selbststaendige-Ambulatorien-in-Oesterreich/"
    "Krankenanstalten-in-Oesterreich.html")
HINWEIS_KLINIKEN_AT = (
    "Quelle: Bundesministerium für Arbeit, Soziales, Gesundheit, Pflege und Konsumentenschutz"
    " (BMASGPK), Liste der Krankenanstalten in Österreich. Das Ministerium gestattet den"
    " Nachdruck mit Quellenangabe außer zu kommerziellen Zwecken. PseudonyMed lädt die Liste nur"
    " in Ihre lokale Datenbank und gibt sie nicht weiter.")
AGENT = f"PseudonyMed/{__version__} (https://github.com/fre-ms/PseudonyMed)"
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_TEXTLAUF = f"{{{_NS['m']}}}t"
_PLZ_ORT = re.compile(r"(?:^|,\s*)(\d{4})\s+([^,]+?)\s*$")


class ZusatzFehler(RuntimeError):
    """A supplementary data set could not be found or read; the message is meant for the user."""


@dataclass
class Ladebericht:
    """What loading one supplementary source achieved, for the CLI and the status line."""
    quelle: str
    anzahl: int
    ohne_gemeinde: int
    stand: str
    herkunft: str

    def beschreibung(self) -> str:
        """One German sentence with the count, the rows without a Gemeinde, date and origin."""
        teil = f", {self.ohne_gemeinde} ohne Gemeinde" if self.ohne_gemeinde else ""
        return (f"{self.quelle}: {self.anzahl} Einträge geladen{teil}"
                f" (Stand {self.stand or 'unbekannt'}, {self.herkunft}).")


def abrufen(url: str, zeit: int = 60) -> bytes:
    """Plain HTTPS GET with PseudonyMed's own user agent; nothing else is sent."""
    if not url.startswith("https://"):
        raise ZusatzFehler(f"nur https erlaubt: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=zeit) as antwort:
        return antwort.read()


def _gemeinsame_zeichenketten(z: zipfile.ZipFile) -> list[str]:
    """The workbook's shared-string table in index order; empty when the workbook has none."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    return ["".join(t.text or "" for t in si.iter(_TEXTLAUF))
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]


def _blattpfade(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, path of its part in the archive) in workbook order, via the relationships."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target", "")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    blaetter = wb.find("m:sheets", _NS)
    aus: list[tuple[str, str]] = []
    for blatt in (list(blaetter) if blaetter is not None else []):
        ziel = rels.get(blatt.get(f"{{{_NS['r']}}}id"), "").lstrip("/")
        aus.append((blatt.get("name", ""), ziel if ziel.startswith("xl/") else "xl/" + ziel))
    return aus


def _spaltenindex(bezug: str) -> int:
    """Zero-based column of a cell reference such as ``C12``; no letters means column A."""
    spalte = 0
    for ch in re.sub(r"\d", "", bezug):
        spalte = spalte * 26 + ord(ch) - 64
    return max(spalte, 1) - 1


def _zelltext(c: ET.Element, strings: list[str]) -> str:
    """Text of one cell (inline string, shared string or stored value), whitespace collapsed."""
    if c.get("t") == "inlineStr":
        v = "".join(t.text or "" for t in c.iter(_TEXTLAUF))
    else:
        v = c.findtext("m:v", default="", namespaces=_NS)
        if c.get("t") == "s" and v:
            v = strings[int(v)]
    return " ".join(v.split())


def _zeile_aus(row: ET.Element, strings: list[str]) -> list[str]:
    """Cell texts of one row from column A to its last cell; gaps become empty strings."""
    werte = {_spaltenindex(c.get("r", "")): _zelltext(c, strings)
             for c in row.findall("m:c", _NS)}
    return [werte.get(i, "") for i in range(max(werte, default=-1) + 1)]


def _blattzeilen(z: zipfile.ZipFile, pfad: str, strings: list[str]) -> list[list[str]]:
    """All rows of one worksheet part."""
    daten_xml = ET.fromstring(z.read(pfad)).find("m:sheetData", _NS)
    if daten_xml is None:
        return []
    return [_zeile_aus(row, strings) for row in daten_xml.findall("m:row", _NS)]


def xlsx_blaetter(daten: bytes) -> dict[str, list[list[str]]]:
    """Every sheet of a workbook as rows of cell texts (shared and inline strings, values)."""
    z = zipfile.ZipFile(BytesIO(daten))
    strings = _gemeinsame_zeichenketten(z)
    return {name: _blattzeilen(z, pfad, strings) for name, pfad in _blattpfade(z)}


def mappe_finden(html: str, basis: str = SEITE_KLINIKEN_AT) -> str:
    """The link to the current workbook on the ministry's page."""
    links = re.findall(r'href="([^"]+\.xlsx[^"]*)"', html, flags=re.IGNORECASE)
    passend = [u for u in links if re.search(r"verzeichnis|krankenanstalt", urllib.parse.unquote(u),
                                              flags=re.IGNORECASE)] or links
    if not passend:
        raise ZusatzFehler("Auf der Seite des Ministeriums wurde keine Excel-Mappe gefunden.")
    return urllib.parse.urljoin(basis, passend[0].replace("&amp;", "&"))


def _norm(text: str) -> str:
    """Lower-case ASCII form of a name, accents stripped, one space between the words."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c))
                  ).strip()


@dataclass(frozen=True)
class _AtTabellen:
    """The bundled Austrian look-up tables that resolve a site to its Gemeinde code."""
    gemeinden: dict[str, str]  # Gemeinde code → name
    bezirke: dict[str, str]  # Bezirk code → name
    nach_plz: dict[str, str]  # postcode → Gemeinde code with the largest share


def _gebuendelte_zeilen(name: str) -> Iterator[dict[str, str]]:
    """Rows of a bundled gzip CSV, comment lines skipped; nothing when the file is absent."""
    pfad = lexikon.DATEN / name
    if not pfad.exists():
        return
    with gzip.open(pfad, "rt", encoding="utf-8") as f:
        yield from csv.DictReader(x for x in f if not x.startswith("#"))


def _at_namen(zeilen: Iterator[dict[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """Gemeinde code → name and Bezirk code → name from the Austrian rows of the demography."""
    gemeinden: dict[str, str] = {}
    bezirke: dict[str, str] = {}
    nach_ebene = {"gemeinde": gemeinden, "kreis": bezirke}
    for z in zeilen:
        ziel = nach_ebene.get(z["ebene"]) if z["land"] == "AT" else None
        if ziel is not None:
            ziel[z["kennung"]] = z["name"]
    return gemeinden, bezirke


def _anteil(z: dict[str, str]) -> float:
    """Share of a postcode row as a number; a missing or unreadable share counts as zero."""
    try:
        return float(z.get("anteil") or 0)
    except ValueError:
        return 0.0


def _at_plz_gemeinden(zeilen: Iterator[dict[str, str]]) -> dict[str, str]:
    """Postcode → Gemeinde code with the largest share, from the Austrian rows of the table."""
    beste: dict[str, tuple[float, str]] = {}
    for z in zeilen:
        if z.get("land") != "AT":
            continue
        anteil = _anteil(z)
        if anteil > beste.get(z["plz"], (-1.0, ""))[0]:
            beste[z["plz"]] = (anteil, z["gemeinde_kennung"])
    return {plz: kennung for plz, (_a, kennung) in beste.items()}


def _at_tabellen() -> _AtTabellen:
    """Gemeinde code → name, Bezirk code → name (bundled demography, Statistik Austria) and
    postcode → Gemeinde code with the largest share (bundled postcode table, if present)."""
    gemeinden, bezirke = _at_namen(_gebuendelte_zeilen("demografie_dach.csv.gz"))
    return _AtTabellen(gemeinden, bezirke,
                       _at_plz_gemeinden(_gebuendelte_zeilen("plz_dach.csv.gz")))


def _woerter(text: str) -> set[str]:
    """The distinctive words of a place name: normalised, four letters or more, no Stadt/Land."""
    return {w for w in _norm(text).split() if len(w) >= 4 and w not in ("stadt", "land")}


def _wiener_bezirk(plz: str, gemeinden: dict[str, str]) -> str:
    """Gemeinde code of a Viennese district from its postcode (1010 to 1230), if known."""
    if not (re.fullmatch(r"1\d{2}0", plz) and 1 <= int(plz[1:3]) <= 23):
        return ""
    kennung = f"9{int(plz[1:3]):02d}01"
    return kennung if kennung in gemeinden or not gemeinden else ""


def _bezirk_codes(bezirk: str, bezirke: dict[str, str]) -> set[str]:
    """Codes of the Bezirke with exactly that name, else of those sharing a distinctive word."""
    return ({k for k, n in bezirke.items() if _norm(n) == _norm(bezirk)}
            or {k for k, n in bezirke.items() if _woerter(n) & _woerter(bezirk)})


def _bester_worttreffer(ort: str, codes: set[str], gemeinden: dict[str, str]) -> str:
    """The one Gemeinde inside those Bezirke sharing the most words with the place name;
    nothing when no word is shared or two Gemeinden tie for first."""
    woerter = _woerter(ort)
    beste = sorted(((len(woerter & _woerter(n)), k) for k, n in gemeinden.items()
                    if k[:3] in codes), reverse=True)
    if beste and beste[0][0] > 0 and (len(beste) == 1 or beste[1][0] < beste[0][0]):
        return beste[0][1]
    return ""


def _eindeutiger_name(ort: str, gemeinden: dict[str, str]) -> str:
    """Code of the single Gemeinde whose name equals the place name, else nothing."""
    gleich = [k for k, n in gemeinden.items() if _norm(n) == _norm(ort)]
    return gleich[0] if len(gleich) == 1 else ""


def _gemeinde_at(ort: str, plz: str, bezirk: str, tabellen: _AtTabellen) -> str:
    """Gemeinde code for a site without one: Vienna's district from the postcode, the
    postcode table, then the best word match inside the Bezirk, then a unique name."""
    kennung = _wiener_bezirk(plz, tabellen.gemeinden)
    if kennung:
        return kennung
    if plz in tabellen.nach_plz:
        return tabellen.nach_plz[plz]
    codes = _bezirk_codes(bezirk, tabellen.bezirke)
    return (_bester_worttreffer(ort, codes, tabellen.gemeinden)
            or _eindeutiger_name(ort, tabellen.gemeinden))


def _spalten(kopf: list[str], *namen: str) -> list[int | None]:
    """Index of the first header starting with each name (case-insensitive), None if absent."""
    aus: list[int | None] = []
    for name in namen:
        aus.append(next((i for i, k in enumerate(kopf) if k.casefold().startswith(name.casefold())),
                        None))
    return aus


def _wert(zeile: list[str], i: int | None) -> str:
    """The cell at column ``i``, empty when the column is unknown or the row is shorter."""
    return zeile[i] if i is not None and i < len(zeile) else ""


@dataclass(frozen=True)
class _Spalten:
    """Column indices of the sheet "KA gesamt"; None where the workbook lacks a column."""
    nr: int
    name: int
    sektor: int | None
    bereich: int | None
    bezirk: int | None
    adresse: int | None
    betten: int | None
    land: int | None
    traeger: int | None


@dataclass(frozen=True)
class _Standort:
    """One site of a hospital as the raw-data sheet lists it."""
    code: str  # KA number with site suffix, e.g. K901.1
    gemeinde: str  # Gemeinde code, empty when the sheet gives none
    plz: str
    ort: str


@dataclass(frozen=True)
class _Klinik:
    """One hospital of the sheet "KA gesamt" with the fields its lexicon rows carry."""
    nr: str
    name: str
    typ: str
    bezirk: str
    plz: str
    ort: str
    betten: int | None
    bundesland: str
    traeger: str


def _blatt(blaetter: dict[str, list[list[str]]], anfang: str) -> list[list[str]]:
    """Rows of the first sheet whose name starts with ``anfang`` (case-insensitive), else none."""
    return next((z for n, z in blaetter.items() if n.casefold().startswith(anfang)), [])


def _kopfspalten(kopf: list[str]) -> _Spalten:
    """Where the expected columns sit in the header row; KA-Nr. and Bezeichnung must exist."""
    nr, name, sektor, bereich, bezirk, adresse, betten, land = _spalten(
        kopf, "KA-Nr", "Bezeichnung", "Versorgungssektor", "Versorgungsbereich", "Bezirk",
        "Adresse", "Bettenanzahl", "Bundesland")
    traeger = next((i for i, k in enumerate(kopf) if "träger" in k.casefold()
                    and "nr" not in k.casefold()), None)
    if nr is None or name is None:
        raise ZusatzFehler("Die Mappe hat nicht die erwarteten Spalten (KA-Nr., Bezeichnung).")
    return _Spalten(nr, name, sektor, bereich, bezirk, adresse, betten, land, traeger)


def _standort_aus(code: str, z: list[str], s_gem: int | None, s_ort: int | None) -> _Standort:
    """A raw-data row as a site: Gemeinde code from "12345 Name", postcode and place from
    "1234 Ort", the Gemeinde's name standing in when the Ort column gives no place."""
    gem = re.match(r"(\d{5})\s+(.*)", _wert(z, s_gem))
    plz_ort = re.match(r"(\d{4})\s+(.*)", _wert(z, s_ort))
    return _Standort(code, gem.group(1) if gem else "", plz_ort.group(1) if plz_ort else "",
                     plz_ort.group(2) if plz_ort else (gem.group(2) if gem else ""))


def _standorte(roh: list[list[str]]) -> dict[str, list[_Standort]]:
    """Sites per KA number from the raw-data sheet in sheet order; a site code listed several
    times (one row per department) counts once."""
    if not roh:
        return {}
    s_code, s_gem, s_ort = _spalten(roh[0], "KA-Nr", "Gemeinde", "Ort")
    aus: dict[str, list[_Standort]] = {}
    gesehen: set[str] = set()
    for z in roh[1:]:
        code = _wert(z, s_code)
        if not code or code in gesehen:
            continue
        gesehen.add(code)
        aus.setdefault(code.split(".")[0], []).append(_standort_aus(code, z, s_gem, s_ort))
    return aus


def _typ(sektor: str, bereich: str, name: str) -> str:
    """Rehabilitation by sector, psychiatry by area or name, otherwise a general hospital."""
    if "rehab" in sektor.casefold():
        return "reha"
    return "psychiatrie" if "psych" in (bereich + " " + name).casefold() else "krankenhaus"


def _betten(text: str) -> int | None:
    """Bed count from a cell that may hold a decimal ("12.0"); None when empty or unreadable."""
    try:
        return int(float(text)) if text else None
    except ValueError:
        return None


def _plz_ort(adresse: str) -> tuple[str, str]:
    """Postcode and place from the last part of an address, else two empty strings."""
    m = _PLZ_ORT.search(adresse)
    return (m.group(1), m.group(2)) if m else ("", "")


def _klinik_aus(z: list[str], s: _Spalten) -> _Klinik | None:
    """The hospital a row describes, or None for rows without number or name (notes, totals)."""
    nr, name = _wert(z, s.nr), _wert(z, s.name)
    if not nr or not name:
        return None
    plz, ort = _plz_ort(_wert(z, s.adresse))
    return _Klinik(nr, name, _typ(_wert(z, s.sektor), _wert(z, s.bereich), name),
                   _wert(z, s.bezirk), plz, ort, _betten(_wert(z, s.betten)),
                   _wert(z, s.land), _wert(z, s.traeger))


def _standorte_mit_gemeinde(klinik: _Klinik, standorte: dict[str, list[_Standort]],
                            tabellen: _AtTabellen) -> list[_Standort]:
    """The hospital's sites (its own address when the raw data lists none), each with a
    Gemeinde code where one can be found."""
    orte = standorte.get(klinik.nr) or [_Standort(klinik.nr, "", klinik.plz, klinik.ort)]
    return [s if s.gemeinde else dataclasses.replace(
        s, gemeinde=_gemeinde_at(s.ort, s.plz, klinik.bezirk, tabellen)) for s in orte]


def _lexikonzeile(klinik: _Klinik, standort: _Standort) -> lexikon.Zeile:
    """The lexicon row of one site: name as term, ``AT:<code>`` as key, details as JSON."""
    felder = {"typ": klinik.typ, "ort": standort.ort, "plz": standort.plz,
              "gemeinde": standort.gemeinde, "land": "AT", "bundesland": klinik.bundesland,
              "betten": klinik.betten, "traeger": klinik.traeger}
    zusatz = json.dumps({k: v for k, v in felder.items() if v not in ("", None)},
                        ensure_ascii=False, separators=(",", ":"))
    return (klinik.name, "Klinik", lexikon.MERKMAL, f"AT:{standort.code}", zusatz)


def kliniken_at_zeilen(daten: bytes) -> tuple[list[lexikon.Zeile], int]:
    """Lexicon rows from the ministry's workbook: one per site with its Gemeinde code, or one
    per hospital when the workbook lists no site; returns (rows, rows without Gemeinde)."""
    blaetter = xlsx_blaetter(daten)
    gesamt = _blatt(blaetter, "ka gesamt")
    if not gesamt:
        raise ZusatzFehler("Die Mappe hat kein Blatt „KA gesamt“.")
    spalten = _kopfspalten(gesamt[0])
    standorte = _standorte(_blatt(blaetter, "rohdaten"))
    tabellen = _at_tabellen()
    zeilen: list[lexikon.Zeile] = []
    ohne = 0
    for z in gesamt[1:]:
        klinik = _klinik_aus(z, spalten)
        if klinik is None:
            continue
        orte = _standorte_mit_gemeinde(klinik, standorte, tabellen)
        ohne += sum(1 for s in orte if not s.gemeinde)
        zeilen.extend(_lexikonzeile(klinik, s) for s in orte)
    return zeilen, ohne


def lade_kliniken_at(verbindung: sqlite3.Connection, datei: Path | None = None,
                     holen: Callable[[str], bytes] = abrufen) -> Ladebericht:
    """Fetch (or read) the ministry's workbook and replace the source in the lexicon."""
    if datei is None:
        seite = holen(SEITE_KLINIKEN_AT).decode("utf-8", errors="replace")
        url = mappe_finden(seite)
        daten, herkunft = holen(url), url
    else:
        daten, herkunft = Path(datei).read_bytes(), str(datei)
    zeilen, ohne = kliniken_at_zeilen(daten)
    if not zeilen:
        raise ZusatzFehler("Die Mappe enthält keine Krankenanstalten.")
    datum = re.search(r"(\d{4}-\d{2}-\d{2})", urllib.parse.unquote(herkunft))
    stand = datum.group(1) if datum else ""
    verbindung.executescript(lexikon.SCHEMA)
    verbindung.execute("DELETE FROM lexikon WHERE quelle = ?", (QUELLE_KLINIKEN_AT,))
    vorher = verbindung.total_changes
    verbindung.executemany(
        "INSERT OR IGNORE INTO lexikon (begriff, klasse, rolle, quelle, kennung, zusatz)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ((b, k, r, QUELLE_KLINIKEN_AT, ke, zu) for b, k, r, ke, zu in zeilen))
    n = verbindung.total_changes - vorher
    verbindung.execute(
        "INSERT OR REPLACE INTO lexikon_meta (quelle, stand, anzahl, geladen) VALUES (?, ?, ?, ?)",
        (QUELLE_KLINIKEN_AT, f"{stand} {hashlib.sha256(daten).hexdigest()[:16]}".strip(), n,
         dt.datetime.now().isoformat(timespec="seconds")))
    verbindung.commit()
    return Ladebericht(QUELLE_KLINIKEN_AT, n, ohne, stand, herkunft)


def geladene(verbindung: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """User-loaded sources: (source, rows, loaded at)."""
    gebuendelt = {q.name for q in lexikon.QUELLEN}
    try:
        return [(q, n, g) for q, n, g in verbindung.execute(
            "SELECT quelle, anzahl, geladen FROM lexikon_meta ORDER BY quelle")
            if q not in gebuendelt]
    except sqlite3.OperationalError:
        return []
