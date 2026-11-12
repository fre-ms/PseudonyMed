"""Commands over the pseudonym database and the OpenPGP keys."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import umgebung
from pseudonymed.cli.basis import (
    SpeicherOption,
    abbruch,
    app,
)
from pseudonymed.pseudonyme.speicher import (
    standardpfad,
)

datenbank_app = typer.Typer(
    help="Pseudonym-Datenbank und Schlüssel: Statistik, Sicherung, Wiederherstellung.",
    no_args_is_help=True,
)
app.add_typer(datenbank_app, name="datenbank")


@datenbank_app.command("status")
def datenbank_status(speicher: SpeicherOption = None) -> None:
    """Was gespeichert ist: Zuordnungen je Kategorie, Größe, Integrität, Schlüssel."""
    from pseudonymed.pseudonyme import sicherung as sichmodul

    st = sichmodul.statistik(speicher)
    breite = max(len(m) for m, _w in st.zeilen())
    for merkmal, wert in st.zeilen():
        typer.echo(f"{merkmal:<{breite}}  {wert}")


@datenbank_app.command("zusatzdaten")
def datenbank_zusatzdaten(
    name: Annotated[str, typer.Argument(help="Welche Zusatzdaten: kliniken-at")],
    datei: Annotated[Path | None, typer.Option(
        "--datei", help="Excel-Mappe des Ministeriums statt Abruf über das Netz")] = None,
    speicher: SpeicherOption = None,
) -> None:
    """Daten laden, die PseudonyMed nicht mitliefern darf, direkt aus der Quelle in die eigene
    Datenbank (bisher: Krankenanstalten in Österreich, BMASGPK)."""
    from pseudonymed.erkennung import zusatzdaten as zusatzmodul
    from pseudonymed.erkennung.vokabular import Vokabular

    if name != "kliniken-at":
        raise typer.BadParameter("bekannt ist nur: kliniken-at")
    typer.echo(zusatzmodul.HINWEIS_KLINIKEN_AT)
    try:
        with Vokabular(speicher) as v:
            bericht = zusatzmodul.lade_kliniken_at(v.verbindung, datei=datei)
    except (zusatzmodul.ZusatzFehler, OSError, ValueError) as e:
        abbruch(f"Laden fehlgeschlagen: {e}", ursache=e)
    typer.echo(bericht.beschreibung())


@datenbank_app.command("speicherort")
def datenbank_speicherort(
    datenbank: Annotated[Path | None, typer.Option("--datenbank", help="Neuer Pfad der"
                                                                      " Datenbank.")] = None,
    schluessel: Annotated[Path | None, typer.Option("--schluessel", help="Neuer Pfad der"
                                                                        " Schlüsseldatei.")]
    = None,
    kopieren: Annotated[bool, typer.Option(
        "--kopieren/--nicht-kopieren",
        help="Vorhandene Dateien an den neuen Ort kopieren (Originale werden umbenannt).")]
    = True,
) -> None:
    """Datenbank und Schlüsseldatei an beliebigen Orten ablegen; ohne Angaben: anzeigen."""
    from pseudonymed.pseudonyme import sicherung as sichmodul
    from pseudonymed.pseudonyme.speicher import standard_schluesselpfad

    if datenbank is None and schluessel is None:
        typer.echo(f"Datenbank:     {standardpfad()}")
        typer.echo(f"Schlüsseldatei: {standard_schluesselpfad()}")
        return
    try:
        v = sichmodul.verlege(datenbank or standardpfad(), schluessel or standard_schluesselpfad(),
                              kopieren)
    except (sichmodul.SicherungFehler, OSError) as e:
        abbruch(str(e), ursache=e)
    typer.echo(v.beschreibung())


@datenbank_app.command("exportieren")
def datenbank_exportieren(
    ziel: Annotated[Path, typer.Argument(help="Zielordner oder Dateiname (.zip).")],
    speicher: SpeicherOption = None,
    konfig: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help="konfig.toml einschließen.")] = True,
    vokabular: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                            help="Nutzervokabular einschließen.")] = True,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=umgebung.namen("SICHERUNG_PASSPHRASE"),
        help="Passphrase für das Archiv (sonst Abfrage).")] = None,
    unverschluesselt: Annotated[bool, typer.Option(
        "--unverschluesselt", help="Ohne Verschlüsselung schreiben (nicht empfohlen).")] = False,
) -> None:
    """Datenbank, Schlüssel und wahlweise Konfiguration und Vokabular sichern."""
    from pseudonymed.pseudonyme import pgp as pgpmodul
    from pseudonymed.pseudonyme import sicherung as sichmodul

    if unverschluesselt:
        passphrase = None
    elif passphrase is None:
        if not pgpmodul.verfuegbar():
            typer.echo("Verschlüsselung braucht das Extra 'pgp' oder GnuPG; sonst"
                       " --unverschluesselt.", err=True)
            raise typer.Exit(code=1)
        passphrase = typer.prompt("Passphrase für das Archiv", hide_input=True,
                                  confirmation_prompt=True)
    if passphrase is not None and len(passphrase) < 8:
        abbruch("Passphrase zu kurz (mindestens 8 Zeichen).")
    try:
        pfad = sichmodul.exportiere(ziel, speicher, konfig, vokabular, passphrase)
    except (sichmodul.SicherungFehler, pgpmodul.PGPFehler, OSError) as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"Gesichert: {pfad}" + ("" if passphrase else "  (UNVERSCHLÜSSELT: enthält"
                                                           " Schlüssel und Klardaten)"))


@datenbank_app.command("importieren")
def datenbank_importieren(
    archiv: Annotated[Path, typer.Argument(exists=True, help="Sicherungsarchiv.")],
    speicher: SpeicherOption = None,
    modus: Annotated[str, typer.Option("--modus", help="ersetzen oder zusammenfuehren.")]
    = "ersetzen",
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=umgebung.namen("SICHERUNG_PASSPHRASE"),
        help="Passphrase des Archivs (sonst Abfrage, falls verschlüsselt).")] = None,
    konfig: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help="Konfiguration übernehmen.")] = True,
    vokabular: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                            help="Nutzervokabular übernehmen.")] = True,
    ja: Annotated[bool, typer.Option("--ja", help="Ohne Rückfrage.")] = False,
) -> None:
    """Ein Archiv wiederherstellen (ersetzen) oder in die Datenbank übernehmen."""
    from pseudonymed.pseudonyme import sicherung as sichmodul

    if modus not in sichmodul.MODI:
        abbruch(f"--modus muss eines von {', '.join(sichmodul.MODI)} sein.")
    if passphrase is None and not archiv.read_bytes().startswith(b"PK"):
        passphrase = typer.prompt("Passphrase des Archivs", hide_input=True)
    try:
        m = sichmodul.pruefe(archiv, passphrase)
    except (sichmodul.SicherungFehler, OSError) as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"Archiv: {m.beschreibung()}")
    if not ja and not typer.confirm(
            "Datenbank und Schlüssel ersetzen (die jetzigen werden daneben gesichert)?"
            if modus == "ersetzen" else "Einträge des Archivs übernehmen?"):
        raise typer.Exit(code=1)
    try:
        b = sichmodul.importiere(archiv, speicher, passphrase, modus, konfig, vokabular)
    except (sichmodul.SicherungFehler, OSError) as e:
        abbruch(str(e), ursache=e)
    typer.echo(b.beschreibung())


schluessel_app = typer.Typer(
    help="OpenPGP-Schlüssel für Schlüsselkapseln: Pseudonyme, die ein Empfänger auflöst.",
    no_args_is_help=True,
)
app.add_typer(schluessel_app, name="schluessel")


@schluessel_app.command("zeigen")
def schluessel_zeigen(
    quelle: Annotated[str, typer.Argument(help="Schlüsseldatei (.asc) oder https-Adresse.")],
) -> None:
    """Fingerabdruck und Namen eines öffentlichen Schlüssels anzeigen (zum Abgleich)."""
    from pseudonymed.pseudonyme import pgp as pgpmodul

    try:
        z = pgpmodul.lade_zertifikat(quelle)
    except pgpmodul.PGPFehler as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"Fingerabdruck: {z.gruppen}")
    for n in z.nutzer:
        typer.echo(f"Name:          {n}")
    if z.ablauf:
        typer.echo(f"Gültig bis:    {z.ablauf}")


@schluessel_app.command("erzeugen")
def schluessel_erzeugen(
    name: Annotated[str, typer.Option("--name", prompt="Name", help="Ihr Name.")],
    email: Annotated[str, typer.Option("--email", prompt="E-Mail", help="Ihre E-Mail-Adresse.")],
    ziel: Annotated[Path, typer.Option("--ziel", help="Ordner für die beiden Dateien.")]
    = Path("."),
) -> None:
    """Ein Schlüsselpaar für Empfänger ohne GnuPG erzeugen: öffentlichen Schlüssel
    veröffentlichen, den privaten sicher verwahren (er hat keine Passphrase)."""
    from pseudonymed.pseudonyme import pgp as pgpmodul

    try:
        oeffentlich, geheim = pgpmodul.erzeuge_schluesselpaar(name, email)
    except pgpmodul.PGPFehler as e:
        abbruch(str(e), ursache=e)
    ziel.mkdir(parents=True, exist_ok=True)
    pub, sec = ziel / "pseudonymed_oeffentlich.asc", ziel / "pseudonymed_privat.asc"
    if sec.exists():
        abbruch(f"{sec} existiert schon; nicht überschrieben.")
    pub.write_text(oeffentlich, encoding="utf-8")
    sec.write_text(geheim, encoding="utf-8")
    try:
        os.chmod(sec, 0o600)
    except OSError:
        pass
    z = pgpmodul.lade_zertifikat(oeffentlich)
    typer.echo(f"Öffentlicher Schlüssel: {pub}  (veröffentlichen, z. B. auf der Webseite)")
    typer.echo(f"Privater Schlüssel:     {sec}  (geheim halten, sichern; ohne Passphrase)")
    typer.echo(f"Fingerabdruck:          {z.gruppen}")
