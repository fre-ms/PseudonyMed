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
    add_group,
    command,
    connection,
    error_message,
    flags,
    open_store,
    patient_line,
    search_contacts,
    selection,
    shown,
    w,
)
from pseudonymed.config import (
    PASSWORD_ENV_VAR,
    CardDAVConfig,
    Settings,
    load_settings,
    save_config,
    save_password,
)
from pseudonymed.core.model import Patient
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError

contacts_app = typer.Typer(
    help=w("Patientensuche in den Adressbüchern (vCard-Datei, Kontakte dieses Mac, Nextcloud).",
           "Patient search in the address books (vCard file, contacts on this Mac, Nextcloud)."),
    no_args_is_help=True,
)
add_group(contacts_app, "kontakte", "contacts")


@command(contacts_app, "einrichten", "setup", help=w(
    "Zugang einrichten: Adresse, Benutzer, App-Passwort (Nextcloud → Sicherheit).",
    "Set up access: address, user, app password (Nextcloud → Security)."))
def set_up(
    url: Annotated[str, typer.Option("--url", prompt=w("Nextcloud-Adresse (https://…)",
                                                       "Nextcloud address (https://…)"),
                                     help=w("Basisadresse der Nextcloud.",
                                            "Base address of the Nextcloud server."))],
    user: Annotated[str, typer.Option(*flags("--benutzer"), prompt=w("Benutzer", "User"),
                                      help=w("Nextcloud-Benutzer.", "Nextcloud user."))],
    address_book: Annotated[str, typer.Option(
        *flags("--adressbuch"), help=w("Adressbuch (URI oder Name), leer = einziges.",
                                       "Address book (URI or name), empty = the only one."))] = "",
    config: ConfigOption = None,
    keyring_backend: Annotated[bool, typer.Option(
        *flags("--schluesselbund/--kein-schluesselbund"),
        help=w("App-Passwort im Schlüsselbund des Betriebssystems ablegen.",
               "Keep the app password in the operating system's keyring."))] = True,
    password_in_file: Annotated[bool, typer.Option(
        *flags("--passwort-datei"),
        help=w("Ohne Schlüsselbund: App-Passwort in der Konfigurationsdatei"
               " (nur für den Benutzer lesbar) ablegen.",
               "Without a keyring: keep the app password in the"
               " configuration file (readable by the user only)."))] = False,
) -> None:
    """Check the CardDAV access, store the app password and save the configuration."""
    password = (environment.variable("CARDDAV_PASSWORD")
                or typer.prompt(w("App-Passwort", "App password"), hide_input=True))
    k = CardDAVConfig(url=url.strip(), user=user.strip(), address_book=address_book.strip())
    try:
        client = CardDAVClient(k.url, k.user, password)
        books = client.address_books()
        if k.address_book:
            client.address_book(k.address_book)
    except CardDAVError as e:
        reason = error_message(e)
        abort(w(f"Verbindung fehlgeschlagen: {reason}", f"Connection failed: {reason}"),
              cause=e)
    if keyring_backend and save_password(k, password):
        typer.echo(w(f"App-Passwort im Schlüsselbund abgelegt ({k.code}).",
                     f"App password kept in the keyring ({k.code})."))
    elif password_in_file:
        k.password_in_file = password
        typer.echo(w("App-Passwort in der Konfigurationsdatei abgelegt (Rechte 0600).",
                     "App password kept in the configuration file (permissions 0600)."))
    else:
        typer.echo(w(
            "Kein Schlüsselbund verfügbar (Extra 'carddav' installieren) – Passwort stattdessen"
            f" in {PASSWORD_ENV_VAR} setzen oder --passwort-datei wählen.",
            "No keyring available (install the extra 'carddav') – set the password in"
            f" {PASSWORD_ENV_VAR} instead or choose --password-file."), err=True,
        )
    path = save_config(k, config)
    typer.echo(w(f"Konfiguration gespeichert: {path}", f"Configuration saved: {path}"))
    typer.echo(w("Adressbücher: ", "Address books: ")
               + ", ".join(f"{b.name} ({b.uri})" for b in books))


@command(contacts_app, "lokal", "local", help=w(
    "Ein lokales Adressbuch (vCard-Datei oder Ordner) einrichten oder anzeigen.",
    "Set up or show a local address book (vCard file or folder)."))
def local_contacts(
    path: Annotated[Path | None, typer.Argument(
        metavar=w("pfad", "path"),
        help=w("vCard-Datei oder Ordner mit vCard-Dateien; ohne Angabe: anzeigen.",
               "vCard file or folder of vCard files; without one: show the current one."))]
    = None,
    remove: Annotated[bool, typer.Option(*flags("--entfernen"),
                                         help=w("Lokales Adressbuch austragen.",
                                                "Remove the local address book."))] = False,
    config: ConfigOption = None,
) -> None:
    """Save, remove or print the local vCard address book in the settings."""
    from pseudonymed.config import save_settings
    from pseudonymed.sources.address_book import local_files

    e = load_settings(config)
    if remove:
        e.local_address_book = ""
        save_settings(e, config)
        typer.echo(w("Lokales Adressbuch ausgetragen.", "Local address book removed."))
        return
    if path is None:
        typer.echo(e.local_address_book or w("Kein lokales Adressbuch eingerichtet.",
                                             "No local address book set up."))
        return
    files = local_files(path)
    if not files:
        abort(w(f"{path}: keine vCard-Datei(en) gefunden.", f"{path}: no vCard file(s) found."))
    e.local_address_book = str(path.expanduser().resolve())
    save_settings(e, config)
    typer.echo(w(f"Lokales Adressbuch: {e.local_address_book} ({len(files)} Datei(en)).",
                 f"Local address book: {e.local_address_book} ({len(files)} file(s))."))


@command(contacts_app, "mac", "mac", help=w(
    "Die Kontakte-App dieses Mac durchsuchen (alle oder gewählte Adressbücher) oder anzeigen.",
    "Search the Contacts app of this Mac (every or chosen address books), or show it."))
def mac_contacts_command(
    on: Annotated[bool | None, typer.Option(
        *flags("--an/--aus"), help=w("Kontakte dieses Mac durchsuchen oder nicht.",
                                     "Search the contacts on this Mac, or not."))] = None,
    books: Annotated[list[str] | None, typer.Option(
        *flags("--adressbuch"),
        help=w("Nur dieses Adressbuch der Kontakte-App (Name oder Kennung, mehrfach möglich).",
               "Only this address book of the Contacts app (name or identifier, may be"
               " repeated)."))] = None,
    every: Annotated[bool, typer.Option(*flags("--alle"),
                                        help=w("Alle Adressbücher der Kontakte-App.",
                                               "Every address book of the Contacts app."))]
    = False,
    config: ConfigOption = None,
) -> None:
    """Switch the Mac's contacts on (asking macOS for access) or off, choose their address
    books, or show the state and the address books there are."""
    from pseudonymed.config import save_settings
    from pseudonymed.sources import mac_contacts

    if not mac_contacts.supported():
        abort(w("Die Kontakte-App gibt es nur auf dem Mac.", "The Contacts app is on a Mac only."))
    e = load_settings(config)
    if on is False:
        e.mac_contacts = False
        save_settings(e, config)
        typer.echo(w("Kontakte dieses Mac werden nicht mehr durchsucht.",
                     "The contacts on this Mac are no longer searched."))
        return
    if on or books or every:
        _switch_mac_on(e, books or [], every)
        save_settings(e, config)
    _print_mac_contacts(e)


def _switch_mac_on(e: Settings, books: list[str], every: bool) -> None:
    """Ask macOS for access (the first time) and switch the Mac's contacts on in ``e``, with
    the address books named, or every one; stops with the reason when that cannot be."""
    from pseudonymed.sources import mac_contacts

    state = mac_contacts.ask()
    if state != mac_contacts.GRANTED:
        abort(w(f"Kein Zugriff auf die Kontakte dieses Mac: {mac_contacts.access_text(state)}",
                f"No access to the contacts on this Mac: {mac_contacts.access_text(state).en}"))
    try:
        chosen = mac_contacts.choose(mac_contacts.address_books(), books)
    except mac_contacts.MacContactsError as ex:
        abort(error_message(ex), cause=ex)
    e.mac_contacts = True
    if books or every:
        e.mac_address_books = [b.identifier for b in chosen]


def _print_mac_contacts(e: Settings) -> None:
    """The state of the Mac's contacts and, once they may be read, their address books with
    the chosen ones marked."""
    from pseudonymed.sources import mac_contacts

    state = mac_contacts.access()
    use = w("an", "on") if e.mac_contacts else w("aus", "off")
    typer.echo(w(f"Kontakte dieses Mac: {use}; Zugriff {mac_contacts.access_text(state)}",
                 f"Contacts on this Mac: {use}; access {mac_contacts.access_text(state).en}"))
    if state != mac_contacts.GRANTED:
        return
    try:
        books = mac_contacts.address_books()
    except mac_contacts.MacContactsError as ex:
        abort(error_message(ex), cause=ex)
    for b in books:
        mark = "x" if not e.mac_address_books or b.identifier in e.mac_address_books else " "
        typer.echo(f"  [{mark}] {shown(b.shown):40}  {b.identifier}")


@command(contacts_app, "adressbuecher", "address-books",
         help=w("Adressbücher des Benutzers auflisten.",
                "List the user's address books."))
def address_books(config: ConfigOption = None) -> None:
    """Print URI and name of every address book on the CardDAV server."""
    client, _k = connection(config)
    try:
        books = client.address_books()
    except CardDAVError as e:
        abort(error_message(e), cause=e)
    for b in books:
        typer.echo(f"{b.uri:30}  {b.name}")


@command(contacts_app, "suchen", "search", help=w(
    "Kontakte suchen; zeigt vergebene Pseudonym-Buchstaben an.",
    "Search contacts; shows the pseudonym codes already assigned."))
def run_search(
    term: Annotated[str, typer.Argument(
        metavar=w("begriff", "term"),
        help=w("Name, Namensteil, Geburtsjahr oder -datum.",
               "Name, part of a name, year or date of birth."))],
    config: ConfigOption = None,
    store: StoreOption = None,
) -> None:
    """Print the matching contacts with the codes the pseudonym store holds for them."""
    patients = search_contacts(term, config)
    if not patients:
        typer.echo(w(f"Kein Kontakt passt zu {term!r}.", f"No contact matches {term!r}."))
        raise typer.Exit(code=1)
    with open_store(store) as sp:
        for i, p in enumerate(patients, 1):
            info = sp.patient_info(p.key())
            typer.echo(patient_line(i, p, info["code"] if info else None))


def _patient_text(p: Patient) -> str:
    """The derived identifiers of a contact as labelled lines."""
    sexes = {"m": w("Herr", "Mr"), "f": w("Frau", "Ms")}
    fields = [
        (w("Name", "Name"), f"{p.surname}, {p.first_name}"),
        (w("Anrede/Geschlecht", "Salutation/sex"), sexes.get(p.sex, w("unbekannt", "unknown"))),
        (w("Titel", "Title"), p.title),
        (w("Geburtsname", "Birth name"), p.birth_name),
        (w("Geburtsdatum", "Birth date"),
         p.birth_date.strftime(w("%d.%m.%Y", "%Y-%m-%d")) if p.birth_date else ""),
        (w("Anschrift", "Address"),
         ", ".join(t for t in (p.street, f"{p.postcode} {p.place}".strip()) if t)),
        (w("Telefon", "Phone"), "; ".join(p.phones)),
        (w("E-Mail", "E-mail"), "; ".join(p.emails)),
        (w("Versichertennummer", "Insurance number"), p.insurance_number),
        (w("Patienten-ID", "Patient ID"), p.patient_id),
    ]
    lines = [f"{name + ':':20} {value}" for name, value in fields if value]
    for h in p.notes:
        lines.append(f"{w('Notiz:', 'Note:'):20} {shown(h)}")
    lines.append(
        f"{w('Pseudonymschlüssel:', 'Pseudonym key:'):20} {p.key()}"
    )
    return "\n".join(lines)


@command(contacts_app, "zeigen", "show", help=w(
    "Abgeleitete Identifikatoren eines Kontakts anzeigen (nur lokal).",
    "Show the identifiers derived from a contact (locally only)."))
def show_contact(
    term: Annotated[str, typer.Argument(metavar=w("begriff", "term"),
                                        help=w("Suchbegriff.", "Search term."))],
    num: NumberOption = None,
    as_json: Annotated[bool, typer.Option("--json",
                                          help=w("Als Patientendatei (JSON) ausgeben.",
                                                 "Print as a patient file (JSON)."))]
    = False,
    config: ConfigOption = None,
) -> None:
    """Print one contact's identifiers as text or as a patient file in JSON."""
    p = selection(search_contacts(term, config), term, num)
    if as_json:
        typer.echo(json.dumps(p.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(_patient_text(p))


@command(contacts_app, "export", "export", help=w(
    "Kontakt als Patientendatei speichern, um Angehörige, Zuweiser und Begriffe zu ergänzen.",
    "Save a contact as a patient file, to add relatives, referrers and terms to it."))
def export(
    term: Annotated[str, typer.Argument(metavar=w("begriff", "term"),
                                        help=w("Suchbegriff.", "Search term."))],
    output: Annotated[Path, typer.Option(*flags("--ausgabe", "-o"),
                                         help=w("Zieldatei (JSON).", "Target file (JSON)."))],
    num: NumberOption = None,
    overwrite: Annotated[bool, typer.Option(*flags("--ueberschreiben"),
                                            help=w("Vorhandene Datei ersetzen.",
                                                   "Replace an existing file."))] = False,
    config: ConfigOption = None,
) -> None:
    """Write one contact as a patient file with empty lists to fill in."""
    p = selection(search_contacts(term, config), term, num)
    if output.exists() and not overwrite:
        abort(w(f"{output} existiert bereits (--ueberschreiben).",
                f"{output} already exists (--overwrite)."))
    data = p.as_dict()
    for key in ("relatives", "referrers", "case_numbers", "terms"):
        data.setdefault(key, [])
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(w(f"Patientendatei geschrieben: {output}", f"Patient file written: {output}"))
