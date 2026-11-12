#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproducible code-quality snapshot for PseudonyMed, the Python member of the family battery
(qdaPy/script/quality_metrics.py, officeQDA/script/quality-metrics.mjs,
TimeAttest/script/quality-metrics.py).

Nagappan, Ball and Zeller (2006, doi:10.1145/1134285.1134349) found no metric set that fits all
projects and recommend calibrating against a project's OWN history.  So the numbers here are a
trend instrument plus a RATCHET: the keys in REGRESSION_KEYS must never rise above the committed
baseline; everything else is recorded, not judged.  Radjenovic et al. (2013,
doi:10.1016/j.infsof.2013.02.009) and Scalabrino et al. (2021, doi:10.1109/TSE.2019.2901468)
find that no static metric captures understandability reliably, hence no universal pass/fail
bars.  Read radon's MI with care: it is the SEI-normalised 0-100 variant (A above 19), not the
unnormalised formula whose "below 65" rule Coleman et al. (1994, doi:10.1109/2.303623) give.

WHAT IT MEASURES (the package ``pseudonymed``; each tool independent, a missing tool is
recorded, not fatal)
  * radon       cyclomatic complexity per block, functions over CC 10 and over CC 20, the grade
                histogram, module maintainability index
  * complexipy  cognitive complexity (nesting-sensitive), functions over 15
  * structure   functions longer than 60 statements, nesting deeper than 4, modules over 800
                lines, functions with more than 7 parameters: the monolith instrument
  * duplication copy-pasted blocks of 8 lines or more across the package (pylint's symilar,
                ignoring imports, docstrings and signatures): the DRY instrument
  * ruff        finding counts per rule under the project's own config (package, tests,
                packaging, werkzeuge, script)
  * mypy        error count under the project's own config (expected: zero)
  * docstrings  interrogate coverage, plus the number that matters: public modules, classes,
                functions and methods without a docstring (Qt overrides excepted)
  * vulture     unreferenced-code candidates after script/vulture_whitelist.py (framework
                hooks, fixtures, dataclass fields); every remaining hit needs a look
  * coverage    line coverage of the package from a fresh test run, total and per module

USAGE
  python script/quality_metrics.py                   # measure, summary, ratchet check
  python script/quality_metrics.py --json out.json   # also write the snapshot ("-": stdout)
  python script/quality_metrics.py --no-coverage     # skip the test run (minutes)
  python script/quality_metrics.py --update          # rewrite quality-baseline.json
Exit 1 on a ratchet regression; the fix is to improve the code or to consciously commit a new
baseline with --update, never to skip the run.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pseudonymed"
TESTS = ROOT / "tests"
BASELINE = ROOT / "quality-baseline.json"
RUFF_ZIELE = ("pseudonymed", "tests", "packaging", "werkzeuge", "script")

# The family's bands: CC 10 (McCabe's own guidance) and 20, cognitive 15 (the SonarQube
# convention), 60 statements and nesting 4 for the structure of a function, 800 lines for a
# module, 8 lines for a duplicate.  Trend lines and ratchet counts, not gates.
CC_WARN, CC_HIGH, COG_WARN = 10, 20, 15
LANGE_FUNKTION, TIEFE_SCHACHTELUNG, LANGES_MODUL, VIELE_PARAMETER = 60, 4, 800, 7
DUPLIKAT_ZEILEN = 8

# The ratchet: these must never rise above the committed baseline.
REGRESSION_KEYS: tuple[tuple[str, str], ...] = (
    ("ruff", "total_findings"),
    ("mypy", "errors"),
    ("radon", "functions_over_cc10"),
    ("radon", "functions_over_cc20"),
    ("complexipy", "functions_over_15"),
    ("structure", "long_functions"),
    ("structure", "deeply_nested"),
    ("structure", "long_modules"),
    ("duplication", "blocks"),
    ("docstrings", "public_missing"),
)
COVERAGE_SPIELRAUM = 0.5  # half a point of measurement noise

# Qt virtuals a widget overrides; their meaning is Qt's documentation, not ours.
_QT_OVERRIDES = re.compile(r"^(?:[a-z]+[A-Z]\w*|run|data|flags|headerData|rowCount|columnCount"
                           r"|setData)$")


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a command, returning (returncode, stdout+stderr).  Never raises: a missing tool
    is data about the environment, not a crash."""
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError as exc:
        return 127, str(exc)


def _py(*args: str) -> tuple[int, str]:
    return _run([sys.executable, "-m", *args])


def _werkzeug(name: str) -> str:
    """The tool next to the interpreter (the project's venv), else by name on the PATH."""
    kandidat = Path(sys.executable).parent / name
    return str(kandidat) if kandidat.exists() else name


def _module_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path | str) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


# -- complexity -----------------------------------------------------------------------------------
def radon_metrics() -> dict[str, Any]:
    """Cyclomatic complexity per block, plus the maintainability index per module."""
    rc, out = _py("radon", "cc", "-s", "-j", str(SRC))
    if rc != 0 or not out.strip():
        return {"error": "radon cc failed", "detail": out[:400]}
    data = json.loads(out)
    grades: dict[str, int] = {}
    worst: list[dict[str, Any]] = []
    total = n = 0
    for path, blocks in data.items():
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            grades[b["rank"]] = grades.get(b["rank"], 0) + 1
            total += b["complexity"]
            n += 1
            if b["complexity"] > CC_WARN:
                worst.append({"file": _rel(path), "name": b["name"], "line": b["lineno"],
                              "cc": b["complexity"]})
    worst.sort(key=lambda r: (-r["cc"], r["file"], r["name"]))
    mi: dict[str, float] = {}
    rc_mi, out_mi = _py("radon", "mi", "-j", str(SRC))
    if rc_mi == 0 and out_mi.strip():
        for path, rec in json.loads(out_mi).items():
            if isinstance(rec, dict) and "mi" in rec:
                mi[_rel(path)] = round(rec["mi"], 2)
    return {
        "blocks": n,
        "mean_cc": round(total / n, 2) if n else None,
        "grade_histogram": dict(sorted(grades.items())),
        "functions_over_cc10": len(worst),
        "functions_over_cc20": sum(1 for w in worst if w["cc"] > CC_HIGH),
        "over_cc10": worst,
        "mi_by_module": dict(sorted(mi.items(), key=lambda kv: kv[1])),
        "mi_min": min(mi.values()) if mi else None,
    }


def complexipy_metrics() -> dict[str, Any]:
    """Cognitive complexity: nesting-sensitive, unlike cyclomatic."""
    rc, out = _run([_werkzeug("complexipy"), str(SRC), "--output-csv", "-q"])
    csv_path = ROOT / "complexipy.csv"
    if not csv_path.exists():
        return {"error": "complexipy produced no CSV", "detail": out[:400], "returncode": rc}
    rows = []
    try:
        with csv_path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append((int(r["Cognitive Complexity"]), _rel(r["Path"]),
                             r["Function Name"]))
    finally:
        csv_path.unlink(missing_ok=True)
    rows.sort(reverse=True)
    return {
        "functions": len(rows),
        "max_cognitive": rows[0][0] if rows else None,
        "functions_over_15": sum(1 for c, _, _ in rows if c > COG_WARN),
        "over_15": [{"file": f, "name": n, "cognitive": c} for c, f, n in rows if c > COG_WARN],
    }


# -- structure ------------------------------------------------------------------------------------
def _statements(node: ast.AST) -> int:
    return sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt))


def _nesting(node: ast.AST, tiefe: int = 0) -> int:
    """Deepest nesting of blocks (if/for/while/with/try/match) inside a function."""
    bloecke = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
               ast.Match)
    tiefste = tiefe
    for kind in ast.iter_child_nodes(node):
        if isinstance(kind, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue  # a nested function starts its own count
        neu = tiefe + 1 if isinstance(kind, bloecke) else tiefe
        tiefste = max(tiefste, _nesting(kind, neu))
    return tiefste


def _funktionen(baum: ast.AST):
    for n in ast.walk(baum):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            yield n


def structure_metrics() -> dict[str, Any]:
    """The monolith instrument: long functions, deep nesting, long modules, wide signatures."""
    lang: list[dict[str, Any]] = []
    tief: list[dict[str, Any]] = []
    breit: list[dict[str, Any]] = []
    module: list[dict[str, Any]] = []
    funktionen = 0
    for p in _module_files():
        quelle = p.read_text(encoding="utf-8")
        zeilen = quelle.count("\n") + 1
        if zeilen > LANGES_MODUL:
            module.append({"file": _rel(p), "lines": zeilen})
        baum = ast.parse(quelle)
        for f in _funktionen(baum):
            funktionen += 1
            n = _statements(f) - 1
            if n > LANGE_FUNKTION:
                lang.append({"file": _rel(p), "name": f.name, "line": f.lineno, "statements": n})
            t = _nesting(f)
            if t > TIEFE_SCHACHTELUNG:
                tief.append({"file": _rel(p), "name": f.name, "line": f.lineno, "depth": t})
            args = f.args
            anzahl = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
            anzahl -= 1 if args.args and args.args[0].arg in ("self", "cls") else 0
            if anzahl > VIELE_PARAMETER:
                breit.append({"file": _rel(p), "name": f.name, "line": f.lineno,
                              "parameters": anzahl})
    lang.sort(key=lambda r: -r["statements"])
    tief.sort(key=lambda r: -r["depth"])
    breit.sort(key=lambda r: -r["parameters"])
    module.sort(key=lambda r: -r["lines"])
    return {
        "functions": funktionen,
        "long_functions": len(lang), "long": lang,
        "deeply_nested": len(tief), "nested": tief,
        "wide_signatures": len(breit), "wide": breit,
        "long_modules": len(module), "modules": module,
    }


# -- duplication ----------------------------------------------------------------------------------
def duplication_metrics() -> dict[str, Any]:
    """Copy-pasted blocks across the package (pylint's symilar)."""
    dateien = [str(p) for p in _module_files()]
    rc, out = _run([sys.executable, "-m", "pylint.checkers.symilar", "-d", str(DUPLIKAT_ZEILEN),
                    "--ignore-imports", "--ignore-docstrings", "--ignore-signatures", *dateien])
    if rc == 127 or "duplicates=" not in out:
        return {"error": "symilar failed", "detail": out[:400]}
    bloecke: list[dict[str, Any]] = []
    zeilen_gesamt = 0
    for m in re.finditer(r"(\d+) similar lines in (\d+) files\n((?:==.*\n)+)", out):
        n, dateien_n = int(m.group(1)), int(m.group(2))
        orte = [_rel(o.group(1)) + ":" + o.group(2)
                for o in re.finditer(r"==(\S+):\[(\d+):\d+\]", m.group(3))]
        bloecke.append({"lines": n, "files": dateien_n, "where": orte})
        zeilen_gesamt += n
    bloecke.sort(key=lambda b: -b["lines"])
    m = re.search(r"percent=([\d.]+)", out)
    return {"blocks": len(bloecke), "duplicated_lines": zeilen_gesamt,
            "percent": float(m.group(1)) if m else None, "detail": bloecke}


# -- lint, types, docs, dead code ------------------------------------------------------------------
def ruff_metrics() -> dict[str, Any]:
    """Findings under the project's own ruff config."""
    rc, out = _py("ruff", "check", *RUFF_ZIELE, "--statistics")
    if rc == 127:
        return {"error": out}
    counts: dict[str, int] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+([A-Z]+\d+)\s", line)
        if m:
            counts[m.group(2)] = int(m.group(1))
    return {"findings_by_rule": dict(sorted(counts.items())),
            "total_findings": sum(counts.values())}


def mypy_metrics() -> dict[str, Any]:
    """Type errors under the project's own config.  Expected: zero."""
    rc, out = _py("mypy")
    if rc == 127:
        return {"error": out}
    m = re.search(r"Found (\d+) error", out)
    fehler = [ln for ln in out.splitlines() if ": error:" in ln]
    return {"errors": int(m.group(1)) if m else 0, "clean": "Success" in out,
            "detail": fehler[:40]}


def _public_missing(p: Path) -> list[str]:
    """Public module, classes, functions and methods of ``p`` without a docstring."""
    quelle = p.read_text(encoding="utf-8")
    baum = ast.parse(quelle)
    fehlt: list[str] = []
    if p.name != "__init__.py" and ast.get_docstring(baum) is None:
        fehlt.append(f"{_rel(p)}:1 (module)")

    def pruefe(node: ast.AST, eltern: str) -> None:
        for kind in ast.iter_child_nodes(node):
            if not isinstance(kind, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            if kind.name.startswith("_") or _QT_OVERRIDES.match(kind.name):
                continue
            if any(isinstance(d, ast.Name) and d.id == "overload" for d in kind.decorator_list):
                continue
            if ast.get_docstring(kind) is None:
                fehlt.append(f"{_rel(p)}:{kind.lineno} {eltern}{kind.name}")
            if isinstance(kind, ast.ClassDef):
                pruefe(kind, kind.name + ".")

    pruefe(baum, "")
    return fehlt


def docstring_metrics() -> dict[str, Any]:
    """Coverage overall, and the number that matters: public definitions without one."""
    rc, out = _run([_werkzeug("interrogate"), str(SRC)])
    pct = None
    m = re.search(r"actual:\s*([\d.]+)%", out) or re.search(r"([\d.]+)%", out)
    if m:
        pct = float(m.group(1))
    missing = [eintrag for p in _module_files() for eintrag in _public_missing(p)]
    return {"coverage_pct": pct, "public_missing": len(missing), "public_missing_names": missing,
            "note": None if rc != 127 else out}


def vulture_metrics() -> dict[str, Any]:
    """Unreferenced-code candidates; candidates, not findings."""
    rc, out = _run([_werkzeug("vulture"), str(SRC), str(TESTS),
                    str(ROOT / "script" / "vulture_whitelist.py"), "--min-confidence", "60"])
    if rc == 127:
        return {"error": out}
    hits = [_rel(m.group(1)) + ":" + m.group(2)
            for m in (re.match(r"(\S+?):(\d+: unused .*)$", ln.strip()) for ln in out.splitlines())
            if m]  # warnings the interpreter prints while importing are not findings
    return {"candidates": len(hits), "detail": hits}


# -- coverage -------------------------------------------------------------------------------------
def coverage_metrics() -> dict[str, Any]:
    """Line coverage of the package from a fresh, offscreen test run."""
    # no -q here: pyproject adds one already, and a second one hides the test count
    rc, out = _run([sys.executable, "-m", "coverage", "run", "--source", str(SRC),
                    "-m", "pytest", "-p", "no:cacheprovider"])
    if rc == 127:
        return {"error": out}
    tests = re.search(r"(\d+) passed", out)
    rc_j, out_j = _py("coverage", "json", "-o", "-", "--quiet")
    if rc_j != 0 or not out_j.strip():
        return {"error": "coverage json failed", "detail": out_j[:400]}
    data = json.loads(out_j)
    per_module = {_rel(f): round(rec["summary"]["percent_covered"], 1)
                  for f, rec in sorted(data.get("files", {}).items())}
    (ROOT / ".coverage").unlink(missing_ok=True)
    return {
        "total_pct": round(data["totals"]["percent_covered"], 1),
        "tests_passed": int(tests.group(1)) if tests else None,
        "suite_green": rc == 0,
        "by_module_pct": dict(sorted(per_module.items(), key=lambda kv: kv[1])),
    }


# -- snapshot, summary, ratchet -------------------------------------------------------------------
def collect(mit_coverage: bool = True) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "radon": radon_metrics(),
        "complexipy": complexipy_metrics(),
        "structure": structure_metrics(),
        "duplication": duplication_metrics(),
        "ruff": ruff_metrics(),
        "mypy": mypy_metrics(),
        "docstrings": docstring_metrics(),
        "vulture": vulture_metrics(),
    }
    if mit_coverage:
        snap["coverage"] = coverage_metrics()
    return snap


def _summary(snap: dict[str, Any]) -> str:
    r, cx, st, du = snap["radon"], snap["complexipy"], snap["structure"], snap["duplication"]
    rf, my, ds, vu = snap["ruff"], snap["mypy"], snap["docstrings"], snap["vulture"]
    out = ["PseudonyMed quality snapshot", "-" * 60]
    if "mean_cc" in r:
        out.append(f"radon       mean CC {r['mean_cc']} over {r['blocks']} blocks"
                   f"  |  {r['functions_over_cc10']} > CC10, {r['functions_over_cc20']} > CC20"
                   f"  |  grades {r['grade_histogram']}")
        out.append(f"            MI min {r['mi_min']} (radon scale, A above 19)")
    if "max_cognitive" in cx:
        out.append(f"complexipy  max CogC {cx['max_cognitive']}  |  {cx['functions_over_15']} > 15")
    if "functions" in st:
        out.append(f"structure   {st['functions']} functions  |  {st['long_functions']} longer"
                   f" than {LANGE_FUNKTION} statements  |  {st['deeply_nested']} nested deeper"
                   f" than {TIEFE_SCHACHTELUNG}  |  {st['wide_signatures']} with more than"
                   f" {VIELE_PARAMETER} parameters  |  {st['long_modules']} modules over"
                   f" {LANGES_MODUL} lines")
    if "blocks" in du:
        out.append(f"duplication {du['blocks']} blocks of {DUPLIKAT_ZEILEN}+ similar lines"
                   f"  |  {du['duplicated_lines']} lines  |  {du['percent']}%")
    if "total_findings" in rf:
        out.append(f"ruff        {rf['total_findings']} findings {rf['findings_by_rule'] or ''}")
    if "errors" in my:
        out.append(f"mypy        {my['errors']} errors{'  (clean)' if my.get('clean') else ''}")
    if ds.get("coverage_pct") is not None:
        out.append(f"docstrings  {ds['coverage_pct']}% overall"
                   f"  |  {ds['public_missing']} public definitions undocumented")
    if "candidates" in vu:
        out.append(f"vulture     {vu['candidates']} candidates")
    cov = snap.get("coverage")
    if cov and "total_pct" in cov:
        out.append(f"coverage    {cov['total_pct']}%  |  {cov['tests_passed']} tests"
                   f"  |  suite {'green' if cov['suite_green'] else 'RED'}")
    return "\n".join(out)


def _compare(snap: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Regressions against a baseline, on the keys where higher is worse."""
    bad = []
    for section, key in REGRESSION_KEYS:
        now = snap.get(section, {}).get(key)
        was = baseline.get(section, {}).get(key)
        if isinstance(now, int) and isinstance(was, int) and now > was:
            bad.append(f"{section}.{key}: {was} -> {now}")
    cov_now = snap.get("coverage", {}).get("total_pct")
    cov_was = baseline.get("coverage", {}).get("total_pct")
    if (isinstance(cov_now, int | float) and isinstance(cov_was, int | float)
            and cov_now < cov_was - COVERAGE_SPIELRAUM):
        bad.append(f"coverage.total_pct: {cov_was} -> {cov_now}")
    if snap.get("coverage", {}).get("suite_green") is False:
        bad.append("the test suite is not green")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", metavar="PATH", help="write the snapshot ('-' for stdout only)")
    ap.add_argument("--baseline", metavar="PATH", default=str(BASELINE),
                    help="compare against this snapshot (default: quality-baseline.json)")
    ap.add_argument("--no-coverage", action="store_true", help="skip the test run")
    ap.add_argument("--update", action="store_true", help="rewrite the baseline from this run")
    args = ap.parse_args(argv)

    snap = collect(mit_coverage=not args.no_coverage)
    if args.json == "-":
        print(json.dumps(snap, indent=1, ensure_ascii=False))
    else:
        if args.json:
            Path(args.json).write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
            print(f"-> {args.json}")
        print(_summary(snap))
    if args.update:
        if args.no_coverage and BASELINE.exists():
            alt = json.loads(BASELINE.read_text(encoding="utf-8"))
            if "coverage" in alt:
                snap["coverage"] = alt["coverage"]  # keep the last measured coverage
        BASELINE.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"\nbaseline rewritten: {BASELINE.name}")
        return 0
    pfad = Path(args.baseline)
    if not pfad.exists():
        print(f"\nno baseline at {pfad.name}; run with --update to create one")
        return 0
    bad = _compare(snap, json.loads(pfad.read_text(encoding="utf-8")))
    if bad:
        print("\nRATCHET REGRESSION against " + pfad.name, file=sys.stderr)
        for line in bad:
            print("  " + line, file=sys.stderr)
        return 1
    print(f"\nno regression against {pfad.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
