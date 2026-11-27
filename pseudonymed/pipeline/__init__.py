"""The pipeline shared by CLI and GUI: load, read (text layer and/or OCR), find,
redact, clean, check, report.

The phases live in their own modules; this package re-exports what the CLI, the
desktop and the working state use:

- :mod:`.optionen`: :class:`PipelineOptions`, :class:`Result`, :func:`target_path`
- :mod:`.seiten`: one page as read, the text with pseudonyms, the word under a point
- :mod:`.reading`: the reading phase over docrepro's chain (:class:`Reading`, :func:`read`,
  :func:`read_chain`)
- :mod:`.ruestzeug`: what detection needs for one patient
- :mod:`.erkennen`: the detection over pages that were read
- :mod:`.vorbereitung`: :class:`Preparation` and :func:`analyse`
- :mod:`.textdateien`: :class:`TextPreparation` and the text-file path
- :mod:`.ausgabe`: the side outputs both paths share
- :mod:`.abschluss`: :func:`finalise`, :func:`prepare`, :func:`pseudonymise`
"""

from pseudonymed.pipeline.detect import remember_medical_terms
from pseudonymed.pipeline.finish import finalise, prepare, pseudonymise
from pseudonymed.pipeline.options import PipelineOptions, Result, target_path
from pseudonymed.pipeline.output import VOCABULARY_HEADER, remember_decisions, vocabulary_from
from pseudonymed.pipeline.pages import PageEntry, markdown_from_text, word_at
from pseudonymed.pipeline.preparation import Preparation, analyse
from pseudonymed.pipeline.reading import Reading, chain_settings, read, read_chain
from pseudonymed.pipeline.text_files import TextPreparation, recheck_text, recheck_text_file
from pseudonymed.pipeline.toolkit import Toolkit, toolkit

__all__ = [
    "VOCABULARY_HEADER",
    "PipelineOptions",
    "Result",
    "Reading",
    "Toolkit",
    "PageEntry",
    "TextPreparation",
    "Preparation",
    "finalise",
    "analyse",
    "remember_decisions",
    "recheck_text",
    "recheck_text_file",
    "chain_settings",
    "read",
    "read_chain",
    "markdown_from_text",
    "remember_medical_terms",
    "pseudonymise",
    "toolkit",
    "vocabulary_from",
    "prepare",
    "word_at",
    "target_path",
]
