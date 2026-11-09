#!/usr/bin/env python3
"""Predict the plugins.qgis.org upload scan locally, on the ZIP that ships.

The registry's model (github.com/qgis/QGIS-Plugins-Website, ``qgis-app/
plugins/security_scanner.py`` — ``PluginSecurityScanner``; ``qgis-app/plugins/
tasks/run_security_scan.py`` — the blocking decision; ``qgis-app/plugins/
models.py`` — ``PluginVersionSecurityScan.pass_rate``; rule lists from
``management/commands/data/*_rules.json`` as overridden by the admins, live at
https://plugins.qgis.org/docs/security-scanning/rules, read 2026-11-09):

Every upload runs five CHECKS.  A check passes only when it has ZERO findings;
the rule a finding came from does not grade it — the check's own severity does.

==========================  ==========  =====================================
Check                       Severity    What runs
==========================  ==========  =====================================
Bandit Security Analysis    critical    ``bandit -r <dir> -f json --quiet
                                        -t <the 72 enabled rules>`` (no -ll,
                                        so LOW findings count; ``# nosec``
                                        honoured)
Secrets Detection           critical    ``detect-secrets scan --all-files
                                        --exclude-files metadata\\.txt
                                        --exclude-files \\.secrets\\.baseline
                                        [--baseline ..] --disable-plugin
                                        IPPublicDetector .`` in the extracted
                                        tree; every finding fails it
Code Quality (Flake8)       info        ``flake8 --format=json
                                        --max-line-length=120 [--config
                                        .flake8] --select <26 codes>`` on
                                        every .py (plain-format fallback when
                                        flake8-json is absent; NO
                                        --max-complexity, so C901 is inert)
File Permissions            info        .py ZIP entries with an execute bit
Suspicious Files            warning     .exe .dll .so .dylib .bat .sh .ps1
                                        .cmd entries (FILE_HIDDEN is
                                        disabled; FILE_BINARY is enabled in
                                        the rule table but no code checks it)
==========================  ==========  =====================================

A version is BLOCKED when any critical-severity check failed — i.e. any
Bandit or detect-secrets finding at all.  Pass rate = passed checks / 5,
rounded to 0.1 %.  A blocked version can NOT be unblocked or rescanned into
"validated": the next upload needs a higher version number.  Developers may
skip *skippable* rules at upload time; this tool assumes none are skipped.

Output: one ``CHECK:`` line per check (name, severity, findings, PASS/FAIL),
the findings grouped by rule with file:line (Bandit with its issue severity /
confidence), then ``REGISTRY:`` lines (critical checks failed, pass rate) and
the ``RESULT:`` verdict "WOULD BE BLOCKED" / "would validate".  The rule
table's own per-rule severity is shown in brackets for information only.

Exit status 1 when the registry would block the version, else 0 (flake8,
permission and suspicious-file findings are informational).

The three analysers are dev tools, never plugin dependencies.  Install them
(the check container pins bandit 1.9.4, detect-secrets 1.5.0, flake8 7.4.1)
into a scratch venv inside the gitignored ``tmp/`` folder::

    python3 -m venv tmp/scan-venv
    tmp/scan-venv/bin/pip install bandit detect-secrets flake8

Usage::

    python3 tools/qgis_scan.py [--zip dist/waveshed.X.Y.Z.zip] [--tools DIR]
                               [--verbose]

Without ``--zip`` the package is built first (``package.py`` into
``tmp/qgis-scan``).  ``--tools`` is the directory holding the analyser
executables (default ``tmp/scan-venv/bin``, ``Scripts`` on Windows; else
``PATH``).  ``--verbose`` adds each finding's message.

Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess  # nosec B404 — runs the local analysers, fixed argv
import sys
import tempfile
import zipfile
from typing import Dict, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# The registry's enabled rules (rules page, 2026-11-09).  The JSON seed files
# still enable B111/B320; the live table has them off — the live table wins.
# --------------------------------------------------------------------------

BANDIT_ENABLED = (
    "B101 B102 B103 B104 B105 B106 B107 B108 B110 B112 B113 B201 B202 B301 "
    "B302 B303 B304 B305 B306 B307 B308 B310 B311 B312 B313 B314 B315 B316 "
    "B317 B318 B319 B321 B323 B324 B401 B402 B403 B405 B406 B407 B408 B409 "
    "B412 B413 B501 B502 B503 B504 B505 B506 B507 B508 B509 B601 B602 B603 "
    "B604 B605 B606 B607 B608 B609 B610 B611 B612 B613 B614 B615 B701 B702 "
    "B703 B704").split()
BANDIT_DISABLED = ("B109", "B111", "B320", "B404", "B410", "B411")

#: All detect-secrets plugins in the registry's table; the scanner passes
#: ``--disable-plugin`` for every one that is not enabled.
SECRETS_DISABLED = ("IPPublicDetector",)
SECRETS_ENABLED = (
    "AWSKeyDetector ArtifactoryDetector AzureStorageKeyDetector "
    "BasicAuthDetector CloudantDetector DiscordBotTokenDetector "
    "GitHubTokenDetector GitLabTokenDetector IbmCloudIamDetector "
    "IbmCosHmacDetector JwtTokenDetector MailchimpDetector NpmDetector "
    "OpenAIDetector PrivateKeyDetector PypiTokenDetector SendGridDetector "
    "SlackDetector SoftlayerDetector SquareOAuthDetector StripeDetector "
    "TelegramBotTokenDetector TwilioKeyDetector Base64HighEntropyString "
    "HexHighEntropyString KeywordDetector").split()

FLAKE8_ENABLED = (
    "E901 E902 E999 F821 F823 F831 C901 E101 E711 E712 E713 E714 E721 E722 "
    "E731 E741 E742 E743 F402 F403 F404 F405 F811 F822 F901 W605").split()

FILE_ANALYSIS_ENABLED = ("FILE_SUSPICIOUS", "FILE_BINARY", "FILE_EXECUTABLE")
SUSPICIOUS_EXTS = (".exe", ".dll", ".so", ".dylib", ".bat", ".sh", ".ps1",
                   ".cmd")
#: Files the secrets check drops from detect-secrets' results.
_SECRETS_SKIP_EXTS = (".pyc", ".pyo", ".so", ".dll", ".exe")

# Per-rule severity from the rule table — shown for information only; the
# verdict comes from the CHECK severity below.
_RULE_CRITICAL = {
    "B102", "B103", "B105", "B106", "B107", "B201", "B202", "B301", "B302",
    "B304", "B305", "B306", "B307", "B312", "B321", "B323", "B401", "B402",
    "B412", "B413", "B501", "B502", "B503", "B505", "B506", "B507", "B601",
    "B602", "B604", "B605", "B609", "B610", "B611", "B612", "B613", "B615",
    "B701", "E901", "E902", "E999", "F821", "F823", "F831", "FILE_SUSPICIOUS",
}

BANDIT = "Bandit Security Analysis"
SECRETS = "Secrets Detection"
FLAKE8 = "Code Quality (Flake8)"
PERMISSIONS = "File Permissions"
SUSPICIOUS = "Suspicious Files"
CRITICAL, WARNING, INFO = "critical", "warning", "info"


class Check:
    """One registry check: passed only with zero findings."""

    def __init__(self, name: str, severity: str) -> None:
        self.name = name
        self.severity = severity
        self.findings: List[Dict[str, object]] = []
        self.notes: List[str] = []

    @property
    def passed(self) -> bool:
        return not self.findings

    def add(self, rule: str, file: str, line: int = 0, **extra: object) -> None:
        self.findings.append(dict(rule=rule, file=file, line=line, **extra))


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


def _run(argv: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,  # nosec B603
                          cwd=cwd)


def _rel(path: str, root: str) -> str:
    return os.path.relpath(os.path.join(root, path), root).replace(os.sep, "/")


def build_zip(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    result = _run([sys.executable, os.path.join(REPO, "package.py"), out_dir])
    if result.returncode != 0:
        sys.exit("package.py failed:\n" + result.stdout + result.stderr)
    zips = sorted(f for f in os.listdir(out_dir) if f.endswith(".zip"))
    if not zips:
        sys.exit(f"package.py produced no ZIP in {out_dir}")
    return os.path.join(out_dir, zips[-1])


def _python_files(root: str) -> List[str]:
    return [os.path.join(dirpath, name)
            for dirpath, _, names in os.walk(root)
            for name in names if name.endswith(".py")]


# --------------------------------------------------------------------------
# The five checks (security_scanner.py, same order).
# --------------------------------------------------------------------------

def bandit_argv(tools_dir: str, root: str) -> List[str]:
    return [_tool(tools_dir, "bandit"), "-r", root, "-f", "json", "--quiet",
            "-t", ",".join(sorted(BANDIT_ENABLED))]


def bandit(tools_dir: str, root: str) -> Check:
    check = Check(BANDIT, CRITICAL)
    if not _python_files(root):
        return check
    result = _run(bandit_argv(tools_dir, root))
    if not result.stdout.strip():
        # The registry passes the check here; do not hide it.
        check.notes.append("bandit printed nothing: " + result.stderr[:200])
        return check
    report = json.loads(result.stdout)
    for hit in report.get("results", []):
        check.add(hit.get("test_id", ""), _rel(hit.get("filename", ""), root),
                  hit.get("line_number", 0),
                  severity=hit.get("issue_severity", ""),
                  confidence=hit.get("issue_confidence", ""),
                  message=hit.get("issue_text", ""))
    for error in report.get("errors", []):
        # Ignored by the registry (it reads only "results").
        check.notes.append(f"bandit could not scan {error.get('filename', '?')}"
                           f": {error.get('reason', '')}")
    return check


def secrets_argv(tools_dir: str, root: str) -> List[str]:
    argv = [_tool(tools_dir, "detect-secrets"), "scan", "--all-files",
            "--exclude-files", r"metadata\.txt",
            "--exclude-files", r"\.secrets\.baseline"]
    for dirpath, _, names in os.walk(root):
        if ".secrets.baseline" in names:
            argv += ["--baseline", os.path.relpath(
                os.path.join(dirpath, ".secrets.baseline"), root)]
            break
    for plugin in SECRETS_DISABLED:
        argv += ["--disable-plugin", plugin]
    return argv + ["."]


def detect_secrets(tools_dir: str, root: str) -> Check:
    check = Check(SECRETS, CRITICAL)
    result = _run(secrets_argv(tools_dir, root), cwd=root)
    if not result.stdout:
        check.notes.append("detect-secrets printed nothing: "
                           + result.stderr[:200])
        return check
    for filename, hits in json.loads(result.stdout).get("results", {}).items():
        if filename.endswith(_SECRETS_SKIP_EXTS):
            continue
        for hit in hits:
            check.add(hit.get("type", "Unknown"), _rel(filename, root),
                      hit.get("line_number", 0))
    return check


def _flake8_config(root: str) -> Optional[str]:
    config = os.path.join(root, ".flake8")
    if os.path.isfile(config):
        return config
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, ".flake8")):
            return os.path.join(entry.path, ".flake8")
    return None


def flake8_argv(tools_dir: str, root: str, files: Sequence[str],
                json_format: bool = True) -> List[str]:
    argv = [_tool(tools_dir, "flake8")]
    if json_format:
        argv.append("--format=json")
    argv.append("--max-line-length=120")
    config = _flake8_config(root)
    if config:
        argv += ["--config", config]
    return argv + ["--select", ",".join(sorted(FLAKE8_ENABLED)), *files]


def flake8(tools_dir: str, root: str) -> Check:
    check = Check(FLAKE8, INFO)
    files = _python_files(root)
    if not files:
        return check
    # An empty cwd: flake8 must not pick up a setup.cfg / tox.ini of ours.
    cwd = tempfile.mkdtemp(prefix="flake8-cwd-")
    try:
        result = _run(flake8_argv(tools_dir, root, files), cwd=cwd)
        try:
            data = json.loads(result.stdout) if result.stdout else None
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for path, issues in data.items():
                for issue in issues:
                    check.add(issue.get("code", ""), _rel(path, root),
                              issue.get("line_number", 0),
                              message=issue.get("text", ""))
            return check
        # Registry fallback: no flake8-json plugin → default text format.
        result = _run(flake8_argv(tools_dir, root, files, json_format=False),
                      cwd=cwd)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        code, _, message = parts[3].strip().partition(" ")
        check.add(code, _rel(parts[0], root),
                  int(parts[1]) if parts[1].isdigit() else 0, message=message)
    return check


def file_permissions(zip_path: str) -> Check:
    check = Check(PERMISSIONS, INFO)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if mode & 0o111 and info.filename.endswith(".py"):
                check.add("FILE_EXECUTABLE", info.filename)
    return check


def suspicious_files(zip_path: str) -> Check:
    # FILE_HIDDEN is disabled in the registry's table, so hidden names pass.
    check = Check(SUSPICIOUS, WARNING)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.lower()
            if name.endswith(SUSPICIOUS_EXTS):
                check.add("FILE_SUSPICIOUS", info.filename)
    return check


def scan(zip_path: str, tools_dir: str, scratch: str) -> List[Check]:
    os.makedirs(scratch, exist_ok=True)
    root = tempfile.mkdtemp(prefix="extract-", dir=scratch)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(root)
        return [bandit(tools_dir, root), detect_secrets(tools_dir, root),
                flake8(tools_dir, root), file_permissions(zip_path),
                suspicious_files(zip_path)]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# The registry's verdict (run_security_scan.py, models.py).
# --------------------------------------------------------------------------

def summarize(checks: Sequence[Check]) -> Dict[str, object]:
    passed = sum(1 for c in checks if c.passed)
    critical = sum(1 for c in checks if not c.passed and c.severity == CRITICAL)
    total = len(checks)
    return {
        "total": total,
        "passed": passed,
        "critical_failed": critical,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "blocked": critical > 0,
    }


def _rule_label(rule: str) -> str:
    # Everything else enabled is Warning (entropy / keyword secrets too).
    return "Critical" if rule in _RULE_CRITICAL else "Warning"


def report(checks: Sequence[Check], verbose: bool = False) -> int:
    for check in checks:
        print(f"CHECK: {check.name:26} {check.severity:8} "
              f"{len(check.findings):4} findings  "
              f"{'PASS' if check.passed else 'FAIL'}")
    for check in checks:
        if check.passed and not check.notes:
            continue
        print(f"\n== {check.name} ({check.severity}): "
              f"{len(check.findings)} findings")
        for note in check.notes:
            print(f"   note: {note}")
        groups: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
        for finding in check.findings:
            groups[str(finding["rule"])].append(finding)
        for rule in sorted(groups):
            hits = groups[rule]
            print(f"   {rule:30} {len(hits):4}   [rule table: "
                  f"{_rule_label(rule)}]")
            for hit in hits:
                where = f"{hit['file']}:{hit['line']}" if hit["line"] else hit["file"]
                extra = ""
                if "severity" in hit:
                    extra = f"  severity {hit['severity']} / confidence {hit['confidence']}"
                if verbose and hit.get("message"):
                    extra += f"  — {hit['message']}"
                print(f"        {where}{extra}")
    summary = summarize(checks)
    print(f"\nREGISTRY: critical checks failed: {summary['critical_failed']}")
    print(f"REGISTRY: checks passed: {summary['passed']} / {summary['total']}")
    print(f"REGISTRY: pass rate: {summary['pass_rate']} %")
    if summary["blocked"]:
        print("RESULT: WOULD BE BLOCKED — a Bandit / detect-secrets finding; a "
              "blocked version cannot be unblocked, re-upload needs a higher "
              "version")
        return 1
    print("RESULT: would validate — no critical check failed")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--zip", help="plugin ZIP to scan (default: build one)")
    parser.add_argument("--tools", default=os.path.join(
        REPO, "tmp", "scan-venv", "Scripts" if os.name == "nt" else "bin"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    scratch = os.path.join(REPO, "tmp", "qgis-scan")
    zip_path = args.zip or build_zip(scratch)
    # detect-secrets and flake8 run from inside the extracted tree, so a
    # relative --tools (e.g. tmp/scan-venv/bin) must be resolved first.
    tools_dir = os.path.abspath(args.tools)
    print(f"Scanning {zip_path}")
    return report(scan(zip_path, tools_dir, scratch), args.verbose)


if __name__ == "__main__":
    sys.exit(main())
