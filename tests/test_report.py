"""The report text: every block is written by its own helper, assembled in a fixed order,
and the printed text does not change (the CLI shows it).  All data is synthetic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pseudonymed.checks import report as rep
from pseudonymed.core.model import Recheck, Report, RunLog

DE = rep._Words("de")

EXPECTED = (
    "Werkzeug 1.0 – Prüfbericht\n"
    "Quelle: befund.pdf\n"
    "Ziel: (nicht freigegeben)\n"
    "Seiten: 2    Textebene: nein\n"
    "Zeitpunkt: 2026-01-02T03:04:05+01:00\n"
    "Treffer: 4\n"
    "  Seite 1: 3  (name.nachname 1, name.vorname 2)\n"
    "  Seite 2: 1  (muster.email 1)\n"
    "Pseudonyme: Patient ABCDE, [geb. 1990]\n"
    "Gegenprobe: NICHT bestanden (1 Resttreffer) – nichts exportiert\n"
    "  Seite 2: name.nachname (ocr)\n"
    "  Anmerkung: Seiten ohne Textebene konnten nicht geprüft werden\n"
    "Hinweise (durchgeführte Schritte):\n"
    "  - Ausgabe gerastert.\n"
    "Warnungen:\n"
    "  - Nicht freigegeben: Ausgabe verworfen.\n"
)


def _matches(page: int, rule: str) -> dict[str, Any]:
    return {"seite": page, "art": "name", "regel": rule, "pseudonym": "X", "rects": [],
            "quelle": "text"}


def _report(**fields: Any) -> Report:
    values: dict[str, Any] = dict(source="befund.pdf", target="", pages=2,
                                  timestamp="2026-01-02T03:04:05+01:00", tool="Werkzeug 1.0")
    values.update(fields)
    return Report(**values)


def test_text_stays_verbatim() -> None:
    b = _report(
        text_layer=False,
        hits=[_matches(2, "muster.email"), _matches(1, "name.vorname"),
              _matches(1, "name.nachname"), _matches(1, "name.vorname")],
        pseudonyms=["Patient ABCDE", "[geb. 1990]"],
        recheck=Recheck(residual_matches=[{"seite": 2, "regel": "name.nachname",
                                           "quelle": "ocr"}],
                        remarks=["Seiten ohne Textebene konnten nicht geprüft werden"]),
        notes=["Ausgabe gerastert."], warnings=["Nicht freigegeben: Ausgabe verworfen."],
    )
    assert rep.as_text(b) == EXPECTED


def test_match_lines_in_page_and_rule_order() -> None:
    b = _report(hits=[_matches(3, "b.regel"), _matches(1, "z.regel"),
                      _matches(1, "a.regel"), _matches(1, "z.regel")])
    assert rep._match_lines(b, DE) == [
        "  Seite 1: 3  (a.regel 1, z.regel 2)",
        "  Seite 3: 1  (b.regel 1)",
        "Pseudonyme: –",
    ]
    assert rep._match_lines(_report(pseudonyms=["A", "B"]), DE) == ["Pseudonyme: A, B"]


def test_recheck_without_run_and_passed() -> None:
    assert rep._recheck_lines(None, DE) == ["Gegenprobe: nicht durchgeführt"]
    assert rep._recheck_lines(Recheck(), DE) == ["Gegenprobe: bestanden"]
    g = Recheck(checked=["Telefon", "E-Mail"],
                kept_on_purpose=["name.nachname", "muster.email", "name.nachname"],
                remarks=["Notiz"])
    assert rep._recheck_lines(g, DE) == [
        "Gegenprobe: bestanden",
        "  Erneut geprüft nach dem Prüfschritt: Telefon, E-Mail – nichts gefunden",
        "  Bewusst belassen (im Prüfschritt verworfen): muster.email ×1, name.nachname ×2",
        "  Anmerkung: Notiz",
    ]


def test_recheck_failed_truncates_the_residual_matches() -> None:
    rest = [{"seite": i, "regel": "r", "quelle": "text"} for i in range(1, 26)]
    lines = rep._recheck_lines(Recheck(residual_matches=rest, remarks=["Notiz"]), DE)
    assert lines[0] == "Gegenprobe: NICHT bestanden (25 Resttreffer) – nichts exportiert"
    shown = rep.RESIDUAL_MATCHES_IN_TEXT
    assert lines[1:1 + shown] == [f"  Seite {i}: r (text)" for i in range(1, 21)]
    assert lines[1 + rep.RESIDUAL_MATCHES_IN_TEXT:] == ["  Anmerkung: Notiz"]
    # metadata or structure left over fails the re-check without a single residual hit
    assert rep._recheck_lines(Recheck(metadata_empty=False), DE) == [
        "Gegenprobe: NICHT bestanden (0 Resttreffer) – nichts exportiert"]


def test_section_omitted_without_entries() -> None:
    assert rep._section("Warnungen:", [], DE) == []
    assert rep._section("Warnungen:", ["a", "b"], DE) == ["Warnungen:", "  - a", "  - b"]


def test_residual_risk_only_when_computed() -> None:
    from identicalc import Estimate

    from pseudonymed.checks.residual_risk import stored

    assert rep._residual_risk_lines(_report(residual_risk=None), "de") == []
    assert rep._residual_risk_lines(_report(residual_risk={}), "de") == []
    lines = rep._residual_risk_lines(_report(residual_risk=stored(Estimate())), "de")
    assert lines and lines[0].startswith("Restrisiko (Schätzung")


def test_create_report_copies_the_lists(tmp_path: Path) -> None:
    warnings = ["eins"]
    b = rep.create_report(tmp_path / "q.pdf", None, 1, True, [], None,
                          RunLog(warnings=warnings))
    warnings.append("zwei")
    assert b.warnings == ["eins"] and b.target == "" and b.source == "q.pdf"
