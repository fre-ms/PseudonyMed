"""Commands over the address books: CardDAV set-up, local vCards, search, export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import environment
from pseudonymed.cli.base import (
    ConfigOption,
    NumberOption,
    StoreOption,
    abort,
    app,
    connection,
    open_store,
    patient_line,
    search_contacts,
    selection,
)
from pseudonymed.config import (
    PASSWORD_ENV_VAR,
    CardDAVConfig,
    load_settings,
    save_config,
    save_password,
)
from pseudonymed.core.model import Patient
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError

contacts_app = typer.Typer(
    help="Patientensuche im CardDAV-Adressbuch (Nextcloud).", no_args_is_help=True
)
app.add_typer(contacts_app, name="kontakte")


@contacts_app.command("einrichten")
def set_up(
    url: Annotated[str, typer.Option("--url", prompt="Nextcloud-Adresse (https://…)",
                                     help="Basisadresse der Nextcloud.")],
    user: Annotated[str, typer.Option("--benutzer", prompt=True, help="Nextcloud-Benutzer.")],
    address_book: Annotated[str, typer.Option(
        "--adressbuch", help="Adressbuch (URI oder Name), leer = einziges.")] = "",
    config: ConfigOption = None,
    keyring_backend: Annotated[bool, typer.Option(
        "--schluesselbund/--kein-schluesselbund",
        help="App-Passwort im Schlüsselbund des Betriebssystems ablegen.")] = True,
    password_in_file: Annotated[bool, typer.Option(
        "--passwort-datei", help="Ohne Schlüsselbund: App-Passwort in der Konfigurationsdatei"
                                 " (nur für den Benutzer lesbar) ablegen.")] = False,
) -> None:
    """Zugang einrichten: Adresse, Benutzer, App-Passwort (Nextcloud → Sicherheit)."""
    password = (environment.variable("CARDDAV_PASSWORT")
                or typer.prompt("App-Passwort", hide_input=True))
    k = CardDAVConfig(url=url.strip(), user=user.strip(), address_book=address_book.strip())
    try:
        client = CardDAVClient(k.url, k.user, password)
        books = client.address_books()
        if k.address_book:
            client.address_book(k.address_book)
    except CardDAVError as e:
        abort(f"Verbindung fehlgeschlagen: {e}", cause=e)
    if keyring_backend and save_password(k, password):
        typer.echo(f"App-Passwort im Schlüsselbund abgelegt ({k.code}).")
    elif password_in_file:
        k.password_in_file = password
        typer.echo("App-Passwort in der Konfigurationsdatei abgelegt (Rechte 0600).")
    else:
        typer.echo(
            "Kein Schlüsselbund verfügbar (Extra 'carddav' installieren) – Passwort stattdessen"
            f" in {PASSWORD_ENV_VAR} setzen oder --passwort-datei wählen.", err=True,
        )
    path = save_config(k, config)
    typer.echo(f"Konfiguration gespeichert: {path}")
    typer.echo("Adressbücher: " + ", ".join(f"{b.name} ({b.uri})" for b in books))


@contacts_app.command("lokal")
def local_contacts(
    path: Annotated[Path | None, typer.Argument(metavar="pfad", 
        help="vCard-Datei oder Ordner mit vCard-Dateien; ohne Angabe: anzeigen.")] = None,
    remove: Annotated[bool, typer.Option("--entfernen",
                                         help="Lokales Adressbuch austragen.")] = False,
    config: ConfigOption = None,
) -> None:
    """Ein lokales Adressbuch (vCard-Datei oder Ordner) einrichten oder anzeigen."""
    from pseudonymed.config import save_settings
    from pseudonymed.sources.address_book import local_files

    e = load_settings(config)
    if remove:
        e.local_address_book = ""
        save_settings(e, config)
        typer.echo("Lokales Adressbuch ausgetragen.")
        return
    if path is None:
        typer.echo(e.local_address_book or "Kein lokales Adressbuch eingerichtet.")
        return
    files = local_files(path)
    if not files:
        abort(f"{path}: keine vCard-Datei(en) gefunden.")
    e.local_address_book = str(path.expanduser().resolve())
    save_settings(e, config)
    typer.echo(f"Lokales Adressbuch: {e.local_address_book} ({len(files)} Datei(en)).")


@contacts_app.command("adressbuecher")
def address_books(config: ConfigOption = None) -> None:
    """Adressbücher des Benutzers auflisten."""
    client, _k = connection(config)
    try:
        books = client.address_books()
    except CardDAVError as e:
        abort(str(e), cause=e)
    for b in books:
        typer.echo(f"{b.uri:30}  {b.name}")


@contacts_app.command("suchen")
def run_search(
    term: Annotated[str, typer.Argument(metavar="begriff",
                                        help="Name, Namensteil, Geburtsjahr oder -datum.")],
    config: ConfigOption = None,
    store: StoreOption = None,
) -> None:
    """Kontakte suchen; zeigt vergebene Pseudonym-Buchstaben an."""
    patients = search_contacts(term, config)
    if not patients:
        typer.echo(f"Kein Kontakt passt zu {term!r}.")
        raise typer.Exit(code=1)
    with open_store(store) as sp:
        for i, p in enumerate(patients, 1):
            info = sp.patient_info(p.key())
            typer.echo(patient_line(i, p, info["kennung"] if info else None))


def _patient_text(p: Patient) -> str:
    fields = [
        ("Name", f"{p.surname}, {p.first_name}"),
        ("Anrede/Geschlecht", {"m": "Herr", "w": "Frau"}.get(p.sex, "unbekannt")),
        ("Titel", p.title),
        ("Geburtsname", p.birth_name),
        ("Geburtsdatum", p.birth_date.strftime("%d.%m.%Y") if p.birth_date else ""),
        ("Anschrift", ", ".join(t for t in (p.street, f"{p.postcode} {p.place}".strip()) if t)),
        ("Telefon", "; ".join(p.phones)),
        ("E-Mail", "; ".join(p.emails)),
        ("Versichertennummer", p.insurance_number),
        ("Patienten-ID", p.patient_id),
    ]
    lines = [f"{name + ':':20} {value}" for name, value in fields if value]
    for h in p.notes:
        lines.append(f"{'Notiz:':20} {h}")
    lines.append(
        f"{'Pseudonymschlüssel:':20} {p.key()}"
    )
    return "\n".join(lines)


@contacts_app.command("zeigen")
def show_contact(
    term: Annotated[str, typer.Argument(metavar="begriff", help="Suchbegriff.")],
    num: NumberOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Als Patientendatei (JSON) ausgeben.")]
    = False,
    config: ConfigOption = None,
) -> None:
    """Abgeleitete Identifikatoren eines Kontakts anzeigen (nur lokal)."""
    p = selection(search_contacts(term, config), term, num)
    if as_json:
        typer.echo(json.dumps(p.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(_patient_text(p))


@contacts_app.command()
def export(
    term: Annotated[str, typer.Argument(metavar="begriff", help="Suchbegriff.")],
    output: Annotated[Path, typer.Option("--ausgabe", "-o", help="Zieldatei (JSON).")],
    num: NumberOption = None,
    overwrite: Annotated[bool, typer.Option("--ueberschreiben",
                                            help="Vorhandene Datei ersetzen.")] = False,
    config: ConfigOption = None,
) -> None:
    """Kontakt als Patientendatei speichern, um Angehörige, Zuweiser und Begriffe zu ergänzen."""
    p = selection(search_contacts(term, config), term, num)
    if output.exists() and not overwrite:
        abort(f"{output} existiert bereits (--ueberschreiben).")
    data = p.as_dict()
    data.setdefault("angehoerige", [])
    data.setdefault("zuweiser", [])
    data.setdefault("aktenzeichen", [])
    data.setdefault("begriffe", [])
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Patientendatei geschrieben: {output}")
