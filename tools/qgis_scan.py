#!/usr/bin/env python3
"""Run the plugins.qgis.org upload scans locally, on the ZIP that ships.

plugins.qgis.org scans every uploaded plugin with four analysers — "Bandit
security", "Detect Secrets", "Flake8 quality" and "File analysis" — and
grades each finding Critical / Warning / Info.  This mirrors them on the
package ``package.py`` builds, so a release is checked before upload rather
than by the registry.

What runs:

* **Bandit** over the extracted package (every rule; graded by the registry's
  table below, not Bandit's own severity).
* **detect-secrets** ``scan --all-files`` over the extracted package.
* **flake8** with exactly the registry's codes selected and
  ``--max-complexity 10`` (C901).
* **File analysis** in Python, on the ZIP entries themselves: suspicious
  extensions, binary content, executable bits and hidden names.

Exit status is 1 when any *Critical* finding exists, else 0.  Warnings and
Info are listed (``--verbose`` prints every hit) but do not fail the run —
the registry shows them to reviewers; it does not reject the upload for them.

The three analysers are dev tools, never plugin dependencies.  Install them
into a scratch venv inside the gitignored ``tmp/`` folder::

    python3 -m venv tmp/scan-venv
    tmp/scan-venv/bin/pip install bandit detect-secrets flake8

Usage::

    python3 tools/qgis_scan.py [--zip dist/waveshed.X.Y.Z.zip] [--tools DIR]
                               [--verbose]

Without ``--zip`` the package is built first (``package.py`` into
``tmp/qgis-scan``).  ``--tools`` is the directory holding the analyser
executables (default ``tmp/scan-venv/bin``, ``Scripts`` on Windows; else
``PATH``).

Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import stat
import subprocess  # nosec B404 — runs the local analysers, fixed argv
import sys
import tempfile
import zipfile
from typing import Dict, List, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# The registry's grading (copied from its rule table, 2026-10).
# --------------------------------------------------------------------------

BANDIT_CRITICAL = {
    "B102", "B103", "B105", "B106", "B107", "B111", "B201", "B202", "B301",
    "B302", "B304", "B305", "B306", "B307", "B312", "B321", "B323", "B401",
    "B402", "B412", "B413", "B501", "B502", "B503", "B505", "B506", "B507",
    "B601", "B602", "B604", "B605", "B609", "B610", "B611", "B612", "B613",
    "B615", "B701",
}
BANDIT_INFO = {"B109", "B404", "B410", "B411"}
# Everything else Bandit reports is a Warning in the registry's table.

SECRETS_CRITICAL = {
    "AWS Access Key", "Artifactory Credentials", "Azure Storage Account access key",
    "Basic Auth Credentials", "Cloudant Credentials", "Discord Bot Token",
    "GitHub Token", "GitLab Token", "IBM Cloud IAM Key", "IBM COS HMAC Credentials",
    "JSON Web Token", "Mailchimp Access Key", "NPM tokens", "OpenAI Token",
    "Private Key", "PyPI Token", "SendGrid API Key", "Slack Token",
    "SoftLayer Credentials", "Square OAuth Secret", "Stripe Access Key",
    "Telegram Bot Token", "Twilio API Key",
}
SECRETS_INFO = {"Public IP (ipv4)"}
# High-entropy strings and secret keywords are Warnings.

FLAKE8_CRITICAL = ["E901", "E902", "E999", "F821", "F823", "F831"]
FLAKE8_WARNING = [
    "C901", "E101", "E711", "E712", "E713", "E714", "E721", "E722", "E731",
    "E741", "E742", "E743", "F402", "F403", "F404", "F405", "F811", "F822",
    "F901", "W605",
]
FLAKE8_INFO = [
    "E111", "E112", "E113", "E114", "E115", "E116", "E117", "E121", "E122",
    "E123", "E124", "E125", "E126", "E127", "E128", "E129", "E131", "E133",
    "E201", "E202", "E203", "E211", "E221", "E222", "E223", "E224", "E225",
    "E226", "E227", "E228", "E231", "E241", "E242", "E251", "E261", "E262",
    "E265", "E266", "E271", "E272", "E273", "E274", "E275", "E301", "E302",
    "E303", "E304", "E305", "E306", "E401", "E402", "E501", "E502", "E701",
    "E702", "E703", "E704", "F401", "F841", "W191", "W291", "W292", "W293",
    "W391", "W503", "W504", "W601", "W602", "W603", "W604",
]

SUSPICIOUS_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".ps1", ".com",
    ".msi", ".scr", ".pyd", ".jar", ".bin",
}
#: Formats a reviewer can still inspect; flagged as binary but expected.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".qm"}

CRITICAL, WARNING, INFO = "Critical", "Warning", "Info"

Finding = Tuple[str, str, str, str]  # (severity, scanner, code, where)


def _tool(tools_dir: str, name: str) -> str:
    for candidate in (os.path.join(tools_dir, name),
                      os.path.join(tools_dir, name + ".exe")):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which(name)
    if not found:
        sys.exit(f"{name} not found in {tools_dir} or PATH — see the module "
                 "docstring for the venv setup.")
    return found


def _run(argv: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)  # nosec B603


def build_zip(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    result = _run([sys.executable, os.path.join(REPO, "package.py"), out_dir])
    if result.returncode != 0:
        sys.exit("package.py failed:\n" + result.stdout + result.stderr)
    zips = sorted(f for f in os.listdir(out_dir) if f.endswith(".zip"))
    if not zips:
        sys.exit(f"package.py produced no ZIP in {out_dir}")
    return os.path.join(out_dir, zips[-1])


def file_analysis(zip_path: str) -> List[Finding]:
    findings: List[Finding] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            base = os.path.basename(name)
            ext = os.path.splitext(base)[1].lower()
            if ext in SUSPICIOUS_EXTS:
                findings.append((CRITICAL, "files", "FILE_SUSPICIOUS", name))
            mode = info.external_attr >> 16
            if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                findings.append((WARNING, "files", "FILE_EXECUTABLE", name))
            if any(part.startswith(".") for part in name.split("/")):
                findings.append((INFO, "files", "FILE_HIDDEN", name))
            data = archive.read(info)[:8192]
            if b"\0" in data:
                findings.append((WARNING, "files", "FILE_BINARY",
                                 name + (" (image)" if ext in _IMAGE_EXTS
                                         else "")))
    return findings


def bandit(tools_dir: str, root: str) -> List[Finding]:
    result = _run([_tool(tools_dir, "bandit"), "-r", root, "-f", "json", "-q"])
    report = json.loads(result.stdout or "{}")
    findings: List[Finding] = []
    for hit in report.get("results", []):
        code = hit["test_id"]
        severity = (CRITICAL if code in BANDIT_CRITICAL
                    else INFO if code in BANDIT_INFO else WARNING)
        where = f"{os.path.relpath(hit['filename'], root)}:{hit['line_number']}"
        findings.append((severity, "bandit", code, where))
    for error in report.get("errors", []):
        findings.append((CRITICAL, "bandit", "PARSE", error.get("filename", "?")))
    return findings


def detect_secrets(tools_dir: str, root: str) -> List[Finding]:
    result = _run([_tool(tools_dir, "detect-secrets"), "scan", "--all-files",
                   root])
    report = json.loads(result.stdout or "{}")
    findings: List[Finding] = []
    for filename, hits in report.get("results", {}).items():
        for hit in hits:
            kind = hit["type"]
            severity = (CRITICAL if kind in SECRETS_CRITICAL
                        else INFO if kind in SECRETS_INFO else WARNING)
            where = f"{os.path.relpath(filename, root)}:{hit['line_number']}"
            findings.append((severity, "detect-secrets", kind, where))
    return findings


def flake8(tools_dir: str, root: str) -> List[Finding]:
    codes = FLAKE8_CRITICAL + FLAKE8_WARNING + FLAKE8_INFO
    result = _run([
        _tool(tools_dir, "flake8"), "--isolated", "--max-complexity", "10",
        "--select", ",".join(codes), "--format",
        "%(path)s:%(row)d\t%(code)s", root,
    ])
    findings: List[Finding] = []
    for line in result.stdout.splitlines():
        where, _, code = line.partition("\t")
        severity = (CRITICAL if code in FLAKE8_CRITICAL
                    else WARNING if code in FLAKE8_WARNING else INFO)
        findings.append((severity, "flake8", code, os.path.relpath(where, root)))
    return findings


def report(findings: List[Finding], verbose: bool) -> int:
    by_severity: Dict[str, List[Finding]] = collections.defaultdict(list)
    for finding in findings:
        by_severity[finding[0]].append(finding)
    for severity in (CRITICAL, WARNING, INFO):
        hits = by_severity.get(severity, [])
        print(f"\n== {severity}: {len(hits)}")
        counts = collections.Counter((scanner, code) for _, scanner, code, _ in hits)
        for (scanner, code), count in sorted(counts.items()):
            print(f"   {scanner:15} {code:30} {count:5}")
            if verbose or severity == CRITICAL:
                for _, s, c, where in hits:
                    if (s, c) == (scanner, code):
                        print(f"        {where}")
    critical = len(by_severity.get(CRITICAL, []))
    print("\nRESULT:", "FAIL — critical findings" if critical
          else "PASS — no critical findings")
    return 1 if critical else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--zip", help="plugin ZIP to scan (default: build one)")
    parser.add_argument("--tools", default=os.path.join(
        REPO, "tmp", "scan-venv", "Scripts" if os.name == "nt" else "bin"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    scratch = os.path.join(REPO, "tmp", "qgis-scan")
    zip_path = args.zip or build_zip(scratch)
    print(f"Scanning {zip_path}")
    os.makedirs(scratch, exist_ok=True)
    root = tempfile.mkdtemp(prefix="extract-", dir=scratch)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(root)
        findings = (file_analysis(zip_path) + bandit(args.tools, root)
                    + detect_secrets(args.tools, root) + flake8(args.tools, root))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return report(findings, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
