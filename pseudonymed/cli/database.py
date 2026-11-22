"""Commands over the pseudonym database and the OpenPGP keys."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import environment
from pseudonymed.cli.base import (
    LANGUAGE,
    StoreOption,
    abort,
    app,
    error_message,
    shown,
    w,
)
from pseudonymed.pseudonyms.store import (
    default_path,
)

database_app = typer.Typer(
    help=w("Pseudonym-Datenbank und Schlüssel: Statistik, Sicherung, Wiederherstellung.",
           "Pseudonym database and key: statistics, backup, restore."),
    no_args_is_help=True,
)
app.add_typer(database_app, name="datenbank")


@database_app.command(
    "status",
    help=w("Was gespeichert ist: Zuordnungen je Kategorie, Größe, Integrität, Schlüssel.",
           "What is stored: mappings per category, size, integrity, key."))
def database_status(store: StoreOption = None) -> None:
    """Print the statistics of the pseudonym store as an aligned label/value table."""
    from pseudonymed.pseudonyms import statistics as stats_mod

    st = stats_mod.statistics(store)
    lines = [(shown(feature), shown(value)) for feature, value in st.lines()]
    width = max(len(m) for m, _w in lines)
    for feature, value in lines:
        typer.echo(f"{feature:<{width}}  {value}")


@database_app.command(
    "zusatzdaten",
    help=w("Daten laden, die PseudonyMed nicht mitliefern darf, direkt aus der Quelle in die"
           " eigene\nDatenbank (bisher: Krankenanstalten in Österreich, BMASGPK).",
           "Load data that PseudonyMed may not ship, straight from the source into your own"
           " database (so far: hospitals in Austria, BMASGPK)."))
def load_extra_data(
    name: Annotated[str, typer.Argument(help=w("Welche Zusatzdaten: kliniken-at",
                                               "Which extra data: kliniken-at"))],
    file: Annotated[Path | None, typer.Option(
        "--datei", help=w("Excel-Mappe des Ministeriums statt Abruf über das Netz",
                          "The ministry's Excel workbook instead of fetching it over the"
                          " network"))] = None,
    store: StoreOption = None,
) -> None:
    """Load a supplementary data set (so far only the Austrian hospitals) into the lexicon."""
    from pseudonymed.detection import extra_data as extra_data_mod
    from pseudonymed.detection.vocabulary import Vocabulary

    if name != "kliniken-at":
        raise typer.BadParameter(w("bekannt ist nur: kliniken-at",
                                   "the only one known is kliniken-at"))
    typer.echo(w(extra_data_mod.CLINICS_AT_NOTE,
                 "Source: Federal Ministry of Labour, Social Affairs, Health, Care and Consumer"
                 " Protection (BMASGPK), list of hospitals in Austria. The ministry permits"
                 " reprinting with attribution except for commercial purposes. PseudonyMed loads"
                 " the list into your local database only and does not pass it on."))
    try:
        with Vocabulary(store) as v:
            report = extra_data_mod.load_clinics_at(v.connection, file=file)
    except (extra_data_mod.ExtraDataError, OSError, ValueError) as e:
        reason = error_message(e)
        abort(w(f"Laden fehlgeschlagen: {reason}", f"Loading failed: {reason}"), cause=e)
    typer.echo(report.description(LANGUAGE))


@database_app.command(
    "speicherort",
    help=w("Datenbank und Schlüsseldatei an beliebigen Orten ablegen; ohne Angaben: anzeigen.",
           "Keep the database and the key file in any location; without options: show the"
           " locations."))
def database_location(
    database: Annotated[Path | None, typer.Option(
        "--datenbank", help=w("Neuer Pfad der Datenbank.",
                              "New path of the database."))] = None,
    key: Annotated[Path | None, typer.Option(
        "--schluessel", help=w("Neuer Pfad der Schlüsseldatei.",
                               "New path of the key file."))] = None,
    copy: Annotated[bool, typer.Option(
        "--kopieren/--nicht-kopieren",
        help=w("Vorhandene Dateien an den neuen Ort kopieren (Originale werden umbenannt).",
               "Copy the existing files to the new location (the originals are renamed)."))]
    = True,
) -> None:
    """Show the locations of database and key file, or move them (copying by default)."""
    from pseudonymed.pseudonyms import backup as backup_mod
    from pseudonymed.pseudonyms.store import default_key_path

    if database is None and key is None:
        typer.echo(w(f"Datenbank:     {default_path()}", f"Database: {default_path()}"))
        typer.echo(w(f"Schlüsseldatei: {default_key_path()}", f"Key file: {default_key_path()}"))
        return
    try:
        v = backup_mod.relocate(database or default_path(), key or default_key_path(),
                                copy)
    except (backup_mod.BackupError, OSError) as e:
        abort(error_message(e), cause=e)
    typer.echo(v.description(LANGUAGE))


@database_app.command(
    "exportieren",
    help=w("Datenbank, Schlüssel und wahlweise Konfiguration und Vokabular sichern.",
           "Back up the database, the key and optionally the configuration and the"
           " vocabulary."))
def export_database(
    target: Annotated[Path, typer.Argument(
        metavar=w("ziel", "target"),
        help=w("Zielordner oder Dateiname (.zip).", "Target folder or file name (.zip)."))],
    store: StoreOption = None,
    config: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help=w("konfig.toml einschließen.",
                                                "Include konfig.toml."))] = True,
    vocabulary: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                             help=w("Nutzervokabular einschließen.",
                                                    "Include the user vocabulary."))] = True,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("SICHERUNG_PASSPHRASE"),
        help=w("Passphrase für das Archiv (sonst Abfrage).",
               "Passphrase for the archive (otherwise asked for)."))] = None,
    unencrypted: Annotated[bool, typer.Option(
        "--unverschluesselt", help=w("Ohne Verschlüsselung schreiben (nicht empfohlen).",
                                     "Write without encryption (not recommended)."))] = False,
) -> None:
    """Write a backup archive, encrypted with a passphrase unless --unverschluesselt."""
    from pseudonymed.pseudonyms import backup as backup_mod
    from pseudonymed.pseudonyms import pgp as pgp_module

    if unencrypted:
        passphrase = None
    elif passphrase is None:
        if not pgp_module.available():
            typer.echo(w("Verschlüsselung braucht das Extra 'pgp' oder GnuPG; sonst"
                         " --unverschluesselt.",
                         "Encryption needs the extra 'pgp' or GnuPG; otherwise"
                         " --unverschluesselt."), err=True)
            raise typer.Exit(code=1)
        passphrase = typer.prompt(w("Passphrase für das Archiv", "Passphrase for the archive"),
                                  hide_input=True, confirmation_prompt=True)
    if passphrase is not None and len(passphrase) < 8:
        abort(w("Passphrase zu kurz (mindestens 8 Zeichen).",
                "Passphrase too short (at least 8 characters)."))
    try:
        path = backup_mod.export(target, store, config, vocabulary, passphrase)
    except (backup_mod.BackupError, pgp_module.PGPError, OSError) as e:
        abort(error_message(e), cause=e)
    typer.echo(w(f"Gesichert: {path}", f"Backed up: {path}")
               + ("" if passphrase else w("  (UNVERSCHLÜSSELT: enthält Schlüssel und Klardaten)",
                                          "  (UNENCRYPTED: holds the key and clear data)")))


@database_app.command(
    "importieren",
    help=w("Ein Archiv wiederherstellen (ersetzen) oder in die Datenbank übernehmen.",
           "Restore an archive (replace) or merge it into the database."))
def import_database(
    archive: Annotated[Path, typer.Argument(metavar=w("archiv", "archive"), exists=True,
                                            help=w("Sicherungsarchiv.", "Backup archive."))],
    store: StoreOption = None,
    mode: Annotated[str, typer.Option("--modus",
                                      help=w("ersetzen oder zusammenfuehren.",
                                             "ersetzen (replace) or zusammenfuehren (merge)."))]
    = "ersetzen",
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("SICHERUNG_PASSPHRASE"),
        help=w("Passphrase des Archivs (sonst Abfrage, falls verschlüsselt).",
               "Passphrase of the archive (otherwise asked for if it is encrypted)."))] = None,
    config: Annotated[bool, typer.Option("--konfig/--ohne-konfig",
                                         help=w("Konfiguration übernehmen.",
                                                "Take over the configuration."))] = True,
    vocabulary: Annotated[bool, typer.Option("--vokabular/--ohne-vokabular",
                                             help=w("Nutzervokabular übernehmen.",
                                                    "Take over the user vocabulary."))] = True,
    yes: Annotated[bool, typer.Option("--ja", help=w("Ohne Rückfrage.",
                                                     "Without asking for confirmation."))]
    = False,
) -> None:
    """Check a backup archive, ask for confirmation and replace or merge the local store."""
    from pseudonymed.pseudonyms import backup as backup_mod

    if mode not in backup_mod.MODES:
        abort(w(f"--modus muss eines von {', '.join(backup_mod.MODES)} sein.",
                f"--modus must be one of {', '.join(backup_mod.MODES)}."))
    if passphrase is None and not archive.read_bytes().startswith(b"PK"):
        passphrase = typer.prompt(w("Passphrase des Archivs", "Passphrase of the archive"),
                                  hide_input=True)
    try:
        m = backup_mod.check(archive, passphrase)
    except (backup_mod.BackupError, OSError) as e:
        abort(error_message(e), cause=e)
    description = m.description(LANGUAGE)
    typer.echo(w(f"Archiv: {description}", f"Archive: {description}"))
    if not yes and not typer.confirm(
            w("Datenbank und Schlüssel ersetzen (die jetzigen werden daneben gesichert)?",
              "Replace database and key (the current ones are backed up alongside)?")
            if mode == "ersetzen" else w("Einträge des Archivs übernehmen?",
                                         "Add the entries of the archive?")):
        raise typer.Exit(code=1)
    try:
        b = backup_mod.import_file(archive, store, passphrase, mode, config, vocabulary)
    except (backup_mod.BackupError, OSError) as e:
        abort(error_message(e), cause=e)
    typer.echo(b.description(LANGUAGE))


keys_app = typer.Typer(
    help=w("OpenPGP-Schlüssel für Schlüsselkapseln: Pseudonyme, die ein Empfänger auflöst.",
           "OpenPGP keys for key capsules: pseudonyms that a recipient resolves."),
    no_args_is_help=True,
)
app.add_typer(keys_app, name="schluessel")


@keys_app.command(
    "zeigen",
    help=w("Fingerabdruck und Namen eines öffentlichen Schlüssels anzeigen (zum Abgleich).",
           "Show the fingerprint and the names of a public key (for comparison)."))
def show_key(
    source: Annotated[str, typer.Argument(
        metavar=w("quelle", "source"),
        help=w("Schlüsseldatei (.asc) oder https-Adresse.", "Key file (.asc) or https address."))],
) -> None:
    """Print fingerprint, user IDs and expiry of an OpenPGP certificate."""
    from pseudonymed.pseudonyms import pgp as pgp_module

    try:
        z = pgp_module.load_certificate(source)
    except pgp_module.PGPError as e:
        abort(error_message(e), cause=e)
    typer.echo(w(f"Fingerabdruck: {z.groups}", f"Fingerprint:   {z.groups}"))
    for n in z.user_ids:
        typer.echo(f"Name:          {n}")
    if z.expiry:
        typer.echo(w(f"Gültig bis:    {z.expiry}", f"Valid until:   {z.expiry}"))


@keys_app.command(
    "erzeugen",
    help=w("Ein Schlüsselpaar für Empfänger ohne GnuPG erzeugen: öffentlichen Schlüssel\n"
           "veröffentlichen, den privaten sicher verwahren (er hat keine Passphrase).",
           "Create a key pair for recipients without GnuPG: publish the public key, keep the"
           " private one safe (it has no passphrase)."))
def create_key(
    name: Annotated[str, typer.Option("--name", prompt="Name",
                                      help=w("Ihr Name.", "Your name."))],
    email: Annotated[str, typer.Option("--email", prompt=w("E-Mail", "E-mail"),
                                       help=w("Ihre E-Mail-Adresse.", "Your e-mail address."))],
    target: Annotated[Path, typer.Option("--ziel", help=w("Ordner für die beiden Dateien.",
                                                          "Folder for the two files."))]
    = Path("."),
) -> None:
    """Write a new OpenPGP key pair (public and private, no passphrase) into a folder."""
    from pseudonymed.pseudonyms import pgp as pgp_module

    try:
        public_key, secret_key = pgp_module.create_key_pair(name, email)
    except pgp_module.PGPError as e:
        abort(error_message(e), cause=e)
    target.mkdir(parents=True, exist_ok=True)
    pub, sec = target / "pseudonymed_oeffentlich.asc", target / "pseudonymed_privat.asc"
    if sec.exists():
        abort(w(f"{sec} existiert schon; nicht überschrieben.",
                f"{sec} already exists; not overwritten."))
    pub.write_text(public_key, encoding="utf-8")
    sec.write_text(secret_key, encoding="utf-8")
    try:
        os.chmod(sec, 0o600)
    except OSError:
        pass
    z = pgp_module.load_certificate(public_key)
    typer.echo(w(f"Öffentlicher Schlüssel: {pub}  (veröffentlichen, z. B. auf der Webseite)",
                 f"Public key:  {pub}  (publish it, e.g. on your website)"))
    typer.echo(w(f"Privater Schlüssel:     {sec}  (geheim halten, sichern; ohne Passphrase)",
                 f"Private key: {sec}  (keep it secret, back it up; no passphrase)"))
    typer.echo(w(f"Fingerabdruck:          {z.groups}", f"Fingerprint: {z.groups}"))
