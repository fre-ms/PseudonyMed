"""The Typer application, the option types every command shares and the helpers that
turn a patient source into a patient or stop the command with a message."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pseudonymed.kern.modell import Patient
from pseudonymed.konfig import (
    UMGEBUNG_PASSWORT,
    CardDAVKonfig,
    lade_konfig,
    passwort_laden,
)
from pseudonymed.pseudonyme.speicher import (
    SCHEMATA,
    Pseudonymspeicher,
    UnsichererAbgleich,
)
from pseudonymed.quellen.carddav import CardDAVClient, CardDAVFehler, passt

app = typer.Typer(
    help="PseudonyMed – Patientendokumente lokal pseudonymisieren, Pseudonyme später zurücksetzen.",
    no_args_is_help=True,
    add_completion=False,
)

SpeicherOption = Annotated[
    Path | None,
    typer.Option("--speicher", help="Pfad der Pseudonym-Datenbank (Standard: Nutzerdatenordner)."),
]
KonfigOption = Annotated[
    Path | None,
    typer.Option("--konfig", help="Konfigurationsdatei (Standard: Nutzer-Konfigordner)."),
]
PatientOption = Annotated[
    Path | None,
    typer.Option("--patient", "-p", exists=True,
                 help="Patientendatei (JSON/TOML, siehe 'vorlage')."),
]
VcfOption = Annotated[
    Path | None, typer.Option("--vcf", exists=True, help="Lokale vCard-Datei (.vcf).")
]
KontaktOption = Annotated[
    str | None,
    typer.Option("--kontakt", "-k", help="Suchbegriff im CardDAV-Adressbuch (Name, Geburtsjahr)."),
]
NrOption = Annotated[int | None, typer.Option("--nr", help="Nummer des Treffers bei mehreren.")]
SchemaOption = Annotated[
    str, typer.Option("--schema", help="Pseudonymschema: 'hash' (Standard) oder 'buchstabe'.")
]
NeuOption = Annotated[
    bool, typer.Option("--neu", help="Bei unsicherem Abgleich als neue Person anlegen.")
]
GleichOption = Annotated[
    str | None,
    typer.Option("--gleich", help="Bei unsicherem Abgleich dieser Kennung zuordnen."),
]


def abbruch(meldung: str, code: int = 1, ursache: BaseException | None = None) -> NoReturn:
    """Print ``meldung`` to stderr and end the command with ``code``."""
    typer.echo(meldung, err=True)
    if ursache is not None:
        raise typer.Exit(code=code) from ursache
    raise typer.Exit(code=code)


def wahl_pruefen(option: str, wert: str, erlaubt: tuple[str, ...]) -> None:
    """Stop with "--option muss 'a', 'b' oder 'c' sein." unless ``wert`` is one of them."""
    if wert in erlaubt:
        return
    genannt = [f"'{w}'" for w in erlaubt]
    abbruch(f"{option} muss {', '.join(genannt[:-1])} oder {genannt[-1]} sein.")


def speicher_oeffnen(pfad: Path | None, schema: str = "hash") -> Pseudonymspeicher:
    """The pseudonym store at ``pfad`` (default location without), with a valid schema."""
    if schema not in SCHEMATA:
        abbruch(f"--schema muss eines von {', '.join(SCHEMATA)} sein.")
    return Pseudonymspeicher(pfad, schema=schema)


def abgleich_entscheidung(neu: bool, gleich: str | None) -> str | None:
    """The decision for an uncertain match as the store expects it: "neu", "gleich:<KENNUNG>"
    or None."""
    if neu and gleich:
        abbruch("Bitte nur --neu oder --gleich angeben.")
    if neu:
        return "neu"
    return f"gleich:{gleich.strip().upper()}" if gleich else None


def unsicher_melden(p: Patient, fehler: UnsichererAbgleich) -> None:
    """List the similar entries of the store and how to decide between them."""
    typer.echo(
        f"Unsicherer Abgleich für {p.nachname}, {p.vorname}:"
        " ähnliche Einträge im Pseudonymspeicher:",
        err=True,
    )
    for k in fehler.kandidaten:
        felder = ", ".join(f"{n} {v:.2f}" for n, v in k.felder.items())
        geb = k.geburtsdatum or "–"
        typer.echo(f"  {k.kennung}  {k.nachname}, {k.vorname}  geb. {geb}  Score {k.score:.2f}"
                   f" ({felder})", err=True)
    typer.echo("Entscheiden mit --gleich KENNUNG (dieselbe Person) oder --neu (neue Person).",
               err=True)


def patient_laden(pfad: Path) -> Patient:
    """The patient from a JSON or TOML file; a bad file ends the command."""
    from pseudonymed.quellen.manuell import lade_patient

    try:
        return lade_patient(pfad)
    except (OSError, ValueError) as e:
        abbruch(f"Patientendatei nicht lesbar: {e}", ursache=e)


def verbindung(konfig: Path | None) -> tuple[CardDAVClient, CardDAVKonfig]:
    """A CardDAV client from the configuration and the keyring; missing pieces end the
    command with a hint."""
    k = lade_konfig(konfig)
    if not k.vollstaendig:
        typer.echo("CardDAV ist nicht eingerichtet: zuerst 'pseudonymed kontakte einrichten'.",
                   err=True)
        raise typer.Exit(code=1)
    passwort = passwort_laden(k)
    if not passwort:
        typer.echo(
            f"Kein App-Passwort für {k.kennung} gefunden (Schlüsselbund oder {UMGEBUNG_PASSWORT}).",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        return CardDAVClient(k.url, k.benutzer, passwort), k
    except CardDAVFehler as e:
        abbruch(str(e), ursache=e)


def kontakte_suchen(begriff: str, konfig: Path | None) -> list[Patient]:
    """Local vCard book and CardDAV together; a source that fails is reported, and only
    when nothing at all is set up or every source failed the command stops."""
    from pseudonymed.quellen import adressbuch

    if not adressbuch.eingerichtet(konfig):
        typer.echo("Kein Adressbuch eingerichtet: 'pseudonymed kontakte einrichten' (Nextcloud)"
                   " oder 'pseudonymed kontakte lokal DATEI' (vCard-Datei oder Ordner).", err=True)
        raise typer.Exit(code=1)
    ergebnis = adressbuch.suche(begriff, konfig)
    for fehler in ergebnis.fehler:
        typer.echo(fehler, err=True)
    if ergebnis.fehler and len(ergebnis.fehler) == len(ergebnis.quellen):
        raise typer.Exit(code=1)
    return ergebnis.patienten


def patientenzeile(nr: int, p: Patient, kennung: str | None = None) -> str:
    """One line of a hit list: number, name, birth date, place, pseudonym when known."""
    geb = p.geburtsdatum.strftime("%d.%m.%Y") if p.geburtsdatum else "–"
    ort = " ".join(t for t in (p.plz, p.ort) if t)
    pseudonym = f"  → {'Patientin' if p.weiblich else 'Patient'} {kennung}" if kennung else ""
    return f"{nr:>3}  {p.nachname}, {p.vorname}  geb. {geb}  {ort}{pseudonym}"


def auswahl(patienten: list[Patient], begriff: str, nr: int | None) -> Patient:
    """The one patient meant: the only hit, or the one chosen with ``nr``; otherwise the
    hits are listed and the command ends."""
    if not patienten:
        abbruch(f"Kein Kontakt passt zu {begriff!r}.")
    if nr is not None:
        if 1 <= nr <= len(patienten):
            return patienten[nr - 1]
        abbruch(f"--nr muss zwischen 1 und {len(patienten)} liegen.")
    if len(patienten) == 1:
        return patienten[0]
    typer.echo("Mehrere Treffer, bitte mit --nr wählen:", err=True)
    for i, p in enumerate(patienten, 1):
        typer.echo(patientenzeile(i, p), err=True)
    raise typer.Exit(code=1)


def _aus_vcf(vcf: Path, kontakt: str | None, nr: int | None) -> Patient:
    """The patient from a local vCard file, filtered by ``kontakt`` when given."""
    from pseudonymed.quellen.vcf import patienten_aus_vcf

    kandidaten = [p for p in patienten_aus_vcf(vcf) if p.nachname or p.vorname]
    if kontakt:
        kandidaten = [p for p in kandidaten if passt(p, kontakt)]
    return auswahl(kandidaten, kontakt or vcf.name, nr)


def patient_bestimmen(
    patient: Path | None, vcf: Path | None, kontakt: str | None, nr: int | None,
    konfig: Path | None,
) -> Patient:
    """Exactly one source: patient file, local vCard file (optionally filtered) or CardDAV."""
    if patient is not None and (vcf is not None or kontakt):
        abbruch("Bitte nur eine Quelle angeben: --patient, --vcf oder --kontakt.")
    if patient is not None:
        return patient_laden(patient)
    if vcf is not None:
        return _aus_vcf(vcf, kontakt, nr)
    if kontakt:
        return auswahl(kontakte_suchen(kontakt, konfig), kontakt, nr)
    abbruch("Bitte --patient, --vcf oder --kontakt angeben.")

@app.callback()
def _hauptbefehl() -> None:
    """PseudonyMed: lokale Pseudonymisierung mit stabilen Pseudonymen."""
