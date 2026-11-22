"""Commands over the two word lists: the protected medical vocabulary and the identifiers."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pseudonymed.cli.base import (
    abort,
    app,
)

medical_terms_app = typer.Typer(
    help="Medizinisches Fachvokabular, das NER und unscharfe OCR-Suche nicht anfassen dürfen.",
    no_args_is_help=True,
)
app.add_typer(medical_terms_app, name="fachbegriffe")


@medical_terms_app.command("status")
def medical_terms_status() -> None:
    """Umfang und Kategorien des Vokabulars anzeigen."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    fb = terms_module.standard()
    typer.echo(f"{fb.count} geschützte Begriffe")
    for name, n in fb.sources:
        typer.echo(f"  {n:>7}  {name}")
    typer.echo(f"Tabelle vokabular in {vocab_mod.default_path()} (Schritt „3 Fachvokabular“,"
               " 'fachbegriffe hinzufuegen|entfernen|exportieren|importieren')")


@medical_terms_app.command("hinzufuegen")
def add_medical_terms(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe (auch mehrwortig).")],
    category: Annotated[str, typer.Option("--kategorie", help="Kategorie in der Tabelle.")]
    = "Nutzervokabular",
) -> None:
    """Begriffe ins Vokabular aufnehmen."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.add(begriffe, category)
    terms_module.standard.cache_clear()
    typer.echo(f"{n} neue Begriffe ({category}).")


@medical_terms_app.command("entfernen")
def remove_medical_terms(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe, genau wie gespeichert.")],
) -> None:
    """Begriffe aus dem Vokabular löschen (auch mitgelieferte)."""
    from pseudonymed.detection import medical_terms as terms_module
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.remove(begriffe)
    terms_module.standard.cache_clear()
    typer.echo(f"{n} Begriffe gelöscht.")


@medical_terms_app.command("exportieren")
def export_medical_terms(
    file: Annotated[Path, typer.Argument(metavar="datei", help="Zieldatei (.csv).")],
    category: Annotated[str, typer.Option("--kategorie", help="Nur diese Kategorie.")] = "",
) -> None:
    """Das Vokabular als CSV (begriff,kategorie) schreiben."""
    from pseudonymed.detection import vocabulary as vocab_mod

    with vocab_mod.Vocabulary() as v:
        n = v.export_csv(file, category)
    typer.echo(f"{n} Begriffe nach {file} geschrieben.")


@medical_terms_app.command("pruefen")
def check_medical_terms(
    woerter: Annotated[list[str], typer.Argument(help="Wörter oder Wortfolgen.")],
) -> None:
    """Prüfen, ob Wörter als Fachbegriffe geschützt sind."""
    from pseudonymed.detection import medical_terms as terms_module

    fb = terms_module.standard()
    for w in woerter:
        spans = fb.spans(w)
        whole = bool(spans) and fb.is_protected(w, 0, len(w), spans, whole=True)
        typer.echo(f"{w}: {'geschützt' if whole else ('teilweise' if spans else 'frei')}")


@medical_terms_app.command("ergaenzen")
def merge_medical_terms(
    file: Annotated[Path, typer.Argument(metavar="datei", 
        exists=True, help="Liste, ein Begriff je Zeile, z. B. eine"
                          " <name>_pseudonym.vokabular.txt.")],
    name: Annotated[str, typer.Option("--name", help="Eigene Liste im Datenordner.")]
    = "nutzervokabular",
) -> None:
    """Begriffe einer Liste oder CSV in das Vokabular übernehmen (zusammenführen)."""
    from pseudonymed.detection import medical_terms as terms_module

    try:
        target, new = terms_module.merge_terms(file, name)
    except OSError as e:
        abort(str(e), cause=e)
    typer.echo(f"{new} neue Begriffe nach {target} übernommen;"
               f" jetzt {terms_module.standard().count} geschützte Begriffe")


@medical_terms_app.command("importieren")
def import_medical_terms(
    file: Annotated[Path, typer.Argument(metavar="datei", exists=True, help="Quelldatei.")],
    name: Annotated[str, typer.Option("--name", help="Name der Liste im Datenordner.")],
    format_: Annotated[str, typer.Option("--format", help="liste, csv oder mesh-xml.")]
    = "liste",
    column: Annotated[int, typer.Option("--spalte", help="Spalte (0-basiert) bei csv.")] = 0,
    delimiter: Annotated[str, typer.Option("--trennzeichen", help="Trennzeichen bei csv.")]
    = ";",
) -> None:
    """Begriffe aus MeSH-XML (NLM), ICD-10-GM/Alpha-ID (csv) oder einer Liste übernehmen."""
    from pseudonymed.detection import medical_terms as terms_module

    try:
        target, n = terms_module.import_file(file, name, format_, column, delimiter)
    except (ValueError, OSError) as e:
        abort(str(e), cause=e)
    typer.echo(f"{n} Begriffe nach {target} übernommen (Kategorie {name}); jetzt"
               f" {terms_module.standard().count} geschützte Begriffe")


identifiers_app = typer.Typer(
    help="Identifikatoren außerhalb der Patientendaten: Berufe, Kliniken, Studien, Titel.",
    no_args_is_help=True,
)
app.add_typer(identifiers_app, name="identifikatoren")


@identifiers_app.command("status")
def identifiers_status() -> None:
    """Umfang je Klasse anzeigen."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        typer.echo(f"{ident.count()} Identifikatoren in {ident.path}")
        for category, n in ident.categories():
            typer.echo(f"  {n:>7}  {category}")
        if ident.deleted():
            typer.echo(f"  {ident.deleted():>7}  gelöschte mitgelieferte Einträge")
    typer.echo("Registernummern (NCT, DRKS, ISRCTN, EU CT, EudraCT) werden immer erkannt;"
               " Schritt „2 Identifikatoren“.")


@identifiers_app.command("hinzufuegen")
def add_identifiers(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe (auch mehrwortig).")],
    category: Annotated[str, typer.Option(
        "--klasse", help="Klinik, Beruf, Studie, Titel oder Sonstiges (eigene Klassen wie"
                         " Sonstiges: überall geschwärzt).")] = "Sonstiges",
) -> None:
    """Eigene Identifikatoren aufnehmen (Arbeitgeber, Vereine, Einrichtungen …)."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.add(begriffe, category)
    typer.echo(f"{n} neue Identifikatoren ({category}).")


@identifiers_app.command("entfernen")
def remove_identifiers(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe, wie gespeichert.")],
    category: Annotated[str, typer.Option("--klasse", help="Nur in dieser Klasse.")] = "",
) -> None:
    """Identifikatoren löschen (auch mitgelieferte; sie bleiben bei Updates gelöscht)."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.remove(s for b in begriffe for s in ident.keys_for(b, category))
    typer.echo(f"{n} Identifikatoren gelöscht.")


@identifiers_app.command("exportieren")
def export_identifiers(
    file: Annotated[Path, typer.Argument(metavar="datei", help="Zieldatei (.csv).")],
    category: Annotated[str, typer.Option("--klasse", help="Nur diese Klasse.")] = "",
) -> None:
    """Die Identifikatoren als CSV (begriff,klasse,quelle,angaben) schreiben."""
    from pseudonymed.detection import identifiers as ident_module

    with ident_module.IdentifierTable() as ident:
        n = ident.export_csv(file, category)
    typer.echo(f"{n} Identifikatoren nach {file} geschrieben.")


@identifiers_app.command("importieren")
def import_identifiers(
    file: Annotated[Path, typer.Argument(metavar="datei", exists=True,
                                         help="CSV (begriff[,klasse]) oder Liste.")],
    category: Annotated[str, typer.Option("--klasse", help="Klasse ohne Klassenspalte.")]
    = "Sonstiges",
) -> None:
    """Identifikatoren aus einer CSV oder Liste übernehmen."""
    from pseudonymed.detection import identifiers as ident_module

    try:
        with ident_module.IdentifierTable() as ident:
            new, read_in = ident.import_csv(file, category)
    except OSError as e:
        abort(str(e), cause=e)
    typer.echo(f"{new} neue von {read_in} Identifikatoren übernommen.")


@identifiers_app.command("pruefen")
def check_identifiers(
    texte: Annotated[list[str], typer.Argument(help="Sätze oder eine Textdatei.")],
) -> None:
    """Zeigen, was die Analyse in einem Satz als Identifikator schwärzen würde (Titel nur
    vor erkannten Personen, hier nicht geprüft)."""
    from pseudonymed.detection import identifiers as ident_module

    recogniser = ident_module.standard()
    for t in texte:
        if Path(t).is_file():
            t = Path(t).read_text(encoding="utf-8", errors="replace")
        matches = recogniser.candidates(t)
        typer.echo(f"{len(matches)} Treffer" + "".join(
            f"\n  {k.category}: {t[k.start:k.end]}" for k in matches))
