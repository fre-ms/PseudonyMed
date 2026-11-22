"""The Typer application, the option types every command shares and the helpers that
turn a patient source into a patient or stop the command with a message."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pseudonymed.config import (
    PASSWORD_ENV_VAR,
    CardDAVConfig,
    load_config,
    load_password,
)
from pseudonymed.core.model import Patient
from pseudonymed.pseudonyms.store import (
    SCHEMATA,
    PseudonymStore,
    UncertainMatch,
)
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError, fits

app = typer.Typer(
    help="PseudonyMed – Patientendokumente lokal pseudonymisieren, Pseudonyme später zurücksetzen.",
    no_args_is_help=True,
    add_completion=False,
)

StoreOption = Annotated[
    Path | None,
    typer.Option("--speicher", help="Pfad der Pseudonym-Datenbank (Standard: Nutzerdatenordner)."),
]
ConfigOption = Annotated[
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
ContactOption = Annotated[
    str | None,
    typer.Option("--kontakt", "-k", help="Suchbegriff im CardDAV-Adressbuch (Name, Geburtsjahr)."),
]
NumberOption = Annotated[int | None, typer.Option("--nr", help="Nummer des Treffers bei mehreren.")]
SchemaOption = Annotated[
    str, typer.Option("--schema", help="Pseudonymschema: 'hash' (Standard) oder 'buchstabe'.")
]
NewOption = Annotated[
    bool, typer.Option("--neu", help="Bei unsicherem Abgleich als neue Person anlegen.")
]
SameOption = Annotated[
    str | None,
    typer.Option("--gleich", help="Bei unsicherem Abgleich dieser Kennung zuordnen."),
]


def abort(message: str, code: int = 1, cause: BaseException | None = None) -> NoReturn:
    """Print ``message`` to stderr and end the command with ``code``."""
    typer.echo(message, err=True)
    if cause is not None:
        raise typer.Exit(code=code) from cause
    raise typer.Exit(code=code)


def check_choice(option: str, value: str, allowed: tuple[str, ...]) -> None:
    """Stop with "--option muss 'a', 'b' oder 'c' sein." unless ``value`` is one of them."""
    if value in allowed:
        return
    quoted = [f"'{w}'" for w in allowed]
    abort(f"{option} muss {', '.join(quoted[:-1])} oder {quoted[-1]} sein.")


def open_store(path: Path | None, schema: str = "hash") -> PseudonymStore:
    """The pseudonym store at ``path`` (default location without), with a valid schema."""
    if schema not in SCHEMATA:
        abort(f"--schema muss eines von {', '.join(SCHEMATA)} sein.")
    return PseudonymStore(path, schema=schema)


def matching_decision(new: bool, same: str | None) -> str | None:
    """The decision for an uncertain match as the store expects it: "neu", "gleich:<KENNUNG>"
    or None."""
    if new and same:
        abort("Bitte nur --neu oder --gleich angeben.")
    if new:
        return "neu"
    return f"gleich:{same.strip().upper()}" if same else None


def report_uncertain(p: Patient, error: UncertainMatch) -> None:
    """List the similar entries of the store and how to decide between them."""
    typer.echo(
        f"Unsicherer Abgleich für {p.surname}, {p.first_name}:"
        " ähnliche Einträge im Pseudonymspeicher:",
        err=True,
    )
    for k in error.candidates:
        fields = ", ".join(f"{n} {v:.2f}" for n, v in k.fields.items())
        born = k.birth_date or "–"
        typer.echo(f"  {k.code}  {k.surname}, {k.first_name}  geb. {born}  Score {k.score:.2f}"
                   f" ({fields})", err=True)
    typer.echo("Entscheiden mit --gleich KENNUNG (dieselbe Person) oder --neu (neue Person).",
               err=True)


def load_patient_or_abort(path: Path) -> Patient:
    """The patient from a JSON or TOML file; a bad file ends the command."""
    from pseudonymed.sources.manual import load_patient

    try:
        return load_patient(path)
    except (OSError, ValueError) as e:
        abort(f"Patientendatei nicht lesbar: {e}", cause=e)


def connection(config: Path | None) -> tuple[CardDAVClient, CardDAVConfig]:
    """A CardDAV client from the configuration and the keyring; missing pieces end the
    command with a hint."""
    k = load_config(config)
    if not k.complete:
        typer.echo("CardDAV ist nicht eingerichtet: zuerst 'pseudonymed kontakte einrichten'.",
                   err=True)
        raise typer.Exit(code=1)
    password = load_password(k)
    if not password:
        typer.echo(
            f"Kein App-Passwort für {k.code} gefunden (Schlüsselbund oder {PASSWORD_ENV_VAR}).",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        return CardDAVClient(k.url, k.user, password), k
    except CardDAVError as e:
        abort(str(e), cause=e)


def search_contacts(term: str, config: Path | None) -> list[Patient]:
    """Local vCard book and CardDAV together; a source that fails is reported, and only
    when nothing at all is set up or every source failed the command stops."""
    from pseudonymed.sources import address_book

    if not address_book.is_set_up(config):
        typer.echo("Kein Adressbuch eingerichtet: 'pseudonymed kontakte einrichten' (Nextcloud)"
                   " oder 'pseudonymed kontakte lokal DATEI' (vCard-Datei oder Ordner).", err=True)
        raise typer.Exit(code=1)
    result = address_book.search(term, config)
    for error in result.error:
        typer.echo(error, err=True)
    if result.error and len(result.error) == len(result.sources):
        raise typer.Exit(code=1)
    return result.patients


def patient_line(num: int, p: Patient, code: str | None = None) -> str:
    """One line of a hit list: number, name, birth date, place, pseudonym when known."""
    born = p.birth_date.strftime("%d.%m.%Y") if p.birth_date else "–"
    place = " ".join(t for t in (p.postcode, p.place) if t)
    pseudonym = f"  → {'Patientin' if p.female else 'Patient'} {code}" if code else ""
    return f"{num:>3}  {p.surname}, {p.first_name}  geb. {born}  {place}{pseudonym}"


def selection(patients: list[Patient], term: str, num: int | None) -> Patient:
    """The one patient meant: the only hit, or the one chosen with ``num``; otherwise the
    hits are listed and the command ends."""
    if not patients:
        abort(f"Kein Kontakt passt zu {term!r}.")
    if num is not None:
        if 1 <= num <= len(patients):
            return patients[num - 1]
        abort(f"--nr muss zwischen 1 und {len(patients)} liegen.")
    if len(patients) == 1:
        return patients[0]
    typer.echo("Mehrere Treffer, bitte mit --nr wählen:", err=True)
    for i, p in enumerate(patients, 1):
        typer.echo(patient_line(i, p), err=True)
    raise typer.Exit(code=1)


def _from_vcf(vcf: Path, contact: str | None, num: int | None) -> Patient:
    """The patient from a local vCard file, filtered by ``contact`` when given."""
    from pseudonymed.sources.vcf import patients_from_vcf

    candidates = [p for p in patients_from_vcf(vcf) if p.surname or p.first_name]
    if contact:
        candidates = [p for p in candidates if fits(p, contact)]
    return selection(candidates, contact or vcf.name, num)


def resolve_patient(
    patient: Path | None, vcf: Path | None, contact: str | None, num: int | None,
    config: Path | None,
) -> Patient:
    """Exactly one source: patient file, local vCard file (optionally filtered) or CardDAV."""
    if patient is not None and (vcf is not None or contact):
        abort("Bitte nur eine Quelle angeben: --patient, --vcf oder --kontakt.")
    if patient is not None:
        return load_patient_or_abort(patient)
    if vcf is not None:
        return _from_vcf(vcf, contact, num)
    if contact:
        return selection(search_contacts(contact, config), contact, num)
    abort("Bitte --patient, --vcf oder --kontakt angeben.")

@app.callback()
def _main_command() -> None:
    """PseudonyMed: lokale Pseudonymisierung mit stabilen Pseudonymen."""
