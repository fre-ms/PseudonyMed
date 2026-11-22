"""Build ``pseudonymed/daten/gemeinden_de.csv.gz`` from the Destatis municipality directory.

Source: Statistisches Bundesamt (Destatis), Gemeindeverzeichnis-Informationssystem GV-ISys,
quarterly extract "Alle politisch selbständigen Gemeinden mit ausgewählten Merkmalen"
(``AuszugGV3QAktuell.xlsx``), licence Datenlizenz Deutschland – Namensnennung – Version 2.0
(https://www.govdata.de/dl-de/by-2-0).  The workbook is read with the standard library
only (a .xlsx is a zip of XML), rows of record type 60 (municipalities) are kept.

Usage: python tools/municipalities_destatis.py [AuszugGV3QAktuell.xlsx]
"""

from __future__ import annotations

import csv
import gzip
import io
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

URL = ("https://www.destatis.de/DE/Themen/Laender-Regionen/Regionales/Gemeindeverzeichnis/"
       "Administrativ/Archiv/GVAuszugQ/AuszugGV3QAktuell.xlsx?__blob=publicationFile")
TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "gemeinden_de.csv.gz"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_SUFFIX = re.compile(r",\s*(?:Stadt|St\.?|M\.?|Markt|GKSt|Hansestadt|Landeshauptstadt|"
                     r"Kreisfreie Stadt|Universitätsstadt|Große Kreisstadt|Wissenschaftsstadt|"
                     r"Lutherstadt|Hansestadt|Kurort|Bergstadt|Flecken|Bad)\b.*$")


def _cells(line: ET.Element, strings: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in line.findall("m:c", NS):
        ref = c.get("r", "")
        column = "".join(ch for ch in ref if ch.isalpha())
        v = c.findtext("m:v", default="", namespaces=NS)
        if c.get("t") == "s" and v:
            v = strings[int(v)]
        elif c.get("t") == "inlineStr":
            v = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
        out[column] = v.strip()
    return out


def read(xlsx: bytes) -> tuple[str, list[dict[str, str]]]:
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
    strings = ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
               for si in ss.findall("m:si", NS)]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheet = next(s.get("name") for s in wb.find("m:sheets", NS) if "Gemeinden" in s.get("name"))
    state = re.sub(r"\D", "", sheet)[-8:]
    state = f"{state[4:]}-{state[2:4]}-{state[:2]}" if len(state) == 8 else sheet
    data = ET.fromstring(z.read("xl/worksheets/sheet2.xml")).find("m:sheetData", NS)
    municipalities = []
    for line in data:
        c = _cells(line, strings)
        if c.get("A") != "60":
            continue
        name = _SUFFIX.sub("", c.get("H", "")).strip()
        population = c.get("J", "").replace(".", "")
        if not name or not population.isdigit():
            continue
        municipalities.append({
            "name": name, "einwohner": int(population), "plz": c.get("N", ""),
            "land": c.get("C", ""),
            "ars": "".join(c.get(sp, "") for sp in "CDEFG"),
        })
    return state, municipalities


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        raw = Path(argv[1]).read_bytes()
    else:
        request = urllib.request.Request(URL, headers={"User-Agent": "PseudonyMed"})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    state, municipalities = read(raw)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(TARGET, "wt", encoding="utf-8", newline="") as f:
        f.write(f"# Quelle: Statistisches Bundesamt (Destatis), Gemeindeverzeichnis GV-ISys,"
                f" Gebietsstand {state}; Lizenz: Datenlizenz Deutschland – Namensnennung – 2.0\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["name", "einwohner", "plz", "land", "ars"])
        for g in sorted(municipalities, key=lambda g: (g["name"], -g["einwohner"])):
            w.writerow([g["name"], g["einwohner"], g["plz"], g["land"], g["ars"]])
    print(f"{len(municipalities)} Gemeinden (Stand {state}) nach {TARGET} geschrieben"
          f" ({TARGET.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
