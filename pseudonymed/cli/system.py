"""Commands about the installation: the desktop, the patient-file template, the configuration."""

from __future__ import annotations

from pathlib import Path

import typer
from ocrdoc.ocr import available_backends

from pseudonymed import __version__
from pseudonymed.cli.base import (
    EXPLICIT_LANGUAGE,
    StoreOption,
    app,
    command,
    w,
)
from pseudonymed.config import (
    Settings,
    config_path,
    load_config,
    load_settings,
)
from pseudonymed.detection.codes import available as codes_available
from pseudonymed.pseudonyms.store import (
    default_path,
    key_path_for,
)


@command(app, "gui", "gui", help=w("Grafische Oberfläche starten (Extra 'gui').",
                                   "Start the desktop interface (extra 'gui')."))
def gui() -> None:
    """Start the desktop, handing on a language given explicitly on the command line."""
    from pseudonymed.gui.app import main

    raise typer.Exit(code=main(["--sprache", EXPLICIT_LANGUAGE] if EXPLICIT_LANGUAGE else []))


@command(app, "vorlage", "template", help=w("JSON-Vorlage für eine Patientendatei ausgeben.",
                                            "Print a JSON template for a patient file."))
def template() -> None:
    """Print the JSON template of a patient file."""
    from pseudonymed.sources.manual import template as _template

    typer.echo(_template(), nl=False)


@command(app, "konfig", "config",
         help=w("Version und Pfade anzeigen.", "Show the version and the paths."))
def config(store: StoreOption = None) -> None:
    """Print versions, tools, paths, keys and accounts of this installation."""
    import pymupdf

    settings = load_settings()
    typer.echo(f"PseudonyMed {__version__}")
    typer.echo(f"PyMuPDF {pymupdf.version[0]} (MuPDF {pymupdf.version[1]})")
    typer.echo(_document_layer())
    _show_tools(settings)
    _show_keys(store, settings)
    _show_accounts()


def _document_layer() -> str:
    """docrepro and the versions of the steps PseudonyMed takes its documents from."""
    import docrepro
    from docrepro.steps import installed

    from pseudonymed.document.chain import STEPS

    steps = ", ".join(f"{step.name} {adapter.version}" for step, adapter in installed()
                      if step.name in STEPS)
    return f"docrepro {docrepro.__version__} ({steps})"


def _show_tools(settings: Settings) -> None:
    """OCR engines, Tesseract and its languages, barcodes, NER and the vocabulary."""
    from ocrdoc.ocr import tesseract_languages, tesseract_program

    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import ner as ner_module
    from pseudonymed.detection import vocabulary as vocab_mod

    backends = available_backends()
    typer.echo(w("OCR-Backends:        ", "OCR engines:         ")
               + (", ".join(backends) if backends else
                  w("keine (RapidOCR fehlt; Tesseract installieren)",
                    "none (RapidOCR is missing; install Tesseract)"))
               + w(f"  (bevorzugt: {settings.ocr_backend})",
                   f"  (preferred: {settings.ocr_backend})"))
    tess = tesseract_program(settings.tesseract_path or None)
    if tess:
        languages = tesseract_languages(tess)
        relevant = [s for s in ("deu", "eng") if s in languages]
        language_text = ", ".join(relevant) or w("deu/eng fehlen", "deu/eng missing")
        typer.echo(w(f"Tesseract:           {tess}  (Sprachen: {language_text},"
                     f" insgesamt {len(languages)})",
                     f"Tesseract:           {tess}  (languages: {language_text},"
                     f" {len(languages)} in total)"))
    typer.echo("Barcodes/QR:         " + ("zxing-cpp" if codes_available() else
                                          w("nicht verfügbar (zxing-cpp fehlt)",
                                            "not available (zxing-cpp is missing)")))
    typer.echo("NER:                 "
               + (w(f"spaCy mit {ner_module.available_model()}",
                    f"spaCy with {ner_module.available_model()}")
                  if ner_module.available() else
                  w("nicht verfügbar (Extra 'ner' + deutsches Modell)",
                    "not available (extra 'ner' + German model)")))
    fb = terms_module.standard()
    sources = ", ".join(f"{name} {n}" for name, n in fb.sources)
    typer.echo(w(f"Fachbegriffe:        {fb.count} geschützte Begriffe ({sources});"
                 f" Ablage {vocab_mod.default_path()}",
                 f"Medical terms:       {fb.count} protected terms ({sources});"
                 f" store {vocab_mod.default_path()}"))


def _show_keys(store: Path | None, settings: Settings) -> None:
    """Database and key paths, keyring, local address book, OpenPGP, recipient key."""
    from pseudonymed.config import keyring_backend
    from pseudonymed.pseudonyms import pgp as pgp_module
    from pseudonymed.pseudonyms.store import default_key_path

    database = store or default_path()
    typer.echo(w(f"Pseudonym-Datenbank: {database}", f"Pseudonym database:  {database}"))
    key = key_path_for(store) if store else default_key_path()
    typer.echo(w(f"Pseudonymschlüssel:  {key}"
                 "  (sichern; gleicher Schlüssel = gleiche Kennungen auf jedem Rechner)",
                 f"Pseudonym key:       {key}"
                 "  (back it up; the same key gives the same codes on every computer)"))
    typer.echo(w("Schlüsselbund:       ", "Keyring:             ")
               + (keyring_backend() or w("keiner (Extra 'carddav')", "none (extra 'carddav')")))
    if settings.local_address_book:
        typer.echo(w(f"Lokales Adressbuch:  {settings.local_address_book}",
                     f"Local address book:  {settings.local_address_book}"))
    typer.echo("OpenPGP:             " + (", ".join(pgp_module.engines()) or
                                         w("nicht verfügbar (Extra 'pgp' oder GnuPG)",
                                           "not available (extra 'pgp' or GnuPG)")))
    if settings.recipient_key:
        fingerprint = settings.recipient_fingerprint or w("ungeprüft", "unchecked")
        typer.echo(w(f"Empfänger-Schlüssel: {settings.recipient_key}"
                     f" (Fingerabdruck {fingerprint})",
                     f"Recipient key:       {settings.recipient_key}"
                     f" (fingerprint {fingerprint})"))


def _show_accounts() -> None:
    """The configuration file and the CardDAV account."""
    k = load_config()
    typer.echo(w(f"Konfiguration:       {config_path()}", f"Configuration:       {config_path()}"))
    if k.complete:
        book = k.address_book or w("einziges Adressbuch", "the only address book")
        typer.echo(f"CardDAV:             {k.user} @ {k.url} ({book})")
    else:
        typer.echo(w("CardDAV:             nicht eingerichtet", "CardDAV:             not set up"))
