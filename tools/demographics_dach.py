"""Build ``pseudonymed/daten/demografie_dach.csv.gz``: population by sex and single year of age.

Germany, Austria and Switzerland, at national, state (Country, Bundesland, Kanton) and district
level (Kreis, politischer Bezirk, Bezirk); Austria and Switzerland also by municipality.  The
residual-risk check uses the counts as cohort sizes (k-map: lambda = population x share).

Sources and licences
--------------------
Germany (DE): Statistische Ämter des Bundes und der Länder, Zensus 2022, Zensusdatenbank
table ``1000A-2027`` "Personen: Geschlecht - Alter (Jahresschritte)", reference date
15 May 2022, territory of 15 May 2022.  Licence: Datenlizenz Deutschland - Namensnennung -
Version 2.0 (https://www.govdata.de/dl-de/by-2-0).  Read through the JSON interface that the
database's own web front end uses without an account
(``https://ergebnisse.zensus2022.de/proxy/api/rest/tables/<code>/...``); the documented
GENESIS web service needs a registered account.  Cells of sex x age are perturbed by the
census confidentiality procedure, so district sums deviate slightly from state totals; the
national total also contains Germans living abroad on official duty.  Persons recorded as
"divers" or "unbekannt" were redistributed to male and female by the census offices.
Hanau became a kreisfreie Stadt (independent city) on 1 January 2026 (Kreis 06415): its
row is taken from the municipality figures and subtracted from the Main-Kinzig-Kreis
(06435), so the districts match the current directory ``pseudonymed/daten/gemeinden_de.csv.gz``.

Austria (AT): Statistik Austria, open data set ``OGD_bevstandjbab2002_BevStand_2026``
"Bevölkerungsstand zu Jahresbeginn 2026" (municipalities, Vienna by district, single years
0-99 and 100+, sex; 1 January 2026) and the list of political districts ``polbezirke.csv``.
Licence: CC BY 4.0, attribution "Datenquelle: Statistik Austria - data.statistik.gv.at".
Entries "divers", "inter", "offen" and "kein Eintrag" are imputed to male or female by
Statistik Austria.  Districts and states are summed from the municipalities; Vienna's 23
Gemeindebezirke (901-923) stand at district level.

Switzerland (CH): Bundesamt für Statistik (BFS), STATPOP, STAT-TAB table
``px-x-0102010000_101`` "Ständige und nichtständige Wohnbevölkerung nach institutionellen
Gliederungen, Staatsangehörigkeit (Kategorie), Geschlecht und Alter", permanent resident
population on 31 December of the latest year, queried through the PxWeb API in batches (the
server refuses requests of more than about 100,000 cells).  Terms of use OPEN BY: free use,
attribution compulsory (https://www.bfs.admin.ch/asset/de/px-x-0102010000_101).

Output
------
A comment line ``# Quelle: ...; Lizenz: ...; Stand: ...`` and the columns
``land,ebene,kennung,name,geschlecht,alter_von,alter_bis,anzahl,stichtag``.  ``code`` is
the official key (DE: AGS prefix, AT: Gemeinde-/Bezirkskennziffer, CH: BFS number; the
country code for ``layer`` staat).  Ages 0-99 are single years, 100 is the open class
(``alter_bis`` empty).  Rows with a count of zero are omitted.

Usage: python -I tools/demographics_dach.py [--raw FOLDER] [--target FILE]

With ``--raw`` the downloads are kept in (and re-read from) ``FOLDER/<source>/``.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "demografie_dach.csv.gz"
MUNICIPALITIES_DE = TARGET.parent / "gemeinden_de.csv.gz"
HEADER = ["land", "ebene", "kennung", "name", "geschlecht", "alter_von", "alter_bis", "anzahl",
          "stichtag"]
AGENT = {"User-Agent": "PseudonyMed build script (https://github.com/fre-ms/PseudonyMed)"}
OPEN_AGE = 100  # first age of the open-ended class in all three sources

CENSUS = "https://ergebnisse.zensus2022.de/proxy/api/rest/tables/1000A-2027"
CENSUS_DATE = "2022-05-15"
# New districts since the census: new key -> (old district, municipalities moved).
NEW_DISTRICTS_DE = {"06415": ("06435", ["064350014014"])}  # Hanau, kreisfrei since 1.1.2026

AT_YEAR = 2026
AT_DATA = "https://data.statistik.gv.at/data/OGD_bevstandjbab2002_BevStand_{jahr}{teil}.csv"
AT_DISTRICTS = "https://www.statistik.at/verzeichnis/reglisten/polbezirke.csv"

CH_TABLE = ("https://www.pxweb.bfs.admin.ch/api/v1/de/px-x-0102010000_101/"
            "px-x-0102010000_101.px")
CH_REGIONS_PER_QUERY = 440  # x 2 sexes x 101 ages stays below the 100,000-cell limit
CH_CANTONS = ["ZH", "BE", "LU", "UR", "SZ", "OW", "NW", "GL", "ZG", "FR", "SO", "BS", "BL",
              "SH", "AR", "AI", "SG", "GR", "AG", "TG", "TI", "VD", "VS", "NE", "GE", "JU"]

# Expected national totals (rough bounds for the plausibility check).
EXPECTED = {"DE": (80_000_000, 86_000_000), "AT": (8_800_000, 9_600_000),
            "CH": (8_700_000, 9_500_000)}
TOLERANCE = {"DE": 0.002, "AT": 0.0, "CH": 0.0}  # relative deviation child sums vs. parent

Row = tuple[str, str, str, str, str, int, int | None, int, str]


# --------------------------------------------------------------------------- downloads

def _fetch(url: str, raw: Path | None, file: str, data: bytes | None = None,
           attempts: int = 3, timeout: int = 600) -> bytes:
    """Return the body of ``url`` (POST if ``data``), cached as ``roh/datei`` if given."""
    path = raw / file if raw else None
    if path and path.exists():
        return path.read_bytes()
    header = dict(AGENT)
    if data is not None:
        header["Content-Type"] = "application/json"
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=header)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
            break
        except OSError as error:
            if attempt == attempts:
                raise
            print(f"  {url}: {error}; neuer Versuch in {20 * attempt} s", file=sys.stderr)
            time.sleep(20 * attempt)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return content


def _jsonstat(ds: dict) -> Iterator[tuple[dict[str, str], float | None, str]]:
    """Yield ``({dimension: category}, value, status)`` for every cell of a JSON-stat set."""
    dims, size = ds["id"], ds["size"]
    inverse = [{i: k for k, i in ds["dimension"][d]["category"]["index"].items()} for d in dims]
    steps = [1] * len(size)
    for j in range(len(size) - 2, -1, -1):
        steps[j] = steps[j + 1] * size[j + 1]
    status = ds.get("status")
    for pos, value in enumerate(ds["value"]):
        cell = {d: inverse[j][(pos // steps[j]) % size[j]] for j, d in enumerate(dims)}
        st = status[pos] if isinstance(status, list) else (status or {}).get(str(pos), "")
        yield cell, value, st or ""


# --------------------------------------------------------------------------- Germany

def _census_age(code: str) -> tuple[int, int | None] | None:
    if code == "ALTERU01":
        return 0, 0
    m = re.fullmatch(r"ALTER(\d{3})(UM)?", code)
    if not m:
        return None
    a = int(m.group(1))
    return (a, None) if m.group(2) else (a, a)


def germany(raw: Path | None) -> tuple[list[Row], dict[tuple[str, str], str]]:
    """Rows for Germany (state, Länder, Kreise) and the parent of each Land and Kreis."""
    structure = json.loads(_fetch(f"{CENSUS}/structure?language=de", raw, "structure.json"))
    names = {k: v["label"]["de"] for k, v in structure["variableValues"].items()}
    names["DG"] = "Deutschland"
    values: dict[tuple[str, str, str, int, int | None], int] = {}
    needed = {g for _old, municipalities in NEW_DISTRICTS_DE.values() for g in municipalities}
    queries = [("v3", "GEOBL1"), ("v6", "GEOLK4")] + ([("v8", "GEOGM4")] if needed else [])
    for block, region in queries:
        state = json.loads(json.dumps(structure["initialState"]))
        if state["variableBlocks"][block]["mainVariable"] != region:
            raise SystemExit(f"Zensus: Block {block} ist nicht {region}, Tabelle geändert?")
        state["tableStructure"]["colTitle"][1]["blockCode"] = block
        raw_json = _fetch(f"{CENSUS}/data?language=de", raw, f"data_{region}.json",
                          json.dumps(state).encode())
        for ds in json.loads(raw_json)["data"]:
            layer = next((r for r in ("GEODL1", region) if r in ds["id"]), None)
            if layer is None:
                continue
            for cell, value, status in _jsonstat(ds):
                if layer == "GEOGM4" and cell[layer] not in needed:
                    continue  # municipalities only for the territorial changes below
                content = cell["content"].split("$")
                age = _census_age(cell["ALTER1"])
                g = {"GESM": "m", "GESW": "w"}.get(cell["GESCH1"])
                if len(content) < 2 or not content[1].startswith("ID") or not age or not g:
                    continue
                count = 0 if status == "-" or value is None else round(value)
                values[(layer, cell[layer], g, *age)] = count
    # Territorial changes since the census (Hanau).
    for new, (old, municipalities) in NEW_DISTRICTS_DE.items():
        if ("GEOLK4", new, "m", 0, 0) in values:
            continue
        for (layer, k, g, a, b), n in list(values.items()):
            if layer == "GEOGM4" and k in municipalities:
                key = ("GEOLK4", new, g, a, b)
                values[key] = values.get(key, 0) + n
                old_key = ("GEOLK4", old, g, a, b)
                values[old_key] = max(0, values.get(old_key, 0) - n)
        names[new] = names[municipalities[0]] if len(municipalities) == 1 else new
    lines: list[Row] = []
    parents: dict[tuple[str, str], str] = {}
    levels = {"GEODL1": "staat", "GEOBL1": "land", "GEOLK4": "kreis"}
    for (layer, k, g, a, b), n in values.items():
        if layer not in levels:
            continue
        region_id = "DE" if layer == "GEODL1" else k
        lines.append(("DE", levels[layer], region_id, names.get(k, k), g, a, b, n,
                      CENSUS_DATE))
        if layer == "GEOBL1":
            parents[("land", k)] = "DE"
        elif layer == "GEOLK4":
            parents[("kreis", k)] = k[:2]
    return lines, parents


# --------------------------------------------------------------------------- Austria

def _csv_at(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))


def austria(raw: Path | None) -> tuple[list[Row], dict[tuple[str, str], str]]:
    """Rows for Austria: municipalities summed up to districts, Bundesländer and state."""
    def part(t: str) -> bytes:
        name = f"BevStand_{AT_YEAR}{t}.csv"
        return _fetch(AT_DATA.format(jahr=AT_YEAR, teil=t), raw, name)

    municipality_names = {r[0].removeprefix("GRGEMAKT-"): re.sub(r"\s*<\d+>$", "", r[1])
                          for r in _csv_at(part("_C-GRGEMAKT-0"))[1:] if r}
    age: dict[str, tuple[int, int | None]] = {}
    for r in _csv_at(part("_C-GALTEJ112-0"))[1:]:
        m = re.match(r"(\d+) Jahr", r[1]) if r else None
        if m:
            a = int(m.group(1))
            age[r[0]] = (a, None) if "älter" in r[1] or "mehr" in r[1] else (a, a)
    sex = {"C11-1": "m", "C11-2": "w"}
    districts: dict[str, str] = {}
    countries: dict[str, str] = {}
    for r in _csv_at(_fetch(AT_DISTRICTS, raw, "polbezirke.csv")):
        if len(r) >= 5 and r[0].isdigit() and r[4].isdigit():
            countries[r[0]] = r[1]
            if r[4] != "900":  # Vienna as a whole is the Bundesland
                districts[r[4]] = re.sub(r"\s+", " ", r[3]).strip()
    sums: dict[tuple[str, str, str, int, int | None], int] = defaultdict(int)
    reference_date = f"{AT_YEAR}-01-01"
    header, *data = _csv_at(part(""))
    column = {name: i for i, name in enumerate(header)}
    for r in data:
        if not r:
            continue
        mun = r[column["C-GRGEMAKT-0"]].removeprefix("GRGEMAKT-")
        g = sex[r[column["C-C11-0"]]]
        a, b = age[r[column["C-GALTEJ112-0"]]]
        n = int(r[column["F-ISIS-1"]])
        for layer, k in (("gemeinde", mun), ("kreis", mun[:3]), ("land", mun[0]),
                         ("staat", "AT")):
            sums[(layer, k, g, a, b)] += n
    unknown = {k for (e, k, *_r) in sums if e == "kreis" and k not in districts}
    if unknown:
        raise SystemExit(f"AT: Bezirke ohne Namen in polbezirke.csv: {sorted(unknown)}")
    names = {"gemeinde": municipality_names, "kreis": districts, "land": countries,
             "staat": {"AT": "Österreich"}}
    lines = [("AT", e, k, names[e].get(k, k), g, a, b, n, reference_date)
             for (e, k, g, a, b), n in sums.items()]
    parents = {}
    for e, k, *_r in sums:
        if e == "gemeinde":
            parents[(e, k)] = k[:3]
        elif e == "kreis":
            parents[(e, k)] = k[0]
        elif e == "land":
            parents[(e, k)] = "AT"
    return lines, parents


# --------------------------------------------------------------------------- Switzerland

def switzerland(raw: Path | None) -> tuple[list[Row], dict[tuple[str, str], str]]:
    """Rows for Switzerland: state, cantons, districts and municipalities from STATPOP."""
    meta = json.loads(_fetch(CH_TABLE, raw, "metadaten.json"))
    var = {v["code"]: v for v in meta["variables"]}
    region = next(c for c in var if c.startswith("Kanton"))
    year = max(var["Jahr"]["values"])
    reference_date = f"{year}-12-31"
    # Classify the regions by their label prefix and remember the hierarchy.
    units: dict[str, tuple[str, str, str]] = {}  # code -> (ebene, kennung, name)
    parents: dict[tuple[str, str], str] = {}
    canton = district = ""
    for code, text in zip(var[region]["values"], var[region]["valueTexts"], strict=True):
        if text.startswith(">>"):
            district = str(int(code))
            units[code] = ("kreis", district, text[2:].strip())
            parents[("kreis", district)] = canton
        elif text.startswith("......"):
            number, _, name = text.lstrip(".").partition(" ")
            region_id = str(int(number))
            units[code] = ("gemeinde", region_id, name.strip())
            parents[("gemeinde", region_id)] = district
        elif code == "8100":
            units[code] = ("staat", "CH", text.strip())
        elif code in CH_CANTONS:
            canton = str(CH_CANTONS.index(code) + 1)
            units[code] = ("land", canton, text.lstrip("- ").strip())
            parents[("land", canton)] = "CH"
        else:
            units[code] = ("", code, text)  # "Ohne Angabe": checked, not written
    age_codes = [a for a in var["Alter"]["values"] if a.lstrip("-").isdigit() and
                 int(a) >= 0]
    pending = {a for a, t in zip(var["Alter"]["values"], var["Alter"]["valueTexts"], strict=True)
               if "mehr" in t or "älter" in t}
    codes = list(units)
    lines: list[Row] = []
    unspecified = 0
    for num, start in enumerate(range(0, len(codes), CH_REGIONS_PER_QUERY), 1):
        part = codes[start:start + CH_REGIONS_PER_QUERY]
        query = {"query": [
            {"code": "Jahr", "selection": {"filter": "item", "values": [year]}},
            {"code": region, "selection": {"filter": "item", "values": part}},
            {"code": "Bevölkerungstyp", "selection": {"filter": "item", "values": ["1"]}},
            {"code": "Staatsangehörigkeit (Kategorie)",
             "selection": {"filter": "item", "values": ["-99999"]}},
            {"code": "Geschlecht", "selection": {"filter": "item", "values": ["1", "2"]}},
            {"code": "Alter", "selection": {"filter": "item", "values": age_codes}},
        ], "response": {"format": "json-stat2"}}
        print(f"  BFS-Abfrage {num}: {len(part)} Regionen", file=sys.stderr)
        ds = json.loads(_fetch(CH_TABLE, raw, f"daten_{year}_{num:02d}.json",
                               json.dumps(query).encode()))
        for cell, value, _status in _jsonstat(ds):
            layer, region_id, name = units[cell[region]]
            n = int(value or 0)
            if not layer:
                unspecified += n
                continue
            a = int(cell["Alter"])
            b = None if cell["Alter"] in pending else a
            g = {"1": "m", "2": "w"}[cell["Geschlecht"]]
            lines.append(("CH", layer, region_id, name, g, a, b, n, reference_date))
    if unspecified:
        print(f"  CH: {unspecified} Personen ohne Regionsangabe nicht übernommen",
              file=sys.stderr)
    return lines, parents


# --------------------------------------------------------------------------- checks

def _number(n: int) -> str:
    """German thousands separator."""
    return f"{n:,}".replace(",", ".")


def check(lines: list[Row], parents: dict[tuple[str, str, str], str]) -> list[str]:
    """Plausibility checks; returns the problems found (empty list = all fine)."""
    problems: list[str] = []
    sum_total: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for country, layer, region_id, _n, g, _a, _b, n, _s in lines:
        sum_total[(country, layer, region_id, g)] += n
    parent_levels = {"land": "staat", "kreis": "land", "gemeinde": "kreis"}
    for country in ("DE", "AT", "CH"):
        total = sum(v for (ctry, e, _k, _g),
                    v in sum_total.items() if ctry == country and e == "staat")
        lo, hi = EXPECTED[country]
        count = {e: len({k for (ctry, ee, k, _g) in sum_total if ctry == country and ee == e})
                 for e in ("land", "kreis", "gemeinde")}
        print(f"{country}: {_number(total)} Personen; {count['land']} Länder,"
              f" {count['kreis']} Kreise, {count['gemeinde']} Gemeinden")
        if not lo <= total <= hi:
            problems.append(f"{country}: Summe {total} außerhalb {lo}-{hi}")
        for layer, parent_level in parent_levels.items():
            children: dict[tuple[str, str], int] = defaultdict(int)
            for (ctry, e, k, g), v in sum_total.items():
                if ctry == country and e == layer:
                    parent = parents.get((country, layer, k))
                    if parent is None:
                        problems.append(f"{country} {layer} {k}: keine übergeordnete Einheit")
                        continue
                    children[(parent, g)] += v
            if not children:
                continue
            for (parent, g), v in sorted(children.items()):
                expected = sum_total.get((country, parent_level, parent, g))
                if expected is None:
                    problems.append(f"{country} {parent_level} {parent} fehlt")
                    continue
                dev = (v - expected) / expected if expected else 0.0
                if abs(dev) > TOLERANCE[country]:
                    problems.append(f"{country} {layer}->{parent_level} {parent} {g}: "
                                    f"{v} statt {expected} ({dev:+.4%})")
            actual = sum(children.values())
            expected = sum(v for (ctry, e, _k, _g), v in sum_total.items()
                           if ctry == country and e == parent_level)
            largest = max(abs(v - sum_total[(country, parent_level, parent, g)])
                          / sum_total[(country, parent_level, parent, g)]
                          for (parent, g), v in children.items()
                          if sum_total.get((country, parent_level, parent, g)))
            print(f"  Summe {layer} = {_number(actual)} gegen {parent_level} = {_number(expected)}"
                  f" ({(actual - expected) / expected:+.4%}; größte Abweichung je Einheit und"
                  f" Geschlecht {largest:.4%})")
    return problems


def match_municipalities_de(lines: list[Row]) -> None:
    """Report districts of the bundled municipality directory missing from the data."""
    if not MUNICIPALITIES_DE.exists():
        return
    with gzip.open(MUNICIPALITIES_DE, "rt", encoding="utf-8") as f:
        next(f)
        districts = {r["ars"][:5] for r in csv.DictReader(f) if int(r["einwohner"] or 0) > 0}
    existing = {z[2] for z in lines if z[0] == "DE" and z[1] == "kreis"}
    print(f"  Abgleich mit gemeinden_de.csv.gz: {len(districts & existing)} von {len(districts)}"
          f" Kreisen gefunden; fehlend: {sorted(districts - existing) or 'keine'};"
          f" überzählig: {sorted(existing - districts) or 'keine'}")


# --------------------------------------------------------------------------- output

def header_line(reference_dates: dict[str, str]) -> str:
    return (
        "# Quelle: DE: Statistische Ämter des Bundes und der Länder, Zensus 2022,"
        " Zensusdatenbank Tabelle 1000A-2027 (Gebietsstand 15.05.2022, Hanau als kreisfreie"
        " Stadt nachgeführt) | AT: Statistik Austria - data.statistik.gv.at,"
        f" OGD_bevstandjbab2002_BevStand_{AT_YEAR} und polbezirke.csv | CH: Bundesamt für"
        " Statistik (BFS), STATPOP, Ständige Wohnbevölkerung, STAT-TAB px-x-0102010000_101,"
        " https://www.bfs.admin.ch/asset/de/px-x-0102010000_101 | ausgewählt, umgeformt und"
        " für AT summiert von PseudonyMed, Zeilen mit 0 Personen ausgelassen;"
        " Lizenz: DE: Datenlizenz Deutschland - Namensnennung - Version 2.0"
        " (https://www.govdata.de/dl-de/by-2-0) | AT: CC BY 4.0"
        " (https://creativecommons.org/licenses/by/4.0/) | CH: OPEN BY, Quellenangabe Pflicht"
        " (https://statistik.admin.ch/de/nutzungsbedingungen);"
        f" Stand: DE {reference_dates['DE']}, AT {reference_dates['AT']},"
        f" CH {reference_dates['CH']}\n"
    )


def write(lines: list[Row], target: Path) -> None:
    country_order = {"DE": 0, "AT": 1, "CH": 2}
    level_order = {"staat": 0, "land": 1, "kreis": 2, "gemeinde": 3}
    lines = sorted((z for z in lines if z[7] > 0),
                   key=lambda z: (country_order[z[0]], level_order[z[1]], z[2].zfill(12), z[4],
                                  z[5]))
    reference_dates = {z[0]: z[8] for z in lines}
    buffer = io.StringIO()
    buffer.write(header_line(reference_dates))
    w = csv.writer(buffer, lineterminator="\n")
    w.writerow(HEADER)
    for z in lines:
        w.writerow([*z[:6], "" if z[6] is None else z[6], *z[7:]])
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(buffer.getvalue().encode("utf-8"))
    print(f"{len(lines)} Zeilen nach {target} geschrieben ({target.stat().st_size / 1e6:.2f} MB)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--raw", type=Path, help="folder that keeps the downloads")
    parser.add_argument("--target", type=Path, default=TARGET, help="output file")
    args = parser.parse_args(argv[1:])

    def folder(name: str) -> Path | None:
        return args.raw / name if args.raw else None

    lines: list[Row] = []
    parents: dict[tuple[str, str, str], str] = {}
    for country, function, source in (("DE", germany, "zensus2022_1000A-2027"),
                                      ("AT", austria, "statistik_austria_ogd"),
                                      ("CH", switzerland, "bfs_pxweb")):
        print(f"{country}: lade {source}", file=sys.stderr)
        z, e = function(folder(source))
        lines += z
        parents.update({(country, layer, k): v for (layer, k), v in e.items()})
    problems = check(lines, parents)
    match_municipalities_de(lines)
    if problems:
        print(f"{len(problems)} Probleme, nichts geschrieben:", *problems[:30], sep="\n  ")
        return 1
    write(lines, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
