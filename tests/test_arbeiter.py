"""The worker thread of the desktop: the callable runs off the main thread, its result
or its exception arrives as a signal on the thread that connected.  Synthetic callables
only; no document is involved."""

from __future__ import annotations

import itertools
import os
import threading
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pseudonymed.gui.arbeiter import Arbeiter  # noqa: E402


@pytest.fixture
def app() -> QCoreApplication:
    """The Qt application whose event loop delivers the queued signals.  A full offscreen
    QApplication, because a bare QCoreApplication created first would stop every later
    widget test of the session from constructing one."""
    return QApplication.instance() or QApplication([])


def _lauf(app: QCoreApplication, arbeiter: Arbeiter) -> None:
    """Start the worker, wait for its thread to end and deliver the queued signals."""
    arbeiter.start()
    assert arbeiter.wait(30_000), "worker thread did not finish"
    app.processEvents()


def test_ergebnis_kommt_als_signal(app: QCoreApplication) -> None:
    ergebnisse: list[Any] = []
    fehler: list[BaseException] = []
    threads: list[int] = []
    nutzlast = {"seiten": 3, "treffer": 12}

    def aufgabe() -> dict[str, int]:
        threads.append(threading.get_ident())
        return nutzlast

    arbeiter = Arbeiter(aufgabe)
    arbeiter.fertig.connect(ergebnisse.append)
    arbeiter.fehler.connect(fehler.append)
    _lauf(app, arbeiter)
    assert ergebnisse == [nutzlast] and ergebnisse[0] is nutzlast  # the very object
    assert not fehler
    assert threads and threads[0] != threading.get_ident()  # ran off the main thread
    assert arbeiter.isFinished() and not arbeiter.isRunning()


def test_ausnahme_kommt_als_signal(app: QCoreApplication) -> None:
    ergebnisse: list[Any] = []
    fehler: list[BaseException] = []

    def aufgabe() -> None:
        raise ValueError("Datei nicht lesbar")

    arbeiter = Arbeiter(aufgabe)
    arbeiter.fertig.connect(ergebnisse.append)
    arbeiter.fehler.connect(fehler.append)
    _lauf(app, arbeiter)
    assert not ergebnisse
    assert len(fehler) == 1 and isinstance(fehler[0], ValueError)
    assert str(fehler[0]) == "Datei nicht lesbar"


@pytest.mark.usefixtures("app")
def test_run_synchron_im_aufrufenden_thread() -> None:
    """``run()`` is an ordinary method: called directly it does the work in the calling
    thread and both signals arrive at once, without an event loop.  This is also the only
    path the coverage tracer sees, because it is never installed in a thread Qt starts."""
    ergebnisse: list[Any] = []
    fehler: list[BaseException] = []

    def wirft() -> None:
        raise RuntimeError("abgebrochen vom Test")

    arbeiter = Arbeiter(lambda: 7)
    arbeiter.fertig.connect(ergebnisse.append)
    arbeiter.fehler.connect(fehler.append)
    arbeiter.run()
    kaputt = Arbeiter(wirft)
    kaputt.fertig.connect(ergebnisse.append)
    kaputt.fehler.connect(fehler.append)
    kaputt.run()
    assert ergebnisse == [7]
    assert [str(e) for e in fehler] == ["abgebrochen vom Test"]


def test_eltern_und_zweiter_lauf(app: QCoreApplication) -> None:
    """The worker hangs under the page that owns it, and a finished thread may be started
    again for the next document."""
    eltern = QObject()
    zaehler = itertools.count(1)
    arbeiter = Arbeiter(lambda: next(zaehler), eltern)
    assert arbeiter.parent() is eltern
    ergebnisse: list[int] = []
    arbeiter.fertig.connect(ergebnisse.append)
    _lauf(app, arbeiter)
    _lauf(app, arbeiter)
    assert ergebnisse == [1, 2]


def test_abbruch_ueber_unterbrechungsanfrage(app: QCoreApplication) -> None:
    """The class has no cancel of its own; what it supports is QThread's cooperative
    interruption request, which a task may read and answer by returning early."""
    begonnen = threading.Event()
    ergebnisse: list[str] = []

    def aufgabe() -> str:
        begonnen.set()
        thread = QThread.currentThread()
        while not thread.isInterruptionRequested():
            QThread.msleep(5)
        return "abgebrochen"

    arbeiter = Arbeiter(aufgabe)
    arbeiter.fertig.connect(ergebnisse.append)
    arbeiter.start()
    assert begonnen.wait(10)
    arbeiter.requestInterruption()
    assert arbeiter.wait(30_000), "the task did not notice the interruption"
    app.processEvents()
    assert ergebnisse == ["abgebrochen"]
