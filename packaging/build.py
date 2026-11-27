"""Build the folder distribution with PyInstaller and smoke-test it.

Usage: uv run --group build python packaging/build.py [--ohne-test]

Produces dist/PseudonyMed/ (all platforms) and dist/PseudonyMed.app (macOS).  The
smoke test runs the packaged command line (konfig, vorlage, pseudonymisieren on a
synthetic report, on a photo of it through docrepro's chain with OCR, and on the working
state it saved) and starts the packaged interface with --selbsttest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "PseudonyMed"


def run(command: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(b) for b in command), flush=True)
    return subprocess.run(command, check=True, text=True, **kw)


def exe(name: str) -> Path:
    return DIST / (name + (".exe" if sys.platform.startswith("win") else ""))


def build() -> None:
    for old in (ROOT / "build", ROOT / "dist"):
        if old.exists():
            shutil.rmtree(old)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(ROOT / "packaging" / "pseudonymed.spec")], cwd=ROOT)


def fix_app_plist() -> None:
    """The bundle must start the window; PyInstaller may point it at the console program."""
    if sys.platform != "darwin":
        return
    import plistlib

    plist = ROOT / "dist" / "PseudonyMed.app" / "Contents" / "Info.plist"
    if not plist.exists():
        return
    data = plistlib.loads(plist.read_bytes())
    changed = False
    if data.get("CFBundleExecutable") != "pseudonymed-gui":
        data["CFBundleExecutable"] = "pseudonymed-gui"
        changed = True
        print("Info.plist: CFBundleExecutable auf pseudonymed-gui gesetzt")
    # PyInstaller derives LSBackgroundOnly from the console program in the same folder;
    # a background-only app starts without a window, so the key must go
    if data.pop("LSBackgroundOnly", None) is not None:
        changed = True
        print("Info.plist: LSBackgroundOnly entfernt")
    if changed:
        plist.write_bytes(plistlib.dumps(data))
    sign_app(plist.parent.parent)


def sign_app(app: Path) -> None:
    """Ad-hoc signature for the whole bundle (no Apple account needed).

    Editing Info.plist breaks PyInstaller's seal, and macOS gives an unsigned or
    broken app no access to Desktop, Documents or Downloads: TCC cannot build a
    code requirement for it and refuses silently, without the usual prompt.  An
    ad-hoc signature restores a stable identity; Gatekeeper is a separate matter.
    """
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    print("App ad hoc signiert")


def smoke_test() -> None:
    # German, whatever the build machine's locale: the checks below read German output
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PSEUDONYMED_SPRACHE": "de"}
    if sys.platform == "darwin":
        import plistlib

        plist = ROOT / "dist" / "PseudonyMed.app" / "Contents" / "Info.plist"
        data = plistlib.loads(plist.read_bytes())
        assert data["CFBundleExecutable"] == "pseudonymed-gui"
        assert "LSBackgroundOnly" not in data, "App wäre ein unsichtbarer Hintergrundprozess"
        assert data.get("CFBundleIconFile"), "App ohne Icon (packaging/icon.py laufen lassen)"
        assert (plist.parent / "Resources" / data["CFBundleIconFile"]).exists() or (
            plist.parent / "Resources" / (data["CFBundleIconFile"] + ".icns")).exists()
        subprocess.run(["codesign", "--verify", "--deep", "--strict",
                        str(ROOT / "dist" / "PseudonyMed.app")], check=True)
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder)
        environment["PSEUDONYMED_DATENORDNER"] = str(data)
        out = run([str(exe("pseudonymed")), "konfig"], capture_output=True, env=environment)
        print(out.stdout)
        assert "PseudonyMed" in out.stdout and "OCR-Backends" in out.stdout
        assert "Fachbegriffe:" in out.stdout and "Medizin (Wikidata)" in out.stdout
        # docrepro's steps, typdoc found through its entry point, and an OCR engine
        assert "docrepro 0.11.0 (typdoc " in out.stdout and "optdoc " in out.stdout
        assert "rapidocr" in out.stdout, "RapidOCR not shipped"
        import importlib.util

        if importlib.util.find_spec("de_core_news_md") is not None:
            assert "NER:                 spaCy" in out.stdout, "NER model not shipped"
        out = run([str(exe("pseudonymed")), "vorlage"], capture_output=True, env=environment)
        assert '"nachname"' in out.stdout
        # a synthetic report through the packaged pipeline
        sys.path.insert(0, str(ROOT))
        from tests.synthetic import create_report_pdf, synthetic_patient

        p = synthetic_patient(7)
        report_pdf = create_report_pdf(data / "befund.pdf", p)
        (data / "patient.json").write_text(
            __import__("json").dumps(p.as_dict(), ensure_ascii=False), encoding="utf-8")
        out = run([str(exe("pseudonymed")), "pseudonymisieren", str(report_pdf), "--patient",
                   str(data / "patient.json"), "--speicher", str(data / "s.sqlite")],
                  capture_output=True, env=environment)
        assert "Gegenprobe: bestanden" in out.stdout, out.stdout + out.stderr
        assert (data / "befund_pseudonym.pdf").exists()
        photo_through_the_chain(data, environment)
        if importlib.util.find_spec("de_core_news_md") is not None:
            out = run([str(exe("pseudonymed")), "pseudonymisieren", str(report_pdf), "--patient",
                       str(data / "patient.json"), "--speicher", str(data / "s.sqlite"),
                       "--ner", "--ziel", str(data / "ner")], capture_output=True, env=environment)
            assert "NER (" in out.stdout and "Gegenprobe: bestanden" in out.stdout, \
                out.stdout + out.stderr
        gui = exe("pseudonymed-gui")
        out = run([str(gui), "--selbsttest"], capture_output=True, env=environment)
        assert "Selbsttest bestanden" in out.stdout, out.stdout + out.stderr
        out = run([str(gui), "--selbsttest", "--sprache", "en"], capture_output=True,
                  env=environment)
        assert "4 Documents" in out.stdout, out.stdout + out.stderr  # translation shipped
    print("Rauchtest bestanden.")


def photo_through_the_chain(data: Path, environment: dict[str, str]) -> None:
    """A photo of the synthetic report through the packaged pipeline: typdoc binds it,
    optdoc prepares it, ocrdoc reads it by OCR; its working state (the original and
    docrepro's chain document) is finished again without reading anything again."""
    import pymupdf

    with pymupdf.open(data / "befund.pdf") as doc:
        doc[0].get_pixmap(dpi=150).save(data / "foto.png")
    out = run([str(exe("pseudonymed")), "pseudonymisieren", str(data / "foto.png"), "--patient",
               str(data / "patient.json"), "--speicher", str(data / "s.sqlite"),
               "--arbeitsstand", "--ziel", str(data / "foto")],
              capture_output=True, env=environment)
    assert "Gegenprobe: bestanden" in out.stdout and "per OCR gelesen" in out.stdout, \
        out.stdout + out.stderr
    state = data / "foto" / "foto.arbeitsstand.json"
    assert state.exists() and (data / "foto" / "foto.arbeitsstand.png").exists()
    out = run([str(exe("pseudonymed")), "pseudonymisieren", str(state), "--speicher",
               str(data / "s.sqlite"), "--ziel", str(data / "wieder")],
              capture_output=True, env=environment)
    assert "Gegenprobe: bestanden" in out.stdout, out.stdout + out.stderr
    first = (data / "foto" / "foto_pseudonym.txt").read_text(encoding="utf-8")
    again = (data / "wieder" / "foto_pseudonym.txt").read_text(encoding="utf-8")
    assert first == again, "the working state gave another text"


if __name__ == "__main__":
    build()
    fix_app_plist()
    if "--ohne-test" not in sys.argv:
        smoke_test()
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    print(f"dist/PseudonyMed: {size:.0f} MB")
