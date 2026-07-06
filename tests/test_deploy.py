"""Tests for deploy.py binary staging/verification (no QGIS required).

deploy.py lives at the repo root and imports only the standard library, so
these tests import it directly without the QGIS stubs the other suites need.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deploy  # noqa: E402


def _stage(dirpath: Path, name: str, mtime: str, content: str = "x") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(content)
    # Set a deterministic mtime (epoch seconds) for age/staleness assertions.
    ts = _epoch(mtime)
    os.utime(p, (ts, ts))
    return p


def _epoch(iso: str) -> float:
    from datetime import datetime
    return datetime.strptime(iso, "%Y-%m-%d %H:%M").timestamp()


def test_copy_binaries_prefers_windows_subdir(tmp_path):
    """A binary present in both bin/ and bin/windows/ is taken from windows/."""
    bin_dir = tmp_path / "aether" / "bin"
    _stage(bin_dir, "aether_core.exe", "2026-01-01 00:00", content="root")
    _stage(bin_dir / "windows", "aether_core.exe", "2026-06-01 00:00", content="windows")

    target = tmp_path / "plugin"
    records = deploy.copy_binaries(str(bin_dir), target)

    core = next(r for r in records if r["name"] == "aether_core.exe")
    assert core["src"].parent.name == "windows"
    # The copied file is the windows/ variant.
    assert (target / "bin" / "aether_core.exe").read_text() == "windows"


def test_report_flags_missing_required(tmp_path, capsys):
    """Missing engine executables are returned; optional DLLs are not."""
    bin_dir = tmp_path / "aether" / "bin" / "windows"
    _stage(bin_dir, "aether_core.exe", "2026-06-01 00:00")
    _stage(bin_dir, "aether_converter.exe", "2026-06-01 00:00")
    # aether_export.exe (required) and dxcompiler.dll (optional) both absent.

    target = tmp_path / "plugin"
    records = deploy.copy_binaries(str(tmp_path / "aether" / "bin"), target)
    missing = deploy.report_binaries(records, str(tmp_path / "aether" / "bin"))

    assert missing == ["aether_export.exe"]
    out = capsys.readouterr().out
    assert "aether_export.exe: MISSING (REQUIRED)" in out
    assert "dxcompiler.dll: MISSING (optional)" in out


def test_report_detects_stale_stage(tmp_path):
    """A newer cargo build in rust/target flags the staged copy as stale."""
    root = tmp_path / "aether"
    bin_dir = root / "bin"
    _stage(bin_dir / "windows", "aether_core.exe", "2026-06-01 00:00")
    # Fresh build, newer than the staged copy.
    rel = root / "rust" / "target" / "x86_64-pc-windows-msvc" / "release"
    _stage(rel, "aether_core.exe", "2026-07-01 00:00")

    records = deploy.copy_binaries(str(bin_dir), tmp_path / "plugin")
    core = next(r for r in records if r["name"] == "aether_core.exe")
    newer = deploy._newer_build_than(core, str(bin_dir))

    assert newer is not None
    assert newer[0] == rel / "aether_core.exe"


def test_report_no_false_stale_when_stage_current(tmp_path):
    """No stale flag when the staged copy is at least as new as the build."""
    root = tmp_path / "aether"
    bin_dir = root / "bin"
    _stage(bin_dir / "windows", "aether_core.exe", "2026-07-01 00:00")
    rel = root / "rust" / "target" / "x86_64-pc-windows-msvc" / "release"
    _stage(rel, "aether_core.exe", "2026-06-01 00:00")  # older build

    records = deploy.copy_binaries(str(bin_dir), tmp_path / "plugin")
    core = next(r for r in records if r["name"] == "aether_core.exe")

    assert deploy._newer_build_than(core, str(bin_dir)) is None
