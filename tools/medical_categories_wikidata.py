"""Build ``pseudonymed/daten/fachklassen_de.csv.gz``: a semantic class for every Wikidata term.

The bundled vocabulary (``fachbegriffe_de.txt.gz``) only says *that* a word is medical.  This
script records *what* it is, from the same Wikidata items (CC0 1.0): ATC code (P267) or WHO
INN (P2275) → Medikament; ICD-10 (P494) → Erkrankung; LOINC (P4338) → Laborwert; MeSH tree
numbers (P672) → A Anatomie, B Organismus, C Erkrankung (C23.888 Symptom), D Substanz,
E Verfahren, F Psyche, G Physiologie.  A term may carry several classes (one row each); the
Wikidata item is kept as the identifier so that a class can be traced.

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
TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "fachklassen_de.csv.gz"
MESH_CATEGORIES = {"A": "Anatomie", "B": "Organismus", "C": "Erkrankung", "D": "Substanz",
                   "E": "Verfahren", "F": "Psyche", "G": "Physiologie", "S": "Symptom"}
MESH_PREFIXES = ("D000", "D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008",
                 "D009", "D01", "D02", "D03", "D04", "D05", "D06", "D07")
_LABELS = """{ ?item rdfs:label ?label } UNION { ?item skos:altLabel ?label }
  FILTER(LANG(?label) = "de")"""


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
            print(f"  Versuch {attempt + 1} fehlgeschlagen ({e}); warte {wait_time}s",
                  file=sys.stderr)
            time.sleep(wait_time)
    raise RuntimeError("Wikidata-Abfrage wiederholt fehlgeschlagen")


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def property_terms(prop: str, category: str) -> list[tuple[str, str, str]]:
    lines = query(f"SELECT DISTINCT ?item ?label WHERE {{ ?item wdt:{prop} ?v . {_LABELS} }}")
    return [(z[1], category, qid(z[0])) for z in lines if len(z) >= 2]


def mesh(prefix: str) -> list[tuple[str, str, str]]:
    sparql = f"""SELECT ?item ?label (GROUP_CONCAT(DISTINCT ?k; separator=" ") AS ?klassen)
WHERE {{
  ?item wdt:P486 ?v . FILTER(STRSTARTS(?v, "{prefix}"))
  ?item wdt:P672 ?baum . FILTER(REGEX(?baum, "^[A-G]"))
  BIND(IF(STRSTARTS(?baum, "C23.888"), "S", SUBSTR(?baum, 1, 1)) AS ?k)
  {_LABELS}
}} GROUP BY ?item ?label"""
    out = []
    for z in query(sparql):
        if len(z) < 3:
            continue
        for k in sorted(set(z[2].split())):
            out.append((z[1], MESH_CATEGORIES[k], qid(z[0])))
    return out


def main(target: Path) -> None:
    lines: list[tuple[str, str, str]] = []
    steps = [("ATC (P267)", lambda: property_terms("P267", "Medikament")),
             ("INN (P2275)", lambda: property_terms("P2275", "Medikament")),
             ("ICD-10 (P494)", lambda: property_terms("P494", "Erkrankung")),
             ("LOINC (P4338)", lambda: property_terms("P4338", "Laborwert"))]
    steps += [(f"MeSH {p}*", lambda p=p: mesh(p)) for p in MESH_PREFIXES]
    for name, step in steps:
        t = time.time()
        new = step()
        lines += new
        print(f"{name}: {len(new)} Zeilen ({time.time() - t:.0f}s)")
        time.sleep(2)
    unique: dict[tuple[str, str], tuple[str, str, str]] = {}
    for term, category, code in lines:
        term = " ".join(term.split())
        if len(term) < 3 or term.isdigit():
            continue
        unique.setdefault((term.casefold(), category), (term, category, code))
    sorted_rows = sorted(unique.values(), key=lambda z: (z[0].casefold(), z[1]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wt", encoding="utf-8", newline="") as f:
        f.write("# Quelle: Wikidata (ATC P267, INN P2275, ICD-10 P494, LOINC P4338,"
                " MeSH P486/P672),"
                f" abgerufen {dt.date.today().isoformat()}; Lizenz: CC0 1.0,"
                " https://creativecommons.org/publicdomain/zero/1.0/\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["begriff", "klasse", "kennung"])
        w.writerows(sorted_rows)
    per_class: dict[str, int] = {}
    for _b, k, _q in sorted_rows:
        per_class[k] = per_class.get(k, 0) + 1
    terms = len({b.casefold() for b, _k, _q in sorted_rows})
    print(f"geschrieben: {target} ({target.stat().st_size / 1e6:.2f} MB),"
          f" {len(sorted_rows)} Zeilen, {terms} Begriffe")
    print(", ".join(f"{k} {n}" for k, n in sorted(per_class.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET)
