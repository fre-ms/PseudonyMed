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

from pseudonymed.gui.worker import Worker  # noqa: E402


@pytest.fixture
def app() -> QCoreApplication:
    """The Qt application whose event loop delivers the queued signals.  A full offscreen
    QApplication, because a bare QCoreApplication created first would stop every later
    widget test of the session from constructing one."""
    return QApplication.instance() or QApplication([])


def _run(app: QCoreApplication, worker: Worker) -> None:
    """Start the worker, wait for its thread to end and deliver the queued signals."""
    worker.start()
    assert worker.wait(30_000), "worker thread did not finish"
    app.processEvents()


def test_result_arrives_as_signal(app: QCoreApplication) -> None:
    results: list[Any] = []
    error: list[BaseException] = []
    threads: list[int] = []
    payload = {"seiten": 3, "treffer": 12}

    def task() -> dict[str, int]:
        threads.append(threading.get_ident())
        return payload

    worker = Worker(task)
    worker.done.connect(results.append)
    worker.error.connect(error.append)
    _run(app, worker)
    assert results == [payload] and results[0] is payload  # the very object
    assert not error
    assert threads and threads[0] != threading.get_ident()  # ran off the main thread
    assert worker.isFinished() and not worker.isRunning()


def test_exception_arrives_as_signal(app: QCoreApplication) -> None:
    results: list[Any] = []
    error: list[BaseException] = []

    def task() -> None:
        raise ValueError("Datei nicht lesbar")

    worker = Worker(task)
    worker.done.connect(results.append)
    worker.error.connect(error.append)
    _run(app, worker)
    assert not results
    assert len(error) == 1 and isinstance(error[0], ValueError)
    assert str(error[0]) == "Datei nicht lesbar"


@pytest.mark.usefixtures("app")
def test_run_synchronous_in_the_calling_thread() -> None:
    """``run()`` is an ordinary method: called directly it does the work in the calling
    thread and both signals arrive at once, without an event loop.  This is also the only
    path the coverage tracer sees, because it is never installed in a thread Qt starts."""
    results: list[Any] = []
    error: list[BaseException] = []

    def raises() -> None:
        raise RuntimeError("abgebrochen vom Test")

    worker = Worker(lambda: 7)
    worker.done.connect(results.append)
    worker.error.connect(error.append)
    worker.run()
    broken = Worker(raises)
    broken.done.connect(results.append)
    broken.error.connect(error.append)
    broken.run()
    assert results == [7]
    assert [str(e) for e in error] == ["abgebrochen vom Test"]


def test_parent_and_second_run(app: QCoreApplication) -> None:
    """The worker hangs under the page that owns it, and a finished thread may be started
    again for the next document."""
    parent = QObject()
    counter = itertools.count(1)
    worker = Worker(lambda: next(counter), parent)
    assert worker.parent() is parent
    results: list[int] = []
    worker.done.connect(results.append)
    _run(app, worker)
    _run(app, worker)
    assert results == [1, 2]


def test_cancel_via_interruption_request(app: QCoreApplication) -> None:
    """The class has no cancel of its own; what it supports is QThread's cooperative
    interruption request, which a task may read and answer by returning early."""
    started = threading.Event()
    results: list[str] = []

    def task() -> str:
        started.set()
        thread = QThread.currentThread()
        while not thread.isInterruptionRequested():
            QThread.msleep(5)
        return "abgebrochen"

    worker = Worker(task)
    worker.done.connect(results.append)
    worker.start()
    assert started.wait(10)
    worker.requestInterruption()
    assert worker.wait(30_000), "the task did not notice the interruption"
    app.processEvents()
    assert results == ["abgebrochen"]
