"""Commands over documents: pseudonymise, check, re-identify, list pseudonyms."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from pseudonymed import arbeitsstand as standmodul
from pseudonymed import umgebung
from pseudonymed.ablauf import (
    Ablaufoptionen,
    Ergebnis,
    Textvorbereitung,
    Vorbereitung,
    abschliessen,
    analysiere,
    vorbereiten,
)
from pseudonymed.cli.basis import (
    GleichOption,
    KonfigOption,
    KontaktOption,
    NeuOption,
    NrOption,
    PatientOption,
    SchemaOption,
    SpeicherOption,
    VcfOption,
    abbruch,
    abgleich_entscheidung,
    app,
    patient_bestimmen,
    speicher_oeffnen,
    unsicher_melden,
    wahl_pruefen,
)
from pseudonymed.dokument.laden import papierformat
from pseudonymed.dokument.ocr import OCRFehler
from pseudonymed.dokument.textdatei import ist_textdatei
from pseudonymed.kern.modell import Patient
from pseudonymed.konfig import (
    lade_einstellungen,
)
from pseudonymed.pruefung.bericht import als_text
from pseudonymed.pseudonyme.pgp import Zertifikat
from pseudonymed.pseudonyme.rueckersetzung import Rueckersetzung
from pseudonymed.pseudonyme.speicher import (
    Pseudonymspeicher,
    UnsichererAbgleich,
    einmalspeicher,
)
from pseudonymed.schwaerzen.pdf import Schwaerzoptionen

Vorbereitet = Vorbereitung | Textvorbereitung


@dataclass(frozen=True)
class _Durchlauf:
    """What every input of one ``pseudonymisieren`` call shares."""

    opt: Ablaufoptionen
    patient: Patient | None  # None when every input is a saved state with its own patient
    arbeitsstand: bool
    neu_analysieren: bool


def _empfaenger_laden(empfaenger: str | None) -> Zertifikat | None:
    """The recipient's public key when one was named; a bad key ends the command."""
    if not empfaenger:
        return None
    from pseudonymed.pseudonyme import pgp as pgpmodul

    try:
        zertifikat = pgpmodul.lade_zertifikat(empfaenger)
    except pgpmodul.PGPFehler as e:
        abbruch(f"Empfänger-Schlüssel: {e}", ursache=e)
    typer.echo(f"Empfänger: {zertifikat.beschreibung()}", err=True)
    return zertifikat


def _wahlen_pruefen(papier: str, unlesbar: str, datum: str, vorschau: str,
                    aufbereiten: str) -> None:
    """End the command when one of the free-text options holds an unknown value."""
    try:
        papierformat(papier)
    except ValueError as e:
        abbruch(str(e), ursache=e)
    wahl_pruefen("--unlesbar", unlesbar, ("schwaerzen", "lassen"))
    wahl_pruefen("--datum", datum, ("jahr", "alter"))
    wahl_pruefen("--vorschau", vorschau, ("auto", "immer", "nie"))
    wahl_pruefen("--aufbereiten", aufbereiten, ("auto", "aus"))


def _empfaengerfelder(zertifikat: Zertifikat | None) -> tuple[str, str]:
    """The recipient's armoured key and fingerprint for the options, empty without one."""
    if zertifikat is None:
        return "", ""
    return zertifikat.armor, zertifikat.fingerabdruck


def _risikofelder(referenzpopulation: int | None, eigene_orte: str | None) -> tuple[int, str]:
    """Reference population and the sender's places: the option, else the configuration."""
    einstellungen = lade_einstellungen()
    return (referenzpopulation or einstellungen.referenzpopulation,
            eigene_orte if eigene_orte is not None else einstellungen.eigene_orte)


def _notizen_hinweis(p: Patient | None) -> None:
    """Warn that notes on the contact card are not redacted by themselves."""
    if p is not None and p.hinweise:
        typer.echo(
            "Hinweis: Die Kontaktkarte enthält Notizen. Angehörige, Zuweiser oder Begriffe daraus"
            " werden nicht automatisch geschwärzt; bei Bedarf 'kontakte export' nutzen und die"
            " Patientendatei ergänzen.", err=True,
        )


def _aus_arbeitsstand(stand: Path, opt: Ablaufoptionen, sp: Pseudonymspeicher,
                      neu_analysieren: bool) -> tuple[Vorbereitet, Patient]:
    """Phase one from a saved state: restored as saved, or searched again with the
    current options while the user's decisions are carried over."""
    a = standmodul.lies(stand)
    if neu_analysieren and a.ist_text:
        assert a.dokumentpfad is not None
        v: Vorbereitet = vorbereiten(a.dokumentpfad, a.patient, opt, speicher=sp)
        standmodul.uebernehme_entscheidungen(a, v)
    elif neu_analysieren:
        v = analysiere(standmodul.lesung_aus(a, opt), a.patient, opt, speicher=sp)
        standmodul.uebernehme_entscheidungen(a, v)
    else:
        v = standmodul.vorbereitung_aus(a, speicher=sp, opt=opt)
    typer.echo(f"Arbeitsstand {stand.name}: {len(v.fundstellen)} Treffer,"
               f" {len(v.verworfen)} verworfen", err=True)
    return v, a.patient


def _aus_datei(datei: Path, p: Patient, opt: Ablaufoptionen, sp: Pseudonymspeicher,
               arbeitsstand: bool) -> Vorbereitet:
    """Phase one from a document or text, the working state saved beside it when asked."""
    v = vorbereiten(datei, p, opt, speicher=sp)
    if arbeitsstand:
        ordner = opt.ziel_ordner or datei.parent
        dok, js = standmodul.speichere(v, ordner / (datei.stem + standmodul.ENDUNG))
        typer.echo(f"Arbeitsstand: {js}  (+ {dok.name}; enthält Klardaten)")
    return v


def _pfade_melden(erg: Ergebnis) -> None:
    """The side files of a released result: capsule, text, Markdown."""
    if erg.ziel_kapsel:
        typer.echo(f"Kapsel:      {erg.ziel_kapsel}  (nur an den Empfänger geben)")
    if erg.ziel_txt and not erg.ist_text:
        typer.echo(f"Text:        {erg.ziel_txt}")
    if erg.ziel_md:
        typer.echo(f"Markdown:    {erg.ziel_md}")


def _ergebnis_melden(erg: Ergebnis, datei: Path, person: Patient) -> bool:
    """Print the report and the paths; True when the file was released."""
    typer.echo(als_text(erg.bericht))
    if erg.vorschau:
        typer.echo(f"Vorschau:    {erg.vorschau[0].parent} ({len(erg.vorschau)} Seiten)")
    if erg.freigegeben:
        typer.echo(f"Freigegeben: {erg.ziel}")
        _pfade_melden(erg)
    else:
        typer.echo(f"NICHT freigegeben: {datei.name}", err=True)
    if erg.ziel_bericht:
        typer.echo(f"Bericht:     {erg.ziel_bericht}")
    wort = "Patientin" if person.weiblich else "Patient"
    typer.echo(f"Pseudonym der Hauptperson: {wort} {erg.kennung}\n")
    return erg.freigegeben


def _verarbeite(datei: Path, stand: Path | None, lauf: _Durchlauf, sp: Pseudonymspeicher) -> bool:
    """One input from phase one to the printed report; False when it was not released
    or could not be processed.  An uncertain match ends the whole command."""
    person = lauf.patient
    try:
        if stand is not None:
            v, person = _aus_arbeitsstand(stand, lauf.opt, sp, lauf.neu_analysieren)
        else:
            assert person is not None  # a plain file always comes with a patient
            v = _aus_datei(datei, person, lauf.opt, sp, lauf.arbeitsstand)
        erg = abschliessen(v)
    except UnsichererAbgleich as e:
        assert person is not None
        unsicher_melden(person, e)
        raise typer.Exit(code=4) from e
    except (ValueError, NotImplementedError, OCRFehler, standmodul.ArbeitsstandFehler) as e:
        typer.echo(f"{datei.name}: {e}", err=True)
        return False
    assert person is not None
    return _ergebnis_melden(erg, datei, person)


@app.command()
def pseudonymisieren(
    dateien: Annotated[list[Path], typer.Argument(
        help="PDF-, Bild- oder Textdateien (.txt, .md, .srt, .vtt, Whisper-.json, .eml).",
        exists=True)],
    patient: PatientOption = None,
    vcf: VcfOption = None,
    kontakt: KontaktOption = None,
    nr: NrOption = None,
    konfig: KonfigOption = None,
    ziel: Annotated[Path | None, typer.Option("--ziel", "-z", help="Zielordner.")] = None,
    datum: Annotated[str, typer.Option("--datum", help="Geburtsdatum als 'jahr' oder 'alter'.")]
    = "jahr",
    rastern: Annotated[bool, typer.Option("--rastern", help="Ausgabe vollständig rastern.")]
    = False,
    dpi: Annotated[int, typer.Option("--dpi", help="Auflösung beim Rastern.")] = 200,
    textdatei: Annotated[bool, typer.Option("--text/--kein-text",
                                            help="Textdatei in Lesereihenfolge schreiben.")] = True,
    markdown: Annotated[bool, typer.Option("--markdown/--kein-markdown",
                                           help="Zusätzlich eine Markdown-Datei schreiben.")]
    = False,
    textebene: Annotated[bool, typer.Option(
        "--textebene/--keine-textebene",
        help="OCR-Text unsichtbar in Scans einbetten (durchsuchbares PDF).")] = True,
    bericht: Annotated[bool, typer.Option("--bericht/--kein-bericht",
                                          help="Prüfbericht als JSON schreiben.")] = True,
    vermerk: Annotated[bool, typer.Option("--vermerk/--ohne-vermerk",
                                          help="Vermerk in den PDF-Metadaten.")] = True,
    speicher: SpeicherOption = None,
    schema: SchemaOption = "hash",
    neu: NeuOption = False,
    gleich: GleichOption = None,
    ocr: Annotated[str, typer.Option("--ocr", help="OCR für Scans/Fotos: auto, rapidocr,"
                                                  " tesseract oder aus.")] = "auto",
    codes: Annotated[bool, typer.Option("--codes/--keine-codes",
                                        help="Barcodes und QR-Codes schwärzen.")] = True,
    vorschau: Annotated[str, typer.Option("--vorschau", help="Vorschaubilder: auto (nur bei OCR),"
                                                          " immer oder nie.")] = "auto",
    unlesbar: Annotated[str, typer.Option(
        "--unlesbar", help="Von der OCR nicht lesbare Zeilen: schwaerzen oder lassen.")]
    = "schwaerzen",
    ner: Annotated[bool, typer.Option("--ner/--kein-ner", help="Unbekannte Personen und Orte"
                                                              " per NER suchen (Extra 'ner').")]
    = False,
    fachbegriffe: Annotated[bool, typer.Option(
        "--fachbegriffe/--keine-fachbegriffe",
        help="Medizinische Fachbegriffe vor NER und unscharfer OCR-Suche schützen.")] = True,
    identifikatoren: Annotated[bool, typer.Option(
        "--identifikatoren/--keine-identifikatoren",
        help="Berufe, Kliniken, Studien und Titel schwärzen (Tabelle 'identifikatoren').")]
    = True,
    medikamente: Annotated[bool, typer.Option(
        "--medikamente-schwaerzen/--medikamente-lassen",
        help="Medikamente: die Substanz (Wirkstoff, Handelsname) schwärzen. Standard: stehen"
             " lassen, medizinischer Inhalt.")] = False,
    dosierungen: Annotated[bool, typer.Option(
        "--dosierungen-schwaerzen/--dosierungen-lassen",
        help="Medikamente: Dosierung und Verordnungsschema neben der Substanz schwärzen"
             " („500 mg 1-0-1“, „bei Bedarf“). Standard: stehen lassen.")] = False,
    laborwerte: Annotated[bool, typer.Option(
        "--laborwerte-schwaerzen/--laborwerte-lassen",
        help="Laborwerte: die Untersuchung (den Parameter, etwa „TSH“) schwärzen. Standard:"
             " stehen lassen.")] = False,
    messwerte: Annotated[bool, typer.Option(
        "--messwerte-schwaerzen/--messwerte-lassen",
        help="Laborwerte: den Messwert hinter der Untersuchung schwärzen („2,3 mU/l“)."
             " Standard: stehen lassen.")] = False,
    unscharf: Annotated[bool, typer.Option(
        "--unscharf/--genau",
        help="Textdateien: abweichend geschriebene Namen unscharf suchen (Transkripte).")] = True,
    aufbereiten: Annotated[str, typer.Option(
        "--aufbereiten", help="Scans und Fotos vor der OCR aufbereiten (begradigen, Hintergrund,"
                              " Hochskalieren): auto oder aus.")] = "auto",
    papier: Annotated[str, typer.Option(
        "--papier", help="Seitengröße für Fotos und Bilder: a4, a5, a3, letter, legal oder bild"
                         " (so groß wie das Bild).")] = "a4",
    tabellen: Annotated[bool, typer.Option(
        "--tabellen/--keine-tabellen",
        help="Tesseract: Tabellengitter erkennen und die Zellen ohne Linien erneut lesen.")]
    = True,
    vokabular: Annotated[bool, typer.Option(
        "--vokabular/--kein-vokabular",
        help="Verworfene Rate-Treffer als <name>_pseudonym.vokabular.txt schreiben.")] = True,
    arbeitsstand: Annotated[bool, typer.Option(
        "--arbeitsstand",
        help="Vor dem Abschluss den Arbeitsstand neben die Ausgabe schreiben"
             " (<name>.arbeitsstand.pdf + .json, mit Klardaten): wieder aufnehmbar in der"
             " Oberfläche oder als Eingabe dieses Befehls.")] = False,
    neu_analysieren: Annotated[bool, typer.Option(
        "--neu-analysieren",
        help="Einen Arbeitsstand als Eingabe mit den jetzigen Erkennungsoptionen erneut"
             " durchsuchen (Aufbereitung, Texterkennung, Kästen, Masken, Aufnahmen und"
             " verworfene Treffer bleiben); ohne den Schalter wird er abgeschlossen, wie er"
             " gespeichert wurde.")] = False,
    gliedern: Annotated[bool, typer.Option(
        "--gliedern/--roh",
        help="OCR-Text als Markdown gliedern: Absätze zusammenfügen, Überschriften, Listen,"
             " Tabellen.")] = True,
    referenzpopulation: Annotated[int | None, typer.Option(
        "--referenzpopulation",
        help="Restrisiko: aus wie vielen Menschen ein Angreifer wählt (Standard aus der"
             " Konfiguration, sonst 83.600.000 für Deutschland).")] = None,
    eigene_orte: Annotated[str | None, typer.Option(
        "--eigene-orte", help="Restrisiko: Orte des Absenders (Sprechstunde, Klinik), die nicht"
                              " als Patientenmerkmal zählen; Komma-getrennt.")] = None,
    empfaenger: Annotated[str | None, typer.Option(
        "--empfaenger",
        help="Öffentlicher OpenPGP-Schlüssel des Empfängers (Datei oder https-Adresse):"
             " Pseudonyme mit Einmalschlüssel, Zuordnung als verschlüsselte Kapsel"
             " <name>_pseudonym.kapsel.asc für den Empfänger; lokal wird nichts gespeichert.")]
    = None,
) -> None:
    """Dokumente und Texte schwärzen, Pseudonyme einsetzen, Gegenprobe, Bericht."""
    zertifikat = _empfaenger_laden(empfaenger)
    _wahlen_pruefen(papier, unlesbar, datum, vorschau, aufbereiten)
    staende = {datei: standmodul.ist_arbeitsstand(datei) for datei in dateien}
    if zertifikat and any(staende.values()):
        abbruch("Ein Arbeitsstand lässt sich nicht für einen Empfänger abschließen"
                " (seine Codes stammen aus der lokalen Datenbank).")
    p = (patient_bestimmen(patient, vcf, kontakt, nr, konfig)
         if not all(staende.values()) else None)  # saved states carry their patient
    _notizen_hinweis(p)
    empfaenger_armor, empfaenger_fingerabdruck = _empfaengerfelder(zertifikat)
    referenz, orte = _risikofelder(referenzpopulation, eigene_orte)
    opt = Ablaufoptionen(
        datumsform=datum, textdatei=textdatei, markdown=markdown, bericht=bericht,
        ziel_ordner=ziel,
        abgleich=abgleich_entscheidung(neu, gleich), ocr=ocr, codes=codes, vorschau=vorschau,
        unlesbar=unlesbar, ner=ner, fachbegriffe=fachbegriffe, identifikatoren=identifikatoren,
        medikamente_schwaerzen=medikamente, dosierungen_schwaerzen=dosierungen,
        laborwerte_schwaerzen=laborwerte, messwerte_schwaerzen=messwerte,
        unscharf_text=unscharf,
        aufbereiten=aufbereiten, papierformat=papier, ocr_tabellen=tabellen,
        vokabular=vokabular, gliedern=gliedern,
        referenzpopulation=referenz, eigene_orte=orte,
        empfaenger=empfaenger_armor, empfaenger_fingerabdruck=empfaenger_fingerabdruck,
        schwaerzen=Schwaerzoptionen(rastern=rastern, dpi=dpi, vermerk=vermerk,
                                    textebene=textebene),
    )
    lauf = _Durchlauf(opt, p, arbeitsstand, neu_analysieren)
    with (einmalspeicher(schema) if zertifikat else speicher_oeffnen(speicher, schema)) as sp:
        frei = [_verarbeite(datei, staende[datei], lauf, sp) for datei in dateien]
    if not all(frei):
        raise typer.Exit(code=2)

# the command's name until November 2026, kept for existing scripts
app.command("anonymisieren", hidden=True)(pseudonymisieren)


@app.command()
def pruefen(
    datei: Annotated[Path, typer.Argument(exists=True,
                                          help="Bereits pseudonymisierte PDF- oder Textdatei.")],
    patient: PatientOption = None,
    vcf: VcfOption = None,
    kontakt: KontaktOption = None,
    nr: NrOption = None,
    konfig: KonfigOption = None,
    speicher: SpeicherOption = None,
    schema: SchemaOption = "hash",
    neu: NeuOption = False,
    gleich: GleichOption = None,
) -> None:
    """Gegenprobe an einer vorhandenen Datei: sind noch Identifikatoren enthalten?"""
    import pymupdf

    from pseudonymed.erkennung.deterministisch import regeln_fuer
    from pseudonymed.pruefung.gegenprobe import pruefe
    from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

    p = patient_bestimmen(patient, vcf, kontakt, nr, konfig)
    with speicher_oeffnen(speicher, schema) as sp:
        try:
            kennung = sp.kennung_fuer(p, abgleich_entscheidung(neu, gleich)).kennung
        except UnsichererAbgleich as e:
            unsicher_melden(p, e)
            raise typer.Exit(code=4) from e
        geheimnis = sp.geheimnis
    regeln = regeln_fuer(p, Pseudonymvergabe(p, kennung, geheimnis=geheimnis))
    # the patient's own code may look like a file number ("AZ…"): never a leftover
    if ist_textdatei(datei):
        from pseudonymed.ablauf import gegenprobe_textdatei

        g = gegenprobe_textdatei(datei, regeln, pseudonyme=[kennung])
    else:
        with pymupdf.open(datei) as doc:
            g = pruefe(doc, regeln, pseudonyme=[kennung])
    if g.bestanden:
        typer.echo(f"{datei.name}: Gegenprobe bestanden.")
        return
    typer.echo(f"{datei.name}: Gegenprobe NICHT bestanden ({len(g.resttreffer)} Resttreffer).")
    for r in g.resttreffer[:50]:
        typer.echo(f"  Seite {r['seite']}: {r['regel']} ({r['quelle']})")
    for a in g.anmerkungen:
        typer.echo(f"  Anmerkung: {a}")
    raise typer.Exit(code=2)


def _zuordnung_aus_kapsel(
    kapsel: Path, privater_schluessel: Path | None, passphrase: str | None,
    speicher: Path | None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """Open a sender's capsule: its entry, its mapping and its code; a matching local
    entry is mentioned."""
    from pseudonymed.pseudonyme import kapsel as kapselmodul
    from pseudonymed.pseudonyme.abgleich import abgleichen
    from pseudonymed.pseudonyme.pgp import PGPFehler

    try:
        k = kapselmodul.oeffne(kapsel, privater_schluessel, passphrase)
    except (PGPFehler, kapselmodul.KapselFehler, OSError) as e:
        abbruch(f"Kapsel: {e}", ursache=e)
    typer.echo(f"Kapsel: {k.beschreibung()}", err=True)
    info, zuordnung, bekannte = k.info(), k.zuordnungen, [k.kennung]
    try:
        with speicher_oeffnen(speicher) as sp:
            gleiche = [v for v in abgleichen(k.als_patient(), sp.kandidaten()) if v.gleich]
    except OSError:
        gleiche = []
    if gleiche:
        typer.echo(f"Entspricht dem lokalen Eintrag {gleiche[0].kennung}"
                   f" ({gleiche[0].nachname}, {gleiche[0].vorname}).", err=True)
    return info, zuordnung, bekannte


def _zuordnung_aus_speicher(
    kennung: str | None, suche: str | None, speicher: Path | None,
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """The one local entry that matches: its row, its mapping, every known code."""
    with speicher_oeffnen(speicher) as sp:
        treffer = sp.finde(kennung_=kennung, suche=suche)
        if len(treffer) != 1:
            abbruch(f"{len(treffer)} Einträge gefunden; bitte eindeutig wählen"
                    " (siehe 'pseudonyme').")
        info = treffer[0]
        return info, sp.zuordnung(info["schluessel"]), sp.kennungen()


def _rueckersetzung_ausgeben(r: Rueckersetzung, ausgabe: Path | None) -> None:
    """Write or print the re-identified text; unresolved pseudonyms end with code 3."""
    if ausgabe:
        ausgabe.write_text(r.text, encoding="utf-8")
        typer.echo(f"{r.ersetzt} Ersetzungen, geschrieben nach {ausgabe}", err=True)
    else:
        typer.echo(r.text, nl=False)
    if r.verbleibend:
        typer.echo("Nicht aufgelöst: " + ", ".join(r.verbleibend), err=True)
        for rest, hinweis in r.hinweise.items():
            typer.echo(f"  {rest}: {hinweis}", err=True)
        raise typer.Exit(code=3)


@app.command()
def rueckersetzen(
    textdatei: Annotated[Path | None, typer.Argument(
        help="Text des Sprachmodells (sonst Standardeingabe).")] = None,
    kennung: Annotated[str | None, typer.Option(
        "--kennung", "-k", "--buchstabe", help="Pseudonym-Kennung, z. B. K7Q4MX oder A.")] = None,
    suche: Annotated[str | None, typer.Option("--suche", "-s",
                                              help="Namensteil des Patienten.")] = None,
    form: Annotated[str, typer.Option("--form", help="'vollname' oder 'anrede'.")] = "vollname",
    ausgabe: Annotated[Path | None, typer.Option("--ausgabe", "-o", help="Zieldatei.")] = None,
    speicher: SpeicherOption = None,
    kapsel: Annotated[Path | None, typer.Option(
        "--kapsel", exists=True,
        help="Schlüsselkapsel eines Absenders (<name>_pseudonym.kapsel.asc) statt der lokalen"
             " Datenbank.")] = None,
    privater_schluessel: Annotated[Path | None, typer.Option(
        "--privater-schluessel", exists=True,
        help="Privater OpenPGP-Schlüssel als Datei; ohne Angabe entschlüsselt GnuPG aus dem"
             " Schlüsselbund.")] = None,
    passphrase: Annotated[str | None, typer.Option(
        "--passphrase", envvar=umgebung.namen("PGP_PASSPHRASE"),
        help="Passphrase des privaten Schlüssels (sonst fragt der GnuPG-Agent).")] = None,
) -> None:
    """Pseudonyme im Text des Sprachmodells durch die Klardaten ersetzen (lokal)."""
    from pseudonymed.pseudonyme.rueckersetzung import rueckersetzen as _rueck

    wahl_pruefen("--form", form, ("vollname", "anrede"))
    if kapsel is not None:
        info, zuordnung, bekannte = _zuordnung_aus_kapsel(kapsel, privater_schluessel,
                                                          passphrase, speicher)
    elif kennung or suche:
        info, zuordnung, bekannte = _zuordnung_aus_speicher(kennung, suche, speicher)
    else:
        abbruch("Bitte --kennung, --suche oder --kapsel angeben.")
    text = textdatei.read_text(encoding="utf-8") if textdatei else sys.stdin.read()
    r = _rueck(text, zuordnung, info, form=form, bekannte_kennungen=bekannte)
    _rueckersetzung_ausgeben(r, ausgabe)


@app.command()
def pseudonyme(speicher: SpeicherOption = None) -> None:
    """Vergebene Kennungen auflisten, mit verknüpften Schreibweisen (nur lokal)."""
    with speicher_oeffnen(speicher) as sp:
        eintraege = sp.finde()
        aliasse = {e["schluessel"]: sp.aliasse(e["schluessel"]) for e in eintraege}
    if not eintraege:
        typer.echo("Noch keine Pseudonyme vergeben.")
        return
    for e in eintraege:
        geb = e["geburtsdatum"] or "–"
        typer.echo(f"{e['kennung']:>8}  {e['nachname']}, {e['vorname']}  geb. {geb}"
                   f"  ({e['angelegt'][:10]})")
        for a in aliasse[e["schluessel"]]:
            typer.echo(f"{'':8}  = {a['nachname']}, {a['vorname']}  geb. {a['geburtsdatum'] or '–'}"
                       f"  (Abgleich {a['score']:.2f})")
