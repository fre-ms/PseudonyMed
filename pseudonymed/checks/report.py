"""The audit report: counts per page and rule, pseudonyms, result of the check."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from pseudonymed import TOOL
from pseudonymed.core.model import Hit, Recheck, Report, RunLog
from pseudonymed.core.text import english, in_language, joined

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


def as_text(b: Report, language: str = "de") -> str:
    """Render the report as the text the CLI prints and the desktop shows, in German or (for
    the English interface) in English: the header, the hits per page with the pseudonyms,
    the re-check, the residual risk, then the hints and the warnings.  Blocks without
    content are left out; the text ends with a newline."""
    w = _Words(language)
    lines = [
        *_header_lines(b, w),
        *_match_lines(b, w),
        *_recheck_lines(b.recheck, w),
        *_residual_risk_lines(b, language),
        *_section(w("Hinweise (durchgeführte Schritte):", "Notes (steps taken):"), b.notes, w),
        *_section(w("Warnungen:", "Warnings:"), b.warnings, w),
    ]
    return "\n".join(lines) + "\n"


class _Words:
    """The wording of the report's fixed parts and of its messages in one language."""

    def __init__(self, language: str) -> None:
        self.language = language
        self.english = english(language)

    def __call__(self, de: str, en: str) -> str:
        """The German or the English wording of a fixed part."""
        return en if self.english else de

    def text(self, message: object) -> str:
        """A message of the core in the report's language."""
        return in_language(message, self.language)


def _header_lines(b: Report, w: _Words) -> list[str]:
    """The header: tool, source, target (or that it was withheld), pages and text layer,
    time of the run and the total number of hits."""
    return [
        f"{b.tool} – {w('Prüfbericht', 'audit report')}",
        f"{w('Quelle', 'Source')}: {b.source}",
        f"{w('Ziel', 'Target')}: {b.target or w('(nicht freigegeben)', '(not released)')}",
        f"{w('Seiten', 'Pages')}: {b.pages}    {w('Textebene', 'Text layer')}:"
        f" {w('ja', 'yes') if b.text_layer else w('nein', 'no')}",
        f"{w('Zeitpunkt', 'Time')}: {b.timestamp}",
        f"{w('Treffer', 'Hits')}: {len(b.hits)}",
    ]


def _match_lines(b: Report, w: _Words) -> list[str]:
    """One line per page with its hit total and the count per rule, in page order, followed
    by the pseudonyms that were issued (a dash when there were none)."""
    per_page: dict[int, Counter[str]] = {}
    for f in b.hits:
        per_page.setdefault(f["seite"], Counter())[f["regel"]] += 1
    lines = [_page_line(page, per_page[page], w) for page in sorted(per_page)]
    lines.append(w("Pseudonyme: ", "Pseudonyms: ")
                 + (", ".join(b.pseudonyms) if b.pseudonyms else "–"))
    return lines


def _page_line(page: int, rules: Counter[str], w: _Words) -> str:
    """The line for one page: its hit total, then every rule with its count, sorted by rule."""
    listing = ", ".join(f"{r} {n}" for r, n in sorted(rules.items()))
    return f"  {w('Seite', 'Page')} {page}: {sum(rules.values())}  ({listing})"


def _recheck_lines(g: Recheck | None, w: _Words) -> list[str]:
    """The re-check block: its verdict with the details that explain it, then its notes."""
    if g is None:
        return [w("Gegenprobe: nicht durchgeführt", "Re-check: not run")]
    lines = _passed_lines(g, w) if g.passed else _residual_match_lines(g, w)
    lines.extend(f"  {w('Anmerkung', 'Remark')}: {w.text(a)}" for a in g.remarks)
    return lines


def _passed_lines(g: Recheck, w: _Words) -> list[str]:
    """A passed re-check: which identifier kinds were searched again and which rules' hits
    were kept on purpose, each line only when there is something to say."""
    lines = [w("Gegenprobe: bestanden", "Re-check: passed")]
    if g.checked:
        lines.append(w("  Erneut geprüft nach dem Prüfschritt: ",
                       "  Searched again after the review: ")
                     + joined(g.checked, w.language)
                     + w(" – nichts gefunden", " – nothing found"))
    if g.kept_on_purpose:
        counts = Counter(g.kept_on_purpose)
        lines.append(w("  Bewusst belassen (im Prüfschritt verworfen): ",
                       "  Kept on purpose (discarded in the review): ")
                     + ", ".join(f"{r} ×{n}" for r, n in sorted(counts.items())))
    return lines


def _residual_match_lines(g: Recheck, w: _Words) -> list[str]:
    """A failed re-check: the verdict with the number of residual hits, then the first
    ``RESIDUAL_MATCHES_IN_TEXT`` of them, so a long list does not drown the rest of the report."""
    n = len(g.residual_matches)
    lines = [w(f"Gegenprobe: NICHT bestanden ({n} Resttreffer) – nichts exportiert",
               f"Re-check: NOT passed ({n} residual hits) – nothing exported")]
    lines.extend(f"  {w('Seite', 'Page')} {r['seite']}: {r['regel']} ({r['quelle']})"
                 for r in g.residual_matches[:RESIDUAL_MATCHES_IN_TEXT])
    return lines


def _residual_risk_lines(b: Report, language: str) -> list[str]:
    """The residual-risk estimate as its own lines, or nothing when none was computed.  The
    estimate's module is imported here, as before, so that the report module does not
    depend on it at import time."""
    if not b.residual_risk:
        return []
    from pseudonymed.checks.residual_risk import ResidualRisk

    return ResidualRisk.from_dict(b.residual_risk).as_lines(language)


def _section(title: str, entries: list[str], w: _Words) -> list[str]:
    """A titled list, one indented entry per line; nothing at all when the list is empty,
    so that an empty block never appears in the report."""
    if not entries:
        return []
    return [title, *(f"  - {w.text(e)}" for e in entries)]


def save_report(b: Report, path: Path, language: str = "de") -> None:
    """Write the report as indented UTF-8 JSON with a trailing newline to ``path``; umlauts
    stay unescaped so the file reads well by eye.  The keys are those of the stored format;
    the messages (warnings, notes, re-check remarks and kinds, residual-risk wording) are in
    ``language``."""
    path.write_text(json.dumps(as_dict(b, language), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def as_dict(b: Report, language: str = "de") -> dict[str, Any]:
    """The JSON form of the report with its messages in ``language``."""
    d = b.as_dict()
    if not english(language):
        return d
    d["warnungen"] = [in_language(x, language) for x in b.warnings]
    d["hinweise"] = [in_language(x, language) for x in b.notes]
    if b.recheck is not None:
        d["gegenprobe"]["anmerkungen"] = [in_language(x, language) for x in b.recheck.remarks]
        d["gegenprobe"]["geprueft"] = [in_language(x, language) for x in b.recheck.checked]
    if b.residual_risk:
        from pseudonymed.checks.residual_risk import ResidualRisk

        d["restrisiko"] = ResidualRisk.from_dict(b.residual_risk).as_dict(language)
    return d
