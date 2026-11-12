# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification: one folder with the desktop app and the command line.

Build with ``uv run --group build pyinstaller packaging/pseudonymed.spec`` or via
``packaging/build.py``, which also runs the smoke tests.  The OCR models are
part of the rapidocr wheel and travel as data; nothing is downloaded at run time.
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

block_cipher = None
wurzel = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], []
for paket in ("rapidocr", "onnxruntime", "pymupdf"):
    d, b, h = collect_all(paket)
    datas += d
    binaries += b
    hiddenimports += h
datas += [(str(wurzel / "pseudonymed" / "gui" / "uebersetzungen"), "pseudonymed/gui/uebersetzungen")]
datas += [(str(wurzel / "pseudonymed" / "gui" / "icon"), "pseudonymed/gui/icon")]  # window icon
ICON = {"darwin": wurzel / "packaging" / "icon" / "pseudonymed.icns",
        "win32": wurzel / "packaging" / "icon" / "pseudonymed.ico"}.get(sys.platform)
ICON = str(ICON) if ICON is not None and ICON.exists() else None
datas += [(str(wurzel / "pseudonymed" / "daten"), "pseudonymed/daten")]  # medical vocabulary
# NER travels only when the build environment has spaCy and a German model (extra "ner").
for paket in ("spacy", "thinc", "de_core_news_lg", "de_core_news_md", "de_core_news_sm",
              "pysequoia", "keyring"):
    if importlib.util.find_spec(paket) is not None:
        d, b, h = collect_all(paket)
        datas += d
        binaries += b
        hiddenimports += h
binaries += collect_dynamic_libs("pillow_heif")
binaries += collect_dynamic_libs("zxingcpp")
hiddenimports += collect_submodules("pseudonymed")
hiddenimports += ["pillow_heif", "zxingcpp", "PIL.Image", "PIL.ImageFilter", "PIL.ImageOps",
                  "shapely", "cv2", "PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"]

# Qt modules the application does not use; leaving them out saves hundreds of megabytes.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtXml", "PySide6.QtSvg",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtNetworkAuth", "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer", "PySide6.QtSpatialAudio", "PySide6.QtGraphs", "PySide6.QtStateMachine",
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "torch", "paddle",
    "openvino",
]

gui_a = Analysis(
    [str(wurzel / "packaging" / "start_gui.py")],
    pathex=[str(wurzel)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)
cli_a = Analysis(
    [str(wurzel / "packaging" / "start_cli.py")],
    pathex=[str(wurzel)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)
MERGE((gui_a, "pseudonymed-gui", "pseudonymed-gui"), (cli_a, "pseudonymed", "pseudonymed"))

gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)
gui_exe = EXE(
    gui_pyz, gui_a.scripts, [],
    exclude_binaries=True, name="pseudonymed-gui", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin", target_arch=None, codesign_identity=None,
    entitlements_file=None, icon=ICON,
)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data, cipher=block_cipher)
cli_exe = EXE(
    cli_pyz, cli_a.scripts, [],
    exclude_binaries=True, name="pseudonymed", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    gui_exe, gui_a.binaries, gui_a.zipfiles, gui_a.datas,
    cli_exe, cli_a.binaries, cli_a.zipfiles, cli_a.datas,
    strip=False, upx=False, upx_exclude=[], name="PseudonyMed",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PseudonyMed.app",
        icon=ICON,
        bundle_identifier="de.pseudonymed.app",
        info_plist={
            "CFBundleDisplayName": "PseudonyMed",
            "CFBundleName": "PseudonyMed",
            "CFBundleExecutable": "pseudonymed-gui",  # the window, not the console program
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            # shown in the macOS privacy prompt when a file in a protected folder is opened
            "NSDesktopFolderUsageDescription": "PseudonyMed liest die Dokumente, die Sie zum Pseudonymisieren auswählen.",
            "NSDocumentsFolderUsageDescription": "PseudonyMed liest die Dokumente, die Sie zum Pseudonymisieren auswählen.",
            "NSDownloadsFolderUsageDescription": "PseudonyMed liest die Dokumente, die Sie zum Pseudonymisieren auswählen.",
            "NSRemovableVolumesUsageDescription": "PseudonyMed liest Dokumente und schreibt Ergebnisse auf externe Datenträger, wenn Sie sie dort ablegen.",
            "NSNetworkVolumesUsageDescription": "PseudonyMed liest Dokumente und schreibt Ergebnisse auf Netzlaufwerken, wenn Sie sie dort ablegen.",
        },
    )
