"""Commands about the installation: the desktop, the patient-file template, the configuration."""

from __future__ import annotations

from pathlib import Path

import typer
from optdoc.ocr import available_backends

from pseudonymed import __version__
from pseudonymed.cli.base import (
    StoreOption,
    app,
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


@app.command()
def gui() -> None:
    """Grafische Oberfläche starten (Extra 'gui')."""
    from pseudonymed.gui.app import main

    raise typer.Exit(code=main([]))


@app.command("vorlage")
def template() -> None:
    """JSON-Vorlage für eine Patientendatei ausgeben."""
    from pseudonymed.sources.manual import template as _template

    typer.echo(_template(), nl=False)


@app.command("konfig")
def config(store: StoreOption = None) -> None:
    """Version und Pfade anzeigen."""
    import pymupdf

    settings = load_settings()
    typer.echo(f"PseudonyMed {__version__}")
    typer.echo(f"PyMuPDF {pymupdf.version[0]} (MuPDF {pymupdf.version[1]})")
    _show_tools(settings)
    _show_keys(store, settings)
    _show_accounts()


def _show_tools(settings: Settings) -> None:
    """OCR engines, Tesseract and its languages, barcodes, NER and the vocabulary."""
    from optdoc.ocr import tesseract_languages, tesseract_program

    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import ner as ner_module

    backends = available_backends()
    typer.echo("OCR-Backends:        " + (", ".join(backends) if backends else
                                          "keine (Extra 'ocr' installieren oder Tesseract)")
               + f"  (bevorzugt: {settings.ocr_backend})")
    tess = tesseract_program(settings.tesseract_path or None)
    if tess:
        languages = tesseract_languages(tess)
        relevant = [s for s in ("deu", "eng") if s in languages]
        language_text = ", ".join(relevant) or "deu/eng fehlen"
        typer.echo(f"Tesseract:           {tess}  (Sprachen: {language_text},"
                   f" insgesamt {len(languages)})")
    typer.echo("Barcodes/QR:         " + ("zxing-cpp" if codes_available() else
                                          "nicht verfügbar (Extra 'codes')"))
    typer.echo("NER:                 " + (f"spaCy mit {ner_module.available_model()}"
                                          if ner_module.available() else
                                          "nicht verfügbar (Extra 'ner' + deutsches Modell)"))
    fb = terms_module.standard()
    sources = ", ".join(f"{name} {n}" for name, n in fb.sources)
    typer.echo(f"Fachbegriffe:        {fb.count} geschützte Begriffe ({sources});"
               f" eigene Listen in {terms_module.data_folder()}")


def _show_keys(store: Path | None, settings: Settings) -> None:
    """Database and key paths, keyring, local address book, OpenPGP, recipient key."""
    from pseudonymed.config import keyring_backend
    from pseudonymed.pseudonyms import pgp as pgp_module
    from pseudonymed.pseudonyms.store import default_key_path

    typer.echo(f"Pseudonym-Datenbank: {store or default_path()}")
    key = key_path_for(store) if store else default_key_path()
    typer.echo(f"Pseudonymschlüssel:  {key}"
               "  (sichern; gleicher Schlüssel = gleiche Kennungen auf jedem Rechner)")
    typer.echo("Schlüsselbund:       " + (keyring_backend() or "keiner (Extra 'carddav')"))
    if settings.local_address_book:
        typer.echo(f"Lokales Adressbuch:  {settings.local_address_book}")
    typer.echo("OpenPGP:             " + (", ".join(pgp_module.engines()) or
                                         "nicht verfügbar (Extra 'pgp' oder GnuPG)"))
    if settings.recipient_key:
        typer.echo(f"Empfänger-Schlüssel: {settings.recipient_key}"
                   f" (Fingerabdruck {settings.recipient_fingerprint or 'ungeprüft'})")


def _show_accounts() -> None:
    """The configuration file and the CardDAV account."""
    k = load_config()
    typer.echo(f"Konfiguration:       {config_path()}")
    if k.complete:
        book = k.address_book or "einziges Adressbuch"
        typer.echo(f"CardDAV:             {k.user} @ {k.url} ({book})")
    else:
        typer.echo("CardDAV:             nicht eingerichtet")
