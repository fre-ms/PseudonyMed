"""Entry point of the desktop interface: ``pseudonymed gui`` or ``pseudonymed-gui``.

Options: ``--sprache de|en`` (also ``--language``) and ``--selbsttest``.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
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
    app = QApplication.instance() or QApplication([sys.argv[0], *argumente])
    app.setApplicationName("PseudonyMed")
    app.setWindowIcon(app_icon())
    app.setOrganizationName("PseudonyMed")
    app.uebersetzer = uebersetzung.installiere(app, sprache)  # keep a reference
    fenster = Hauptfenster()
    fenster.show()
    if selbsttest:
        # packaged builds: construct everything, render once, report, leave
        app.processEvents()
        tabs = [fenster.tabs.tabText(i) for i in range(fenster.tabs.count())]
        print("Selbsttest bestanden:", ", ".join(tabs))
        fenster.close()
        if app.uebersetzer is not None:
            app.removeTranslator(app.uebersetzer)
        return 0
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
