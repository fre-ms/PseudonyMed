"""The pipeline shared by CLI and GUI: load, read (text layer and/or OCR), find,
redact, clean, check, report.

The phases live in their own modules; this package re-exports what the CLI, the
desktop and the working state use:

- :mod:`.optionen`: :class:`Ablaufoptionen`, :class:`Ergebnis`, :func:`zielpfad`
- :mod:`.seiten`: one page as read, the text with pseudonyms, the word under a point
- :mod:`.lesen`: phase zero (:class:`Vorlage`, :func:`lade`) and the reading phase
  (:class:`Lesung`, :func:`lese`)
- :mod:`.ruestzeug`: what detection needs for one patient
- :mod:`.erkennen`: the detection over pages that were read
- :mod:`.vorbereitung`: :class:`Vorbereitung` and :func:`analysiere`
- :mod:`.textdateien`: :class:`Textvorbereitung` and the text-file path
- :mod:`.ausgabe`: the side outputs both paths share
- :mod:`.abschluss`: :func:`abschliessen`, :func:`vorbereiten`, :func:`pseudonymisiere`
"""

from pseudonymed.ablauf.abschluss import abschliessen, pseudonymisiere, vorbereiten
from pseudonymed.ablauf.ausgabe import VOKABULAR_KOPF, entscheidungen_merken, vokabular_aus
from pseudonymed.ablauf.erkennen import merke_fachbegriffe
from pseudonymed.ablauf.lesen import Lesung, Vorlage, lade, lese
from pseudonymed.ablauf.optionen import Ablaufoptionen, Ergebnis, zielpfad
from pseudonymed.ablauf.ruestzeug import Ruestzeug, ruestzeug
from pseudonymed.ablauf.seiten import Seite, markdown_aus_text, wort_bei
from pseudonymed.ablauf.textdateien import Textvorbereitung, gegenprobe_text, gegenprobe_textdatei
from pseudonymed.ablauf.vorbereitung import Vorbereitung, analysiere

__all__ = [
    "VOKABULAR_KOPF",
    "Ablaufoptionen",
    "Ergebnis",
    "Lesung",
    "Ruestzeug",
    "Seite",
    "Textvorbereitung",
    "Vorbereitung",
    "Vorlage",
    "abschliessen",
    "analysiere",
    "entscheidungen_merken",
    "gegenprobe_text",
    "gegenprobe_textdatei",
    "lade",
    "lese",
    "markdown_aus_text",
    "merke_fachbegriffe",
    "pseudonymisiere",
    "ruestzeug",
    "vokabular_aus",
    "vorbereiten",
    "wort_bei",
    "zielpfad",
]
