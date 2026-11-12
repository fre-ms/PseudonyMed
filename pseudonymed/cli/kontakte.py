"""Commands over the address books: CardDAV set-up, local vCards, search, export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import umgebung
from pseudonymed.cli.basis import (
    KonfigOption,
    NrOption,
    SpeicherOption,
    abbruch,
    app,
    auswahl,
    kontakte_suchen,
    patientenzeile,
    speicher_oeffnen,
    verbindung,
)
from pseudonymed.kern.modell import Patient
from pseudonymed.konfig import (
    UMGEBUNG_PASSWORT,
    CardDAVKonfig,
    lade_einstellungen,
    passwort_speichern,
    speichere_konfig,
)
from pseudonymed.quellen.carddav import CardDAVClient, CardDAVFehler

kontakte_app = typer.Typer(
    help="Patientensuche im CardDAV-Adressbuch (Nextcloud).", no_args_is_help=True
)
app.add_typer(kontakte_app, name="kontakte")


@kontakte_app.command()
def einrichten(
    url: Annotated[str, typer.Option("--url", prompt="Nextcloud-Adresse (https://…)",
                                     help="Basisadresse der Nextcloud.")],
    benutzer: Annotated[str, typer.Option("--benutzer", prompt=True, help="Nextcloud-Benutzer.")],
    adressbuch: Annotated[str, typer.Option(
        "--adressbuch", help="Adressbuch (URI oder Name), leer = einziges.")] = "",
    konfig: KonfigOption = None,
    schluesselbund: Annotated[bool, typer.Option(
        "--schluesselbund/--kein-schluesselbund",
        help="App-Passwort im Schlüsselbund des Betriebssystems ablegen.")] = True,
    passwort_datei: Annotated[bool, typer.Option(
        "--passwort-datei", help="Ohne Schlüsselbund: App-Passwort in der Konfigurationsdatei"
                                 " (nur für den Benutzer lesbar) ablegen.")] = False,
) -> None:
    """Zugang einrichten: Adresse, Benutzer, App-Passwort (Nextcloud → Sicherheit)."""
    passwort = (umgebung.variable("CARDDAV_PASSWORT")
                or typer.prompt("App-Passwort", hide_input=True))
    k = CardDAVKonfig(url=url.strip(), benutzer=benutzer.strip(), adressbuch=adressbuch.strip())
    try:
        client = CardDAVClient(k.url, k.benutzer, passwort)
        buecher = client.adressbuecher()
        if k.adressbuch:
            client.adressbuch(k.adressbuch)
    except CardDAVFehler as e:
        abbruch(f"Verbindung fehlgeschlagen: {e}", ursache=e)
    if schluesselbund and passwort_speichern(k, passwort):
        typer.echo(f"App-Passwort im Schlüsselbund abgelegt ({k.kennung}).")
    elif passwort_datei:
        k.passwort_datei = passwort
        typer.echo("App-Passwort in der Konfigurationsdatei abgelegt (Rechte 0600).")
    else:
        typer.echo(
            "Kein Schlüsselbund verfügbar (Extra 'carddav' installieren) – Passwort stattdessen"
            f" in {UMGEBUNG_PASSWORT} setzen oder --passwort-datei wählen.", err=True,
        )
    pfad = speichere_konfig(k, konfig)
    typer.echo(f"Konfiguration gespeichert: {pfad}")
    typer.echo("Adressbücher: " + ", ".join(f"{b.name} ({b.uri})" for b in buecher))


@kontakte_app.command("lokal")
def kontakte_lokal(
    pfad: Annotated[Path | None, typer.Argument(
        help="vCard-Datei oder Ordner mit vCard-Dateien; ohne Angabe: anzeigen.")] = None,
    entfernen: Annotated[bool, typer.Option("--entfernen",
                                            help="Lokales Adressbuch austragen.")] = False,
    konfig: KonfigOption = None,
) -> None:
    """Ein lokales Adressbuch (vCard-Datei oder Ordner) einrichten oder anzeigen."""
    from pseudonymed.konfig import speichere_einstellungen
    from pseudonymed.quellen.adressbuch import lokale_dateien

    e = lade_einstellungen(konfig)
    if entfernen:
        e.adressbuch_lokal = ""
        speichere_einstellungen(e, konfig)
        typer.echo("Lokales Adressbuch ausgetragen.")
        return
    if pfad is None:
        typer.echo(e.adressbuch_lokal or "Kein lokales Adressbuch eingerichtet.")
        return
    dateien = lokale_dateien(pfad)
    if not dateien:
        abbruch(f"{pfad}: keine vCard-Datei(en) gefunden.")
    e.adressbuch_lokal = str(pfad.expanduser().resolve())
    speichere_einstellungen(e, konfig)
    typer.echo(f"Lokales Adressbuch: {e.adressbuch_lokal} ({len(dateien)} Datei(en)).")


@kontakte_app.command()
def adressbuecher(konfig: KonfigOption = None) -> None:
    """Adressbücher des Benutzers auflisten."""
    client, _k = verbindung(konfig)
    try:
        buecher = client.adressbuecher()
    except CardDAVFehler as e:
        abbruch(str(e), ursache=e)
    for b in buecher:
        typer.echo(f"{b.uri:30}  {b.name}")


@kontakte_app.command()
def suchen(
    begriff: Annotated[str, typer.Argument(help="Name, Namensteil, Geburtsjahr oder -datum.")],
    konfig: KonfigOption = None,
    speicher: SpeicherOption = None,
) -> None:
    """Kontakte suchen; zeigt vergebene Pseudonym-Buchstaben an."""
    patienten = kontakte_suchen(begriff, konfig)
    if not patienten:
        typer.echo(f"Kein Kontakt passt zu {begriff!r}.")
        raise typer.Exit(code=1)
    with speicher_oeffnen(speicher) as sp:
        for i, p in enumerate(patienten, 1):
            info = sp.patient_info(p.schluessel())
            typer.echo(patientenzeile(i, p, info["kennung"] if info else None))


def _patient_text(p: Patient) -> str:
    felder = [
        ("Name", f"{p.nachname}, {p.vorname}"),
        ("Anrede/Geschlecht", {"m": "Herr", "w": "Frau"}.get(p.geschlecht, "unbekannt")),
        ("Titel", p.titel),
        ("Geburtsname", p.geburtsname),
        ("Geburtsdatum", p.geburtsdatum.strftime("%d.%m.%Y") if p.geburtsdatum else ""),
        ("Anschrift", ", ".join(t for t in (p.strasse, f"{p.plz} {p.ort}".strip()) if t)),
        ("Telefon", "; ".join(p.telefone)),
        ("E-Mail", "; ".join(p.emails)),
        ("Versichertennummer", p.versichertennummer),
        ("Patienten-ID", p.patienten_id),
    ]
    zeilen = [f"{name + ':':20} {wert}" for name, wert in felder if wert]
    for h in p.hinweise:
        zeilen.append(f"{'Notiz:':20} {h}")
    zeilen.append(
        f"{'Pseudonymschlüssel:':20} {p.schluessel()}"
    )
    return "\n".join(zeilen)


@kontakte_app.command()
def zeigen(
    begriff: Annotated[str, typer.Argument(help="Suchbegriff.")],
    nr: NrOption = None,
    als_json: Annotated[bool, typer.Option("--json", help="Als Patientendatei (JSON) ausgeben.")]
    = False,
    konfig: KonfigOption = None,
) -> None:
    """Abgeleitete Identifikatoren eines Kontakts anzeigen (nur lokal)."""
    p = auswahl(kontakte_suchen(begriff, konfig), begriff, nr)
    if als_json:
        typer.echo(json.dumps(p.als_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(_patient_text(p))


@kontakte_app.command()
def export(
    begriff: Annotated[str, typer.Argument(help="Suchbegriff.")],
    ausgabe: Annotated[Path, typer.Option("--ausgabe", "-o", help="Zieldatei (JSON).")],
    nr: NrOption = None,
    ueberschreiben: Annotated[bool, typer.Option("--ueberschreiben",
                                                 help="Vorhandene Datei ersetzen.")] = False,
    konfig: KonfigOption = None,
) -> None:
    """Kontakt als Patientendatei speichern, um Angehörige, Zuweiser und Begriffe zu ergänzen."""
    p = auswahl(kontakte_suchen(begriff, konfig), begriff, nr)
    if ausgabe.exists() and not ueberschreiben:
        abbruch(f"{ausgabe} existiert bereits (--ueberschreiben).")
    daten = p.als_dict()
    daten.setdefault("angehoerige", [])
    daten.setdefault("zuweiser", [])
    daten.setdefault("aktenzeichen", [])
    daten.setdefault("begriffe", [])
    ausgabe.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Patientendatei geschrieben: {ausgabe}")
