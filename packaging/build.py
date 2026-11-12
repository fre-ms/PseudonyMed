"""Build the folder distribution with PyInstaller and smoke-test it.

Usage: uv run --group build python packaging/build.py [--ohne-test]

Produces dist/PseudonyMed/ (all platforms) and dist/PseudonyMed.app (macOS).  The
smoke test runs the packaged command line (konfig, vorlage, pseudonymisieren on a
synthetic report) and starts the packaged interface with --selbsttest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
DIST = WURZEL / "dist" / "PseudonyMed"


def lauf(befehl: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(b) for b in befehl), flush=True)
    return subprocess.run(befehl, check=True, text=True, **kw)


def exe(name: str) -> Path:
    return DIST / (name + (".exe" if sys.platform.startswith("win") else ""))


def bauen() -> None:
    for alt in (WURZEL / "build", WURZEL / "dist"):
        if alt.exists():
            shutil.rmtree(alt)
    lauf([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
          str(WURZEL / "packaging" / "pseudonymed.spec")], cwd=WURZEL)


def app_plist_richten() -> None:
    """The bundle must start the window; PyInstaller may point it at the console program."""
    if sys.platform != "darwin":
        return
    import plistlib

    plist = WURZEL / "dist" / "PseudonyMed.app" / "Contents" / "Info.plist"
    if not plist.exists():
        return
    daten = plistlib.loads(plist.read_bytes())
    geaendert = False
    if daten.get("CFBundleExecutable") != "pseudonymed-gui":
        daten["CFBundleExecutable"] = "pseudonymed-gui"
        geaendert = True
        print("Info.plist: CFBundleExecutable auf pseudonymed-gui gesetzt")
    # PyInstaller derives LSBackgroundOnly from the console program in the same folder;
    # a background-only app starts without a window, so the key must go
    if daten.pop("LSBackgroundOnly", None) is not None:
        geaendert = True
        print("Info.plist: LSBackgroundOnly entfernt")
    if geaendert:
        plist.write_bytes(plistlib.dumps(daten))
    app_signieren(plist.parent.parent)


def app_signieren(app: Path) -> None:
    """Ad-hoc signature for the whole bundle (no Apple account needed).

    Editing Info.plist breaks PyInstaller's seal, and macOS gives an unsigned or
    broken app no access to Desktop, Documents or Downloads: TCC cannot build a
    code requirement for it and refuses silently, without the usual prompt.  An
    ad-hoc signature restores a stable identity; Gatekeeper is a separate matter.
    """
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    print("App ad hoc signiert")


def rauchtest() -> None:
    umgebung = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    if sys.platform == "darwin":
        import plistlib

        plist = WURZEL / "dist" / "PseudonyMed.app" / "Contents" / "Info.plist"
        daten = plistlib.loads(plist.read_bytes())
        assert daten["CFBundleExecutable"] == "pseudonymed-gui"
        assert "LSBackgroundOnly" not in daten, "App wäre ein unsichtbarer Hintergrundprozess"
        assert daten.get("CFBundleIconFile"), "App ohne Icon (packaging/icon.py laufen lassen)"
        assert (plist.parent / "Resources" / daten["CFBundleIconFile"]).exists() or (
            plist.parent / "Resources" / (daten["CFBundleIconFile"] + ".icns")).exists()
        subprocess.run(["codesign", "--verify", "--deep", "--strict",
                        str(WURZEL / "dist" / "PseudonyMed.app")], check=True)
    with tempfile.TemporaryDirectory() as ordner:
        daten = Path(ordner)
        umgebung["PSEUDONYMED_DATENORDNER"] = str(daten)
        aus = lauf([str(exe("pseudonymed")), "konfig"], capture_output=True, env=umgebung)
        print(aus.stdout)
        assert "PseudonyMed" in aus.stdout and "OCR-Backends" in aus.stdout
        assert "Fachbegriffe:" in aus.stdout and "Medizin (Wikidata)" in aus.stdout
        import importlib.util

        if importlib.util.find_spec("de_core_news_md") is not None:
            assert "NER:                 spaCy" in aus.stdout, "NER model not shipped"
        aus = lauf([str(exe("pseudonymed")), "vorlage"], capture_output=True, env=umgebung)
        assert '"nachname"' in aus.stdout
        # a synthetic report through the packaged pipeline
        sys.path.insert(0, str(WURZEL))
        from tests.synthetisch import erzeuge_befund, synthetischer_patient

        p = synthetischer_patient(7)
        befund = erzeuge_befund(daten / "befund.pdf", p)
        (daten / "patient.json").write_text(
            __import__("json").dumps(p.als_dict(), ensure_ascii=False), encoding="utf-8")
        aus = lauf([str(exe("pseudonymed")), "pseudonymisieren", str(befund), "--patient",
                    str(daten / "patient.json"), "--speicher", str(daten / "s.sqlite")],
                   capture_output=True, env=umgebung)
        assert "Gegenprobe: bestanden" in aus.stdout, aus.stdout + aus.stderr
        assert (daten / "befund_pseudonym.pdf").exists()
        if importlib.util.find_spec("de_core_news_md") is not None:
            aus = lauf([str(exe("pseudonymed")), "pseudonymisieren", str(befund), "--patient",
                        str(daten / "patient.json"), "--speicher", str(daten / "s.sqlite"),
                        "--ner", "--ziel", str(daten / "ner")], capture_output=True, env=umgebung)
            assert "NER (" in aus.stdout and "Gegenprobe: bestanden" in aus.stdout, \
                aus.stdout + aus.stderr
        gui = exe("pseudonymed-gui")
        aus = lauf([str(gui), "--selbsttest"], capture_output=True, env=umgebung)
        assert "Selbsttest bestanden" in aus.stdout, aus.stdout + aus.stderr
        aus = lauf([str(gui), "--selbsttest", "--sprache", "en"], capture_output=True,
                   env=umgebung)
        assert "4 Documents" in aus.stdout, aus.stdout + aus.stderr  # translation shipped
    print("Rauchtest bestanden.")


if __name__ == "__main__":
    bauen()
    app_plist_richten()
    if "--ohne-test" not in sys.argv:
        rauchtest()
    groesse = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    print(f"dist/PseudonyMed: {groesse:.0f} MB")
