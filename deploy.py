"""Fast test-deploy: close QGIS -> install plugin -> copy binaries -> launch QGIS.

Reads machine-specific paths from deploy.local.ini (gitignored).
Copy deploy.local.template.ini -> deploy.local.ini and fill in your paths.

Run with no arguments for the interactive install above. Other modes:
  python deploy.py --status    # report install location / version / binaries
  python deploy.py --remove    # uninstall the plugin (leaves the source alone)
  python deploy.py --headless  # install without killing/launching QGIS
See ``python deploy.py --help`` for the full flag list.
"""

import argparse
import configparser
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

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


def read_config(required: bool = True) -> dict:
    if not CONFIG_FILE.exists():
        if required:
            print("[Error] deploy.local.ini not found.")
            print(f"        Copy deploy.local.template.ini -> deploy.local.ini and fill in your paths.")
            sys.exit(1)
        return {"qgis_exe": "", "plugin_dir": "", "aether_bin_dir": ""}

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


def launch_qgis(qgis_exe: str) -> None:
    """Start QGIS detached from this process."""
    subprocess.Popen([qgis_exe], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def resolve_plugin_dir(override: str) -> Path:
    """Return the QGIS plugins dir: an existing ``override`` wins, else the
    per-platform default profile location."""
    if override and Path(override).is_dir():
        return Path(override)

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "QGIS" / "QGIS3" / "profiles" / "default" / "python" / "plugins"
    elif sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "QGIS" / "QGIS3" / "profiles" / "default" / "python" / "plugins")

    return Path.home() / ".local" / "share" / "QGIS" / "QGIS3" / "profiles" / "default" / "python" / "plugins"


def is_installed(plugin_dir: Path) -> bool:
    """True when a ``waveshed`` plugin package is present in ``plugin_dir``."""
    return (plugin_dir / "waveshed").is_dir()


def installed_version(plugin_dir: Path) -> Optional[str]:
    """Return ``version`` from the installed metadata.txt, or None if absent."""
    meta = plugin_dir / "waveshed" / "metadata.txt"
    if not meta.is_file():
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read(meta, encoding="utf-8")
    except configparser.Error:
        return None
    return cfg.get("general", "version", fallback=None)


def installed_binaries(plugin_dir: Path) -> list:
    """Report each known engine binary under ``<plugin_dir>/waveshed/bin``.

    Returns one ``{"name", "required", "present"}`` record per binary in
    ``BINARIES`` (independent of the filesystem's contents otherwise).
    """
    bin_dir = plugin_dir / "waveshed" / "bin"
    return [
        {"name": name, "required": name in REQUIRED_BINARIES, "present": (bin_dir / name).is_file()}
        for name in BINARIES
    ]


def remove_plugin(plugin_dir: Path) -> Optional[Path]:
    """Delete ``<plugin_dir>/waveshed`` (including its bin/) if present.

    Returns the removed path, or None when nothing was installed. Refuses to
    delete the plugin source tree so a stray ``--plugin-dir`` can never wipe it.
    """
    target = plugin_dir / "waveshed"
    if target.resolve() == SOURCE_DIR.resolve():
        raise ValueError(f"refusing to remove the plugin source tree: {target}")
    if not target.exists():
        return None
    shutil.rmtree(target)
    return target


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="Test-deploy the Waveshed plugin into the local QGIS plugins dir.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--remove", action="store_true",
                      help="Uninstall the plugin from the plugins dir (never touches the source).")
    mode.add_argument("--status", action="store_true",
                      help="Report install location, version and staged binaries, then exit.")

    parser.add_argument("--plugin-dir", default="", metavar="PATH",
                        help="Override the target QGIS plugins dir (else config / auto-detect).")
    parser.add_argument("--binaries", default="", metavar="DIR",
                        help="Override the engine binary source dir (else aether_bin_dir from config).")
    parser.add_argument("--no-binaries", action="store_true",
                        help="Skip copying the engine binaries when installing.")
    parser.add_argument("--no-kill", action="store_true",
                        help="Do not close a running QGIS before deploying.")
    parser.add_argument("--no-launch", action="store_true",
                        help="Do not launch QGIS after deploying.")
    parser.add_argument("--headless", "--quiet", action="store_true", dest="headless",
                        help="Imply --no-kill and --no-launch (no QGIS process is touched).")
    parser.add_argument("--restart", action="store_true",
                        help="With --remove, also close QGIS first and relaunch it afterwards.")
    return parser


def run_install(args: argparse.Namespace) -> int:
    cfg = read_config()

    no_binaries = args.no_binaries or NO_BINARIES
    no_kill = args.no_kill or args.headless
    no_launch = args.no_launch or args.headless or NO_LAUNCH

    qgis_exe = cfg["qgis_exe"]
    if not no_launch and (not qgis_exe or not Path(qgis_exe).is_file()):
        print(f"[Error] qgis_exe not found: '{qgis_exe}'")
        return 1

    plugin_dir = resolve_plugin_dir(args.plugin_dir or cfg["plugin_dir"])
    target_dir = plugin_dir / "waveshed"
    bin_dir = args.binaries or cfg["aether_bin_dir"]

    print("=== AETHER QGIS Plugin Deploy ===")
    print(f"  Source:    {SOURCE_DIR}")
    print(f"  Target:    {target_dir}")
    if qgis_exe:
        print(f"  QGIS:      {qgis_exe}")
    if bin_dir:
        print(f"  Binaries:  {bin_dir}")
    print()

    # 1. Kill QGIS
    print("[1/4] Closing QGIS...", end=" ")
    if no_kill:
        print("skipped.")
    elif kill_qgis():
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
    if not no_binaries and bin_dir and Path(bin_dir).is_dir():
        print(f"[3/4] Copying binaries from {bin_dir} ...")
        records = copy_binaries(bin_dir, target_dir)
        missing_required = report_binaries(records, bin_dir)
        print(f"       {len(records)} binaries copied.")
        if missing_required:
            print()
            print("[WARN] Missing REQUIRED engine binaries: "
                  + ", ".join(sorted(missing_required)))
            print("       The plugin cannot run analyses without these. Build")
            print("       them in AETHER (rust/deploy.ps1) and re-run deploy.")
    elif no_binaries:
        print("[3/4] Skipping binary copy (--no-binaries).")
    else:
        print("[3/4] Skipping binary copy (aether_bin_dir not set or missing).")

    # 4. Launch QGIS
    if not no_launch:
        print("[4/4] Launching QGIS...", end=" ")
        launch_qgis(qgis_exe)
        print("done.")
    else:
        print("[4/4] Skipping QGIS launch.")

    print("\nDeploy complete.")
    return 0


def run_status(args: argparse.Namespace) -> int:
    cfg = read_config(required=False)
    plugin_dir = resolve_plugin_dir(args.plugin_dir or cfg["plugin_dir"])
    installed = is_installed(plugin_dir)
    version = installed_version(plugin_dir) if installed else None
    binaries = installed_binaries(plugin_dir)

    # Machine-readable (one key=value per line).
    print(f"plugin_dir={plugin_dir}")
    print(f"installed={'yes' if installed else 'no'}")
    print(f"version={version or ''}")
    for rec in binaries:
        tag = "REQUIRED" if rec["required"] else "optional"
        state = "present" if rec["present"] else "missing"
        print(f"binary.{rec['name']}={state} ({tag})")

    # Human-readable.
    print()
    print("=== Waveshed Plugin Status ===")
    print(f"  Plugin dir: {plugin_dir}")
    if installed:
        print(f"  Installed:  yes (version {version or 'unknown'})")
        print("  Binaries in bin/:")
        for rec in binaries:
            tag = "REQUIRED" if rec["required"] else "optional"
            state = "present" if rec["present"] else "MISSING"
            print(f"    - {rec['name']}: {state} ({tag})")
    else:
        print("  Installed:  no")
    return 0


def run_remove(args: argparse.Namespace) -> int:
    cfg = read_config(required=False)
    plugin_dir = resolve_plugin_dir(args.plugin_dir or cfg["plugin_dir"])
    target = plugin_dir / "waveshed"

    print("=== Waveshed Plugin Remove ===")
    print(f"  Plugin dir: {plugin_dir}")

    if args.restart:
        print("Closing QGIS...", end=" ")
        if kill_qgis():
            time.sleep(2)
            print("closed.")
        else:
            print("not running.")

    try:
        removed = remove_plugin(plugin_dir)
    except ValueError as exc:
        print(f"[Error] {exc}")
        return 1

    if removed is None:
        print(f"Nothing to remove ({target} not present).")
    else:
        print(f"Removed {removed}")

    if args.restart:
        qgis_exe = cfg["qgis_exe"]
        if qgis_exe and Path(qgis_exe).is_file():
            print("Relaunching QGIS...", end=" ")
            launch_qgis(qgis_exe)
            print("done.")
        else:
            print(f"[Warn] cannot relaunch QGIS, qgis_exe not found: '{qgis_exe}'")

    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        return run_status(args)
    if args.remove:
        return run_remove(args)
    return run_install(args)


if __name__ == "__main__":
    sys.exit(main())
