"""Build ``pseudonymed/data/medical_classes_de.csv.gz``: a semantic class for every Wikidata term.

The bundled vocabulary (``medical_terms_de.txt.gz``) only says *that* a word is medical.  This
script records *what* it is, from the same Wikidata items (CC0 1.0): ATC code (P267) or WHO
INN (P2275) → drug; ICD-10 (P494) → disease; LOINC (P4338) → lab_test; MeSH tree
numbers (P672) → A anatomy, B organism, C disease (C23.888 symptom), D substance,
E procedure, F psyche, G physiology.  A term may carry several classes (one row each); the
Wikidata item is kept as the code so that a class can be traced.

Usage: uv run python tools/medical_categories_wikidata.py [target.csv.gz]
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
AGENT = ("PseudonyMed/0.1 (open-source medical de-identification;"
         " https://github.com/fre-ms/PseudonyMed)")
TARGET = (Path(__file__).resolve().parent.parent / "pseudonymed" / "data"
          / "medical_classes_de.csv.gz")
HEADER = ["term", "class", "code"]
MESH_CATEGORIES = {"A": "anatomy", "B": "organism", "C": "disease", "D": "substance",
                   "E": "procedure", "F": "psyche", "G": "physiology", "S": "symptom"}
# Order of the classes of one term in the file.
CLASS_ORDER = ("anatomy", "disease", "lab_test", "drug", "organism", "physiology", "psyche",
               "substance", "symptom", "procedure")
MESH_PREFIXES = ("D000", "D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008",
                 "D009", "D01", "D02", "D03", "D04", "D05", "D06", "D07")
_LABELS = """{ ?item rdfs:label ?label } UNION { ?item skos:altLabel ?label }
  FILTER(LANG(?label) = "de")"""

Row = tuple[str, str, str]  # term, class, code


def query(sparql: str, attempts: int = 5) -> list[list[str]]:
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": sparql})
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"Accept": "text/csv", "User-Agent": AGENT})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                text = response.read().decode("utf-8")
            return list(csv.reader(io.StringIO(text)))[1:]
        except Exception as e:  # noqa: BLE001 - retry on timeouts and 429
            wait_time = 15 * (attempt + 1)
            print(f"  attempt {attempt + 1} failed ({e}); waiting {wait_time}s",
                  file=sys.stderr)
            time.sleep(wait_time)
    raise RuntimeError("Wikidata query failed repeatedly")


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def property_terms(prop: str, category: str) -> list[Row]:
    lines = query(f"SELECT DISTINCT ?item ?label WHERE {{ ?item wdt:{prop} ?v . {_LABELS} }}")
    return [(z[1], category, qid(z[0])) for z in lines if len(z) >= 2]


def mesh(prefix: str) -> list[Row]:
    sparql = f"""SELECT ?item ?label (GROUP_CONCAT(DISTINCT ?k; separator=" ") AS ?classes)
WHERE {{
  ?item wdt:P486 ?v . FILTER(STRSTARTS(?v, "{prefix}"))
  ?item wdt:P672 ?tree . FILTER(REGEX(?tree, "^[A-G]"))
  BIND(IF(STRSTARTS(?tree, "C23.888"), "S", SUBSTR(?tree, 1, 1)) AS ?k)
  {_LABELS}
}} GROUP BY ?item ?label"""
    out = []
    for z in query(sparql):
        if len(z) < 3:
            continue
        for k in sorted(set(z[2].split())):
            out.append((z[1], MESH_CATEGORIES[k], qid(z[0])))
    return out


def ordered(lines: list[Row]) -> list[Row]:
    """One row per term (case-insensitively) and class, sorted by term, then class."""
    unique: dict[tuple[str, str], Row] = {}
    for term, category, code in lines:
        term = " ".join(term.split())
        if len(term) < 3 or term.isdigit():
            continue
        unique.setdefault((term.casefold(), category), (term, category, code))
    return sorted(unique.values(), key=lambda z: (z[0].casefold(), CLASS_ORDER.index(z[1])))


def header_line(today: str) -> str:
    """The provenance line at the top of the file: source, licence and date."""
    return ("# Source: Wikidata (ATC P267, INN P2275, ICD-10 P494, LOINC P4338,"
            " MeSH P486/P672),"
            f" retrieved {today}; Licence: CC0 1.0,"
            " https://creativecommons.org/publicdomain/zero/1.0/\n")


def write(target: Path, sorted_rows: list[Row], today: str) -> None:
    """Write the provenance line, the column names and the rows, gzip-compressed with
    reproducible bytes."""
    buffer = io.StringIO()
    buffer.write(header_line(today))
    w = csv.writer(buffer, lineterminator="\n")
    w.writerow(HEADER)
    w.writerows(sorted_rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    # no file name and no time stamp in the gzip header, the text compressed in one piece:
    # reproducible bytes
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz):
        gz.write(buffer.getvalue().encode("utf-8"))


def main(target: Path) -> None:
    lines: list[Row] = []
    steps = [("ATC (P267)", lambda: property_terms("P267", "drug")),
             ("INN (P2275)", lambda: property_terms("P2275", "drug")),
             ("ICD-10 (P494)", lambda: property_terms("P494", "disease")),
             ("LOINC (P4338)", lambda: property_terms("P4338", "lab_test"))]
    steps += [(f"MeSH {p}*", lambda p=p: mesh(p)) for p in MESH_PREFIXES]
    for name, step in steps:
        t = time.time()
        new = step()
        lines += new
        print(f"{name}: {len(new)} rows ({time.time() - t:.0f}s)")
        time.sleep(2)
    sorted_rows = ordered(lines)
    write(target, sorted_rows, dt.date.today().isoformat())
    per_class: dict[str, int] = {}
    for _b, k, _q in sorted_rows:
        per_class[k] = per_class.get(k, 0) + 1
    terms = len({b.casefold() for b, _k, _q in sorted_rows})
    print(f"written: {target} ({target.stat().st_size / 1e6:.2f} MB),"
          f" {len(sorted_rows)} rows, {terms} terms")
    print(", ".join(f"{k} {n}" for k, n in sorted(per_class.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET)
