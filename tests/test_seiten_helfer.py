"""Focused tests for the helpers behind the control and the re-identification page: the
cells of a document row, the rows of the feature table, the risk sentence and the source
of the mapping.  The window tests reach them only through a full run; synthetic data only."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from pseudonymed.ablauf import Ergebnis
from pseudonymed.gui.kontrolle_seite import KontrolleSeite
from pseudonymed.gui.rueckersetzen_seite import RueckersetzenSeite
from pseudonymed.gui.sitzung import Dokument, Sitzung
from pseudonymed.kern.modell import Pruefbericht
from pseudonymed.pruefung import restrisiko as rr
from pseudonymed.pseudonyme.kapsel import Kapsel
from tests.test_gui import qapp  # noqa: F401


def _ergebnis(restrisiko: rr.Restrisiko | None, freigegeben: bool = True) -> Ergebnis:
    """A re-check result with or without an estimate, released or not."""
    bericht = Pruefbericht(quelle="befund.pdf", ziel="befund_pseudonym.pdf", seiten=1,
                           zeitpunkt="", werkzeug="")
    return Ergebnis(quelle=Path("befund.pdf"), bericht=bericht, fundstellen=[], kennung="K7Q4MX",
                    ziel_pdf=Path("befund_pseudonym.pdf") if freigegeben else None,
                    restrisiko=restrisiko)


def _restrisiko() -> rr.Restrisiko:
    """Two counted features, one only listed, one marker; λ ≈ 5.9 against 1000 people."""
    return rr.Restrisiko(
        merkmale=[
            rr.Merkmal("geschlecht", "Geschlecht", "Geschlecht", 1.0, "Patientin"),
            rr.Merkmal("geburtsjahr", "Geburtsdatum", "Geburtsjahr", 6.4, "[geb. 1959]"),
            rr.Merkmal("ort:2", "Ort", "Ort (zweiter)", 12.0, "Kleinstadt", gezaehlt=False),
        ],
        markierungen=[rr.Markierung("alter90", "Alter ab 90", "91-jährig")],
        referenz=1000)


def test_tabellenzeile(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Without an estimate the row shows dashes; with one, λ, κ, the light and the largest
    counted contribution."""
    seite = KontrolleSeite(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    d = Dokument(pfad=Path("befund.pdf"))
    assert seite._tabellenzeile(d, _ergebnis(None, freigegeben=False)) == [
        "befund.pdf", "NICHT bestanden", "–", "–", "–", "–"]
    r = _restrisiko()
    zeile = seite._tabellenzeile(d, _ergebnis(r))
    assert zeile == ["befund.pdf", "bestanden", rr._zahl(r.lam), f"{r.kappa:.2f}",
                     "GELB", "Geburtsjahr (6.4 Bit)"]
    assert seite._tabellenzeile(d, _ergebnis(rr.Restrisiko(referenz=1000)))[5] == "–"


def test_merkmalzeilen(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Counted features first, by bits, then the listed-only ones with their suffix, then
    the markers with "!"; nothing without an estimate."""
    seite = KontrolleSeite(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    assert seite._merkmalzeilen(None) == []
    zeilen = seite._merkmalzeilen(_restrisiko())
    assert [z[0] for z in zeilen] == [
        "Geburtsjahr", "Geschlecht", "Ort (zweiter) (nicht gezählt)", "! Alter ab 90"]
    assert zeilen[0][1:] == ("6.4", "[geb. 1959]")
    assert zeilen[2][1] == "12.0" and zeilen[3][1:] == ("", "91-jährig")


def test_risikosatz(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """The sentence names the largest contribution and the earlier documents only when
    there are any."""
    seite = KontrolleSeite(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    r = _restrisiko()
    satz = seite._risikosatz(r, 0)
    assert satz.startswith("Alle Dokumente dieses Patienten zusammen: λ ≈ ")
    assert "GELB" in satz and satz.count(".") >= 2
    assert " Größter Beitrag: Geburtsjahr (6.4 Bit)." in satz and "früher" not in satz
    assert seite._risikosatz(r, 2).endswith(
        " Einschließlich 2 früher freigegebener Dokument(e).")
    assert "Größter Beitrag" not in seite._risikosatz(rr.Restrisiko(referenz=1000), 0)


def test_zuordnungsquelle(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Without a chosen person there is no source and the page says so; an opened capsule
    takes precedence over the local store."""
    seite = RueckersetzenSeite(Sitzung(speicherpfad=tmp_path / "s.sqlite"))
    assert seite.person.count() == 0 and seite._zuordnungsquelle() is None
    seite.rueckersetzen()
    assert seite.hinweis.text() == "Keine Person gewählt." and seite.ausgabe.toPlainText() == ""
    seite.kapsel = Kapsel(kennung="K7Q4MX", patient={"nachname": "Beispiel", "vorname": "Erika"},
                          zuordnungen={"Patient K7Q4MX": "Erika Beispiel"})
    quelle = seite._zuordnungsquelle()
    assert quelle is not None and quelle.bekannte == ["K7Q4MX"]
    assert quelle.zuordnung == {"Patient K7Q4MX": "Erika Beispiel"}
    assert quelle.info is not None and quelle.info["kennung"] == "K7Q4MX"
    seite.eingabe.setPlainText("Patient K7Q4MX kam allein.")
    seite.rueckersetzen()
    assert seite.ausgabe.toPlainText() == "Erika Beispiel kam allein."
    assert seite.hinweis.text() == "1 Ersetzungen, nichts offen."
