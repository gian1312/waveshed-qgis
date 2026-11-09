"""tools/release_check.py — the scan stage fails exactly when the registry
would BLOCK the version (a Bandit or detect-secrets finding; qgis_scan exit 1
with "WOULD BE BLOCKED"), passes with informational flake8 findings, never
passes a scanner that printed no verdict, and repeats the scanner's per-check
``CHECK:`` / ``REGISTRY:`` lines in its final summary."""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import release_check as rc_mod  # noqa: E402

BLOCKED = [
    "Scanning x.zip",
    "CHECK: Bandit Security Analysis   critical   60 findings  FAIL",
    "CHECK: Secrets Detection          critical    2 findings  FAIL",
    "CHECK: Code Quality (Flake8)      info        2 findings  FAIL",
    "CHECK: File Permissions           info        0 findings  PASS",
    "CHECK: Suspicious Files           warning     0 findings  PASS",
    "   B110   39   [rule table: Warning]",
    "REGISTRY: critical checks failed: 2",
    "REGISTRY: checks passed: 2 / 5",
    "REGISTRY: pass rate: 40.0 %",
    "RESULT: WOULD BE BLOCKED — a Bandit / detect-secrets finding; ...",
]
VALIDATES = [
    "CHECK: Bandit Security Analysis   critical    0 findings  PASS",
    "CHECK: Code Quality (Flake8)      info        2 findings  FAIL",
    "REGISTRY: critical checks failed: 0",
    "REGISTRY: pass rate: 80.0 %",
    "RESULT: would validate — no critical check failed",
]


class TestParseScanOutput:
    def test_blocked(self):
        ok, verdict, summary = rc_mod.parse_scan_output(1, BLOCKED)
        assert not ok and verdict.startswith("WOULD BE BLOCKED")
        assert summary == [line for line in BLOCKED if line.startswith(("CHECK:", "REGISTRY:"))]
        assert len(summary) == 8

    def test_informational_findings_pass(self):
        ok, verdict, summary = rc_mod.parse_scan_output(0, VALIDATES)
        assert ok and verdict.startswith("would validate")
        assert "REGISTRY: pass rate: 80.0 %" in summary

    @pytest.mark.parametrize("rc, lines", [
        (1, ["Traceback (most recent call last):"]),
        (0, ["CHECK: Bandit Security Analysis   critical    0 findings  PASS"]),
        (1, VALIDATES),                      # verdict and exit disagree
        (0, BLOCKED),
    ])
    def test_no_or_contradicting_verdict_fails(self, rc, lines):
        assert rc_mod.parse_scan_output(rc, lines)[0] is False


class TestMainSummary:
    @pytest.fixture
    def stage(self, monkeypatch):
        monkeypatch.setattr(rc_mod, "_missing_modules", lambda: [])
        monkeypatch.setattr(rc_mod, "_missing_scan_tools", lambda: [])
        monkeypatch.setattr(sys, "argv", ["release_check.py"])
        monkeypatch.setattr(rc_mod, "run_tests",
                            lambda bin_dir=None: (True, "9 passed"))

        def with_scan(rc, lines):
            monkeypatch.setattr(rc_mod, "run_scan",
                                lambda: rc_mod.parse_scan_output(rc, lines))
        return with_scan

    def test_blocked_scan_fails_and_prints_the_checks(self, stage, capsys):
        stage(1, BLOCKED)
        assert rc_mod.main() == 1
        out = capsys.readouterr().out
        summary = out[out.index("==== Pre-upload check ===="):]
        assert "FAIL  Registry scans: WOULD BE BLOCKED" in summary
        for line in BLOCKED[1:6] + BLOCKED[7:10]:
            assert line in summary
        assert "NOT READY" in summary

    def test_flake8_only_findings_are_ready(self, stage, capsys):
        stage(0, VALIDATES)
        assert rc_mod.main() == 0
        out = capsys.readouterr().out
        assert "PASS  Registry scans: would validate" in out
        assert "REGISTRY: pass rate: 80.0 %" in out
        assert "READY TO UPLOAD" in out
