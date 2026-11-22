"""The documents step on its own: files dropped on the list, the working states of the
entries, removal, loading in the background and the paper format for images.  Qt runs
offscreen; every file is synthetic (Faker de_DE)."""

from __future__ import annotations

import gc
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.core.model import Patient  # noqa: E402
from pseudonymed.gui import documents_page as module  # noqa: E402
from pseudonymed.gui.documents_page import DocumentsPage, DropZone  # noqa: E402
from pseudonymed.gui.session import Session  # noqa: E402
from pseudonymed.gui.worker import Worker  # noqa: E402
from tests.synthetic import create_transcript  # noqa: E402


@pytest.fixture
def qapp() -> Iterator[QApplication]:
    """The application; afterwards the page of the test is collected on the main thread.
    A traceback (``pytest.raises``) keeps a page alive in a reference cycle; were that cycle
    collected later inside a worker thread, Shiboken would defer the C++ deletion to the
    main thread and crash on it."""
    app = QApplication.instance()
    app = app if isinstance(app, QApplication) else QApplication([])
    yield app
    gc.collect()
    app.processEvents()


class _NeverStarted(Worker):
    """A worker whose thread never runs: the queue stays as it is."""

    def start(self, *_a: object) -> None:  # noqa: N802 - Qt override
        pass


class _Messages:
    """Stand-in for the dialogs of the page: records instead of blocking."""

    def __init__(self, files: list[str] | None = None) -> None:
        self.shown: list[tuple[object, ...]] = []
        self.files = files or []

    def warning(self, *a: object) -> None:
        self.shown.append(a)

    def getOpenFileNames(self, *_a: object) -> tuple[list[str], str]:  # noqa: N802
        return self.files, ""


def _mime(paths: list[Path], foreign: bool = False) -> QMimeData:
    """Mime data of a drag from the file manager, optionally with a remote URL in it."""
    data = QMimeData()
    urls = [QUrl.fromLocalFile(str(p)) for p in paths]
    if foreign:
        urls.append(QUrl("https://example.invalid/befund.pdf"))
    data.setUrls(urls)
    return data


def _drag[E: QDropEvent](category: type[E], data: QMimeData) -> E:
    """A drag event over the list carrying ``data``, not yet accepted by anyone."""
    event = category(QPointF(5, 5), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    event.ignore()
    return event


def _scan(report_pdf: Path, target: Path) -> Path:
    """Image-only copy of the synthetic report: every page a picture, no text layer."""
    with pymupdf.open(report_pdf) as doc, pymupdf.open() as scan:
        for page in doc:
            pix = page.get_pixmap(dpi=100, alpha=False)
            new = scan.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=pix.tobytes("png"))
        scan.save(target)
    return target


def _wait(qapp: QApplication, condition: Callable[[], bool], seconds: float = 120.0) -> None:
    """Pump the event loop until ``condition`` holds (worker threads report through it)."""
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "Zeitüberschreitung beim Warten auf den Arbeiter"
        qapp.processEvents()
        time.sleep(0.01)


def _page(tmp_path: Path, patient: Patient | None = None) -> DocumentsPage:
    """A documents page with its own session, working inline (no threads)."""
    session = Session(store_path=tmp_path / "s.sqlite")
    session.patient = patient
    page = DocumentsPage(session)
    page.synchronous = True
    return page


def test_drop_zone_accepts_local_files(qapp: QApplication, tmp_path: Path,
                                       report_pdf: Path) -> None:
    """A drag with URLs is accepted, one without is not; the drop emits the local paths
    only (a remote URL is dropped, and a drop with none of them is refused)."""
    drop_zone = DropZone()
    received: list[list[Path]] = []
    drop_zone.dropped.connect(received.append)
    note = tmp_path / "notiz.txt"
    with_files, without, mixed, foreign_only = (
        _mime([report_pdf]), QMimeData(), _mime([report_pdf, note], foreign=True),
        _mime([], foreign=True))
    entered = _drag(QDragEnterEvent, with_files)
    drop_zone.dragEnterEvent(entered)
    assert entered.isAccepted()
    moved = _drag(QDragMoveEvent, with_files)
    drop_zone.dragMoveEvent(moved)
    assert moved.isAccepted()
    empty = _drag(QDragEnterEvent, without)
    drop_zone.dragEnterEvent(empty)
    assert not empty.isAccepted()
    dropped = _drag(QDropEvent, mixed)
    drop_zone.dropEvent(dropped)
    assert dropped.isAccepted() and received == [[report_pdf, note]]
    refused = _drag(QDropEvent, foreign_only)
    drop_zone.dropEvent(refused)
    assert not refused.isAccepted() and len(received) == 1


def test_list_shows_the_work_states(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                                    patient: Patient) -> None:
    """A text-layer PDF, a folder with a transcript, an unknown extension and a scan
    arrive together; each entry and the status line name the state, and the states
    move on through analysis, a restored working state, export and an error."""
    page = _page(tmp_path, patient)
    reported: list[int] = []
    page.loaded.connect(lambda: reported.append(1))
    folder = tmp_path / "ordner"
    folder.mkdir()
    create_transcript(folder / "gespraech.txt", patient)
    (folder / "unterordner").mkdir()  # folders inside a folder are not descended into
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    page.add([report_pdf, folder, tmp_path / "egal.xyz", scan])
    assert page.listing.count() == 3 and len(page.session.documents) == 3
    texts = [page.listing.item(i).text() for i in range(3)]
    assert texts[0].startswith("befund.pdf  –  ") and "Seite(n) mit Textebene" in texts[0]
    assert texts[1] == "gespraech.txt  –  Textdatei"
    assert "ohne Textebene: unten aufbereiten" in texts[2]
    assert reported and not page.is_loading
    assert page.status.text() == "3 Dokument(e), 1 mit Seiten zum Aufbereiten"
    assert page.clean_up.tree.topLevelItemCount() == 1

    pdf, text, _ = page.session.documents
    page.session.analyse(pdf)
    page.session.analyse(text)
    page.refresh()
    assert "  –  analysiert, " in page.listing.item(0).text()
    assert "  –  Text, " in page.listing.item(1).text()
    assert page.listing.item(0).text().endswith(" Treffer")
    pdf.working_state = tmp_path / "befund.arbeitsstand.json"  # restored from a saved state
    page.refresh()
    assert "  –  Arbeitsstand, " in page.listing.item(0).text()
    pdf.working_state = None
    result = page.session.export(pdf)
    page.refresh()
    assert result.released and page.listing.item(0).text().endswith("  –  freigegeben")
    pdf.result = replace(result, target_pdf=None, target_txt=None)  # as after a failed re-check
    page.refresh()
    assert page.listing.item(0).text().endswith("  –  nicht freigegeben")
    pdf.error = "Testfehler"
    page.refresh()
    assert page.listing.item(0).text().endswith("  –  Fehler: Testfehler")
    page.session.close()


def test_remove_clears_session_and_cleanup(qapp: QApplication, tmp_path: Path,
                                           report_pdf: Path) -> None:
    """Removing needs a selection; the removed document leaves the session, and the
    clean-up panel below forgets its pages."""
    page = _page(tmp_path)
    scan = _scan(report_pdf, tmp_path / "scan.pdf")
    page.add([report_pdf, scan])
    assert page.clean_up.tree.topLevelItemCount() == 1
    page.remove()  # nothing selected: nothing happens
    assert page.listing.count() == 2
    page.listing.item(1).setSelected(True)
    page.remove()
    assert page.listing.count() == 1
    assert [d.path for d in page.session.documents] == [report_pdf]
    assert page.clean_up.tree.topLevelItemCount() == 0
    assert "nichts aufzubereiten" in page.clean_up.note.text()
    assert page.status.text() == "1 Dokument(e)"
    page.session.close()


def test_waiting_documents(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    """While a document waits for the worker, its entry and the status line say so;
    removing it takes it out of the queue, and the dialog of the file chooser feeds
    the same path as a drop."""
    monkeypatch.setattr(module, "Worker", _NeverStarted)
    monkeypatch.setattr(module, "QFileDialog", _Messages([str(report_pdf)]))
    session = Session(store_path=tmp_path / "s.sqlite")
    page = DocumentsPage(session)
    page.choose_files()
    assert page.listing.count() == 1 and page.is_loading
    assert page.listing.item(0).text() == "befund.pdf  –  wird geladen …"
    assert page.status.text() == "1 Dokument(e) – lade …"
    page.listing.item(0).setSelected(True)
    page.remove()
    assert page.listing.count() == 0 and session.documents == [] and not page.is_loading
    assert page.status.text() == "0 Dokument(e)"


def test_error_on_open_inline(qapp: QApplication, tmp_path: Path) -> None:
    """In the synchronous test mode a file that cannot be opened raises; the entry
    keeps the error and the queue is empty afterwards."""
    page = _page(tmp_path)
    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"kein PDF")
    with pytest.raises(Exception, match="(?i)kaputt|document|pdf|file"):
        page.add([broken])
    d = page.session.documents[0]
    assert d.error and d.opened is None and not page.is_loading
    page.refresh()
    assert page.listing.item(0).text().startswith("kaputt.pdf  –  Fehler: ")


def test_loading_in_the_background(qapp: QApplication, tmp_path: Path, report_pdf: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the synchronous mode the documents are opened in a worker thread one
    after the other; a failure is shown in a dialog (replaced here) and the queue
    moves on to the next file."""
    dialogs = _Messages()
    monkeypatch.setattr(module, "QMessageBox", dialogs)
    session = Session(store_path=tmp_path / "s.sqlite")
    page = DocumentsPage(session)
    broken = tmp_path / "kaputt.pdf"
    broken.write_bytes(b"kein PDF")
    done: list[int] = []
    page.loaded.connect(lambda: done.append(1))
    page.add([broken, report_pdf])
    assert page.is_loading and page.status.text().endswith(" – lade …")
    _wait(qapp, lambda: bool(done))
    _wait(qapp, lambda: not any(a.isRunning() for a in page.findChildren(Worker)))
    assert not page.is_loading
    assert len(dialogs.shown) == 1 and dialogs.shown[0][1] == "kaputt.pdf"
    first, second = session.documents
    assert first.error and first.opened is None
    assert second.opened is not None and second.opened.page_count == 2
    assert page.listing.item(0).text().startswith("kaputt.pdf  –  Fehler: ")
    assert "Seite(n) mit Textebene" in page.listing.item(1).text()
    session.close()


def test_paper_format_reloads_images(qapp: QApplication, tmp_path: Path,
                                     report_pdf: Path) -> None:
    """Changing the paper size re-opens the images that are already in (PDFs are left
    alone) and keeps the choice in the session options."""
    page = _page(tmp_path)
    photo = tmp_path / "foto.png"
    with pymupdf.open(report_pdf) as doc:
        doc[0].get_pixmap(dpi=60).save(photo)
    page.add([photo, report_pdf])
    image, pdf = page.session.documents
    assert image.opened is not None and pdf.opened is not None
    old_image, old_pdf = image.opened, pdf.opened
    a4_height = old_image.doc[0].rect.height
    page.paper.setCurrentIndex(page.paper.findData("a5"))
    assert page.session.options.paper_size == "a5"
    assert image.opened is not None and image.opened is not old_image
    assert pdf.opened is old_pdf
    assert image.opened.doc[0].rect.height < a4_height
    assert "1 Seite(n)" in page.listing.item(0).text()
    page.session.close()


def test_optdoc_messages_in_the_interface_language(
        qapp: QApplication, tmp_path: Path, report_pdf: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An error optdoc raises while a file is opened is kept and shown in the language of
    the interface: German, or English while the English translation is installed."""
    from pseudonymed.gui import translation
    from pseudonymed.pipeline import reading

    def broken(*_a: object, **_k: object) -> None:
        raise ValueError("Unsupported file type: .pdf")

    monkeypatch.setattr(reading, "open_file", broken)
    page = _page(tmp_path)
    with pytest.raises(ValueError):
        page.add([report_pdf])
    assert page.session.documents[0].error == "Dateityp nicht unterstützt: .pdf"
    translator = translation.install(qapp, "en_gb")
    assert translator is not None
    try:
        (tmp_path / "en").mkdir()
        english = _page(tmp_path / "en")
        with pytest.raises(ValueError):
            english.add([report_pdf])
        assert english.session.documents[0].error == "Unsupported file type: .pdf"
    finally:
        qapp.removeTranslator(translator)
