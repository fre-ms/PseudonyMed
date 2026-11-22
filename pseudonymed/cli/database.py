"""Commands over the pseudonym database and the OpenPGP keys."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import environment
from pseudonymed.cli.base import (
    StoreOption,
    abort,
    app,
)
from pseudonymed.pseudonyms.store import (
    default_path,
)

database_app = typer.Typer(
    help="Pseudonym-Datenbank und Schlüssel: Statistik, Sicherung, Wiederherstellung.",
    no_args_is_help=True,
)
app.add_typer(database_app, name="datenbank")


@database_app.command("status")
def database_status(store: StoreOption = None) -> None:
    """Was gespeichert ist: Zuordnungen je Kategorie, Größe, Integrität, Schlüssel."""
    from pseudonymed.pseudonyms import statistics as stats_mod

    st = stats_mod.statistics(store)
    width = max(len(m) for m, _w in st.lines())
    for feature, value in st.lines():
        typer.echo(f"{feature:<{width}}  {value}")


@database_app.command("zusatzdaten")
def load_extra_data(
    name: Annotated[str, typer.Argument(help="Welche Zusatzdaten: kliniken-at")],
    file: Annotated[Path | None, typer.Option(
        "--datei", help="Excel-Mappe des Ministeriums statt Abruf über das Netz")] = None,
    store: StoreOption = None,
) -> None:
    """Daten laden, die PseudonyMed nicht mitliefern darf, direkt aus der Quelle in die eigene
    Datenbank (bisher: Krankenanstalten in Österreich, BMASGPK)."""
    from pseudonymed.detection import extra_data as extra_data_mod
    from pseudonymed.detection.vocabulary import Vocabulary

    if name != "kliniken-at":
        raise typer.BadParameter("bekannt ist nur: kliniken-at")
    typer.echo(extra_data_mod.CLINICS_AT_NOTE)
    try:
        with Vocabulary(store) as v:
            report = extra_data_mod.load_clinics_at(v.connection, file=file)
    except (extra_data_mod.ExtraDataError, OSError, ValueError) as e:
        abort(f"Laden fehlgeschlagen: {e}", cause=e)
    typer.echo(report.description())


@database_app.command("speicherort")
def database_location(
    database: Annotated[Path | None, typer.Option("--datenbank", help="Neuer Pfad der"
                                                                      " Datenbank.")] = None,
    key: Annotated[Path | None, typer.Option("--schluessel", help="Neuer Pfad der"
                                                                        " Schlüsseldatei.")]
    = None,
    copy: Annotated[bool, typer.Option(
        "--kopieren/--nicht-kopieren",
        help="Vorhandene Dateien an den neuen Ort kopieren (Originale werden umbenannt).")]
    = True,
) -> None:
    """Datenbank und Schlüsseldatei an beliebigen Orten ablegen; ohne Angaben: anzeigen."""
    from pseudonymed.pseudonyms import backup as backup_mod
    from pseudonymed.pseudonyms.store import default_key_path

    if database is None and key is None:
        typer.echo(f"Datenbank:     {default_path()}")
        typer.echo(f"Schlüsseldatei: {default_key_path()}")
        return
    try:
        v = backup_mod.relocate(database or default_path(), key or default_key_path(),
                                copy)
    except (backup_mod.BackupError, OSError) as e:
        abort(str(e), cause=e)
    typer.echo(v.description())


@database_app.command("exportieren")
def export_database(
    target: Annotated[Path, typer.Argument(metavar="ziel",
                                           help="Zielordner oder Dateiname (.zip).")],
    store: StoreOption = None,
    config: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help="konfig.toml einschließen.")] = True,
    vocabulary: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                             help="Nutzervokabular einschließen.")] = True,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("SICHERUNG_PASSPHRASE"),
        help="Passphrase für das Archiv (sonst Abfrage).")] = None,
    unencrypted: Annotated[bool, typer.Option(
        "--unverschluesselt", help="Ohne Verschlüsselung schreiben (nicht empfohlen).")] = False,
) -> None:
    """Datenbank, Schlüssel und wahlweise Konfiguration und Vokabular sichern."""
    from pseudonymed.pseudonyms import backup as backup_mod
    from pseudonymed.pseudonyms import pgp as pgp_module

    if unencrypted:
        passphrase = None
    elif passphrase is None:
        if not pgp_module.available():
            typer.echo("Verschlüsselung braucht das Extra 'pgp' oder GnuPG; sonst"
                       " --unverschluesselt.", err=True)
            raise typer.Exit(code=1)
        passphrase = typer.prompt("Passphrase für das Archiv", hide_input=True,
                                  confirmation_prompt=True)
    if passphrase is not None and len(passphrase) < 8:
        abort("Passphrase zu kurz (mindestens 8 Zeichen).")
    try:
        path = backup_mod.export(target, store, config, vocabulary, passphrase)
    except (backup_mod.BackupError, pgp_module.PGPError, OSError) as e:
        abort(str(e), cause=e)
    typer.echo(f"Gesichert: {path}" + ("" if passphrase else "  (UNVERSCHLÜSSELT: enthält"
                                                           " Schlüssel und Klardaten)"))


@database_app.command("importieren")
def import_database(
    archive: Annotated[Path, typer.Argument(metavar="archiv", exists=True,
                                            help="Sicherungsarchiv.")],
    store: StoreOption = None,
    mode: Annotated[str, typer.Option("--modus", help="ersetzen oder zusammenfuehren.")]
    = "ersetzen",
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("SICHERUNG_PASSPHRASE"),
        help="Passphrase des Archivs (sonst Abfrage, falls verschlüsselt).")] = None,
    config: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help="Konfiguration übernehmen.")] = True,
    vocabulary: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                             help="Nutzervokabular übernehmen.")] = True,
    yes: Annotated[bool, typer.Option("--ja", help="Ohne Rückfrage.")] = False,
) -> None:
    """Ein Archiv wiederherstellen (ersetzen) oder in die Datenbank übernehmen."""
    from pseudonymed.pseudonyms import backup as backup_mod

    if mode not in backup_mod.MODES:
        abort(f"--modus muss eines von {', '.join(backup_mod.MODES)} sein.")
    if passphrase is None and not archive.read_bytes().startswith(b"PK"):
        passphrase = typer.prompt("Passphrase des Archivs", hide_input=True)
    try:
        m = backup_mod.check(archive, passphrase)
    except (backup_mod.BackupError, OSError) as e:
        abort(str(e), cause=e)
    typer.echo(f"Archiv: {m.description()}")
    if not yes and not typer.confirm(
            "Datenbank und Schlüssel ersetzen (die jetzigen werden daneben gesichert)?"
            if mode == "ersetzen" else "Einträge des Archivs übernehmen?"):
        raise typer.Exit(code=1)
    try:
        b = backup_mod.import_file(archive, store, passphrase, mode, config, vocabulary)
    except (backup_mod.BackupError, OSError) as e:
        abort(str(e), cause=e)
    typer.echo(b.description())


keys_app = typer.Typer(
    help="OpenPGP-Schlüssel für Schlüsselkapseln: Pseudonyme, die ein Empfänger auflöst.",
    no_args_is_help=True,
)
app.add_typer(keys_app, name="schluessel")


@keys_app.command("zeigen")
def show_key(
    source: Annotated[str, typer.Argument(metavar="quelle",
                                          help="Schlüsseldatei (.asc) oder https-Adresse.")],
) -> None:
    """Fingerabdruck und Namen eines öffentlichen Schlüssels anzeigen (zum Abgleich)."""
    from pseudonymed.pseudonyms import pgp as pgp_module

    try:
        z = pgp_module.load_certificate(source)
    except pgp_module.PGPError as e:
        abort(str(e), cause=e)
    typer.echo(f"Fingerabdruck: {z.groups}")
    for n in z.user_ids:
        typer.echo(f"Name:          {n}")
    if z.expiry:
        typer.echo(f"Gültig bis:    {z.expiry}")


@keys_app.command("erzeugen")
def create_key(
    name: Annotated[str, typer.Option("--name", prompt="Name", help="Ihr Name.")],
    email: Annotated[str, typer.Option("--email", prompt="E-Mail", help="Ihre E-Mail-Adresse.")],
    target: Annotated[Path, typer.Option("--ziel", help="Ordner für die beiden Dateien.")]
    = Path("."),
) -> None:
    """Ein Schlüsselpaar für Empfänger ohne GnuPG erzeugen: öffentlichen Schlüssel
    veröffentlichen, den privaten sicher verwahren (er hat keine Passphrase)."""
    from pseudonymed.pseudonyms import pgp as pgp_module

    try:
        public_key, secret_key = pgp_module.create_key_pair(name, email)
    except pgp_module.PGPError as e:
        abort(str(e), cause=e)
    target.mkdir(parents=True, exist_ok=True)
    pub, sec = target / "pseudonymed_oeffentlich.asc", target / "pseudonymed_privat.asc"
    if sec.exists():
        abort(f"{sec} existiert schon; nicht überschrieben.")
    pub.write_text(public_key, encoding="utf-8")
    sec.write_text(secret_key, encoding="utf-8")
    try:
        os.chmod(sec, 0o600)
    except OSError:
        pass
    z = pgp_module.load_certificate(public_key)
    typer.echo(f"Öffentlicher Schlüssel: {pub}  (veröffentlichen, z. B. auf der Webseite)")
    typer.echo(f"Privater Schlüssel:     {sec}  (geheim halten, sichern; ohne Passphrase)")
    typer.echo(f"Fingerabdruck:          {z.groups}")
