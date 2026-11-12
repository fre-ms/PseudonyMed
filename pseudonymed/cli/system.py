"""Commands about the installation: the desktop, the patient-file template, the configuration."""

from __future__ import annotations

from pathlib import Path

import typer

from pseudonymed import __version__
from pseudonymed.cli.basis import (
    SpeicherOption,
    app,
)
from pseudonymed.dokument.ocr import verfuegbare_backends
from pseudonymed.erkennung.codes import verfuegbar as codes_verfuegbar
from pseudonymed.konfig import (
    Einstellungen,
    konfigpfad,
    lade_einstellungen,
    lade_konfig,
)
from pseudonymed.pseudonyme.speicher import (
    schluesselpfad_zu,
    standardpfad,
)


@app.command()
def gui() -> None:
    """Grafische Oberfläche starten (Extra 'gui')."""
    from pseudonymed.gui.app import main

    raise typer.Exit(code=main([]))


@app.command()
def vorlage() -> None:
    """JSON-Vorlage für eine Patientendatei ausgeben."""
    from pseudonymed.quellen.manuell import vorlage as _vorlage

    typer.echo(_vorlage(), nl=False)


@app.command()
def konfig(speicher: SpeicherOption = None) -> None:
    """Version und Pfade anzeigen."""
    import pymupdf

    einstellungen = lade_einstellungen()
    typer.echo(f"PseudonyMed {__version__}")
    typer.echo(f"PyMuPDF {pymupdf.version[0]} (MuPDF {pymupdf.version[1]})")
    _werkzeuge_zeigen(einstellungen)
    _schluessel_zeigen(speicher, einstellungen)
    _zugaenge_zeigen()


def _werkzeuge_zeigen(einstellungen: Einstellungen) -> None:
    """OCR engines, Tesseract and its languages, barcodes, NER and the vocabulary."""
    from pseudonymed.dokument.ocr import tesseract_programm, tesseract_sprachen
    from pseudonymed.erkennung import fachbegriffe as fachmodul
    from pseudonymed.erkennung import ner as nermodul

    backends = verfuegbare_backends()
    typer.echo("OCR-Backends:        " + (", ".join(backends) if backends else
                                          "keine (Extra 'ocr' installieren oder Tesseract)")
               + f"  (bevorzugt: {einstellungen.ocr_backend})")
    tess = tesseract_programm(einstellungen.tesseract_pfad or None)
    if tess:
        sprachen = tesseract_sprachen(tess)
        relevante = [s for s in ("deu", "eng") if s in sprachen]
        sprachtext = ", ".join(relevante) or "deu/eng fehlen"
        typer.echo(f"Tesseract:           {tess}  (Sprachen: {sprachtext},"
                   f" insgesamt {len(sprachen)})")
    typer.echo("Barcodes/QR:         " + ("zxing-cpp" if codes_verfuegbar() else
                                          "nicht verfügbar (Extra 'codes')"))
    typer.echo("NER:                 " + (f"spaCy mit {nermodul.verfuegbares_modell()}"
                                          if nermodul.verfuegbar() else
                                          "nicht verfügbar (Extra 'ner' + deutsches Modell)"))
    fb = fachmodul.standard()
    quellen = ", ".join(f"{name} {n}" for name, n in fb.quellen)
    typer.echo(f"Fachbegriffe:        {fb.anzahl} geschützte Begriffe ({quellen});"
               f" eigene Listen in {fachmodul.datenordner()}")


def _schluessel_zeigen(speicher: Path | None, einstellungen: Einstellungen) -> None:
    """Database and key paths, keyring, local address book, OpenPGP, recipient key."""
    from pseudonymed.konfig import schluesselbund
    from pseudonymed.pseudonyme import pgp as pgpmodul
    from pseudonymed.pseudonyme.speicher import standard_schluesselpfad

    typer.echo(f"Pseudonym-Datenbank: {speicher or standardpfad()}")
    schluessel = schluesselpfad_zu(speicher) if speicher else standard_schluesselpfad()
    typer.echo(f"Pseudonymschlüssel:  {schluessel}"
               "  (sichern; gleicher Schlüssel = gleiche Kennungen auf jedem Rechner)")
    typer.echo("Schlüsselbund:       " + (schluesselbund() or "keiner (Extra 'carddav')"))
    if einstellungen.adressbuch_lokal:
        typer.echo(f"Lokales Adressbuch:  {einstellungen.adressbuch_lokal}")
    typer.echo("OpenPGP:             " + (", ".join(pgpmodul.engines()) or
                                         "nicht verfügbar (Extra 'pgp' oder GnuPG)"))
    if einstellungen.empfaenger_schluessel:
        typer.echo(f"Empfänger-Schlüssel: {einstellungen.empfaenger_schluessel}"
                   f" (Fingerabdruck {einstellungen.empfaenger_fingerabdruck or 'ungeprüft'})")


def _zugaenge_zeigen() -> None:
    """The configuration file and the CardDAV account."""
    k = lade_konfig()
    typer.echo(f"Konfiguration:       {konfigpfad()}")
    if k.vollstaendig:
        buch = k.adressbuch or "einziges Adressbuch"
        typer.echo(f"CardDAV:             {k.benutzer} @ {k.url} ({buch})")
    else:
        typer.echo("CardDAV:             nicht eingerichtet")
