"""Build ``pseudonymed/daten/orphanet_praevalenz_de.csv.gz`` from Orphanet's German files.

Sources (Orphanet, licence CC BY 4.0, https://www.orphadata.com/):
``de_product9_prev.xml`` (epidemiology: prevalence classes and mean values per 100,000)
and ``de_product1.xml`` (nomenclature: German names and synonyms).  One row per disorder
name or synonym with the disorder's estimated population frequency ``p``.

Choice of the estimate per disorder, in this order of preference: point prevalence, then
prevalence at birth, then lifetime prevalence, then annual incidence (a stand-in), for
Germany, then Europe, then worldwide.  The numeric mean (per 100,000) is used when given,
else the class midpoint; "Unbekannt" becomes 1e-5 (the middle of the rare range), and
disorders known only as cases or families count as < 1/1,000,000.

Usage: python tools/orphanet_prevalence.py [de_product9_prev.xml de_product1.xml]
"""

from __future__ import annotations

import csv
import gzip
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

URL_PREV = "https://www.orphadata.com/data/xml/de_product9_prev.xml"
URL_NOM = "https://www.orphadata.com/data/xml/de_product1.xml"
TARGET = (Path(__file__).resolve().parent.parent / "pseudonymed" / "data"
          / "orphanet_praevalenz_de.csv.gz")
CATEGORIES = {
    ">1 / 1000": 2e-3, "6-9 / 10 000": 7.5e-4, "1-5 / 10 000": 3e-4, "1-9 / 100 000": 5e-5,
    "1-9 / 1 000 000": 5e-6, "<1 / 1 000 000": 5e-7, "Unbekannt": 1e-5,
    "Noch nicht beschrieben": 1e-5,
}
TYPES = {"Punktprävalenz": 0, "Prävalenz bei Geburt": 1, "Lebenszeitprävalenz": 2,
         "Jahresinzidenz": 3}
GEO = {"Germany": 0, "Deutschland": 0, "Europa": 1, "Weltweit": 2}
EXCLUDED_TYPES = {"Kategorie", "Phenom-Gruppe"}


def _load(url: str, path: str | None) -> bytes:
    if path:
        return Path(path).read_bytes()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "PseudonyMed"}),
                                timeout=300) as response:
        return response.read()


def estimate(disorder: ET.Element) -> tuple[float, str, str] | None:
    """(p, class, source) for the best prevalence entry of a disorder."""
    candidates = []
    cases = False
    for p in disorder.iter("Prevalence"):
        type_name = (p.findtext("PrevalenceType/Name") or "").strip()
        if type_name == "Fälle/Familie(n)":
            cases = True
            continue
        if type_name not in TYPES:
            continue
        category = (p.findtext("PrevalenceClass/Name") or "").strip()
        geo = (p.findtext("PrevalenceGeographic/Name") or "").strip()
        value = (p.findtext("ValMoy") or "").strip()
        try:
            mean = float(value) / 100_000 if value and float(value) > 0 else None
        except ValueError:
            mean = None
        if category not in CATEGORIES and mean is None:
            continue
        pw = mean if mean is not None else CATEGORIES[category]
        candidates.append((TYPES[type_name], GEO.get(geo, 3), pw, category or f"{value}/100000",
                           f"{type_name}, {geo}"))
    if candidates:
        candidates.sort(key=lambda k: (k[0], k[1]))
        _t, _g, pw, category, source = candidates[0]
        return pw, category, source
    if cases:
        return 5e-7, "<1 / 1 000 000", "Fälle/Familie(n)"
    return None


def main(argv: list[str]) -> int:
    prev = ET.fromstring(_load(URL_PREV, argv[1] if len(argv) > 2 else None))
    nom = ET.fromstring(_load(URL_NOM, argv[2] if len(argv) > 2 else None))
    state = prev.get("date", "")[:10]
    names: dict[str, tuple[str, list[str], str]] = {}
    for d in nom.iter("Disorder"):
        code = d.findtext("OrphaCode") or ""
        names[code] = (d.findtext("Name") or "", [s.text or "" for s in d.iter("Synonym")],
                       d.findtext("DisorderType/Name") or "")
    lines = []
    for d in prev.iter("Disorder"):
        code = d.findtext("OrphaCode") or ""
        type_name = d.findtext("DisorderType/Name") or names.get(code, ("", [], ""))[2]
        if type_name in EXCLUDED_TYPES:
            continue
        s = estimate(d)
        if s is None:
            continue
        pw, category, source = s
        name = d.findtext("Name") or names.get(code, ("", [], ""))[0]
        terms = [name] + names.get(code, ("", [], ""))[1]
        for i, b in enumerate(terms):
            b = " ".join(b.split())
            if len(b) >= 4:
                lines.append([code, b, f"{pw:.3g}", category, source,
                              "synonym" if i else "name"])
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(TARGET, "wt", encoding="utf-8", newline="") as f:
        f.write(f"# Quelle: Orphanet, Epidemiologie (de_product9_prev.xml) und Nomenklatur"
                f" (de_product1.xml), Stand {state}; Lizenz CC BY 4.0, https://www.orphadata.com/\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["orpha", "begriff", "p", "klasse", "quelle", "art"])
        w.writerows(lines)
    print(f"{len(lines)} Begriffe ({len({z[0] for z in lines})} Erkrankungen, Stand {state})"
          f" nach {TARGET} geschrieben ({TARGET.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
