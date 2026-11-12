"""Run a callable in a Qt thread and hand back the result or the exception."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal


class Arbeiter(QThread):
    """A thread that runs one callable and reports back through signals: ``fertig``
    carries the return value, ``fehler`` the exception, each delivered on the thread of
    the page that connected to it, so the page may touch its widgets in the slot."""

    fertig = Signal(object)
    fehler = Signal(object)

    def __init__(self, aufgabe: Callable[[], Any], eltern: QObject | None = None) -> None:
        """Keep the callable; nothing runs before ``start()``."""
        super().__init__(eltern)
        self._aufgabe = aufgabe

    def run(self) -> None:  # noqa: D102
        try:
            self.fertig.emit(self._aufgabe())
        except Exception as e:  # shown to the user by the page
            self.fehler.emit(e)
