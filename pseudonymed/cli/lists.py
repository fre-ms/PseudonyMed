"""Commands over the two word lists: the protected medical vocabulary and the identifiers."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pseudonymed.cli.base import (
    abort,
    app,
    error_message,
    list_metavar,
    w,
)

medical_terms_app = typer.Typer(
    help=w("Medizinisches Fachvokabular, das NER und unscharfe OCR-Suche nicht anfassen dürfen.",
           "Medical vocabulary that NER and the fuzzy OCR search must not touch."),
    no_args_is_help=True,
)
app.add_typer(medical_terms_app, name="fachbegriffe")


@medical_terms_app.command("status", help=w("Umfang und Kategorien des Vokabulars anzeigen.",
                                            "Show the size and categories of the vocabulary."))
def medical_terms_status() -> None:
    """Print the number of protected terms per source and where the table lives."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    fb = terms_module.standard()
    typer.echo(w(f"{fb.count} geschützte Begriffe", f"{fb.count} protected terms"))
    for name, n in fb.sources:
        typer.echo(f"  {n:>7}  {name}")
    path = vocab_mod.default_path()
    typer.echo(w(f"Tabelle vokabular in {path} (Schritt „3 Fachvokabular“,"
                 " 'fachbegriffe hinzufuegen|entfernen|exportieren|importieren')",
                 f"Table vokabular in {path} (step ‘3 Medical vocabulary’,"
                 " 'fachbegriffe hinzufuegen|entfernen|exportieren|importieren')"))


@medical_terms_app.command("hinzufuegen", help=w("Begriffe ins Vokabular aufnehmen.",
                                                 "Add terms to the vocabulary."))
def add_medical_terms(
    begriffe: Annotated[list[str], typer.Argument(
        metavar=list_metavar("terms"),
        help=w("Begriffe (auch mehrwortig).", "Terms (several words allowed)."))],
    category: Annotated[str, typer.Option("--kategorie",
                                          help=w("Kategorie in der Tabelle.",
                                                 "Category in the table."))]
    = "Nutzervokabular",
) -> None:
    """Add terms to the vocabulary table under a category."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.add(begriffe, category)
    terms_module.standard.cache_clear()
    typer.echo(w(f"{n} neue Begriffe ({category}).", f"{n} new term(s) ({category})."))


@medical_terms_app.command("entfernen",
                           help=w("Begriffe aus dem Vokabular löschen (auch mitgelieferte).",
                                  "Delete terms from the vocabulary (bundled ones too)."))
def remove_medical_terms(
    begriffe: Annotated[list[str], typer.Argument(
        metavar=list_metavar("terms"),
        help=w("Begriffe, genau wie gespeichert.", "Terms, exactly as stored."))],
) -> None:
    """Delete terms from the vocabulary table."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.remove(begriffe)
    terms_module.standard.cache_clear()
    typer.echo(w(f"{n} Begriffe gelöscht.", f"{n} term(s) deleted."))


@medical_terms_app.command("exportieren",
                           help=w("Das Vokabular als CSV (begriff,kategorie) schreiben.",
                                  "Write the vocabulary as CSV (begriff,kategorie)."))
def export_medical_terms(
    file: Annotated[Path, typer.Argument(metavar=w("datei", "file"),
                                         help=w("Zieldatei (.csv).", "Target file (.csv)."))],
    category: Annotated[str, typer.Option("--kategorie",
                                          help=w("Nur diese Kategorie.",
                                                 "Only this category."))] = "",
) -> None:
    """Write the vocabulary (or one category of it) to a CSV file."""
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.export_csv(file, category)
    typer.echo(w(f"{n} Begriffe nach {file} geschrieben.", f"{n} term(s) written to {file}."))


@medical_terms_app.command("pruefen",
                           help=w("Prüfen, ob Wörter als Fachbegriffe geschützt sind.",
                                  "Check whether words are protected as medical terms."))
def check_medical_terms(
    woerter: Annotated[list[str], typer.Argument(
        metavar=list_metavar("words"),
        help=w("Wörter oder Wortfolgen.", "Words or sequences of words."))],
) -> None:
    """Print for each word whether the medical terms protect it wholly, partly or not."""
    from pseudonymed.detection import medical_terms as terms_module

    fb = terms_module.standard()
    for word in woerter:
        spans = fb.spans(word)
        whole = bool(spans) and fb.is_protected(word, 0, len(word), spans, whole=True)
        verdict = (w("geschützt", "protected") if whole
                   else w("teilweise", "partly protected") if spans
                   else w("frei", "not protected"))
        typer.echo(f"{word}: {verdict}")


@medical_terms_app.command("ergaenzen", help=w(
    "Begriffe einer Liste oder CSV in das Vokabular übernehmen (zusammenführen).",
    "Merge the terms of a list or CSV into the vocabulary."))
def merge_medical_terms(
    file: Annotated[Path, typer.Argument(
        metavar=w("datei", "file"), exists=True,
        help=w("Liste, ein Begriff je Zeile, z. B. eine <name>_pseudonym.vokabular.txt.",
               "List with one term per line, e.g. a <name>_pseudonym.vokabular.txt."))],
    name: Annotated[str, typer.Option("--name",
                                      help=w("Eigene Liste im Datenordner.",
                                             "Your own list in the data folder."))]
    = "nutzervokabular",
) -> None:
    """Merge a list or CSV of terms into the vocabulary."""
    from pseudonymed.detection import medical_terms as terms_module

    try:
        target, new = terms_module.merge_terms(file, name)
    except OSError as e:
        abort(error_message(e), cause=e)
    count = terms_module.standard().count
    typer.echo(w(f"{new} neue Begriffe nach {target} übernommen;"
                 f" jetzt {count} geschützte Begriffe",
                 f"{new} new term(s) added to {target};"
                 f" now {count} protected terms"))


@medical_terms_app.command("importieren", help=w(
    "Begriffe aus MeSH-XML (NLM), ICD-10-GM/Alpha-ID (csv) oder einer Liste übernehmen.",
    "Import terms from MeSH XML (NLM), ICD-10-GM/Alpha-ID (csv) or a list."))
def import_medical_terms(
    file: Annotated[Path, typer.Argument(metavar=w("datei", "file"), exists=True,
                                         help=w("Quelldatei.", "Source file."))],
    name: Annotated[str, typer.Option("--name",
                                      help=w("Name der Liste im Datenordner.",
                                             "Name of the list in the data folder."))],
    format_: Annotated[str, typer.Option("--format", help=w("liste, csv oder mesh-xml.",
                                                            "liste, csv or mesh-xml."))]
    = "liste",
    column: Annotated[int, typer.Option("--spalte", help=w("Spalte (0-basiert) bei csv.",
                                                           "Column (0-based) for csv."))] = 0,
    delimiter: Annotated[str, typer.Option("--trennzeichen",
                                           help=w("Trennzeichen bei csv.",
                                                  "Delimiter for csv."))]
    = ";",
) -> None:
    """Import terms from a MeSH XML file, a CSV column or a plain list."""
    from pseudonymed.detection import medical_terms as terms_module

    try:
        target, n = terms_module.import_file(file, name, format_, column, delimiter)
    except (ValueError, OSError) as e:
        abort(error_message(e), cause=e)
    count = terms_module.standard().count
    typer.echo(w(f"{n} Begriffe nach {target} übernommen (Kategorie {name}); jetzt"
                 f" {count} geschützte Begriffe",
                 f"{n} term(s) added to {target} (category {name}); now"
                 f" {count} protected terms"))


identifiers_app = typer.Typer(
    help=w("Identifikatoren außerhalb der Patientendaten: Berufe, Kliniken, Studien, Titel.",
           "Identifiers beyond the patient data: professions, hospitals, studies, titles."),
    no_args_is_help=True,
)
app.add_typer(identifiers_app, name="identifikatoren")


@identifiers_app.command("status", help=w("Umfang je Klasse anzeigen.",
                                          "Show the size of each class."))
def identifiers_status() -> None:
    """Print the number of identifiers per class and the deleted bundled entries."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        typer.echo(w(f"{ident.count()} Identifikatoren in {ident.path}",
                     f"{ident.count()} identifiers in {ident.path}"))
        for category, n in ident.categories():
            typer.echo(f"  {n:>7}  {category}")
        if ident.deleted():
            typer.echo(w(f"  {ident.deleted():>7}  gelöschte mitgelieferte Einträge",
                         f"  {ident.deleted():>7}  deleted bundled entries"))
    typer.echo(w("Registernummern (NCT, DRKS, ISRCTN, EU CT, EudraCT) werden immer erkannt;"
                 " Schritt „2 Identifikatoren“.",
                 "Registry numbers (NCT, DRKS, ISRCTN, EU CT, EudraCT) are always recognised;"
                 " step ‘2 Identifiers’."))


@identifiers_app.command("hinzufuegen", help=w(
    "Eigene Identifikatoren aufnehmen (Arbeitgeber, Vereine, Einrichtungen …).",
    "Add your own identifiers (employers, clubs, institutions …)."))
def add_identifiers(
    begriffe: Annotated[list[str], typer.Argument(
        metavar=list_metavar("terms"),
        help=w("Begriffe (auch mehrwortig).", "Terms (several words allowed)."))],
    category: Annotated[str, typer.Option(
        "--klasse", help=w("Klinik, Beruf, Studie, Titel oder Sonstiges (eigene Klassen wie"
                           " Sonstiges: überall geschwärzt).",
                           "Klinik, Beruf, Studie, Titel or Sonstiges (own classes such as"
                           " Sonstiges: redacted everywhere)."))] = "Sonstiges",
) -> None:
    """Add identifiers of one class to the identifier table."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.add(begriffe, category)
    typer.echo(w(f"{n} neue Identifikatoren ({category}).",
                 f"{n} new identifier(s) ({category})."))


@identifiers_app.command("entfernen", help=w(
    "Identifikatoren löschen (auch mitgelieferte; sie bleiben bei Updates gelöscht).",
    "Delete identifiers (bundled ones too; they stay deleted across updates)."))
def remove_identifiers(
    begriffe: Annotated[list[str], typer.Argument(
        metavar=list_metavar("terms"),
        help=w("Begriffe, wie gespeichert.", "Terms, as stored."))],
    category: Annotated[str, typer.Option("--klasse", help=w("Nur in dieser Klasse.",
                                                             "Only in this class."))] = "",
) -> None:
    """Delete identifiers, optionally only those of one class."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.remove(s for b in begriffe for s in ident.keys_for(b, category))
    typer.echo(w(f"{n} Identifikatoren gelöscht.", f"{n} identifier(s) deleted."))


@identifiers_app.command("exportieren", help=w(
    "Die Identifikatoren als CSV (begriff,klasse,quelle,angaben) schreiben.",
    "Write the identifiers as CSV (begriff,klasse,quelle,angaben)."))
def export_identifiers(
    file: Annotated[Path, typer.Argument(metavar=w("datei", "file"),
                                         help=w("Zieldatei (.csv).", "Target file (.csv)."))],
    category: Annotated[str, typer.Option("--klasse", help=w("Nur diese Klasse.",
                                                             "Only this class."))] = "",
) -> None:
    """Write the identifiers (or one class of them) to a CSV file."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.export_csv(file, category)
    typer.echo(w(f"{n} Identifikatoren nach {file} geschrieben.",
                 f"{n} identifier(s) written to {file}."))


@identifiers_app.command("importieren", help=w(
    "Identifikatoren aus einer CSV oder Liste übernehmen.",
    "Import identifiers from a CSV or a list."))
def import_identifiers(
    file: Annotated[Path, typer.Argument(metavar=w("datei", "file"), exists=True,
                                         help=w("CSV (begriff[,klasse]) oder Liste.",
                                                "CSV (begriff[,klasse]) or a list."))],
    category: Annotated[str, typer.Option("--klasse",
                                          help=w("Klasse ohne Klassenspalte.",
                                                 "Class where there is no class column."))]
    = "Sonstiges",
) -> None:
    """Import identifiers from a CSV file or a plain list."""
    from pseudonymed.detection import identifiers as ident_module

    try:
        with ident_module.IdentifierTable() as ident:
            new, read_in = ident.import_csv(file, category)
    except OSError as e:
        abort(error_message(e), cause=e)
    typer.echo(w(f"{new} neue von {read_in} Identifikatoren übernommen.",
                 f"{new} new of {read_in} identifiers added."))


@identifiers_app.command("pruefen", help=w(
    "Zeigen, was die Analyse in einem Satz als Identifikator schwärzen würde (Titel nur\n"
    "vor erkannten Personen, hier nicht geprüft).",
    "Show what the analysis would redact as an identifier in a sentence (titles only"
    " before recognised persons, not checked here)."))
def check_identifiers(
    texte: Annotated[list[str], typer.Argument(
        metavar=list_metavar("texts"),
        help=w("Sätze oder eine Textdatei.", "Sentences or a text file."))],
) -> None:
    """Print the identifier hits in each sentence or text file."""
    from pseudonymed.detection import identifiers as ident_module

    recogniser = ident_module.standard()
    for t in texte:
        if Path(t).is_file():
            t = Path(t).read_text(encoding="utf-8", errors="replace")
        matches = recogniser.candidates(t)
        typer.echo(w(f"{len(matches)} Treffer", f"{len(matches)} hit(s)") + "".join(
            f"\n  {k.category}: {t[k.start:k.end]}" for k in matches))
