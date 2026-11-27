"""The Typer application, the option types every command shares and the helpers that
turn a patient source into a patient or stop the command with a message.

The command line speaks the language of the interface (:mod:`pseudonymed.language`):
``LANGUAGE`` is fixed once when it starts, because Typer reads the help texts while the
commands are declared.  Every text of the command line is written ``w(german, english)``;
the core's messages and reports go through :func:`shown` and :func:`error_message`.
Commands, groups and options answer to a German and an English name in both languages,
so that scripts run anywhere: the help lists the commands under the names of its
language (the others work hidden), and every option shows both names, its language's
first (:func:`command`, :func:`add_group`, :func:`flags`).  An option that takes one of a
few words answers to the German and the English word alike (:func:`choice`); the help names
the words of its language.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

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
from pseudonymed.document import docrepro_adapter
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


# German option declarations and their English counterparts ("--a/--no-a" for switches)
FLAGS_EN = {
    "--adressbuch": "--address-book", "--arbeitsstand": "--working-state",
    "--aufbereiten": "--clean-up", "--ausgabe": "--output", "--benutzer": "--user",
    "--bericht/--kein-bericht": "--report/--no-report", "--buchstabe": "--letter",
    "--codes/--keine-codes": "--codes/--no-codes", "--datei": "--file",
    "--datenbank": "--database", "--datum": "--date",
    "--dosierungen-schwaerzen/--dosierungen-lassen": "--redact-doses/--keep-doses",
    "--eigene-orte": "--own-places", "--empfaenger": "--recipient", "--entfernen": "--remove",
    "--fachbegriffe/--keine-fachbegriffe": "--medical-terms/--no-medical-terms",
    "--gleich": "--same", "--gliedern/--roh": "--structure/--raw",
    "--identifikatoren/--keine-identifikatoren": "--identifiers/--no-identifiers",
    "--ja": "--yes", "--kapsel": "--capsule", "--kategorie": "--category",
    "--kennung": "--code", "--klasse": "--class", "--konfig": "--config",
    "--konfig/--ohne-konfig": "--config/--no-config", "--kontakt": "--contact",
    "--kopieren/--nicht-kopieren": "--copy/--no-copy",
    "--laborwerte-schwaerzen/--laborwerte-lassen": "--redact-lab-tests/--keep-lab-tests",
    "--markdown/--kein-markdown": "--markdown/--no-markdown",
    "--medikamente-schwaerzen/--medikamente-lassen": "--redact-drugs/--keep-drugs",
    "--messwerte-schwaerzen/--messwerte-lassen": "--redact-measurements/--keep-measurements",
    "--modus": "--mode", "--ner/--kein-ner": "--ner/--no-ner", "--neu": "--new",
    "--neu-analysieren": "--reanalyse", "--nr": "--number",
    "--passwort-datei": "--password-file", "--privater-schluessel": "--private-key",
    "--rastern": "--rasterise", "--referenzpopulation": "--reference-population",
    "--schluessel": "--key", "--schluesselbund/--kein-schluesselbund": "--keyring/--no-keyring",
    "--spalte": "--column", "--speicher": "--store", "--sprache": "--language",
    "--suche": "--search",
    "--tabellen/--keine-tabellen": "--tables/--no-tables", "--text/--kein-text": "--text/--no-text",
    "--textebene/--keine-textebene": "--text-layer/--no-text-layer",
    "--trennzeichen": "--separator", "--ueberschreiben": "--overwrite",
    "--unlesbar": "--unreadable", "--unscharf/--genau": "--fuzzy/--exact",
    "--unverschluesselt": "--unencrypted", "--vermerk/--ohne-vermerk": "--notice/--no-notice",
    "--vokabular/--kein-vokabular": "--vocabulary/--no-vocabulary",
    "--vokabular/--ohne-vokabular": "--vocabulary/--no-vocabulary",
    "--vorschau": "--preview", "--ziel": "--target",
}


def flags(*decls: str) -> tuple[str, ...]:
    """The declarations of an option with their English names added: the German first in
    German, the English first in English.  A switch whose English name is the German one
    gets only its English negative (``" /--no-text"``)."""
    added = []
    for decl in decls:
        en = FLAGS_EN.get(decl)
        if en is None:
            continue
        if "/" in en and en.split("/")[0] == decl.split("/")[0]:
            en = " /" + en.split("/", 1)[1]
        added.append(en)
    return (*added, *decls) if english(LANGUAGE) else (*decls, *added)


def flag(name: str) -> str:
    """An option's name as the command line's language calls it (for messages)."""
    if not english(LANGUAGE):
        return name
    for decl, en in FLAGS_EN.items():
        for de_part, en_part in zip(decl.split("/"), en.split("/"), strict=False):
            if de_part == name:
                return en_part
    return name


# the German words that options take as their value, and the English ones that stand for them
VALUES_EN = {
    "alter": "age", "anrede": "salutation", "aus": "off",
    "buchstabe": "letter", "ersetzen": "replace", "immer": "always", "jahr": "year",
    "kliniken-at": "hospitals-at", "lassen": "keep", "liste": "list", "nie": "never",
    "schwaerzen": "redact", "vollname": "full-name", "zusammenfuehren": "merge",
}


def value_name(word: str) -> str:
    """A value of an option as the command line's language calls it (help, defaults,
    messages)."""
    return VALUES_EN.get(word, word) if english(LANGUAGE) else word


def german(given: str, allowed: tuple[str, ...]) -> str:
    """The word the core knows for a value given in either language: the one of ``allowed``
    whose English word was given, every other value as it came."""
    if given not in allowed:
        for word in allowed:
            if VALUES_EN.get(word) == given:
                return word
    return given


F = TypeVar("F", bound=Callable[..., Any])


def command(group: typer.Typer, de: str, en: str, **settings: Any) -> Callable[[F], F]:
    """Register a command under its German and its English name: the help lists the one of
    the command line's language, the other works hidden."""
    def register(func: F) -> F:
        group.command(de, hidden=english(LANGUAGE) and en != de, **settings)(func)
        if en != de:
            group.command(en, hidden=not english(LANGUAGE), **settings)(func)
        return func
    return register


def add_group(group: typer.Typer, de: str, en: str) -> None:
    """Add a group of commands under its German and its English name, like :func:`command`."""
    app.add_typer(group, name=de, hidden=english(LANGUAGE))
    app.add_typer(group, name=en, hidden=not english(LANGUAGE))


def list_metavar(en: str) -> str | None:
    """The metavar of a list argument: none in German, where Typer shows the German
    parameter name with its "..." (``{dateien}...``), ``en...`` in English."""
    return f"{en}..." if english(LANGUAGE) else None


def shown(text: object) -> str:
    """A text of the core (a message, a report line, a label) in the command line's language."""
    return in_language(text, LANGUAGE)


def error_message(e: BaseException) -> str:
    """The message of an error in the command line's language, docrepro's and the core's alike."""
    return docrepro_adapter.message(e, LANGUAGE)


_OPTION_NAMES = re.compile(r"--?[\w-]+(?:,--?[\w-]+)+")


class _BreakableNames:
    """A Rich highlighter that sets the names of an option one under the other."""

    def __init__(self, inner: Callable[[Any], Any]) -> None:
        self.inner = inner

    def __call__(self, text: Any) -> Any:
        """Highlight like Typer, with a line break after each comma of ``--ziel,--target``."""
        if isinstance(text, str) and _OPTION_NAMES.fullmatch(text):
            text = text.replace(",", ",\n")
        return self.inner(text)


def _readable_help() -> None:
    """Keep Typer's help readable with two names to every option.

    Typer joins an option's names with a bare comma, which Rich cannot break: with a German
    and an English name side by side the column grows until the help beside it is cut off
    with an ellipsis, at 100 columns already.  One under the other, the column is as wide as
    the longest single name, as with German names alone.  Where a terminal is narrow even
    so, the tables fold an over-long word onto the next line instead of cutting it off
    (Rich's default), so that no option, file name or variable goes missing."""
    try:
        from rich.table import Table
        from rich.text import Text
        from typer import rich_utils
    except ImportError:  # without Rich, Typer prints Click's plain help, which wraps
        return

    class FoldingTable(Table):
        """A table whose texts fold over-long words instead of cutting them off."""

        def add_row(self, *renderables: Any, **settings: Any) -> None:
            """Add the row with its texts folding, the parts of a help cell (``Columns``:
            help, variable, default) as well; a column that never wraps stays as it is."""
            for index, renderable in enumerate(renderables):
                if index < len(self.columns) and self.columns[index].no_wrap:
                    continue
                for part in getattr(renderable, "renderables", [renderable]):
                    if isinstance(part, Text) and part.overflow is None:
                        part.overflow = "fold"
            super().add_row(*renderables, **settings)

    for name in ("highlighter", "negative_highlighter"):
        current = getattr(rich_utils, name)
        if not isinstance(current, _BreakableNames):
            setattr(rich_utils, name, _BreakableNames(current))
    setattr(rich_utils, "Table", FoldingTable)  # noqa: B010 (mypy forbids assigning a type)


_readable_help()


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
    typer.Option(*flags("--speicher"),
                 help=w("Pfad der Pseudonym-Datenbank (Standard: Nutzerdatenordner).",
                        "Path of the pseudonym database (default: user data folder).")),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option(*flags("--konfig"),
                 help=w("Konfigurationsdatei (Standard: Nutzer-Konfigordner).",
                        "Configuration file (default: user configuration folder).")),
]
PatientOption = Annotated[
    Path | None,
    typer.Option("--patient", "-p", exists=True,
                 help=w("Patientendatei (JSON/TOML, siehe 'vorlage').",
                        "Patient file (JSON/TOML, see 'template').")),
]
VcfOption = Annotated[
    Path | None, typer.Option("--vcf", exists=True, help=w("Lokale vCard-Datei (.vcf).",
                                                           "Local vCard file (.vcf)."))
]
ContactOption = Annotated[
    str | None,
    typer.Option(*flags("--kontakt", "-k"),
                 help=w("Suchbegriff im CardDAV-Adressbuch (Name, Geburtsjahr).",
                        "Search term in the CardDAV address book (name, year of birth).")),
]
NumberOption = Annotated[int | None, typer.Option(
    *flags("--nr"), help=w("Nummer des Treffers bei mehreren.",
                           "Number of the match when there are several."))]
SchemaOption = Annotated[
    str, typer.Option("--schema", help=w("Pseudonymschema: 'hash' (Standard) oder 'buchstabe'.",
                                         "Pseudonym scheme: 'hash' (default) or 'letter'."))
]
NewOption = Annotated[
    bool, typer.Option(*flags("--neu"), help=w("Bei unsicherem Abgleich als neue Person anlegen.",
                                               "On an uncertain match, create a new person."))
]
SameOption = Annotated[
    str | None,
    typer.Option(*flags("--gleich"), help=w("Bei unsicherem Abgleich dieser Kennung zuordnen.",
                                            "On an uncertain match, assign to this code.")),
]


def abort(message: str, code: int = 1, cause: BaseException | None = None) -> NoReturn:
    """Print ``message`` (in the command line's language) to stderr and end with ``code``."""
    typer.echo(shown(message), err=True)
    if cause is not None:
        raise typer.Exit(code=code) from cause
    raise typer.Exit(code=code)


def choice(option: str, given: str, allowed: tuple[str, ...]) -> str:
    """The value of an option that takes one of a few words, as the core knows it: the
    German word, whether that or its English one was given.  Any other value stops the
    command with "--option muss 'a', 'b' oder 'c' sein."."""
    word = german(given, allowed)
    if word in allowed:
        return word
    quoted = [f"'{value_name(a)}'" for a in allowed]
    option = flag(option)
    abort(w(f"{option} muss {', '.join(quoted[:-1])} oder {quoted[-1]} sein.",
            f"{option} must be {', '.join(quoted[:-1])} or {quoted[-1]}."))


def scheme(schema: str) -> str:
    """The pseudonym scheme as the store knows it; an unknown one ends the command."""
    return choice("--schema", schema, SCHEMATA)


def open_store(path: Path | None, schema: str = "hash") -> PseudonymStore:
    """The pseudonym store at ``path`` (default location without), with a valid schema."""
    return PseudonymStore(path, schema=scheme(schema))


def matching_decision(new: bool, same: str | None) -> str | None:
    """The decision for an uncertain match as the store expects it: "neu", "gleich:<KENNUNG>"
    or None."""
    if new and same:
        abort(w("Bitte nur --neu oder --gleich angeben.", "Please give only --new or --same."))
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
                 "Decide with --same CODE (the same person) or --new (a new person)."),
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
                     "CardDAV is not set up: first 'pseudonymed contacts setup'."), err=True)
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
                     "No address book set up: 'pseudonymed contacts setup' (Nextcloud) or"
                     " 'pseudonymed contacts local FILE' (vCard file or folder)."), err=True)
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
                f"--number must lie between 1 and {len(patients)}."))
    if len(patients) == 1:
        return patients[0]
    typer.echo(w("Mehrere Treffer, bitte mit --nr wählen:",
                 "Several matches, please choose with --number:"), err=True)
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
                "Please give only one source: --patient, --vcf or --contact."))
    if patient is not None:
        return load_patient_or_abort(patient)
    if vcf is not None:
        return _from_vcf(vcf, contact, num)
    if contact:
        return selection(search_contacts(contact, config), contact, num)
    abort(w("Bitte --patient, --vcf oder --kontakt angeben.",
            "Please give --patient, --vcf or --contact."))

@app.callback()
def _main_command(
    language: Annotated[str | None, typer.Option(
        *flags("--sprache"), metavar=w("SPRACHE", "LANGUAGE"),
        help=w("Sprache der Ausgaben: de oder en (sonst PSEUDONYMED_SPRACHE, Konfiguration,"
               " Systemsprache).",
               "Language of the output: de or en (else PSEUDONYMED_SPRACHE, configuration,"
               " system language)."))] = None,
) -> None:
    """The group of all commands; the language option was read before Typer started (see
    the module docstring), so it only needs accepting here."""
    _tolerant_output()


def _tolerant_output() -> None:
    """Output that is not UTF-8 (a Windows console redirected to a file, cp1252) replaces
    what it cannot encode (λ, κ, →) instead of ending the command with an error."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding and encoding != "utf8" and reconfigure is not None:
            reconfigure(errors="replace")
