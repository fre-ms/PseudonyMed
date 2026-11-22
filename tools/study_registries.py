"""Build ``pseudonymed/daten/studien_dach.csv.gz``: clinical studies with sites in DE, AT or CH.

A named study ("Teilnahme an der XY-Studie") narrows down who a patient can be, the more so
the smaller the study and the fewer its centres in the DACH region.  This script collects
acronym, titles, size and DACH sites of registered studies so that the residual-risk check
can look them up.

Sources:

1. ClinicalTrials.gov, U.S. National Library of Medicine, API v2
   (https://clinicaltrials.gov/api/v2/studies): every study, interventional or observational,
   with at least one location in Germany, Austria or Switzerland.  Use is governed by the
   ClinicalTrials.gov Terms and Conditions (https://clinicaltrials.gov/about-site/terms-conditions):
   free of charge, redistribution allowed provided that the source is attributed as
   ClinicalTrials.gov, the date the data were processed is displayed, modifications are stated
   and no proprietary rights are claimed; outside the United States NLM asserts copyright.
   Pages are fetched with ``pageSize=1000`` and ``pageToken``, at most one request per second,
   and cached per data timestamp, so an interrupted run resumes where it stopped.
2. DRKS – Deutsches Register Klinischer Studien (BfArM), full JSON export
   (https://drks.de/search/de/download/all-json, documented in the BfArM's public help, no
   account).  The DRKS terms of use
   (https://www.bfarm.de/DE/Das-BfArM/Aufgaben/Deutsches-Register-Klinischer-Studien/Studie-suchen/_node.html#nutzungsbedingungen)
   put entries registered from 2025 (or last updated in 2025) under CC BY 4.0, while older
   entries may be subject to their authors' copyright and must not be redistributed without
   consent.  Only entries registered from 1 January 2025 are therefore kept by default (the
   unambiguous reading); ``--drks-scope aktualisiert-ab-2025`` also keeps older entries last
   updated in 2025 or later.

Modifications (stated in the file header as the terms require): only studies with a DACH
site; selected fields; titles kept in full unless ``--title-max`` or ``--without-title`` is given;
at most 20 DACH cities, without placeholders such as "Online" or "bundesweit", postcodes and
country suffixes; DRKS status codes mapped to the ClinicalTrials.gov vocabulary; secondary
identifiers reduced to register numbers (NCT, DRKS, EudraCT, EU CT, ISRCTN, UTN and other WHO
primary registers).

Columns: register, kennung, akronym, kurztitel, titel, teilnehmer, teilnehmer_art
(``planned``/``actual``, empty if the register does not say), zentren_dach (registered
sites in DE/AT/CH, placeholders included), laender_dach, orte_dach, status, start,
weitere_kennungen.  For ClinicalTrials.gov, ``kurztitel`` is the brief and ``title`` the
official title (empty if identical); for DRKS, the German and the English title.  DRKS sites
carry no country: DRKS studies whose recruitment countries exclude DE, AT and CH are dropped;
for studies that also recruit elsewhere (or name no country), only sites in a known DACH place
(Destatis municipalities, ClinicalTrials.gov DACH cities named by at least three studies) count.

Usage: python -I tools/study_registries.py [--cache DIR] [--drks-json FILE]
       [--without-drks] [--drks-scope registriert-ab-2025|aktualisiert-ab-2025]
       [--title-max N] [--without-title]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import re
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "pseudonymed" / "data" / "studien_dach.csv.gz"
MUNICIPALITIES = ROOT / "pseudonymed" / "data" / "gemeinden_de.csv.gz"
AGENT = "PseudonyMed-studienregister/1.0 (+https://github.com/fre-ms/PseudonyMed)"

CTG_STUDIES = "https://clinicaltrials.gov/api/v2/studies"
CTG_VERSION = "https://clinicaltrials.gov/api/v2/version"
CTG_TERMS = "https://clinicaltrials.gov/about-site/terms-conditions"
CTG_FILTER = ("AREA[LocationCountry]Germany OR AREA[LocationCountry]Austria"
              " OR AREA[LocationCountry]Switzerland")
CTG_FIELDS = ",".join([
    "NCTId", "Acronym", "BriefTitle", "OfficialTitle", "SecondaryIdInfo", "OrgStudyIdInfo",
    "OverallStatus", "StartDate", "EnrollmentInfo", "LocationCity", "LocationCountry",
])
CTG_COUNTRIES = {"Germany": "DE", "Austria": "AT", "Switzerland": "CH"}

DRKS_EXPORT = "https://drks.de/search/de/download/all-json"
DRKS_TERMS = ("https://www.bfarm.de/DE/Das-BfArM/Aufgaben/Deutsches-Register-Klinischer-"
              "Studien/Studie-suchen/_node.html#nutzungsbedingungen")
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
# DRKS recruitment status -> ClinicalTrials.gov overall status.
DRKS_STATUS = {
    "PENDING": "NOT_YET_RECRUITING", "RECRUITING": "RECRUITING",
    "INVITE_ONLY": "ENROLLING_BY_INVITATION", "SUSPENDED": "SUSPENDED",
    "COMPLETE_FOLLOW_UP_CONTINUING": "ACTIVE_NOT_RECRUITING",
    "COMPLETE_FOLLOW_UP_COMPLETE": "COMPLETED", "DISCONTINIUED": "TERMINATED",
    "DISCONTINUED": "TERMINATED", "WITHDRAWN": "WITHDRAWN",
}

DACH = ("DE", "AT", "CH")
MAX_PLACES = 20
MIN_STUDIES_PER_PLACE = 3  # ClinicalTrials.gov cities named less often may be data-entry errors
_PLACEHOLDER = re.compile(
    r"(?:online.*|keine angabe|k\.? ?a\.?|n/?a|none|unknown|tbd|diverse|various|several"
    r"|multiple.*|many locations|investigat\w* (?:centers?|centres?|sites?)|(?:research|study)"
    r" sites?|(?:deutschland|bundes|europa|welt) ?weit|deutschland|germany|österreich|austria"
    r"|schweiz|switzerland|universität|hochschule|klinik|[-.?]+)")
_COUNTRY_SUFFIX = re.compile(r"\s*(?:,|-|–)\s*(?:deutschland|germany|österreich|austria|schweiz"
                             r"|switzerland)\s*$", re.I)
PAUSE = 1.1  # seconds between requests to the same server
HEADER = ["register", "kennung", "akronym", "kurztitel", "titel", "teilnehmer", "teilnehmer_art",
          "zentren_dach", "laender_dach", "orte_dach", "status", "start", "weitere_kennungen"]

# Register numbers worth keeping as secondary identifiers.  The EU CT pattern must be tried
# before EudraCT, whose shape is a prefix of it.
_CODES = [
    (re.compile(r"\bNCT\s?(\d{8})\b", re.I), "NCT{}"),
    (re.compile(r"\bDRKS\s?(\d{8})\b", re.I), "DRKS{}"),
    (re.compile(r"\bISRCTN\s?(\d{8})\b", re.I), "ISRCTN{}"),
    (re.compile(r"\b((?:19|20)\d{2}-5\d{5}-\d{2}-\d{2})\b"), "EUCT:{}"),
    (re.compile(r"\b((?:19|20)\d{2}-\d{6}-\d{2})\b(?!-\d)"), "EudraCT:{}"),
    (re.compile(r"\b(U1111-\d{4}-\d{4})\b", re.I), "UTN:{}"),
    (re.compile(r"\b(ACTRN\d{14}|ChiCTR[-\dA-Z]{6,}|CTRI/\d{4}/\d+/\d+|jRCT\w{8,}"
                r"|NTR\d{3,}|KCT\d{7}|IRCT\w{8,}|PACTR\d{15}|SLCTR/\d{4}/\d+"
                r"|RBR-\w{6,}|TCTR\d{11}|ITMCTR\d+|LBCTR\d+|CTIS\d+)\b", re.I), "{}"),
]


# --------------------------------------------------------------------------- downloads


def _save(file: Path, data: bytes) -> None:
    """Write atomically, so that an interrupted run never leaves a truncated cache file."""
    part = file.with_name(file.name + ".teil")
    part.write_bytes(data)
    part.replace(file)


def _fetch(url: str, attempts: int = 6, timeout: int = 120) -> bytes:
    """GET ``url`` with retries and exponential back-off on server or network errors."""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        delay = 2 ** (attempt + 1)
        print(f"  Fehler bei {url[:80]}…, neuer Versuch in {delay} s", file=sys.stderr)
        time.sleep(delay)
    raise RuntimeError("unreachable")


def ctgov_pages(cache: Path) -> tuple[str, Iterator[dict]]:
    """Download (or resume) all DACH pages; return the data timestamp and the studies."""
    stamp = json.loads(_fetch(CTG_VERSION))["dataTimestamp"]
    folder = cache / f"ctgov_{re.sub(r'[^0-9T]', '', stamp)}"
    folder.mkdir(parents=True, exist_ok=True)
    number, token = 1, None
    while True:
        file = folder / f"seite_{number:04d}.json"
        if file.exists():
            page = json.loads(file.read_text(encoding="utf-8"))
        else:
            parameter = {"filter.advanced": CTG_FILTER, "fields": CTG_FIELDS,
                         "pageSize": "1000", "countTotal": "true" if number == 1 else "false"}
            if token:
                parameter["pageToken"] = token
            time.sleep(PAUSE)
            raw = _fetch(f"{CTG_STUDIES}?{urllib.parse.urlencode(parameter)}")
            page = json.loads(raw)
            _save(file, raw)
            total = page.get("totalCount")
            print(f"  ClinicalTrials.gov Seite {number}"
                  + (f" (insgesamt {total} Studien)" if total else ""), file=sys.stderr)
        token = page.get("nextPageToken")
        if not token:
            break
        number += 1

    def studies() -> Iterator[dict]:
        for file in sorted(folder.glob("seite_*.json")):
            yield from json.loads(file.read_text(encoding="utf-8")).get("studies", [])

    return stamp[:10], studies()


def drks_export(cache: Path, path: str | None) -> tuple[str, list[dict]]:
    """Load the DRKS full export from ``path`` or download today's copy once."""
    if path:
        file = Path(path)
    else:
        file = cache / f"drks_all_{dt.date.today():%Y%m%d}.zip"
        if not file.exists():
            print("  DRKS-Gesamtexport wird geladen …", file=sys.stderr)
            _save(file, _fetch(DRKS_EXPORT, timeout=900))
    raw = file.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(raw)):
        z = zipfile.ZipFile(io.BytesIO(raw))
        name = next(n for n in z.namelist() if n.lower().endswith(".json"))
        state = re.search(r"(\d{4})(\d{2})(\d{2})", name)
        raw = z.read(name)
    else:
        state = re.search(r"(\d{4})(\d{2})(\d{2})", file.name)
    date = "-".join(state.groups()) if state else dt.date.today().isoformat()
    return date, json.loads(raw)


# --------------------------------------------------------------------------- helpers


def _text(value: object) -> str:
    """Single-line text without the separator used in list columns."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace("_x000D_", " ")).replace(" | ", " / ").strip()


def _shorten(text: str, length: int) -> str:
    """Shorten ``text`` to ``length`` characters; 0 means no limit."""
    if length <= 0 or len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"


def _codes(values: list[str], own: str) -> str:
    """Register numbers found in ``values``, normalised, without the record's own one."""
    found: list[str] = []
    for value in values:
        rest = value
        for muster, form in _CODES:
            for matches in muster.finditer(rest):
                code = form.format(matches.group(1))
                if code.upper().startswith(("NCT", "DRKS", "ISRCTN")):
                    code = code.upper()
                if code != own and code not in found:
                    found.append(code)
            rest = muster.sub(" ", rest)
    return " | ".join(found)


def _place(city: str) -> str:
    """Display form of a site's city: no postcode, no country suffix, no placeholder."""
    city = _text(city).replace("|", "/")
    city = _COUNTRY_SUFFIX.sub("", re.sub(r"^(?:[A-Z]-)?\d{4,5}\s+", "", city)).strip()
    return "" if _PLACEHOLDER.fullmatch(city.casefold()) else city


def _places(cities: list[str]) -> str:
    seen: dict[str, str] = {}
    for city in map(_place, cities):
        if city:
            seen.setdefault(city.casefold(), city)
    return " | ".join(list(seen.values())[:MAX_PLACES])


def _number(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _place_key(city: str) -> str:
    """Comparable form of a city name ("Halle (Saale)" -> "halle")."""
    city = re.split(r"[(/,]| - ", _place(city))[0]
    return re.sub(r"\s+", " ", city).strip().casefold()


# --------------------------------------------------------------------------- records


def ctgov_line(study: dict, title_max: int, without_title: bool = False) -> list | None:
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    nct = ident.get("nctId", "")
    places = [o for o in p.get("contactsLocationsModule", {}).get("locations", [])
              if o.get("country") in CTG_COUNTRIES]
    if not nct or not places:
        return None
    countries = {CTG_COUNTRIES[o["country"]] for o in places}
    extent = p.get("designModule", {}).get("enrollmentInfo", {})
    participants = _number(extent.get("count"))
    kind = {"ACTUAL": "tatsaechlich", "ESTIMATED": "geplant"}.get(extent.get("type", ""), "")
    secondary = [s.get("id", "") for s in ident.get("secondaryIdInfos", [])]
    secondary.append(ident.get("orgStudyIdInfo", {}).get("id", ""))
    short = _text(ident.get("briefTitle"))
    long = _text(ident.get("officialTitle"))
    return [
        "ClinicalTrials.gov", nct, _text(ident.get("acronym")),
        _shorten(short, title_max),
        "" if long == short or without_title else _shorten(long, title_max),
        "" if participants is None else participants, kind if participants is not None else "",
        len(places), " ".join(c for c in DACH if c in countries),
        _places([o.get("city", "") for o in places]),
        p.get("statusModule", {}).get("overallStatus", ""),
        p.get("statusModule", {}).get("startDateStruct", {}).get("date", ""),
        _codes(secondary, nct),
    ]


def drks_line(study: dict, title_max: int, dach_places: set[str],
              without_title: bool = False) -> list | None:
    code = study.get("drksId", "")
    r = study.get("recruitment") or {}
    countries = {c.get("idCountry", {}).get("code") for c in r.get("countries", [])}
    institute = r.get("institutes") or []
    if countries and countries <= set(DACH):
        places = institute
    else:  # mixed or missing countries: keep only sites in a known DACH place
        places = [i for i in institute if _place_key(i.get("city") or "") in dach_places]
    countries_dach = [c for c in DACH if c in countries]
    # Recruitment countries without DE/AT/CH rule a study out; a DACH place counts only when
    # no country is given at all (then Germany, the register's home, is assumed).
    if not code or (countries and not countries_dach) or not (countries_dach or places):
        return None
    if not countries_dach:
        countries_dach = ["DE"]
    texts = {d.get("idLocale", {}).get("locale"): d for d in study.get("trialDescriptions", [])}
    de, en = texts.get("de", {}), texts.get("en", {})
    acronym = _text(de.get("acronym")) or _text(en.get("acronym"))
    short, long = _text(de.get("title")), _text(en.get("title"))
    if not short:
        short, long = long, ""
    actual, planned = _number(r.get("totalParticipants")), _number(r.get("plannedParticipants"))
    if actual:
        participants, kind = actual, "tatsaechlich"
    elif planned is not None:
        participants, kind = planned, "geplant"
    else:
        participants, kind = actual, "tatsaechlich" if actual is not None else ""
    s = study.get("secondaryIds") or {}
    secondary = [str(s.get(k) or "") for k in
                 ("otherPrimaryRegisterId", "universalTrialNumber", "eudraCtNumber")]
    secondary += [str(o.get("value") or "") for o in study.get("otherSecondaryIds") or []]
    eudract = _text(s.get("eudraCtNumber"))
    return [
        "DRKS", code, acronym, _shorten(short, title_max),
        "" if long == short or without_title else _shorten(long, title_max),
        "" if participants is None else participants, kind,
        len(places), " ".join(countries_dach), _places([i.get("city", "") for i in places]),
        DRKS_STATUS.get(r.get("status") or "", r.get("status") or ""),
        r.get("actualStartDate") or r.get("scheduledStartDate") or "",
        _codes(secondary + ([f"EudraCT {eudract}"] if eudract else []), code),
    ]


def drks_open(study: dict, extent: str) -> bool:
    """True if the entry falls under CC BY 4.0 according to the DRKS terms of use."""
    if (study.get("registrationDrks") or "") >= "2025-01-01":
        return True
    return extent == "aktualisiert-ab-2025" and (study.get("lastUpdate") or "") >= "2025-01-01"


def municipality_names() -> set[str]:
    """German municipality names from the bundled Destatis file (for DRKS site matching)."""
    if not MUNICIPALITIES.exists():
        return set()
    with gzip.open(MUNICIPALITIES, "rt", encoding="utf-8") as f:
        lines = (z for z in f if not z.startswith("#"))
        return {_place_key(z["name"]) for z in csv.DictReader(lines)}


# --------------------------------------------------------------------------- main


def key_figures(lines: list[list]) -> str:
    """Short summary per register: count, share with acronym, quartiles of participants."""
    out = []
    for register in dict.fromkeys(z[0] for z in lines):
        part = [z for z in lines if z[0] == register]
        numbers = sorted(z[5] for z in part if isinstance(z[5], int))
        quartile = statistics.quantiles(numbers, n=4) if len(numbers) > 1 else [0, 0, 0]
        out.append(f"{register}: {len(part)} Studien, {sum(bool(z[2]) for z in part)} mit Akronym"
                   f" ({sum(bool(z[2]) for z in part) / len(part):.1%}), Teilnehmer Q1/Median/Q3"
                   f" {quartile[0]:.0f}/{quartile[1]:.0f}/{quartile[2]:.0f}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path,
                    default=Path(tempfile.gettempdir()) / "pseudonymed-studienregister")
    ap.add_argument("--drks-json", help="local DRKS export (zip or json) instead of downloading")
    ap.add_argument("--without-drks", action="store_true")
    ap.add_argument("--drks-scope", choices=["registriert-ab-2025", "aktualisiert-ab-2025"],
                    default="registriert-ab-2025")
    ap.add_argument("--title-max", type=int, default=0,
                    help="shorten titles to N characters (default 0: keep them in full)")
    ap.add_argument("--without-title", action="store_true",
                    help="leave the column titel empty (kurztitel stays)")
    ap.add_argument("--target", type=Path, default=TARGET)
    a = ap.parse_args(argv[1:])
    a.cache.mkdir(parents=True, exist_ok=True)

    ctg_date, studies = ctgov_pages(a.cache)
    lines, place_counts = [], Counter()
    for study in studies:
        line = ctgov_line(study, a.title_max, a.without_title)
        if line:
            lines.append(line)
            place_counts.update({_place_key(o) for o in line[9].split(" | ") if o})
    lines.sort(key=lambda z: z[1])
    dach_places = municipality_names() | {o for o, n in place_counts.items()
                                          if n >= MIN_STUDIES_PER_PLACE}

    source = (f"ClinicalTrials.gov (U.S. National Library of Medicine, a United States Government"
              f" database), API v2, Daten verarbeitet am {ctg_date}")
    licence = f"ClinicalTrials.gov Terms and Conditions ({CTG_TERMS})"
    if not a.without_drks:
        drks_date, drks = drks_export(a.cache, a.drks_json)
        free = [s for s in drks if drks_open(s, a.drks_scope)]
        new = [z for z in (drks_line(s, a.title_max, dach_places, a.without_title)
                           for s in free) if z]
        lines += sorted(new, key=lambda z: z[1])
        selection = ("ab 2025 registrierte Einträge" if a.drks_scope == "registriert-ab-2025"
                     else "ab 2025 registrierte oder aktualisierte Einträge")
        source += (f", und DRKS – Deutsches Register Klinischer Studien (BfArM), Gesamtexport"
                   f" abgerufen am {drks_date}, nur {selection}")
        licence += (f", DRKS-Einträge CC BY 4.0 ({CC_BY}) laut DRKS-Nutzungsbedingungen"
                    f" ({DRKS_TERMS})")
        print(f"  DRKS: {len(drks)} Einträge, davon {len(free)} unter CC BY 4.0,"
              f" {len(new)} mit DACH-Bezug", file=sys.stderr)
    title = ("Spalte titel leer" if a.without_title else
             f"Titel auf {a.title_max} Zeichen gekürzt" if a.title_max else "Titel ungekürzt")
    source += (". Auszug mit Änderungen: nur Studien mit Standort in DE/AT/CH, ausgewählte"
               f" Felder, {title}, höchstens {MAX_PLACES} Orte ohne Platzhalter, Status"
               " vereinheitlicht, nur Registernummern als weitere Kennungen"
               " (Einzelheiten in tools/study_registries.py)")

    a.target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(a.target, "wb", compresslevel=9, mtime=0) as raw, \
            io.TextIOWrapper(raw, encoding="utf-8", newline="") as f:
        f.write(f"# Quelle: {source}; Lizenz/Nutzungsbedingungen: {licence};"
                f" Stand: {dt.date.today().isoformat()}\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(lines)
    print(key_figures(lines))
    print(f"{len(lines)} Studien nach {a.target} geschrieben"
          f" ({a.target.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
