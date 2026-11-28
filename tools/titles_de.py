"""Build ``pseudonymed/data/titles_de.csv.gz``: academic, noble, religious and official titles.

Sources
-------
* **Curated list** in this script (``curated()``): academic degrees and positions with
  their usual abbreviations, Austrian and Swiss particularities (Mag., DI, lic. phil.,
  med. pract., Austrian "Berufstitel" such as Hofrat or Kommerzialrat), noble titles and
  particles, ecclesiastical titles, official titles ("Amtsbezeichnungen", ranks of the
  police and the Bundeswehr, parliamentary mandates such as MdB) and honorary titles.
  Names of titles and their abbreviations are facts; no table of a third party was copied.
  Abbreviations are only given where they are in common use; otherwise the field is empty.
* **Wikidata** (CC0 1.0): items that are instances or subclasses of "academic degree"
  (Q189533) with a German label and a German short name (P1813, language ``de``; Latin
  short names only in the German pattern "Dr. …", "Mag. …", "lic. …").  Items tied to a
  country outside Germany, Austria and Switzerland (P17), a few items with wrong short names
  (``WIKIDATA_EXCLUDED``), lower-case short names and abbreviations already in the
  curated list are left out, so Wikidata only adds degrees and abbreviations the list lacks.
  Wikidata's noble and honorary title classes were checked too but are dominated by
  non-German titles (British peerages, Soviet honorary titles) and are not used.

Columns: ``term`` (full form, one row per gender form), ``class`` (always "title"),
``kind`` (academic, nobility, religious, office, honorary), ``abbreviation`` (one per row; a
title with several abbreviations has several rows), ``country`` (DE, AT, CH or all).

Usage::

    .venv/bin/python -I tools/titles_de.py [--without-wikidata] [target.csv.gz]
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import gzip
import io
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
AGENT = ("PseudonyMed/0.1 (open-source medical de-identification;"
         " https://github.com/fre-ms/PseudonyMed)")
TARGET = Path(__file__).resolve().parent.parent / "pseudonymed" / "data" / "titles_de.csv.gz"
HEADER = ["term", "class", "kind", "abbreviation", "country"]
COUNTRIES = {"Q183": "DE", "Q40": "AT", "Q39": "CH"}
# Wrong or misleading short names on Wikidata (checked 2026-11-02): Abitur is no title;
# "Ph.D." carries the short name "Dr. phil."; "Dipl.-Ing." carries "M.Sc."; "Candidata
# oeconomiae" carries "cand. odont."; "Landinspektor" is a Danish degree.
WIKIDATA_EXCLUDED = frozenset({"Q708387", "Q752297", "Q15206450", "Q55334492",
                               "Q66499193"})
KINDS = ("academic", "nobility", "religious", "office", "honorary")  # in the order of the file
ALL = "all"  # the country of a title in use in all three countries

Row = tuple[str, str, str, str]          # term, kind, abbreviation, country


def female(term: str) -> str:
    """Feminine form of a German title noun ("Diplom-Psychologe" → "Diplom-Psychologin")."""
    header, _, rest = term.partition(" ")
    if header.endswith("mann"):
        header = header[:-4] + "frau"
    elif header.lower().endswith("rat"):
        header = header[:-3] + ("Rätin" if header[-3] == "R" else "rätin")
    elif header.endswith("anwalt"):
        header = header[:-6] + "anwältin"
    elif header.endswith("e"):
        header = header[:-1] + "in"
    else:
        header += "in"
    return f"{header} {rest}" if rest else header


def curated() -> list[Row]:
    """The curated list of titles."""
    z: list[Row] = []

    def t(kind: str, country: str, term: str, *abbr: str) -> None:
        for a in abbr or ("",):
            z.append((term, kind, a, country))

    def mf(kind: str, country: str, m: str, *abbr: str, f: str | None = None,
           abbr_f: tuple[str, ...] | None = None) -> None:
        """Masculine and feminine form with the same (or given) abbreviations."""
        t(kind, country, m, *abbr)
        t(kind, country, f or female(m), *(abbr if abbr_f is None else abbr_f))

    a = "academic"
    # doctorates (Germany and in general)
    mf(a, "all", "Doktor", "Dr.", f="Doktorin")
    t(a, "AT", "Doktorin", "Dr.in")
    t(a, "all", "Doktoren", "Dres.")
    for term, abbr, country in (
        ("Doktor der Medizin", "Dr. med.", "all"),
        ("Doktor der Zahnmedizin", "Dr. med. dent.", "all"),
        ("Doktor der Tiermedizin", "Dr. med. vet.", "all"),
        ("Doktor der Naturwissenschaften", "Dr. rer. nat.", "all"),
        ("Doktor der Philosophie", "Dr. phil.", "all"),
        ("Doktor der Rechtswissenschaft", "Dr. iur.", "all"),
        ("Doktor der Rechtswissenschaft", "Dr. jur.", "all"),
        ("Doktor beider Rechte", "Dr. iur. utr.", "all"),
        ("Doktor der Staatswissenschaften", "Dr. rer. pol.", "all"),
        ("Doktor der Wirtschaftswissenschaften", "Dr. rer. oec.", "all"),
        ("Doktor der Sozialwissenschaften", "Dr. rer. soc.", "all"),
        ("Doktor der Theologie", "Dr. theol.", "all"),
        ("Doktor der Ingenieurwissenschaften", "Dr.-Ing.", "DE"),
        ("Doktor der Agrarwissenschaften", "Dr. agr.", "all"),
        ("Doktor der Agrarwissenschaften", "Dr. sc. agr.", "DE"),
        ("Doktor der Erziehungswissenschaften", "Dr. paed.", "all"),
        ("Doktor der Humanbiologie", "Dr. rer. biol. hum.", "DE"),
        ("Doktor der Humanwissenschaften", "Dr. sc. hum.", "DE"),
        ("Doktor der Medizinwissenschaften", "Dr. rer. medic.", "DE"),
        ("Doktor der Gesundheitswissenschaften", "Dr. PH", "DE"),
        ("Doktor der Gesundheitswissenschaften", "Dr. P.H.", "DE"),
        ("Doktor der Pflegewissenschaft", "Dr. rer. cur.", "DE"),
        ("Doktor der Ernährungswissenschaften", "Dr. oec. troph.", "DE"),
        ("Doktor der Forstwissenschaften", "Dr. rer. silv.", "DE"),
        ("Doktor der Gartenbauwissenschaften", "Dr. rer. hort.", "DE"),
        ("Doktor der Sportwissenschaft", "Dr. Sportwiss.", "DE"),
        ("Doktor der Wissenschaften (Promotion B)", "Dr. sc.", "DE"),
        ("Doktor der Medizinwissenschaften (Promotion B)", "Dr. sc. med.", "DE"),
        ("Doktor der gesamten Heilkunde", "Dr. med. univ.", "AT"),
        ("Doktor der Zahnheilkunde", "Dr. med. dent.", "AT"),
        ("Doktor der medizinischen Wissenschaft", "Dr. scient. med.", "AT"),
        ("Doktor der technischen Wissenschaften", "Dr. techn.", "AT"),
        ("Doktor der montanistischen Wissenschaften", "Dr. mont.", "AT"),
        ("Doktor der Bodenkultur", "Dr. nat. techn.", "AT"),
        ("Doktor der Sozial- und Wirtschaftswissenschaften", "Dr. rer. soc. oec.", "AT"),
        ("Doktor der Naturwissenschaften", "Dr. sc. nat.", "CH"),
        ("Doktor der Naturwissenschaften", "Dr. phil. nat.", "CH"),
        ("Doktor der technischen Wissenschaften", "Dr. sc. techn.", "CH"),
        ("Doktor der Wissenschaften", "Dr. sc.", "CH"),
        ("Ehrendoktor", "Dr. h. c.", "all"),
        ("Ehrendoktor", "Dr. h.c.", "all"),
        ("Ehrendoktor der Ingenieurwissenschaften", "Dr.-Ing. E. h.", "DE"),
        ("mehrfacher Doktor", "Dr. mult.", "all"),
        ("zweifacher Doktor", "DDr.", "AT"),
        ("zweifacher Magister", "MMag.", "AT"),
        ("habilitierter Doktor", "Dr. habil.", "all"),
        ("habilitierter Doktor der Medizin", "Dr. med. habil.", "all"),
        ("habilitierter Doktor der Naturwissenschaften", "Dr. rer. nat. habil.", "all"),
        ("habilitierter Doktor der Philosophie", "Dr. phil. habil.", "all"),
        ("habilitiert", "habil.", "all"),
        ("Doctor of Philosophy", "Ph.D.", "all"), ("Doctor of Philosophy", "PhD", "all"),
        ("Doctor of Philosophy", "D.Phil.", "all"),
        ("Doctor of Medicine", "M.D.", "all"), ("Doctor of Medicine", "MD", "all"),
        ("Doctor of Dental Surgery", "DDS", "all"),
        ("Doctor of Dental Medicine", "DMD", "all"),
        ("Doctor of Public Health", "DrPH", "all"),
        ("Doctor of Business Administration", "DBA", "all"),
        ("Doctor of Science", "D.Sc.", "all"), ("Juris Doctor", "J.D.", "all"),
        ("Doctor of Pharmacy", "Pharm.D.", "all"),
    ):
        t(a, country, term, abbr)
    # students and candidates
    mf(a, "all", "Kandidat der Medizin", "cand. med.", f="Kandidatin der Medizin")
    mf(a, "all", "Kandidat der Zahnmedizin", "cand. med. dent.",
       f="Kandidatin der Zahnmedizin")
    mf(a, "all", "Kandidat der Philosophie", "cand. phil.", f="Kandidatin der Philosophie")
    mf(a, "all", "Kandidat der Rechtswissenschaft", "cand. iur.",
       f="Kandidatin der Rechtswissenschaft")
    mf(a, "all", "Kandidat der Psychologie", "cand. psych.", f="Kandidatin der Psychologie")
    mf(a, "all", "Student der Medizin", "stud. med.", f="Studentin der Medizin")
    # diploma degrees (Germany, Austria, GDR)
    for m, abbr in (
        ("Diplom-Ingenieur", "Dipl.-Ing."), ("Diplom-Psychologe", "Dipl.-Psych."),
        ("Diplom-Betriebswirt", "Dipl.-Betriebsw."), ("Diplom-Volkswirt", "Dipl.-Volksw."),
        ("Diplom-Ökonom", "Dipl.-Ök."), ("Diplom-Ökonom", "Dipl.-Oec."),
        ("Diplom-Informatiker", "Dipl.-Inf."), ("Diplom-Informatiker", "Dipl.-Inform."),
        ("Diplom-Physiker", "Dipl.-Phys."), ("Diplom-Chemiker", "Dipl.-Chem."),
        ("Diplom-Biologe", "Dipl.-Biol."), ("Diplom-Mathematiker", "Dipl.-Math."),
        ("Diplom-Pädagoge", "Dipl.-Päd."), ("Diplom-Sozialpädagoge", "Dipl.-Soz.-Päd."),
        ("Diplom-Sozialpädagoge", "Dipl.-Sozialpäd."),
        ("Diplom-Sozialarbeiter", "Dipl.-Soz.-Arb."), ("Diplom-Soziologe", "Dipl.-Soz."),
        ("Diplom-Politologe", "Dipl.-Pol."), ("Diplom-Geograph", "Dipl.-Geogr."),
        ("Diplom-Geologe", "Dipl.-Geol."), ("Diplom-Jurist", "Dipl.-Jur."),
        ("Diplom-Theologe", "Dipl.-Theol."), ("Diplom-Pflegewirt", "Dipl.-Pflegew."),
        ("Diplom-Pflegepädagoge", "Dipl.-Pflegepäd."),
        ("Diplom-Heilpädagoge", "Dipl.-Heilpäd."),
        ("Diplom-Sportwissenschaftler", "Dipl.-Sportwiss."),
        ("Diplom-Sportlehrer", "Dipl.-Sportl."), ("Diplom-Designer", "Dipl.-Des."),
        ("Diplom-Wirtschaftsingenieur", "Dipl.-Wirt.-Ing."),
        ("Diplom-Verwaltungswirt", "Dipl.-Verw.-Wirt"), ("Diplom-Finanzwirt", "Dipl.-Finw."),
        ("Diplom-Rechtspfleger", "Dipl.-Rpfl."), ("Diplom-Dolmetscher", "Dipl.-Dolm."),
        ("Diplom-Übersetzer", "Dipl.-Übers."), ("Diplom-Musiklehrer", ""),
        ("Diplom-Oecotrophologe", "Dipl. oec. troph."),
        ("Diplom-Ingenieur (FH)", "Dipl.-Ing. (FH)"),
        ("Diplom-Betriebswirt (FH)", "Dipl.-Betriebsw. (FH)"),
        ("Diplom-Sozialpädagoge (FH)", "Dipl.-Soz.-Päd. (FH)"),
    ):
        mf(a, "DE" if "(FH)" in m else "all", m, abbr)
    mf(a, "all", "Diplom-Kaufmann", "Dipl.-Kfm.", abbr_f=("Dipl.-Kffr.", "Dipl.-Kfr."))
    mf(a, "DE", "Diplom-Kaufmann (FH)", "Dipl.-Kfm. (FH)", abbr_f=("Dipl.-Kffr. (FH)",))
    mf(a, "DE", "Diplom-Mediziner", "Dipl.-Med.")
    mf(a, "DE", "Diplom-Stomatologe", "Dipl.-Stom.")
    mf(a, "DE", "Diplomlehrer", "")
    t(a, "AT", "Diplom-Ingenieur", "DI")
    t(a, "AT", "Diplom-Ingenieurin", "DIin", "Dipl.-Ing.in")
    mf(a, "AT", "Ingenieur", "Ing.")
    t(a, "DE", "graduierter Ingenieur", "Ing. grad.")
    # bachelor and master
    for term, abbr in (
        ("Bachelor of Arts", ("B.A.", "BA", "B. A.")),
        ("Bachelor of Science", ("B.Sc.", "BSc", "B. Sc.")),
        ("Bachelor of Engineering", ("B.Eng.", "B. Eng.")),
        ("Bachelor of Laws", ("LL.B.",)), ("Bachelor of Education", ("B.Ed.", "BEd")),
        ("Bachelor of Fine Arts", ("B.F.A.", "BFA")), ("Bachelor of Music", ("B.Mus.",)),
        ("Bachelor of Business Administration", ("BBA",)),
        ("Master of Arts", ("M.A.", "MA", "M. A.")),
        ("Master of Science", ("M.Sc.", "MSc", "M. Sc.")),
        ("Master of Engineering", ("M.Eng.", "M. Eng.")), ("Master of Laws", ("LL.M.",)),
        ("Master of Education", ("M.Ed.", "MEd")), ("Master of Fine Arts", ("M.F.A.", "MFA")),
        ("Master of Music", ("M.Mus.",)), ("Master of Business Administration", ("MBA",)),
        ("Master of Public Health", ("MPH",)), ("Master of Public Administration", ("MPA",)),
        ("Master of Advanced Studies", ("MAS",)), ("Master of Philosophy", ("M.Phil.",)),
        ("Master of Research", ("MRes",)), ("Master of Social Work", ("MSW",)),
        ("Master of Science in Nursing", ("MScN",)),
        ("Bachelor Professional", ("",)), ("Master Professional", ("",)),
    ):
        t(a, "all", term, *abbr)
    t(a, "DE", "Magister Artium", "M.A.")
    mf(a, "AT", "Bakkalaureus", "Bakk.", f="Bakkalaurea")
    # Austrian magister degrees
    t(a, "AT", "Magister", "Mag.")
    t(a, "AT", "Magistra", "Mag.", "Mag.a")
    for term, abbr in (
        ("Magister der Philosophie", "Mag. phil."),
        ("Magister der Naturwissenschaften", "Mag. rer. nat."),
        ("Magister der Rechtswissenschaften", "Mag. iur."),
        ("Magister der Sozial- und Wirtschaftswissenschaften", "Mag. rer. soc. oec."),
        ("Magister der Pharmazie", "Mag. pharm."), ("Magister der Künste", "Mag. art."),
        ("Magister (FH)", "Mag. (FH)"),
    ):
        t(a, "AT", term, abbr)
    t(a, "all", "Magister der Theologie", "Mag. theol.")
    # Swiss degrees and titles
    for term, abbr in (
        ("Lizentiat der Philosophie", "lic. phil."),
        ("Lizentiat der Rechtswissenschaft", "lic. iur."),
        ("Lizentiat der Wirtschaftswissenschaften", "lic. oec."),
        ("Lizentiat der Staatswissenschaften", "lic. rer. pol."),
        ("diplomierter Ingenieur ETH", "dipl. Ing. ETH"),
        ("diplomierter Architekt ETH", "dipl. Arch. ETH"),
        ("eidgenössisch diplomiert", "eidg. dipl."),
        ("eidgenössisch diplomierter Arzt", "med. pract."),
        ("Facharzttitel FMH", "FMH"),
    ):
        t(a, "CH", term, abbr)
    t(a, "all", "Lizentiat der Theologie", "lic. theol.")
    # academic positions
    mf(a, "all", "Professor", "Prof.")
    t(a, "AT", "Professorin", "Prof.in")
    mf(a, "all", "Universitätsprofessor", "Univ.-Prof.")
    t(a, "AT", "Universitätsprofessorin", "Univ.-Prof.in")
    mf(a, "AT", "außerordentlicher Universitätsprofessor", "ao. Univ.-Prof.",
       f="außerordentliche Universitätsprofessorin")
    mf(a, "AT", "ordentlicher Universitätsprofessor", "o. Univ.-Prof.",
       f="ordentliche Universitätsprofessorin")
    mf(a, "DE", "außerplanmäßiger Professor", "apl. Prof.",
       f="außerplanmäßige Professorin")
    mf(a, "all", "Honorarprofessor", "Hon.-Prof.")
    mf(a, "DE", "Juniorprofessor", "Jun.-Prof.")
    t(a, "all", "Professor emeritus", "Prof. em.")
    mf(a, "all", "emeritierter Professor", "em. Prof.", f="emeritierte Professorin")
    mf(a, "CH", "Titularprofessor", "Tit.-Prof.")
    mf(a, "AT", "Assistenzprofessor", "Ass.-Prof.")
    mf(a, "AT", "assoziierter Professor", "Assoz. Prof.", f="assoziierte Professorin")
    mf(a, "all", "Gastprofessor", "Gastprof.")
    mf(a, "all", "Privatdozent", "PD", "Priv.-Doz.")
    mf(a, "AT", "Universitätsdozent", "Univ.-Doz.")
    mf(a, "all", "Dozent", "Doz.")
    mf(a, "all", "Rektor", "")
    mf(a, "all", "Dekan", "")

    a = "nobility"
    for m, f, abbr, abbr_f in (
        ("Kaiser", "Kaiserin", "", ""), ("König", "Königin", "", ""),
        ("Erzherzog", "Erzherzogin", "", ""), ("Großherzog", "Großherzogin", "", ""),
        ("Kurfürst", "Kurfürstin", "", ""), ("Herzog", "Herzogin", "", ""),
        ("Erbherzog", "Erbherzogin", "", ""), ("Fürst", "Fürstin", "", ""),
        ("Prinz", "Prinzessin", "", ""), ("Erbprinz", "Erbprinzessin", "", ""),
        ("Kronprinz", "Kronprinzessin", "", ""), ("Markgraf", "Markgräfin", "", ""),
        ("Landgraf", "Landgräfin", "", ""), ("Pfalzgraf", "Pfalzgräfin", "", ""),
        ("Burggraf", "Burggräfin", "", ""), ("Reichsgraf", "Reichsgräfin", "", ""),
        ("Altgraf", "Altgräfin", "", ""), ("Erbgraf", "Erbgräfin", "", ""),
        ("Graf", "Gräfin", "Gf.", "Gfn."), ("Freiherr", "Freifrau", "Frhr.", "Frfr."),
        ("Reichsfreiherr", "Reichsfreifrau", "", ""), ("Baron", "Baronin", "", ""),
        ("Edler", "Edle", "", ""), ("Ritter", "", "", ""), ("Junker", "", "", ""),
    ):
        t(a, "all", m, abbr)
        if f:
            t(a, "all", f, abbr_f)
    t(a, "all", "Freiherr", "Frh.")
    for term in ("Freiin", "Baronesse", "Komtesse", "Prinzgemahl"):
        t(a, "all", term)
    for term, abbr in (("von", "v."), ("zu", "z."), ("von und zu", "v. u. z."),
                       ("vom", ""), ("von der", ""), ("zur", ""), ("zum", ""),
                       ("Edler von", "Edler v."), ("Ritter von", "Ritter v.")):
        t(a, "all", term, abbr)
    for term, abbr in (("Majestät", ""), ("Seine Majestät", "S. M."),
                       ("Ihre Majestät", "I. M."), ("Hoheit", ""),
                       ("Königliche Hoheit", ""), ("Seine Königliche Hoheit", "S. K. H."),
                       ("Ihre Königliche Hoheit", "I. K. H."), ("Kaiserliche Hoheit", ""),
                       ("Durchlaucht", ""), ("Seine Durchlaucht", "S. D."),
                       ("Ihre Durchlaucht", "I. D."), ("Erlaucht", ""),
                       ("Hochgeboren", ""), ("Hochwohlgeboren", "")):
        t(a, "all", term, abbr)

    a = "religious"
    for m, f, abbr, abbr_f in (
        ("Kardinal", "", "Kard.", ""), ("Erzbischof", "", "Erzb.", ""),
        ("Bischof", "Bischöfin", "", ""), ("Weihbischof", "", "", ""),
        ("Landesbischof", "Landesbischöfin", "", ""), ("Generalvikar", "", "", ""),
        ("Prälat", "", "", ""), ("Monsignore", "", "Msgr.", ""),
        ("Domkapitular", "", "", ""), ("Domherr", "", "", ""), ("Dompropst", "", "", ""),
        ("Domdekan", "", "", ""), ("Kanonikus", "", "", ""),
        ("Propst", "Pröpstin", "", ""), ("Dekan", "Dekanin", "", ""),
        ("Superintendent", "Superintendentin", "Sup.", "Sup."),
        ("Oberkirchenrat", "Oberkirchenrätin", "OKR", "OKR"),
        ("Kirchenrat", "Kirchenrätin", "KR", "KR"),
        ("Pfarrer", "Pfarrerin", "Pfr.", "Pfrn."), ("Pastor", "Pastorin", "", ""),
        ("Pastoralreferent", "Pastoralreferentin", "", ""),
        ("Gemeindereferent", "Gemeindereferentin", "", ""),
        ("Diakon", "Diakonin", "", ""), ("Ständiger Diakon", "", "", ""),
        ("Vikar", "Vikarin", "", ""), ("Kaplan", "", "", ""),
        ("Pater", "", "P.", ""), ("Frater", "", "Fr.", ""), ("Bruder", "", "Br.", ""),
        ("Schwester", "", "Sr.", ""), ("Abt", "Äbtissin", "", ""),
        ("Prior", "Priorin", "", ""), ("Oberin", "", "", ""), ("Mutter Oberin", "", "", ""),
        ("Diakonisse", "", "", ""), ("Rabbiner", "Rabbinerin", "", ""),
        ("Landesrabbiner", "Landesrabbinerin", "", ""), ("Imam", "", "", ""),
        ("Hochwürden", "", "Hw.", ""), ("Ehrwürden", "", "", ""),
    ):
        t(a, "all", m, abbr)
        if f:
            t(a, "all", f, abbr_f)
    t(a, "AT", "Kooperator", "Koop.")

    a = "office"
    for m, abbr, country in (
        ("Studienrat", "StR", "DE"), ("Oberstudienrat", "OStR", "DE"),
        ("Studiendirektor", "StD", "DE"), ("Oberstudiendirektor", "OStD", "DE"),
        ("Konrektor", "", "DE"), ("Schulrat", "", "DE"),
        ("Regierungsrat", "RR", "DE"), ("Oberregierungsrat", "ORR", "DE"),
        ("Regierungsdirektor", "RD", "DE"), ("Leitender Regierungsdirektor", "LtdRD", "DE"),
        ("Ministerialrat", "MinR", "DE"), ("Ministerialdirigent", "MinDirig", "DE"),
        ("Ministerialdirektor", "MinDir", "DE"), ("Staatssekretär", "StS", "DE"),
        ("Amtsrat", "AR", "DE"), ("Oberamtsrat", "OAR", "DE"),
        ("Medizinalrat", "MedR", "DE"), ("Obermedizinalrat", "OMedR", "DE"),
        ("Medizinaldirektor", "MedDir", "DE"),
        ("Leitender Medizinaldirektor", "LtdMedDir", "DE"),
        ("Veterinärrat", "VetR", "DE"), ("Veterinärdirektor", "VetDir", "DE"),
        ("Richter am Amtsgericht", "RiAG", "DE"), ("Richter am Landgericht", "RiLG", "DE"),
        ("Vorsitzender Richter am Landgericht", "VRiLG", "DE"),
        ("Richter am Oberlandesgericht", "RiOLG", "DE"),
        ("Staatsanwalt", "StA", "DE"), ("Oberstaatsanwalt", "OStA", "DE"),
        ("Leitender Oberstaatsanwalt", "LOStA", "DE"),
        ("Generalstaatsanwalt", "GStA", "DE"),
        ("Polizeimeister", "PM", "DE"), ("Polizeiobermeister", "POM", "DE"),
        ("Polizeihauptmeister", "PHM", "DE"), ("Polizeikommissar", "PK", "DE"),
        ("Polizeioberkommissar", "POK", "DE"), ("Polizeihauptkommissar", "PHK", "DE"),
        ("Kriminalkommissar", "KK", "DE"), ("Kriminaloberkommissar", "KOK", "DE"),
        ("Kriminalhauptkommissar", "KHK", "DE"),
        ("Bürgermeister", "Bgm.", "all"), ("Oberbürgermeister", "OB", "DE"),
        ("Landrat", "", "DE"), ("Stadtrat", "", "all"), ("Gemeinderat", "", "all"),
        ("Minister", "", "all"), ("Bundesminister", "", "all"),
        ("Ministerpräsident", "", "DE"), ("Staatsminister", "", "DE"),
        ("Senator", "", "DE"), ("Bundeskanzler", "", "all"),
        ("Bundespräsident", "", "all"), ("Nationalrat", "", "AT"),
        ("Nationalrat", "", "CH"), ("Ständerat", "", "CH"), ("Bundesrat", "", "CH"),
        ("Landeshauptmann", "LH", "AT"), ("Bezirkshauptmann", "BH", "AT"),
        ("Botschafter", "", "all"), ("Generalkonsul", "", "all"),
        ("Konsul", "", "all"), ("Honorarkonsul", "", "all"),
    ):
        f = (m.replace("Leitender ", "Leitende ").replace("Vorsitzender ", "Vorsitzende ")
             if m.startswith(("Leitender ", "Vorsitzender ")) else m)
        if f.startswith(("Leitende ", "Vorsitzende ")):
            f = f.split(" ", 1)[0] + " " + female(f.split(" ", 1)[1])
        else:
            f = female(f)
        if m == "Landeshauptmann":
            f = "Landeshauptfrau"
        t(a, country, m, abbr)
        t(a, country, f, abbr)
    for term, abbr, country in (
        ("Mitglied des Bundestages", "MdB", "DE"), ("Mitglied des Landtages", "MdL", "DE"),
        ("Mitglied des Europäischen Parlaments", "MdEP", "all"),
        ("Mitglied des Abgeordnetenhauses", "MdA", "DE"),
        ("Mitglied der Hamburgischen Bürgerschaft", "MdHB", "DE"),
        ("Mitglied der Bremischen Bürgerschaft", "MdBB", "DE"),
        ("Abgeordneter zum Nationalrat", "Abg. z. NR", "AT"),
        ("Abgeordnete zum Nationalrat", "Abg. z. NR", "AT"),
        ("außer Dienst", "a. D.", "all"), ("außer Dienst", "a.D.", "all"),
        ("im Ruhestand", "i. R.", "all"), ("im Ruhestand", "i.R.", "all"),
        ("emeritus", "em.", "all"),
        ("Gefreiter", "Gefr.", "DE"), ("Obergefreiter", "", "DE"),
        ("Hauptgefreiter", "", "DE"), ("Stabsgefreiter", "", "DE"),
        ("Unteroffizier", "Uffz.", "DE"), ("Stabsunteroffizier", "", "DE"),
        ("Feldwebel", "Fw.", "DE"), ("Oberfeldwebel", "", "DE"),
        ("Hauptfeldwebel", "", "DE"), ("Stabsfeldwebel", "", "DE"),
        ("Oberstabsfeldwebel", "", "DE"), ("Leutnant", "Lt.", "DE"),
        ("Oberleutnant", "", "DE"), ("Hauptmann", "Hptm.", "DE"), ("Major", "", "DE"),
        ("Oberstleutnant", "", "DE"), ("Oberst", "", "DE"),
        ("Brigadegeneral", "", "DE"), ("Generalmajor", "", "DE"),
        ("Generalleutnant", "", "DE"), ("General", "", "DE"),
        ("Kapitänleutnant", "", "DE"), ("Korvettenkapitän", "", "DE"),
        ("Fregattenkapitän", "", "DE"), ("Kapitän zur See", "", "DE"),
        ("Flottillenadmiral", "", "DE"), ("Konteradmiral", "", "DE"),
        ("Vizeadmiral", "", "DE"), ("Admiral", "", "DE"),
        ("Stabsarzt", "", "DE"), ("Oberstabsarzt", "", "DE"), ("Oberfeldarzt", "", "DE"),
        ("Oberstarzt", "", "DE"), ("Generalstabsarzt", "", "DE"), ("Generalarzt", "", "DE"),
        ("Exzellenz", "", "all"), ("Seine Exzellenz", "S. E.", "all"),
        ("Ihre Exzellenz", "I. E.", "all"),
    ):
        t(a, country, term, abbr)

    a = "honorary"
    for m, abbr, country in (
        ("Sanitätsrat", "SanR", "DE"), ("Justizrat", "JR", "DE"),
        ("Geheimrat", "Geh. Rat", "DE"), ("Geheimer Sanitätsrat", "", "DE"),
        ("Geheimer Regierungsrat", "", "DE"), ("Kommerzienrat", "", "DE"),
        ("Kammersänger", "KS", "DE"), ("Kammersänger", "KS", "AT"),
        ("Kammerschauspieler", "", "DE"), ("Kammerschauspieler", "", "AT"),
        ("Kammervirtuose", "", "DE"), ("Kammermusiker", "", "DE"),
        ("Ehrenbürger", "", "all"), ("Ehrensenator", "", "all"),
        ("Hofrat", "HR", "AT"), ("Wirklicher Hofrat", "", "AT"),
        ("Regierungsrat", "RegR", "AT"), ("Kommerzialrat", "KommR", "AT"),
        ("Ökonomierat", "ÖkR", "AT"), ("Medizinalrat", "MR", "AT"),
        ("Obermedizinalrat", "OMR", "AT"), ("Veterinärrat", "VR", "AT"),
        ("Studienrat", "StR", "AT"), ("Oberstudienrat", "OStR", "AT"),
        ("Schulrat", "SR", "AT"), ("Oberschulrat", "OSR", "AT"),
        ("Technischer Rat", "TR", "AT"), ("Baurat h. c.", "BR h. c.", "AT"),
        ("Bergrat h. c.", "", "AT"), ("Forstrat h. c.", "", "AT"),
        ("Konsulent", "", "AT"), ("Professor", "Prof.", "AT"),
    ):
        if m.startswith(("Geheimer ", "Wirklicher ", "Technischer ")):
            first, rest = m.split(" ", 1)
            f = first[:-1] + " " + female(rest)
        else:
            f = female(m)
        t(a, country, m, abbr)
        t(a, country, f, abbr)
    t(a, "DE", "Professor h. c.", "Prof. h. c.")
    # examples of the GeMTeX annotation guideline, version 2 (7 September 2025, section 5.3,
    # doi:10.5281/zenodo.15747389) that the lists above did not have yet
    for term, abbr, country in (
        ("Doktor der Medizinischen Wissenschaften", "Dr. rer. med.", "DE"),
        ("Doktor der Medizinischen Wissenschaften", "Dr. med. sci.", "DE"),
        ("Doktor der Medizinischen Wissenschaften", "Dr. rer. hum.", "DE"),
        ("Doktor der naturwissenschaftlichen Medizin", "Dr. nat. med.", "DE"),
        ("Doktor ehrenhalber (Ingenieurwissenschaften)", "Dr. E.h.", "DE"),
        ("Diplommedizinpädagoge", "Dipl.-Med. Päd.", "DE"),
        ("Diplommediziner", "Dipl. med.", "DE"),
        ("Bachelor of Science in Medical Sciences", "BScMed", "AT"),
        ("Bachelor of Medicine", "BMed", "CH"),
        ("Master of Medicine", "MMed", "CH"),
        ("Bachelor of Dental Medicine", "B Dent Med", "CH"),
        ("Master of Dental Medicine", "M Dent Med", "CH"),
        ("Master of Chiropractic Medicine", "M Chiro Med", "CH"),
        ("Doktor der Chiropraktischen Medizin", "Dr. med. chiro.", "CH"),
        ("Bachelor of Science in Nursing", "BSN", "CH"),
        ("Bachelor of Science in Nursing", "BScN", "CH"),
        ("Master of Science in Nursing", "MSN", "CH"),
        ("Medicinae universae doctor", "MUDr.", "all"),
        ("Medicinae dentalis doctor", "MDDr.", "all"),
        ("Candidatus scientiarum", "CSc", "all"),
        ("Doctor scientiarum", "DrSc", "all"),
        ("Doctor scientiarum", "DSc", "all"),
    ):
        t("academic", country, term, abbr)
    return z


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
            print(f"  attempt {attempt + 1} failed ({e}); waiting {wait_time}s",
                  file=sys.stderr)
            time.sleep(wait_time)
    raise RuntimeError("Wikidata query failed repeatedly")


def wikidata_degrees(known_abbr: set[str]) -> list[Row]:
    """Academic degrees and abbreviations from Wikidata that the curated list lacks."""
    sparql = """SELECT DISTINCT ?item ?de ?short (LANG(?short) AS ?lang) ?country WHERE {
  ?item (wdt:P31|wdt:P279)/wdt:P279* wd:Q189533 .
  ?item rdfs:label ?de . FILTER(LANG(?de) = "de")
  ?item wdt:P1813 ?short . FILTER(LANG(?short) IN ("de", "la"))
  OPTIONAL { ?item wdt:P17 ?country }
}"""
    names: dict[str, str] = {}
    short_forms: dict[str, set[str]] = collections.defaultdict(set)
    states: dict[str, set[str]] = collections.defaultdict(set)
    for z in query(sparql):
        qid = z["item"].rsplit("/", 1)[-1]
        names[qid] = z["de"].strip()
        short = z["short"].strip()
        if z["lang"] == "de" or re.match(r"^(?:Dr\.|Mag\.|lic\.) ", short):
            short_forms[qid].add(short)
        if z["country"]:
            states[qid].add(z["country"].rsplit("/", 1)[-1])
    lines: list[Row] = []
    for qid, term in sorted(names.items()):
        countries = states[qid]
        if qid in WIKIDATA_EXCLUDED or (countries and not countries <= set(COUNTRIES)):
            continue
        country = COUNTRIES[next(iter(countries))] if len(countries) == 1 else ALL
        for short in sorted(short_forms[qid]):
            if (short in known_abbr or not short[:1].isupper()
                    or re.search(r"[A-Z][a-z]*Dr\.$", short)):
                continue                    # known, or Czech/Slovak forms such as "MVDr."
            lines.append((term, "academic", short, country))
    return lines


def ordered(rows: set[Row]) -> list[Row]:
    """The rows of the file: by kind, then term, country and abbreviation."""
    return sorted(rows, key=lambda z: (KINDS.index(z[1]), z[0].casefold(), z[3], z[2]))


def header_line(today: str) -> str:
    """The provenance line at the top of the file: sources, licences and date."""
    return ("# Source: curated list of PseudonyMed (designations and common abbreviations as"
            " facts, supplemented with the title examples of the GeMTeX annotation guideline,"
            " version 2, 2025, doi:10.5281/zenodo.15747389) and Wikidata (academic degrees,"
            " Q189533, with a German short name P1813); Licence: curated list"
            " AGPL-3.0-or-later like PseudonyMed, Wikidata CC0 1.0; As of: " + today + "\n")


def write(target: Path, header: str, unique: list[Row]) -> None:
    """Write the provenance line, the column names and the rows, gzip-compressed with
    reproducible bytes."""
    buffer = io.StringIO()
    buffer.write(header)
    w = csv.writer(buffer, lineterminator="\n")
    w.writerow(HEADER)
    for term, kind, abbr, country in unique:
        w.writerow([term, "title", kind, abbr, country])
    target.parent.mkdir(parents=True, exist_ok=True)
    # no file name and no time stamp in the gzip header, the text compressed in one piece:
    # reproducible bytes
    with (open(target, "wb") as raw,
          gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz):
        gz.write(buffer.getvalue().encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("target", nargs="?", default=str(TARGET))
    p.add_argument("--without-wikidata", action="store_true", help="curated list only")
    args = p.parse_args(argv)

    lines = curated()
    wd: list[Row] = []
    if not args.without_wikidata:
        wd = wikidata_degrees({a for _, _, a, _ in lines if a})
        print(f"Wikidata: {len(wd)} additional degrees: "
              + "; ".join(f"{b} ({a})" for b, _, a, _ in wd))
    unique = ordered(set(lines) | set(wd))
    target = Path(args.target)
    write(target, header_line(dt.date.today().isoformat()), unique)
    per_kind = collections.Counter(kind for _, kind, _, _ in unique)
    terms_per_kind = collections.Counter(kind for _, kind in {(b, a) for b, a, _, _ in unique})
    print(f"{len(unique)} rows, " + ", ".join(
        f"{kind} {per_kind[kind]} ({terms_per_kind[kind]} terms)" for kind in KINDS))
    print(f"written: {target} ({target.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
