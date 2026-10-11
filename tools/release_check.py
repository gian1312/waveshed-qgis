#!/usr/bin/env python3
"""Pre-upload check: the full test suite, then the plugins.qgis.org scans.

One command for "is this tree fit to ship", used by the Waveshed pipeline GUI
(Build tab → QGIS plugin → *Run checks*) and runnable by hand:

    python3 tools/release_check.py                # run both stages
    python3 tools/release_check.py --install-tools  # one-time setup

Stages, each ending in PASS or FAIL — nothing is reported green that did not
run:

1. **Tests** — ``pytest tests`` with this interpreter.  FAIL on any failure or
   error **and on any skip**: a skipped test is a check that did not happen,
   and most skips here mean a missing prerequisite (GDAL, numpy, the
   converter binary) that would hide real defects.  Every skip is listed with
   its reason.
2. **Registry scans** — ``tools/qgis_scan.py`` on a freshly built ZIP
   (Bandit, detect-secrets, flake8, file analysis).  FAIL on any Critical.

The interpreter must provide GDAL (``osgeo``), numpy and pytest — on Windows
that is QGIS's own Python (``python-qgis.bat``), which is what the GUI uses.
The converter-backed tests need ``aether_converter``: ``--bin-dir``, then
``AETHER_BIN_DIR``, else ``~/.aether/bin`` (where the GUI's *Deploy engine* stages it)
is used when it holds one.

``--install-tools`` installs what is missing and nothing else: pytest into
this interpreter's user site (``pip install --user``) when it lacks it, and
the scan analysers into a venv in the gitignored ``tmp/scan-venv``.

Exit status: 0 all stages passed, 1 a stage failed, 2 prerequisites missing.

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess  # nosec B404 — runs pytest / pip / the scan, fixed argv
import sys
from typing import Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_VENV = os.path.join(REPO, "tmp", "scan-venv")
SCAN_TOOLS = ("bandit", "detect-secrets", "flake8")

_FINAL_RE = re.compile(r"\d+ (passed|failed|skipped|errors?)\b.* in [\d.]+s")
_SUMMARY_RE = re.compile(
    r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|warnings?)")


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def scan_tools_dir() -> str:
    return os.path.join(SCAN_VENV, "Scripts" if os.name == "nt" else "bin")


def _missing_modules() -> List[str]:
    return [label for module, label in (("osgeo", "GDAL (osgeo)"),
                                        ("numpy", "numpy"),
                                        ("pytest", "pytest"))
            if importlib.util.find_spec(module) is None]


def _missing_scan_tools() -> List[str]:
    tools = scan_tools_dir()
    return [name for name in SCAN_TOOLS
            if not os.path.isfile(os.path.join(tools, _exe(name)))]


def _stream(argv: List[str], env: Optional[Dict[str, str]] = None) -> Tuple[int, List[str]]:
    """Run *argv* in the repo, echoing every line; return (rc, lines)."""
    print("$ " + " ".join(argv), flush=True)
    proc = subprocess.Popen(  # nosec B603 — argv built here, no shell
        argv, cwd=REPO, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    lines: List[str] = []
    assert proc.stdout is not None  # nosec B101 — Popen with PIPE
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        print(line, flush=True)
    return proc.wait(), lines


def _converter_dir(bin_dir: Optional[str] = None) -> Optional[str]:
    """Where the converter-backed tests should find ``aether_converter``:
    ``--bin-dir``, then ``AETHER_BIN_DIR``, then ``~/.aether/bin``."""
    if bin_dir:
        return bin_dir
    configured = os.environ.get("AETHER_BIN_DIR")
    if configured:
        return configured
    staged = os.path.join(os.path.expanduser("~"), ".aether", "bin")
    if os.path.isfile(os.path.join(staged, _exe("aether_converter"))):
        return staged
    return None


def parse_pytest_summary(lines: List[str]) -> Dict[str, int]:
    """Counts from pytest's final summary line — ``3 failed, 9 passed in 2.1s``
    (``-q``) or the same framed in ``=`` (default verbosity)."""
    for line in reversed(lines):
        if _FINAL_RE.search(line) or "no tests ran" in line:
            counts: Dict[str, int] = {}
            for number, word in _SUMMARY_RE.findall(line):
                key = {"error": "errors", "warning": "warnings"}.get(word, word)
                counts[key] = counts.get(key, 0) + int(number)
            return counts
    return {}


def run_tests(bin_dir: Optional[str] = None) -> Tuple[bool, str]:
    env = dict(os.environ)
    converter = _converter_dir(bin_dir)
    if converter:
        env["AETHER_BIN_DIR"] = converter
        print(f"[check] AETHER_BIN_DIR={converter}")
    else:
        print("[check] no aether_converter found (AETHER_BIN_DIR unset, "
              "~/.aether/bin empty) — the converter tests will skip, which "
              "fails this stage. Stage the engine first (GUI: Deploy engine).")
    rc, lines = _stream([sys.executable, "-m", "pytest", "tests", "-q",
                         "-rfEs", "-p", "no:cacheprovider"], env)
    counts = parse_pytest_summary(lines)
    failed = counts.get("failed", 0) + counts.get("errors", 0)
    skipped = counts.get("skipped", 0)
    passed = counts.get("passed", 0)
    summary = f"{passed} passed, {failed} failed/errors, {skipped} skipped"
    if not counts:
        return False, f"no pytest summary found (exit {rc})"
    if failed or rc != 0:
        return False, f"{summary} (exit {rc})"
    if skipped:
        return False, summary + " — skips count as not run (listed above)"
    return passed > 0, summary


def run_scan() -> Tuple[bool, str]:
    rc, lines = _stream([sys.executable, os.path.join(REPO, "tools", "qgis_scan.py"),
                         "--tools", scan_tools_dir()])
    result = next((line for line in reversed(lines)
                   if line.startswith("RESULT:")), "no result line")
    return rc == 0, result.replace("RESULT: ", "")


def install_tools() -> int:
    rc = 0
    if importlib.util.find_spec("pytest") is None:
        rc |= _stream([sys.executable, "-m", "pip", "install", "--user",
                       "pytest"])[0]
    if _missing_scan_tools():
        if not os.path.isdir(SCAN_VENV):
            rc |= _stream([sys.executable, "-m", "venv", SCAN_VENV])[0]
        pip = os.path.join(scan_tools_dir(), _exe("pip"))
        rc |= _stream([pip, "install", *SCAN_TOOLS])[0]
    missing = _missing_modules()
    if missing:
        print(f"[check] still missing: {', '.join(missing)} — GDAL and numpy "
              "come with QGIS's Python (python-qgis.bat); they cannot be "
              "installed from here.")
        return 2
    print("[check] tools ready." if rc == 0 else "[check] install failed.")
    return 1 if rc else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--install-tools", action="store_true",
                        help="install pytest / the scan analysers, then exit")
    parser.add_argument("--bin-dir",
                        help="engine binaries for the converter-backed tests "
                             "(default: AETHER_BIN_DIR, else ~/.aether/bin)")
    args = parser.parse_args()
    print(f"[check] interpreter: {sys.executable}")
    if args.install_tools:
        return install_tools()

    missing = _missing_modules() + [f"{t} (scan venv)" for t in _missing_scan_tools()]
    if missing:
        print("[check] prerequisites missing: " + ", ".join(missing))
        print("[check] run with --install-tools (GUI: Install check tools); "
              "GDAL/numpy need QGIS's Python.")
        return 2

    results = [("Tests", *run_tests(args.bin_dir)), ("Registry scans", *run_scan())]
    print("\n==== Pre-upload check ====")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    ok = all(passed for _, passed, _ in results)
    print("  => " + ("READY TO UPLOAD (scans + tests)" if ok
                     else "NOT READY — fix the FAIL lines above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
