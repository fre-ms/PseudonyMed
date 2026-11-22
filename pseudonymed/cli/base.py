"""The Typer application, the option types every command shares and the helpers that
turn a patient source into a patient or stop the command with a message.

The command line speaks the language of the interface (:mod:`pseudonymed.language`):
``LANGUAGE`` is fixed once when it starts, because Typer reads the help texts while the
commands are declared.  Every text of the command line is written ``w(german, english)``;
the core's messages and reports go through :func:`shown` and :func:`error_message`.
Command and option names stay German in both languages, so that scripts run anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from pseudonymed import language as language_module
from pseudonymed.config import (
    PASSWORD_ENV_VAR,
    CardDAVConfig,
    load_config,
    load_password,
)
from pseudonymed.core.model import Patient
from pseudonymed.core.text import english, in_language
from pseudonymed.document import optdoc_adapter
from pseudonymed.pseudonyms.store import (
    SCHEMATA,
    PseudonymStore,
    UncertainMatch,
)
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError, fits

EXPLICIT_LANGUAGE = language_module.from_arguments(sys.argv[1:])
LANGUAGE = language_module.determine(sys.argv[1:])


def w(de: str, en: str) -> str:
    """The command line's wording in its language: the German text, or the English one when
    the language asks for English."""
    return en if english(LANGUAGE) else de


def list_metavar(en: str) -> str | None:
    """The metavar of a list argument: none in German, where Typer shows the German
    parameter name with its "..." (``{dateien}...``), ``en...`` in English."""
    return f"{en}..." if english(LANGUAGE) else None


def shown(text: object) -> str:
    """A text of the core (a message, a report line, a label) in the command line's language."""
    return in_language(text, LANGUAGE)


def error_message(e: BaseException) -> str:
    """The message of an error in the command line's language, optdoc's and the core's alike."""
    return optdoc_adapter.message(e, LANGUAGE)


app = typer.Typer(
    help=w("PseudonyMed – Patientendokumente lokal pseudonymisieren, Pseudonyme später"
           " zurücksetzen.",
           "PseudonyMed – pseudonymise patient documents locally, put the clear data back"
           " later."),
    no_args_is_help=True,
    add_completion=False,
)

StoreOption = Annotated[
    Path | None,
    typer.Option("--speicher",
                 help=w("Pfad der Pseudonym-Datenbank (Standard: Nutzerdatenordner).",
                        "Path of the pseudonym database (default: user data folder).")),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option("--konfig", help=w("Konfigurationsdatei (Standard: Nutzer-Konfigordner).",
                                    "Configuration file (default: user configuration folder).")),
]
PatientOption = Annotated[
    Path | None,
    typer.Option("--patient", "-p", exists=True,
                 help=w("Patientendatei (JSON/TOML, siehe 'vorlage').",
                        "Patient file (JSON/TOML, see 'vorlage').")),
]
VcfOption = Annotated[
    Path | None, typer.Option("--vcf", exists=True, help=w("Lokale vCard-Datei (.vcf).",
                                                           "Local vCard file (.vcf)."))
]
ContactOption = Annotated[
    str | None,
    typer.Option("--kontakt", "-k",
                 help=w("Suchbegriff im CardDAV-Adressbuch (Name, Geburtsjahr).",
                        "Search term in the CardDAV address book (name, year of birth).")),
]
NumberOption = Annotated[int | None, typer.Option(
    "--nr", help=w("Nummer des Treffers bei mehreren.",
                   "Number of the match when there are several."))]
SchemaOption = Annotated[
    str, typer.Option("--schema", help=w("Pseudonymschema: 'hash' (Standard) oder 'buchstabe'.",
                                         "Pseudonym scheme: 'hash' (default) or 'buchstabe'."))
]
NewOption = Annotated[
    bool, typer.Option("--neu", help=w("Bei unsicherem Abgleich als neue Person anlegen.",
                                       "On an uncertain match, create a new person."))
]
SameOption = Annotated[
    str | None,
    typer.Option("--gleich", help=w("Bei unsicherem Abgleich dieser Kennung zuordnen.",
                                    "On an uncertain match, assign to this code.")),
]


def abort(message: str, code: int = 1, cause: BaseException | None = None) -> NoReturn:
    """Print ``message`` (in the command line's language) to stderr and end with ``code``."""
    typer.echo(shown(message), err=True)
    if cause is not None:
        raise typer.Exit(code=code) from cause
    raise typer.Exit(code=code)


def check_choice(option: str, value: str, allowed: tuple[str, ...]) -> None:
    """Stop with "--option muss 'a', 'b' oder 'c' sein." unless ``value`` is one of them."""
    if value in allowed:
        return
    quoted = [f"'{a}'" for a in allowed]
    abort(w(f"{option} muss {', '.join(quoted[:-1])} oder {quoted[-1]} sein.",
            f"{option} must be {', '.join(quoted[:-1])} or {quoted[-1]}."))


def open_store(path: Path | None, schema: str = "hash") -> PseudonymStore:
    """The pseudonym store at ``path`` (default location without), with a valid schema."""
    if schema not in SCHEMATA:
        abort(w(f"--schema muss eines von {', '.join(SCHEMATA)} sein.",
                f"--schema must be one of {', '.join(SCHEMATA)}."))
    return PseudonymStore(path, schema=schema)


def matching_decision(new: bool, same: str | None) -> str | None:
    """The decision for an uncertain match as the store expects it: "neu", "gleich:<KENNUNG>"
    or None."""
    if new and same:
        abort(w("Bitte nur --neu oder --gleich angeben.", "Please give only --neu or --gleich."))
    if new:
        return "neu"
    return f"gleich:{same.strip().upper()}" if same else None


def report_uncertain(p: Patient, uncertain: UncertainMatch) -> None:
    """List the similar entries of the store and how to decide between them."""
    typer.echo(w(f"Unsicherer Abgleich für {p.surname}, {p.first_name}:"
                 " ähnliche Einträge im Pseudonymspeicher:",
                 f"Uncertain match for {p.surname}, {p.first_name}:"
                 " similar entries in the pseudonym store:"), err=True)
    for k in uncertain.candidates:
        fields = ", ".join(f"{n} {v:.2f}" for n, v in k.fields.items())
        born = k.birth_date or "–"
        typer.echo(f"  {k.code}  {k.surname}, {k.first_name}  {w('geb.', 'born')} {born}"
                   f"  Score {k.score:.2f} ({fields})", err=True)
    typer.echo(w("Entscheiden mit --gleich KENNUNG (dieselbe Person) oder --neu (neue Person).",
                 "Decide with --gleich CODE (the same person) or --neu (a new person)."),
               err=True)


def load_patient_or_abort(path: Path) -> Patient:
    """The patient from a JSON or TOML file; a bad file ends the command."""
    from pseudonymed.sources.manual import load_patient

    try:
        return load_patient(path)
    except (OSError, ValueError) as e:
        abort(w(f"Patientendatei nicht lesbar: {e}", f"Patient file not readable: {e}"), cause=e)


def connection(config: Path | None) -> tuple[CardDAVClient, CardDAVConfig]:
    """A CardDAV client from the configuration and the keyring; missing pieces end the
    command with a hint."""
    k = load_config(config)
    if not k.complete:
        typer.echo(w("CardDAV ist nicht eingerichtet: zuerst 'pseudonymed kontakte einrichten'.",
                     "CardDAV is not set up: first 'pseudonymed kontakte einrichten'."), err=True)
        raise typer.Exit(code=1)
    password = load_password(k)
    if not password:
        typer.echo(w(f"Kein App-Passwort für {k.code} gefunden (Schlüsselbund oder"
                     f" {PASSWORD_ENV_VAR}).",
                     f"No app password found for {k.code} (keyring or {PASSWORD_ENV_VAR})."),
                   err=True)
        raise typer.Exit(code=1)
    try:
        return CardDAVClient(k.url, k.user, password), k
    except CardDAVError as e:
        abort(error_message(e), cause=e)


def search_contacts(term: str, config: Path | None) -> list[Patient]:
    """Local vCard book and CardDAV together; a source that fails is reported, and only
    when nothing at all is set up or every source failed the command stops."""
    from pseudonymed.sources import address_book

    if not address_book.is_set_up(config):
        typer.echo(w("Kein Adressbuch eingerichtet: 'pseudonymed kontakte einrichten'"
                     " (Nextcloud) oder 'pseudonymed kontakte lokal DATEI' (vCard-Datei oder"
                     " Ordner).",
                     "No address book set up: 'pseudonymed kontakte einrichten' (Nextcloud) or"
                     " 'pseudonymed kontakte lokal FILE' (vCard file or folder)."), err=True)
        raise typer.Exit(code=1)
    result = address_book.search(term, config)
    for failure in result.error:
        typer.echo(shown(failure), err=True)
    if result.error and len(result.error) == len(result.sources):
        raise typer.Exit(code=1)
    return result.patients


def patient_line(num: int, p: Patient, code: str | None = None) -> str:
    """One line of a hit list: number, name, birth date, place, pseudonym when known."""
    born = p.birth_date.strftime(w("%d.%m.%Y", "%Y-%m-%d")) if p.birth_date else "–"
    place = " ".join(t for t in (p.postcode, p.place) if t)
    pseudonym = f"  → {'Patientin' if p.female else 'Patient'} {code}" if code else ""
    return f"{num:>3}  {p.surname}, {p.first_name}  {w('geb.', 'born')} {born}  {place}{pseudonym}"


def selection(patients: list[Patient], term: str, num: int | None) -> Patient:
    """The one patient meant: the only hit, or the one chosen with ``num``; otherwise the
    hits are listed and the command ends."""
    if not patients:
        abort(w(f"Kein Kontakt passt zu {term!r}.", f"No contact matches {term!r}."))
    if num is not None:
        if 1 <= num <= len(patients):
            return patients[num - 1]
        abort(w(f"--nr muss zwischen 1 und {len(patients)} liegen.",
                f"--nr must lie between 1 and {len(patients)}."))
    if len(patients) == 1:
        return patients[0]
    typer.echo(w("Mehrere Treffer, bitte mit --nr wählen:",
                 "Several matches, please choose with --nr:"), err=True)
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
        abort(w("Bitte nur eine Quelle angeben: --patient, --vcf oder --kontakt.",
                "Please give only one source: --patient, --vcf or --kontakt."))
    if patient is not None:
        return load_patient_or_abort(patient)
    if vcf is not None:
        return _from_vcf(vcf, contact, num)
    if contact:
        return selection(search_contacts(contact, config), contact, num)
    abort(w("Bitte --patient, --vcf oder --kontakt angeben.",
            "Please give --patient, --vcf or --kontakt."))

@app.callback()
def _main_command(
    language: Annotated[str | None, typer.Option(
        "--sprache", "--language", metavar=w("SPRACHE", "LANGUAGE"),
        help=w("Sprache der Ausgaben: de oder en (sonst PSEUDONYMED_SPRACHE, Konfiguration,"
               " Systemsprache).",
               "Language of the output: de or en (else PSEUDONYMED_SPRACHE, configuration,"
               " system language)."))] = None,
) -> None:
    """The group of all commands; the language option was read before Typer started (see
    the module docstring), so it only needs accepting here."""
