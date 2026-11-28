"""Build ``pseudonymed/data/medical_terms_de.txt.gz``: the bundled medical vocabulary (CC0).

German labels and aliases of every Wikidata item that carries an ICD-10 code (P494), an
ATC code (P267), a WHO INN (P2275) or a MeSH descriptor ID (P486) in one of the
medical MeSH trees A–G (tree code P672), fetched in chunks from the SPARQL
endpoint and written gzip-compressed, one term per line, with provenance lines
at the top.

Usage: uv run python tools/medical_terms_wikidata.py [target.txt.gz]
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
          / "medical_terms_de.txt.gz")


def query(sparql: str, attempts: int = 4) -> list[str]:
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": sparql})
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"Accept": "text/csv", "User-Agent": AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                text = response.read().decode("utf-8")
            lines = list(csv.reader(io.StringIO(text)))
            return [z[0] for z in lines[1:] if z and z[0]]
        except Exception as e:  # noqa: BLE001 - retry on timeouts and 429
            wait_time = 10 * (attempt + 1)
            print(f"  attempt {attempt + 1} failed ({e}); waiting {wait_time}s",
                  file=sys.stderr)
            time.sleep(wait_time)
    raise RuntimeError("Wikidata query failed repeatedly")


def terms(condition: str) -> set[str]:
    sparql = f"""SELECT DISTINCT ?label WHERE {{
  {condition}
  {{ ?item rdfs:label ?label }} UNION {{ ?item skos:altLabel ?label }}
  FILTER(LANG(?label) = "de")
}}"""
    return set(query(sparql))


def header_lines(count: int, today: str) -> list[str]:
    """The provenance lines at the top of the file (without line ends)."""
    return [
        f"# PseudonyMed medical vocabulary (base), generated on {today} from Wikidata"
        " (CC0 1.0).",
        "# German labels and aliases of all items with ICD-10 (P494), ATC (P267), INN"
        " (P2275) or MeSH descriptor (P486) in the trees A–G (P672).",
        "# Source: https://query.wikidata.org/ – Licence:"
        " https://creativecommons.org/publicdomain/zero/1.0/",
        f"# {count} terms, one per line.",
    ]


def write(target: Path, sorted_terms: list[str], today: str) -> None:
    """Write the provenance lines and the terms, gzip-compressed with reproducible bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    text = ("\n".join(header_lines(len(sorted_terms), today)) + "\n"
            + "\n".join(sorted_terms) + "\n")
    # no file name and no time stamp in the gzip header, the text compressed in one piece:
    # reproducible bytes
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz):
        gz.write(text.encode("utf-8"))


def main(target: Path) -> None:
    total: set[str] = set()
    steps = [("P494 ICD-10", "?item wdt:P494 ?v ."), ("P267 ATC", "?item wdt:P267 ?v ."),
             ("P2275 INN", "?item wdt:P2275 ?v .")]
    # MeSH only in its medical trees: A anatomy, B organisms, C diseases, D chemicals and
    # drugs, E techniques, F psychiatry and psychology, G phenomena.  Geography (Z), persons
    # and occupations (M), food, technology, humanities and the like are left out, so that
    # towns and surnames do not end up protected from the name recognition.
    for prefix in ("D000", "D001", "D002", "D003", "D004", "D005", "D006", "D007", "D008",
                   "D009", "D01", "D02", "D03", "D04", "D05", "D06", "D07"):
        steps.append((f"P486 MeSH {prefix}* (trees A–G)",
                      f'?item wdt:P486 ?v . FILTER(STRSTARTS(?v, "{prefix}"))'
                      ' ?item wdt:P672 ?tree . FILTER(REGEX(?tree, "^[A-G]"))'))
    for name, condition in steps:
        t = time.time()
        new = terms(condition)
        total |= new
        print(f"{name}: {len(new)} terms ({time.time() - t:.0f}s), total {len(total)}")
        time.sleep(2)
    usable = {b.strip() for b in total if len(b.strip()) >= 3 and not b.strip().isdigit()}
    cleaned = sorted(usable, key=str.casefold)
    write(target, cleaned, dt.date.today().isoformat())
    print(f"written: {target} ({target.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET)
