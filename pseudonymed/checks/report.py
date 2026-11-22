"""The audit report: counts per page and rule, pseudonyms, result of the check."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pseudonymed import TOOL
from pseudonymed.core.model import Hit, Recheck, Report, RunLog

RESIDUAL_MATCHES_IN_TEXT = 20  # residual hits listed in the text; a longer list is cut here


def create_report(
    source: Path,
    target: Path | None,
    pages: int,
    text_layer: bool,
    hits: list[Hit],
    recheck: Recheck | None,
    log: RunLog | None = None,
) -> Report:
    """Build the report of one run without any clear text: the hits keep page, rule and
    pseudonym only, the pseudonyms are listed once in sorted order, and ``target`` stays empty
    when the output was not released.  The protocol's lists are copied so later changes by
    the caller do not reach the report."""
    pseudonyms = sorted({f.pseudonym for f in hits})
    log = log or RunLog()
    return Report(
        source=source.name,
        target=target.name if target else "",
        pages=pages,
        timestamp=Report.now(),
        tool=TOOL,
        text_layer=text_layer,
        hits=[f.without_clear_text() for f in hits],
        pseudonyms=pseudonyms,
        recheck=recheck,
        options=log.options,
        warnings=list(log.warnings),
        notes=list(log.notes),
        residual_risk=log.residual_risk,
    )


def as_text(b: Report) -> str:
    """Render the report as the text the CLI prints and the desktop shows: the header, the
    hits per page with the pseudonyms, the re-check, the residual risk, then the hints and
    the warnings.  Blocks without content are left out; the text ends with a newline."""
    lines = [
        *_header_lines(b),
        *_match_lines(b),
        *_recheck_lines(b.recheck),
        *_residual_risk_lines(b),
        *_section("Hinweise (durchgeführte Schritte):", b.notes),
        *_section("Warnungen:", b.warnings),
    ]
    return "\n".join(lines) + "\n"


def _header_lines(b: Report) -> list[str]:
    """The header: tool, source, target (or that it was withheld), pages and text layer,
    time of the run and the total number of hits."""
    return [
        f"{b.tool} – Prüfbericht",
        f"Quelle: {b.source}",
        f"Ziel: {b.target or '(nicht freigegeben)'}",
        f"Seiten: {b.pages}    Textebene: {'ja' if b.text_layer else 'nein'}",
        f"Zeitpunkt: {b.timestamp}",
        f"Treffer: {len(b.hits)}",
    ]


def _match_lines(b: Report) -> list[str]:
    """One line per page with its hit total and the count per rule, in page order, followed
    by the pseudonyms that were issued (a dash when there were none)."""
    per_page: dict[int, Counter[str]] = {}
    for f in b.hits:
        per_page.setdefault(f["seite"], Counter())[f["regel"]] += 1
    lines = [_page_line(page, per_page[page]) for page in sorted(per_page)]
    lines.append("Pseudonyme: " + (", ".join(b.pseudonyms) if b.pseudonyms else "–"))
    return lines


def _page_line(page: int, rules: Counter[str]) -> str:
    """The line for one page: its hit total, then every rule with its count, sorted by rule."""
    listing = ", ".join(f"{r} {n}" for r, n in sorted(rules.items()))
    return f"  Seite {page}: {sum(rules.values())}  ({listing})"


def _recheck_lines(g: Recheck | None) -> list[str]:
    """The re-check block: its verdict with the details that explain it, then its notes."""
    if g is None:
        return ["Gegenprobe: nicht durchgeführt"]
    lines = _passed_lines(g) if g.passed else _residual_match_lines(g)
    lines.extend(f"  Anmerkung: {a}" for a in g.remarks)
    return lines


def _passed_lines(g: Recheck) -> list[str]:
    """A passed re-check: which identifier kinds were searched again and which rules' hits
    were kept on purpose, each line only when there is something to say."""
    lines = ["Gegenprobe: bestanden"]
    if g.checked:
        lines.append("  Erneut geprüft nach dem Prüfschritt: " + ", ".join(g.checked)
                     + " – nichts gefunden")
    if g.kept_on_purpose:
        counts = Counter(g.kept_on_purpose)
        lines.append("  Bewusst belassen (im Prüfschritt verworfen): "
                     + ", ".join(f"{r} ×{n}" for r, n in sorted(counts.items())))
    return lines


def _residual_match_lines(g: Recheck) -> list[str]:
    """A failed re-check: the verdict with the number of residual hits, then the first
    ``RESIDUAL_MATCHES_IN_TEXT`` of them, so a long list does not drown the rest of the report."""
    lines = [f"Gegenprobe: NICHT bestanden ({len(g.residual_matches)} Resttreffer)"
             " – nichts exportiert"]
    lines.extend(f"  Seite {r['seite']}: {r['regel']} ({r['quelle']})"
                 for r in g.residual_matches[:RESIDUAL_MATCHES_IN_TEXT])
    return lines


def _residual_risk_lines(b: Report) -> list[str]:
    """The residual-risk estimate as its own lines, or nothing when none was computed.  The
    estimate's module is imported here, as before, so that the report module does not
    depend on it at import time."""
    if not b.residual_risk:
        return []
    from pseudonymed.checks.residual_risk import ResidualRisk

    return ResidualRisk.from_dict(b.residual_risk).as_lines()


def _section(title: str, entries: list[str]) -> list[str]:
    """A titled list, one indented entry per line; nothing at all when the list is empty,
    so that an empty block never appears in the report."""
    if not entries:
        return []
    return [title, *(f"  - {e}" for e in entries)]


def save_report(b: Report, path: Path) -> None:
    """Write the report as indented UTF-8 JSON with a trailing newline to ``path``; umlauts
    stay unescaped so the file reads well by eye."""
    path.write_text(json.dumps(b.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
