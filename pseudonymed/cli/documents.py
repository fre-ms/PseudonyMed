"""Commands over documents: pseudonymise, check, re-identify, list pseudonyms."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import environment
from pseudonymed import working_state as state_mod
from pseudonymed.checks.report import as_text
from pseudonymed.cli.base import (
    ConfigOption,
    ContactOption,
    NewOption,
    NumberOption,
    PatientOption,
    SameOption,
    SchemaOption,
    StoreOption,
    VcfOption,
    abort,
    app,
    check_choice,
    matching_decision,
    open_store,
    report_uncertain,
    resolve_patient,
)
from pseudonymed.config import (
    load_settings,
)
from pseudonymed.core.model import Patient
from pseudonymed.document import optdoc_adapter
from pseudonymed.document.optdoc_adapter import OCRError
from pseudonymed.document.text_file import is_text_file
from pseudonymed.pipeline import (
    PipelineOptions,
    Preparation,
    Result,
    TextPreparation,
    analyse,
    finalise,
    prepare,
)
from pseudonymed.pseudonyms.pgp import Certificate
from pseudonymed.pseudonyms.reidentification import Reidentification
from pseudonymed.pseudonyms.store import (
    PseudonymStore,
    UncertainMatch,
    one_time_store,
)
from pseudonymed.redaction.pdf import RedactionOptions

Prepared = Preparation | TextPreparation


@dataclass(frozen=True)
class _Run:
    """What every input of one ``pseudonymise`` call shares."""

    opt: PipelineOptions
    patient: Patient | None  # None when every input is a saved state with its own patient
    working_state: bool
    reanalyse: bool


def _load_recipient(recipient: str | None) -> Certificate | None:
    """The recipient's public key when one was named; a bad key ends the command."""
    if not recipient:
        return None
    from pseudonymed.pseudonyms import pgp as pgp_module

    try:
        certificate = pgp_module.load_certificate(recipient)
    except pgp_module.PGPError as e:
        abort(f"Empfänger-Schlüssel: {e}", cause=e)
    typer.echo(f"Empfänger: {certificate.description()}", err=True)
    return certificate


def _check_choices(paper: str, unreadable: str, date: str, preview: str,
                   clean_up: str) -> None:
    """End the command when one of the free-text options holds an unknown value."""
    try:
        optdoc_adapter.paper_size(paper)
    except ValueError as e:
        abort(optdoc_adapter.message(e), cause=e)
    check_choice("--unlesbar", unreadable, ("schwaerzen", "lassen"))
    check_choice("--datum", date, ("jahr", "alter"))
    check_choice("--vorschau", preview, ("auto", "immer", "nie"))
    check_choice("--aufbereiten", clean_up, ("auto", "aus"))


def _recipient_fields(certificate: Certificate | None) -> tuple[str, str]:
    """The recipient's armoured key and fingerprint for the options, empty without one."""
    if certificate is None:
        return "", ""
    return certificate.armor, certificate.fingerprint


def _risk_fields(reference_population: int | None, own_places: str | None) -> tuple[int, str]:
    """Reference population and the sender's places: the option, else the configuration."""
    settings = load_settings()
    return (reference_population or settings.reference_population,
            own_places if own_places is not None else settings.own_places)


def _warn_about_notes(p: Patient | None) -> None:
    """Warn that notes on the contact card are not redacted by themselves."""
    if p is not None and p.notes:
        typer.echo(
            "Hinweis: Die Kontaktkarte enthält Notizen. Angehörige, Zuweiser oder Begriffe daraus"
            " werden nicht automatisch geschwärzt; bei Bedarf 'kontakte export' nutzen und die"
            " Patientendatei ergänzen.", err=True,
        )


def _from_working_state(state: Path, opt: PipelineOptions, sp: PseudonymStore,
                        reanalyse: bool) -> tuple[Prepared, Patient]:
    """Phase one from a saved state: restored as saved, or searched again with the
    current options while the user's decisions are carried over."""
    a = state_mod.read(state)
    if reanalyse and a.is_text:
        assert a.document_path is not None
        v: Prepared = prepare(a.document_path, a.patient, opt, store=sp)
        state_mod.adopt_decisions(a, v)
    elif reanalyse:
        v = analyse(state_mod.reading_from(a, opt), a.patient, opt, store=sp)
        state_mod.adopt_decisions(a, v)
    else:
        v = state_mod.preparation_from(a, store=sp, opt=opt)
    typer.echo(f"Arbeitsstand {state.name}: {len(v.hits)} Treffer,"
               f" {len(v.discarded)} verworfen", err=True)
    return v, a.patient


def _from_file(file: Path, p: Patient, opt: PipelineOptions, sp: PseudonymStore,
               working_state: bool) -> Prepared:
    """Phase one from a document or text, the working state saved beside it when asked."""
    v = prepare(file, p, opt, store=sp)
    if working_state:
        folder = opt.target_folder or file.parent
        doc_path, json_path = state_mod.save(v, folder / (file.stem + state_mod.EXTENSION))
        typer.echo(f"Arbeitsstand: {json_path}  (+ {doc_path.name}; enthält Klardaten)")
    return v


def _report_paths(res: Result) -> None:
    """The side files of a released result: capsule, text, Markdown."""
    if res.target_capsule:
        typer.echo(f"Kapsel:      {res.target_capsule}  (nur an den Empfänger geben)")
    if res.target_txt and not res.is_text:
        typer.echo(f"Text:        {res.target_txt}")
    if res.target_md:
        typer.echo(f"Markdown:    {res.target_md}")


def _report_result(res: Result, file: Path, person: Patient) -> bool:
    """Print the report and the paths; True when the file was released."""
    typer.echo(as_text(res.report))
    if res.preview:
        typer.echo(f"Vorschau:    {res.preview[0].parent} ({len(res.preview)} Seiten)")
    if res.released:
        typer.echo(f"Freigegeben: {res.target}")
        _report_paths(res)
    else:
        typer.echo(f"NICHT freigegeben: {file.name}", err=True)
    if res.target_report:
        typer.echo(f"Bericht:     {res.target_report}")
    word = "Patientin" if person.female else "Patient"
    typer.echo(f"Pseudonym der Hauptperson: {word} {res.code}\n")
    return res.released


def _process(file: Path, state: Path | None, run: _Run, sp: PseudonymStore) -> bool:
    """One input from phase one to the printed report; False when it was not released
    or could not be processed.  An uncertain match ends the whole command."""
    person = run.patient
    try:
        if state is not None:
            v, person = _from_working_state(state, run.opt, sp, run.reanalyse)
        else:
            assert person is not None  # a plain file always comes with a patient
            v = _from_file(file, person, run.opt, sp, run.working_state)
        res = finalise(v)
    except UncertainMatch as e:
        assert person is not None
        report_uncertain(person, e)
        raise typer.Exit(code=4) from e
    except (ValueError, NotImplementedError, OCRError, state_mod.WorkingStateError) as e:
        typer.echo(f"{file.name}: {optdoc_adapter.message(e)}", err=True)
        return False
    assert person is not None
    return _report_result(res, file, person)


@app.command("pseudonymisieren")
def pseudonymise(
    dateien: Annotated[list[Path], typer.Argument(
        help="PDF-, Bild- oder Textdateien (.txt, .md, .srt, .vtt, Whisper-.json, .eml).",
        exists=True)],
    patient: PatientOption = None,
    vcf: VcfOption = None,
    contact: ContactOption = None,
    num: NumberOption = None,
    config: ConfigOption = None,
    target: Annotated[Path | None, typer.Option("--ziel", "-z", help="Zielordner.")] = None,
    date: Annotated[str, typer.Option("--datum", help="Geburtsdatum als 'jahr' oder 'alter'.")]
    = "jahr",
    rasterise: Annotated[bool, typer.Option("--rastern", help="Ausgabe vollständig rastern.")]
    = False,
    dpi: Annotated[int, typer.Option("--dpi", help="Auflösung beim Rastern.")] = 200,
    text_file: Annotated[bool, typer.Option("--text/--kein-text",
                                            help="Textdatei in Lesereihenfolge schreiben.")] = True,
    markdown: Annotated[bool, typer.Option("--markdown/--kein-markdown",
                                           help="Zusätzlich eine Markdown-Datei schreiben.")]
    = False,
    text_layer: Annotated[bool, typer.Option(
        "--textebene/--keine-textebene",
        help="OCR-Text unsichtbar in Scans einbetten (durchsuchbares PDF).")] = True,
    report: Annotated[bool, typer.Option("--bericht/--kein-bericht",
                                         help="Prüfbericht als JSON schreiben.")] = True,
    notice: Annotated[bool, typer.Option("--vermerk/--ohne-vermerk",
                                         help="Vermerk in den PDF-Metadaten.")] = True,
    store: StoreOption = None,
    schema: SchemaOption = "hash",
    new: NewOption = False,
    same: SameOption = None,
    ocr: Annotated[str, typer.Option("--ocr", help="OCR für Scans/Fotos: auto, rapidocr,"
                                                  " tesseract oder aus.")] = "auto",
    codes: Annotated[bool, typer.Option("--codes/--keine-codes",
                                        help="Barcodes und QR-Codes schwärzen.")] = True,
    preview: Annotated[str, typer.Option("--vorschau", help="Vorschaubilder: auto (nur bei OCR),"
                                                          " immer oder nie.")] = "auto",
    unreadable: Annotated[str, typer.Option(
        "--unlesbar", help="Von der OCR nicht lesbare Zeilen: schwaerzen oder lassen.")]
    = "schwaerzen",
    ner: Annotated[bool, typer.Option("--ner/--kein-ner", help="Unbekannte Personen und Orte"
                                                              " per NER suchen (Extra 'ner').")]
    = False,
    medical_terms: Annotated[bool, typer.Option(
        "--fachbegriffe/--keine-fachbegriffe",
        help="Medizinische Fachbegriffe vor NER und unscharfer OCR-Suche schützen.")] = True,
    identifiers: Annotated[bool, typer.Option(
        "--identifikatoren/--keine-identifikatoren",
        help="Berufe, Kliniken, Studien und Titel schwärzen (Tabelle 'identifikatoren').")]
    = True,
    drugs: Annotated[bool, typer.Option(
        "--medikamente-schwaerzen/--medikamente-lassen",
        help="Medikamente: die Substanz (Wirkstoff, Handelsname) schwärzen. Standard: stehen"
             " lassen, medizinischer Inhalt.")] = False,
    doses: Annotated[bool, typer.Option(
        "--dosierungen-schwaerzen/--dosierungen-lassen",
        help="Medikamente: Dosierung und Verordnungsschema neben der Substanz schwärzen"
             " („500 mg 1-0-1“, „bei Bedarf“). Standard: stehen lassen.")] = False,
    lab_tests: Annotated[bool, typer.Option(
        "--laborwerte-schwaerzen/--laborwerte-lassen",
        help="Laborwerte: die Untersuchung (den Parameter, etwa „TSH“) schwärzen. Standard:"
             " stehen lassen.")] = False,
    measurements: Annotated[bool, typer.Option(
        "--messwerte-schwaerzen/--messwerte-lassen",
        help="Laborwerte: den Messwert hinter der Untersuchung schwärzen („2,3 mU/l“)."
             " Standard: stehen lassen.")] = False,
    fuzzy: Annotated[bool, typer.Option(
        "--unscharf/--genau",
        help="Textdateien: abweichend geschriebene Namen unscharf suchen (Transkripte).")] = True,
    clean_up: Annotated[str, typer.Option(
        "--aufbereiten", help="Scans und Fotos vor der OCR aufbereiten (begradigen, Hintergrund,"
                              " Hochskalieren): auto oder aus.")] = "auto",
    paper: Annotated[str, typer.Option(
        "--papier", help="Seitengröße für Fotos und Bilder: a4, a5, a3, letter, legal oder bild"
                         " (so groß wie das Bild).")] = "a4",
    tables: Annotated[bool, typer.Option(
        "--tabellen/--keine-tabellen",
        help="Tesseract: Tabellengitter erkennen und die Zellen ohne Linien erneut lesen.")]
    = True,
    vocabulary: Annotated[bool, typer.Option(
        "--vokabular/--kein-vokabular",
        help="Verworfene Rate-Treffer als <name>_pseudonym.vokabular.txt schreiben.")] = True,
    working_state: Annotated[bool, typer.Option(
        "--arbeitsstand",
        help="Vor dem Abschluss den Arbeitsstand neben die Ausgabe schreiben"
             " (<name>.arbeitsstand.pdf + .json, mit Klardaten): wieder aufnehmbar in der"
             " Oberfläche oder als Eingabe dieses Befehls.")] = False,
    reanalyse: Annotated[bool, typer.Option(
        "--neu-analysieren",
        help="Einen Arbeitsstand als Eingabe mit den jetzigen Erkennungsoptionen erneut"
             " durchsuchen (Aufbereitung, Texterkennung, Kästen, Masken, Aufnahmen und"
             " verworfene Treffer bleiben); ohne den Schalter wird er abgeschlossen, wie er"
             " gespeichert wurde.")] = False,
    structured: Annotated[bool, typer.Option(
        "--gliedern/--roh",
        help="OCR-Text als Markdown gliedern: Absätze zusammenfügen, Überschriften, Listen,"
             " Tabellen.")] = True,
    reference_population: Annotated[int | None, typer.Option(
        "--referenzpopulation",
        help="Restrisiko: aus wie vielen Menschen ein Angreifer wählt (Standard aus der"
             " Konfiguration, sonst 83.600.000 für Deutschland).")] = None,
    own_places: Annotated[str | None, typer.Option(
        "--eigene-orte", help="Restrisiko: Orte des Absenders (Sprechstunde, Klinik), die nicht"
                              " als Patientenmerkmal zählen; Komma-getrennt.")] = None,
    recipient: Annotated[str | None, typer.Option(
        "--empfaenger",
        help="Öffentlicher OpenPGP-Schlüssel des Empfängers (Datei oder https-Adresse):"
             " Pseudonyme mit Einmalschlüssel, Zuordnung als verschlüsselte Kapsel"
             " <name>_pseudonym.kapsel.asc für den Empfänger; lokal wird nichts gespeichert.")]
    = None,
) -> None:
    """Dokumente und Texte schwärzen, Pseudonyme einsetzen, Gegenprobe, Bericht."""
    certificate = _load_recipient(recipient)
    _check_choices(paper, unreadable, date, preview, clean_up)
    states = {file: state_mod.is_working_state(file) for file in dateien}
    if certificate and any(states.values()):
        abort("Ein Arbeitsstand lässt sich nicht für einen Empfänger abschließen"
              " (seine Codes stammen aus der lokalen Datenbank).")
    p = (resolve_patient(patient, vcf, contact, num, config)
         if not all(states.values()) else None)  # saved states carry their patient
    _warn_about_notes(p)
    recipient_armor, recipient_fingerprint = _recipient_fields(certificate)
    reference, places = _risk_fields(reference_population, own_places)
    opt = PipelineOptions(
        date_format=date, text_file=text_file, markdown=markdown, report=report,
        target_folder=target,
        matching=matching_decision(new, same), ocr=ocr, codes=codes, preview=preview,
        unreadable=unreadable, ner=ner, medical_terms=medical_terms, identifiers=identifiers,
        redact_drugs=drugs, redact_doses=doses,
        redact_lab_tests=lab_tests, redact_measurements=measurements,
        fuzzy_text=fuzzy,
        clean_up=clean_up, paper_size=paper, ocr_tables=tables,
        vocabulary=vocabulary, structured=structured,
        reference_population=reference, own_places=places,
        recipient=recipient_armor, recipient_fingerprint=recipient_fingerprint,
        redaction=RedactionOptions(rasterise=rasterise, dpi=dpi, notice=notice,
                                   text_layer=text_layer),
    )
    run = _Run(opt, p, working_state, reanalyse)
    with (one_time_store(schema) if certificate else open_store(store, schema)) as sp:
        free = [_process(file, states[file], run, sp) for file in dateien]
    if not all(free):
        raise typer.Exit(code=2)

# the command's name until November 2026, kept for existing scripts
app.command("anonymisieren", hidden=True)(pseudonymise)


@app.command("pruefen")
def check(
    file: Annotated[Path, typer.Argument(metavar="datei", exists=True,
                                         help="Bereits pseudonymisierte PDF- oder Textdatei.")],
    patient: PatientOption = None,
    vcf: VcfOption = None,
    contact: ContactOption = None,
    num: NumberOption = None,
    config: ConfigOption = None,
    store: StoreOption = None,
    schema: SchemaOption = "hash",
    new: NewOption = False,
    same: SameOption = None,
) -> None:
    """Gegenprobe an einer vorhandenen Datei: sind noch Identifikatoren enthalten?"""
    import pymupdf

    from pseudonymed.checks.recheck import check
    from pseudonymed.detection.rules import rules_for
    from pseudonymed.pseudonyms.assignment import PseudonymAssignment

    p = resolve_patient(patient, vcf, contact, num, config)
    with open_store(store, schema) as sp:
        try:
            code = sp.code_for(p, matching_decision(new, same)).code
        except UncertainMatch as e:
            report_uncertain(p, e)
            raise typer.Exit(code=4) from e
        secret = sp.secret
    rules = rules_for(p, PseudonymAssignment(p, code, secret=secret))
    # the patient's own code may look like a file number ("AZ…"): never a leftover
    if is_text_file(file):
        from pseudonymed.pipeline import recheck_text_file

        g = recheck_text_file(file, rules, pseudonyms=[code])
    else:
        with pymupdf.open(file) as doc:
            g = check(doc, rules, pseudonyms=[code])
    if g.passed:
        typer.echo(f"{file.name}: Gegenprobe bestanden.")
        return
    typer.echo(f"{file.name}: Gegenprobe NICHT bestanden ({len(g.residual_matches)} Resttreffer).")
    for r in g.residual_matches[:50]:
        typer.echo(f"  Seite {r['seite']}: {r['regel']} ({r['quelle']})")
    for a in g.remarks:
        typer.echo(f"  Anmerkung: {a}")
    raise typer.Exit(code=2)


def _mapping_from_capsule(
    capsule: Path, private_key: Path | None, passphrase: str | None,
    store: Path | None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """Open a sender's capsule: its entry, its mapping and its code; a matching local
    entry is mentioned."""
    from pseudonymed.pseudonyms import capsule as capsule_module
    from pseudonymed.pseudonyms.matching import match_candidates
    from pseudonymed.pseudonyms.pgp import PGPError

    try:
        k = capsule_module.open_file(capsule, private_key, passphrase)
    except (PGPError, capsule_module.CapsuleError, OSError) as e:
        abort(f"Kapsel: {e}", cause=e)
    typer.echo(f"Kapsel: {k.description()}", err=True)
    info, mapping, known = k.info(), k.mappings, [k.code]
    try:
        with open_store(store) as sp:
            same_entries = [v for v in match_candidates(k.as_patient(), sp.candidates()) if v.same]
    except OSError:
        same_entries = []
    if same_entries:
        typer.echo(f"Entspricht dem lokalen Eintrag {same_entries[0].code}"
                   f" ({same_entries[0].surname}, {same_entries[0].first_name}).", err=True)
    return info, mapping, known


def _mapping_from_store(
    code: str | None, search: str | None, store: Path | None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """The one local entry that matches: its row, its mapping, every known code."""
    with open_store(store) as sp:
        matches = sp.find(code_=code, search=search)
        if len(matches) != 1:
            abort(f"{len(matches)} Einträge gefunden; bitte eindeutig wählen"
                  " (siehe 'pseudonyme').")
        info = matches[0]
        return info, sp.mapping(info["schluessel"]), sp.codes()


def _output_reidentification(r: Reidentification, output: Path | None) -> None:
    """Write or print the re-identified text; unresolved pseudonyms end with code 3."""
    if output:
        output.write_text(r.text, encoding="utf-8")
        typer.echo(f"{r.replaced} Ersetzungen, geschrieben nach {output}", err=True)
    else:
        typer.echo(r.text, nl=False)
    if r.remaining:
        typer.echo("Nicht aufgelöst: " + ", ".join(r.remaining), err=True)
        for rest, note in r.notes.items():
            typer.echo(f"  {rest}: {note}", err=True)
        raise typer.Exit(code=3)


@app.command("rueckersetzen")
def reidentify(
    text_file: Annotated[Path | None, typer.Argument(metavar="textdatei", 
        help="Text des Sprachmodells (sonst Standardeingabe).")] = None,
    code: Annotated[str | None, typer.Option(
        "--kennung", "-k", "--buchstabe", help="Pseudonym-Kennung, z. B. K7Q4MX oder A.")] = None,
    search: Annotated[str | None, typer.Option("--suche", "-s",
                                               help="Namensteil des Patienten.")] = None,
    form: Annotated[str, typer.Option("--form", help="'vollname' oder 'anrede'.")] = "vollname",
    output: Annotated[Path | None, typer.Option("--ausgabe", "-o", help="Zieldatei.")] = None,
    store: StoreOption = None,
    capsule: Annotated[Path | None, typer.Option(
        "--kapsel", exists=True,
        help="Schlüsselkapsel eines Absenders (<name>_pseudonym.kapsel.asc) statt der lokalen"
             " Datenbank.")] = None,
    private_key: Annotated[Path | None, typer.Option(
        "--privater-schluessel", exists=True,
        help="Privater OpenPGP-Schlüssel als Datei; ohne Angabe entschlüsselt GnuPG aus dem"
             " Schlüsselbund.")] = None,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("PGP_PASSPHRASE"),
        help="Passphrase des privaten Schlüssels (sonst fragt der GnuPG-Agent).")] = None,
) -> None:
    """Pseudonyme im Text des Sprachmodells durch die Klardaten ersetzen (lokal)."""
    from pseudonymed.pseudonyms.reidentification import reidentify as _reidentify

    check_choice("--form", form, ("vollname", "anrede"))
    if capsule is not None:
        info, mapping, known = _mapping_from_capsule(capsule, private_key, passphrase, store)
    elif code or search:
        info, mapping, known = _mapping_from_store(code, search, store)
    else:
        abort("Bitte --kennung, --suche oder --kapsel angeben.")
    text = text_file.read_text(encoding="utf-8") if text_file else sys.stdin.read()
    r = _reidentify(text, mapping, info, form=form, known_codes=known)
    _output_reidentification(r, output)


@app.command("pseudonyme")
def pseudonyms(store: StoreOption = None) -> None:
    """Vergebene Kennungen auflisten, mit verknüpften Schreibweisen (nur lokal)."""
    with open_store(store) as sp:
        entries = sp.find()
        aliases = {e["schluessel"]: sp.aliases(e["schluessel"]) for e in entries}
    if not entries:
        typer.echo("Noch keine Pseudonyme vergeben.")
        return
    for e in entries:
        born = e["geburtsdatum"] or "–"
        typer.echo(f"{e['kennung']:>8}  {e['nachname']}, {e['vorname']}  geb. {born}"
                   f"  ({e['angelegt'][:10]})")
        for a in aliases[e["schluessel"]]:
            typer.echo(f"{'':8}  = {a['nachname']}, {a['vorname']}  geb. {a['geburtsdatum'] or '–'}"
                       f"  (Abgleich {a['score']:.2f})")
