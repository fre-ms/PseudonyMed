"""The audit report: counts per page and rule, pseudonyms, result of the check."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from pseudonymed import WERKZEUG
from pseudonymed.kern.modell import Fundstelle, Gegenprobe, Pruefbericht

RESTTREFFER_IM_TEXT = 20  # residual hits listed in the text; a longer list is cut here


def erstelle_bericht(
    quelle: Path,
    ziel: Path | None,
    seiten: int,
    textebene: bool,
    fundstellen: list[Fundstelle],
    gegenprobe: Gegenprobe | None,
    optionen: dict[str, Any] | None = None,
    warnungen: list[str] | None = None,
    hinweise: list[str] | None = None,
    restrisiko: dict[str, Any] | None = None,
) -> Pruefbericht:
    """Build the report of one run without any clear text: the hits keep page, rule and
    pseudonym only, the pseudonyms are listed once in sorted order, and ``ziel`` stays empty
    when the output was not released.  The lists are copied so later changes by the caller
    do not reach the report."""
    pseudonyme = sorted({f.pseudonym for f in fundstellen})
    return Pruefbericht(
        quelle=quelle.name,
        ziel=ziel.name if ziel else "",
        seiten=seiten,
        zeitpunkt=Pruefbericht.jetzt(),
        werkzeug=WERKZEUG,
        textebene=textebene,
        fundstellen=[f.ohne_klartext() for f in fundstellen],
        pseudonyme=pseudonyme,
        gegenprobe=gegenprobe,
        optionen=optionen or {},
        warnungen=list(warnungen or []),
        hinweise=list(hinweise or []),
        restrisiko=restrisiko,
    )


def als_text(b: Pruefbericht) -> str:
    """Render the report as the text the CLI prints and the desktop shows: the header, the
    hits per page with the pseudonyms, the re-check, the residual risk, then the hints and
    the warnings.  Blocks without content are left out; the text ends with a newline."""
    zeilen = [
        *_kopfzeilen(b),
        *_trefferzeilen(b),
        *_gegenprobezeilen(b.gegenprobe),
        *_restrisikozeilen(b),
        *_abschnitt("Hinweise (durchgeführte Schritte):", b.hinweise),
        *_abschnitt("Warnungen:", b.warnungen),
    ]
    return "\n".join(zeilen) + "\n"


def _kopfzeilen(b: Pruefbericht) -> list[str]:
    """The header: tool, source, target (or that it was withheld), pages and text layer,
    time of the run and the total number of hits."""
    return [
        f"{b.werkzeug} – Prüfbericht",
        f"Quelle: {b.quelle}",
        f"Ziel: {b.ziel or '(nicht freigegeben)'}",
        f"Seiten: {b.seiten}    Textebene: {'ja' if b.textebene else 'nein'}",
        f"Zeitpunkt: {b.zeitpunkt}",
        f"Treffer: {len(b.fundstellen)}",
    ]


def _trefferzeilen(b: Pruefbericht) -> list[str]:
    """One line per page with its hit total and the count per rule, in page order, followed
    by the pseudonyms that were issued (a dash when there were none)."""
    je_seite: dict[int, Counter[str]] = {}
    for f in b.fundstellen:
        je_seite.setdefault(f["seite"], Counter())[f["regel"]] += 1
    zeilen = [_seitenzeile(seite, je_seite[seite]) for seite in sorted(je_seite)]
    zeilen.append("Pseudonyme: " + (", ".join(b.pseudonyme) if b.pseudonyme else "–"))
    return zeilen


def _seitenzeile(seite: int, regeln: Counter[str]) -> str:
    """The line for one page: its hit total, then every rule with its count, sorted by rule."""
    aufzaehlung = ", ".join(f"{r} {n}" for r, n in sorted(regeln.items()))
    return f"  Seite {seite}: {sum(regeln.values())}  ({aufzaehlung})"


def _gegenprobezeilen(g: Gegenprobe | None) -> list[str]:
    """The re-check block: its verdict with the details that explain it, then its notes."""
    if g is None:
        return ["Gegenprobe: nicht durchgeführt"]
    zeilen = _bestanden_zeilen(g) if g.bestanden else _resttrefferzeilen(g)
    zeilen.extend(f"  Anmerkung: {a}" for a in g.anmerkungen)
    return zeilen


def _bestanden_zeilen(g: Gegenprobe) -> list[str]:
    """A passed re-check: which identifier kinds were searched again and which rules' hits
    were kept on purpose, each line only when there is something to say."""
    zeilen = ["Gegenprobe: bestanden"]
    if g.geprueft:
        zeilen.append("  Erneut geprüft nach dem Prüfschritt: " + ", ".join(g.geprueft)
                      + " – nichts gefunden")
    if g.belassen:
        zaehlung = Counter(g.belassen)
        zeilen.append("  Bewusst belassen (im Prüfschritt verworfen): "
                      + ", ".join(f"{r} ×{n}" for r, n in sorted(zaehlung.items())))
    return zeilen


def _resttrefferzeilen(g: Gegenprobe) -> list[str]:
    """A failed re-check: the verdict with the number of residual hits, then the first
    ``RESTTREFFER_IM_TEXT`` of them, so a long list does not drown the rest of the report."""
    zeilen = [f"Gegenprobe: NICHT bestanden ({len(g.resttreffer)} Resttreffer)"
              " – nichts exportiert"]
    zeilen.extend(f"  Seite {r['seite']}: {r['regel']} ({r['quelle']})"
                  for r in g.resttreffer[:RESTTREFFER_IM_TEXT])
    return zeilen


def _restrisikozeilen(b: Pruefbericht) -> list[str]:
    """The residual-risk estimate as its own lines, or nothing when none was computed.  The
    estimate's module is imported here, as before, so that the report module does not
    depend on it at import time."""
    if not b.restrisiko:
        return []
    from pseudonymed.pruefung.restrisiko import Restrisiko

    return Restrisiko.aus_dict(b.restrisiko).als_zeilen()


def _abschnitt(titel: str, eintraege: list[str]) -> list[str]:
    """A titled list, one indented entry per line; nothing at all when the list is empty,
    so that an empty block never appears in the report."""
    if not eintraege:
        return []
    return [titel, *(f"  - {e}" for e in eintraege)]


def speichere_bericht(b: Pruefbericht, pfad: Path) -> None:
    """Write the report as indented UTF-8 JSON with a trailing newline to ``pfad``; umlauts
    stay unescaped so the file reads well by eye."""
    pfad.write_text(json.dumps(b.als_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
