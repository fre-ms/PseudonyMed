"""Entry point of the desktop interface: ``pseudonymed gui`` or ``pseudonymed-gui``.

Options: ``--sprache de|en`` (also ``--language``) and ``--selbsttest``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QTranslator
    from PySide6.QtWidgets import QApplication

    from pseudonymed.gui.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    """Start the desktop interface and return its exit code.  Without PySide6 a hint
    goes to stderr and the code is 1; with ``--selbsttest`` the window is built, rendered
    once and reported, then the program leaves with 0."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("Die Oberfläche braucht das Extra 'gui': uv sync --extra gui", file=sys.stderr)
        return 1
    from pseudonymed.gui import translation
    from pseudonymed.gui.icon import app_icon
    from pseudonymed.gui.main_window import MainWindow

    arguments = list(argv if argv is not None else sys.argv[1:])
    self_test = "--selbsttest" in arguments
    if self_test:
        arguments.remove("--selbsttest")
    language = translation.determine_language(arguments)
    arguments = translation.without_language_arguments(arguments)
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([sys.argv[0], *arguments])
    app.setApplicationName("PseudonyMed")
    app.setWindowIcon(app_icon())
    app.setOrganizationName("PseudonyMed")
    translator = translation.install(app, language)  # the reference keeps it alive
    window = MainWindow()
    window.show()
    if self_test:
        return _self_test(app, window, translator)
    return app.exec()


def _self_test(app: QApplication, window: MainWindow,
               translator: QTranslator | None) -> int:
    """Packaged builds: everything is constructed, rendered once, the step names are
    printed, then the window closes and the translator is taken out again."""
    app.processEvents()
    tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    print("Selbsttest bestanden:", ", ".join(tabs))
    window.close()
    if translator is not None:
        app.removeTranslator(translator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
