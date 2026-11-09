"""tools/qgis_scan.py — the plugins.qgis.org upload scan, predicted locally.

The registry (qgis-app/plugins/security_scanner.py, tasks/run_security_scan.py,
models.py) runs five checks; each passes only with zero findings; a failed
critical-severity check (Bandit, Secrets Detection) BLOCKS the version; pass
rate = passed checks / 5.  The old tool graded findings by the per-rule table
and called 0.4.0 "PASS" — the registry blocked it.

* No tools needed: the rule lists and argv (72 Bandit rules, no -ll, no
  --max-complexity, the detect-secrets excludes), the file checks, the
  verdict and the pass-rate arithmetic.
* Real analysers (``WAVESHED_SCAN_TOOLS``, else ``tmp/scan-venv``; skipped
  naming the directory when absent): one synthetic plugin scanned once plus
  two ZIPs through ``main`` — an enabled Bandit rule blocks, a disabled one (B404) and a ``# nosec``
  line are not reported, a LOW-severity enabled rule (B101) counts, a secret
  in metadata.txt is ignored and one in a .py blocks, flake8 F811 is
  reported with exit 0.
"""

import os
import sys
import zipfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import qgis_scan as qs  # noqa: E402

TOOLS = (os.environ.get("WAVESHED_SCAN_TOOLS")
         or os.path.join(_ROOT, "tmp", "scan-venv",
                         "Scripts" if os.name == "nt" else "bin"))
_HAVE_TOOLS = all(
    os.path.isfile(os.path.join(TOOLS, n + (".exe" if os.name == "nt" else "")))
    for n in ("bandit", "detect-secrets", "flake8"))
needs_tools = pytest.mark.skipif(
    not _HAVE_TOOLS, reason=f"bandit / detect-secrets / flake8 not in {TOOLS}")

METADATA = "[general]\nname=Synthetic\nversion=0.0.1\n"
CLEAN_INIT = "def classFactory(iface):\n    return None\n"
#: AWS's documented example key: detect-secrets' AWSKeyDetector fires on it.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def make_zip(tmp_path, files, modes=None):
    """A plugin ZIP ``synthetic/<name>`` per entry of *files*."""
    path = str(tmp_path / "synthetic.zip")
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            info = zipfile.ZipInfo("synthetic/" + name)
            info.external_attr = (modes or {}).get(name, 0o644) << 16
            archive.writestr(info, text)
    return path


def run(tmp_path, capsys, files, modes=None):
    rc = qs.main(["--zip", make_zip(tmp_path, files, modes), "--tools", TOOLS])
    return rc, capsys.readouterr().out


def check_line(out, name):
    return next(line for line in out.splitlines()
                if line.startswith("CHECK: " + name))


# --------------------------------------------------------------------------
# Rule lists and invocations (no tools needed).
# --------------------------------------------------------------------------

class TestRegistryInvocations:
    @pytest.fixture(autouse=True)
    def _fake_tool(self, monkeypatch):
        monkeypatch.setattr(qs, "_tool", lambda tools, name: name)

    def test_bandit_runs_the_72_enabled_rules_without_ll(self, tmp_path):
        argv = qs.bandit_argv("t", str(tmp_path))
        assert argv[:6] == ["bandit", "-r", str(tmp_path), "-f", "json",
                            "--quiet"]
        assert argv[6] == "-t"
        rules = argv[7].split(",")
        assert len(rules) == 72 == len(set(rules))
        assert not set(qs.BANDIT_DISABLED) & set(rules)
        assert set(qs.BANDIT_DISABLED) == {"B109", "B111", "B320", "B404",
                                           "B410", "B411"}
        assert "-ll" not in argv and len(argv) == 8

    def test_detect_secrets_excludes_and_disables_like_the_registry(self, tmp_path):
        assert qs.secrets_argv("t", str(tmp_path)) == [
            "detect-secrets", "scan", "--all-files",
            "--exclude-files", r"metadata\.txt",
            "--exclude-files", r"\.secrets\.baseline",
            "--disable-plugin", "IPPublicDetector", "."]
        assert len(qs.SECRETS_ENABLED) == 26

    def test_a_shipped_baseline_is_passed(self, tmp_path):
        (tmp_path / "plug").mkdir()
        (tmp_path / "plug" / ".secrets.baseline").write_text("{}")
        argv = qs.secrets_argv("t", str(tmp_path))
        assert argv[argv.index("--baseline") + 1] == os.path.join(
            "plug", ".secrets.baseline")

    def test_flake8_selects_26_codes_and_no_complexity(self, tmp_path):
        argv = qs.flake8_argv("t", str(tmp_path), ["a.py"])
        assert argv[:3] == ["flake8", "--format=json", "--max-line-length=120"]
        codes = argv[argv.index("--select") + 1].split(",")
        assert len(codes) == 26 and "F811" in codes and "E501" not in codes
        assert not any("complexity" in a for a in argv)
        assert argv[-1] == "a.py"
        plain = qs.flake8_argv("t", str(tmp_path), ["a.py"], json_format=False)
        assert "--format=json" not in plain

    def test_a_shipped_flake8_config_is_forwarded(self, tmp_path):
        (tmp_path / "plug").mkdir()
        (tmp_path / "plug" / ".flake8").write_text("[flake8]\n")
        argv = qs.flake8_argv("t", str(tmp_path), [])
        assert argv[argv.index("--config") + 1] == str(tmp_path / "plug" / ".flake8")


# --------------------------------------------------------------------------
# File checks, verdict, pass rate (no tools needed).
# --------------------------------------------------------------------------

class TestFileChecks:
    def test_only_executable_py_and_suspicious_extensions_fail(self, tmp_path):
        z = make_zip(tmp_path, {
            "__init__.py": CLEAN_INIT, "run.py": "", "tool.sh": "",
            "README": "", ".hidden": "", "icon.png": "\x89PNG\0\0",
            "data.bin": "\0\0"}, modes={"run.py": 0o755, "README": 0o755})
        perms, suspicious = qs.file_permissions(z), qs.suspicious_files(z)
        assert [(f["rule"], f["file"]) for f in perms.findings] == [
            ("FILE_EXECUTABLE", "synthetic/run.py")]
        assert [(f["rule"], f["file"]) for f in suspicious.findings] == [
            ("FILE_SUSPICIOUS", "synthetic/tool.sh")]
        assert (perms.severity, suspicious.severity) == ("info", "warning")

    def test_hidden_and_binary_files_pass(self, tmp_path):
        z = make_zip(tmp_path, {".hidden": "", "data.bin": "\0\0"})
        assert qs.file_permissions(z).passed and qs.suspicious_files(z).passed


def _checks(failing=()):
    checks = [qs.Check(qs.BANDIT, "critical"), qs.Check(qs.SECRETS, "critical"),
              qs.Check(qs.FLAKE8, "info"), qs.Check(qs.PERMISSIONS, "info"),
              qs.Check(qs.SUSPICIOUS, "warning")]
    for check in checks:
        if check.name in failing:
            check.add("X1", "f.py", 1)
    return checks


class TestVerdict:
    @pytest.mark.parametrize("failing, passed, rate, critical", [
        ((), 5, 100.0, 0),
        ((qs.FLAKE8,), 4, 80.0, 0),
        ((qs.BANDIT, qs.SECRETS, qs.FLAKE8), 2, 40.0, 2),   # 0.4.0
        ((qs.BANDIT, qs.FLAKE8, qs.SUSPICIOUS), 2, 40.0, 1),
        ((qs.SUSPICIOUS, qs.PERMISSIONS, qs.FLAKE8), 2, 40.0, 0),
        ((qs.BANDIT, qs.SECRETS, qs.FLAKE8, qs.PERMISSIONS, qs.SUSPICIOUS),
         0, 0.0, 2),
    ])
    def test_pass_rate_and_blocking(self, failing, passed, rate, critical):
        summary = qs.summarize(_checks(failing))
        assert summary["passed"] == passed and summary["total"] == 5
        assert summary["pass_rate"] == rate
        assert summary["critical_failed"] == critical
        assert summary["blocked"] is (critical > 0)

    def test_pass_rate_rounds_to_one_decimal(self):
        checks = _checks((qs.BANDIT, qs.SECRETS))[:3]      # 1 of 3 passed
        assert qs.summarize(checks)["pass_rate"] == 33.3

    def test_one_finding_in_a_critical_check_blocks_whatever_its_rule(self, capsys):
        checks = _checks()
        checks[0].add("B110", "x.py", 3, severity="LOW", confidence="HIGH")
        assert qs.report(checks) == 1
        out = capsys.readouterr().out
        assert "CHECK: Bandit Security Analysis   critical    1 findings  FAIL" in out
        assert "B110" in out and "x.py:3  severity LOW / confidence HIGH" in out
        assert "[rule table: Warning]" in out
        assert "REGISTRY: critical checks failed: 1" in out
        assert "REGISTRY: pass rate: 80.0 %" in out
        assert "RESULT: WOULD BE BLOCKED" in out

    def test_informational_failures_validate(self, capsys):
        assert qs.report(_checks((qs.FLAKE8, qs.SUSPICIOUS, qs.PERMISSIONS))) == 0
        out = capsys.readouterr().out
        assert "REGISTRY: pass rate: 40.0 %" in out
        assert "RESULT: would validate" in out


# --------------------------------------------------------------------------
# The real analysers on synthetic plugins.
# --------------------------------------------------------------------------

#: One plugin holding every case in its own file, scanned ONCE (the three
#: analysers take ~13 s to start on the container's mount).
CASES = {
    "__init__.py": CLEAN_INIT,
    "metadata.txt": METADATA + f"note={AWS_KEY}\n",    # excluded file
    "enabled.py": "def run(code):\n    exec(code)\n",  # B102 line 2
    "low.py": "def check(x):\n    assert x\n",       # B101 (LOW) line 2
    "disabled.py": ("import subprocess\n\n\n"          # B404: disabled
                    "def run(code):\n"
                    "    exec(code)  # nosec\n"          # B102 under nosec
                    "    return subprocess\n"),
    "secret.py": f'ID = "{AWS_KEY}"\n',
    "twice.py": "def f():\n    return 1\n\n\ndef f():\n    return 2\n",
}


@pytest.fixture(scope="module")
def case_scan(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scan")
    checks = qs.scan(make_zip(tmp, CASES), TOOLS, str(tmp / "scratch"))
    return {c.name: c for c in checks}


def _hits(check, name):
    return [(f["rule"], f["line"]) for f in check.findings
            if f["file"] == "synthetic/" + name]


@needs_tools
class TestRealAnalysers:
    def test_enabled_bandit_rule_is_reported(self, case_scan):
        assert _hits(case_scan[qs.BANDIT], "enabled.py") == [("B102", 2)]
        hit = [f for f in case_scan[qs.BANDIT].findings
               if f["file"] == "synthetic/enabled.py"][0]
        assert (hit["severity"], hit["confidence"]) == ("MEDIUM", "HIGH")

    def test_low_severity_enabled_rule_counts_too(self, case_scan):
        assert _hits(case_scan[qs.BANDIT], "low.py") == [("B101", 2)]

    def test_disabled_rule_and_nosec_are_not_reported(self, case_scan):
        assert _hits(case_scan[qs.BANDIT], "disabled.py") == []

    def test_bandit_reports_nothing_else(self, case_scan):
        assert sorted(f["file"] for f in case_scan[qs.BANDIT].findings) == [
            "synthetic/enabled.py", "synthetic/low.py"]

    def test_secret_in_metadata_is_ignored_in_python_reported(self, case_scan):
        secrets = case_scan[qs.SECRETS]
        assert [(f["file"], f["rule"], f["line"]) for f in secrets.findings] == [
            ("synthetic/secret.py", "AWS Access Key", 1)]

    def test_flake8_reports_f811_only(self, case_scan):
        assert [(f["file"], f["rule"], f["line"])
                for f in case_scan[qs.FLAKE8].findings] == [
            ("synthetic/twice.py", "F811", 5)]

    def test_the_case_plugin_would_be_blocked_at_40_percent(self, case_scan, capsys):
        checks = list(case_scan.values())
        assert qs.summarize(checks) == {"total": 5, "passed": 2,
                                        "critical_failed": 2,
                                        "pass_rate": 40.0, "blocked": True}
        assert qs.report(checks) == 1
        assert "RESULT: WOULD BE BLOCKED" in capsys.readouterr().out

    def test_main_blocks_on_one_enabled_bandit_finding(self, tmp_path, capsys):
        rc, out = run(tmp_path, capsys, {"__init__.py": CLEAN_INIT,
                                         "metadata.txt": METADATA,
                                         "enabled.py": CASES["enabled.py"]})
        assert rc == 1, out
        assert check_line(out, "Bandit").endswith("1 findings  FAIL")
        assert "synthetic/enabled.py:2  severity MEDIUM / confidence HIGH" in out
        assert "REGISTRY: critical checks failed: 1" in out
        assert "REGISTRY: pass rate: 80.0 %" in out
        assert "RESULT: WOULD BE BLOCKED" in out

    def test_main_validates_with_f811_and_an_executable_py(self, tmp_path, capsys):
        rc, out = run(tmp_path, capsys, {"__init__.py": CLEAN_INIT,
                                         "metadata.txt": METADATA,
                                         "twice.py": CASES["twice.py"]},
                      modes={"__init__.py": 0o755})
        assert rc == 0, out
        assert check_line(out, "Code Quality").endswith("1 findings  FAIL")
        assert check_line(out, "File Permissions").endswith("1 findings  FAIL")
        assert "synthetic/twice.py:5" in out
        assert "REGISTRY: critical checks failed: 0" in out
        assert "REGISTRY: pass rate: 60.0 %" in out
        assert "RESULT: would validate" in out
