"""Build ``pseudonymed/daten/berufe_de.csv.gz``: German occupations and employment status.

Sources
-------
* **Occupations:** Statistik der Bundesagentur für Arbeit (BA), "Klassifikation der Berufe
  2010 – überarbeitete Fassung 2020, alphabetisches Verzeichnis der Berufsbenennungen"
  (``Alphabetisches-Verzeichnis-Berufsbenennungen.xlsx``, https://statistik.arbeitsagentur.de
  > Grundlagen > Klassifikationen > KldB 2010 > Systematik und Verzeichnisse).  Terms of use,
  quoted from the workbook's sheet "Impressum": "Daten und Tabellen dürfen uneingeschränkt
  verwendet werden. Informationen dürfen (auch auszugsweise) gespeichert und mit
  Quellenangabe weitergegeben, vervielfältigt und verbreitet werden. Die Inhalte dürfen
  nicht verändert oder verfälscht werden. Eigene Berechnungen sind erlaubt, jedoch als
  solche kenntlich zu machen."  Internet publications should link to the BA statistics
  homepage.  Everything this script derives (gender forms, shortened terms, the choice of
  one code per term) is such an own processing step and is marked so in the header line.
* **Everyday occupation words** missing from the directory ("Polizist", "Lkw-Fahrer",
  "MFA"): curated list ``EVERYDAY_OCCUPATIONS`` with the KldB code of the matching official term
  (own assignment) or a Wikidata QID.
* **Employment status:** curated list in this script; Wikidata QIDs (CC0 1.0) where an item
  with a matching German label and description was checked by hand on 2026-11-02.
* **Surname flag:** Bundesamt für Statistik (BFS), STATPOP, "Nachnamen der ständigen
  Wohnbevölkerung nach Sprachregion" (opendata.swiss, terms "Freie Nutzung. Quellenangabe ist
  Pflicht."; only the counts for German-speaking Switzerland are used), and Wikidata (CC0
  1.0): family-name items (P31 Q101352) labelled exactly like the term, and the number of
  people with that family name (P734) and a German, Austrian or Swiss citizenship (P27).

Method
------
Each "Berufsbenennung" is cut to its core: parenthesised additions, everything after a
spaced dash (" - Fachrichtung"), appended "/Bachelor Professional …" or "/Master
Professional …" qualifications, prepositional tails ("für …", "im …") and trailing
qualifiers after the head noun ("Werkstoffprüfer/in Metall") are dropped.  Alternatives
separated by " / ", commas, "und" or "oder" become separate terms; truncated compounds
("Brau- und Getränketechnologe", "Kiosk-/Imbissverkäufer/in", "Prüfer/in und -berater/in")
are completed with the head noun of the full word, found as the longest suffix that is
itself a term of the directory.  Gender forms are resolved: "Lehrer/in" → Lehrer, Lehrerin;
"Fachangestellte/r" → Fachangestellter, Fachangestellte; "Kaufmann/-frau" → Kaufmann,
Kauffrau; "Gemüsebauer/-bäuerin" → Gemüsebauer, Gemüsebäuerin; "Arzt/Ärztin" and
"Kinderkrankenschwester/-pfleger" (female form first) are oriented by their endings.
``grundform`` is the masculine form (the lemma of the pair); gender-neutral terms
("Fachkraft") are their own base form.  A term that occurs in several Benennungen gets the
KldB code of the Benennung that consists of the bare term alone if there is one, otherwise
the code that occurs most often (ties: lowest code).  A handful of typing errors and
irregular notations in the source are corrected first (``CORRECTIONS``).  Terms of the
curated employment-status list take precedence over occupations of the same spelling.

``surname`` is 1 when the exact term is a frequent family name: at least
``BFS_THRESHOLD`` bearers in German-speaking Switzerland (BFS) or at least
``WIKIDATA_THRESHOLD`` people with that family name and a DACH citizenship in Wikidata.

Usage::

    .venv/bin/python -I tools/occupations_de.py [--kldb FILE.xlsx] [--bfs FILE.csv]
        [--without-wikidata] [--log surnames.csv]
        [--wikidata-cache wikidata.json] [target.csv.gz]

Without ``--kldb``/``--bfs`` the files are downloaded into memory.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

URL_KLDB = ("https://statistik.arbeitsagentur.de/DE/Statischer-Content/Grundlagen/"
            "Klassifikationen/Klassifikation-der-Berufe/KldB2010-Fassung2020/"
            "Systematik-Verzeichnisse/Generische-Publikationen/"
            "Alphabetisches-Verzeichnis-Berufsbenennungen.xlsx?__blob=publicationFile&v=1")
URL_BFS = "https://dam-api.bfs.admin.ch/hub/api/dam/assets/36752522/master"
ENDPOINT = "https://query.wikidata.org/sparql"
AGENT = ("PseudonyMed/0.1 (open-source medical de-identification;"
         " https://github.com/fre-ms/PseudonyMed)")
TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "berufe_de.csv.gz"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

BFS_THRESHOLD = 100       # bearers in German-speaking Switzerland (population about 6 million)
WIKIDATA_THRESHOLD = 10   # people in Wikidata with that family name and a DACH citizenship
# Germany, Austria, Switzerland and their predecessor states since 1806.
DACH_STATES = ("Q183", "Q40", "Q39", "Q713750", "Q16957", "Q43287", "Q41304", "Q7318",
               "Q27306", "Q154195", "Q28513", "Q131964", "Q12548", "Q151624", "Q518101")

# Typing errors and irregular notations of the source, corrected before parsing
# (substring, replacement).  Own corrections, listed in the research notes.
CORRECTIONS: tuple[tuple[str, str], ...] = (
    ("/i n(", "/in ("),
    ("ingenieu/inr", "ingenieur/in"),
    ("Fellverarbeiterhelfer/n", "Fellverarbeiterhelfer/in"),
    ("Facharbeiter/In", "Facharbeiter/in"),
    ("Medizinischer/r", "Medizinische/r"),
    ("Techniche/r", "Technische/r"),
    ("Vormann-/frau", "Vormann/-frau"),
    ("Evangelisch Theologe", "Evangelische/r Theologe"),
    ("Dritte Technische/r", "Dritte/r Technische/r"),
    ("Katholischer Theologe/Katholische Theologin", "Katholische/r Theologe/Theologin"),
    ("Leitender Unterrichtspfleger/Leitende Unterrichtsschwester",
     "Leitende/r Unterrichtspfleger/Unterrichtsschwester"),
    ("Hausdame/Housekeeper", "Hausdame / Housekeeper"),
    ("Virtual/Augmented Reality-Entwickler/in",
     "Virtual-Reality-Entwickler/in / Augmented-Reality-Entwickler/in"),
    ("Application-Engineer/-Manager/in", "Application-Engineer / Application-Manager/in"),
    ("E-Commerce-Manager/-Assistent/in", "E-Commerce-Manager/in / E-Commerce-Assistent/in"),
    ("Automatenbefüller/-kassierer/in", "Automatenbefüller/in / Automatenkassierer/in"),
    ("Marketingfachkraft/-assistent/in", "Marketingfachkraft / Marketingassistent/in"),
    ("Sammlungs- und ähnliche/r Aufseher/in", "Sammlungsaufseher/in"),
    ("Erstgesell/e/in", "Erstgeselle/Erstgesellin"),
    ("ADAS/AD Engineer", "ADAS Engineer / AD Engineer"),
    ("Aufsicht -Messe", "Aufsicht - Messe"),
    ("Bahnbeamter/Bankbeamtin", "Bahnbeamter/Bahnbeamtin"),
    ("(Uni)- ", "(Uni) - "),
    ("Branntsteinbrenner, -trockner, -glasierer/in",
     "Branntsteinbrenner/in, Branntsteintrockner/in, Branntsteinglasierer/in"),
    ("Keramgravierer, -ätzer/in", "Keramgravierer/in, Keramätzer/in"),
    ("Konservierungsguttrockner, -erhitzer/in",
     "Konservierungsguttrockner/in, Konservierungsguterhitzer/in"),
    ("CNC-Fachkraft/NC-Anwendungsfachmann/-frau", "CNC-Fachkraft / NC-Anwendungsfachmann/-frau"),
    ("EDV/PC-Servicetechniker/in", "EDV-Servicetechniker/in / PC-Servicetechniker/in"),
    ("Fachverkäufer/-berater/in", "Fachverkäufer/in / Fachberater/in"),
    ("Foto- und medientechnische/r Assistent/in",
     "Fototechnische/r Assistent/in / Medientechnische/r Assistent/in"),
    ("Informations- und kommunikationstechnische/r Assistent/in",
     "Informationstechnische/r Assistent/in / Kommunikationstechnische/r Assistent/in"),
    ("Magister/Magistra public health", "Magister/Magistra"),
    ("Magister/Magistra sanitatis publicae", "Magister/Magistra"),
    ("Polsterer und Dekorateur/Polsterin und Dekorateurin", "Polsterer/in / Dekorateur/in"),
    ("Relocation-Specialist/-Manager/in", "Relocation-Specialist / Relocation-Manager/in"),
    ("Stallbursche/-mädchen", "Stallbursche/Stallmädchen"),
    ("Commercialmanger", "Commercialmanager"),
    ("Straßenbauer/in -Pflasterer/Pflasterin-", "Straßenbauer/in / Pflasterer/Pflasterin"),
    ("Psychologischer Berater/in", "Psychologische/r Berater/in"),
    ("Textiltechnischer Assistent/in", "Textiltechnische/r Assistent/in"),
    ("Zimmermädchen/Roomboy", "Zimmermädchen / Roomboy"),
)

# Everyday occupation words missing from the directory, which uses official names:
# (notation, reference).  The reference is "=Term" to reuse the KldB code of an official
# term of the directory (own assignment), a Wikidata QID checked by hand, or "".
# Abbreviations get the referenced term as base form.  Left out on purpose because they
# clash with frequent medical abbreviations: PTA (angioplasty), CTA (CT angiography), ATA,
# MTA, PKA (protein kinase A), TFA.
EVERYDAY_OCCUPATIONS: tuple[tuple[str, str], ...] = (
    ("Polizist/in", "Q384593"), ("Polizeibeamter/-beamtin", "=Polizeivollzugsbeamter"),
    ("Unternehmer/in", "Q131524"), ("Programmierer/in", "Q5482740"),
    ("Lkw-Fahrer/in", "=Berufskraftfahrer"), ("LKW-Fahrer/in", "=Berufskraftfahrer"),
    ("Lastwagenfahrer/in", "=Berufskraftfahrer"), ("Kraftfahrer/in", "=Berufskraftfahrer"),
    ("Bauarbeiter/in", "=Bauhilfsarbeiter"), ("Fabrikarbeiter/in", ""),
    ("Industriearbeiter/in", ""), ("Handwerker/in", ""), ("Geschäftsmann/-frau", ""),
    ("Babysitter/in", "=Kinderbetreuer"), ("Wissenschaftler/in", "Q901"),
    ("Forscher/in", ""), ("Politiker/in", "Q82955"), ("Anwalt/Anwältin", "=Rechtsanwalt"),
    ("Seemann", "Q45199"), ("Kurier/in", "=Kurierfahrer"),
    ("Paketzusteller/in", "=Paketbote"), ("Künstler/in", "Q483501"),
    ("Systemadministrator/in", "=Netzwerkadministrator"),
    ("Versicherungsvertreter/in", ""), ("Referendar/in", ""),
    ("Rechtsreferendar/in", ""), ("Studienreferendar/in", ""),
    ("Pflegekraft", "=Pflegefachkraft"), ("Sportler/in", "Q2066131"),
    ("Profisportler/in", ""), ("Leistungssportler/in", ""), ("Händler/in", ""),
    ("Einzelhändler/in", "=Einzelhandelskaufmann"), ("Wirt/in", "=Gastwirt"),
    ("Hotelier", ""), ("Bankberater/in", "=Bankkaufmann"),
    ("Justizbeamter/-beamtin", ""), ("Berufssoldat/in", "=Soldat"),
    ("Zeitsoldat/in", "=Soldat"),
    ("MFA", "=Medizinischer Fachangestellter"), ("ZFA", "=Zahnmedizinischer Fachangestellter"),
    ("MTRA", "=Medizinisch-technischer Radiologieassistent"),
    ("MTLA", "=Medizinisch-technischer Laboratoriumsassistent"),
)

# Employment status: (notation, QID of the masculine/neutral form, QID of the feminine form
# or None for the same).  Notation as in the KldB ("Rentner/in", "Arbeitslose/r").
EMPLOYMENT_STATUS: tuple[tuple[str, str, str | None], ...] = (
    # pension and retirement
    ("Rentner/in", "Q1749879", None), ("Altersrentner/in", "", None),
    ("Frührentner/in", "", None), ("Erwerbsminderungsrentner/in", "", None),
    ("EU-Rentner/in", "", None), ("EM-Rentner/in", "", None), ("BU-Rentner/in", "", None),
    ("Teilrentner/in", "", None), ("Pensionär/in", "", None), ("Pensionist/in", "", None),
    ("Ruheständler/in", "", None), ("Vorruheständler/in", "", None),
    ("Privatier/Privatière", "", None), ("AHV-Rentner/in", "", None),
    ("IV-Rentner/in", "", None), ("IV-Bezüger/in", "", None),
    ("berentet", "", None), ("verrentet", "", None), ("frühberentet", "", None),
    ("pensioniert", "", None), ("frühpensioniert", "", None), ("emeritiert", "", None),
    ("im Ruhestand", "", None), ("in Rente", "", None), ("in Pension", "", None),
    ("in Frührente", "", None), ("im Vorruhestand", "", None),
    ("Ruhestand", "Q64825398", None), ("Vorruhestand", "", None),
    ("Pensionierung", "Q946865", None), ("Frühpensionierung", "", None),
    ("Rente", "Q268617", None), ("Altersrente", "Q438965", None), ("Frührente", "", None),
    ("Erwerbsminderungsrente", "", None), ("EM-Rente", "", None), ("EU-Rente", "", None),
    ("BU-Rente", "", None), ("Berufsunfähigkeitsrente", "", None),
    ("Erwerbsunfähigkeitsrente", "", None), ("Teilrente", "", None),
    ("Witwenrente", "", None), ("Witwerrente", "", None), ("Waisenrente", "", None),
    ("Pension", "Q156223", None), ("Frühpension", "", None),
    ("Invaliditätspension", "", None), ("Berufsunfähigkeitspension", "", None),
    ("Rehabilitationsgeld", "", None), ("AHV-Rente", "", None), ("IV-Rente", "", None),
    ("Rentenantrag", "", None), ("Altersteilzeit", "Q438986", None),
    ("in Altersteilzeit", "", None),
    # unemployment and benefits
    ("Arbeitslose/r", "Q28790169", None), ("Langzeitarbeitslose/r", "", None),
    ("Erwerbslose/r", "", None), ("Arbeitssuchende/r", "", None),
    ("Arbeitsuchende/r", "", None), ("arbeitslos", "", None),
    ("langzeitarbeitslos", "", None), ("erwerbslos", "", None),
    ("arbeitssuchend", "", None), ("arbeitsuchend", "", None),
    ("beschäftigungslos", "", None), ("ohne Arbeit", "", None),
    ("ohne Beschäftigung", "", None), ("ohne Job", "", None), ("ausgesteuert", "", None),
    ("Arbeitslosigkeit", "Q41171", None), ("Langzeitarbeitslosigkeit", "", None),
    ("Erwerbslosigkeit", "Q1362733", None), ("Arbeitslosengeld", "Q628538", None),
    ("Arbeitslosengeld I", "", None), ("Arbeitslosengeld II", "Q628569", None),
    ("ALG I", "", None), ("ALG 1", "", None), ("ALG II", "", None), ("ALG 2", "", None),
    ("Hartz IV", "", None), ("Hartz 4", "", None), ("Hartz-IV-Empfänger/in", "", None),
    ("Bürgergeld", "Q113957846", None), ("Bürgergeldempfänger/in", "", None),
    ("Bürgergeldbezieher/in", "", None), ("Grundsicherungsgeld", "Q140406332", None),
    ("Grundsicherung", "Q1550701", None), ("Grundsicherung im Alter", "", None),
    ("Sozialhilfe", "Q12322674", None), ("Sozialhilfeempfänger/in", "", None),
    ("Aufstocker/in", "Q760069", None), ("Ein-Euro-Job", "", None),
    ("Notstandshilfe", "Q109923975", None), ("Mindestsicherung", "", None),
    ("Kurzarbeit", "Q316025", None), ("in Kurzarbeit", "", None),
    ("Kurzarbeiter/in", "", None), ("Kurzarbeitergeld", "", None),
    ("Übergangsgeld", "Q331883", None), ("Krankengeld", "Q1786518", None),
    # incapacity for work, rehabilitation
    ("erwerbsunfähig", "", None), ("berufsunfähig", "", None),
    ("erwerbsgemindert", "", None), ("voll erwerbsgemindert", "", None),
    ("teilweise erwerbsgemindert", "", None), ("dienstunfähig", "", None),
    ("arbeitsunfähig", "", None), ("krankgeschrieben", "", None),
    ("im Krankenstand", "", None), ("Erwerbsunfähige/r", "", None),
    ("Berufsunfähige/r", "", None), ("Erwerbsunfähigkeit", "", None),
    ("Berufsunfähigkeit", "Q829071", None), ("Erwerbsminderung", "", None),
    ("Dienstunfähigkeit", "Q1221130", None), ("Arbeitsunfähigkeit", "Q19834774", None),
    ("Wiedereingliederung", "", None), ("stufenweise Wiedereingliederung", "", None),
    ("Hamburger Modell", "", None), ("Umschulung", "Q2478667", None),
    ("in Umschulung", "", None), ("Umschüler/in", "", None), ("Rehabilitand/in", "", None),
    ("Werkstatt für behinderte Menschen", "Q2295494", None), ("WfbM", "", None),
    ("Werkstattbeschäftigte/r", "", None), ("beschützende Werkstatt", "", None),
    # school, training, studies, voluntary and civilian service
    ("Schüler/in", "Q48942", None), ("Grundschüler/in", "", None),
    ("Hauptschüler/in", "", None), ("Realschüler/in", "", None),
    ("Gymnasiast/in", "Q112918691", None), ("Gesamtschüler/in", "", None),
    ("Förderschüler/in", "", None), ("Berufsschüler/in", "", None),
    ("Fachschüler/in", "", None), ("Abiturient/in", "", None),
    ("Student/in", "Q48282", None), ("Studierende/r", "", None),
    ("Werkstudent/in", "Q23685787", None), ("Doktorand/in", "", None),
    ("Auszubildende/r", "Q253567", None), ("Azubi", "", None), ("Lehrling", "", None),
    ("Praktikant/in", "Q315625", None), ("Trainee", "Q126559148", None),
    ("Schulabbrecher/in", "", None), ("Studienabbrecher/in", "", None),
    ("in Ausbildung", "", None), ("im Studium", "", None),
    ("ohne Berufsausbildung", "", None), ("ohne Schulabschluss", "", None),
    ("ungelernt", "", None), ("angelernt", "", None),
    ("Bundesfreiwilligendienst", "Q1006004", None), ("Bufdi", "", None),
    ("Freiwilliges Soziales Jahr", "Q1430892", None), ("FSJ", "", None),
    ("Freiwilliges Ökologisches Jahr", "Q1454945", None), ("FÖJ", "", None),
    ("Freiwilligendienst", "Q1454904", None), ("Zivildienst", "Q702635", None),
    ("Zivildienstleistende/r", "", None), ("Zivildiener", "", None),
    ("Präsenzdienst", "Q1802953", None), ("Präsenzdiener", "", None),
    ("Wehrdienst", "Q209572", None), ("Grundwehrdienst", "", None),
    ("Grundwehrdienstleistende/r", "", None),
    # household and family
    ("Hausmann/-frau", "Q2154210", "Q1934684"), ("Elternzeit", "Q772989", None),
    ("in Elternzeit", "", None), ("Erziehungsurlaub", "Q3122539", None),
    ("Erziehungszeit", "", None), ("Mutterschutz", "Q1956054", None),
    ("im Mutterschutz", "", None), ("Elterngeld", "Q1419924", None),
    ("in Karenz", "", None), ("Bildungskarenz", "", None),
    ("Kinderbetreuungsgeld", "Q1662971", None), ("Pflegezeit", "", None),
    ("Familienpflegezeit", "", None),
    # form of employment
    ("erwerbstätig", "", None), ("berufstätig", "", None),
    ("nicht erwerbstätig", "", None), ("nicht berufstätig", "", None),
    ("vollzeitbeschäftigt", "", None), ("teilzeitbeschäftigt", "", None),
    ("in Vollzeit", "", None), ("in Teilzeit", "", None), ("Vollzeit", "", None),
    ("Teilzeit", "", None), ("Teilzeitarbeit", "Q587972", None),
    ("Teilzeitkraft", "", None), ("Minijob", "", None), ("Minijobber/in", "", None),
    ("Midijob", "Q1390072", None), ("geringfügig beschäftigt", "", None),
    ("geringfügige Beschäftigung", "Q1297203", None), ("450-Euro-Job", "", None),
    ("520-Euro-Job", "", None), ("Aushilfe", "", None), ("Aushilfskraft", "", None),
    ("Leiharbeiter/in", "", None), ("Zeitarbeiter/in", "", None),
    ("Leiharbeit", "", None), ("Zeitarbeit", "", None), ("Saisonarbeiter/in", "", None),
    ("Saisonarbeit", "Q1626021", None), ("Saisonkraft", "", None),
    ("Tagelöhner/in", "Q897359", None), ("Gelegenheitsarbeiter/in", "", None),
    ("Gelegenheitsjob", "", None),
    ("selbstständig", "", None), ("selbständig", "", None),
    ("Selbstständige/r", "", None), ("Selbständige/r", "", None),
    ("Selbstständigkeit", "", None), ("Selbständigkeit", "Q1005490", None),
    ("freiberuflich", "", None), ("Freiberufler/in", "Q19365375", None),
    ("freischaffend", "", None), ("Freelancer/in", "", None),
    ("Gewerbetreibende/r", "", None), ("Existenzgründer/in", "", None),
    ("Ich-AG", "", None), ("verbeamtet", "", None),
    ("Beamter/Beamtin", "Q212238", None), ("Angestellte/r", "Q255274", None),
    ("Arbeiter/in", "Q12713481", None), ("Arbeitnehmer/in", "Q703534", None),
)

PREPOSITIONS = frozenset({
    "für", "im", "in", "an", "am", "bei", "beim", "von", "vom", "mit", "auf", "aus", "zur",
    "zum", "der", "des", "einer", "eines", "nach", "ohne", "über", "unter", "als", "gemäß",
})
# Suffix pairs (masculine, feminine) whose forms share no common beginning.
PAIRS: tuple[tuple[str, str], ...] = (
    ("mann", "frau"), ("man", "woman"), ("vater", "mutter"), ("pfleger", "schwester"),
    ("entbindungspfleger", "hebamme"), ("modelleur", "direktrice"), ("steward", "hostess"),
)
FEMALE_ENDINGS = ("in", "frau", "mutter", "schwester", "hebamme", "dame", "ess", "esse",
                  "ette", "ice", "iere", "isse", "woman", "magd", "madchen")
_FOLDING = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "é": "e", "è": "e", "ß": "s"})
_MARKER = re.compile(r"/(?:-|[a-zäöüß])")
_VALID = re.compile(r"^[0-9A-ZÄÖÜ][^/()]*[^-]$")
# Academic degrees listed as Benennungen ("Bachelor of Science - Physik"): left to titel_de.py.
_DEGREES = re.compile(r"^(?:Bachelor|Master|European Master|Magister|Magistra)\b")


def _fold(word: str) -> str:
    """Lower-case and strip umlauts and accents for comparing word beginnings."""
    return word.lower().translate(_FOLDING)


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(_fold(a), _fold(b), strict=False):
        if x != y:
            break
        n += 1
    return n


def is_female(word: str) -> bool:
    """Whether a German person noun looks feminine (used only to orient pairs)."""
    return _fold(word.split()[-1] if word.split() else word).endswith(FEMALE_ENDINGS)


def _replace_ending(base: str, replacement: str) -> str | None:
    """Apply a hyphen replacement such as ``Kaufmann/-frau`` or ``Bauer/-bäuerin``."""
    if replacement[:1].isupper() and "-" in base:
        return base.rsplit("-", 1)[0] + "-" + replacement
    for m, f in PAIRS:
        for begin, last in ((m, f), (f, m)):
            if _fold(replacement) == last and _fold(base).endswith(begin):
                rest = base[:len(base) - len(begin)]
                return rest + (replacement if rest else replacement.capitalize())
    best, position = 0, -1
    for i in range(len(base)):
        n = _common_prefix(base[i:], replacement)
        if n >= best and n > 0:
            best, position = n, i
    if best < 2:
        return None
    rest = base[:position]
    return rest + (replacement if rest else replacement.capitalize())


def _word_forms(word: str) -> tuple[str, str] | None:
    """(masculine, feminine) of one word carrying a gender marker, e.g. ``Lehrer/in``."""
    base, _, rest = word.partition("/")
    if rest in ("in", "-in", "innen", "n"):
        female = (base[:-1] if base.endswith("e") and not base.endswith("ee")
                  else base) + "in"
        return base, female
    if rest == "r":
        return base + "r", base
    if rest == "e":
        return base, base + "e"
    replacement = rest.lstrip("-")
    if not replacement or "/" in replacement:
        return None
    other = _replace_ending(base, replacement)
    if other is None:
        return None
    if is_female(base) and not is_female(other):
        return other, base
    return base, other


def _resolve_markers(text: str, sex: str) -> str | None:
    """Resolve all word-level markers of a phrase for one gender (``m`` or ``f``)."""
    words = []
    for word in text.split():
        if _MARKER.search(word):
            forms = _word_forms(word)
            if forms is None:
                return None
            word = forms[0] if sex == "m" else forms[1]
        words.append(word)
    return " ".join(words)


def _similar(a: str, b: str) -> bool:
    if _common_prefix(a, b) >= 3:
        return True
    fa, fb = _fold(a), _fold(b)
    for m, f in PAIRS:
        for x, y in ((m, f), (f, m)):
            if fa.endswith(x) and fb.endswith(y) and fa[:len(fa) - len(x)] == fb[:len(fb) - len(y)]:
                return True
    return False


def _counterpart(a: str, b: str) -> str | None:
    """The full counterpart phrase if ``b`` is the other gender form of ``a``, else None."""
    words_a, words_b = a.split(), b.split()
    head_a = words_a[-1]
    part_a = head_a.rsplit("-", 1)[-1]
    if len(words_b) >= len(words_a):
        # full form, possibly followed by a qualifier ("Metallurge/Metallurgin Erzeugung")
        candidate = words_b[:len(words_a)]
        if _similar(head_a, candidate[-1]):
            return " ".join(candidate)
        if "-" in head_a and "-" not in candidate[-1] and _similar(part_a, candidate[-1]):
            candidate[-1] = head_a.rsplit("-", 1)[0] + "-" + candidate[-1]   # "EDV-Pädagogin"
            return " ".join(candidate)
        return None
    if not (_similar(head_a, b) or _similar(part_a, b)):
        return None
    new = b
    if "-" in head_a and "-" not in b and not _similar(head_a, b):
        new = head_a.rsplit("-", 1)[0] + "-" + b
    return " ".join(a.split()[:-1] + [new])


def _pair(a: str, b: str | None) -> tuple[str, str] | None:
    """(masculine, feminine) of one alternative, ``b`` being its explicit counterpart."""
    if b is None:
        m, f = _resolve_markers(a, "m"), _resolve_markers(a, "f")
    elif is_female(a) and not is_female(b):
        m, f = _resolve_markers(b, "m"), _resolve_markers(a, "f")
    else:
        m, f = _resolve_markers(a, "m"), _resolve_markers(b, "f")
    if m is None or f is None:
        return None
    return m, f


def _alternatives(part: str) -> list[tuple[str, str]]:
    """Split one part at ``/Uppercase`` into gender counterparts and further alternatives."""
    pieces = [s.strip() for s in re.split(r"/(?=[0-9A-ZÄÖÜ])", part) if s.strip()]
    if not pieces:
        return []
    result: list[tuple[str, str]] = []
    current, partner = pieces[0], None
    for s in pieces[1:]:
        # only a marker on the head noun completes the pair ("Leitende/r Arzt/Ärztin")
        if partner is None and not _MARKER.search(current.split()[-1]):
            counterpart = _counterpart(current, s)
            if counterpart is not None:
                partner = counterpart
                continue
            current = current + "/" + s        # e.g. "ADAS/AD Engineer": keep literally
            continue
        forms = _pair(current, partner)
        if forms:
            result.append(forms)
        current, partner = s, None
    forms = _pair(current, partner)
    if forms:
        result.append(forms)
    return result


def _is_adjective(token: str, following: str | None) -> bool:
    base = token.split("/")[0]
    return (following is not None and following[:1].isupper()
            and base.endswith(("e", "er", "es", "en")))


def _cut_after_head(part: str) -> str:
    """Drop qualifiers after the first marked head noun ("Werkstoffprüfer/in Metall")."""
    tokens = part.split()
    for k, token in enumerate(tokens):
        following = tokens[k + 1] if k + 1 < len(tokens) else None
        if _MARKER.search(token) and not (token.endswith("/r")
                                           and _is_adjective(token, following)):
            return " ".join(tokens[:k + 1])
    return part


def _head(word: str, lexicon: set[str]) -> int | None:
    """Start of the head noun of a compound: the longest proper suffix in the lexicon."""
    base = word.split("/")[0]
    if "-" in base:
        return base.rindex("-") + 1
    for i in range(3, len(base) - 3):
        if base[i:].lower() in lexicon:
            return i
    return None


def _parts(text: str, lexicon: set[str]) -> list[str]:
    """Split at " / ", commas, "und", "oder" and complete truncated compounds."""
    text = re.sub(r"(\w)-/(?=\w)", r"\1- und ", text)
    parts = [t.strip() for t in re.split(r"\s*,\s*|\s+und\s+|\s+oder\s+|\s+/\s+", text)]
    parts = [t for t in parts if t]
    done: list[str] = []
    for i, t in enumerate(parts):
        if t.endswith("-"):
            j = next((j for j in range(i + 1, len(parts)) if not parts[j].endswith("-")), None)
            if j is None:
                continue
            first = parts[j].split()[0]
            k = _head(first, lexicon)
            if k is None:
                continue
            *front, stub = t.split()
            header = first[k:]
            done.append(" ".join([*front, stub + header if "-" in first.split("/")[0]
                                  else stub[:-1] + header]))
            if front and len(parts[j].split()) == 1:
                # "Analytische/r Kinder- und Jugendlichenpsychotherapeut/in"
                parts[j] = " ".join([*front, parts[j]])
        elif t.startswith("-") and done:
            before = done[-1].split()[-1]
            k = _head(before, lexicon)
            if k is None:
                continue
            done.append(before.split("/")[0][:k] + t[1:])
        else:
            done.append(t)
    return done


def core(designation: str) -> str:
    """The part of a Benennung before qualifiers (still with gender markers)."""
    s = designation
    for old, new in CORRECTIONS:
        s = s.replace(old, new)
    while re.search(r"\([^()]*\)", s):          # innermost first: "(… lehrer(innen) …)"
        s = re.sub(r"\s*\([^()]*\)", "", s)
    s = s.replace("(", "").replace(")", "")
    s = re.sub(r"/in-(?=[A-ZÄÖÜ])", "/in - ", s)   # "Maschinenformer/in-Gießerei"
    s = re.split(r"\s+[-–]\s+|\s+[-–]\s*$", s)[0]
    s = re.sub(r"\s*/\s*(?:Bachelor|Master) Professional.*$", "", s)
    s = re.sub(r"(?<=\S) /(?=\S)|(?<=\S)/ (?=\S)", "/", s)
    tokens = s.split()
    for j, token in enumerate(tokens[1:], start=1):
        if token in PREPOSITIONS:
            tokens = tokens[:j]
            break
    return " ".join(tokens)


def decompose(designation: str, lexicon: set[str]) -> list[tuple[str, str]]:
    """All (masculine, feminine) terms of one Benennung of the directory."""
    s = core(designation)
    if not s:
        return []
    parts = [_cut_after_head(t) for t in _parts(s, lexicon)]
    with_markers = any("/" in t for t in parts)
    result = []
    for part in parts:
        if with_markers and "/" not in part:
            continue                      # "…/in, Einweisung": a qualifier, not a term
        for m, f in _alternatives(part):
            result.append((" ".join(m.split()), " ".join(f.split())))
    return result


def read_kldb(xlsx: bytes) -> tuple[str, list[tuple[str, str]]]:
    """(creation date, [(Benennung, 5-digit code)]) from the BA workbook."""
    z = zipfile.ZipFile(io.BytesIO(xlsx))
    ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
    strings = ["".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
               for si in ss.findall("m:si", NS)]
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name", "") for s in wb.find("m:sheets", NS)]
    sheet = next(i for i, n in enumerate(names, start=1) if "Verz" in n)
    data = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml")).find("m:sheetData", NS)
    pairs, state = [], ""
    for line in data:
        values: dict[str, str] = {}
        for c in line.findall("m:c", NS):
            v = c.findtext("m:v", default="", namespaces=NS)
            if c.get("t") == "s" and v:
                v = strings[int(v)]
            values["".join(ch for ch in c.get("r", "") if ch.isalpha())] = v.strip()
        a, b = values.get("A", ""), values.get("B", "")
        if re.fullmatch(r"\d{5}", b) and a:
            pairs.append((a, b))
        elif a.startswith("Erstellungsdatum"):
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", a)
            state = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else a
    return state, pairs


def occupations(pairs: list[tuple[str, str]]) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """{term: (KldB code, base form)} and the Benennungen that could not be resolved."""
    lexicon = {"kraft"}
    for a, _ in pairs:
        first = core(a).split(" ")[-1].split("/")[0] if core(a) else ""
        if first and "-" not in first and len(first) >= 4:
            lexicon.add(first.lower())
    votes: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    base_forms: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter)
    failures = []
    for a, code in pairs:
        forms = decompose(a, lexicon)
        good = [(m, f) for m, f in forms if _VALID.match(m) and _VALID.match(f)
                and not _DEGREES.match(m)]
        if not good:
            if not any(_DEGREES.match(m) for m, _ in forms):
                failures.append(a)
            continue
        bare = "(" not in a and " - " not in a and len(good) == 1
        for m, f in good:
            for term in {m, f}:
                votes[term][code] += 1000 if bare else 1
                base_forms[term][m] += 1000 if bare else 1
    result = {}
    for term, counter in votes.items():
        code = min(counter, key=lambda c: (-counter[c], c))
        base = min(base_forms[term], key=lambda g: (-base_forms[term][g], g))
        result[term] = (code, base)
    for notation, reference in EVERYDAY_OCCUPATIONS:
        if reference.startswith("="):
            if reference[1:] not in result:
                print(f"  Verweis fehlt: {reference[1:]}", file=sys.stderr)
            kldb_code = result.get(reference[1:], ("", ""))[0]
        else:
            kldb_code = reference
        for m, f in _alternatives(notation):
            base = reference[1:] if m.isupper() and reference.startswith("=") else m
            for term in (m, f):
                result.setdefault(term, (kldb_code, base))
    return result, failures


def employment_status() -> dict[str, tuple[str, str]]:
    """{term: (QID or "", base form)} of the curated employment-status list."""
    result = {}
    for notation, qid_m, qid_f in EMPLOYMENT_STATUS:
        for m, f in _alternatives(notation):
            result.setdefault(m, (qid_m, m))
            result.setdefault(f, (qid_f if qid_f is not None else qid_m, m))
    return result


def _load(url: str, path: str | None) -> bytes:
    if path:
        return Path(path).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=300) as response:
        return response.read()


def bfs_frequencies(raw: bytes) -> tuple[str, dict[str, int]]:
    """(year, {surname: bearers in German-speaking Switzerland}) from the BFS file."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    numbers, year = {}, ""
    for z in reader:
        if z.get("Sprachregion") == "1" and z.get("VALUE", "").isdigit():
            numbers[z["LASTNAME"]] = int(z["VALUE"])
            year = z.get("TIME_PERIOD", year)
    return year, numbers


def query(sparql: str, attempts: int = 5) -> list[dict[str, str]]:
    """Run a SPARQL query (POST, CSV) with retries and growing pauses."""
    data = urllib.parse.urlencode({"query": sparql}).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(ENDPOINT, data=data, headers={
            "Accept": "text/csv", "User-Agent": AGENT,
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                text = response.read().decode("utf-8")
            return list(csv.DictReader(io.StringIO(text)))
        except Exception as e:  # noqa: BLE001 - retry on timeouts and 429
            wait_time = 15 * (attempt + 1)
            print(f"  Versuch {attempt + 1} fehlgeschlagen ({e}); warte {wait_time}s",
                  file=sys.stderr)
            time.sleep(wait_time)
    raise RuntimeError("Wikidata-Abfrage wiederholt fehlgeschlagen")


def _literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wikidata_surnames(terms: list[str]) -> dict[str, int]:
    """{term: number of DACH citizens in Wikidata with that family name}, for name terms."""
    items: dict[str, set[str]] = collections.defaultdict(set)
    for i in range(0, len(terms), 200):
        stack = terms[i:i + 200]
        values = " ".join(f"{_literal(b)}@de {_literal(b)}@mul" for b in stack)
        sparql = (f"SELECT ?l ?item WHERE {{ VALUES ?l {{ {values} }} ?item rdfs:label ?l . "
                  "?item wdt:P31 wd:Q101352 . }")
        for z in query(sparql):
            items[z["l"]].add(z["item"].rsplit("/", 1)[-1])
        print(f"  Familiennamen gesucht: {min(i + 200, len(terms))}/{len(terms)},"
              f" gefunden {len(items)}", file=sys.stderr)
        time.sleep(1)
    all_items = sorted({q for qs in items.values() for q in qs})
    states = " ".join(f"wd:{q}" for q in DACH_STATES)
    count: dict[str, int] = {}
    for i in range(0, len(all_items), 20):
        stack = " ".join(f"wd:{q}" for q in all_items[i:i + 20])
        sparql = (f"SELECT ?item (COUNT(DISTINCT ?p) AS ?n) WHERE {{ VALUES ?item {{ {stack} }}"
                  f" ?p wdt:P734 ?item . ?p wdt:P27 ?st . VALUES ?st {{ {states} }} }}"
                  " GROUP BY ?item")
        for z in query(sparql):
            count[z["item"].rsplit("/", 1)[-1]] = int(z["n"])
        print(f"  Namensträger gezählt: {min(i + 20, len(all_items))}/{len(all_items)}",
              file=sys.stderr)
        time.sleep(1)
    return {b: sum(count.get(q, 0) for q in qs) for b, qs in items.items()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("target", nargs="?", default=str(TARGET))
    p.add_argument("--kldb", help="local copy of Alphabetisches-Verzeichnis-…xlsx")
    p.add_argument("--bfs", help="local copy of the BFS surname CSV (by language region)")
    p.add_argument("--without-wikidata", action="store_true",
                   help="surname flag from the BFS list only")
    p.add_argument("--log", help="write the surname counts per term to this CSV file")
    p.add_argument("--wikidata-cache", metavar="FILE",
                   help="JSON cache of the Wikidata surname counts (read if present, else written)")
    args = p.parse_args(argv)

    kldb_date, pairs = read_kldb(_load(URL_KLDB, args.kldb))
    occupation, failures = occupations(pairs)
    status = employment_status()
    print(f"KldB (Stand {kldb_date}): {len(pairs)} Benennungen, {len(occupation)} Begriffe,"
          f" {len(failures)} nicht aufgelöst")
    for a in failures:
        print(f"  nicht aufgelöst: {a}", file=sys.stderr)
    lines = {b: ("Beruf", k, g) for b, (k, g) in occupation.items() if b not in status}
    lines.update({b: ("Erwerbsstatus", k, g) for b, (k, g) in status.items()})

    bfs_year, bfs = bfs_frequencies(_load(URL_BFS, args.bfs))
    name_like = sorted(b for b in lines if re.fullmatch(r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ]"
                                                             r"[a-zäöüß]+)?", b))
    wd: dict[str, int] = {}
    store = Path(args.wikidata_cache) if args.wikidata_cache else None
    if not args.without_wikidata:
        if store and store.exists():
            stored = json.loads(store.read_text(encoding="utf-8"))
            missing = sorted(set(name_like) - set(stored["abgefragt"]))
            wd = stored["anzahl"] | (wikidata_surnames(missing) if missing else {})
        else:
            wd = wikidata_surnames(name_like)
        if store:
            store.write_text(json.dumps({"abgefragt": name_like, "anzahl": wd},
                                        ensure_ascii=False, indent=0), encoding="utf-8")
    surname = {b for b in name_like
               if bfs.get(b, 0) >= BFS_THRESHOLD or wd.get(b, 0) >= WIKIDATA_THRESHOLD}
    if args.log:
        with open(args.log, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["begriff", "klasse", "bfs_deutschschweiz", "wikidata_dach", "nachname"])
            for b in name_like:
                if b in bfs or b in wd:
                    w.writerow([b, lines[b][0], bfs.get(b, 0), wd.get(b, ""),
                                int(b in surname)])

    today = dt.date.today().isoformat()
    header = ("# Quelle: Statistik der Bundesagentur für Arbeit, Klassifikation der Berufe 2010 –"
              " überarbeitete Fassung 2020, Alphabetisches Verzeichnis der Berufsbenennungen"
              f" (Stand {kldb_date}), https://statistik.arbeitsagentur.de, eigene Berechnungen"
              " (Geschlechterformen aufgelöst, Zusätze gekürzt, ein Code je Begriff);"
              " Alltagsberufe und Erwerbsstatus: kuratierte Listen PseudonyMed mit Wikidata-QIDs"
              " bzw. zugeordneten KldB-Codes; Spalte nachname:"
              f" BFS STATPOP {bfs_year}, Nachnamen der ständigen Wohnbevölkerung nach"
              " Sprachregion (Deutschschweiz ≥ "
              f"{BFS_THRESHOLD}), opendata.swiss, und Wikidata (≥ {WIKIDATA_THRESHOLD} Personen"
              " mit DACH-Staatsangehörigkeit); Lizenz: BA-Nutzungsbedingungen (Weitergabe mit"
              " Quellenangabe, eigene Berechnungen kenntlich), BFS opendata.swiss"
              " (Quellenangabe Pflicht), Wikidata CC0 1.0; Stand: " + today + "\n")
    target = Path(args.target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # no file name and no time stamp in the gzip header: reproducible bytes
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz,
          io.TextIOWrapper(gz, encoding="utf-8", newline="") as f):
        f.write(header)
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["begriff", "klasse", "kennung", "grundform", "nachname"])
        for b in sorted(lines, key=lambda b: (lines[b][0], b.casefold(), b)):
            category, kldb_code, base = lines[b]
            w.writerow([b, category, kldb_code, base, int(b in surname)])
    count = collections.Counter(k for k, _, _ in lines.values())
    print(f"Berufe {count['Beruf']}, Erwerbsstatus {count['Erwerbsstatus']},"
          f" Nachnamenkonflikte {len(surname)}")
    print(f"geschrieben: {target} ({target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
