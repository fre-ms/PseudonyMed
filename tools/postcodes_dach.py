"""Build ``pseudonymed/daten/plz_dach.csv.gz``: postcode -> municipality -> district, DACH.

The residual-risk check estimates the catchment area of the practice (the districts its
patients come from) locally from the postcodes in the address book.  This file maps every
postcode of Germany, Austria and Switzerland to the municipalities it covers, with the
district of each municipality and the estimated share of the postcode's residents living
there.  The keys match ``gemeinden_de.csv.gz`` and ``demografie_dach.csv.gz``.

Sources and licences
--------------------
Germany (DE): street directory of the OpenPLZ API project, file
``src/de/osm/streets.updated.csv`` of https://github.com/openpotato/openplzapi.data, an
extract from OpenStreetMap that lists every named street with the postcode area and the
municipality (8-digit AGS) its centre lies in.  Licence: Open Database License (ODbL) 1.0,
"© OpenStreetMap contributors".  Municipality keys (ARS), names and populations come from
``gemeinden_de.csv.gz`` (Destatis, Datenlizenz Deutschland - Namensnennung - 2.0); keys that
changed after the OSM extract are followed through the Destatis lists of territorial changes
("Gebietsänderungen", ``DE_CHANGES``; streets of dissolved uninhabited areas count as
streets, not as residential units).  Populated municipalities without any OSM street get one
unit at the postcode of their seat, and postcodes of a municipal seat for which OSM has no
area (mostly PO box or large-customer postcodes) are added from the same directory.

Austria (AT): BEV - Bundesamt für Eich- und Vermessungswesen, "Adresse Relationale Tabellen -
Stichtagsdaten" (every address with postcode, municipality and census district; buildings
with their prevailing use), https://data.bev.gv.at.  Licence: CC BY 4.0, attribution
"© Österreichisches Adressregister, Stichtagsdaten vom DD.MM.YYYY".  Vienna is split into
its 23 districts (Gemeindebezirke 90101-92301, districts 901-923) by the census district
(Zählsprengel) of each address.

Switzerland (CH): Federal Office of Topography swisstopo, "Amtliches
Gebäudeadressverzeichnis" (every building address with postcode, BFS municipality number,
building category and status) and, as cross-check and fallback, "Amtliches
Ortschaftenverzeichnis mit Postleitzahl und Perimeter", both from https://data.geo.admin.ch.
Terms: swisstopo OGD - free use including commercial use, source "Bundesamt für
Landestopografie swisstopo" compulsory
(https://www.swisstopo.admin.ch/de/nutzungsbedingungen-kostenlose-geodaten-und-geodienste).
Districts and mergers from the BFS "Amtliches Gemeindeverzeichnis der Schweiz" (REST service
https://www.agvchapp.bfs.admin.ch; BFS terms OPEN BY).  Municipalities are written in the
territorial state of ``demografie_dach.csv.gz`` (STATPOP reference date), so that every key
can be joined: an address of a commune merged since then goes to the former commune named
like its postal locality, else to the surviving former commune, else to all former communes
by population.

Share (``share``)
------------------
For a postcode P and a municipality G the residents of P living in G are estimated as
``population(G) * units(P, G) / units(G)``: the population of the municipality, spread over
its postcodes in proportion to its residential units.  Units are residential addresses in AT
(addresses with a building of use 01-03) and CH (building categories residential,
other_residential and partly_residential, status real), and named streets in DE.
``share`` is that estimate divided by its sum over all municipalities of P, written with four
decimals and summing to exactly 1 per postcode; pairs without estimated residents and shares
below 0.00005 are dropped.  If no
municipality of P has residents and residential units, all addresses (DE: streets) weighted by
population are used, then unweighted addresses, then the fallback weights (DE seat
postcodes: population; CH perimeter-only postcodes: swisstopo address share).
Populations: DE ``gemeinden_de.csv.gz``, AT and CH municipality totals of
``demografie_dach.csv.gz``.

Output
------
A comment line ``# Quelle: ...; Lizenz: ...; Stand: ...``, then the columns
``land,plz,ort,gemeinde_kennung,gemeinde_name,kreis_kennung,anteil``.  ``gemeinde_kennung``
is the 12-digit ARS (DE), the 5-digit Gemeindekennziffer (AT, Vienna by district) or the BFS
number (CH); ``kreis_kennung`` the 5-digit AGS of the Kreis (DE), the 3-digit political
district (AT) or the BFS district number (CH).  ``place`` is the most frequent postal locality
of the pair (DE: OSM locality, AT: Zustellort, CH: locality of the postcode label).  gzip
without time stamp, so equal downloads give a byte-identical file.  Because the German rows
are derived from OpenStreetMap, the file as a whole is distributed under the ODbL 1.0.

Usage: python -I tools/postcodes_dach.py [--raw FOLDER] [--target FILE]

With ``--raw`` the downloads are kept in (and re-read from) ``FOLDER/<source>/``.  The
certificate of data.bev.gv.at chains to "HARICA TLS RSA Root CA 2021", which older CA bundles
(e.g. macOS ``/etc/ssl/cert.pem``) lack; then point ``SSL_CERT_FILE`` at a current bundle.
Certificate checks are never switched off.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "pseudonymed" / "data"
TARGET = DATA / "plz_dach.csv.gz"
MUNICIPALITIES_DE = DATA / "gemeinden_de.csv.gz"
DEMOGRAPHICS = DATA / "demografie_dach.csv.gz"
HEADER = ["land", "plz", "ort", "gemeinde_kennung", "gemeinde_name", "kreis_kennung", "anteil"]
AGENT = {"User-Agent": "PseudonyMed build script (https://github.com/fre-ms/PseudonyMed)"}
SCALE = 10_000  # shares are written in units of 1/10,000

DE_REPO = "openpotato/openplzapi.data"
DE_PATH = "src/de/osm/streets.updated.csv"
DE_COMMITS = f"https://api.github.com/repos/{DE_REPO}/commits?path={DE_PATH}&per_page=1"
DE_RAW = f"https://raw.githubusercontent.com/{DE_REPO}/{{sha}}/{DE_PATH}"
DE_CHANGES = ("https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/"
              "Gemeindeverzeichnis/Namens-Grenz-Aenderung/")

AT_ADDRESSES = ("https://data.bev.gv.at/download/Adressregister/"
                "Adresse_Relationale_Tabellen_Stichtagsdaten.zip")
AT_RESIDENTIAL = {"01", "02", "03"}  # one dwelling, two or more dwellings, communal residence
AT_VIENNA = "90001"  # Vienna as one municipality in the address register

CH_ADDRESSES = ("https://data.geo.admin.ch/ch.swisstopo.amtliches-gebaeudeadressverzeichnis/"
                "amtliches-gebaeudeadressverzeichnis_ch/"
                "amtliches-gebaeudeadressverzeichnis_ch_2056.csv.zip")
CH_LOCALITIES = ("https://data.geo.admin.ch/ch.swisstopo-vd.ortschaftenverzeichnis_plz/"
                 "ortschaftenverzeichnis_plz/ortschaftenverzeichnis_plz_2056.csv.zip")
CH_AGV = "https://www.agvchapp.bfs.admin.ch/api/communes/"
CH_RESIDENTIAL = {"residential", "other_residential", "partly_residential"}

# Plausibility: expected number of postcodes and the district of well-known postcodes.
EXPECTED = {"DE": (8_000, 8_400), "AT": (2_100, 2_400), "CH": (3_000, 3_400)}
SAMPLES = {("DE", "86150"): "09761", ("DE", "80331"): "09162", ("DE", "10115"): "11000",
           ("AT", "1010"): "901", ("AT", "8010"): "601", ("CH", "8001"): "112",
           ("CH", "3011"): "246"}
MIN_COVERAGE = 0.98  # share of districts that must have at least one postcode

Pair = tuple[str, str]  # (postcode, municipality key)
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass
class Country:
    """Counts of one country per (postcode, municipality) and the metadata to write them."""

    code: str
    residential: dict[Pair, float] = field(default_factory=lambda: defaultdict(float))
    all_items: dict[Pair, float] = field(default_factory=lambda: defaultdict(float))
    replacement: dict[Pair, float] = field(default_factory=lambda: defaultdict(float))
    places: dict[Pair, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    population: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    district: dict[str, str] = field(default_factory=dict)
    state: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _fetch(url: str, raw: Path | None, file: str, attempts: int = 3,
           timeout: int = 900) -> bytes:
    """Return the body of ``url``, cached as ``roh/datei`` if a folder is given."""
    path = raw / file if raw else None
    if path and path.exists():
        return path.read_bytes()
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=AGENT)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
            break
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise SystemExit(
                    f"{url}: Zertifikat nicht prüfbar ({error.reason.verify_message}). Dem"
                    " CA-Bündel dieser Python-Installation fehlt vermutlich die Wurzel (z. B."
                    " HARICA TLS RSA Root CA 2021 bei data.bev.gv.at); SSL_CERT_FILE auf ein"
                    " aktuelles Bündel setzen oder die Datei selbst nach ORDNER/<quelle>/"
                    f"{file} laden und mit --raw ORDNER bauen.") from error
            if attempt == attempts:
                raise
            print(f"  {url}: {error}; neuer Versuch in {20 * attempt} s", file=sys.stderr)
            time.sleep(20 * attempt)
        except OSError as error:
            if attempt == attempts:
                raise
            print(f"  {url}: {error}; neuer Versuch in {20 * attempt} s", file=sys.stderr)
            time.sleep(20 * attempt)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return content


def _csv(z: zipfile.ZipFile, name: str, separator: str = ";") -> Iterator[dict[str, str]]:
    """Rows of a CSV member of a zip archive (UTF-8, optional byte order mark)."""
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""),
                                  delimiter=separator)


def _xlsx_lines(content: bytes, muster: str) -> Iterator[dict[str, str]]:
    """Rows ``{column letter: text}`` of the first sheet whose name contains ``muster``."""
    z = zipfile.ZipFile(io.BytesIO(content))
    strings: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
                   for si in ss.findall("m:si", NS)]
    sheets = ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS)
    rid = next(b.get(REL_ID) for b in sheets if muster in (b.get("name") or "").lower())
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = next(r.get("Target", "") for r in rels if r.get("Id") == rid)
    path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    for line in ET.fromstring(z.read(path)).find("m:sheetData", NS):
        out: dict[str, str] = {}
        for c in line.findall("m:c", NS):
            column = "".join(ch for ch in c.get("r", "") if ch.isalpha())
            value = c.findtext("m:v", default="", namespaces=NS)
            if c.get("t") == "s" and value:
                value = strings[int(value)]
            elif c.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
            out[column] = value.strip()
        yield out


def _most_frequent(counter: Counter[str]) -> str:
    """Most frequent non-empty name (ties broken alphabetically), else an empty string."""
    candidates = [(-n, name) for name, n in counter.items() if name]
    return min(candidates)[1] if candidates else ""


def _demographics() -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], str],
                             dict[str, str]]:
    """Population and name per (land, ebene, kennung) of districts and municipalities."""
    sum_total: dict[tuple[str, str, str], int] = defaultdict(int)
    names: dict[tuple[str, str, str], str] = {}
    reference_date: dict[str, str] = {}
    with gzip.open(DEMOGRAPHICS, "rt", encoding="utf-8", newline="") as f:
        next(f)
        for r in csv.DictReader(f):
            if r["ebene"] in ("kreis", "gemeinde"):
                key = (r["land"], r["ebene"], r["kennung"])
                sum_total[key] += int(r["anzahl"])
                names[key] = r["name"]
                reference_date[r["land"]] = r["stichtag"]
    return sum_total, names, reference_date


# --------------------------------------------------------------------------- Germany

Successors = dict[str, list[tuple[str, float, bool]]]


def _successors_de(raw: Path | None, from_year: int) -> Successors:
    """Successors (AGS) of dissolved or re-keyed municipalities: (AGS, area moved, whether
    the moved part had residents)."""
    overview = _fetch(DE_CHANGES + "namens-grenz-aenderung.html", raw,
                      "namens-grenz-aenderung.html").decode("utf-8", "replace")
    parts: dict[str, str] = {}
    for part in re.findall(r"Namens-Grenz-Aenderung/(\d{4}(?:-\d{2})?)\.html", overview):
        if int(part[:4]) >= from_year:
            parts[part[:4]] = max(parts.get(part[:4], ""), part)  # latest list per year
    if not parts:
        raise SystemExit("DE: keine Listen der Gebietsänderungen gefunden")
    successors: Successors = defaultdict(list)
    for part in sorted(parts.values()):
        page = _fetch(DE_CHANGES + f"{part}.html", raw, f"{part}.html").decode(
            "utf-8", "replace")
        reference = re.search(r'href="([^"]*Namens-Grenz-Aenderung/[^"]*\.xlsx[^"]*)"', page)
        if not reference:
            raise SystemExit(f"DE: keine Tabelle der Gebietsänderungen {part} gefunden")
        url = "https://www.destatis.de" + html.unescape(reference.group(1))
        for z in _xlsx_lines(_fetch(url, raw, f"aenderungen_{part}.xlsx"), "nderung"):
            # Columns: B unit type, D old AGS, F kind (1 dissolution, 3 new key), G area,
            # H residents moved (empty for a new key), J new AGS.
            old, new = z.get("D", ""), z.get("J", "")
            if (z.get("B") == "Gemeinde" and z.get("F") in ("1", "3") and len(old) == 8
                    and len(new) == 8 and old != new):
                successors[old].append((new, float(z.get("G") or 0) or 1.0,
                                        z.get("H", "") != "0"))
    return successors


def _resolve(ags: str, successors: Successors, known: dict[str, str],
             depth: int = 0) -> list[tuple[str, float, bool]]:
    """Current municipalities (AGS) for ``ags``: the weight each one takes over (by area)
    and whether the part taken over had residents."""
    if ags in known:
        return [(ags, 1.0, True)]
    targets = successors.get(ags, [])
    if depth > 5 or not targets:
        return []
    area = sum(f for _n, f, _b in targets)
    return [(k, g * f / area, inhabited and b) for new, f, inhabited in targets
            for k, g, b in _resolve(new, successors, known, depth + 1)]


def germany(raw: Path | None, raw_changes: Path | None) -> Country:
    """Germany: streets per (postcode, municipality) from the OpenPLZ OSM extract."""
    country = Country("DE")
    commit = json.loads(_fetch(DE_COMMITS, raw, "commit.json"))[0]
    sha, date = commit["sha"], commit["commit"]["committer"]["date"][:10]
    streets = _fetch(DE_RAW.format(sha=sha), raw, f"streets.updated_{sha[:12]}.csv")
    with gzip.open(MUNICIPALITIES_DE, "rt", encoding="utf-8", newline="") as f:
        header = f.readline()
        municipalities = list(csv.DictReader(f))
    territory_date = re.search(r"Gebietsstand (\d{4}-\d{2}-\d{2})", header)
    country.state = (f"OSM-Auszug {date} (openplzapi.data {sha[:7]}), Gebietsstand"
                     f" {territory_date.group(1) if territory_date else '?'}")
    ars = {g["ars"][:5] + g["ars"][9:]: g["ars"] for g in municipalities}
    for g in municipalities:
        country.population[g["ars"]] = int(g["einwohner"])
        country.names[g["ars"]] = g["name"]
        country.district[g["ars"]] = g["ars"][:5]
    successors = _successors_de(raw_changes, int(date[:4]))
    pending: Counter[str] = Counter()
    rekeyed: Counter[str] = Counter()
    for z in csv.DictReader(io.StringIO(streets.decode("utf-8"))):
        postcode, ags = z["PostalCode"], z["RegionalKey"]
        if not re.fullmatch(r"\d{5}", postcode):
            continue
        targets = _resolve(ags, successors, ars)
        if not targets:
            pending[ags] += 1
            continue
        if ags not in ars:
            rekeyed[ags] += 1
        for a, weight, inhabited in targets:
            pair = (postcode, ars[a])
            if inhabited:  # streets of uninhabited dissolved areas carry no residents
                country.residential[pair] += weight
            country.all_items[pair] += weight
            country.places[pair][z["Locality"]] += weight
    # Populated municipalities without any OSM street: one unit at the postcode of the seat,
    # so their whole population is counted there.
    with_streets = {g for _p, g in country.all_items}
    without = [g for g in municipalities if int(g["einwohner"]) > 0 and g["ars"] not in with_streets
               and re.fullmatch(r"\d{5}", g["plz"])]
    for g in without:
        pair = (g["plz"], g["ars"])
        country.residential[pair] = country.all_items[pair] = 1.0
        country.places[pair][g["name"]] += 1
    existing = {p for p, _g in country.all_items}
    seats = [g for g in municipalities if re.fullmatch(r"\d{5}", g["plz"])
             and g["plz"] not in existing]
    for g in seats:
        pair = (g["plz"], g["ars"])
        country.replacement[pair] = max(int(g["einwohner"]), 1)
        country.places[pair][g["name"]] += 1
    country.notes += [
        f"{sum(rekeyed.values())} Straßen aus {len(rekeyed)} geänderten"
        f" Gemeindeschlüsseln nachgeführt ({', '.join(sorted(rekeyed))})",
        f"{sum(pending.values())} Straßen ohne bekannte Gemeinde verworfen"
        f" ({', '.join(sorted(pending)) or 'keine'})",
        f"{len(without)} bewohnte Gemeinden ohne OSM-Straße mit der PLZ des Gemeindesitzes"
        f" ergänzt ({sum(int(g['einwohner']) for g in without)} Einwohner)",
        f"{len({g['plz'] for g in seats})} PLZ von Gemeindesitzen ohne OSM-Gebiet ergänzt",
    ]
    return country


# --------------------------------------------------------------------------- Austria

def austria(raw: Path | None, demo: tuple) -> Country:
    """Austria: addresses per (postcode, municipality) from the BEV address register."""
    sum_total, names, _reference_date = demo
    country = Country("AT")
    z = zipfile.ZipFile(io.BytesIO(_fetch(AT_ADDRESSES, raw,
                                          "Adresse_Relationale_Tabellen_Stichtagsdaten.zip")))
    date = re.search(r"(\d{2})\.(\d{2})\.(\d{4})",
                     z.read("Aktualitaetsstand.txt").decode("utf-8", "replace"))
    if not date:
        raise SystemExit("AT: Stichtag des Adressregisters nicht gefunden")
    country.state = f"{date.group(3)}-{date.group(2)}-{date.group(1)}"
    residential_addresses = {r["ADRCD"] for r in _csv(z, "GEBAEUDE.csv")
                             if r["EIGENSCHAFT"] in AT_RESIDENTIAL}
    delivery_place = {r["SKZ"]: r["ZUSTELLORT"] for r in _csv(z, "STRASSE.csv")}
    locality = {(r["GKZ"], r["OKZ"]): r["ORTSNAME"] for r in _csv(z, "ORTSCHAFT.csv")}
    for r in _csv(z, "ADRESSE.csv"):
        if not re.fullmatch(r"\d{4}", r["PLZ"]):
            continue
        municipality = r["ZAEHLSPRENGEL"][:5] if r["GKZ"] == AT_VIENNA else r["GKZ"]
        pair = (r["PLZ"], municipality)
        country.all_items[pair] += 1
        if r["ADRCD"] in residential_addresses:
            country.residential[pair] += 1
        place = delivery_place.get(r["SKZ"]) or locality.get((r["GKZ"], r["OKZ"]), "")
        country.places[pair][place] += 1
    for (ctry, layer, code), n in sum_total.items():
        if ctry == "AT" and layer == "gemeinde":
            country.population[code] = n
            country.names[code] = names[(ctry, layer, code)]
            country.district[code] = code[:3]
    return country


# --------------------------------------------------------------------------- Switzerland

def _normalised(name: str) -> str:
    """Locality or municipality name without canton suffix and parenthesised additions."""
    name = re.sub(r"\s*\(.*?\)", "", name).strip()
    return re.sub(r"\s+[A-Z]{2}$", "", name).casefold()


def _agv_date(iso: str) -> str:
    """ISO date to the DD-MM-YYYY form of the BFS REST service."""
    j, m, t = iso.split("-")
    return f"{t}-{m}-{j}"


def switzerland(raw_addresses: Path | None, raw_localities: Path | None, raw_agv: Path | None,
                demo: tuple) -> Country:
    """Switzerland: addresses per (postcode, municipality) from swisstopo, BFS state."""
    sum_total, names, reference_date = demo
    country = Country("CH")
    z = zipfile.ZipFile(io.BytesIO(_fetch(CH_ADDRESSES, raw_addresses,
                                          "amtliches-gebaeudeadressverzeichnis_ch_2056.csv.zip")))
    entry = next(e for e in z.infolist() if e.filename.endswith(".csv"))
    country.state = "{:04d}-{:02d}-{:02d}".format(*entry.date_time[:3])
    # Districts in the territorial state of the demography file, mergers since then.
    begin, end = _agv_date(reference_date["CH"]), _agv_date(country.state)
    state_old = list(csv.DictReader(io.StringIO(_fetch(
        f"{CH_AGV}snapshot?date={begin}", raw_agv, f"snapshot_{begin}.csv").decode("utf-8-sig"))))
    districts = {r["HistoricalCode"]: r["BfsCode"] for r in state_old if r["Level"] == "2"}
    for r in state_old:
        if r["Level"] == "3":
            country.district[r["BfsCode"]] = districts[r["Parent"]]
    predecessors: dict[str, list[str]] = defaultdict(list)
    for r in csv.DictReader(io.StringIO(_fetch(
            f"{CH_AGV}correspondances?startPeriod={begin}&endPeriod={end}", raw_agv,
            f"correspondances_{begin}_{end}.csv").decode("utf-8-sig"))):
        predecessors[r["TerminalCode"]].append(r["InitialCode"])
    for (ctry, layer, code), n in sum_total.items():
        if ctry == "CH" and layer == "gemeinde":
            country.population[code] = n
            country.names[code] = names[(ctry, layer, code)]
    decomposition: dict[tuple[str, str], list[tuple[str, float]]] = {}

    def decompose(bfs: str, place: str) -> list[tuple[str, float]]:
        """Municipalities of the demography state for a current BFS number: the former
        commune named like the locality, else the surviving one, else all by population."""
        if (bfs, place) not in decomposition:
            old = sorted(predecessors.get(bfs, [bfs]))
            same = [a for a in old if _normalised(country.names.get(a, "")) == _normalised(place)]
            if len(old) > 1 and (same or bfs in old):
                old = same or [bfs]
            pop = {a: country.population.get(a, 0) for a in old}
            total = sum(pop.values())
            decomposition[(bfs, place)] = [(a, pop[a] / total if total else 1 / len(old))
                                           for a in old]
        return decomposition[(bfs, place)]

    for r in _csv(z, entry.filename):
        if r["ADR_STATUS"] != "real":
            continue
        postcode, place = r["ZIP_LABEL"][:4], r["ZIP_LABEL"][4:].strip()
        if not re.fullmatch(r"\d{4}", postcode):
            continue
        residential = r["BDG_CATEGORY"] in CH_RESIDENTIAL
        for municipality, weight in decompose(r["COM_FOSNR"], place):
            pair = (postcode, municipality)
            country.all_items[pair] += weight
            if residential:
                country.residential[pair] += weight
            country.places[pair][place] += weight
    # Cross-check with the official locality directory; postcodes without any address
    # get the directory's address shares as fallback.
    locality_zip = zipfile.ZipFile(io.BytesIO(_fetch(CH_LOCALITIES, raw_localities,
                                                     "ortschaftenverzeichnis_plz_2056.csv.zip")))
    with_addresses = {p for p, _g in country.all_items}
    in_directory: set[str] = set()
    for r in _csv(locality_zip, next(n for n in locality_zip.namelist() if n.endswith(".csv"))):
        if not r["Kantonskürzel"]:  # Liechtenstein
            continue
        postcode = r["PLZ4"]
        in_directory.add(postcode)
        if postcode in with_addresses:
            continue
        share = float(r["Adressenanteil"].replace("%", "").strip() or 0) / 100
        for municipality, weight in decompose(r["BFS-Nr"], r["Ortschaftsname"]):
            country.replacement[(postcode, municipality)] += share * weight
            country.places[(postcode, municipality)][r["Ortschaftsname"]] += 1
    country.notes += [
        f"Ortschaftenverzeichnis: {len(in_directory)} PLZ, davon ohne Adresse"
        f" {len(in_directory - with_addresses)}; PLZ nur im Adressverzeichnis:"
        f" {sorted(with_addresses - in_directory) or 'keine'}",
        "zusammengelegte Gemeinden seit dem Stichtag der Demografie: "
        + (", ".join(f"{t} <- {'+'.join(sorted(v))}" for t, v in sorted(predecessors.items()))
           or "keine"),
    ]
    return country


# --------------------------------------------------------------------------- shares

def _round_shares(shares: dict[str, float]) -> dict[str, int]:
    """Shares in units of 1/STELLEN summing to STELLEN; shares that round to 0 are dropped."""
    large = {g: a for g, a in shares.items() if a * SCALE >= 0.5}
    total = sum(large.values())
    n = {g: round(a / total * SCALE) for g, a in large.items()}
    largest = max(n, key=lambda g: (n[g], g))
    n[largest] += SCALE - sum(n.values())
    return {g: v for g, v in n.items() if v > 0}


def lines(country: Country) -> tuple[list[list[str]], Counter[str], int, int]:
    """Rows of one country, weighting stage used per postcode, pairs without residents and
    pairs dropped by rounding."""
    units_res: dict[str, float] = defaultdict(float)
    units_all: dict[str, float] = defaultdict(float)
    for (_p, g), v in country.residential.items():
        units_res[g] += v
    for (_p, g), v in country.all_items.items():
        units_all[g] += v
    per_postcode: dict[str, dict[str, tuple[float, float, float, float]]] = defaultdict(dict)
    for pair in set(country.all_items) | set(country.residential) | set(country.replacement):
        p, g = pair
        pop = country.population.get(g, 0)
        w, a = country.residential.get(pair, 0.0), country.all_items.get(pair, 0.0)
        per_postcode[p][g] = (pop * w / units_res[g] if units_res[g] else 0.0,
                              pop * a / units_all[g] if units_all[g] else 0.0,
                              a, country.replacement.get(pair, 0.0))
    stages = ["Einwohner x Wohneinheiten", "Einwohner x alle Adressen", "alle Adressen",
              "Ersatzgewicht"]
    used: Counter[str] = Counter()
    out: list[list[str]] = []
    discarded = without_residents = 0
    for p in sorted(per_postcode):
        values = per_postcode[p]
        stage = next((i for i in range(4) if sum(v[i] for v in values.values()) > 0), None)
        if stage is None:
            continue
        used[stages[stage]] += 1
        total = sum(v[stage] for v in values.values())
        without_residents += sum(1 for v in values.values() if v[stage] == 0)
        rounded = _round_shares({g: v[stage] / total for g, v in values.items() if v[stage] > 0})
        discarded += sum(1 for v in values.values() if v[stage] > 0) - len(rounded)
        for g, n in sorted(rounded.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append([country.code, p, _most_frequent(country.places[(p, g)]), g,
                        country.names.get(g, ""), country.district.get(g, ""), f"{n / SCALE:.4f}"])
    return out, used, without_residents, discarded


# --------------------------------------------------------------------------- checks

def check(all_lines: list[list[str]], demo: tuple, countries: dict[str, Country]) -> list[str]:
    """Plausibility checks; prints counts and returns the problems found."""
    sum_total, _names, _reference_date = demo
    problems: list[str] = []
    for code, country in countries.items():
        own = [z for z in all_lines if z[0] == code]
        postcode = {z[1] for z in own}
        lo, hi = EXPECTED[code]
        if not lo <= len(postcode) <= hi:
            problems.append(f"{code}: {len(postcode)} PLZ außerhalb {lo}-{hi}")
        expected_districts = {k for (ctry, e, k) in sum_total if ctry == code and e == "kreis"}
        districts = {z[5] for z in own}
        municipalities = {z[3] for z in own}
        coverage = len(districts & expected_districts) / len(expected_districts)
        print(f"{code}: {len(postcode)} PLZ, {len(own)} Zeilen, {len(municipalities)} Gemeinden,"
              f" Kreise mit PLZ {len(districts & expected_districts)} von {len(expected_districts)}"
              f" ({coverage:.1%}); ohne PLZ: {sorted(expected_districts - districts) or 'keine'}")
        if coverage < MIN_COVERAGE:
            problems.append(f"{code}: nur {coverage:.1%} der Kreise mit PLZ")
        foreign_districts = districts - expected_districts
        if foreign_districts:
            problems.append(f"{code}: Kreise nicht in der Demografie: {sorted(foreign_districts)}")
        known = set(country.names) if code == "DE" else {
            k for (ctry, e, k) in sum_total if ctry == code and e == "gemeinde"}
        if municipalities - known:
            problems.append(f"{code}: unbekannte Gemeinden: {sorted(municipalities - known)[:10]}")
        multiple = Counter(z[1] for z in own)
        print(f"  PLZ mit mehreren Gemeinden: {sum(1 for n in multiple.values() if n > 1)};"
              f" Gemeinden mit mehreren PLZ:"
              f" {sum(1 for n in Counter(z[3] for z in own).values() if n > 1)}")
        for note in country.notes:
            print(f"  {note}")
    sums: dict[tuple[str, str], int] = defaultdict(int)
    for z in all_lines:
        sums[(z[0], z[1])] += round(float(z[6]) * SCALE)
    wrong = [k for k, v in sums.items() if v != SCALE]
    if wrong:
        problems.append(f"Anteile summieren sich nicht zu 1: {wrong[:10]}")
    for (code, postcode), district in SAMPLES.items():
        matches = [z for z in all_lines if z[0] == code and z[1] == postcode]
        print(f"  Stichprobe {code} {postcode}: "
              + "; ".join(f"{z[2]} -> {z[3]} {z[4]}, Kreis {z[5]}, {z[6]}" for z in matches))
        if not matches or matches[0][5] != district:
            problems.append(f"Stichprobe {code} {postcode}: Kreis {district} erwartet")
    return problems


# --------------------------------------------------------------------------- output

def header_line(countries: dict[str, Country]) -> str:
    at = countries["AT"].state.split("-")
    return (
        "# Quelle: DE: © OpenStreetMap-Mitwirkende, Straßenverzeichnis der OpenPLZ API"
        " (https://github.com/openpotato/openplzapi.data), Gemeindeschlüssel und Einwohner:"
        " Statistisches Bundesamt (Destatis), Gemeindeverzeichnis GV-ISys und Gebietsänderungen"
        " | AT: BEV - Bundesamt für Eich- und Vermessungswesen, © Österreichisches"
        f" Adressregister, Stichtagsdaten vom {at[2]}.{at[1]}.{at[0]}; Einwohner: Statistik"
        " Austria - data.statistik.gv.at | CH: Bundesamt für Landestopografie swisstopo,"
        " Amtliches Gebäudeadressverzeichnis und Amtliches Ortschaftenverzeichnis; Bundesamt"
        " für Statistik (BFS), Amtliches Gemeindeverzeichnis der Schweiz"
        " (https://www.agvchapp.bfs.admin.ch) und STATPOP"
        " | zusammengeführt, umgeformt und Anteile geschätzt von PseudonyMed;"
        " Lizenz: Datei: Open Database License (ODbL) 1.0"
        " (https://opendatacommons.org/licenses/odbl/1-0/), weil die DE-Zeilen aus"
        " OpenStreetMap abgeleitet sind; Einzelquellen: DE: OSM/OpenPLZ ODbL 1.0, Destatis"
        " Datenlizenz Deutschland - Namensnennung - 2.0 (https://www.govdata.de/dl-de/by-2-0)"
        " | AT: CC BY 4.0"
        " (https://creativecommons.org/licenses/by/4.0/) | CH: swisstopo OGD, Quellenangabe"
        " Pflicht; BFS OPEN BY;"
        f" Stand: DE {countries['DE'].state}, AT {countries['AT'].state},"
        f" CH {countries['CH'].state} (Gemeinden im Gebietsstand der Demografie-Datei)\n"
    )


def write(all_lines: list[list[str]], countries: dict[str, Country], target: Path) -> None:
    buffer = io.StringIO()
    buffer.write(header_line(countries))
    w = csv.writer(buffer, lineterminator="\n")
    w.writerow(HEADER)
    w.writerows(all_lines)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz):
        gz.write(buffer.getvalue().encode("utf-8"))
    print(f"{len(all_lines)} Zeilen nach {target} geschrieben"
          f" ({target.stat().st_size / 1e6:.2f} MB)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--raw", type=Path, help="folder that keeps the downloads")
    parser.add_argument("--target", type=Path, default=TARGET, help="output file")
    args = parser.parse_args(argv[1:])

    def folder(name: str) -> Path | None:
        return args.raw / name if args.raw else None

    demo = _demographics()
    print("DE: lade OpenPLZ-Straßen und Destatis-Gebietsänderungen", file=sys.stderr)
    countries = {"DE": germany(folder("openplz"), folder("destatis_aenderungen"))}
    print("AT: lade BEV-Adressregister", file=sys.stderr)
    countries["AT"] = austria(folder("bev_adressregister"), demo)
    print("CH: lade swisstopo-Verzeichnisse und BFS-Gemeindestand", file=sys.stderr)
    countries["CH"] = switzerland(folder("swisstopo_adressen"), folder("swisstopo"),
                                  folder("bfs_agv"), demo)
    all_lines: list[list[str]] = []
    for country in countries.values():
        z, used, without_residents, discarded = lines(country)
        country.notes.append(f"Gewichtung je PLZ: {dict(used)}; Paare ohne geschätzte"
                             f" Einwohner: {without_residents}; Anteil unter 0,00005 verworfen:"
                             f" {discarded}")
        all_lines += z
    problems = check(all_lines, demo, countries)
    if problems:
        print(f"{len(problems)} Probleme, nichts geschrieben:", *problems[:30], sep="\n  ")
        return 1
    write(all_lines, countries, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
