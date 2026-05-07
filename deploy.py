"""Fast test-deploy: close QGIS -> install plugin -> copy binaries -> launch QGIS.

Reads machine-specific paths from deploy.local.ini (gitignored).
Copy deploy.local.template.ini -> deploy.local.ini and fill in your paths.
"""

import configparser
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ============================================================================
# Options — toggle these before hitting Run in PyCharm
# ============================================================================
NO_BINARIES = False   # True = skip copying AETHER binaries
NO_LAUNCH   = False   # True = deploy only, don't start QGIS
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "deploy.local.ini"
SOURCE_DIR = SCRIPT_DIR / "aether_qgis"

BINARIES = [
    "aether_core.exe", "aether_converter.exe", "aether_export.exe",
    "dxcompiler.dll",
]

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".idea"}
SKIP_EXTS = {".pyc", ".pyo"}


def read_config() -> dict:
    if not CONFIG_FILE.exists():
        print("[Error] deploy.local.ini not found.")
        print(f"        Copy deploy.local.template.ini -> deploy.local.ini and fill in your paths.")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")

    return {
        "qgis_exe": cfg.get("paths", "qgis_exe", fallback="").strip(),
        "plugin_dir": cfg.get("paths", "plugin_dir", fallback="").strip(),
        "aether_bin_dir": cfg.get("paths", "aether_bin_dir", fallback="").strip(),
    }


def kill_qgis() -> bool:
    killed = False
    for name in ("qgis-bin.exe", "qgis-ltr-bin.exe", "qgis.exe"):
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                killed = True
        except FileNotFoundError:
            break

    if not killed and sys.platform != "win32":
        for name in ("qgis-bin", "qgis-ltr-bin", "qgis"):
            subprocess.run(["pkill", "-f", name], capture_output=True)

    return killed


def resolve_plugin_dir(override: str) -> Path:
    if override and Path(override).is_dir():
        return Path(override)

    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "QGIS" / "QGIS3" / "profiles" / "default" / "python" / "plugins"

    return Path.home() / ".local" / "share" / "QGIS" / "QGIS3" / "profiles" / "default" / "python" / "plugins"


def copy_plugin(source: Path, target: Path) -> int:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    count = 0
    for src_path in source.rglob("*"):
        if any(part in SKIP_DIRS for part in src_path.parts):
            continue
        if src_path.suffix in SKIP_EXTS:
            continue

        rel = src_path.relative_to(source)
        dst = target / rel

        if src_path.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
            count += 1

    return count


def copy_binaries(bin_dir: str, target: Path) -> int:
    bin_path = Path(bin_dir)
    bin_target = target / "bin"
    bin_target.mkdir(parents=True, exist_ok=True)

    copied = 0
    search_dirs = [bin_path]
    win_sub = bin_path / "windows"
    if win_sub.is_dir():
        search_dirs.append(win_sub)

    for search in search_dirs:
        for name in BINARIES:
            src = search / name
            if src.is_file():
                shutil.copy2(src, bin_target / name)
                copied += 1

    return copied


def main():
    cfg = read_config()

    qgis_exe = cfg["qgis_exe"]
    if not qgis_exe or not Path(qgis_exe).is_file():
        print(f"[Error] qgis_exe not found: '{qgis_exe}'")
        sys.exit(1)

    plugin_dir = resolve_plugin_dir(cfg["plugin_dir"])
    target_dir = plugin_dir / "aether_qgis"

    print("=== AETHER QGIS Plugin Deploy ===")
    print(f"  Source:    {SOURCE_DIR}")
    print(f"  Target:    {target_dir}")
    print(f"  QGIS:      {qgis_exe}")
    if cfg["aether_bin_dir"]:
        print(f"  Binaries:  {cfg['aether_bin_dir']}")
    print()

    # 1. Kill QGIS
    print("[1/4] Closing QGIS...", end=" ")
    if kill_qgis():
        time.sleep(2)
        print("closed.")
    else:
        print("not running.")

    # 2. Copy plugin
    print("[2/4] Installing plugin...", end=" ")
    count = copy_plugin(SOURCE_DIR, target_dir)
    print(f"{count} files.")

    # 3. Copy binaries
    if not NO_BINARIES and cfg["aether_bin_dir"] and Path(cfg["aether_bin_dir"]).is_dir():
        print("[3/4] Copying binaries...", end=" ")
        copied = copy_binaries(cfg["aether_bin_dir"], target_dir)
        print(f"{copied} binaries.")
    else:
        print("[3/4] Skipping binary copy.")

    # 4. Launch QGIS
    if not NO_LAUNCH:
        print("[4/4] Launching QGIS...", end=" ")
        subprocess.Popen([qgis_exe], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        print("done.")
    else:
        print("[4/4] Skipping QGIS launch.")

    print("\nDeploy complete.")


if __name__ == "__main__":
    main()
