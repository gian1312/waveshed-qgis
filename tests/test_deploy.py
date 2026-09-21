"""Tests for deploy.py binary staging/verification (no QGIS required).

deploy.py lives at the repo root and imports only the standard library, so
these tests import it directly without the QGIS stubs the other suites need.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

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


# ---------------------------------------------------------------------------
# resolve_plugin_dir — per-platform default locations
# ---------------------------------------------------------------------------

_TAIL = ("QGIS", "QGIS3", "profiles", "default", "python", "plugins")


def test_resolve_plugin_dir_windows(tmp_path, monkeypatch):
    """On Windows the plugins dir lives under %APPDATA%."""
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    assert deploy.resolve_plugin_dir("") == appdata.joinpath(*_TAIL)


def test_resolve_plugin_dir_macos(tmp_path, monkeypatch):
    """On macOS the plugins dir lives under ~/Library/Application Support."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    expected = tmp_path.joinpath("Library", "Application Support", *_TAIL)
    assert deploy.resolve_plugin_dir("") == expected


def test_resolve_plugin_dir_linux(tmp_path, monkeypatch):
    """On Linux the plugins dir lives under ~/.local/share."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    expected = tmp_path.joinpath(".local", "share", *_TAIL)
    assert deploy.resolve_plugin_dir("") == expected


def test_resolve_plugin_dir_honours_existing_override(tmp_path):
    """An existing override dir wins over the platform default."""
    assert deploy.resolve_plugin_dir(str(tmp_path)) == tmp_path


# ---------------------------------------------------------------------------
# Install -> status -> remove round trip (temp fake plugin dir only)
# ---------------------------------------------------------------------------

def _no_config(monkeypatch):
    """Neutralise deploy.local.ini so status/remove use only the CLI args."""
    monkeypatch.setattr(
        deploy, "read_config",
        lambda required=True: {"qgis_exe": "", "plugin_dir": "", "aether_bin_dir": ""},
    )


def _stage_fake_plugin(plugins: Path, version: str = "7.7.7") -> Path:
    """Copy a fake waveshed/ package into the temp plugins dir via copy_plugin."""
    src = plugins.parent / "fake_src"
    (src).mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("# fake\n")
    (src / "metadata.txt").write_text(f"[general]\nname=Waveshed\nversion={version}\n")
    (src / "bin").mkdir()
    (src / "bin" / "aether_core.exe").write_text("x")
    deploy.copy_plugin(src, plugins / "waveshed")
    return src


def test_install_status_remove_round_trip(tmp_path, capsys, monkeypatch):
    _no_config(monkeypatch)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    src = _stage_fake_plugin(plugins, version="7.7.7")

    # --status reports it installed, with version and the staged binary.
    assert deploy.main(["--status", "--plugin-dir", str(plugins)]) == 0
    out = capsys.readouterr().out
    assert "installed=yes" in out
    assert "version=7.7.7" in out
    assert "binary.aether_core.exe=present (REQUIRED)" in out

    # --remove deletes the installed package.
    assert deploy.main(["--remove", "--plugin-dir", str(plugins)]) == 0
    out = capsys.readouterr().out
    assert "Removed" in out
    assert not (plugins / "waveshed").exists()

    # --status now reports it gone.
    assert deploy.main(["--status", "--plugin-dir", str(plugins)]) == 0
    out = capsys.readouterr().out
    assert "installed=no" in out

    # Neither the fake source nor the real repo source was touched.
    assert (src / "metadata.txt").is_file()
    assert deploy.SOURCE_DIR.is_dir()


def test_remove_empty_target_is_noop(tmp_path, capsys, monkeypatch):
    _no_config(monkeypatch)
    plugins = tmp_path / "plugins"
    plugins.mkdir()

    assert deploy.main(["--remove", "--plugin-dir", str(plugins)]) == 0
    out = capsys.readouterr().out
    assert "Nothing to remove" in out


def test_remove_refuses_to_delete_source(capsys):
    """--plugin-dir pointing at the repo root must not wipe the source tree."""
    assert deploy.main(["--remove", "--plugin-dir", str(deploy.SCRIPT_DIR)]) == 1
    out = capsys.readouterr().out
    assert "refusing to remove the plugin source" in out
    assert deploy.SOURCE_DIR.is_dir()


# ---------------------------------------------------------------------------
# --no-kill / --no-launch / --headless wiring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "flags, expect_kill, expect_launch",
    [
        ([], True, True),                        # default: interactive install
        (["--no-kill"], False, True),
        (["--no-launch"], True, False),
        (["--headless"], False, False),
        (["--quiet"], False, False),             # alias of --headless
        (["--no-kill", "--no-launch"], False, False),
    ],
)
def test_flags_control_kill_and_launch(tmp_path, monkeypatch, flags, expect_kill, expect_launch):
    qgis_exe = tmp_path / "qgis.bin"
    qgis_exe.write_text("")
    plugins = tmp_path / "plugins"
    plugins.mkdir()

    monkeypatch.setattr(deploy, "read_config", lambda required=True: {
        "qgis_exe": str(qgis_exe), "plugin_dir": str(plugins), "aether_bin_dir": "",
    })
    kill = mock.Mock(return_value=False)
    launch = mock.Mock()
    monkeypatch.setattr(deploy, "kill_qgis", kill)
    monkeypatch.setattr(deploy, "launch_qgis", launch)
    monkeypatch.setattr(deploy, "copy_plugin", mock.Mock(return_value=0))

    assert deploy.main(flags) == 0
    assert kill.called is expect_kill
    assert launch.called is expect_launch


# ---------------------------------------------------------------------------
# --qgis 3 / 4 / both — QGIS 4 keeps its own profile folder and executable
# ---------------------------------------------------------------------------

_TAIL4 = ("QGIS", "QGIS4", "profiles", "default", "python", "plugins")


def test_resolve_plugin_dir_qgis4_uses_its_own_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    assert deploy.resolve_plugin_dir("", major="4") == appdata.joinpath(*_TAIL4)
    assert deploy.resolve_plugin_dir("", major="3") == appdata.joinpath(*_TAIL)
    assert deploy.resolve_plugin_dir("") == appdata.joinpath(*_TAIL)


def test_read_config_exposes_the_qgis4_section(tmp_path, monkeypatch):
    ini = tmp_path / "deploy.local.ini"
    ini.write_text(
        "[paths]\nqgis_exe = C:/q3/qgis-bin.exe\nplugin_dir =\naether_bin_dir =\n"
        "[qgis4]\nqgis_exe = C:/q4/qgis-qt6-bin.exe\nplugin_dir = C:/q4/plugins\n"
    )
    monkeypatch.setattr(deploy, "CONFIG_FILE", ini)
    cfg = deploy.read_config()
    assert cfg["qgis_exe"] == "C:/q3/qgis-bin.exe"
    assert cfg["qgis4_exe"] == "C:/q4/qgis-qt6-bin.exe"
    assert cfg["qgis4_plugin_dir"] == "C:/q4/plugins"


def test_read_config_without_qgis4_section_is_blank_not_missing(tmp_path, monkeypatch):
    ini = tmp_path / "deploy.local.ini"
    ini.write_text("[paths]\nqgis_exe = C:/q3/qgis-bin.exe\n")
    monkeypatch.setattr(deploy, "CONFIG_FILE", ini)
    cfg = deploy.read_config()
    assert cfg["qgis4_exe"] == ""
    assert cfg["qgis4_plugin_dir"] == ""


def _two_installs(tmp_path, monkeypatch):
    q3 = tmp_path / "q3" / "plugins"
    q4 = tmp_path / "q4" / "plugins"
    q3.mkdir(parents=True)
    q4.mkdir(parents=True)
    exe3 = tmp_path / "qgis-bin.exe"
    exe4 = tmp_path / "qgis-qt6-bin.exe"
    exe3.write_text("")
    exe4.write_text("")
    monkeypatch.setattr(deploy, "read_config", lambda required=True: {
        "qgis_exe": str(exe3), "plugin_dir": str(q3), "aether_bin_dir": "",
        "qgis4_exe": str(exe4), "qgis4_plugin_dir": str(q4),
    })
    return q3, q4, exe3, exe4


def test_install_both_targets_both_profiles_and_launches_each(tmp_path, monkeypatch):
    q3, q4, exe3, exe4 = _two_installs(tmp_path, monkeypatch)
    launch = mock.Mock()
    monkeypatch.setattr(deploy, "kill_qgis", mock.Mock(return_value=False))
    monkeypatch.setattr(deploy, "launch_qgis", launch)
    copied = []
    monkeypatch.setattr(deploy, "copy_plugin",
                        lambda src, dst: copied.append(dst) or 0)

    assert deploy.main(["--qgis", "both"]) == 0
    assert copied == [q3 / "waveshed", q4 / "waveshed"]
    assert [c.args[0] for c in launch.call_args_list] == [str(exe3), str(exe4)]


def test_install_qgis4_only_touches_the_qgis4_profile(tmp_path, monkeypatch):
    q3, q4, exe3, exe4 = _two_installs(tmp_path, monkeypatch)
    launch = mock.Mock()
    monkeypatch.setattr(deploy, "kill_qgis", mock.Mock(return_value=False))
    monkeypatch.setattr(deploy, "launch_qgis", launch)
    copied = []
    monkeypatch.setattr(deploy, "copy_plugin",
                        lambda src, dst: copied.append(dst) or 0)

    assert deploy.main(["--qgis", "4"]) == 0
    assert copied == [q4 / "waveshed"]
    launch.assert_called_once_with(str(exe4))


def test_install_qgis4_without_its_exe_fails_loudly_unless_no_launch(tmp_path, monkeypatch, capsys):
    q3, q4, exe3, exe4 = _two_installs(tmp_path, monkeypatch)
    exe4.unlink()
    monkeypatch.setattr(deploy, "kill_qgis", mock.Mock(return_value=False))
    monkeypatch.setattr(deploy, "launch_qgis", mock.Mock())
    monkeypatch.setattr(deploy, "copy_plugin", mock.Mock(return_value=0))

    assert deploy.main(["--qgis", "4"]) == 1
    assert "[qgis4] qgis_exe" in capsys.readouterr().out

    assert deploy.main(["--qgis", "4", "--no-launch"]) == 0


def test_plugin_dir_override_refused_with_both(tmp_path, monkeypatch, capsys):
    _two_installs(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "kill_qgis", mock.Mock(return_value=False))
    monkeypatch.setattr(deploy, "launch_qgis", mock.Mock())
    monkeypatch.setattr(deploy, "copy_plugin", mock.Mock(return_value=0))

    assert deploy.main(["--qgis", "both", "--plugin-dir", str(tmp_path)]) == 1
    assert "--qgis both" in capsys.readouterr().out
    assert deploy.main(["--status", "--qgis", "both", "--plugin-dir", str(tmp_path)]) == 1


def test_status_and_remove_cover_both_majors(tmp_path, monkeypatch, capsys):
    q3, q4, _, _ = _two_installs(tmp_path, monkeypatch)
    _stage_fake_plugin(q3, version="3.3.3")
    _stage_fake_plugin(q4, version="4.4.4")

    assert deploy.main(["--status", "--qgis", "both"]) == 0
    out = capsys.readouterr().out
    assert "qgis3.version=3.3.3" in out
    assert "qgis4.version=4.4.4" in out

    assert deploy.main(["--remove", "--qgis", "both"]) == 0
    assert not (q3 / "waveshed").exists()
    assert not (q4 / "waveshed").exists()

