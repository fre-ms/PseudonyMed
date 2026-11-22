"""Every message of the core exists in both interface languages.

The core keeps writing German (the command line, working states and the code read it);
what the desktop also shows in English goes through ``say(german, english)``.  The guard
below reads the core's source and fails on any German text that is neither inside
``say(...)`` nor on the short list of texts that are data, pseudonyms or document content.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest

from pseudonymed.core.text import Text, error_text, in_language, joined, read_back, say
from tests.test_gui import qapp  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent / "pseudonymed"

GERMAN = re.compile(
    r"[äöüÄÖÜß]|\b(?:der|die|das|und|nicht|ist|sind|kein|keine|keinen|mit|für|von|wird|"
    r"werden|nur|noch|Seite|Seiten|Treffer|Datei|bitte|oder|aus|bei|zu|im|den|dem|des|ein|"
    r"eine|einer|als|auf|wurde|wurden|Fehler|gefunden|Dokument|Schlüssel|gespeichert|"
    r"geprüft|ja|nein|vorhanden|Datenbank|Eintrag|Einträge|Zuordnungen|Kennung|Vokabular|"
    r"Lexikon|Konfiguration|Integrität|frei|Größe|Merkmal|Referenz|Personen|unbekannt|"
    r"gelesen|geladen|Ziel|Quelle|Zeitpunkt|Pseudonyme|Gegenprobe|bestanden|Warnungen|"
    r"Hinweise|Anmerkung|Leere|Unbekanntes|Ausgabe|gerastert|verworfen|Beruf|fehlt|"
    r"Passphrase|Pfade|Archiv|meldet|Modell|Transkript|Auswahl)\b")

# German literals that are not messages: data the code matches against, pseudonyms and
# other text that goes into the pseudonymised document, file and table names.
NOT_MESSAGES = {
    "checks/recheck.py": {  # templates, formatted into say(...) below them
        "{n} eingebettete Datei(en)", "Lesezeichen vorhanden", "{n} Anmerkung(en) vorhanden",
        "{n} Formularfeld(er) vorhanden"},
    "checks/residual_risk.py": {
        "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember",
        "bitte", "frei"},
    "detection/extra_data.py": {"träger"},  # a column name of the ministry's workbook
    "detection/fuzzy.py": {"März"},
    "detection/identifiers.py": {"Beruf", "Städtisches Klinikum", "Universitätsklinik",
                                 "Universitätsklinikum", "und", "der", "die", "das", "des",
                                 "dem", "den", "im", "in"},
    "detection/lexicon.py": {"Körperteile", "ja", "Beruf"},
    "detection/medical_terms.py": {"manöver", "phänomen", "Körperteile"},
    "detection/ner.py": {"sjögren"},
    "detection/number_words.py": {"ein", "eine", "einer", "und"},
    "detection/rules.py": {"[geschwärzt]"},
    "detection/variants.py": {"von", "der", "den", "zu", "und", "Jänner", "März", "Mär", "ß"},
    "detection/vocabulary.py": {"Körperteile"},
    "document/text_file.py": {"[Anhänge, nicht enthalten: "},
    "pipeline/pages.py": {"=== Seite ", "--- Bildtext (OCR, ungeprüft) ---",
                          "\n\n--- Bildtext (OCR, ungeprüft) ---\n\n",
                          "## Bildtext (OCR, ungeprüft)"},
    "pipeline/preparation.py": {"[geschwärzt]"},
    "pipeline/reading.py": {"aus"},
    "pipeline/text_files.py": {"[geschwärzt]"},
    "pseudonyms/assignment.py": {"Patient"},
    "pseudonyms/backup.py": {"verworfen"},  # a table name
    "pseudonyms/statistics.py": {"Patient", "Patient ", "Angehörige", "[Straße ", "Straße",
                                 "PLZ und Ort", "[Beruf", "Beruf"},  # category labels (words)
    "pseudonyms/patient_code.py": {"von", "der", "den", "zu", "dem", "im", "in", "bei", "auf"},
    "redaction/pdf.py": {"pseudonymisiert mit PseudonyMed"},
    "core/__init__.py": {"Patient"},
}
# literals by their beginning: the citation stored with the ministry's rows (data, like the
# citations in the first line of every bundled file)
NOT_MESSAGE_PREFIXES = {"detection/extra_data.py": ("Quelle: Bundesministerium",)}
# f-strings that are document content (pseudonyms, page headings), by their source
NOT_MESSAGE_FSTRINGS = (
    'f"[Straße {code}]"', 'f"Angehörige {self.code}-{number}"',
    'f"=== Seite {s.number + 1} ===\\n\\n{text}"',
)


def _core_modules() -> list[Path]:
    """The core: everything but the command line (German only) and the Qt pages; the
    session model of the desktop belongs to the core."""
    files = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if rel.parts[0] == "cli":
            continue
        if rel.parts[0] == "gui" and p.name != "session.py":
            continue
        if rel.as_posix() in ("core/text.py", "document/optdoc_adapter.py"):
            continue  # the translation tables themselves
        files.append(p)
    return files


def _unwrapped_german(path: Path) -> list[str]:
    """German literals of one module outside ``say(...)``, docstrings, regexes, SQL,
    subscripts and dict keys."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    docstrings = {n.body[0].value for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                    ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}
    inner = {v for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    rel = path.relative_to(ROOT).as_posix()  # the keys use /
    allowed = NOT_MESSAGES.get(rel, set())
    found = []
    for node in ast.walk(tree):
        if node in docstrings or node in inner:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = ast.get_source_segment(source, node) or ""
        else:
            continue
        if text.startswith(("https://", *NOT_MESSAGE_PREFIXES.get(rel, ()))):
            continue
        if not GERMAN.search(text) or "(?" in text or "\\b" in text or text in allowed \
                or text in NOT_MESSAGE_FSTRINGS or text.lstrip().startswith(
                    ("SELECT", "INSERT", "CREATE", "UPDATE", "DELETE")):
            continue
        if _inside_say(node, parents) or _not_a_message(node, parents):
            continue
        found.append(f"{rel}:{node.lineno}: {text[:80]!r}")
    return found


def _inside_say(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether the literal is (part of) an argument of ``say``."""
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "say":
            return True
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)):
            return False
    return False


def _not_a_message(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Keys and lookups: a subscript, a dict key, the pattern of a regex call, the text a
    German message is searched for (``"Arbeitsstand" in w``), a language switch's German
    branch (``"…" if not english … else``)."""
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript) or (isinstance(parent, ast.Dict)
                                             and node in parent.keys):
        return True
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) and \
            parent.func.attr in ("compile", "search", "match", "fullmatch", "sub", "findall",
                                 "execute", "get", "startswith", "endswith", "split"):
        return True
    if isinstance(parent, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn, ast.Eq))
                                               for op in parent.ops):
        return True
    return _in_language_branch(node, parents)


def _in_language_branch(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether the literal sits in a function that picks the wording by language (it takes
    a ``language`` parameter or calls ``english``/``_Words``)."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {a.arg for a in node.args.args + node.args.kwonlyargs}
            calls = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            return "language" in names or "w" in names or "english" in calls
    return False


def test_every_core_message_has_an_english_form() -> None:
    found = [x for p in _core_modules() for x in _unwrapped_german(p)]
    assert found == [], "German messages without say(...):\n" + "\n".join(found)


def test_text_behaves_as_its_german_string() -> None:
    t = say("Seite 3 gelesen", "Page 3 read")
    assert t == "Seite 3 gelesen" and str(t) == "Seite 3 gelesen" and f"{t}" == "Seite 3 gelesen"
    assert "gelesen" in t and json.dumps([t]) == '["Seite 3 gelesen"]'
    assert copy.deepcopy(t).en == "Page 3 read" and copy.copy(t).en == "Page 3 read"
    assert in_language(t, "de") == "Seite 3 gelesen" and in_language(t, "en_GB") == "Page 3 read"
    assert isinstance(t, str) and isinstance(t, Text)


def test_plain_text_and_errors() -> None:
    assert in_language("Ort", "en") == "Place" and in_language("Ort", "de") == "Ort"
    assert in_language("frei erfunden", "en") == "frei erfunden"
    assert read_back("Ort Weitnau (3.512 Einwohner)") == "Place Weitnau (3,512 inhabitants)"
    assert joined([say("a", "A"), "Ort"], "en") == "A, Place"
    e = ValueError(say("Kein Patient gewählt.", "No patient chosen."))
    assert str(e) == "Kein Patient gewählt." and error_text(e, "en") == "No patient chosen."
    assert error_text(ValueError("plain"), "en") == "plain"


@pytest.mark.parametrize("language", ["de", "en"])
def test_report_text_in_the_interface_language(language: str) -> None:
    from pseudonymed.checks.report import as_text
    from pseudonymed.core.model import Recheck, Report

    r = Report(source="a.pdf", target="", pages=1, timestamp="2026-11-22T12:00:00+01:00",
               tool="PseudonyMed 0.1.0", recheck=Recheck(remarks=[say("Lesezeichen vorhanden",
                                                                      "Bookmarks present")]),
               warnings=[say("Nicht freigegeben: Ausgabe verworfen.",
                             "Not released: output discarded.")])
    text = as_text(r, language)
    if language == "de":
        assert "Prüfbericht" in text and "Lesezeichen vorhanden" in text
    else:
        assert "audit report" in text and "Bookmarks present" in text
        assert not GERMAN.search(text.replace("PseudonyMed", "").replace("a.pdf", "")), text


def _messages(res: object) -> list[object]:
    """Every message a run hands to the user: warnings, notes, re-check remarks and kinds,
    the residual-risk descriptions and flags."""
    report = res.report  # type: ignore[attr-defined]
    found: list[object] = [*report.warnings, *report.notes]
    if report.recheck is not None:
        found += [*report.recheck.remarks, *report.recheck.checked]
    risk = res.residual_risk  # type: ignore[attr-defined]
    if risk is not None:
        found += [m.description for m in risk.features] + [mk.text for mk in risk.markers]
    return found


def test_a_run_speaks_both_languages(tmp_path: Path, report_pdf: Path, patient: object) -> None:
    """Every message of a run carries its English form, and the report written for the
    English interface is English: the JSON keys stay those of the stored format."""
    from pseudonymed.checks.report import as_text
    from pseudonymed.pipeline import PipelineOptions, pseudonymise
    from pseudonymed.pseudonyms.store import PseudonymStore
    from tests.synthetic import create_transcript

    transcript = create_transcript(tmp_path / "gespraech.txt", patient)  # type: ignore[arg-type]
    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        for source in (report_pdf, transcript):
            opt = PipelineOptions(target_folder=tmp_path / source.stem, language="en")
            res = pseudonymise(source, patient, opt, store=sp)  # type: ignore[arg-type]
            plain = [m for m in _messages(res) if not isinstance(m, Text)]
            assert plain == [], plain
            text = as_text(res.report, "en")
            for word in ("Prüfbericht", "Gegenprobe", "Hinweise", "Warnungen", "Restrisiko"):
                assert word not in text, (word, text)
            assert "audit report" in text and "Re-check: passed" in text
            stored = json.loads(res.target_report.read_text(encoding="utf-8"))
            assert stored["gegenprobe"]["geprueft"][0] == "Name" and "hinweise" in stored
            assert all(in_language(m, "en") in stored["hinweise"] for m in res.report.notes)
            german = as_text(res.report)  # the command line's report is unchanged
            assert "Prüfbericht" in german and "Gegenprobe: bestanden" in german


def test_a_resumed_review_keeps_the_english_form(tmp_path: Path, report_pdf: Path,
                                                patient: object) -> None:
    """A saved working state keeps the English form of its messages next to the German ones,
    and a resumed review shows them again."""
    from pseudonymed import working_state as state_mod
    from pseudonymed.pipeline import PipelineOptions, prepare
    from pseudonymed.pseudonyms.store import PseudonymStore

    with PseudonymStore(tmp_path / "s.sqlite") as sp:
        v = prepare(report_pdf, patient, PipelineOptions(target_folder=tmp_path),  # type: ignore[arg-type]
                    store=sp)
        assert all(isinstance(n, Text) for n in v.notes + v.warnings)
        _, json_path = state_mod.save(v, tmp_path / "stand")
        v.close()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert len(data["hinweise_en"]) == len(data["hinweise"])
        again = state_mod.preparation_from(state_mod.read(json_path), sp)
        try:
            assert again.notes and all(isinstance(n, Text) for n in again.notes)
            assert any(in_language(n, "en").startswith("Working state taken over")
                       for n in again.notes)
        finally:
            again.close()


def test_the_english_desktop_reports_in_english(qapp: object, tmp_path: Path,  # noqa: F811
                                                report_pdf: Path, patient: object) -> None:
    """With the English translation installed, the control step, its lights and table, the
    exported report and a later session's earlier weights are English."""
    from pseudonymed.gui import translation
    from pseudonymed.gui.main_window import MainWindow
    from pseudonymed.gui.session import Session

    translator = translation.install(qapp, "en_gb")  # type: ignore[arg-type]
    assert translator is not None
    try:
        for run in (1, 2):
            session = Session(store_path=tmp_path / "s.sqlite")
            window = MainWindow(session)
            assert session.language == "en"
            for page in (window.documents, window.analyse, window.export, window.control):
                page.synchronous = True
            window.patient.fill_in(patient)  # type: ignore[arg-type]
            window.patient.adopt()
            window.documents.add([report_pdf])
            window.analyse.analyse()
            ko = window.control
            window.show_page(ko)
            qapp.processEvents()  # type: ignore[attr-defined]
            log = ko.log.toPlainText()
            assert "audit report" in log and "Gegenprobe" not in log, log
            assert ko.traffic_light_text.text().startswith("Re-check passed"), \
                ko.traffic_light_text.text()
            cells = [ko.features.item(i, 0).text() for i in range(ko.features.rowCount())]
            assert cells and not any("Geschlecht" in c or "Geburtsjahr" in c for c in cells)
            assert "similar people" in ko.risk_text.text()
            if run == 1:
                window.export.target.setText(str(tmp_path / "out"))
                window.export.refresh()
                window.export.risk_confirmed.setChecked(True)
                window.export.run_export()
                qapp.processEvents()  # type: ignore[attr-defined]
                result = session.documents[0].result
                assert result is not None and result.released
                stored = json.loads(result.target_report.read_text(encoding="utf-8"))
                assert stored["gegenprobe"]["geprueft"][1] == "Surname"
            else:  # the weights of the first document come back from the store
                assert "earlier" in ko.risk_text.text()
            window.close()
    finally:
        qapp.removeTranslator(translator)  # type: ignore[attr-defined]
