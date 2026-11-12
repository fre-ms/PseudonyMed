"""The report text: every block is written by its own helper, assembled in a fixed order,
and the printed text does not change (the CLI shows it).  All data is synthetic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pseudonymed.kern.modell import Gegenprobe, Pruefbericht
from pseudonymed.pruefung import bericht as bm

ERWARTET = (
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


def _treffer(seite: int, regel: str) -> dict[str, Any]:
    return {"seite": seite, "art": "name", "regel": regel, "pseudonym": "X", "rects": [],
            "quelle": "text"}


def _bericht(**felder: Any) -> Pruefbericht:
    werte: dict[str, Any] = dict(quelle="befund.pdf", ziel="", seiten=2,
                                 zeitpunkt="2026-01-02T03:04:05+01:00", werkzeug="Werkzeug 1.0")
    werte.update(felder)
    return Pruefbericht(**werte)


def test_text_bleibt_wortgleich() -> None:
    b = _bericht(
        textebene=False,
        fundstellen=[_treffer(2, "muster.email"), _treffer(1, "name.vorname"),
                     _treffer(1, "name.nachname"), _treffer(1, "name.vorname")],
        pseudonyme=["Patient ABCDE", "[geb. 1990]"],
        gegenprobe=Gegenprobe(resttreffer=[{"seite": 2, "regel": "name.nachname",
                                            "quelle": "ocr"}],
                              anmerkungen=["Seiten ohne Textebene konnten nicht geprüft werden"]),
        hinweise=["Ausgabe gerastert."], warnungen=["Nicht freigegeben: Ausgabe verworfen."],
    )
    assert bm.als_text(b) == ERWARTET


def test_trefferzeilen_in_seiten_und_regelreihenfolge() -> None:
    b = _bericht(fundstellen=[_treffer(3, "b.regel"), _treffer(1, "z.regel"),
                              _treffer(1, "a.regel"), _treffer(1, "z.regel")])
    assert bm._trefferzeilen(b) == [
        "  Seite 1: 3  (a.regel 1, z.regel 2)",
        "  Seite 3: 1  (b.regel 1)",
        "Pseudonyme: –",
    ]
    assert bm._trefferzeilen(_bericht(pseudonyme=["A", "B"])) == ["Pseudonyme: A, B"]


def test_gegenprobe_ohne_lauf_und_bestanden() -> None:
    assert bm._gegenprobezeilen(None) == ["Gegenprobe: nicht durchgeführt"]
    assert bm._gegenprobezeilen(Gegenprobe()) == ["Gegenprobe: bestanden"]
    g = Gegenprobe(geprueft=["Telefon", "E-Mail"],
                   belassen=["name.nachname", "muster.email", "name.nachname"],
                   anmerkungen=["Notiz"])
    assert bm._gegenprobezeilen(g) == [
        "Gegenprobe: bestanden",
        "  Erneut geprüft nach dem Prüfschritt: Telefon, E-Mail – nichts gefunden",
        "  Bewusst belassen (im Prüfschritt verworfen): muster.email ×1, name.nachname ×2",
        "  Anmerkung: Notiz",
    ]


def test_gegenprobe_nicht_bestanden_kuerzt_die_resttreffer() -> None:
    rest = [{"seite": i, "regel": "r", "quelle": "text"} for i in range(1, 26)]
    zeilen = bm._gegenprobezeilen(Gegenprobe(resttreffer=rest, anmerkungen=["Notiz"]))
    assert zeilen[0] == "Gegenprobe: NICHT bestanden (25 Resttreffer) – nichts exportiert"
    assert zeilen[1:1 + bm.RESTTREFFER_IM_TEXT] == [f"  Seite {i}: r (text)" for i in range(1, 21)]
    assert zeilen[1 + bm.RESTTREFFER_IM_TEXT:] == ["  Anmerkung: Notiz"]
    # metadata or structure left over fails the re-check without a single residual hit
    assert bm._gegenprobezeilen(Gegenprobe(metadaten_leer=False)) == [
        "Gegenprobe: NICHT bestanden (0 Resttreffer) – nichts exportiert"]


def test_abschnitt_entfaellt_ohne_eintraege() -> None:
    assert bm._abschnitt("Warnungen:", []) == []
    assert bm._abschnitt("Warnungen:", ["a", "b"]) == ["Warnungen:", "  - a", "  - b"]


def test_restrisiko_nur_wenn_berechnet() -> None:
    from pseudonymed.pruefung.restrisiko import Restrisiko

    assert bm._restrisikozeilen(_bericht(restrisiko=None)) == []
    assert bm._restrisikozeilen(_bericht(restrisiko={})) == []
    zeilen = bm._restrisikozeilen(_bericht(restrisiko=Restrisiko().als_dict()))
    assert zeilen and zeilen[0].startswith("Restrisiko (Schätzung")


def test_erstelle_bericht_kopiert_die_listen(tmp_path: Path) -> None:
    warnungen = ["eins"]
    b = bm.erstelle_bericht(tmp_path / "q.pdf", None, 1, True, [], None, warnungen=warnungen)
    warnungen.append("zwei")
    assert b.warnungen == ["eins"] and b.ziel == "" and b.quelle == "q.pdf"
