"""Run a callable in a Qt thread and hand back the result or the exception."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal


class Worker(QThread):
    """A thread that runs one callable and reports back through signals: ``done``
    carries the return value, ``error`` the exception, each delivered on the thread of
    the page that connected to it, so the page may touch its widgets in the slot."""

    done = Signal(object)
    error = Signal(object)

    def __init__(self, task: Callable[[], Any], parent: QObject | None = None) -> None:
        """Keep the callable; nothing runs before ``start()``."""
        super().__init__(parent)
        self._task = task

    def run(self) -> None:  # noqa: D102
        try:
            self.done.emit(self._task())
        except Exception as e:  # shown to the user by the page
            self.error.emit(e)
