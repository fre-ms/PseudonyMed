"""Focused tests for the helpers behind the control and the re-identification page: the
cells of a document row, the rows of the feature table, the risk sentence and the source
of the mapping.  The window tests reach them only through a full run; synthetic data only."""

from __future__ import annotations

from pathlib import Path

from identicalc import Estimate, Feature, Marker, number
from PySide6.QtWidgets import QApplication

from pseudonymed.core.model import Report
from pseudonymed.gui.control_page import ControlPage
from pseudonymed.gui.reidentify_page import ReidentifyPage
from pseudonymed.gui.session import Document, Session
from pseudonymed.pipeline import Result
from pseudonymed.pseudonyms.capsule import Capsule
from tests.test_gui import qapp  # noqa: F401


def _result(residual_risk: Estimate | None, released: bool = True) -> Result:
    """A re-check result with or without an estimate, released or not."""
    report = Report(source="befund.pdf", target="befund_pseudonym.pdf", pages=1,
                    timestamp="", tool="")
    return Result(source=Path("befund.pdf"), report=report, hits=[], code="K7Q4MX",
                  target_pdf=Path("befund_pseudonym.pdf") if released else None,
                  residual_risk=residual_risk)


def _residual_risk() -> Estimate:
    """Two counted features, one only listed, one marker; λ ≈ 5.9 against 1000 people."""
    return Estimate(
        features=[
            Feature("sex", "sex", "Geschlecht", 1.0, "Patientin"),
            Feature("birth:year", "birth", "Geburtsjahr", 6.4, "[geb. 1959]"),
            Feature("place:2", "place", "Ort (zweiter)", 12.0, "Kleinstadt", counted=False),
        ],
        markers=[Marker("age_90", "Alter ab 90", "91-jährig")],
        reference=1000)


def test_table_line(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Without an estimate the row shows dashes; with one, λ, κ, the light and the largest
    counted contribution."""
    page = ControlPage(Session(store_path=tmp_path / "s.sqlite"))
    d = Document(path=Path("befund.pdf"))
    assert page._table_row(d, _result(None, released=False)) == [
        "befund.pdf", "NICHT bestanden", "–", "–", "–", "–"]
    r = _residual_risk()
    line = page._table_row(d, _result(r))
    assert line == ["befund.pdf", "bestanden", number(r.lam), f"{r.kappa:.2f}",
                    "GELB", "Geburtsjahr (6.4 Bit)"]
    assert page._table_row(d, _result(Estimate(reference=1000)))[5] == "–"


def test_feature_lines(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Counted features first, by bits, then the listed-only ones with their suffix, then
    the markers with "!"; nothing without an estimate."""
    page = ControlPage(Session(store_path=tmp_path / "s.sqlite"))
    assert page._feature_rows(None) == []
    lines = page._feature_rows(_residual_risk())
    assert [z[0] for z in lines] == [
        "Geburtsjahr", "Geschlecht", "Ort (zweiter) (nicht gezählt)", "! Alter ab 90"]
    assert lines[0][1:] == ("6.4", "[geb. 1959]")
    assert lines[2][1] == "12.0" and lines[3][1:] == ("", "91-jährig")


def test_risk_sentence(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """The sentence names the largest contribution and the earlier documents only when
    there are any."""
    page = ControlPage(Session(store_path=tmp_path / "s.sqlite"))
    r = _residual_risk()
    sentence = page._risk_sentence(r, 0)
    assert sentence.startswith("Alle Dokumente dieses Patienten zusammen: λ ≈ ")
    assert "GELB" in sentence and sentence.count(".") >= 2
    assert " Größter Beitrag: Geburtsjahr (6.4 Bit)." in sentence and "früher" not in sentence
    assert page._risk_sentence(r, 2).endswith(
        " Einschließlich 2 früher freigegebener Dokument(e).")
    assert "Größter Beitrag" not in page._risk_sentence(Estimate(reference=1000), 0)


def test_mapping_source(qapp: QApplication, tmp_path: Path) -> None:  # noqa: F811
    """Without a chosen person there is no source and the page says so; an opened capsule
    takes precedence over the local store."""
    page = ReidentifyPage(Session(store_path=tmp_path / "s.sqlite"))
    assert page.person.count() == 0 and page._mapping_source() is None
    page.reidentify()
    assert page.note.text() == "Keine Person gewählt." and page.output.toPlainText() == ""
    page.capsule = Capsule(code="K7Q4MX", patient={"surname": "Beispiel", "first_name": "Erika"},
                           mappings={"Patient K7Q4MX": "Erika Beispiel"})
    source = page._mapping_source()
    assert source is not None and source.known == ["K7Q4MX"]
    assert source.mapping == {"Patient K7Q4MX": "Erika Beispiel"}
    assert source.info is not None and source.info["code"] == "K7Q4MX"
    page.input_text.setPlainText("Patient K7Q4MX kam allein.")
    page.reidentify()
    assert page.output.toPlainText() == "Erika Beispiel kam allein."
    assert page.note.text() == "1 Ersetzungen, nichts offen."
