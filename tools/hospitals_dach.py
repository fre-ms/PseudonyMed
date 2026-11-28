"""Build ``pseudonymed/data/hospitals_dach.csv.gz``: hospitals and rehabilitation clinics in DE
and CH.

One row per site with name, type, address, official municipality code, provider and beds, so
that a clinic name found in a document can later be tied to a municipality and its population.

Sources and licences
--------------------
Germany (DE): Statistische Ämter des Bundes und der Länder, "Verzeichnis der Krankenhäuser und
Vorsorge- oder Rehabilitationseinrichtungen in Deutschland", reporting year 2024 (as of
31 December 2024), workbook ``krankenhausverzeichnis-3500100247005.xlsx`` published by Destatis
in May 2026.  Sheet ``KHV_<year>`` has one row per hospital site, sheet ``RHV_<year>`` one row
per prevention or rehabilitation facility (Wirtschaftseinheit).  Licence: "Vervielfältigung und
Verbreitung, auch auszugsweise, mit Quellenangabe gestattet" (imprint of the workbook; the
general terms at https://www.destatis.de/DE/Service/Impressum/copyright-allgemein.html allow
commercial reuse and ask that changes be marked).  The workbook gives the municipality key
(Land, Kreis including Regierungsbezirk, Gemeinde) as of 31 December 2024; it is turned into
the 12-digit ARS of identicalc's ``municipalities_de.csv.gz`` (Destatis GV-ISys), with a
fallback over place name and postcode for municipalities that changed since.  Berlin and
Hamburg carry district codes in the source and map to the city as a whole.

Switzerland (CH): Bundesamt für Gesundheit (BAG), "Kennzahlen der Schweizer Spitäler 2023",
workbook ``kzp23_data.xlsx`` from https://opendata.swiss/de/dataset/kennzahlen-der-schweizer-
spitaler-2023; sheet ``KZ2023_KZP23`` (one row per hospital enterprise: type, beds) and sheet
``Standorte_KZ2023_KZP23`` (one row per site).  Licence: opendata.swiss "terms_by" (open use,
commercial use allowed, source must be named).  The 2024 edition is published under
"terms_by_ask" (commercial use only with permission) and is deliberately not used.  BFS
municipality numbers come from swisstopo's "Amtliches Ortschaftenverzeichnis mit
Postleitzahl" (``AMTOVZ_CSV_LV95``, opendata.swiss "terms_open").

Not included: Austria (the ministry's "Liste der Krankenanstalten in Österreich" allows
reprinting only for non-commercial purposes) and the Bundes-Klinik-Atlas export (no open
licence; the G-BA imprint reserves all rights).

Columns: country, code, name, type, street, postcode, place, municipality_code, provider, beds,
source.  ``code`` is the source's site key: Destatis has only a random hospital ID per edition
(``KH<id>-<n>`` for the n-th site of a hospital, ``RH<id>`` for rehabilitation facilities); the
BAG data have no key, so ``kzp-<hash>`` is derived from canton, enterprise, site and address.
``name`` is the site name; where the site label alone is not self-contained it is joined to the
hospital name with " – ".  ``type`` is hospital, psychiatry (most beds in psychiatric, child
and adolescent psychiatric or psychosomatic departments; sites without beds by name, by the
hospital as a whole, or as day clinic/outpatient unit of a hospital with psychiatric beds; CH
by the BFS hospital type K211/K212) or rehabilitation (Destatis RHV, BFS K221).  ``beds`` is
empty for CH sites of multi-site enterprises (beds are given per enterprise only).

Usage: python -I tools/hospitals_dach.py [--destatis X.xlsx] [--bag Y.xlsx]
       [--localities AMTOVZ.zip]   (without arguments the files are downloaded)
"""

from __future__ import annotations

import argparse
import collections
import csv
import difflib
import gzip
import hashlib
import io
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import identicalc.data

URL_DESTATIS = ("https://www.destatis.de/DE/Themen/Gesellschaft-Umwelt/Gesundheit/Krankenhauser/"
                "Publikationen/Downloads-Krankenhaeuser/krankenhausverzeichnis-3500100247005.xlsx"
                "?__blob=publicationFile")
URL_BAG = ("https://opendata.bagnet.ch/?r=/download&path="
           "L0taUC9renAyMy9renAyM19kYXRhLnhsc3g%3D")  # /KZP/kzp23/kzp23_data.xlsx
URL_LOCALITIES = ("https://data.geo.admin.ch/ch.swisstopo-vd.ortschaftenverzeichnis_plz/"
                  "ortschaftenverzeichnis_plz/ortschaftenverzeichnis_plz_2056.csv.zip")
TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "hospitals_dach.csv.gz"
MUNICIPALITIES = identicalc.data.MUNICIPALITIES  # Destatis, as identicalc ships it
COLUMNS = ["country", "code", "name", "type", "street", "postcode", "place", "municipality_code",
           "provider", "beds", "source"]
HOSPITAL, PSYCHIATRY, REHABILITATION = "hospital", "psychiatry", "rehabilitation"

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_T = f"{{{NS['m']}}}t"

# Psychiatric departments (§ 301 SGB V keys): 29xx general psychiatry, 30xx child and
# adolescent psychiatry, 31xx psychosomatics/psychotherapy, 3756 addiction medicine.
_PSYCH_SPECIALIST = re.compile(r"^FA_(29|30|31)\d\d$|^FA_3756$")
_PSYCH_WORD = re.compile(r"psychiatr|psychosomat|psychotherap|\bpsych\b|seelisch|nervenklinik|"
                         r"\bkjp\b|\bpia\b|institutsambulanz|sucht|entzug", re.IGNORECASE)
_DAY_CLINIC = re.compile(r"tageskl|nachtkl|\btk\b|ambulanz", re.IGNORECASE)
_TREATMENT = re.compile(r"\s*[–-]\s*(?:stationäre|teilstationäre|tagesklinische)\s+behandlung"
                        r"\s*$|\s*\(stationär\)\s*$|^hauptstandort\s*[–-]\s*", re.IGNORECASE)
_GENERIC = {"", "hauptstandort", "standort"}
_LEGAL_FORM = re.compile(r"\b(?:ggmbh|gmbh|mbh|gag|ag|kg|kgaa|gbr|aör|aoer|adör|kdör|ku|se|"
                         r"e\.?\s?v|co|sa|sarl)\b\.?")
CITY_STATES = {"02": "02000000", "11": "11000000"}  # Hamburg, Berlin: whole city
TYPES_CH = {"K211": PSYCHIATRY, "K212": PSYCHIATRY, "K221": REHABILITATION}
# German month names of the Destatis imprint ("Erschienen im Mai 2026, …").
MONTHS = {"Januar": "January", "Februar": "February", "März": "March", "April": "April",
          "Mai": "May", "Juni": "June", "Juli": "July", "August": "August",
          "September": "September", "Oktober": "October", "November": "November",
          "Dezember": "December"}
_PUBLISHED = re.compile(r"Erschienen im (\w+) (\d{4})"
                        r"(?:, aktualisiert am (\d{1,2})\. (\w+) (\d{4}))?")


def load(source: str | None, url: str) -> bytes:
    """Read a local file, or download ``url`` into memory when no file is given."""
    if source:
        return Path(source).read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "PseudonyMed"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def normalise_spaces(text: str) -> str:
    return " ".join(text.split())


def simplify(text: str) -> str:
    """Casefolded letters and digits only, umlauts kept, accents dropped (for comparisons)."""
    text = text.casefold().replace("ß", "ss")
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c) or c == "\u0308")
    text = unicodedata.normalize("NFC", text)
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def publication_note(imprint: str) -> str:
    """The publication note of the Destatis imprint in English ("Erschienen im Mai 2026,
    aktualisiert am 9. Juni 2026" → "published May 2026, updated 9 June 2026"); a note of
    another form stays as it is."""
    m = _PUBLISHED.fullmatch(imprint)
    if not m or m.group(1) not in MONTHS or (m.group(4) and m.group(4) not in MONTHS):
        return imprint
    note = f"published {MONTHS[m.group(1)]} {m.group(2)}"
    if m.group(3):
        note += f", updated {int(m.group(3))} {MONTHS[m.group(4)]} {m.group(5)}"
    return note


# --- xlsx with the standard library -----------------------------------------------------------

class Workbook:
    """Minimal reader for the sheets of an .xlsx (a zip of SpreadsheetML)."""

    def __init__(self, data: bytes) -> None:
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.strings: list[str] = []
        if "xl/sharedStrings.xml" in self.z.namelist():
            ss = ET.fromstring(self.z.read("xl/sharedStrings.xml"))
            self.strings = ["".join(t.text or "" for t in si.iter(_T))
                            for si in ss.findall("m:si", NS)]
        wb = ET.fromstring(self.z.read("xl/workbook.xml"))
        rels = ET.fromstring(self.z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        self.sheets: dict[str, str] = {}
        sheets = wb.find("m:sheets", NS)
        for s in sheets if sheets is not None else []:
            target = targets[s.get(f"{{{NS['r']}}}id")].lstrip("/")
            self.sheets[(s.get("name") or "").strip()] = (
                target if target.startswith("xl/") else "xl/" + target)

    def sheet(self, pattern: str) -> str:
        """Name of the first sheet whose name matches the regular expression ``pattern``."""
        for name in self.sheets:
            if re.fullmatch(pattern, name):
                return name
        raise KeyError(f"no sheet matches {pattern!r}: {list(self.sheets)}")

    def raw(self, name: str) -> Iterator[dict[str, str]]:
        """Rows of a sheet as {column letter: text}."""
        data = ET.fromstring(self.z.read(self.sheets[name])).find("m:sheetData", NS)
        for line in data if data is not None else []:
            out: dict[str, str] = {}
            for c in line.findall("m:c", NS):
                column = "".join(ch for ch in c.get("r", "") if ch.isalpha())
                v = c.findtext("m:v", default="", namespaces=NS)
                if c.get("t") == "s" and v:
                    v = self.strings[int(v)]
                elif c.get("t") == "inlineStr":
                    v = "".join(t.text or "" for t in c.iter(_T))
                out[column] = v.strip()
            yield out

    def table(self, name: str, is_header: Callable[[dict[str, str]], bool]
              ) -> Iterator[dict[str, str]]:
        """Rows below the first header row (``is_header``) as {header: text}; empty rows skipped."""
        header: dict[str, str] | None = None
        for line in self.raw(name):
            if header is None:
                if is_header(line):
                    header = {k: v for k, v in line.items() if v}
                continue
            if any(line.values()):
                yield {header[k]: v for k, v in line.items() if k in header}


def to_int(text: str) -> int:
    try:
        return round(float(text))
    except ValueError:
        return 0


# --- municipality keys ------------------------------------------------------------------------

class MunicipalityKeys:
    """AGS (Destatis, 31 December 2024) → ARS of identicalc's ``municipalities_de.csv.gz``,
    fallback by place name and postcode."""

    def __init__(self, path: Path) -> None:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            self.source = f.readline().lstrip("# ").strip()
            lines = list(csv.DictReader(f))
        self.by_ags = {z["ars"][:5] + z["ars"][9:]: z for z in lines}
        self.by_name: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        self.by_postcode: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        for z in lines:
            # "Hanau, Brüder-Grimm-Stadt": index the name before the comma as well
            for name in {simplify(z["name"]), simplify(z["name"].split(",")[0])}:
                self.by_name[name].append(z)
            self.by_postcode[z["postcode"]].append(z)

    def ars(self, ags: str, postcode: str, place: str) -> tuple[str, str]:
        """(ARS, method); method is key, place, place+postcode, place+district, place+state,
        postcode or ''."""
        ags = CITY_STATES.get(ags[:2], ags)
        if ags in self.by_ags:
            return self.by_ags[ags]["ars"], "key"
        candidates = self.by_name.get(simplify(place), [])
        if not candidates:
            # postal place names may carry additions ("Bad Homburg v.d. Höhe", "Halle (Saale)")
            short = simplify(re.split(r"[,(/]", place)[0])
            candidates = self.by_name.get(short, [])
        if len(candidates) == 1:
            return candidates[0]["ars"], "place"
        for method, fits in (("place+postcode", lambda z: z["postcode"] == postcode),
                             ("place+district", lambda z: z["ars"][:5] == ags[:5]),
                             ("place+state", lambda z: z["ars"][:2] == ags[:2])):
            narrower = [z for z in candidates if fits(z)]
            if len(narrower) == 1:
                return narrower[0]["ars"], method
        if not candidates and len(self.by_postcode.get(postcode, [])) == 1:
            return self.by_postcode[postcode][0]["ars"], "postcode"
        return "", ""


class Localities:
    """swisstopo Amtliches Ortschaftenverzeichnis: (postcode, place, canton) → BFS municipality
    number."""

    def __init__(self, data: bytes) -> None:
        z = zipfile.ZipFile(io.BytesIO(data))
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        year, month, day = z.getinfo(name).date_time[:3]
        self.state = f"{year:04d}-{month:02d}-{day:02d}"
        text = z.read(name).decode("utf-8-sig")
        self.by_postcode: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        self.municipalities: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        self.localities: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
        for r in csv.DictReader(io.StringIO(text), delimiter=";"):
            self.by_postcode[r["PLZ4"]].append(r)
            for variant in self._municipality(r):
                self.municipalities[variant].append(r)
            self.localities[simplify(r["Ortschaftsname"])].append(r)

    @staticmethod
    def _municipality(r: dict[str, str]) -> set[str]:
        """Municipality name with and without canton suffix ("Wil (SG)" → "wil sg", "wil")."""
        return {simplify(r["Gemeindename"]),
                simplify(re.sub(r"\s*\([A-Z]{2}\)$", "", r["Gemeindename"]))}

    @staticmethod
    def _share(r: dict[str, str]) -> float:
        try:
            return float(r.get("Adressenanteil", "0").replace("%", "").strip() or 0)
        except ValueError:
            return 0.0

    @staticmethod
    def _unique(lines: list[dict[str, str]], canton: str) -> str:
        numbers = {r["BFS-Nr"] for r in lines if not canton or r["Kantonskürzel"] == canton}
        return numbers.pop() if len(numbers) == 1 else ""

    def bfs(self, postcode: str, place: str, canton: str = "") -> tuple[str, str]:
        """(BFS number, method).

        Methods: ``postcode`` (postcode lies in one municipality), ``postcode+municipality``
        (place name is the municipality's name), ``postcode+place`` (place name is a locality
        of one municipality), ``municipality-not-postcode`` (the place is a municipality of
        the same canton that the postcode does not cover: the name wins), ``postcode-share``
        (still ambiguous: the municipality with the largest share of the postcode's
        addresses), ``municipality``/``place`` (postcode unknown, e.g. a post-box or
        large-customer postcode, found by name within the canton), or '' (not found).
        """
        o = simplify(re.sub(r"\s+\d+$", "", place))  # "Bern 60", "Luzern 16"
        candidates = self.by_postcode.get(postcode, [])
        if len({r["BFS-Nr"] for r in candidates}) == 1:
            return candidates[0]["BFS-Nr"], "postcode"
        if candidates:
            municipality = [r for r in candidates if o in self._municipality(r)]
            if number := self._unique(municipality, ""):
                return number, "postcode+municipality"
            locality = [r for r in candidates if simplify(r["Ortschaftsname"]) == o]
            if number := self._unique(locality, ""):
                return number, "postcode+place"
            if not municipality and not locality:
                if number := self._unique(self.municipalities.get(o, []), canton):
                    return number, "municipality-not-postcode"
            pool = municipality or locality or candidates
            pool = [r for r in pool if r["Kantonskürzel"] == canton] or pool
            return max(pool, key=self._share)["BFS-Nr"], "postcode-share"
        if number := self._unique(self.municipalities.get(o, []), canton):
            return number, "municipality"
        if number := self._unique(self.localities.get(o, []), canton):
            return number, "place"
        return "", ""


# --- names and types --------------------------------------------------------------------------

def _core(name: str) -> str:
    return " ".join(_LEGAL_FORM.sub(" ", simplify(name)).split())


def site_name(hospital: str, site: str) -> str:
    """Self-contained site name from the hospital name and the source's site label."""
    hospital, site = normalise_spaces(hospital), _TREATMENT.sub("", normalise_spaces(site)).strip()
    if simplify(site) in _GENERIC or not hospital:
        return hospital or site
    core_hospital, core_site = _core(hospital), _core(site)
    if core_hospital and core_hospital in core_site:
        return site
    words_hospital, words_site = core_hospital.split(), core_site.split()
    if len(words_hospital) >= 2 and len(words_site) >= 3 and words_hospital[:2] == words_site[:2]:
        return site  # "Bezirkskrankenhaus Landshut Fachklinik Passau"
    if (core_site in core_hospital or set(core_site.split()) <= set(core_hospital.split())
            or difflib.SequenceMatcher(None, core_hospital, core_site).ratio() >= 0.9):
        return hospital  # "Universitätsklinikum des Saarlandes (UKS)" under "UKS - Uni…"
    return f"{hospital} – {site}"


def _psych_beds(line: dict[str, str]) -> int:
    return sum(to_int(v) for k, v in line.items() if v and _PSYCH_SPECIALIST.match(k))


# --- sources ----------------------------------------------------------------------------------

def read_destatis(data: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Hospital sites (KHV) and rehabilitation facilities (RHV) as output rows plus ``ags``."""
    workbook = Workbook(data)
    hospital_sheet, rehab_sheet = workbook.sheet(r"KHV_\d{4}"), workbook.sheet(r"RHV_\d{4}")
    year = hospital_sheet[-4:]
    imprint = [v for z in workbook.raw(workbook.sheet("Impressum")) for v in z.values() if v]
    published = next((v for v in imprint if v.startswith("Erschienen")), "")
    copyright_ = next((normalise_spaces(v) for v in imprint
                       if v.startswith("©") and "Statistisches Bundesamt" in v), "")
    meta = {"year": year, "published": publication_note(published.rstrip(".")),
            "copyright": copyright_}

    hospitals = [z for z in workbook.table(hospital_sheet, lambda z: z.get("A") == "KH_ID_Pseudo")
                 if z.get("KH_ID_Pseudo")]
    rehabs = [z for z in workbook.table(rehab_sheet, lambda z: z.get("A") == "RH_ID_Pseudo")
              if z.get("RH_ID_Pseudo")]

    # hospital-level share of psychiatric beds, for sites without beds of their own
    hospital_psych: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for z in hospitals:
        hospital_psych[z["KH_ID_Pseudo"]][0] += _psych_beds(z)
        hospital_psych[z["KH_ID_Pseudo"]][1] += to_int(z.get("Betten", ""))

    def shared(z: dict[str, str], kind: str) -> dict[str, str]:
        postcode = z.get("PLZ", "")
        return {
            "country": "DE",
            "street": normalise_spaces(f"{z.get('Straße', '')} {z.get('Hausnummer', '')}"),
            "postcode": postcode.zfill(5) if postcode.isdigit() else postcode,
            "place": normalise_spaces(z.get("Ort", "")),
            "provider": normalise_spaces(z.get("Trägername", "")),
            "beds": str(to_int(z.get("Betten", ""))),
            "source": f"destatis-{kind}-{year}",
            "ags": (z.get("Land", "").zfill(2) + z.get("Kreis", "").zfill(3)
                    + z.get("Gemeinde", "").zfill(3)),
        }

    lines: list[dict[str, str]] = []
    serial: collections.Counter[str] = collections.Counter()
    for z in hospitals:
        hid = z["KH_ID_Pseudo"]
        serial[hid] += 1
        name = site_name(z.get("KH_Name", ""), z.get("Standortname", ""))
        beds, psych = to_int(z.get("Betten", "")), _psych_beds(z)
        if beds > 0:
            type_name = PSYCHIATRY if 2 * psych > beds else HOSPITAL
        else:
            # no beds (day clinic, outpatient unit): by name, else by the hospital as a whole;
            # a day clinic or outpatient unit of a hospital with psychiatric beds counts as
            # psychiatric, since somatic day care rarely forms a site of its own
            p, t = hospital_psych[hid]
            psychiatric = (_PSYCH_WORD.search(name) or (t > 0 and 2 * p > t)
                           or (p > 0 and _DAY_CLINIC.search(name)))
            type_name = PSYCHIATRY if psychiatric else HOSPITAL
        lines.append({**shared(z, "khv"), "code": f"KH{hid}-{serial[hid]}",
                      "name": name, "type": type_name})
    for z in rehabs:
        lines.append({**shared(z, "rhv"), "code": f"RH{z['RH_ID_Pseudo']}",
                      "name": normalise_spaces(z.get("RH_Name", "")), "type": REHABILITATION})
    meta["khv"], meta["rhv"] = str(len(hospitals)), str(len(rehabs))
    meta["hospitals"] = str(len({z["KH_ID_Pseudo"] for z in hospitals}))
    return meta, lines


def read_bag(data: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Swiss hospital sites with the enterprise's type and (for single sites) beds."""
    workbook = Workbook(data)
    sites_sheet = workbook.sheet(r"Standorte_KZ\d{4}_KZP\d\d")
    enterprises_sheet = workbook.sheet(sites_sheet.removeprefix("Standorte_"))
    year = sites_sheet[len("Standorte_KZ"):][:4]

    def is_header(z: dict[str, str]) -> bool:
        return z.get("A") == "JAHR"

    enterprises = {(z["KT"], z["Inst"]): z for z in workbook.table(enterprises_sheet, is_header)
                   if z.get("KT") and z.get("Inst")}
    lines = []
    for s in workbook.table(sites_sheet, is_header):
        if not s.get("Inst"):
            continue
        b = enterprises[(s["KT"], s["Inst"])]
        postcode, _, place = s.get("Ort_Standort", "").partition(" ")
        beds = ""
        if b.get("AnzStand") == "1":
            beds = str(sum(to_int(b.get(f"BettenStat{a}", "")) for a in "APRB"))
        key = "|".join([s["KT"], s["Inst"], s.get("Standort", ""),
                        s.get("Adr_Standort", ""), s.get("Ort_Standort", "")])
        lines.append({
            "country": "CH",
            "code": "kzp-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10],
            "name": site_name(s["Inst"], s.get("Standort", "")),
            "type": TYPES_CH.get(b.get("Typ", ""), HOSPITAL),
            "street": normalise_spaces(s.get("Adr_Standort", "")),
            "postcode": postcode, "place": normalise_spaces(place),
            "provider": normalise_spaces(s["Inst"]), "beds": beds,
            "source": f"bag-kzp-{year}",
            "canton": s.get("KT_Standort") or s["KT"],
        })
    return {"year": year, "enterprises": str(len(enterprises)), "sites": str(len(lines))}, lines


# --- output -----------------------------------------------------------------------------------

def header_line(de_meta: dict[str, str], ch_meta: dict[str, str], territory_date: str,
                localities_state: str) -> str:
    """The provenance line at the top of the file: sources, licences and dates."""
    return (
        f"# Source: DE Statistische Ämter des Bundes und der Länder (statistical offices of the"
        f" Federation and the Länder), Verzeichnis der Krankenhäuser und Vorsorge- oder"
        f" Rehabilitationseinrichtungen in Deutschland {de_meta['year']} (directory of"
        f" hospitals and prevention or rehabilitation facilities in Germany; Destatis,"
        f" {de_meta['published']}), modified: site names merged, type derived, ARS added from"
        f" Destatis GV-ISys (territory as of {territory_date}); CH Bundesamt für Gesundheit"
        f" (BAG, Federal Office of Public Health), Kennzahlen der Schweizer Spitäler"
        f" {ch_meta['year']} (key figures of Swiss hospitals), sites, modified: BFS numbers"
        f" added from swisstopo Amtliches Ortschaftenverzeichnis (official directory of"
        f" localities, {localities_state}); AT not included; Licence: DE reproduction and"
        f" distribution permitted with attribution ({de_meta['copyright']}), CH opendata.swiss"
        f" terms_by (free use, attribution compulsory), swisstopo terms_open; As of:"
        f" DE {de_meta['year']}-12-31, CH {ch_meta['year']}\n"
    )


def write(target: Path, header: str, lines: list[dict[str, str]]) -> None:
    """Write the provenance line, the column names and the rows, gzip-compressed with
    reproducible bytes."""
    buffer = io.StringIO()
    buffer.write(header)
    w = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n", extrasaction="raise")
    w.writeheader()
    w.writerows(lines)
    target.parent.mkdir(parents=True, exist_ok=True)
    # no file name and no time stamp in the gzip header, the text compressed in one piece:
    # reproducible bytes
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz):
        gz.write(buffer.getvalue().encode("utf-8"))


# --- main -------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--destatis", help="Krankenhausverzeichnis .xlsx (else downloaded)")
    ap.add_argument("--bag", help="kzp23_data.xlsx (else downloaded)")
    ap.add_argument("--localities", help="ortschaftenverzeichnis_plz_2056.csv.zip")
    ap.add_argument("--target", type=Path, default=TARGET)
    args = ap.parse_args(argv[1:])

    de_meta, de = read_destatis(load(args.destatis, URL_DESTATIS))
    key = MunicipalityKeys(MUNICIPALITIES)
    methods_de: collections.Counter[str] = collections.Counter()
    unresolved_de = []
    for z in de:
        ars, method = key.ars(z.pop("ags"), z["postcode"], z["place"])
        z["municipality_code"] = ars
        methods_de[method or "open"] += 1
        if method not in ("key", ""):
            unresolved_de.append(f"  {method}: {z['postcode']} {z['place']} → {ars}"
                                 f" ({z['name']})")
        elif not method:
            unresolved_de.append(f"  open: {z['postcode']} {z['place']} ({z['name']})")

    ch_meta, ch = read_bag(load(args.bag, URL_BAG))
    places = Localities(load(args.localities, URL_LOCALITIES))
    methods_ch: collections.Counter[str] = collections.Counter()
    unresolved_ch = []
    for z in ch:
        number, method = places.bfs(z["postcode"], z["place"], z.pop("canton"))
        z["municipality_code"] = number
        methods_ch[method or "open"] += 1
        if method in ("postcode-share", "municipality-not-postcode", ""):
            unresolved_ch.append(f"  {method or 'open'}: {z['postcode']} {z['place']}"
                                 f" → {number} ({z['name']})")

    lines = de + ch
    found = re.search(r"(?:Gebietsstand|territory as of) ([\d-]+)", key.source)
    territory_date = found[1] if found else "?"
    write(args.target, header_line(de_meta, ch_meta, territory_date, places.state), lines)

    types = collections.Counter((z["country"], z["type"]) for z in lines)
    print(f"DE: {de_meta['khv']} hospital sites ({de_meta['hospitals']} hospitals), "
          f"{de_meta['rhv']} prevention/rehabilitation facilities, reporting year"
          f" {de_meta['year']}")
    print(f"CH: {ch_meta['sites']} sites of {ch_meta['enterprises']} hospital enterprises, "
          f"data year {ch_meta['year']}")
    for (country, type_name), n in sorted(types.items()):
        print(f"  {country} {type_name}: {n}")
    print("municipality keys DE:", dict(methods_de.most_common()))
    print("municipality keys CH:", dict(methods_ch.most_common()))
    print("DE without a direct key:", *sorted(set(unresolved_de)), sep="\n")
    print("CH ambiguous or open:", *unresolved_ch, sep="\n")
    print(f"{len(lines)} rows written to {args.target}"
          f" ({args.target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
