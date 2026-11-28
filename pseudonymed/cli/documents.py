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
    LANGUAGE,
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
    canonical,
    choice,
    command,
    error_message,
    flags,
    list_metavar,
    matching_decision,
    open_store,
    report_uncertain,
    resolve_patient,
    scheme,
    shown,
    value_name,
    w,
)
from pseudonymed.config import (
    load_settings,
)
from pseudonymed.core.model import Patient
from pseudonymed.document.docrepro_adapter import DocError
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

# the help of 'pseudonymisieren'
_PSEUDONYMISE_HELP = w("Dokumente und Texte schwärzen, Pseudonyme einsetzen, Gegenprobe, Bericht.",
                       "Redact documents and texts, insert pseudonyms, re-check, report.")


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
        abort(w(f"Empfänger-Schlüssel: {error_message(e)}", f"Recipient key: {error_message(e)}"),
              cause=e)
    description = certificate.description(LANGUAGE)
    typer.echo(w(f"Empfänger: {description}", f"Recipient: {description}"), err=True)
    return certificate


def _choices(unreadable: str, date: str, preview: str, clean_up: str,
             ocr: str) -> tuple[str, str, str, str, str]:
    """The options that take one of a few words, each as the core knows it (the German word,
    whether that or its English one was given); an unknown value ends the command."""
    return (choice("--unlesbar", unreadable, ("redact", "keep")),
            choice("--datum", date, ("year", "age")),
            choice("--vorschau", preview, ("auto", "always", "never")),
            choice("--aufbereiten", clean_up, ("auto", "off")),
            canonical(ocr, ("off",)))  # an unknown engine is the reader's to refuse


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
            w("Hinweis: Die Kontaktkarte enthält Notizen. Angehörige, Zuweiser oder Begriffe daraus"
              " werden nicht automatisch geschwärzt; bei Bedarf 'kontakte export' nutzen und die"
              " Patientendatei ergänzen.",
              "Note: the contact card has notes. Relatives, referrers or terms from them are not"
              " redacted automatically; if needed, use 'contacts export' and add them to the"
              " patient file."), err=True,
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
    typer.echo(w(f"Arbeitsstand {state.name}: {len(v.hits)} Treffer,"
                 f" {len(v.discarded)} verworfen",
                 f"Working state {state.name}: {len(v.hits)} hit(s),"
                 f" {len(v.discarded)} discarded"), err=True)
    return v, a.patient


def _from_file(file: Path, p: Patient, opt: PipelineOptions, sp: PseudonymStore,
               working_state: bool) -> Prepared:
    """Phase one from a document or text, the working state saved beside it when asked."""
    v = prepare(file, p, opt, store=sp)
    if working_state:
        folder = opt.target_folder or file.parent
        doc_path, json_path = state_mod.save(v, folder / (file.stem + state_mod.EXTENSION))
        typer.echo(w(f"Arbeitsstand: {json_path}  (+ {doc_path.name}; enthält Klardaten)",
                     f"Working state: {json_path}  (+ {doc_path.name}; contains clear data)"))
    return v


def _report_paths(res: Result) -> None:
    """The side files of a released result: capsule, text, Markdown."""
    if res.target_capsule:
        typer.echo(w(f"Kapsel:      {res.target_capsule}  (nur an den Empfänger geben)",
                     f"Capsule:     {res.target_capsule}  (give it to the recipient only)"))
    if res.target_txt and not res.is_text:
        typer.echo(f"Text:        {res.target_txt}")
    if res.target_md:
        typer.echo(f"Markdown:    {res.target_md}")


def _report_result(res: Result, file: Path, person: Patient) -> bool:
    """Print the report and the paths; True when the file was released."""
    typer.echo(as_text(res.report, LANGUAGE))
    if res.preview:
        typer.echo(w(f"Vorschau:    {res.preview[0].parent} ({len(res.preview)} Seiten)",
                     f"Preview:     {res.preview[0].parent} ({len(res.preview)} page(s))"))
    if res.released:
        typer.echo(w(f"Freigegeben: {res.target}", f"Released:    {res.target}"))
        _report_paths(res)
    else:
        typer.echo(w(f"NICHT freigegeben: {file.name}", f"NOT released: {file.name}"), err=True)
    if res.target_report:
        typer.echo(w(f"Bericht:     {res.target_report}", f"Report:      {res.target_report}"))
    word = "Patientin" if person.female else "Patient"
    typer.echo(w(f"Pseudonym der Hauptperson: {word} {res.code}\n",
                 f"Pseudonym of the main person: {word} {res.code}\n"))
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
    except (ValueError, NotImplementedError, DocError, state_mod.WorkingStateError) as e:
        typer.echo(f"{file.name}: {error_message(e)}", err=True)
        return False
    assert person is not None
    return _report_result(res, file, person)


@command(app, "pseudonymisieren", "pseudonymise", help=_PSEUDONYMISE_HELP)
def pseudonymise(
    dateien: Annotated[list[Path], typer.Argument(
        metavar=list_metavar("files"),
        help=w("PDF-, Bild- oder Textdateien (.txt, .md, .srt, .vtt, Whisper-.json, .eml).",
               "PDF, image or text files (.txt, .md, .srt, .vtt, Whisper .json, .eml)."),
        exists=True)],
    patient: PatientOption = None,
    vcf: VcfOption = None,
    contact: ContactOption = None,
    num: NumberOption = None,
    config: ConfigOption = None,
    target: Annotated[Path | None, typer.Option(
        *flags("--ziel", "-z"), help=w("Zielordner.", "Target folder."))] = None,
    date: Annotated[str, typer.Option(
        *flags("--datum"), help=w("Geburtsdatum als 'jahr' oder 'alter'.",
                                  "Date of birth as 'year' or 'age'."))] = value_name("year"),
    rasterise: Annotated[bool, typer.Option(
        *flags("--rastern"), help=w("Ausgabe vollständig rastern.",
                                    "Rasterise the output completely."))] = False,
    dpi: Annotated[int, typer.Option(
        "--dpi", help=w("Auflösung beim Rastern.", "Resolution when rasterising."))] = 200,
    text_file: Annotated[bool, typer.Option(
        *flags("--text/--kein-text"), help=w("Textdatei in Lesereihenfolge schreiben.",
                                             "Write a text file in reading order."))] = True,
    markdown: Annotated[bool, typer.Option(
        *flags("--markdown/--kein-markdown"), help=w("Zusätzlich eine Markdown-Datei schreiben.",
                                                     "Also write a Markdown file."))] = False,
    text_layer: Annotated[bool, typer.Option(
        *flags("--textebene/--keine-textebene"),
        help=w("OCR-Text unsichtbar in Scans einbetten (durchsuchbares PDF).",
               "Embed the OCR text invisibly in scans (searchable PDF)."))] = True,
    report: Annotated[bool, typer.Option(
        *flags("--bericht/--kein-bericht"), help=w("Prüfbericht als JSON schreiben.",
                                                   "Write the audit report as JSON."))] = True,
    notice: Annotated[bool, typer.Option(
        *flags("--vermerk/--ohne-vermerk"), help=w("Vermerk in den PDF-Metadaten.",
                                                   "Note in the PDF metadata."))] = True,
    store: StoreOption = None,
    schema: SchemaOption = "hash",
    new: NewOption = False,
    same: SameOption = None,
    ocr: Annotated[str, typer.Option(
        "--ocr", help=w("OCR für Scans/Fotos: auto, rapidocr, tesseract oder aus.",
                        "OCR for scans/photos: auto, rapidocr, tesseract or off."))]
    = "auto",
    codes: Annotated[bool, typer.Option(
        *flags("--codes/--keine-codes"), help=w("Barcodes und QR-Codes schwärzen.",
                                                "Redact barcodes and QR codes."))] = True,
    preview: Annotated[str, typer.Option(
        *flags("--vorschau"), help=w("Vorschaubilder: auto (nur bei OCR), immer oder nie.",
                                     "Preview images: auto (only with OCR), always or"
                                     " never."))] = "auto",
    unreadable: Annotated[str, typer.Option(
        *flags("--unlesbar"), help=w("Von der OCR nicht lesbare Zeilen: schwaerzen oder lassen.",
                                     "Lines the OCR cannot read: redact or keep."))]
    = value_name("redact"),
    ner: Annotated[bool, typer.Option(
        *flags("--ner/--kein-ner"),
        help=w("Unbekannte Personen und Orte per NER suchen (Extra 'ner').",
               "Search for unknown persons and places with NER (extra"
               " 'ner')."))]
    = False,
    medical_terms: Annotated[bool, typer.Option(
        *flags("--fachbegriffe/--keine-fachbegriffe"),
        help=w("Medizinische Fachbegriffe vor NER und unscharfer OCR-Suche schützen.",
               "Protect medical terms from NER and the fuzzy OCR search."))] = True,
    identifiers: Annotated[bool, typer.Option(
        *flags("--identifikatoren/--keine-identifikatoren"),
        help=w("Berufe, Kliniken, Studien und Titel schwärzen (Tabelle 'identifikatoren').",
               "Redact professions, hospitals, studies and titles (table"
               " 'identifiers')."))]
    = True,
    drugs: Annotated[bool, typer.Option(
        *flags("--medikamente-schwaerzen/--medikamente-lassen"),
        help=w("Medikamente: die Substanz (Wirkstoff, Handelsname) schwärzen. Standard: stehen"
               " lassen, medizinischer Inhalt.",
               "Medications: redact the substance (active ingredient, trade name). Default: keep"
               " it, medical content."))] = False,
    doses: Annotated[bool, typer.Option(
        *flags("--dosierungen-schwaerzen/--dosierungen-lassen"),
        help=w("Medikamente: Dosierung und Verordnungsschema neben der Substanz schwärzen"
               " („500 mg 1-0-1“, „bei Bedarf“). Standard: stehen lassen.",
               "Medications: redact the dose and the prescription schedule next to the"
               " substance (‘500 mg 1-0-1’, ‘bei Bedarf’). Default: keep them."))] = False,
    lab_tests: Annotated[bool, typer.Option(
        *flags("--laborwerte-schwaerzen/--laborwerte-lassen"),
        help=w("Laborwerte: die Untersuchung (den Parameter, etwa „TSH“) schwärzen. Standard:"
               " stehen lassen.",
               "Lab values: redact the examination (the parameter, such as ‘TSH’). Default:"
               " keep it."))] = False,
    measurements: Annotated[bool, typer.Option(
        *flags("--messwerte-schwaerzen/--messwerte-lassen"),
        help=w("Laborwerte: den Messwert hinter der Untersuchung schwärzen („2,3 mU/l“)."
               " Standard: stehen lassen.",
               "Lab values: redact the measurement after the examination (‘2,3 mU/l’)."
               " Default: keep it."))] = False,
    fuzzy: Annotated[bool, typer.Option(
        *flags("--unscharf/--genau"),
        help=w("Textdateien: abweichend geschriebene Namen unscharf suchen (Transkripte).",
               "Text files: fuzzy search for misspelt names (transcripts)."))] = True,
    clean_up: Annotated[str, typer.Option(
        *flags("--aufbereiten"),
        help=w("Scans und Fotos vor der OCR nach dem Vorschlag von optdoc aufbereiten"
               " (aufrichten, entzerren, begradigen, Hintergrund): auto oder aus.",
               "Prepare scans and photos before the OCR as optdoc suggests (upright, unwarp,"
               " straighten, background): auto or off."))] = "auto",
    tables: Annotated[bool, typer.Option(
        *flags("--tabellen/--keine-tabellen"),
        help=w("Tesseract: Tabellengitter erkennen und die Zellen ohne Linien erneut lesen.",
               "Tesseract: recognise table grids and read the cells again without the"
               " lines."))]
    = True,
    vocabulary: Annotated[bool, typer.Option(
        *flags("--vokabular/--kein-vokabular"),
        help=w("Verworfene Rate-Treffer als <name>_pseudonym.vocabulary.txt schreiben.",
               "Write the discarded guesses as <name>_pseudonym.vocabulary.txt."))] = True,
    working_state: Annotated[bool, typer.Option(
        *flags("--arbeitsstand"),
        help=w("Vor dem Abschluss den Arbeitsstand neben die Ausgabe schreiben"
               " (<name>.working-state.json mit der Kette von docrepro und das Original"
               " daneben, mit Klardaten): wieder aufnehmbar in der Oberfläche oder als"
               " Eingabe dieses Befehls.",
               "Before finishing, write the working state next to the output"
               " (<name>.working-state.json with docrepro's chain, and the original beside it,"
               " with clear data): it can be resumed in the desktop app or as input to this"
               " command."))] = False,
    reanalyse: Annotated[bool, typer.Option(
        *flags("--neu-analysieren"),
        help=w("Einen Arbeitsstand als Eingabe mit den jetzigen Erkennungsoptionen erneut"
               " durchsuchen (Aufbereitung, Texterkennung, Kästen, Masken, Aufnahmen und"
               " verworfene Treffer bleiben); ohne den Schalter wird er abgeschlossen, wie er"
               " gespeichert wurde.",
               "Search a working state given as input again with the current detection"
               " options (clean-up, text recognition, boxes, masks, taken-up selections and"
               " discarded hits stay); without the switch it is finished as it was"
               " saved."))] = False,
    structured: Annotated[bool, typer.Option(
        *flags("--gliedern/--roh"),
        help=w("OCR-Text als Markdown gliedern: Absätze zusammenfügen, Überschriften, Listen,"
               " Tabellen.",
               "Structure the OCR text as Markdown: join paragraphs, headings, lists,"
               " tables."))] = True,
    reference_population: Annotated[int | None, typer.Option(
        *flags("--referenzpopulation"),
        help=w("Restrisiko: aus wie vielen Menschen ein Angreifer wählt (Standard aus der"
               " Konfiguration, sonst 83.600.000 für Deutschland).",
               "Residual risk: how many people an attacker chooses from (default from the"
               " configuration, else 83,600,000 for Germany)."))] = None,
    own_places: Annotated[str | None, typer.Option(
        *flags("--eigene-orte"),
        help=w("Restrisiko: Orte des Absenders (Sprechstunde, Klinik), die nicht"
               " als Patientenmerkmal zählen; Komma-getrennt.",
               "Residual risk: the sender's places (consultation service, clinic) that do"
               " not count as a feature of the patient; comma-separated."))] = None,
    recipient: Annotated[str | None, typer.Option(
        *flags("--empfaenger"),
        help=w("Öffentlicher OpenPGP-Schlüssel des Empfängers (Datei oder https-Adresse):"
               " Pseudonyme mit Einmalschlüssel, Zuordnung als verschlüsselte Kapsel"
               " <name>_pseudonym.capsule.asc für den Empfänger; lokal wird nichts gespeichert.",
               "The recipient's public OpenPGP key (file or https address): pseudonyms with a"
               " one-time key, the mapping as an encrypted capsule <name>_pseudonym.capsule.asc"
               " for the recipient; nothing is stored locally."))]
    = None,
) -> None:
    """Pseudonymise every input (document, text or saved working state) for one patient
    and print its report; exit code 2 when an input was not released."""
    certificate = _load_recipient(recipient)
    unreadable, date, preview, clean_up, ocr = _choices(
        unreadable, date, preview, clean_up, ocr)
    schema = scheme(schema)
    states = {file: state_mod.is_working_state(file) for file in dateien}
    if certificate and any(states.values()):
        abort(w("Ein Arbeitsstand lässt sich nicht für einen Empfänger abschließen"
                " (seine Codes stammen aus der lokalen Datenbank).",
                "A working state cannot be finished for a recipient"
                " (its codes come from the local database)."))
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
        clean_up=clean_up, ocr_tables=tables,
        vocabulary=vocabulary, structured=structured,
        reference_population=reference, own_places=places,
        recipient=recipient_armor, recipient_fingerprint=recipient_fingerprint,
        redaction=RedactionOptions(rasterise=rasterise, dpi=dpi, notice=notice,
                                   text_layer=text_layer),
        language=LANGUAGE,
    )
    run = _Run(opt, p, working_state, reanalyse)
    with (one_time_store(schema) if certificate else open_store(store, schema)) as sp:
        free = [_process(file, states[file], run, sp) for file in dateien]
    if not all(free):
        raise typer.Exit(code=2)



@command(app, "pruefen", "check", help=w(
    "Gegenprobe an einer vorhandenen Datei: sind noch Identifikatoren enthalten?",
    "Re-check an existing file: does it still contain identifiers?"))
def check(
    file: Annotated[Path, typer.Argument(
        metavar=w("datei", "file"), exists=True,
        help=w("Bereits pseudonymisierte PDF- oder Textdatei.",
               "PDF or text file that has already been pseudonymised."))],
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
    """Re-check an already pseudonymised file with the patient's rules; exit code 2 when
    identifiers are left."""
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
        typer.echo(w(f"{file.name}: Gegenprobe bestanden.", f"{file.name}: re-check passed."))
        return
    left = len(g.residual_matches)
    typer.echo(w(f"{file.name}: Gegenprobe NICHT bestanden ({left} Resttreffer).",
                 f"{file.name}: re-check FAILED ({left} residual hit(s))."))
    for r in g.residual_matches[:50]:
        typer.echo(w(f"  Seite {r['page']}: {r['rule']} ({r['source']})",
                     f"  Page {r['page']}: {r['rule']} ({r['source']})"))
    for a in g.remarks:
        typer.echo(w(f"  Anmerkung: {shown(a)}", f"  Remark: {shown(a)}"))
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
        abort(w(f"Kapsel: {error_message(e)}", f"Capsule: {error_message(e)}"), cause=e)
    description = k.description(LANGUAGE)
    typer.echo(w(f"Kapsel: {description}", f"Capsule: {description}"), err=True)
    info, mapping, known = k.info(), k.mappings, [k.code]
    try:
        with open_store(store) as sp:
            same_entries = [v for v in match_candidates(k.as_patient(), sp.candidates()) if v.same]
    except OSError:
        same_entries = []
    if same_entries:
        typer.echo(w(f"Entspricht dem lokalen Eintrag {same_entries[0].code}"
                     f" ({same_entries[0].surname}, {same_entries[0].first_name}).",
                     f"Matches the local entry {same_entries[0].code}"
                     f" ({same_entries[0].surname}, {same_entries[0].first_name})."), err=True)
    return info, mapping, known


def _mapping_from_store(
    code: str | None, search: str | None, store: Path | None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """The one local entry that matches: its row, its mapping, every known code."""
    with open_store(store) as sp:
        matches = sp.find(code_=code, search=search)
        if len(matches) != 1:
            abort(w(f"{len(matches)} Einträge gefunden; bitte eindeutig wählen"
                    " (siehe 'pseudonyme').",
                    f"{len(matches)} entries found; please choose one unambiguously"
                    " (see 'pseudonyms')."))
        info = matches[0]
        return info, sp.mapping(info["patient_key"]), sp.codes()


def _output_reidentification(r: Reidentification, output: Path | None) -> None:
    """Write or print the re-identified text; unresolved pseudonyms end with code 3."""
    if output:
        output.write_text(r.text, encoding="utf-8")
        typer.echo(w(f"{r.replaced} Ersetzungen, geschrieben nach {output}",
                     f"{r.replaced} replacement(s), written to {output}"), err=True)
    else:
        typer.echo(r.text, nl=False)
    if r.remaining:
        typer.echo(w("Nicht aufgelöst: ", "Unresolved: ") + ", ".join(r.remaining), err=True)
        for rest, note in r.notes.items():
            typer.echo(f"  {rest}: {shown(note)}", err=True)
        raise typer.Exit(code=3)


@command(app, "rueckersetzen", "reidentify", help=w(
    "Pseudonyme im Text des Sprachmodells durch die Klardaten ersetzen (lokal).",
    "Replace the pseudonyms in the language model's text with the clear data (locally)."))
def reidentify(
    text_file: Annotated[Path | None, typer.Argument(
        metavar=w("textdatei", "textfile"),
        help=w("Text des Sprachmodells (sonst Standardeingabe).",
               "The language model's text (else standard input)."))] = None,
    code: Annotated[str | None, typer.Option(
        *flags("--kennung", "-k", "--buchstabe"),
        help=w("Pseudonym-Kennung, z. B. K7Q4MX oder A.",
               "Pseudonym code, e.g. K7Q4MX or A."))] = None,
    search: Annotated[str | None, typer.Option(
        *flags("--suche", "-s"), help=w("Namensteil des Patienten.",
                                        "Part of the patient's name."))] = None,
    form: Annotated[str, typer.Option(
        "--form", help=w("'vollname' oder 'anrede'.",
                         "'full-name' or 'salutation' (Herr or Frau with the surname)."))]
    = value_name("full-name"),
    output: Annotated[Path | None, typer.Option(
        *flags("--ausgabe", "-o"), help=w("Zieldatei.", "Target file."))] = None,
    store: StoreOption = None,
    capsule: Annotated[Path | None, typer.Option(
        *flags("--kapsel"), exists=True,
        help=w("Schlüsselkapsel eines Absenders (<name>_pseudonym.capsule.asc) statt der lokalen"
               " Datenbank.",
               "A sender's key capsule (<name>_pseudonym.capsule.asc) instead of the local"
               " database."))] = None,
    private_key: Annotated[Path | None, typer.Option(
        *flags("--privater-schluessel"), exists=True,
        help=w("Privater OpenPGP-Schlüssel als Datei; ohne Angabe entschlüsselt GnuPG aus dem"
               " Schlüsselbund.",
               "Private OpenPGP key as a file; without it GnuPG decrypts with the key from the"
               " keyring."))] = None,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=environment.names("PGP_PASSPHRASE"),
        help=w("Passphrase des privaten Schlüssels (sonst fragt der GnuPG-Agent).",
               "Passphrase of the private key (else the GnuPG agent asks for it)."))] = None,
) -> None:
    """Put the clear data back in place of the pseudonyms in a language model's text, with
    the mapping from the local store or from a sender's capsule."""
    from pseudonymed.pseudonyms.reidentification import reidentify as _reidentify

    form = choice("--form", form, ("full-name", "salutation"))
    if capsule is not None:
        info, mapping, known = _mapping_from_capsule(capsule, private_key, passphrase, store)
    elif code or search:
        info, mapping, known = _mapping_from_store(code, search, store)
    else:
        abort(w("Bitte --kennung, --suche oder --kapsel angeben.",
                "Please give --code, --search or --capsule."))
    text = text_file.read_text(encoding="utf-8") if text_file else sys.stdin.read()
    r = _reidentify(text, mapping, info, form=form, known_codes=known)
    _output_reidentification(r, output)


@command(app, "pseudonyme", "pseudonyms", help=w(
    "Vergebene Kennungen auflisten, mit verknüpften Schreibweisen (nur lokal).",
    "List the codes issued, with their linked spellings (local only)."))
def pseudonyms(store: StoreOption = None) -> None:
    """List the codes of the local store with the spellings linked to each."""
    with open_store(store) as sp:
        entries = sp.find()
        aliases = {e["patient_key"]: sp.aliases(e["patient_key"]) for e in entries}
    if not entries:
        typer.echo(w("Noch keine Pseudonyme vergeben.", "No pseudonyms issued yet."))
        return
    born_label = w("geb.", "born")
    match_label = w("Abgleich", "match")
    for e in entries:
        born = e["birth_date"] or "–"
        typer.echo(f"{e['code']:>8}  {e['surname']}, {e['first_name']}  {born_label} {born}"
                   f"  ({e['created'][:10]})")
        for a in aliases[e["patient_key"]]:
            typer.echo(f"{'':8}  = {a['surname']}, {a['first_name']}"
                       f"  {born_label} {a['birth_date'] or '–'}"
                       f"  ({match_label} {a['score']:.2f})")
