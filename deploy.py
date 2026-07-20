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
SOURCE_DIR = SCRIPT_DIR / "waveshed"

BINARIES = [
    "aether_core.exe", "aether_converter.exe", "aether_export.exe",
    "dxcompiler.dll",
]

# The three engine executables the plugin cannot run without. dxcompiler.dll
# is a GPU-shader dependency of aether_core and is treated as optional here so
# a missing DLL warns rather than blocks (CPU backend still works).
REQUIRED_BINARIES = {
    "aether_core.exe", "aether_converter.exe", "aether_export.exe",
}

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


# Rust build outputs (per-target release dirs) that AETHER's rust/deploy.ps1
# stages into bin/windows. We peek at these to warn when the staged copy the
# plugin deploys is older than a fresh `cargo build --release` — i.e. the dev
# rebuilt AETHER but forgot to re-run rust/deploy.ps1, so we'd ship a stale
# engine. Paths are relative to the AETHER repo root (the parent of bin/).
_RUST_RELEASE_SUBPATHS = (
    Path("rust") / "target" / "x86_64-pc-windows-msvc" / "release",
    Path("rust") / "target" / "release",
)


def copy_binaries(bin_dir: str, target: Path) -> list:
    """Copy engine binaries into <plugin>/bin.

    Searches <bin_dir> and <bin_dir>/windows (rust/deploy.ps1 stages the
    Windows executables under windows/); when a binary exists in both, the
    windows/ copy wins. Returns one record per binary actually found:
    ``{"name", "src", "size", "mtime"}`` so the caller can verify the
    required set was copied and report each binary's build date.
    """
    bin_path = Path(bin_dir)
    bin_target = target / "bin"
    bin_target.mkdir(parents=True, exist_ok=True)

    search_dirs = [bin_path]
    win_sub = bin_path / "windows"
    if win_sub.is_dir():
        search_dirs.append(win_sub)

    # Last search dir wins (windows/ overrides the bin/ root), matching the
    # previous copy-twice behaviour without copying the same file twice.
    found: dict = {}
    for search in search_dirs:
        for name in BINARIES:
            src = search / name
            if src.is_file():
                st = src.stat()
                found[name] = {
                    "name": name, "src": src,
                    "size": st.st_size, "mtime": st.st_mtime,
                }

    for rec in found.values():
        shutil.copy2(rec["src"], bin_target / rec["name"])

    return list(found.values())


def _newer_build_than(rec: dict, aether_bin_dir: str):
    """Return a fresh build of this binary newer than the staged copy, else None.

    Best-effort: looks in the AETHER rust release target dirs next to bin/.
    Used purely to warn the dev that bin/windows is stale.
    """
    aether_root = Path(aether_bin_dir).resolve().parent  # <root>/bin -> <root>
    newest = None
    for sub in _RUST_RELEASE_SUBPATHS:
        cand = aether_root / sub / rec["name"]
        try:
            if cand.is_file():
                mtime = cand.stat().st_mtime
                # +2s guard against filesystem timestamp granularity.
                if mtime > rec["mtime"] + 2 and (newest is None or mtime > newest[1]):
                    newest = (cand, mtime)
        except OSError:
            continue
    return newest


def report_binaries(records: list, aether_bin_dir: str) -> list:
    """Print each binary's size/build-date, flag stale stages, and return the
    list of missing REQUIRED binary names (empty when all present)."""
    from datetime import datetime

    by_name = {r["name"]: r for r in records}
    now = datetime.now()
    for name in BINARIES:
        rec = by_name.get(name)
        if rec is None:
            tag = "REQUIRED" if name in REQUIRED_BINARIES else "optional"
            print(f"         - {name}: MISSING ({tag})")
            continue
        built = datetime.fromtimestamp(rec["mtime"])
        age_days = (now - built).days
        note = ""
        newer = _newer_build_than(rec, aether_bin_dir)
        if newer:
            note = (
                f"  <-- STALE: a newer build exists at {newer[0]} "
                f"(run AETHER rust/deploy.ps1 to stage it)"
            )
        print(
            f"         - {name}: {rec['size'] // 1024} KB, "
            f"built {built:%Y-%m-%d %H:%M} ({age_days}d ago){note}"
        )

    return [n for n in REQUIRED_BINARIES if n not in by_name]


def main():
    cfg = read_config()

    qgis_exe = cfg["qgis_exe"]
    if not qgis_exe or not Path(qgis_exe).is_file():
        print(f"[Error] qgis_exe not found: '{qgis_exe}'")
        sys.exit(1)

    plugin_dir = resolve_plugin_dir(cfg["plugin_dir"])
    target_dir = plugin_dir / "waveshed"

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
    missing_required: list = []
    if not NO_BINARIES and cfg["aether_bin_dir"] and Path(cfg["aether_bin_dir"]).is_dir():
        print(f"[3/4] Copying binaries from {cfg['aether_bin_dir']} ...")
        records = copy_binaries(cfg["aether_bin_dir"], target_dir)
        missing_required = report_binaries(records, cfg["aether_bin_dir"])
        print(f"       {len(records)} binaries copied.")
        if missing_required:
            print()
            print("[WARN] Missing REQUIRED engine binaries: "
                  + ", ".join(sorted(missing_required)))
            print("       The plugin cannot run analyses without these. Build")
            print("       them in AETHER (rust/deploy.ps1) and re-run deploy.")
    elif NO_BINARIES:
        print("[3/4] Skipping binary copy (NO_BINARIES).")
    else:
        print("[3/4] Skipping binary copy (aether_bin_dir not set or missing).")

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
