"""Commands over the two word lists: the protected medical vocabulary and the identifiers."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pseudonymed.cli.basis import (
    abbruch,
    app,
)

fachbegriffe_app = typer.Typer(
    help="Medizinisches Fachvokabular, das NER und unscharfe OCR-Suche nicht anfassen dürfen.",
    no_args_is_help=True,
)
app.add_typer(fachbegriffe_app, name="fachbegriffe")


@fachbegriffe_app.command("status")
def fachbegriffe_status() -> None:
    """Umfang und Kategorien des Vokabulars anzeigen."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul
    from pseudonymed.erkennung import vokabular as vokmodul

    fb = fachmodul.standard()
    typer.echo(f"{fb.anzahl} geschützte Begriffe")
    for name, n in fb.quellen:
        typer.echo(f"  {n:>7}  {name}")
    typer.echo(f"Tabelle vokabular in {vokmodul.standardpfad()} (Schritt „3 Fachvokabular“,"
               " 'fachbegriffe hinzufuegen|entfernen|exportieren|importieren')")


@fachbegriffe_app.command("hinzufuegen")
def fachbegriffe_hinzufuegen(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe (auch mehrwortig).")],
    kategorie: Annotated[str, typer.Option("--kategorie", help="Kategorie in der Tabelle.")]
    = "Nutzervokabular",
) -> None:
    """Begriffe ins Vokabular aufnehmen."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul
    from pseudonymed.erkennung import vokabular as vokmodul

    with vokmodul.Vokabular() as v:
        n = v.fuege_hinzu(begriffe, kategorie)
    fachmodul.standard.cache_clear()
    typer.echo(f"{n} neue Begriffe ({kategorie}).")


@fachbegriffe_app.command("entfernen")
def fachbegriffe_entfernen(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe, genau wie gespeichert.")],
) -> None:
    """Begriffe aus dem Vokabular löschen (auch mitgelieferte)."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul
    from pseudonymed.erkennung import vokabular as vokmodul

    with vokmodul.Vokabular() as v:
        n = v.entferne(begriffe)
    fachmodul.standard.cache_clear()
    typer.echo(f"{n} Begriffe gelöscht.")


@fachbegriffe_app.command("exportieren")
def fachbegriffe_exportieren(
    datei: Annotated[Path, typer.Argument(help="Zieldatei (.csv).")],
    kategorie: Annotated[str, typer.Option("--kategorie", help="Nur diese Kategorie.")] = "",
) -> None:
    """Das Vokabular als CSV (begriff,kategorie) schreiben."""
    from pseudonymed.erkennung import vokabular as vokmodul

    with vokmodul.Vokabular() as v:
        n = v.exportiere_csv(datei, kategorie)
    typer.echo(f"{n} Begriffe nach {datei} geschrieben.")


@fachbegriffe_app.command("pruefen")
def fachbegriffe_pruefen(
    woerter: Annotated[list[str], typer.Argument(help="Wörter oder Wortfolgen.")],
) -> None:
    """Prüfen, ob Wörter als Fachbegriffe geschützt sind."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul

    fb = fachmodul.standard()
    for w in woerter:
        spannen = fb.spannen(w)
        ganz = bool(spannen) and fb.geschuetzt(w, 0, len(w), spannen, ganz=True)
        typer.echo(f"{w}: {'geschützt' if ganz else ('teilweise' if spannen else 'frei')}")


@fachbegriffe_app.command("ergaenzen")
def fachbegriffe_ergaenzen(
    datei: Annotated[Path, typer.Argument(
        exists=True, help="Liste, ein Begriff je Zeile, z. B. eine"
                          " <name>_pseudonym.vokabular.txt.")],
    name: Annotated[str, typer.Option("--name", help="Eigene Liste im Datenordner.")]
    = "nutzervokabular",
) -> None:
    """Begriffe einer Liste oder CSV in das Vokabular übernehmen (zusammenführen)."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul

    try:
        ziel, neu = fachmodul.ergaenze(datei, name)
    except OSError as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"{neu} neue Begriffe nach {ziel} übernommen; jetzt {fachmodul.standard().anzahl}"
               " geschützte Begriffe")


@fachbegriffe_app.command("importieren")
def fachbegriffe_importieren(
    datei: Annotated[Path, typer.Argument(exists=True, help="Quelldatei.")],
    name: Annotated[str, typer.Option("--name", help="Name der Liste im Datenordner.")],
    format_: Annotated[str, typer.Option("--format", help="liste, csv oder mesh-xml.")]
    = "liste",
    spalte: Annotated[int, typer.Option("--spalte", help="Spalte (0-basiert) bei csv.")] = 0,
    trennzeichen: Annotated[str, typer.Option("--trennzeichen", help="Trennzeichen bei csv.")]
    = ";",
) -> None:
    """Begriffe aus MeSH-XML (NLM), ICD-10-GM/Alpha-ID (csv) oder einer Liste übernehmen."""
    from pseudonymed.erkennung import fachbegriffe as fachmodul

    try:
        ziel, n = fachmodul.importiere(datei, name, format_, spalte, trennzeichen)
    except (ValueError, OSError) as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"{n} Begriffe nach {ziel} übernommen (Kategorie {name}); jetzt"
               f" {fachmodul.standard().anzahl} geschützte Begriffe")


identifikatoren_app = typer.Typer(
    help="Identifikatoren außerhalb der Patientendaten: Berufe, Kliniken, Studien, Titel.",
    no_args_is_help=True,
)
app.add_typer(identifikatoren_app, name="identifikatoren")


@identifikatoren_app.command("status")
def identifikatoren_status() -> None:
    """Umfang je Klasse anzeigen."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    with identmodul.Identifikatoren() as ident:
        typer.echo(f"{ident.anzahl()} Identifikatoren in {ident.pfad}")
        for klasse, n in ident.klassen():
            typer.echo(f"  {n:>7}  {klasse}")
        if ident.geloescht():
            typer.echo(f"  {ident.geloescht():>7}  gelöschte mitgelieferte Einträge")
    typer.echo("Registernummern (NCT, DRKS, ISRCTN, EU CT, EudraCT) werden immer erkannt;"
               " Schritt „2 Identifikatoren“.")


@identifikatoren_app.command("hinzufuegen")
def identifikatoren_hinzufuegen(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe (auch mehrwortig).")],
    klasse: Annotated[str, typer.Option(
        "--klasse", help="Klinik, Beruf, Studie, Titel oder Sonstiges (eigene Klassen wie"
                         " Sonstiges: überall geschwärzt).")] = "Sonstiges",
) -> None:
    """Eigene Identifikatoren aufnehmen (Arbeitgeber, Vereine, Einrichtungen …)."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    with identmodul.Identifikatoren() as ident:
        n = ident.fuege_hinzu(begriffe, klasse)
    typer.echo(f"{n} neue Identifikatoren ({klasse}).")


@identifikatoren_app.command("entfernen")
def identifikatoren_entfernen(
    begriffe: Annotated[list[str], typer.Argument(help="Begriffe, wie gespeichert.")],
    klasse: Annotated[str, typer.Option("--klasse", help="Nur in dieser Klasse.")] = "",
) -> None:
    """Identifikatoren löschen (auch mitgelieferte; sie bleiben bei Updates gelöscht)."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    with identmodul.Identifikatoren() as ident:
        n = ident.entferne(s for b in begriffe for s in ident.schluessel_fuer(b, klasse))
    typer.echo(f"{n} Identifikatoren gelöscht.")


@identifikatoren_app.command("exportieren")
def identifikatoren_exportieren(
    datei: Annotated[Path, typer.Argument(help="Zieldatei (.csv).")],
    klasse: Annotated[str, typer.Option("--klasse", help="Nur diese Klasse.")] = "",
) -> None:
    """Die Identifikatoren als CSV (begriff,klasse,quelle,angaben) schreiben."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    with identmodul.Identifikatoren() as ident:
        n = ident.exportiere_csv(datei, klasse)
    typer.echo(f"{n} Identifikatoren nach {datei} geschrieben.")


@identifikatoren_app.command("importieren")
def identifikatoren_importieren(
    datei: Annotated[Path, typer.Argument(exists=True, help="CSV (begriff[,klasse]) oder Liste.")],
    klasse: Annotated[str, typer.Option("--klasse", help="Klasse ohne Klassenspalte.")]
    = "Sonstiges",
) -> None:
    """Identifikatoren aus einer CSV oder Liste übernehmen."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    try:
        with identmodul.Identifikatoren() as ident:
            neu, gelesen = ident.importiere_csv(datei, klasse)
    except OSError as e:
        abbruch(str(e), ursache=e)
    typer.echo(f"{neu} neue von {gelesen} Identifikatoren übernommen.")


@identifikatoren_app.command("pruefen")
def identifikatoren_pruefen(
    texte: Annotated[list[str], typer.Argument(help="Sätze oder eine Textdatei.")],
) -> None:
    """Zeigen, was die Analyse in einem Satz als Identifikator schwärzen würde (Titel nur
    vor erkannten Personen, hier nicht geprüft)."""
    from pseudonymed.erkennung import identifikatoren as identmodul

    erkenner = identmodul.standard()
    for t in texte:
        if Path(t).is_file():
            t = Path(t).read_text(encoding="utf-8", errors="replace")
        treffer = erkenner.kandidaten(t)
        typer.echo(f"{len(treffer)} Treffer" + "".join(
            f"\n  {k.klasse}: {t[k.start:k.ende]}" for k in treffer))
