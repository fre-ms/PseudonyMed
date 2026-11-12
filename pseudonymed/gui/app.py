"""Entry point of the desktop interface: ``pseudonymed gui`` or ``pseudonymed-gui``.

Options: ``--sprache de|en`` (also ``--language``) and ``--selbsttest``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QTranslator
    from PySide6.QtWidgets import QApplication

    from pseudonymed.gui.hauptfenster import Hauptfenster


def main(argv: list[str] | None = None) -> int:
    """Start the desktop interface and return its exit code.  Without PySide6 a hint
    goes to stderr and the code is 1; with ``--selbsttest`` the window is built, rendered
    once and reported, then the program leaves with 0."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("Die Oberfläche braucht das Extra 'gui': uv sync --extra gui", file=sys.stderr)
        return 1
    from pseudonymed.gui import uebersetzung
    from pseudonymed.gui.hauptfenster import Hauptfenster
    from pseudonymed.gui.icon import app_icon

    argumente = list(argv if argv is not None else sys.argv[1:])
    selbsttest = "--selbsttest" in argumente
    if selbsttest:
        argumente.remove("--selbsttest")
    sprache = uebersetzung.sprache_bestimmen(argumente)
    argumente = uebersetzung.ohne_sprachargumente(argumente)
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([sys.argv[0], *argumente])
    app.setApplicationName("PseudonyMed")
    app.setWindowIcon(app_icon())
    app.setOrganizationName("PseudonyMed")
    uebersetzer = uebersetzung.installiere(app, sprache)  # the reference keeps it alive
    fenster = Hauptfenster()
    fenster.show()
    if selbsttest:
        return _selbsttest(app, fenster, uebersetzer)
    return app.exec()


def _selbsttest(app: QApplication, fenster: Hauptfenster,
                uebersetzer: QTranslator | None) -> int:
    """Packaged builds: everything is constructed, rendered once, the step names are
    printed, then the window closes and the translator is taken out again."""
    app.processEvents()
    tabs = [fenster.tabs.tabText(i) for i in range(fenster.tabs.count())]
    print("Selbsttest bestanden:", ", ".join(tabs))
    fenster.close()
    if uebersetzer is not None:
        app.removeTranslator(uebersetzer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
